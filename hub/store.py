"""DuckDB store: energy Parquet + water history Parquet + live water rows synced from the platform."""
from __future__ import annotations

import threading
from contextlib import contextmanager

import duckdb
from django.conf import settings

WATER_COLS = ("asset_id", "ts", "device_id", "device_type", "pump", "tank_raw", "tank_pct", "sump", "flow", "well",
              "ph", "float_sw", "v1", "v2", "v3", "src")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS water_live (asset_id VARCHAR, ts TIMESTAMP, device_id INTEGER, device_type VARCHAR,
    pump INTEGER, tank_raw VARCHAR, tank_pct DOUBLE, sump VARCHAR, flow VARCHAR, well VARCHAR, ph DOUBLE,
    float_sw VARCHAR, v1 DOUBLE, v2 DOUBLE, v3 DOUBLE, src VARCHAR);
CREATE TABLE IF NOT EXISTS water_devices (device_id INTEGER PRIMARY KEY, device_type VARCHAR, kind VARCHAR,
    tank_level DOUBLE, pump VARCHAR, last_update TIMESTAMP, synced_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR);
"""


@contextmanager
def connect(read_only=False):
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        con = duckdb.connect(str(settings.HUB_DB))
        try:
            con.execute(SCHEMA)
            yield con
        finally:
            con.close()


def query(sql, params=None):
    with connect() as con:
        cur = con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_meta(key, default=None):
    r = query("SELECT value FROM meta WHERE key = ?", [key])
    return r[0]["value"] if r else default


def set_meta(key, value):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, str(value)])


def energy_source():
    p = settings.ENERGY_PARQUET
    return f"read_parquet('{p}')" if p.exists() else None


def water_source():
    """All water readings: imported history ∪ live sync (live wins on duplicates)."""
    parts = ["SELECT * FROM water_live"]
    if settings.WATER_HISTORY.exists():
        parts.append(f"SELECT * FROM read_parquet('{settings.WATER_HISTORY}')")
    return "(" + " UNION ALL ".join(parts) + ")"


def data_version() -> str:
    """Changes whenever a source changes — used as the analytics cache key."""
    bits = []
    for p in (settings.ENERGY_PARQUET, settings.WATER_HISTORY, settings.SOLAR_FLEET):
        bits.append(f"{p.stat().st_mtime_ns}" if p.exists() else "-")
    bits.append(get_meta("water_live_version", "0"))
    m = settings.METER_DIR / "meters.parquet"
    bits.append(f"{m.stat().st_mtime_ns}" if m.exists() else "-")
    return "|".join(bits)
