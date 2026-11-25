"""Consumer smart meters (AMI): meter health index, predicted failure window, fault signals,
feeder → DT → meter topology and interval logs — from the predictive meter-health workspace.

``snapshot()`` copies what the hub needs into ``data/meters/*.parquet`` (read-only on the source),
so the hub never holds a lock on the other app's DuckDB files.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import duckdb
from django.conf import settings

from . import store

GENERIC_ACTION = "Open the linked source dashboard"
BANDS = ["Critical", "Degradation", "Watch", "Optimal"]
WINDOW_ORDER = ["Immediate to 2 weeks", "2 to 4 weeks if repeated", "Within 1 month if trend continues", "1 to 3 months",
                "2 to 4 months", "3 to 6 months", "Monitor in next reading cycle", "No near-term failure indication"]
# Fault signal → field category, with the physical action for the meter reader / lineman.
CATEGORIES = [
    ("ct_open", "Current circuit open / bypass", ("current circuit open", "current bypass"),
     "Inspect the terminal block and current links under load; look for a bypass jumper or cut current wire; "
     "check the CT secondary on CT-operated meters. Raise an assessment if a bypass is found."),
    ("tamper", "Tamper or wrong wiring", ("tamper", "wrong current wiring", "wrong phase", "phase sequence",
                                          "installation or neutral", "neutral fault", "neutral disturbance", "voltage missing"),
     "Check seals and cover-open marks; verify phase / neutral wiring at the meter and service cable; correct the wiring and re-seal."),
    ("comms", "Communication / missing data", ("missing blp", "late blp", "communication stopped"),
     "Check the meter's communication module and antenna / signal; power-cycle the module; read the missing intervals locally with the HHU."),
    ("over_current", "Over-current", ("excessive current", "over-current"),
     "Measure the connected load against the sanctioned load; inspect the terminal block for heating; issue a load-augmentation notice."),
    ("over_voltage", "Over-voltage", ("high voltage", "over-voltage"),
     "Measure phase-to-neutral voltage at the service point; check for a broken neutral upstream; inform the DT maintenance team."),
    ("register", "Register / reading anomaly", ("reverse cumulative", "kwh jump"),
     "Take a manual reading and compare with the display; if the cumulative kWh ran backwards or jumped, replace the meter and correct the bill."),
]
FLAG_CATS = [("rtc", "Clock drift", "is_rtc_drift"), ("loose", "Loose contact", "is_loose_contact")]


def _p(name):
    return settings.METER_DIR / f"{name}.parquet"


def available():
    return _p("meters").exists()


def snapshot() -> dict:
    """Copy meter health, topology, DT twin and interval logs into data/meters (read-only on the source)."""
    out = settings.METER_DIR
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.DATA_DIR) as tmp:
        # work on copies so a running source app's write lock can never block us
        comb, twin = Path(tmp) / "c.duckdb", Path(tmp) / "t.duckdb"
        shutil.copyfile(settings.METER_COMBINED_DB, comb)
        shutil.copyfile(settings.METER_TWIN_DB, twin)
        con = duckdb.connect()
        con.execute(f"ATTACH '{comb}' AS c (READ_ONLY)")
        con.execute(f"ATTACH '{twin}' AS t (READ_ONLY)")
        con.execute(f"""COPY (
            SELECT f.meter_id, f.mhi_score, f.mhi_band, f.mhi_failure_window, f.highest_severity, f.source_combo,
                   f.event_faults, f.blp_faults, f.dlp_faults, f.mhi_reasons, f.combined_summary,
                   f.diagnosis_en, f.diagnosis_hi, f.why_en, f.why_hi, f.evidence_en,
                   f.corrective_action_en, f.corrective_action_hi, f.preventive_action_en,
                   f.is_installation, f.is_over_voltage, f.is_over_current, f.is_meter_failure, f.is_kwh_jump,
                   f.is_loose_contact, f.is_rtc_drift,
                   tp.feeder_id, tp.feeder_name, tp.dt_id, tp.dt_name
            FROM c.combined_meter_faults f LEFT JOIN t.topology tp ON tp.retail_meter_id = f.meter_id
        ) TO '{_p("meters")}' (FORMAT PARQUET)""")
        con.execute(f"COPY (SELECT * FROM t.dt_summary) TO '{_p('twin_dt')}' (FORMAT PARQUET)")
        con.execute(f"COPY (SELECT * FROM t.meter_current_logs ORDER BY meter_id, meter_timestamp) TO '{_p('meter_logs')}' (FORMAT PARQUET)")
        con.execute(f"COPY (SELECT * FROM t.dlp_energy_logs ORDER BY meter_id, meter_timestamp) TO '{_p('meter_daily')}' (FORMAT PARQUET)")
        n = con.execute(f"SELECT count(*) FROM '{_p('meters')}'").fetchone()[0]
        con.close()
    store.set_meta("meters_version", f"{n}:{_p('meters').stat().st_mtime_ns}")
    return {"meters": n}


def _q(sql, params=None):
    con = duckdb.connect()
    try:
        cur = con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def categorise(m) -> list[tuple[str, str, str]]:
    text = " | ".join(str(m.get(k) or "") for k in ("event_faults", "blp_faults", "dlp_faults")).lower()
    cats = [(k, label, act) for k, label, keys, act in CATEGORIES if any(x in text for x in keys)]
    src_act = m.get("corrective_action_en") or ""
    for k, label, flag in FLAG_CATS:
        if m.get(flag):
            cats.append((k, label, src_act))
    return cats


def field_action(m) -> str:
    src = m.get("corrective_action_en") or ""
    if src and GENERIC_ACTION not in src:
        return src
    cats = categorise(m)
    return cats[0][2] if cats else "Verify at the next reading cycle."


def _cat_sql():
    parts = []
    for k, _, keys, _ in CATEGORIES:
        cond = " OR ".join(f"lower(coalesce(event_faults,'') || ' | ' || coalesce(blp_faults,'') || ' | ' || coalesce(dlp_faults,'')) LIKE '%{x}%'" for x in keys)
        parts.append(f"sum(CASE WHEN {cond} THEN 1 ELSE 0 END) AS {k}")
    parts += [f"sum(CASE WHEN {flag} THEN 1 ELSE 0 END) AS {k}" for k, _, flag in FLAG_CATS]
    return ", ".join(parts)


def summary():
    if not available():
        return None
    src = f"'{_p('meters')}'"
    bands = {r["mhi_band"]: r for r in _q(f"SELECT mhi_band, count(*) AS n, avg(mhi_score) AS mhi FROM {src} GROUP BY 1")}
    tot = _q(f"SELECT count(*) AS n, avg(mhi_score) AS mhi, count(DISTINCT feeder_id) AS feeders, count(DISTINCT dt_id) AS dts FROM {src}")[0]
    windows = {r["w"]: r["n"] for r in _q(f"SELECT mhi_failure_window AS w, count(*) AS n FROM {src} GROUP BY 1")}
    cats = _q(f"SELECT {_cat_sql()} FROM {src} WHERE mhi_band IN ('Critical', 'Degradation')")[0]
    labels = {k: label for k, label, *_ in CATEGORIES} | {k: label for k, label, _ in FLAG_CATS}
    twin = {r["twin_status"]: r["n"] for r in _q(f"SELECT twin_status, count(*) AS n FROM '{_p('twin_dt')}' GROUP BY 1")}
    soon = windows.get("Immediate to 2 weeks", 0) + windows.get("Within 1 month if trend continues", 0)
    if_repeated = windows.get("2 to 4 weeks if repeated", 0)
    return {
        "total": tot["n"], "mhi": tot["mhi"], "feeders": tot["feeders"], "dts": tot["dts"],
        "bands": [{"band": b, "n": bands.get(b, {}).get("n", 0), "mhi": bands.get(b, {}).get("mhi")} for b in BANDS],
        "critical": bands.get("Critical", {}).get("n", 0), "degradation": bands.get("Degradation", {}).get("n", 0),
        "fail_within_month": soon, "fail_if_repeated": if_repeated,
        "windows": [{"w": w, "n": windows.get(w, 0)} for w in WINDOW_ORDER if windows.get(w)],
        "categories": sorted(({"key": k, "label": labels[k], "n": int(cats[k] or 0)} for k in labels), key=lambda x: -x["n"]),
        "twin": twin,
    }


def feeders():
    if not available():
        return []
    return _q(f"""SELECT feeder_id, any_value(feeder_name) AS feeder_name, count(*) AS meters, count(DISTINCT dt_id) AS dts,
                         avg(mhi_score) AS mhi, sum(CASE WHEN mhi_band = 'Critical' THEN 1 ELSE 0 END) AS critical,
                         sum(CASE WHEN mhi_band = 'Degradation' THEN 1 ELSE 0 END) AS degradation
                  FROM '{_p('meters')}' WHERE feeder_id IS NOT NULL GROUP BY 1 ORDER BY critical DESC, degradation DESC, mhi""")


def hot_dts(limit=25):
    """DTs where the meters underneath are failing — plus the digital twin's DT-level status."""
    if not available():
        return []
    return _q(f"""WITH m AS (SELECT dt_id, any_value(dt_name) AS dt_name, any_value(feeder_id) AS feeder_id, count(*) AS meters,
                                    avg(mhi_score) AS mhi, sum(CASE WHEN mhi_band = 'Critical' THEN 1 ELSE 0 END) AS critical,
                                    sum(CASE WHEN mhi_band = 'Degradation' THEN 1 ELSE 0 END) AS degradation
                             FROM '{_p('meters')}' WHERE dt_id IS NOT NULL GROUP BY 1)
                  SELECT m.*, t.twin_status, t.reporting_meters, t.dt_voltage, t.voltage_spread, t.current_difference,
                         t.completeness_pct
                  FROM m LEFT JOIN '{_p('twin_dt')}' t USING (dt_id)
                  ORDER BY m.critical DESC, m.degradation DESC, m.mhi LIMIT ?""", [limit])


