"""
=============================================================
  SIE Pizarra — Colector Completo Mejorado
  Captura:
    - snapshot crudo completo
    - plantas (renovables + no renovables)
    - resumen por tecnología
    - detalle por tecnología
    - programado vs ejecutado (Mes, Semana, Dia)
    - timestamps UTC + RD
=============================================================
Uso:
    python collector.py
    python collector.py --loop
    python collector.py --resumen
"""

import os
import re
import json
import time
import hashlib
import sqlite3
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests

API_URL = "https://pizarradev.sie.gob.do/v1/Generacion/PDespachoPotencia"
TZ_RD = ZoneInfo("America/Santo_Domingo")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

DB_LOCAL = Path("sie_generacion.db")
HASH_FILE = Path(".ultimo_hash")
LOOP_SEGUNDOS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("collector.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

HEADERS_API = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://pizarra.sie.gob.do/",
    "Origin": "https://pizarra.sie.gob.do",
}


# ── HELPERS ───────────────────────────────────────────────────

def calcular_hash(datos: dict) -> str:
    contenido = json.dumps(datos, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.md5(contenido.encode("utf-8")).hexdigest()

def leer_ultimo_hash_local() -> str:
    if HASH_FILE.exists():
        return HASH_FILE.read_text(encoding="utf-8").strip()
    return ""

def guardar_hash_local(h: str):
    HASH_FILE.write_text(h, encoding="utf-8")

def extraer_minutos(texto):
    if not texto:
        return None
    m = re.search(r"(\d+)", str(texto))
    return int(m.group(1)) if m else None

def parsear_fecha_fuente_rd(texto_iso):
    if not texto_iso:
        return None, None
    try:
        dt_rd = datetime.fromisoformat(texto_iso).replace(tzinfo=TZ_RD)
        dt_utc = dt_rd.astimezone(timezone.utc)
        return dt_rd.isoformat(), dt_utc.isoformat()
    except Exception:
        return None, None

def chunks(lista, tam=500):
    for i in range(0, len(lista), tam):
        yield lista[i:i + tam]


# ── FETCH ─────────────────────────────────────────────────────

def fetch_api():
    try:
        r = requests.get(API_URL, headers=HEADERS_API, timeout=30)
        r.raise_for_status()
        texto = r.content.decode("utf-8", errors="replace")
        return json.loads(texto)
    except Exception as e:
        log.error(f"Error API: {e}")
        return None


# ── SUPABASE ──────────────────────────────────────────────────

def supabase_headers(prefer="return=representation"):
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }

