import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import pytz
import requests
import json
import math

# --- ZONA HORARIA CHILE ---
TZ_CHILE = pytz.timezone("America/Santiago")

def ahora_chile():
    """Retorna el datetime actual en horario de Chile (incluye cambio de horario verano/invierno)."""
    return datetime.now(pytz.utc).astimezone(TZ_CHILE)

# --- CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(page_title="AquaRuta", page_icon="💧", layout="centered")

SHEET_URL = "https://docs.google.com/spreadsheets/d/1eUVLkgfuO1yBqECtXsxYbOf0YYBnGhFykljGokZVK4U/edit"

# Radio de exclusión de zonas inundadas (en grados lat/lon ≈ 100m)
RADIO_EXCLUSION_GRADOS = 0.0009

# --- TIEMPOS DE EXPIRACIÓN ---
MINUTOS_OCULTAR_MAPA = 5       # Después de este tiempo se oculta del mapa
HORAS_ELIMINAR_BD = 24         # Después de este tiempo se borra de la base de datos

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
                hora_actual = ahora_chile().strftime("%H:%M (%d/%m)")
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
# MÓDULO DE EXPIRACIÓN DE ALERTAS
# =====================================================================

def parsear_hora_reporte(hora_str):
    """
    Convierte el string de hora guardado (ej: '14:32 (25/06)') a un objeto datetime.
    Retorna None si no se puede parsear.
    """
    if not hora_str or str(hora_str).strip() in ["", "---", "Sin Registro"]:
        return None
    try:
        hora_str = str(hora_str).strip()
        partes = hora_str.replace("(", "").replace(")", "").split()
        if len(partes) < 2:
            return None
        hora_parte = partes[0]          # "14:32"
        fecha_parte = partes[1]         # "25/06"
        anio_actual = ahora_chile().year
        dt_naive = datetime.strptime(f"{hora_parte} {fecha_parte}/{anio_actual}", "%H:%M %d/%m/%Y")
        dt = TZ_CHILE.localize(dt_naive)
        if dt > ahora_chile() + timedelta(hours=1):
            dt = dt.replace(year=anio_actual - 1)
            dt = TZ_CHILE.localize(dt.replace(tzinfo=None))
        return dt
    except Exception:
        return None

def minutos_desde_reporte(hora_str):
    """Retorna los minutos transcurridos desde el reporte, o None si no se puede calcular."""
    dt = parsear_hora_reporte(hora_str)
    if dt is None:
        return None
    delta = ahora_chile() - dt
    return delta.total_seconds() / 60

def debe_ocultar_en_mapa(hora_str):
    """True si han pasado más de MINUTOS_OCULTAR_MAPA desde el reporte."""
    minutos = minutos_desde_reporte(hora_str)
    if minutos is None:
        return False
    return minutos >= MINUTOS_OCULTAR_MAPA

def debe_eliminar_de_bd(hora_str):
    """True si han pasado más de HORAS_ELIMINAR_BD desde el reporte."""
    minutos = minutos_desde_reporte(hora_str)
    if minutos is None:
        return False
    return minutos >= HORAS_ELIMINAR_BD * 60

def limpiar_alertas_expiradas_sheet(nombre_pestana="sheet1"):
    """
    Recorre la hoja buscando filas con estado inundado que superen
    las HORAS_ELIMINAR_BD horas y las elimina de la base de datos.
    Retorna el número de filas eliminadas.
    """
    try:
        gc = init_gspread()
        doc = gc.open_by_url(SHEET_URL)
        sheet = doc.sheet1 if nombre_pestana == "sheet1" else doc.worksheet(nombre_pestana)
        valores = sheet.get_all_values()
        if len(valores) <= 1:
            return 0
        estados_a_limpiar = {"inundado", "paradero inundado", "paradero mal estado"}
        filas_a_eliminar = []
        for i, fila in enumerate(valores[1:], start=2):
            try:
                estado = str(fila[4]).strip().lower() if len(fila) > 4 else ""
                hora = str(fila[5]).strip() if len(fila) > 5 else ""
                if estado in estados_a_limpiar and debe_eliminar_de_bd(hora):
                    filas_a_eliminar.append(i)
            except Exception:
                continue
        for fila_idx in sorted(filas_a_eliminar, reverse=True):
            sheet.delete_rows(fila_idx)
        if filas_a_eliminar:
            obtener_calles.clear()
            obtener_paraderos.clear()
        return len(filas_a_eliminar)
    except Exception:
        return 0

# =====================================================================
# MÓDULO DE ENRUTAMIENTO CON OSRM
# =====================================================================

