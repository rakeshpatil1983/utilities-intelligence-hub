"""Water history from a MySQL dump of the IoT platform (mqtt_logdata + devices).

Streams the dump once, keeps every reading whose JSON payload carries water keys
(pump / tank / sump / flow / well / pH / float), normalises it and writes Parquet.
Only device ids, device types and readings are used — no user or group data.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb

TUP = re.compile(r"\('(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)',(\d+),'((?:[^'\\]|\\.)*)','((?:[^'\\]|\\.)*)'\)")
DEV = re.compile(r"\((\d+),(\d+),(\d+),'(?:[^'\\]|\\.)*'")
TYP = re.compile(r"\((\d+),'((?:[^'\\]|\\.)*)'")
WATER_KEYS = {"pump", "tank", "sump", "flow", "Pump Status", "Tank Status", "Well Status", "PH", "Float", "sw"}


def _on(v):
    if v is None:
        return None
    s = str(v).strip().upper()
    return 1 if s in ("ON", "1", "TRUE", "RUNNING") else 0 if s in ("OFF", "0", "FALSE", "STOP", "STOPPED") else None


def tank_pct(v):
    """Hardware tank string → %, as the platform's own OHT endpoint parses it."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if s == "EMPTY":
        return 0.0
    if s == "LOW":          # low-level float made, not yet empty
        return 25.0
    if s == "FULL":
        return 100.0
    if s in ("SENSOR ERROR", "SENSOR_ERROR", "ERROR", ""):
        return None
    try:
        return float(s.replace("%", ""))
    except ValueError:
        return None


def _num(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def normalise(js: dict) -> dict | None:
    """One platform payload → a water reading (None if it has no water content)."""
    if not WATER_KEYS & set(js):
        return None
    tank = js.get("tank", js.get("Tank Status"))
    return {
        "pump": _on(js.get("pump", js.get("Pump Status"))),
        "tank_raw": None if tank is None else str(tank),
        "tank_pct": tank_pct(tank),
        "sump": None if js.get("sump") is None else str(js.get("sump")).lower(),
        "flow": None if js.get("flow") is None else str(js.get("flow")).lower(),
        "well": None if js.get("Well Status") is None else str(js.get("Well Status")).lower(),
        "ph": _num(js.get("PH")),
        "float_sw": None if js.get("Float", js.get("sw")) is None else str(js.get("Float", js.get("sw"))).lower(),
        "v1": _num(js.get("Ph1")), "v2": _num(js.get("Ph2")), "v3": _num(js.get("Ph3")),
    }


def import_dump(dump: Path, out: Path) -> dict:
    types, devs, rows = {}, {}, []
    with open(dump, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("INSERT INTO `devicetypes`"):
                types.update({int(m[1]): m[2] for m in TYP.finditer(line)})
            elif line.startswith("INSERT INTO `devices`"):
                devs.update({int(m[1]): int(m[2]) for m in DEV.finditer(line)})
            elif line.startswith("INSERT INTO `mqtt_logdata`"):
                for m in TUP.finditer(line):
                    raw = m[4]
                    if not any(k in raw for k in ("pump", "tank", "sump", "flow", "Status", "PH", "Float", "sw")):
                        continue
                    try:
                        js = json.loads(raw.replace('\\"', '"').replace("\\\\", "\\"))
                    except ValueError:
                        continue
                    if not isinstance(js, dict):
                        continue
                    r = normalise(js)
                    if r:
                        d = int(m[2])
                        rows.append((f"W{d}", m[1], d, types.get(devs.get(d), ""), r["pump"], r["tank_raw"], r["tank_pct"],
                                     r["sump"], r["flow"], r["well"], r["ph"], r["float_sw"], r["v1"], r["v2"], r["v3"], "history"))
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("""CREATE TABLE w (asset_id VARCHAR, ts TIMESTAMP, device_id INTEGER, device_type VARCHAR, pump INTEGER,
                   tank_raw VARCHAR, tank_pct DOUBLE, sump VARCHAR, flow VARCHAR, well VARCHAR, ph DOUBLE, float_sw VARCHAR,
                   v1 DOUBLE, v2 DOUBLE, v3 DOUBLE, src VARCHAR)""")
    con.executemany("INSERT INTO w VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute(f"COPY (SELECT * FROM w ORDER BY asset_id, ts) TO '{out}' (FORMAT PARQUET)")
    return dict(con.execute("SELECT count(*) AS n, count(DISTINCT asset_id) AS devices, min(ts) AS first, max(ts) AS last FROM w")
                .fetchdf().iloc[0])
