"""
=============================================================
  SIE Pizarra — Colector Completo
  Captura: plantas renovables, térmicas, totales por
  tecnología y serie horaria programado vs ejecutado
=============================================================
Uso:
    python collector.py            # una ejecución (GitHub Actions)
    python collector.py --loop     # loop continuo en tu PC
    python collector.py --resumen  # ver estadísticas locales
"""

import os
import hashlib
import json
import sqlite3
import time
import argparse
import logging
import requests
from datetime import datetime
from pathlib import Path

# ── CONFIGURACIÓN ─────────────────────────────────────────────
API_URL = "https://pizarradev.sie.gob.do/v1/Generacion/PDespachoPotencia"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

DB_LOCAL      = Path("sie_generacion.db")
HASH_FILE     = Path(".ultimo_hash")
LOOP_SEGUNDOS = 120   # en modo --loop, consulta cada 2 minutos

# ── LOGGING ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("collector.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

HEADERS_API = {
    "User-Agent"  : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    "Accept"      : "application/json, text/plain, */*",
    "Referer"     : "https://pizarra.sie.gob.do/",
    "Origin"      : "https://pizarra.sie.gob.do",
}


# ── DETECCIÓN DE CAMBIOS ──────────────────────────────────────

def calcular_hash(datos) -> str:
    contenido = json.dumps(datos, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(contenido.encode()).hexdigest()

def leer_ultimo_hash() -> str:
    return HASH_FILE.read_text().strip() if HASH_FILE.exists() else ""

def guardar_hash(h: str):
    HASH_FILE.write_text(h)

def datos_son_nuevos(datos) -> bool:
    h = calcular_hash(datos)
    if h == leer_ultimo_hash():
        return False
    guardar_hash(h)
    return True


# ── FETCH ─────────────────────────────────────────────────────

def fetch_api():
    try:
        r = requests.get(API_URL, headers=HEADERS_API, timeout=20)
        r.encoding = "utf-8"
        if r.status_code == 200:
            return r.json()
        log.error(f"API HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Error API: {e}")
    return None


# ── PARSEO DEL JSON ───────────────────────────────────────────

def parsear(datos: dict, descargado_en: str) -> dict:
    """
    Extrae las 4 secciones del JSON en listas de filas listas
    para insertar en DB.
    """

    # 1. PLANTAS — renovables + noRenovables combinadas
    plantas = []
    for tipo_fuente, lista in [("renovable", datos.get("renovables", [])),
                                ("noRenovable", datos.get("noRenovables", []))]:
        for p in lista:
            plantas.append({
                "central"      : p.get("central"),
                "generado"     : p.get("generado"),
                "generadoAnt"  : p.get("generadoAnt"),
                "porcentaje"   : p.get("porcentaje"),
                "grupo"        : p.get("grupo"),
                "tipo_fuente"  : tipo_fuente,
                "descargado_en": descargado_en,
            })

    # 2. TOTALES POR TECNOLOGÍA (resumen del snapshot actual)
    tecnologia = []
    for nombre, val in datos.get("tipoTecnologia", {}).items():
        tecnologia.append({
            "tecnologia"   : nombre,
            "generado"     : val.get("generado"),
            "generadoAnt"  : val.get("generadoAnt"),
            "porcentaje"   : val.get("porcentaje"),
            "descargado_en": descargado_en,
        })

    # 3. SERIE HORARIA — programado vs ejecutado (solo grupo "Dia" para evitar duplicados)
    #    Contiene el día actual hora por hora
    horaria = []
    for grupo_data in datos.get("programadoVsEjecutado", []):
        if grupo_data.get("grupo") != "Dia":
            continue
        labels    = grupo_data.get("labels", [])
        generados = grupo_data.get("generado", [])
        programas = grupo_data.get("programado", [])
        for i, label in enumerate(labels):
            horaria.append({
                "fecha"        : label,
                "generado"     : generados[i] if i < len(generados) else None,
                "programado"   : programas[i] if i < len(programas) else None,
                "descargado_en": descargado_en,
            })

    return {"plantas": plantas, "tecnologia": tecnologia, "horaria": horaria}


# ── SUPABASE ──────────────────────────────────────────────────

def supabase_post(tabla: str, filas: list) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY or not filas:
        return False
    url = f"{SUPABASE_URL}/rest/v1/{tabla}"
    headers = {
        "apikey"       : SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type" : "application/json",
        "Prefer"       : "return=minimal",
    }
    try:
        r = requests.post(url, headers=headers, json=filas, timeout=20)
        return r.status_code in (200, 201)
    except Exception as e:
        log.error(f"Supabase {tabla}: {e}")
        return False


# ── SQLITE LOCAL ──────────────────────────────────────────────

def sqlite_insertar(tabla: str, filas: list, conn: sqlite3.Connection):
    if not filas:
        return
    # Crear tabla si no existe con columna id y timestamp
    cols_base = list(filas[0].keys())
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {tabla} (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            insertado_en TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # Agregar columnas dinámicamente
    existentes = {r[1] for r in conn.execute(f"PRAGMA table_info({tabla})")}
    for col in cols_base:
        if col not in existentes:
            conn.execute(f"ALTER TABLE {tabla} ADD COLUMN [{col}] TEXT")

    for fila in filas:
        cols = list(fila.keys())
        vals = [str(v) if v is not None else None for v in fila.values()]
        ph   = ", ".join(["?"] * len(cols))
        cs   = ", ".join(f"[{c}]" for c in cols)
        conn.execute(f"INSERT INTO {tabla} ({cs}) VALUES ({ph})", vals)
    conn.commit()


# ── CICLO PRINCIPAL ───────────────────────────────────────────

def ejecutar():
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("Consultando API...")

    datos = fetch_api()
    if not datos:
        log.error("Sin datos del API.")
        return

    if not datos_son_nuevos(datos):
        n_plantas = len(datos.get("renovables", [])) + len(datos.get("noRenovables", []))
        log.info(f"Sin cambios. ({n_plantas} plantas, misma data)")
        return

    parsed = parsear(datos, ahora)
    n_p = len(parsed["plantas"])
    n_t = len(parsed["tecnologia"])
    n_h = len(parsed["horaria"])
    log.info(f"¡Datos nuevos! {n_p} plantas | {n_t} tecnologías | {n_h} horas")

    # ── Supabase ──
    ok1 = supabase_post("generacion_plantas", parsed["plantas"])
    ok2 = supabase_post("tecnologia_resumen", parsed["tecnologia"])
    ok3 = supabase_post("generacion_horaria", parsed["horaria"])

    if SUPABASE_URL:
        estados = f"plantas={'✅' if ok1 else '❌'}  tecnologia={'✅' if ok2 else '❌'}  horaria={'✅' if ok3 else '❌'}"
        log.info(f"  Supabase → {estados}")
    else:
        log.warning("  Supabase no configurado — solo guardando local")

    # ── SQLite local ──
    with sqlite3.connect(DB_LOCAL) as conn:
        sqlite_insertar("generacion_plantas", parsed["plantas"], conn)
        sqlite_insertar("tecnologia_resumen", parsed["tecnologia"], conn)
        sqlite_insertar("generacion_horaria", parsed["horaria"], conn)
    log.info(f"  SQLite ✅  |  {DB_LOCAL}")


# ── RESUMEN ───────────────────────────────────────────────────

def resumen():
    if not DB_LOCAL.exists():
        print("⚠  No hay base de datos local todavía.")
        return
    with sqlite3.connect(DB_LOCAL) as conn:
        tablas = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        print(f"\n{'='*50}")
        print(f"  Base de Datos SIE — {DB_LOCAL}  ({DB_LOCAL.stat().st_size/1e6:.2f} MB)")
        print(f"{'='*50}")
        for t in tablas:
            try:
                n     = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                desde = conn.execute(f"SELECT MIN(descargado_en) FROM {t}").fetchone()[0]
                hasta = conn.execute(f"SELECT MAX(descargado_en) FROM {t}").fetchone()[0]
                print(f"  {t:<25} {n:>7,} filas  |  {desde} → {hasta}")
            except Exception:
                pass
        print(f"{'='*50}\n")


# ── MAIN ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop",    action="store_true")
    parser.add_argument("--resumen", action="store_true")
    args = parser.parse_args()

    if args.resumen:
        resumen()
        return

    if args.loop:
        log.info(f"Loop — consultando cada {LOOP_SEGUNDOS}s. Ctrl+C para detener.")
        try:
            while True:
                ejecutar()
                time.sleep(LOOP_SEGUNDOS)
        except KeyboardInterrupt:
            log.info("Detenido.")
            resumen()
    else:
        ejecutar()


if __name__ == "__main__":
    main()