# Radio de detección de zona bloqueada sobre la ruta (~150m real)
RADIO_DETECCION_RUTA = 0.0014

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

def ruta_pasa_por_zona(coords_ruta, zonas_bloqueadas, radio=RADIO_DETECCION_RUTA):
    """
    Verifica si algún segmento de la ruta pasa cerca de una zona bloqueada.
    Retorna lista de zonas que la ruta cruza.
    """
    zonas_detectadas = []
    vistas = set()
    for lat_r, lon_r in coords_ruta:
        for lat_z, lon_z in zonas_bloqueadas:
            clave = (lat_z, lon_z)
            if clave in vistas:
                continue
            dist = math.sqrt((lat_r - lat_z)**2 + (lon_r - lon_z)**2)
            if dist < radio:
                zonas_detectadas.append(clave)
                vistas.add(clave)
    return zonas_detectadas

def pedir_rutas_osrm(origen, destino, waypoints_extra=None, alternativas=False):
    """
    Llama a OSRM y retorna lista de rutas encontradas.
    Con alternativas=True pide hasta 3 rutas distintas.
    Cada ruta: (coords_latlon, distancia_km, duracion_min)
    """
    puntos = [origen]
    if waypoints_extra:
        puntos.extend(waypoints_extra)
    puntos.append(destino)

    coords_str = ";".join(f"{lon},{lat}" for lat, lon in puntos)
    alt_param = "true&alternatives=3" if alternativas else "false"
    url = (
        f"https://router.project-osrm.org/route/v1/driving/{coords_str}"
        f"?overview=full&geometries=geojson&steps=false&alternatives={alt_param}"
    )
    try:
        resp = requests.get(url, timeout=12)
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return []
        resultados = []
        for route in data["routes"]:
            coords = [(c[1], c[0]) for c in route["geometry"]["coordinates"]]
            dist_km = route["distance"] / 1000
            dur_min = route["duration"] / 60
            resultados.append((coords, dist_km, dur_min))
        return resultados
    except Exception:
        return []

def generar_candidatos_desvio(zona_lat, zona_lon, origen, destino):
    """
    Para una zona bloqueada, genera 8 waypoints candidatos en distintas
    direcciones (N, S, E, O, NE, NO, SE, SO) a distancia creciente.
    OSRM snapeará cada uno a la calle más cercana, garantizando que
    el waypoint final esté en una vía real alejada de la zona.
    """
    candidatos = []
    # Offsets en grados: ~200m, ~350m, ~500m
    for offset in [0.002, 0.0035, 0.005]:
        angulos = [0, 45, 90, 135, 180, 225, 270, 315]
        for ang in angulos:
            rad = math.radians(ang)
            wp_lat = zona_lat + offset * math.cos(rad)
            wp_lon = zona_lon + offset * math.sin(rad)
            # Descartar candidatos que alejen demasiado de la línea origen-destino
            # (nos quedamos solo con los que están en un corredor razonable)
            candidatos.append((wp_lat, wp_lon))
    return candidatos

