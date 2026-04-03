"""
=============================================================
  SIE Pizarra — Colector con Detección de Cambios
  Guarda en Supabase (nube) + SQLite (local como respaldo)
=============================================================
Uso:
    python collector.py            # una ejecución (para GitHub Actions)
    python collector.py --loop     # loop continuo (para correr en tu PC)
    python collector.py --resumen  # ver estadísticas de la DB local
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

# Estas variables las define GitHub Actions automáticamente desde los Secrets.
# Para correr local, crea un archivo .env o defínelas en tu sistema.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")   # https://xxxx.supabase.co
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")   # tu anon/service key

TABLA_SUPABASE = "generacion"
DB_LOCAL       = Path("sie_generacion.db")
HASH_FILE      = Path(".ultimo_hash")      # guarda el hash del último dato
LOOP_SEGUNDOS  = 120                       # en modo --loop, consulta cada 2 min

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
    "User-Agent"      : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    "Accept"          : "application/json, text/plain, */*",
    "Referer"         : "https://pizarra.sie.gob.do/",
    "Origin"          : "https://pizarra.sie.gob.do",
}


# ── DETECCIÓN DE CAMBIOS ──────────────────────────────────────

def calcular_hash(datos: list) -> str:
    """Hash MD5 del contenido. Si cambia un solo valor, el hash cambia."""
    contenido = json.dumps(datos, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(contenido.encode()).hexdigest()


def leer_ultimo_hash() -> str:
    if HASH_FILE.exists():
        return HASH_FILE.read_text().strip()
    return ""


def guardar_hash(h: str):
    HASH_FILE.write_text(h)


def datos_son_nuevos(datos: list) -> bool:
    """True si los datos cambiaron respecto a la última vez."""
    nuevo_hash = calcular_hash(datos)
    if nuevo_hash == leer_ultimo_hash():
        return False
    guardar_hash(nuevo_hash)
    return True


# ── FETCH ─────────────────────────────────────────────────────

def fetch_api():
    """Llama al API de la Pizarra SIE."""
    try:
        r = requests.get(API_URL, headers=HEADERS_API, timeout=20)
        if r.status_code == 200:
            datos = r.json()
            # Normalizar a lista de filas
            if isinstance(datos, list):
                return datos
            if isinstance(datos, dict):
                for v in datos.values():
                    if isinstance(v, list) and v:
                        return v
                return [datos]
        log.error(f"API respondió HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Error consultando API: {e}")
    return None


# ── SUPABASE ──────────────────────────────────────────────────

def supabase_insertar(filas: list[dict]) -> bool:
    """Inserta las filas en Supabase via REST API."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.warning("Supabase no configurado — solo guardando local.")
        return False

    url = f"{SUPABASE_URL}/rest/v1/{TABLA_SUPABASE}"
    headers = {
        "apikey"        : SUPABASE_KEY,
        "Authorization" : f"Bearer {SUPABASE_KEY}",
        "Content-Type"  : "application/json",
        "Prefer"        : "return=minimal",
    }

    try:
        r = requests.post(url, headers=headers, json=filas, timeout=20)
        if r.status_code in (200, 201):
            return True
        log.error(f"Supabase error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Error conectando a Supabase: {e}")
    return False


# ── SQLITE LOCAL (respaldo) ───────────────────────────────────

def sqlite_insertar(filas: list[dict], descargado_en: str):
    """Guarda en SQLite local. Crea columnas nuevas si el API las devuelve."""
    with sqlite3.connect(DB_LOCAL) as conn:
        # Crear tabla si no existe
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generacion (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                descargado_en TEXT NOT NULL,
                insertado_en  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)

        # Agregar columnas dinámicamente
        existentes = {r[1] for r in conn.execute("PRAGMA table_info(generacion)")}
        for col in filas[0].keys():
            if col not in existentes:
                conn.execute(f"ALTER TABLE generacion ADD COLUMN [{col}] TEXT")
                log.info(f"  Nueva columna: '{col}'")

        # Insertar filas
        for fila in filas:
            cols   = list(fila.keys()) + ["descargado_en"]
            vals   = [str(v) if v is not None else None for v in fila.values()] + [descargado_en]
            ph     = ", ".join(["?"] * len(cols))
            cols_s = ", ".join(f"[{c}]" for c in cols)
            conn.execute(f"INSERT INTO generacion ({cols_s}) VALUES ({ph})", vals)

        conn.commit()


# ── CICLO PRINCIPAL ───────────────────────────────────────────

def ejecutar():
    """
    Una ejecución completa:
    1. Consultar API
    2. Verificar si cambió
    3. Si cambió → guardar en Supabase + SQLite
    """
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("Consultando API...")

    filas = fetch_api()
    if not filas:
        log.error("Sin datos del API.")
        return

    if not datos_son_nuevos(filas):
        log.info(f"Sin cambios en los datos. ({len(filas)} filas, misma data)")
        return

    log.info(f"¡Datos nuevos detectados! {len(filas)} filas — guardando...")

    # Agregar timestamp a cada fila
    filas_con_ts = [{**f, "descargado_en": ahora} for f in filas]

    # Guardar en Supabase
    ok_nube = supabase_insertar(filas_con_ts)
    if ok_nube:
        log.info(f"  ✅ Supabase: {len(filas)} filas insertadas")
    else:
        log.warning("  ⚠  Supabase falló — solo guardando local")

    # Guardar en SQLite (siempre)
    sqlite_insertar(filas, ahora)
    log.info(f"  ✅ SQLite local: {len(filas)} filas  |  {DB_LOCAL}")


# ── RESUMEN ───────────────────────────────────────────────────

def resumen():
    if not DB_LOCAL.exists():
        print("⚠  No hay base de datos local todavía.")
        return

    with sqlite3.connect(DB_LOCAL) as conn:
        total  = conn.execute("SELECT COUNT(*) FROM generacion").fetchone()[0]
        primero = conn.execute("SELECT MIN(descargado_en) FROM generacion").fetchone()[0]
        ultimo  = conn.execute("SELECT MAX(descargado_en) FROM generacion").fetchone()[0]
        cols   = [r[1] for r in conn.execute("PRAGMA table_info(generacion)")]

    tam = DB_LOCAL.stat().st_size / 1_048_576
    print(f"\n{'='*50}")
    print(f"  Base de Datos SIE — Resumen")
    print(f"{'='*50}")
    print(f"  Archivo  : {DB_LOCAL}  ({tam:.2f} MB)")
    print(f"  Registros: {total:,}")
    print(f"  Desde    : {primero}")
    print(f"  Hasta    : {ultimo}")
    print(f"  Columnas : {', '.join(c for c in cols if c not in ('id','insertado_en'))}")
    print(f"{'='*50}\n")


# ── MAIN ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop",    action="store_true", help="Loop continuo cada 2 min")
    parser.add_argument("--resumen", action="store_true", help="Ver estadísticas locales")
    args = parser.parse_args()

    if args.resumen:
        resumen()
        return

    if args.loop:
        log.info(f"Modo loop — consultando cada {LOOP_SEGUNDOS}s. Ctrl+C para detener.")
        try:
            while True:
                ejecutar()
                time.sleep(LOOP_SEGUNDOS)
        except KeyboardInterrupt:
            log.info("Detenido.")
            resumen()
    else:
        # Modo una sola ejecución (usado por GitHub Actions)
        ejecutar()


if __name__ == "__main__":
    main()
