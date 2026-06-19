import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import requests
import json
import math

# --- CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(page_title="AquaRuta", page_icon="🗺️", layout="centered")

SHEET_URL = "https://docs.google.com/spreadsheets/d/1eUVLkgfuO1yBqECtXsxYbOf0YYBnGhFykljGokZVK4U/edit"

# Radio de exclusión de zonas inundadas (en grados lat/lon ≈ 100m)
RADIO_EXCLUSION_GRADOS = 0.0009

# --- ESTADO DE SESIÓN ---
defaults = {
    "ultimo_click_procesado": None,
    "ultimo_objeto_clickeado": None,
    "modo_ruta": False,
    "origen": None,
    "destino": None,
    "ruta_geojson": None,
    "ruta_info": None,
    "ruta_alternativa": False,
    "paso_seleccion": "origen",  # "origen" o "destino"
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# --- GOOGLE SHEETS ---
@st.cache_resource
def init_gspread():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    return gspread.authorize(credentials)

def limpiar_dataframe(df):
    if df.empty:
        return df
    df.columns = df.columns.str.strip()
    if "Latitud" in df.columns and "Longitud" in df.columns:
        df["Latitud"] = df["Latitud"].astype(str).str.replace(',', '.')
        df["Longitud"] = df["Longitud"].astype(str).str.replace(',', '.')
        df["Latitud"] = pd.to_numeric(df["Latitud"], errors='coerce')
        df["Longitud"] = pd.to_numeric(df["Longitud"], errors='coerce')
        df.loc[df["Latitud"] < -90, "Latitud"] = df["Latitud"] / 10000
        df.loc[df["Longitud"] < -180, "Longitud"] = df["Longitud"] / 10000
        df = df.dropna(subset=["Latitud", "Longitud"])
    if "Estado" in df.columns:
        df["Estado_clean"] = df["Estado"].astype(str).str.strip().str.lower()
    return df

@st.cache_data(ttl=30)
def obtener_calles():
    try:
        gc = init_gspread()
        sheet = gc.open_by_url(SHEET_URL).sheet1
        return limpiar_dataframe(pd.DataFrame(sheet.get_all_records()))
    except Exception as e:
        st.error(f"Error cargando calles: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=30)
def obtener_paraderos():
    try:
        gc = init_gspread()
        sheet = gc.open_by_url(SHEET_URL).worksheet("Hoja 2")
        return limpiar_dataframe(pd.DataFrame(sheet.get_all_records()))
    except Exception as e:
        st.error(f"Error cargando paraderos: {e}")
        return pd.DataFrame()

def actualizar_estado_db(fila_ref, nuevo_estado, nombre_pestana="sheet1"):
    try:
        with st.spinner("Actualizando base de datos..."):
            gc = init_gspread()
            doc = gc.open_by_url(SHEET_URL)
            sheet = doc.sheet1 if nombre_pestana == "sheet1" else doc.worksheet(nombre_pestana)
            valores_crudos = sheet.get_all_values()
            fila_a_modificar = None
            for i, r in enumerate(valores_crudos[1:], start=2):
                try:
                    r_lat = float(str(r[1]).replace(',', '.'))
                    r_lon = float(str(r[2]).replace(',', '.'))
                    if (abs(r_lat - fila_ref["Latitud"]) < 1e-4
                            and abs(r_lon - fila_ref["Longitud"]) < 1e-4
                            and str(r[4]).strip() == fila_ref["Estado"]):
                        fila_a_modificar = i
                        break
                except:
                    continue
            if fila_a_modificar:
                sheet.update_cell(fila_a_modificar, 5, nuevo_estado)
                hora_actual = datetime.now().strftime("%H:%M (%d/%m)")
                if len(valores_crudos[0]) >= 6 or sheet.col_count >= 6:
                    sheet.update_cell(fila_a_modificar, 6, hora_actual)
                obtener_calles.clear()
                obtener_paraderos.clear()
                st.success("¡Base de datos sincronizada!")
                st.session_state.ultimo_click_procesado = None
                st.session_state.ultimo_objeto_clickeado = None
                # Recalcular ruta si hay una activa
                if st.session_state.origen and st.session_state.destino:
                    st.session_state.ruta_geojson = None
                    st.session_state.ruta_info = None
                st.rerun()
            else:
                st.error("No se encontró el registro físico en las celdas.")
    except Exception as e:
        st.error(f"Error: {e}")

# =====================================================================
# MÓDULO DE ENRUTAMIENTO CON OSRM
# =====================================================================

def obtener_zonas_bloqueadas(calles_inundadas, paraderos_inundados=None):
    """Devuelve lista de (lat, lon) de todas las zonas a evitar."""
    zonas = []
    if calles_inundadas is not None and not calles_inundadas.empty:
        for _, f in calles_inundadas.iterrows():
            zonas.append((float(f["Latitud"]), float(f["Longitud"])))
    if paraderos_inundados is not None and not paraderos_inundados.empty:
        for _, p in paraderos_inundados.iterrows():
            zonas.append((float(p["Latitud"]), float(p["Longitud"])))
    return zonas

def ruta_pasa_por_zona(coordenadas_ruta, zonas_bloqueadas, radio=RADIO_EXCLUSION_GRADOS):
    """Verifica si algún punto de la ruta está dentro del radio de una zona bloqueada."""
    zonas_detectadas = []
    for lat_r, lon_r in coordenadas_ruta:
        for lat_z, lon_z in zonas_bloqueadas:
            dist = math.sqrt((lat_r - lat_z)**2 + (lon_r - lon_z)**2)
            if dist < radio:
                zonas_detectadas.append((lat_z, lon_z))
    return zonas_detectadas

def calcular_ruta_osrm(origen, destino, waypoints_extra=None):
    """
    Llama a la API pública de OSRM para calcular una ruta.
    origen/destino: (lat, lon)
    waypoints_extra: lista de (lat, lon) para desvíos
    Retorna: (geojson_coords, distancia_km, duracion_min) o None
    """
    puntos = [origen]
    if waypoints_extra:
        puntos.extend(waypoints_extra)
    puntos.append(destino)

    # OSRM espera lon,lat
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in puntos)
    url = (
        f"https://router.project-osrm.org/route/v1/driving/{coords_str}"
        f"?overview=full&geometries=geojson&steps=false"
    )
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        route = data["routes"][0]
        coords_geojson = route["geometry"]["coordinates"]  # [[lon, lat], ...]
        coords_latlon = [(c[1], c[0]) for c in coords_geojson]
        distancia_km = route["distance"] / 1000
        duracion_min = route["duration"] / 60
        return coords_latlon, distancia_km, duracion_min
    except Exception as e:
        return None

def generar_waypoints_desvio(zonas, origen, destino, offset=0.003):
    """
    Genera puntos intermedios para esquivar zonas inundadas.
    Desplaza perpendicularmente a la línea origen-destino.
    """
    if not zonas:
        return []

    # Vector dirección origen → destino
    dlat = destino[0] - origen[0]
    dlon = destino[1] - origen[1]
    norma = math.sqrt(dlat**2 + dlon**2)
    if norma == 0:
        return []

    # Vector perpendicular (rotado 90°)
    perp_lat = -dlon / norma
    perp_lon = dlat / norma

    waypoints = []
    for lat_z, lon_z in zonas:
        # Añadir waypoint desplazado perpendicularmente desde la zona bloqueada
        wp_lat = lat_z + perp_lat * offset
        wp_lon = lon_z + perp_lon * offset
        waypoints.append((wp_lat, wp_lon))

    # Ordenar waypoints según su proyección sobre la línea origen-destino
    def proyeccion(wp):
        return (wp[0] - origen[0]) * dlat + (wp[1] - origen[1]) * dlon
    waypoints.sort(key=proyeccion)
    return waypoints

def calcular_mejor_ruta(origen, destino, zonas_bloqueadas):
    """
    Calcula la ruta directa y verifica si pasa por zonas inundadas.
    Si hay conflicto, calcula ruta alternativa con waypoints de desvío.
    Retorna: dict con info de ruta
    """
    # 1. Ruta directa
    resultado_directo = calcular_ruta_osrm(origen, destino)
    if not resultado_directo:
        return None

    coords_directa, dist_directa, dur_directa = resultado_directo

    # 2. Verificar si pasa por zonas inundadas
    zonas_en_ruta = ruta_pasa_por_zona(coords_directa, zonas_bloqueadas)

    if not zonas_en_ruta:
        return {
            "coords": coords_directa,
            "distancia_km": dist_directa,
            "duracion_min": dur_directa,
            "es_alternativa": False,
            "zonas_evitadas": [],
            "coords_directa_bloqueada": None,
        }

    # 3. Calcular ruta alternativa evitando zonas inundadas
    waypoints = generar_waypoints_desvio(zonas_en_ruta, origen, destino)
    resultado_alt = calcular_ruta_osrm(origen, destino, waypoints)

    if not resultado_alt:
        # Si falla el desvío, devolver la directa con advertencia
        return {
            "coords": coords_directa,
            "distancia_km": dist_directa,
            "duracion_min": dur_directa,
            "es_alternativa": False,
            "zonas_evitadas": zonas_en_ruta,
            "coords_directa_bloqueada": coords_directa,
            "advertencia": "No se pudo calcular ruta alternativa",
        }

    coords_alt, dist_alt, dur_alt = resultado_alt
    zonas_en_alt = ruta_pasa_por_zona(coords_alt, zonas_bloqueadas)

    return {
        "coords": coords_alt,
        "distancia_km": dist_alt,
        "duracion_min": dur_alt,
        "es_alternativa": True,
        "zonas_evitadas": zonas_en_ruta,
        "coords_directa_bloqueada": coords_directa,
        "ruta_directa_dist": dist_directa,
        "ruta_directa_dur": dur_directa,
        "aun_pasa_por_zonas": len(zonas_en_alt) > 0,
    }

# =====================================================================
# MODALS
# =====================================================================

@st.dialog("🚨 Registrar Nueva Calle Inundada")
def modal_nueva_alerta(lat, lon):
    with st.spinner("Localizando nombre de la vía..."):
        try:
            geolocator = Nominatim(user_agent="alerta_austral_bot")
            location = geolocator.reverse((lat, lon), timeout=3)
            calle_detectada = (
                location.raw['address']['road']
                if location and 'road' in location.raw['address']
                else "Punto Registrado"
            )
        except Exception:
            calle_detectada = "Punto Registrado"
    st.info("📍 Coordenadas capturadas correctamente.")
    calle_final = st.text_input("Confirmar nombre de la calle:", value=calle_detectada)
    descripcion_incidente = st.text_input("Detalle del incidente:", value="Agua acumulada en calzada")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("❌ Cancelar", use_container_width=True):
            st.session_state.ultimo_click_procesado = None
            st.session_state.ultimo_objeto_clickeado = None
            st.rerun()
    with col2:
        if st.button("🚨 Guardar Alerta", type="primary", use_container_width=True):
            hora_reporte = datetime.now().strftime("%H:%M (%d/%m)")
            nueva_fila = [calle_final, str(lat), str(lon), descripcion_incidente, "Inundado", hora_reporte]
            try:
                gc = init_gspread()
                sheet = gc.open_by_url(SHEET_URL).sheet1
                sheet.insert_row(nueva_fila, index=2)
                obtener_calles.clear()
                if st.session_state.origen and st.session_state.destino:
                    st.session_state.ruta_geojson = None
                    st.session_state.ruta_info = None
                st.success("¡Alerta registrada! La ruta se recalculará automáticamente.")
                st.session_state.ultimo_click_procesado = None
                st.session_state.ultimo_objeto_clickeado = None
                st.rerun()
            except Exception as e:
                st.error(f"Error al guardar: {e}")

@st.dialog("🔄 Gestionar Calle Inundada")
def modal_eliminar_alerta(alerta):
    st.info(f"📍 **Calle:** {alerta.get('Lugar')}\n\n🕒 **Reportado a las:** {alerta.get('Hora', 'Sin Registro')}\n\n📝 **Detalle:** {alerta.get('Descripcion')}")
    st.markdown("<p style='text-align:center;font-weight:bold;'>¿El tránsito volvió a la normalidad?</p>", unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1:
        if st.button("❌ Cancelar", use_container_width=True):
            st.session_state.ultimo_click_procesado = None
            st.session_state.ultimo_objeto_clickeado = None
            st.rerun()
    with col2:
        if st.button("✅ Despejar Calle", type="primary", use_container_width=True):
            actualizar_estado_db(alerta, "Historial", "sheet1")

@st.dialog("🚏 Gestionar Paradero")
def modal_gestionar_paradero(paradero):
    st.info(f"📍 **Paradero:** {paradero.get('Lugar')}\n\n📝 **Detalle:** {paradero.get('Descripcion')}")
    estado_limpio = paradero.get("Estado_clean")
    if estado_limpio == "paradero normal":
        st.markdown("<p style='text-align:center;font-weight:bold;'>¿Qué problema presenta este paradero?</p>", unsafe_allow_html=True)
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🌊 Inundado", use_container_width=True):
                actualizar_estado_db(paradero, "Paradero Inundado", "Hoja 2")
        with col2:
            if st.button("⚠️ Mal Estado", use_container_width=True):
                actualizar_estado_db(paradero, "Paradero Mal Estado", "Hoja 2")
    else:
        st.markdown("<p style='text-align:center;font-weight:bold;'>¿Este paradero ya fue reparado o despejado?</p>", unsafe_allow_html=True)
        if st.button("✅ Volver a la Normalidad", type="primary", use_container_width=True):
            actualizar_estado_db(paradero, "Paradero Normal", "Hoja 2")
    if st.button("❌ Cerrar menú", use_container_width=True):
        st.session_state.ultimo_click_procesado = None
        st.session_state.ultimo_objeto_clickeado = None
        st.rerun()

@st.dialog("📍 Seleccionar Punto de Ruta")
def modal_seleccion_ruta(lat, lon):
    paso = st.session_state.paso_seleccion
    titulo = "🟢 Confirmar como ORIGEN" if paso == "origen" else "🔴 Confirmar como DESTINO"
    with st.spinner("Obteniendo nombre del lugar..."):
        try:
            geolocator = Nominatim(user_agent="alerta_austral_ruta_bot")
            location = geolocator.reverse((lat, lon), timeout=3)
            if location:
                addr = location.raw.get('address', {})
                nombre = addr.get('road', addr.get('neighbourhood', 'Punto seleccionado'))
            else:
                nombre = "Punto seleccionado"
        except:
            nombre = "Punto seleccionado"

    st.info(f"📌 **{nombre}**\n\nCoordenadas: {lat:.5f}, {lon:.5f}")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("❌ Cancelar", use_container_width=True):
            st.session_state.ultimo_click_procesado = None
            st.session_state.ultimo_objeto_clickeado = None
            st.rerun()
    with col2:
        if st.button(titulo, type="primary", use_container_width=True):
            if paso == "origen":
                st.session_state.origen = (lat, lon)
                st.session_state.origen_nombre = nombre
                st.session_state.paso_seleccion = "destino"
                st.session_state.ruta_geojson = None
                st.session_state.ruta_info = None
                st.toast("✅ Origen establecido. Ahora toca el destino en el mapa.", icon="🟢")
            else:
                st.session_state.destino = (lat, lon)
                st.session_state.destino_nombre = nombre
                st.session_state.paso_seleccion = "origen"
                st.session_state.ruta_geojson = None
                st.session_state.ruta_info = None
                st.toast("✅ Destino establecido. Calculando ruta...", icon="🔴")
            st.session_state.ultimo_click_procesado = None
            st.session_state.ultimo_objeto_clickeado = None
            st.rerun()

# =====================================================================
# CSS
# =====================================================================

st.markdown('''
<style>
.stApp { background-color: #1a1a1a; color: white !important; }
h1,h2,h3,h4,h5,h6,p,div,span,label,li,small,strong { color: #FFFFFF !important; }
.stTextInput input { background-color: #333333 !important; color: white !important; border: 1px solid #555 !important; padding: 12px !important; }
div[data-testid="stDialog"] div[role="dialog"] { background-color: #222 !important; border: 1px solid #555; border-radius: 12px; }
.status-card,.danger-card,.warning-card { background-color: #2b2b2b !important; border: 1px solid #444; padding: 15px; border-radius: 10px; margin-bottom: 12px; color: white !important; }
.danger-card { border-left: 5px solid #d9534f; }
.warning-card { border-left: 5px solid #f0ad4e; }
.ruta-card { background-color: #1e2d1e !important; border: 1px solid #2d6a2d; padding: 15px; border-radius: 10px; margin-bottom: 12px; }
.ruta-alt-card { background-color: #2d1e1e !important; border: 1px solid #8b2020; border-left: 5px solid #ff4444; padding: 15px; border-radius: 10px; margin-bottom: 12px; }
.main-header { font-family: 'Helvetica Neue', sans-serif; color: #FFFFFF !important; text-align: center; font-size: 2.2em; font-weight: bold; padding-bottom: 10px; margin-bottom: 15px; border-bottom: 2px dashed #FFFFFF; }
.ruta-badge { display: inline-block; background: #2563eb; color: white !important; padding: 2px 10px; border-radius: 12px; font-size: 0.8em; font-weight: bold; margin-left: 8px; }
.alt-badge { display: inline-block; background: #dc2626; color: white !important; padding: 2px 10px; border-radius: 12px; font-size: 0.8em; font-weight: bold; margin-left: 8px; }
.stButton button { border-radius: 8px !important; }
</style>
<div class="main-header">AquaRuta</div>
''', unsafe_allow_html=True)

# =====================================================================
# CARGA DE DATOS
# =====================================================================

df_calles = obtener_calles()
df_paraderos = obtener_paraderos()

calles_inundadas = (
    df_calles[df_calles["Estado_clean"] == "inundado"]
    if not df_calles.empty else pd.DataFrame()
)
paraderos_activos = df_paraderos if not df_paraderos.empty else pd.DataFrame()
paraderos_inundados = (
    paraderos_activos[paraderos_activos["Estado_clean"].isin(["paradero inundado"])]
    if not paraderos_activos.empty else pd.DataFrame()
)

# =====================================================================
# PANEL DE CONTROL DE RUTA
# =====================================================================

st.markdown("### 🗺️ Planificador de Rutas")

col_modo, col_limpiar = st.columns([3, 1])
with col_modo:
    modo_ruta = st.toggle(
        "🧭 Modo selección de ruta",
        value=st.session_state.modo_ruta,
        help="Activa para tocar el mapa y seleccionar origen/destino"
    )
    if modo_ruta != st.session_state.modo_ruta:
        st.session_state.modo_ruta = modo_ruta
        if not modo_ruta:
            st.session_state.origen = None
            st.session_state.destino = None
            st.session_state.ruta_geojson = None
            st.session_state.ruta_info = None
            st.session_state.paso_seleccion = "origen"
        st.rerun()

with col_limpiar:
    if st.button("🗑️ Limpiar", use_container_width=True):
        st.session_state.origen = None
        st.session_state.destino = None
        st.session_state.ruta_geojson = None
        st.session_state.ruta_info = None
        st.session_state.paso_seleccion = "origen"
        st.rerun()

# Indicador de selección paso a paso
if st.session_state.modo_ruta:
    paso_actual = st.session_state.paso_seleccion
    origen_nombre = getattr(st.session_state, 'origen_nombre', None)
    destino_nombre = getattr(st.session_state, 'destino_nombre', None)

    if not st.session_state.origen:
        st.info("🟢 **Paso 1:** Toca el mapa para marcar tu **ORIGEN**")
    elif not st.session_state.destino:
        st.success(f"✅ Origen: **{origen_nombre or 'Seleccionado'}**")
        st.info("🔴 **Paso 2:** Toca el mapa para marcar tu **DESTINO**")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.success(f"🟢 {origen_nombre or 'Origen'}")
        with c2:
            st.error(f"🔴 {destino_nombre or 'Destino'}")
else:
    st.caption("📱 *Toca una calle para reportar, o toca un Paradero (🚏) para administrarlo.*")

# =====================================================================
# CÁLCULO DE RUTA (cuando hay origen y destino)
# =====================================================================

if st.session_state.origen and st.session_state.destino and not st.session_state.ruta_info:
    zonas_bloqueadas = obtener_zonas_bloqueadas(calles_inundadas, paraderos_inundados)
    with st.spinner("🔄 Calculando ruta óptima..."):
        info = calcular_mejor_ruta(
            st.session_state.origen,
            st.session_state.destino,
            zonas_bloqueadas
        )
    if info:
        st.session_state.ruta_info = info
    else:
        st.error("No se pudo calcular la ruta. Verifica tu conexión a internet.")

# =====================================================================
# PANEL DE INFORMACIÓN DE RUTA
# =====================================================================

if st.session_state.ruta_info:
    info = st.session_state.ruta_info
    es_alt = info.get("es_alternativa", False)
    zonas_evitadas = info.get("zonas_evitadas", [])

    if es_alt and zonas_evitadas:
        st.markdown(f"""
        <div class="ruta-alt-card">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <strong>🔀 Ruta Alternativa Activa</strong>
                <span class="alt-badge">⚠️ DESVÍO</span>
            </div>
            <div style="margin-top:8px;font-size:0.9em;color:#ffaaaa !important;">
                🌊 Se detectaron <strong>{len(zonas_evitadas)} zona(s) inundada(s)</strong> en la ruta directa.<br>
                La ruta fue recalculada automáticamente para evitarlas.
            </div>
            <div style="margin-top:10px;display:flex;gap:20px;">
                <span>📏 {info['distancia_km']:.1f} km</span>
                <span>⏱️ ~{info['duracion_min']:.0f} min</span>
                <span style="font-size:0.85em;color:#aaa !important;">
                    vs ruta directa: {info.get('ruta_directa_dist', 0):.1f} km
                </span>
            </div>
        </div>
        """, unsafe_allow_html=True)

        if info.get("aun_pasa_por_zonas"):
            st.warning("⚠️ La ruta alternativa aún podría pasar cerca de zonas afectadas. Procede con precaución.")
    else:
        st.markdown(f"""
        <div class="ruta-card">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <strong>✅ Ruta Óptima</strong>
                <span class="ruta-badge">✓ LIBRE</span>
            </div>
            <div style="margin-top:8px;font-size:0.9em;color:#aaffaa !important;">
                La ruta no pasa por zonas inundadas registradas.
            </div>
            <div style="margin-top:10px;display:flex;gap:20px;">
                <span>📏 {info['distancia_km']:.1f} km</span>
                <span>⏱️ ~{info['duracion_min']:.0f} min</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

# =====================================================================
# CONSTRUCCIÓN DEL MAPA
# =====================================================================

centro_lat_pm = -41.4693
centro_lon_pm = -72.9423

mapa = folium.Map(
    location=[centro_lat_pm, centro_lon_pm],
    zoom_start=14,
    tiles="OpenStreetMap"
)

# Dibujar ruta directa bloqueada (gris punteado)
if st.session_state.ruta_info:
    info = st.session_state.ruta_info
    coords_bloqueada = info.get("coords_directa_bloqueada")
    if coords_bloqueada and info.get("es_alternativa"):
        folium.PolyLine(
            locations=coords_bloqueada,
            color="#888888",
            weight=3,
            opacity=0.4,
            dash_array="8 6",
            tooltip="Ruta directa bloqueada por inundaciones"
        ).add_to(mapa)

    # Dibujar ruta activa
    coords_ruta = info["coords"]
    color_ruta = "#ff4444" if info.get("es_alternativa") else "#2196F3"
    tooltip_ruta = "🔀 Ruta alternativa (evita inundaciones)" if info.get("es_alternativa") else "✅ Ruta óptima"
    folium.PolyLine(
        locations=coords_ruta,
        color=color_ruta,
        weight=5,
        opacity=0.85,
        tooltip=tooltip_ruta
    ).add_to(mapa)

    # Marcador de inicio
    if st.session_state.origen:
        folium.Marker(
            location=list(st.session_state.origen),
            icon=folium.Icon(color="green", icon="play", prefix="fa"),
            tooltip=f"🟢 Origen: {getattr(st.session_state, 'origen_nombre', 'Origen')}"
        ).add_to(mapa)

    # Marcador de destino
    if st.session_state.destino:
        folium.Marker(
            location=list(st.session_state.destino),
            icon=folium.Icon(color="red", icon="flag-checkered", prefix="fa"),
            tooltip=f"🔴 Destino: {getattr(st.session_state, 'destino_nombre', 'Destino')}"
        ).add_to(mapa)

elif st.session_state.origen and not st.session_state.destino:
    # Solo origen seleccionado
    folium.Marker(
        location=list(st.session_state.origen),
        icon=folium.Icon(color="green", icon="play", prefix="fa"),
        tooltip=f"🟢 Origen: {getattr(st.session_state, 'origen_nombre', 'Origen')}"
    ).add_to(mapa)

# Calles inundadas: círculo rojo con zona de exclusión visual
if not calles_inundadas.empty:
    for _, fila in calles_inundadas.iterrows():
        # Zona de exclusión translúcida
        folium.Circle(
            location=[float(fila["Latitud"]), float(fila["Longitud"])],
            radius=100,
            color="#ff0000",
            fill=True,
            fill_color="#ff0000",
            fill_opacity=0.12,
            weight=1,
            dash_array="4",
            tooltip="Zona excluida del enrutamiento"
        ).add_to(mapa)
        # Marcador principal
        folium.Circle(
            location=[float(fila["Latitud"]), float(fila["Longitud"])],
            radius=40,
            color="#d9534f",
            fill=True,
            fill_color="#d9534f",
            fill_opacity=0.6,
            tooltip=f"🌊 {fila.get('Lugar', 'Calle Inundada')} — Toca para gestionar"
        ).add_to(mapa)

# Paraderos
if not paraderos_activos.empty:
    for _, p in paraderos_activos.iterrows():
        estado_p = p["Estado_clean"]
        lat = float(p["Latitud"])
        lon = float(p["Longitud"])
        if estado_p == "paradero normal":
            folium.Marker(
                location=[lat, lon],
                icon=folium.Icon(color="blue", icon="bus", prefix="fa"),
                tooltip="🚏 Paradero Normal"
            ).add_to(mapa)
        elif estado_p == "paradero inundado":
            folium.Circle(
                location=[lat, lon], radius=80,
                color="#ff0000", fill=True, fill_color="#ff0000", fill_opacity=0.1,
                weight=1, dash_array="4"
            ).add_to(mapa)
            folium.Marker(
                location=[lat, lon],
                icon=folium.Icon(color="red", icon="bus", prefix="fa"),
                tooltip="🚨 Paradero Inundado"
            ).add_to(mapa)
        elif estado_p == "paradero mal estado":
            folium.Marker(
                location=[lat, lon],
                icon=folium.Icon(color="orange", icon="bus", prefix="fa"),
                tooltip="⚠️ Paradero en Mal Estado"
            ).add_to(mapa)

# Leyenda del mapa
legend_html = """
<div style="position:fixed;bottom:12px;left:12px;z-index:1000;
     background:#111;color:#eee;padding:8px 12px;border-radius:8px;
     font-size:11px;border:1px solid #333;max-width:200px;">
  <b>Leyenda</b><br>
  <span style="color:#2196F3">━━</span> Ruta óptima<br>
  <span style="color:#ff4444">━━</span> Ruta alternativa<br>
  <span style="color:#888">┅┅</span> Ruta bloqueada<br>
  <span style="color:#d9534f">●</span> Zona inundada<br>
  🟢 Origen &nbsp; 🔴 Destino
</div>
"""
mapa.get_root().html.add_child(folium.Element(legend_html))

mapa_salida = st_folium(mapa, width="100%", height=420, key="mapa_principal")

# =====================================================================
# PROCESAMIENTO DE CLICKS
# =====================================================================

click_mapa = mapa_salida.get("last_clicked")
click_objeto = mapa_salida.get("last_object_clicked")
click_a_procesar = None

if click_objeto and click_objeto != st.session_state.ultimo_objeto_clickeado:
    st.session_state.ultimo_objeto_clickeado = click_objeto
    click_a_procesar = click_objeto
elif click_mapa and click_mapa != st.session_state.ultimo_click_procesado:
    st.session_state.ultimo_click_procesado = click_mapa
    click_a_procesar = click_mapa

if click_a_procesar:
    lat_actual = click_a_procesar["lat"]
    lon_actual = click_a_procesar["lng"]

    # Buscar paradero coincidente
    paradero_coincidente = None
    if not paraderos_activos.empty:
        for _, p in paraderos_activos.iterrows():
            if abs(p["Latitud"] - lat_actual) < 0.0008 and abs(p["Longitud"] - lon_actual) < 0.0008:
                paradero_coincidente = p
                break

    # Buscar calle inundada coincidente
    alerta_coincidente = None
    if paradero_coincidente is None and not calles_inundadas.empty:
        for _, fila_activa in calles_inundadas.iterrows():
            if abs(fila_activa["Latitud"] - lat_actual) < 0.0008 and abs(fila_activa["Longitud"] - lon_actual) < 0.0008:
                alerta_coincidente = fila_activa
                break

    if st.session_state.modo_ruta:
        # En modo ruta: los clicks sirven para seleccionar origen/destino
        # (excepto si toca encima de un marcador existente)
        if paradero_coincidente is not None:
            modal_gestionar_paradero(paradero_coincidente)
        elif alerta_coincidente is not None:
            modal_eliminar_alerta(alerta_coincidente)
        else:
            modal_seleccion_ruta(lat_actual, lon_actual)
    else:
        # Modo normal: reportar/gestionar
        if paradero_coincidente is not None:
            modal_gestionar_paradero(paradero_coincidente)
        elif alerta_coincidente is not None:
            modal_eliminar_alerta(alerta_coincidente)
        else:
            modal_nueva_alerta(lat_actual, lon_actual)

# =====================================================================
# PANEL DE EMERGENCIAS
# =====================================================================

st.write("---")
lista_emergencias = []
if not calles_inundadas.empty:
    lista_emergencias.append(calles_inundadas)
if not paraderos_activos.empty:
    paraderos_con_problemas = paraderos_activos[
        paraderos_activos["Estado_clean"].isin(["paradero inundado", "paradero mal estado"])
    ]
    if not paraderos_con_problemas.empty:
        lista_emergencias.append(paraderos_con_problemas)

emergencias_activas = (
    pd.concat(lista_emergencias, ignore_index=True)
    if lista_emergencias else pd.DataFrame()
)

cantidad_alertas = len(emergencias_activas) if not emergencias_activas.empty else 0
st.markdown(f"### 📊 Emergencias Activas ({cantidad_alertas})")

if emergencias_activas.empty:
    st.info("✅ La ciudad no registra emergencias actualmente. Las rutas están libres.")
else:
    for _, alerta in emergencias_activas.iterrows():
        hora_display = (
            alerta.get('Hora', '---')
            if pd.notna(alerta.get('Hora')) and alerta.get('Hora') != ""
            else "---"
        )
        estado_alerta = alerta.get('Estado_clean', '')
        clase_css = "warning-card" if estado_alerta == "paradero mal estado" else "danger-card"
        icono = "⚠️" if estado_alerta == "paradero mal estado" else "🌊"

        # Verificar si esta zona afecta la ruta activa
        afecta_ruta = ""
        if st.session_state.ruta_info:
            zonas_evitadas = st.session_state.ruta_info.get("zonas_evitadas", [])
            for lat_z, lon_z in zonas_evitadas:
                if (abs(lat_z - float(alerta.get('Latitud', 0))) < 0.001
                        and abs(lon_z - float(alerta.get('Longitud', 0))) < 0.001):
                    afecta_ruta = " &nbsp;<span style='background:#8b1a1a;color:#ffaaaa;padding:1px 7px;border-radius:8px;font-size:0.8em;'>AFECTA TU RUTA</span>"
                    break

        st.markdown(f"""
        <div class="{clase_css}">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <strong>{icono} {alerta.get('Lugar', 'Punto Registrado')}{afecta_ruta}</strong>
                <span style="font-size:0.85em;color:#aaaaaa !important;font-weight:bold;
                      background-color:#333;padding:2px 8px;border-radius:5px;">🕒 {hora_display}</span>
            </div>
            <div style="margin-top:5px;">
                <span style="font-size:0.85em;color:#ffcccc !important;">{alerta.get('Descripcion', '')}</span><br>
                <span style="font-size:0.8em;color:#aaa !important;">
                    Coord: {alerta.get('Latitud', 0):.4f}, {alerta.get('Longitud', 0):.4f}
                </span>
            </div>
        </div>
        """, unsafe_allow_html=True)

# Pie de página informativo
st.markdown("""
<div style="text-align:center;margin-top:20px;padding:10px;
     border-top:1px solid #333;font-size:0.8em;color:#666 !important;">
    🔄 Las rutas se recalculan automáticamente cuando se registra una nueva inundación.<br>
    Enrutamiento vía <strong>OSRM</strong> (Open Source Routing Machine)
</div>
""", unsafe_allow_html=True)