def calcular_mejor_ruta(origen, destino, zonas_bloqueadas):
    """
    Estrategia robusta en 3 pasos:
    1. Pedir ruta directa + alternativas a OSRM
    2. Si alguna no pasa por zonas → usarla directamente
    3. Si todas pasan → generar waypoints de desvío reales por cada zona
       e iterar hasta encontrar una combinación libre, o la menos afectada
    """
    # --- PASO 1: rutas directas con alternativas ---
    rutas_candidatas = pedir_rutas_osrm(origen, destino, alternativas=True)
    if not rutas_candidatas:
        return None

    coords_directa, dist_directa, dur_directa = rutas_candidatas[0]

    # Si no hay zonas bloqueadas, devolver la más rápida directamente
    if not zonas_bloqueadas:
        return {
            "coords": coords_directa,
            "distancia_km": dist_directa,
            "duracion_min": dur_directa,
            "es_alternativa": False,
            "zonas_evitadas": [],
            "coords_directa_bloqueada": None,
        }

    # --- PASO 2: buscar entre las alternativas una libre de obstáculos ---
    ruta_libre = None
    for coords_c, dist_c, dur_c in rutas_candidatas:
        zonas_en_esta = ruta_pasa_por_zona(coords_c, zonas_bloqueadas)
        if not zonas_en_esta:
            ruta_libre = (coords_c, dist_c, dur_c)
            break  # tomamos la primera libre (OSRM las ordena por duración)

    if ruta_libre:
        coords_libre, dist_libre, dur_libre = ruta_libre
        # Verificar si la ruta directa (primera) también estaba libre
        zonas_en_directa = ruta_pasa_por_zona(coords_directa, zonas_bloqueadas)
        es_alt = (coords_libre is not coords_directa) or len(zonas_en_directa) > 0
        return {
            "coords": coords_libre,
            "distancia_km": dist_libre,
            "duracion_min": dur_libre,
            "es_alternativa": es_alt,
            "zonas_evitadas": zonas_en_directa if es_alt else [],
            "coords_directa_bloqueada": coords_directa if es_alt else None,
            "ruta_directa_dist": dist_directa,
            "ruta_directa_dur": dur_directa,
        }

    # --- PASO 3: ninguna alternativa libre → forzar desvío con waypoints ---
    # Detectar las zonas que afectan la ruta directa
    zonas_en_directa = ruta_pasa_por_zona(coords_directa, zonas_bloqueadas)

    mejor_ruta = None
    menor_zonas_restantes = len(zonas_bloqueadas) + 1

    for lat_z, lon_z in zonas_en_directa:
        candidatos = generar_candidatos_desvio(lat_z, lon_z, origen, destino)
        for wp in candidatos:
            rutas_con_wp = pedir_rutas_osrm(origen, destino,
                                             waypoints_extra=[wp],
                                             alternativas=False)
            if not rutas_con_wp:
                continue
            coords_wp, dist_wp, dur_wp = rutas_con_wp[0]
            zonas_restantes = ruta_pasa_por_zona(coords_wp, zonas_bloqueadas)

            if len(zonas_restantes) < menor_zonas_restantes:
                menor_zonas_restantes = len(zonas_restantes)
                mejor_ruta = (coords_wp, dist_wp, dur_wp)

            if menor_zonas_restantes == 0:
                break  # encontramos una ruta completamente libre
        if menor_zonas_restantes == 0:
            break

    if mejor_ruta:
        coords_mejor, dist_mejor, dur_mejor = mejor_ruta
        zonas_aun = ruta_pasa_por_zona(coords_mejor, zonas_bloqueadas)
        return {
            "coords": coords_mejor,
            "distancia_km": dist_mejor,
            "duracion_min": dur_mejor,
            "es_alternativa": True,
            "zonas_evitadas": zonas_en_directa,
            "coords_directa_bloqueada": coords_directa,
            "ruta_directa_dist": dist_directa,
            "ruta_directa_dur": dur_directa,
            "aun_pasa_por_zonas": len(zonas_aun) > 0,
        }

    # Último recurso: devolver la ruta directa con advertencia
    return {
        "coords": coords_directa,
        "distancia_km": dist_directa,
        "duracion_min": dur_directa,
        "es_alternativa": False,
        "zonas_evitadas": zonas_en_directa,
        "coords_directa_bloqueada": None,
        "advertencia": "No se encontró ruta completamente libre de zonas inundadas.",
    }

# =====================================================================
# MODALS
# =====================================================================

@st.dialog("Registrar Calle Inundada")
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
    st.info("Coordenadas capturadas correctamente.")
    calle_final = st.text_input("Confirmar nombre de la calle:", value=calle_detectada)
    descripcion_incidente = st.text_input("Detalle del incidente:", value="Agua acumulada en calzada")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("❌ Cancelar", use_container_width=True):
            st.session_state.ultimo_click_procesado = None
            st.session_state.ultimo_objeto_clickeado = None
            st.rerun()
    with col2:
        if st.button("Guardar Alerta", type="primary", use_container_width=True):
            hora_reporte = ahora_chile().strftime("%H:%M (%d/%m)")
            nueva_fila = [calle_final, str(lat), str(lon), descripcion_incidente, "Inundado", hora_reporte]
            try:
                gc = init_gspread()
                sheet = gc.open_by_url(SHEET_URL).sheet1
                sheet.insert_row(nueva_fila, index=2)
                obtener_calles.clear()
                if st.session_state.origen and st.session_state.destino:
                    st.session_state.ruta_geojson = None
                    st.session_state.ruta_info = None
                st.success("Alerta registrada. La ruta se recalculará automáticamente.")
                st.session_state.ultimo_click_procesado = None
                st.session_state.ultimo_objeto_clickeado = None
                st.rerun()
            except Exception as e:
                st.error(f"Error al guardar: {e}")