PRED_WINDOWS = {"month": ("Immediate to 2 weeks", "Within 1 month if trend continues"), "repeat": ("2 to 4 weeks if repeated",)}


def search(band=None, cat=None, feeder=None, dt=None, q=None, limit=100, offset=0, pred=None):
    if not available():
        return [], 0
    where, params = ["1=1"], []
    if band:
        where.append("mhi_band = ?"); params.append(band)
    if feeder:
        where.append("feeder_id = ?"); params.append(feeder)
    if dt:
        where.append("dt_id = ?"); params.append(dt)
    if q:
        where.append("meter_id ILIKE ?"); params.append(f"%{q}%")
    if pred in PRED_WINDOWS:
        ws = PRED_WINDOWS[pred]
        where.append("mhi_failure_window IN (" + ", ".join("?" for _ in ws) + ")"); params += list(ws)
    if cat:
        c = next((c for c in CATEGORIES if c[0] == cat), None)
        f = next((f for f in FLAG_CATS if f[0] == cat), None)
        if c:
            where.append("(" + " OR ".join(f"lower(coalesce(event_faults,'') || ' | ' || coalesce(blp_faults,'') || ' | ' || coalesce(dlp_faults,'')) LIKE ?" for _ in c[2]) + ")")
            params += [f"%{x}%" for x in c[2]]
        elif f:
            where.append(f"{f[2]}")
    w = " AND ".join(where)
    src = f"'{_p('meters')}'"
    total = _q(f"SELECT count(*) AS n FROM {src} WHERE {w}", params)[0]["n"]
    rows = _q(f"SELECT * FROM {src} WHERE {w} ORDER BY mhi_score, meter_id LIMIT ? OFFSET ?", params + [limit, offset])
    for r in rows:
        r["categories"] = [label for _, label, _ in categorise(r)]
        r["action"] = field_action(r)
    return rows, total


