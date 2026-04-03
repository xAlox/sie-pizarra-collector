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
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://pizarradev.sie.gob.do/v1/Generacion/PDespachoPotencia"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

DB_LOCAL = Path("sie_generacion.db")
HASH_FILE = Path(".ultimo_hash")
LOOP_SEGUNDOS = 120

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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://pizarra.sie.gob.do/",
    "Origin": "https://pizarra.sie.gob.do",
}


# ── HASH ──────────────────────────────────────────────────────

def calcular_hash(datos) -> str:
    contenido = json.dumps(datos, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(contenido.encode("utf-8")).hexdigest()

def leer_ultimo_hash() -> str:
    if HASH_FILE.exists():
        return HASH_FILE.read_text(encoding="utf-8").strip()
    return ""

def guardar_hash(h: str):
    HASH_FILE.write_text(h, encoding="utf-8")

def datos_son_nuevos(datos) -> tuple[bool, str]:
    h = calcular_hash(datos)
    return h != leer_ultimo_hash(), h


# ── FETCH ─────────────────────────────────────────────────────

def fetch_api():
    try:
        r = requests.get(API_URL, headers=HEADERS_API, timeout=20)
        r.raise_for_status()

        # Forzar lectura correcta en UTF-8
        texto = r.content.decode("utf-8", errors="replace")
        return json.loads(texto)

    except Exception as e:
        log.error(f"Error API: {e}")
        return None


# ── PARSEO ────────────────────────────────────────────────────

def parsear(datos: dict, descargado_en: str) -> dict:
    plantas = []

    for tipo_fuente, lista in [
        ("renovable", datos.get("renovables", [])),
        ("noRenovable", datos.get("noRenovables", [])),
    ]:
        for p in lista:
            plantas.append({
                "central": p.get("central"),
                "generado": p.get("generado"),
                "generadoAnt": p.get("generadoAnt"),
                "porcentaje": p.get("porcentaje"),
                "grupo": p.get("grupo"),
                "tipo_fuente": tipo_fuente,
                "descargado_en": descargado_en,
            })

    tecnologia = []
    tipo_tec = datos.get("tipoTecnologia", {})
    if isinstance(tipo_tec, dict):
        for nombre, val in tipo_tec.items():
            if isinstance(val, dict):
                tecnologia.append({
                    "tecnologia": nombre,
                    "generado": val.get("generado"),
                    "generadoAnt": val.get("generadoAnt"),
                    "porcentaje": val.get("porcentaje"),
                    "descargado_en": descargado_en,
                })

    horaria = []
    for grupo_data in datos.get("programadoVsEjecutado", []):
        if grupo_data.get("grupo") != "Dia":
            continue

        labels = grupo_data.get("labels", [])
        generados = grupo_data.get("generado", [])
        programas = grupo_data.get("programado", [])

        for i, label in enumerate(labels):
            horaria.append({
                "fecha": label,
                "generado": generados[i] if i < len(generados) else None,
                "programado": programas[i] if i < len(programas) else None,
                "descargado_en": descargado_en,
            })

    return {
        "plantas": plantas,
        "tecnologia": tecnologia,
        "horaria": horaria,
    }


# ── SUPABASE ──────────────────────────────────────────────────

def supabase_post(tabla: str, filas: list) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error(f"Supabase {tabla}: faltan credenciales")
        return False

    if not filas:
        log.warning(f"Supabase {tabla}: no hay filas para insertar")
        return True

    url = f"{SUPABASE_URL}/rest/v1/{tabla}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    try:
        r = requests.post(url, headers=headers, json=filas, timeout=30)

        if r.status_code not in (200, 201):
            log.error(f"Supabase {tabla} HTTP {r.status_code}: {r.text}")
            return False

        log.info(f"Supabase {tabla}: {len(filas)} filas insertadas")
        return True

    except Exception as e:
        log.error(f"Supabase {tabla}: {e}")
        return False


# ── SQLITE ────────────────────────────────────────────────────

def sqlite_insertar(tabla: str, filas: list, conn: sqlite3.Connection):
    if not filas:
        return

    cols_base = list(filas[0].keys())

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {tabla} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            insertado_en TEXT DEFAULT (datetime('now'))
        )
    """)

    existentes = {r[1] for r in conn.execute(f"PRAGMA table_info({tabla})")}
    for col in cols_base:
        if col not in existentes:
            conn.execute(f'ALTER TABLE "{tabla}" ADD COLUMN "{col}" TEXT')

    for fila in filas:
        cols = list(fila.keys())
        vals = [str(v) if v is not None else None for v in fila.values()]
        placeholders = ", ".join(["?"] * len(cols))
        columnas = ", ".join(f'"{c}"' for c in cols)
        conn.execute(
            f'INSERT INTO "{tabla}" ({columnas}) VALUES ({placeholders})',
            vals
        )

    conn.commit()


# ── CICLO ─────────────────────────────────────────────────────

def ejecutar():
    descargado_en = datetime.now(timezone.utc).isoformat()

    log.info("Consultando API...")
    datos = fetch_api()

    if not datos:
        log.error("Sin datos del API.")
        return

    es_nuevo, nuevo_hash = datos_son_nuevos(datos)
    n_plantas_api = len(datos.get("renovables", [])) + len(datos.get("noRenovables", []))

    if not es_nuevo:
        log.info(f"Sin cambios. ({n_plantas_api} plantas, misma data)")
        return

    parsed = parsear(datos, descargado_en)
    n_p = len(parsed["plantas"])
    n_t = len(parsed["tecnologia"])
    n_h = len(parsed["horaria"])

    log.info(f"¡Datos nuevos! {n_p} plantas | {n_t} tecnologías | {n_h} horas")

    ok1 = ok2 = ok3 = False

    if SUPABASE_URL and SUPABASE_KEY:
        ok1 = supabase_post("generacion_plantas", parsed["plantas"])
        ok2 = supabase_post("tecnologia_resumen", parsed["tecnologia"])
        ok3 = supabase_post("generacion_horaria", parsed["horaria"])

        estados = f"plantas={'✅' if ok1 else '❌'}  tecnologia={'✅' if ok2 else '❌'}  horaria={'✅' if ok3 else '❌'}"
        log.info(f"Supabase → {estados}")
    else:
        log.warning("Supabase no configurado — solo guardando local")

    sqlite_ok = False
    try:
        with sqlite3.connect(DB_LOCAL) as conn:
            sqlite_insertar("generacion_plantas", parsed["plantas"], conn)
            sqlite_insertar("tecnologia_resumen", parsed["tecnologia"], conn)
            sqlite_insertar("generacion_horaria", parsed["horaria"], conn)
        sqlite_ok = True
        log.info(f"SQLite ✅ | {DB_LOCAL}")
    except Exception as e:
        log.error(f"SQLite error: {e}")

    # Guardar hash solo si la escritura relevante salió bien
    if SUPABASE_URL and SUPABASE_KEY:
        if ok1 and ok2 and ok3:
            guardar_hash(nuevo_hash)
            log.info("Hash actualizado.")
        else:
            log.warning("Hash NO actualizado porque Supabase no se llenó correctamente.")
    else:
        if sqlite_ok:
            guardar_hash(nuevo_hash)
            log.info("Hash actualizado con respaldo local.")
        else:
            log.warning("Hash NO actualizado porque no se pudo guardar ni localmente.")


# ── RESUMEN ───────────────────────────────────────────────────

def resumen():
    if not DB_LOCAL.exists():
        print("⚠ No hay base de datos local todavía.")
        return

    with sqlite3.connect(DB_LOCAL) as conn:
        tablas = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        print("\n" + "=" * 60)
        print(f"Base de Datos SIE — {DB_LOCAL}  ({DB_LOCAL.stat().st_size / 1e6:.2f} MB)")
        print("=" * 60)

        for t in tablas:
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                desde = conn.execute(f'SELECT MIN(descargado_en) FROM "{t}"').fetchone()[0]
                hasta = conn.execute(f'SELECT MAX(descargado_en) FROM "{t}"').fetchone()[0]
                print(f"{t:<25} {n:>8,} filas | {desde} → {hasta}")
            except Exception:
                pass

        print("=" * 60 + "\n")


# ── MAIN ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--resumen", action="store_true")
    args = parser.parse_args()

    if args.resumen:
        resumen()
        return

    if args.loop:
        log.info(f"Loop activo — consulta cada {LOOP_SEGUNDOS}s. Ctrl+C para detener.")
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