def supabase_hash_exists(payload_hash: str) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False

    params = {
        "select": "id",
        "payload_hash": f"eq.{payload_hash}",
        "limit": "1",
    }
    url = f"{SUPABASE_URL}/rest/v1/raw_snapshot?{urlencode(params)}"

    try:
        r = requests.get(url, headers=supabase_headers(), timeout=20)
        if r.status_code == 200:
            data = r.json()
            return len(data) > 0
        log.error(f"Supabase raw_snapshot check HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log.error(f"Supabase raw_snapshot check: {e}")
        return False

def supabase_upsert(tabla: str, filas: list, on_conflict_cols: list[str]) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error(f"Supabase {tabla}: faltan credenciales")
        return False

    if not filas:
        log.warning(f"Supabase {tabla}: no hay filas para insertar")
        return True

    ok_total = True

    for lote in chunks(filas, 500):
        params = {}
        if on_conflict_cols:
            params["on_conflict"] = ",".join(on_conflict_cols)

        url = f"{SUPABASE_URL}/rest/v1/{tabla}"
        if params:
            url += "?" + urlencode(params)

        headers = supabase_headers(prefer="resolution=ignore-duplicates,return=representation")

        try:
            r = requests.post(url, headers=headers, json=lote, timeout=60)

            if r.status_code not in (200, 201):
                log.error(f"Supabase {tabla} HTTP {r.status_code}: {r.text}")
                ok_total = False
            else:
                log.info(f"Supabase {tabla}: lote OK ({len(lote)} filas)")
        except Exception as e:
            log.error(f"Supabase {tabla}: {e}")
            ok_total = False

    return ok_total


# ── SQLITE ────────────────────────────────────────────────────

def sqlite_insertar(tabla: str, filas: list, conn: sqlite3.Connection):
    if not filas:
        return

    cols_base = list(filas[0].keys())

    conn.execute(f'''
        CREATE TABLE IF NOT EXISTS "{tabla}" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            insertado_en TEXT DEFAULT (datetime('now'))
        )
    ''')

    existentes = {r[1] for r in conn.execute(f'PRAGMA table_info("{tabla}")')}
    for col in cols_base:
        if col not in existentes:
            conn.execute(f'ALTER TABLE "{tabla}" ADD COLUMN "{col}" TEXT')

    for fila in filas:
        cols = list(fila.keys())
        vals = []
        for v in fila.values():
            if isinstance(v, (dict, list)):
                vals.append(json.dumps(v, ensure_ascii=False))
            elif v is None:
                vals.append(None)
            else:
                vals.append(str(v))

        placeholders = ", ".join(["?"] * len(cols))
        columnas = ", ".join(f'"{c}"' for c in cols)
        conn.execute(
            f'INSERT INTO "{tabla}" ({columnas}) VALUES ({placeholders})',
            vals
        )

    conn.commit()


# ── PARSEO ────────────────────────────────────────────────────

def parsear(datos: dict, capturado_en_utc: str, capturado_en_rd: str, payload_hash: str) -> dict:
    ultima_actualizacion_texto = datos.get("ultimaActualizacion")
    ultima_actualizacion_minutos = extraer_minutos(ultima_actualizacion_texto)

    renovables = datos.get("renovables", []) or []
    no_renovables = datos.get("noRenovables", []) or []
    tipo_tec = datos.get("tipoTecnologia", {}) or {}
    pve = datos.get("programadoVsEjecutado", []) or []

    raw_snapshot = [{
        "payload_hash": payload_hash,
        "fuente_endpoint": API_URL,
        "capturado_en_utc": capturado_en_utc,
        "capturado_en_rd": capturado_en_rd,
        "ultima_actualizacion_texto": ultima_actualizacion_texto,
        "ultima_actualizacion_minutos": ultima_actualizacion_minutos,
        "cantidad_renovables": len(renovables),
        "cantidad_no_renovables": len(no_renovables),
        "cantidad_total_plantas": len(renovables) + len(no_renovables),
        "payload_json": datos,
    }]

    plantas = []
    for tipo_fuente, lista in [
        ("renovable", renovables),
        ("noRenovable", no_renovables),
    ]:
        for p in lista:
            plantas.append({
                "payload_hash": payload_hash,
                "fuente_endpoint": API_URL,
                "central": p.get("central"),
                "generado": p.get("generado"),
                "generadoAnt": p.get("generadoAnt"),
                "porcentaje": p.get("porcentaje"),
                "grupo": p.get("grupo"),
                "tipo_fuente": tipo_fuente,
                "capturado_en_utc": capturado_en_utc,
                "capturado_en_rd": capturado_en_rd,
                "ultima_actualizacion_texto": ultima_actualizacion_texto,
                "ultima_actualizacion_minutos": ultima_actualizacion_minutos,
            })

    tecnologia_resumen = []
    tecnologia_detalle = []

    if isinstance(tipo_tec, dict):
        for nombre, val in tipo_tec.items():
            if not isinstance(val, dict):
                continue

            tecnologia_resumen.append({
                "payload_hash": payload_hash,
                "fuente_endpoint": API_URL,
                "tecnologia": nombre,
                "generado": val.get("generado"),
                "generadoAnt": val.get("generadoAnt"),
                "porcentaje": val.get("porcentaje"),
                "capturado_en_utc": capturado_en_utc,
                "capturado_en_rd": capturado_en_rd,
                "ultima_actualizacion_texto": ultima_actualizacion_texto,
                "ultima_actualizacion_minutos": ultima_actualizacion_minutos,
            })

            for d in val.get("detalle", []) or []:
                fecha_fuente_texto = d.get("fecha")
                fecha_fuente_rd, fecha_fuente_utc = parsear_fecha_fuente_rd(fecha_fuente_texto)

                tecnologia_detalle.append({
                    "payload_hash": payload_hash,
                    "fuente_endpoint": API_URL,
                    "tecnologia": nombre,
                    "fecha_fuente_texto": fecha_fuente_texto,
                    "fecha_fuente_rd": fecha_fuente_rd,
                    "fecha_fuente_utc": fecha_fuente_utc,
                    "generado": d.get("generado"),
                    "generadoAnt": d.get("generadoAnt"),
                    "porcentaje": d.get("porcentaje"),
                    "capturado_en_utc": capturado_en_utc,
                    "capturado_en_rd": capturado_en_rd,
                })

    programado_vs_ejecutado = []
    for bloque in pve:
        grupo = bloque.get("grupo")
        labels = bloque.get("labels", []) or []
        generados = bloque.get("generado", []) or []
        programados = bloque.get("programado", []) or []

        max_len = max(len(labels), len(generados), len(programados))

        for i in range(max_len):
            fecha_fuente_texto = labels[i] if i < len(labels) else None
            fecha_fuente_rd, fecha_fuente_utc = parsear_fecha_fuente_rd(fecha_fuente_texto)

            programado_vs_ejecutado.append({
                "payload_hash": payload_hash,
                "fuente_endpoint": API_URL,
                "grupo": grupo,
                "indice": i,
                "fecha_fuente_texto": fecha_fuente_texto,
                "fecha_fuente_rd": fecha_fuente_rd,
                "fecha_fuente_utc": fecha_fuente_utc,
                "generado": generados[i] if i < len(generados) else None,
                "programado": programados[i] if i < len(programados) else None,
                "capturado_en_utc": capturado_en_utc,
                "capturado_en_rd": capturado_en_rd,
            })

    return {
        "raw_snapshot": raw_snapshot,
        "generacion_plantas": plantas,
        "tecnologia_resumen": tecnologia_resumen,
        "tecnologia_detalle": tecnologia_detalle,
        "programado_vs_ejecutado": programado_vs_ejecutado,
    }


# ── RESUMEN ───────────────────────────────────────────────────

def resumen():
    if not DB_LOCAL.exists():
        print("⚠ No hay base de datos local todavía.")
        return

    with sqlite3.connect(DB_LOCAL) as conn:
        tablas = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        print("\n" + "=" * 70)
        print(f"Base de Datos SIE — {DB_LOCAL}  ({DB_LOCAL.stat().st_size / 1e6:.2f} MB)")
        print("=" * 70)

        for t in tablas:
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                print(f"{t:<30} {n:>10,} filas")
            except Exception:
                pass

        print("=" * 70 + "\n")


# ── CICLO ─────────────────────────────────────────────────────

def ejecutar():
    ahora_utc = datetime.now(timezone.utc)
    ahora_rd = ahora_utc.astimezone(TZ_RD)

    capturado_en_utc = ahora_utc.isoformat()
    capturado_en_rd = ahora_rd.isoformat()

    log.info("Consultando API...")
    datos = fetch_api()

    if not datos:
        log.error("Sin datos del API.")
        return

    payload_hash = calcular_hash(datos)

    if SUPABASE_URL and SUPABASE_KEY:
        if supabase_hash_exists(payload_hash):
            total = len(datos.get("renovables", [])) + len(datos.get("noRenovables", []))
            log.info(f"Sin cambios. Snapshot ya existe en Supabase. ({total} plantas)")
            return
    else:
        if payload_hash == leer_ultimo_hash_local():
            total = len(datos.get("renovables", [])) + len(datos.get("noRenovables", []))
            log.info(f"Sin cambios. Mismo hash local. ({total} plantas)")
            return

    parsed = parsear(datos, capturado_en_utc, capturado_en_rd, payload_hash)

    log.info(
        "Datos nuevos → "
        f"plantas={len(parsed['generacion_plantas'])} | "
        f"tec_resumen={len(parsed['tecnologia_resumen'])} | "
        f"tec_detalle={len(parsed['tecnologia_detalle'])} | "
        f"pve={len(parsed['programado_vs_ejecutado'])}"
    )

    ok_raw = ok1 = ok2 = ok3 = ok4 = False

    if SUPABASE_URL and SUPABASE_KEY:
        ok_raw = supabase_upsert("raw_snapshot", parsed["raw_snapshot"], ["payload_hash"])
        ok1 = supabase_upsert("generacion_plantas", parsed["generacion_plantas"], ["payload_hash", "central", "tipo_fuente"])
        ok2 = supabase_upsert("tecnologia_resumen", parsed["tecnologia_resumen"], ["payload_hash", "tecnologia"])
        ok3 = supabase_upsert("tecnologia_detalle", parsed["tecnologia_detalle"], ["payload_hash", "tecnologia", "fecha_fuente_texto"])
        ok4 = supabase_upsert("programado_vs_ejecutado", parsed["programado_vs_ejecutado"], ["payload_hash", "grupo", "fecha_fuente_texto"])

        log.info(
            "Supabase → "
            f"raw={'✅' if ok_raw else '❌'} "
            f"plantas={'✅' if ok1 else '❌'} "
            f"tec_res={'✅' if ok2 else '❌'} "
            f"tec_det={'✅' if ok3 else '❌'} "
            f"pve={'✅' if ok4 else '❌'}"
        )
    else:
        log.warning("Supabase no configurado — solo guardando local")

    sqlite_ok = False
    try:
        with sqlite3.connect(DB_LOCAL) as conn:
            for tabla, filas in parsed.items():
                sqlite_insertar(tabla, filas, conn)
        sqlite_ok = True
        log.info(f"SQLite ✅ | {DB_LOCAL}")
    except Exception as e:
        log.error(f"SQLite error: {e}")

    if SUPABASE_URL and SUPABASE_KEY:
        if ok_raw and ok1 and ok2 and ok3 and ok4:
            guardar_hash_local(payload_hash)
            log.info("Hash local actualizado.")
        else:
            log.warning("Hash local NO actualizado porque hubo fallos en Supabase.")
    else:
        if sqlite_ok:
            guardar_hash_local(payload_hash)
            log.info("Hash local actualizado con respaldo SQLite.")
        else:
            log.warning("Hash local NO actualizado.")


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