def meter(meter_id):
    if not available():
        return None
    r = _q(f"SELECT * FROM '{_p('meters')}' WHERE meter_id = ?", [meter_id])
    if not r:
        return None
    m = r[0]
    m["categories"] = categorise(m)
    m["action"] = field_action(m)
    m["utility"] = "meters"
    return m


def logs(meter_id):
    return _q(f"SELECT meter_timestamp AS ts, current, voltage, power_factor, interval_kwh, demand_kw "
              f"FROM '{_p('meter_logs')}' WHERE meter_id = ? ORDER BY ts", [meter_id]) if available() else []


def daily(meter_id):
    return _q(f"SELECT meter_timestamp AS ts, daily_kwh, reading_kwh FROM '{_p('meter_daily')}' WHERE meter_id = ? ORDER BY ts",
              [meter_id]) if available() else []


def findings():
    """Critical meters and DT twin alarms in the common finding format."""
    if not available():
        return []
    out = []
    for m in _q(f"SELECT * FROM '{_p('meters')}' WHERE mhi_band = 'Critical' ORDER BY mhi_score, meter_id"):
        out.append({"asset_id": m["meter_id"], "utility": "meters", "code": "METER_" + (categorise(m)[0][0].upper() if categorise(m) else "HEALTH"),
                    "severity": "critical", "title": (m["diagnosis_en"] or "Meter health critical").rstrip("."),
                    "value": f"MHI {m['mhi_score']} · fails {m['mhi_failure_window'].lower()}",
                    "why": m["why_en"] or "", "action": field_action(m), "start": None, "end": None,
                    "url": f"/meter/{m['meter_id']}/"})
    for d in _q(f"SELECT * FROM '{_p('twin_dt')}' WHERE twin_status IN ('Critical', 'Warning')"):
        out.append({"asset_id": d["dt_id"], "utility": "meters", "code": "TWIN_DT", "severity": d["twin_status"].lower(),
                    "title": "DT energy balance / voltage mismatch (digital twin)",
                    "value": f"voltage spread {d['voltage_spread'] or 0:.0f} V, current gap {d['current_difference'] or 0:.1f} A, "
                             f"{d['completeness_pct'] or 0:.0f} % meters reporting",
                    "why": "The DT's own reading disagrees with the meters below it: losses, theft, wrong mapping or a failing meter.",
                    "action": "Walk the DT's LT network: check service connections for hooking / bypass; verify the meter-to-DT mapping.",
                    "start": None, "end": None, "url": f"/meters/?dt={d['dt_id']}"})
    return out