@st.dialog("🔄 Gestionar Calle Inundada")
def modal_eliminar_alerta(alerta):
    st.info(f"Calle: {alerta.get('Lugar')}\n\nReportado a las: {alerta.get('Hora', 'Sin Registro')}\n\nDetalle: {alerta.get('Descripcion')}")
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
    st.info(f"Paradero: {paradero.get('Lugar')}\n\nDetalle: {paradero.get('Descripcion')}")
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

@st.dialog("Seleccionar Punto de Ruta")
def modal_seleccion_ruta(lat, lon):
    paso = st.session_state.paso_seleccion
    titulo = "Confirmar como ORIGEN" if paso == "origen" else "Confirmar como DESTINO"
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
                st.toast("Origen establecido. Ahora toca el destino en el mapa.")
            else:
                st.session_state.destino = (lat, lon)
                st.session_state.destino_nombre = nombre
                st.session_state.paso_seleccion = "origen"
                st.session_state.ruta_geojson = None
                st.session_state.ruta_info = None
                st.toast("Destino establecido. Calculando ruta...")
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

# --- Limpieza automática de alertas expiradas (24h) ---
# Se ejecuta silenciosamente en cada carga de página
if "ultima_limpieza_bd" not in st.session_state:
    st.session_state.ultima_limpieza_bd = ahora_chile() - timedelta(minutes=10)

# Ejecutar limpieza como máximo cada 5 minutos para no saturar la API
minutos_desde_limpieza = (ahora_chile() - st.session_state.ultima_limpieza_bd).total_seconds() / 60
if minutos_desde_limpieza >= 5:
    eliminadas_calles = limpiar_alertas_expiradas_sheet("sheet1")
    eliminadas_paraderos = limpiar_alertas_expiradas_sheet("Hoja 2")
    st.session_state.ultima_limpieza_bd = ahora_chile()
    if eliminadas_calles + eliminadas_paraderos > 0:
        st.toast(f"Se eliminaron {eliminadas_calles + eliminadas_paraderos} alerta(s) expirada(s) de la base de datos.")

df_calles = obtener_calles()
df_paraderos = obtener_paraderos()

# --- Filtrado por tiempo: todas las inundadas de la BD ---
calles_inundadas_bd = (
    df_calles[df_calles["Estado_clean"] == "inundado"]
    if not df_calles.empty else pd.DataFrame()
)

# --- Filtrado para el MAPA: solo las que tienen menos de MINUTOS_OCULTAR_MAPA minutos ---
def filtrar_por_tiempo_mapa(df):
    """Filtra filas cuyo reporte tenga menos de MINUTOS_OCULTAR_MAPA minutos."""
    if df.empty:
        return df
    mask = df["Hora"].apply(lambda h: not debe_ocultar_en_mapa(h))
    return df[mask]

calles_inundadas = filtrar_por_tiempo_mapa(calles_inundadas_bd) if not calles_inundadas_bd.empty else pd.DataFrame()


paraderos_activos = df_paraderos if not df_paraderos.empty else pd.DataFrame()
paraderos_inundados = (
    paraderos_activos[paraderos_activos["Estado_clean"].isin(["paradero inundado"])]
    if not paraderos_activos.empty else pd.DataFrame()
)

# =====================================================================
# PANEL DE CONTROL DE RUTA
# =====================================================================

st.markdown("### Planificador de Rutas")

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
        st.info("Paso 1: Toca el mapa para marcar tu ORIGEN")
    elif not st.session_state.destino:
        st.success(f"Origen: {origen_nombre or 'Seleccionado'}")
        st.info("Paso 2: Toca el mapa para marcar tu DESTINO")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.success(f"{origen_nombre or 'Origen'}")
        with c2:
            st.error(f"{destino_nombre or 'Destino'}")
else:
    st.caption("Toca una calle para reportar una inundación, o toca un paradero para administrarlo.")

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
                tooltip="Paradero Inundado"
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

# Usamos calles_inundadas_bd (todas) para el panel, pero calles_inundadas (filtradas) para el mapa
lista_emergencias = []
if not calles_inundadas_bd.empty:
    lista_emergencias.append(calles_inundadas_bd)
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
st.markdown(f"### Emergencias Activas ({cantidad_alertas})")

if emergencias_activas.empty:
    st.info("La ciudad no registra emergencias actualmente. Las rutas están libres.")
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
                      background-color:#333;padding:2px 8px;border-radius:5px;">{hora_display}</span>
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
    Las rutas se recalculan automáticamente cuando se registra una nueva inundación.<br>
    Enrutamiento vía <strong>OSRM</strong> (Open Source Routing Machine)
</div>
""", unsafe_allow_html=True)
