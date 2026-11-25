"""Energy + water analytics with one common finding format.

finding = {asset_id, utility, code, severity (critical|warning|info), title, value, why, action,
           start, end}   — start/end select the evidence rows behind it.
"""
from __future__ import annotations

import json
import statistics
import threading
from collections import defaultdict
from datetime import datetime, timedelta

from django.conf import settings

from . import store

V_NOM, V_TOL = 230.0, 0.06          # LV phase-to-neutral, ±6 %
V_LO, V_HI = V_NOM * (1 - V_TOL), V_NOM * (1 + V_TOL)
WINDOW = timedelta(days=30)
SEV_RANK = {"critical": 0, "warning": 1, "info": 2}
PENALTY = {"critical": 25, "warning": 10, "info": 2}

_cache, _cache_lock = {}, threading.Lock()


def cached(name, fn):
    key = (name, store.data_version())
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    val = fn()
    with _cache_lock:
        for k in [k for k in _cache if k[0] == name]:
            del _cache[k]
        _cache[key] = val
    return val


def _f(sev, asset, utility, code, title, value, why, action, start=None, end=None):
    return {"asset_id": asset, "utility": utility, "code": code, "severity": sev, "title": title, "value": value,
            "why": why, "action": action, "start": start, "end": end}


def _health(findings):
    return max(0, 100 - sum(PENALTY[f["severity"]] for f in findings))


# ── Energy ───────────────────────────────────────────────────────────────────

def _units():
    try:
        units = json.loads(settings.ENERGY_UNITS_JSON.read_text())["units"]
    except (OSError, ValueError, KeyError):
        units = []
    if isinstance(units, dict):
        return {k: (v if isinstance(v, dict) else {}) for k, v in units.items()}
    return {u["imei"]: u for u in units}


def energy_assets():
    src = store.energy_source()
    if not src:
        return {}
    meta = _units()
    imeis = [r["imei"] for r in store.query(f"SELECT DISTINCT imei FROM {src} ORDER BY imei")]
    out = {}
    for n, imei in enumerate(imeis, 1):
        rating = (meta.get(imei) or {}).get("rating_kva") or 250
        aid = f"DT-{n:02d}"
        out[aid] = {"asset_id": aid, "imei": imei, "utility": "energy", "kind": "Distribution transformer",
                    "name": f"Distribution transformer {aid}", "rating_kva": rating,
                    "rated_a": rating * 1000 / (3 ** 0.5 * 400)}
    return out


def _energy_build():
    src = store.energy_source()
    assets = energy_assets()
    if not src or not assets:
        return {"assets": {}, "as_of": None, "findings": []}
    by_imei = {a["imei"]: a for a in assets.values()}
    as_of = store.query(f"SELECT max(slot) AS t FROM {src}")[0]["t"]
    since = as_of - WINDOW
    stats = store.query(f"""
        WITH w AS (SELECT * FROM {src} WHERE slot > ?),
        x AS (SELECT *, (i_r + i_y + i_b) / 3 AS i_avg,
                     greatest(abs(i_r - (i_r+i_y+i_b)/3), abs(i_y - (i_r+i_y+i_b)/3), abs(i_b - (i_r+i_y+i_b)/3))
                       / nullif((i_r+i_y+i_b)/3, 0) AS unbal,
                     least(v_rn, v_yn, v_bn) AS v_min, greatest(v_rn, v_yn, v_bn) AS v_max
              FROM w)
        SELECT imei, count(*) AS slots, sum(CASE WHEN supply = 1 THEN 1 ELSE 0 END) AS on_slots,
               sum(kw) * 0.25 AS kwh, max(kw) AS peak_kw, max(kva) AS peak_kva, quantile_cont(kva, 0.95) AS p95_kva,
               avg(CASE WHEN supply = 1 THEN kw END) AS avg_kw, sum(kw) / nullif(sum(kva), 0) AS pf,
               avg(CASE WHEN i_avg > 10 THEN unbal END) AS unbal,
               avg(CASE WHEN supply = 1 THEN CASE WHEN v_min < {V_LO} THEN 1.0 ELSE 0.0 END END) AS low_v_share,
               avg(CASE WHEN supply = 1 THEN CASE WHEN v_max > {V_HI} THEN 1.0 ELSE 0.0 END END) AS high_v_share,
               -- median: a few readings in the export carry THD on a different scale (capped at 60 %)
               median(CASE WHEN supply = 1 AND thd_v < 50 THEN thd_v END) AS thd_v,
               median(CASE WHEN supply = 1 AND thd_i < 50 THEN thd_i END) AS thd_i,
               avg(CASE WHEN supply = 1 THEN CASE WHEN thd_v >= 50 THEN 1.0 ELSE 0.0 END END) AS thd_bad,
               sum(i_n) / nullif(sum(i_avg), 0) AS in_ratio,
               sum(CASE WHEN supply = 1 AND (freq < 49.5 OR freq > 50.5) THEN 1 ELSE 0 END) AS freq_exc,
               min(v_min) AS v_floor, max(v_max) AS v_peak, max(i_n) AS in_peak,
               max(slot) AS last_seen, min(slot) AS first_seen
        FROM x GROUP BY imei""", [since])
    life = {r["imei"]: r for r in store.query(f"SELECT imei, max(slot) AS last_seen, min(slot) AS first_seen FROM {src} GROUP BY 1")}
    daily = defaultdict(list)
    for r in store.query(f"SELECT imei, slot::DATE AS day, sum(kw) * 0.25 AS kwh, count(*) AS n FROM {src} "
                         f"WHERE slot > ? GROUP BY 1, 2 ORDER BY 1, 2", [as_of - timedelta(days=63)]):
        if r["n"] >= 72:
            daily[r["imei"]].append(r)
    st = {r["imei"]: r for r in stats}
    findings, out = [], {}
    for imei, a in by_imei.items():
        aid, s, l = a["asset_id"], st.get(imei), life[imei]
        fs = []
        if l["last_seen"] < as_of - timedelta(days=1):
            fs.append(_f("critical", aid, "energy", "OFFLINE", "Monitoring unit not reporting",
                         f"last reading {l['last_seen']:%d %b %Y %H:%M}",
                         "No readings: loss of visibility of a live transformer (unit power, SIM or modem).",
                         "Check the STMU's MCB and SIM / signal at site; power-cycle the modem.",
                         l["last_seen"] - timedelta(hours=6), l["last_seen"]))
        m = dict(a)
        if s:
            load_pk = s["peak_kva"] / a["rating_kva"] if s["peak_kva"] else 0
            load_95 = s["p95_kva"] / a["rating_kva"] if s["p95_kva"] else 0
            avail = s["on_slots"] / max(1, s["slots"])
            win = (since, as_of)
            if load_pk > 1.0:
                fs.append(_f("critical" if load_pk > 1.2 else "warning", aid, "energy", "OVERLOAD", "Overloaded at peak",
                             f"peak {s['peak_kva']:.0f} kVA = {load_pk*100:.0f} % of {a['rating_kva']} kVA",
                             "Load above nameplate heats the winding and shortens insulation life.",
                             "Clamp-meter the peak phase currents; plan load transfer or a higher-rated DT.", *win))
            elif load_95 > 0.8:
                fs.append(_f("warning", aid, "energy", "HIGH_LOADING", "Running close to rating",
                             f"95th percentile {load_95*100:.0f} % of rating", "Little headroom for load growth.",
                             "Plan augmentation before the next peak season.", *win))
            if s["pf"] and s["pf"] < 0.9:
                fs.append(_f("critical" if s["pf"] < 0.8 else "warning", aid, "energy", "LOW_PF", "Low power factor",
                             f"PF {s['pf']:.2f}", "Reactive current adds losses and voltage drop.",
                             "Check capacitor banks at large consumers; inspect motor loads.", *win))
            if s["unbal"] and s["unbal"] > 0.2:
                fs.append(_f("critical" if s["unbal"] > 0.35 else "warning", aid, "energy", "UNBALANCE",
                             "Current unbalance between phases", f"{s['unbal']*100:.0f} % average deviation",
                             "One phase runs hotter; neutral current and losses rise.",
                             "Move single-phase connections from the heavy phase to the light one.", *win))
            if s["in_ratio"] and s["in_ratio"] > 0.4:
                fs.append(_f("critical" if s["in_ratio"] > 0.7 else "warning", aid, "energy", "NEUTRAL",
                             "High neutral current", f"neutral = {s['in_ratio']*100:.0f} % of phase current (peak {s['in_peak']:.0f} A)",
                             "Overheats the neutral lug; a broken neutral damages consumer appliances.",
                             "Tighten / replace the neutral lug; rebalance phases.", *win))
            if s["low_v_share"] and s["low_v_share"] > 0.1:
                fs.append(_f("warning", aid, "energy", "LOW_VOLTAGE", "Low voltage", f"{s['low_v_share']*100:.0f} % of the time below {V_LO:.0f} V (min {s['v_floor']:.0f} V)",
                             "Tail-end consumers get poor voltage; motors overheat.", "Check joints and conductor size; consider tap change.", *win))
            if s["high_v_share"] and s["high_v_share"] > 0.1:
                fs.append(_f("warning", aid, "energy", "HIGH_VOLTAGE", "High voltage", f"{s['high_v_share']*100:.0f} % of the time above {V_HI:.0f} V (max {s['v_peak']:.0f} V)",
                             "Appliance stress and higher no-load losses.", "Check the off-load tap position.", *win))
            if s["thd_v"] and s["thd_v"] > 5:
                fs.append(_f("critical" if s["thd_v"] > 8 else "warning", aid, "energy", "THD", "Voltage harmonics",
                             f"THDv {s['thd_v']:.1f} % (median)", "Harmonics heat transformers and disturb electronics.",
                             "Identify non-linear loads (drives, UPS); consider filters.", *win))
            if s["thd_bad"] and s["thd_bad"] > 0.5:
                fs.append(_f("info", aid, "energy", "METER_DATA", "Harmonics channel not usable",
                             f"{s['thd_bad']*100:.0f} % of THD readings at the 60 % scale limit",
                             "The meter reports THD on a different scale; harmonics cannot be judged.",
                             "Check the meter's THD register scaling / firmware setting.", *win))
            if avail < 0.9 and not any(f["code"] == "OFFLINE" for f in fs):
                fs.append(_f("warning", aid, "energy", "SUPPLY", "Supply interruptions", f"supply available {avail*100:.0f} % of the time",
                             "Outages hit consumers and revenue.", "Check HV fuse / feeder trips at the substation log.", *win))
            if s["freq_exc"] and s["freq_exc"] >= 4:
                fs.append(_f("info", aid, "energy", "FREQUENCY", "Grid frequency excursions", f"{s['freq_exc']} intervals outside 49.5–50.5 Hz",
                             "Grid-side stress (not a site fault).", "Note for the grid operator; no site action.", *win))
            d = daily.get(imei, [])
            if len(d) >= 21:
                base = [x["kwh"] for x in d[-35:-7]]
                last7 = statistics.mean(x["kwh"] for x in d[-7:])
                mu, sd = statistics.mean(base), max(statistics.pstdev(base), 0.05 * statistics.mean(base) or 1)
                z = (last7 - mu) / sd
                m["kwh_z"] = round(z, 1)
                if abs(z) >= 3:
                    fs.append(_f("warning", aid, "energy", "CONSUMPTION_SHIFT",
                                 "Consumption jump" if z > 0 else "Consumption drop",
                                 f"last 7 days {last7:.0f} kWh/day vs {mu:.0f} usual (z {z:+.1f})",
                                 "Sudden change: new load, theft / bypass, or meter / CT problem.",
                                 "Compare with consumer billing; inspect for hooking or bypass.",
                                 datetime.combine(d[-7]["day"], datetime.min.time()), as_of))
            m.update(kwh=s["kwh"], peak_kw=s["peak_kw"], peak_kva=s["peak_kva"], load_pk=load_pk, load_95=load_95,
                     pf=s["pf"], unbal=s["unbal"], in_ratio=s["in_ratio"], thd_v=s["thd_v"], avail=avail,
                     low_v=s["low_v_share"], high_v=s["high_v_share"])
        m.update(last_seen=l["last_seen"], first_seen=l["first_seen"], online=l["last_seen"] >= as_of - timedelta(days=1),
                 findings=sorted(fs, key=lambda f: SEV_RANK[f["severity"]]), health=_health(fs),
                 forecast=_forecast(daily.get(imei, []), as_of))
        out[aid] = m
        findings += fs
    return {"assets": out, "as_of": as_of, "findings": sorted(findings, key=lambda f: SEV_RANK[f["severity"]])}


def _forecast(days, as_of):
    """Next 7 days' kWh: same-weekday median of the last 4 weeks, scaled by the 2-week trend."""
    if len(days) < 28:
        return []
    by_dow = defaultdict(list)
    for d in days[-28:]:
        by_dow[d["day"].weekday()].append(d["kwh"])
    trend = statistics.mean(x["kwh"] for x in days[-14:]) / max(1e-6, statistics.mean(x["kwh"] for x in days[-28:-14]))
    trend = min(1.3, max(0.7, trend)) ** 0.5
    out = []
    for k in range(1, 8):
        day = as_of.date() + timedelta(days=k)
        out.append({"day": day, "kwh": round(statistics.median(by_dow[day.weekday()]) * trend, 1)})
    return out


def energy():
    return cached("energy", _energy_build)


def energy_series(asset_id, days=7):
    a = energy()["assets"].get(asset_id)
    src = store.energy_source()
    if not a or not src:
        return []
    end = a["last_seen"]
    return store.query(f"SELECT slot AS ts, supply, kw, kva, v_rn, v_yn, v_bn, i_r, i_y, i_b, i_n, thd_v, freq, "
                       f"kw / nullif(kva, 0) AS pf FROM {src} WHERE imei = ? AND slot > ? ORDER BY slot",
                       [a["imei"], end - timedelta(days=days)])


def energy_daily(asset_id=None, days=365):
    src = store.energy_source()
    if not src:
        return []
    assets = energy()["assets"]
    if asset_id:
        return store.query(f"SELECT slot::DATE AS day, sum(kw) * 0.25 AS kwh, max(kva) AS peak_kva FROM {src} "
                           f"WHERE imei = ? GROUP BY 1 ORDER BY 1", [assets[asset_id]["imei"]])[-days:]
    rows = store.query(f"SELECT slot::DATE AS day, sum(kw) * 0.25 / 1000 AS mwh, count(*) AS n FROM {src} GROUP BY 1 ORDER BY 1")
    if rows:  # drop partial days (start / end of the export)
        med = statistics.median(r["n"] for r in rows)
        rows = [r for r in rows if r["n"] >= 0.8 * med]
        last = store.query(f"SELECT max(slot) AS t FROM {src}")[0]["t"]
        if rows and last.hour < 23 and rows[-1]["day"] == last.date():   # export ends part-way through its last day
            rows = rows[:-1]
    return rows[-days:]


def energy_profile():
    """Fleet hourly load shape over the last 30 days (MW)."""
    src = store.energy_source()
    e = energy()
    if not src or not e["as_of"]:
        return []
    return store.query(f"SELECT hour(slot) AS h, sum(kw) / 1000 / count(DISTINCT slot::DATE) / 4 AS mw FROM {src} "
                       f"WHERE slot > ? GROUP BY 1 ORDER BY 1", [e["as_of"] - WINDOW])


# ── Water ────────────────────────────────────────────────────────────────────

MAX_GAP = timedelta(minutes=30)       # a reading speaks for at most 30 min of pump state
PH_LO, PH_HI = 6.5, 8.5


def _wkind(rows):
    if any(r["ph"] is not None for r in rows):
        return "Water quality monitor"
    if any(r["tank_pct"] is not None for r in rows):
        return "Pump + overhead tank" if any(r["pump"] is not None for r in rows) else "Overhead tank level sensor"
    if any(r["pump"] is not None for r in rows):
        return "Pump controller"
    return "Level switch"


def _water_build():
    rows = store.query(f"SELECT * FROM {store.water_source()} ORDER BY asset_id, ts")
    by = defaultdict(list)
    for r in rows:
        by[r["asset_id"]].append(r)
    out, findings = {}, []
    live_now = datetime.now()
    for aid, rs in by.items():
        # de-duplicate (history ∪ live)
        seen, clean = set(), []
        for r in rs:
            if r["ts"] not in seen:
                seen.add(r["ts"])
                clean.append(r)
        rs = clean
        live = rs[-1]["src"] == "live"
        as_of = live_now if live else rs[-1]["ts"]
        last = rs[-1]["ts"]
        # live controllers: last 30 days; history-only controllers: their whole recorded span
        span_start = last - WINDOW if live else rs[0]["ts"]
        win_rows = [r for r in rs if r["ts"] >= span_start]
        kind = _wkind(rs)
        # pump runtime / starts per day
        run_h, starts, prev = defaultdict(float), defaultdict(int), None
        pumps = [r for r in win_rows if r["pump"] is not None]
        for i, r in enumerate(pumps):
            nxt = pumps[i + 1]["ts"] if i + 1 < len(pumps) else r["ts"]
            if r["pump"] == 1:
                run_h[r["ts"].date()] += min(nxt - r["ts"], MAX_GAP).total_seconds() / 3600
                if prev == 0:
                    starts[r["ts"].date()] += 1
            prev = r["pump"]
        days_seen = max(1, len({r["ts"].date() for r in win_rows}))
        runtime = sum(run_h.values())
        # episodes
        def episodes(pred):
            eps, cur = [], None
            for r in win_rows:
                if pred(r):
                    cur = [r["ts"], r["ts"], 1] if cur is None or r["ts"] - cur[1] > MAX_GAP else [cur[0], r["ts"], cur[2] + 1]
                    if len(eps) and eps[-1][0] == cur[0]:
                        eps[-1] = cur
                    else:
                        eps.append(cur)
                else:
                    cur = None
            return eps
        gaps = sorted((b["ts"] - a["ts"]).total_seconds() for a, b in zip(win_rows, win_rows[1:]) if b["ts"] > a["ts"])
        tick_s = min(900.0, gaps[len(gaps) // 2]) if gaps else 60.0    # this controller's usual reporting interval
        ep_s = lambda e: (e[1] - e[0]).total_seconds() + tick_s           # last reading speaks for one interval
        dry = episodes(lambda r: r["pump"] == 1 and ("dry" in (r["flow"] or "") or (r["sump"] or "") in ("empty", "dry", "no water")
                                                      or (r["well"] or "") == "empty"))
        overflow = episodes(lambda r: r["pump"] == 1 and (r["tank_pct"] or 0) >= 100)
        phase = episodes(lambda r: r["pump"] == 1 and None not in (r["v1"], r["v2"], r["v3"]) and max(r["v1"], r["v2"], r["v3"]) > 180
                         and min(r["v1"], r["v2"], r["v3"]) < 100)
        lowv = episodes(lambda r: r["pump"] == 1 and None not in (r["v1"], r["v2"], r["v3"]) and 100 <= min(r["v1"], r["v2"], r["v3"]) < 200)
        ph_ok = lambda v: v is not None and 0.5 < v <= 14      # 0 / out-of-scale = dead probe, not acid water
        phs = [r["ph"] for r in win_rows if ph_ok(r["ph"])]
        ph_dead = [r for r in win_rows if r["ph"] is not None and not ph_ok(r["ph"])]
        ph_bad = [r for r in win_rows if ph_ok(r["ph"]) and not PH_LO <= r["ph"] <= PH_HI]
        sensor_err = [r for r in win_rows if (r["tank_raw"] or "").upper().replace("_", " ") in ("SENSOR ERROR", "ERROR")]
        levels = [r for r in win_rows if r["tank_pct"] is not None]
        fs = []
        w = (max(span_start, last - WINDOW), last)
        if as_of - last > timedelta(hours=6):
            fs.append(_f("critical" if live else "info", aid, "water", "OFFLINE", "Controller not reporting",
                         f"last reading {last:%d %b %Y %H:%M}",
                         "Pump house runs blind: dry runs and overflows go unnoticed.",
                         "Check controller supply, SIM / signal and antenna at the pump house.", last - timedelta(hours=6), last))
        if dry:
            mins = sum(ep_s(e) for e in dry) / 60
            fs.append(_f("critical" if len(dry) >= 3 else "warning", aid, "water", "DRY_RUN", "Pump running dry",
                         f"{len(dry)} episodes, ~{mins:.0f} min" if mins >= 1 else f"{len(dry)} episodes, ~{mins * 60:.0f} s",
                         "A pump without water overheats its seals and burns the winding within minutes.",
                         "Check sump / well level and foot valve; verify the dry-run float wiring trips the starter.",
                         dry[-1][0] - timedelta(minutes=30), dry[-1][1] + timedelta(minutes=30)))
        if overflow:
            fs.append(_f("warning", aid, "water", "OVERFLOW", "Pump on with tank full",
                         f"{len(overflow)} episodes", "Water and energy wasted through the overflow pipe.",
                         "Check the tank-full float / level sensor and the auto-stop logic.",
                         overflow[-1][0] - timedelta(minutes=30), overflow[-1][1] + timedelta(minutes=30)))
        if phase:
            fs.append(_f("critical", aid, "water", "SINGLE_PHASING", "Pump running on a missing phase",
                         f"{len(phase)} episodes", "A three-phase motor on two phases overheats and burns out.",
                         "Check incoming fuses / contactor contacts; fit or test the phase-failure relay.",
                         phase[-1][0] - timedelta(minutes=30), phase[-1][1] + timedelta(minutes=30)))
        if lowv:
            fs.append(_f("warning", aid, "water", "LOW_VOLTAGE", "Pump running on low voltage",
                         f"{len(lowv)} episodes", "Low voltage raises motor current and temperature.",
                         "Measure voltage at the starter during pumping; report to the electricity utility.",
                         lowv[-1][0] - timedelta(minutes=30), lowv[-1][1] + timedelta(minutes=30)))
        per_day = runtime / days_seen
        st_day = sum(starts.values()) / days_seen
        if pumps and st_day > 12:
            fs.append(_f("warning", aid, "water", "PUMP_CYCLING", "Pump short-cycling", f"{st_day:.0f} starts per day",
                         "Frequent starts wear the starter and motor.", "Check float differential / level sensor hysteresis.", *w))
        if pumps and per_day > 16:
            fs.append(_f("warning", aid, "water", "LONG_RUN", "Pump running most of the day", f"{per_day:.1f} h per day",
                         "Long run-time with no stop suggests a leak, a stuck float or a failing pump.",
                         "Walk the rising main for leaks; check the delivery head and pump output.", *w))
        if ph_bad:
            fs.append(_f("critical" if len(ph_bad) > 0.2 * len(phs) else "warning", aid, "water", "PH", "Water pH out of range",
                         f"{len(ph_bad)} of {len(phs)} readings outside {PH_LO}–{PH_HI} (last {phs[-1]:.1f})",
                         "Unsafe or corrosive water for supply.", "Collect a sample for lab test; check dosing.",
                         ph_bad[-1]["ts"] - timedelta(hours=2), ph_bad[-1]["ts"] + timedelta(hours=2)))
        if sensor_err:
            fs.append(_f("warning", aid, "water", "SENSOR", "Tank level sensor error", f"{len(sensor_err)} readings",
                         "Auto-control cannot see the tank.", "Inspect the level probe and its cable.",
                         sensor_err[-1]["ts"] - timedelta(hours=1), sensor_err[-1]["ts"] + timedelta(hours=1)))
        if ph_dead:
            fs.append(_f("warning", aid, "water", "PH_SENSOR", "pH probe not reading", f"{len(ph_dead)} readings at 0 / out of scale",
                         "Water quality is unmonitored while the probe is dead or dry.",
                         "Check the probe is immersed; clean and recalibrate with pH 4 / 7 buffers, or replace.",
                         ph_dead[-1]["ts"] - timedelta(hours=2), ph_dead[-1]["ts"] + timedelta(hours=2)))
        ep_h = lambda eps: sum(ep_s(e) for e in eps) / 3600
        waste_h = ep_h(dry) + ep_h(overflow)
        kwh = runtime * settings.PUMP_KW
        out[aid] = {
            "asset_id": aid, "utility": "water", "kind": kind, "name": f"{kind} {aid}", "device_id": rs[-1]["device_id"],
            "live": live, "source": "live platform" if live else "platform history",
            "first_seen": rs[0]["ts"], "last_seen": last, "readings": len(rs), "online": as_of - last <= timedelta(hours=6),
            "pump_now": next((r["pump"] for r in reversed(rs) if r["pump"] is not None), None),
            "tank_now": levels[-1]["tank_pct"] if levels else None, "ph_now": phs[-1] if phs else None,
            "window": "last 30 days" if live else f"{span_start:%b %Y} – {last:%b %Y}", "window_days": days_seen,
            "runtime_h": runtime, "runtime_day": per_day if pumps else None, "starts_day": st_day if pumps else None,
            "pump_kwh": kwh, "dry_runs": len(dry), "overflows": len(overflow),
            "waste_h": waste_h, "waste_kwh": waste_h * settings.PUMP_KW,
            "findings": sorted(fs, key=lambda f: SEV_RANK[f["severity"]]), "health": _health(fs),
            "run_daily": sorted(run_h.items()),
        }
        findings += fs
    return {"assets": out, "findings": sorted(findings, key=lambda f: SEV_RANK[f["severity"]]),
            "live_devices": sum(1 for a in out.values() if a["live"]),
            "synced": store.get_meta("water_live_synced"), "sync_error": store.get_meta("water_live_error")}


def water():
    return cached("water", _water_build)


def water_recent(asset_id, end, n=2000, max_days=14):
    """The last n readings up to `end` (bursty controllers: a time window can be nearly empty)."""
    rows = store.query(f"SELECT DISTINCT * FROM {store.water_source()} WHERE asset_id = ? AND ts <= ? AND ts > ? "
                       f"ORDER BY ts DESC LIMIT ?", [asset_id, end, end - timedelta(days=max_days), n])
    # keep the reporting session(s) that end at the case: stop at the first silence > 2 h once 200 readings are in
    out = []
    for r in rows:
        if out and out[-1]["ts"] - r["ts"] > timedelta(hours=2) and len(out) >= 200:
            break
        out.append(r)
    return out[::-1]


def water_series(asset_id, days=7, end=None):
    a = water()["assets"].get(asset_id)
    if not a:
        return []
    end = end or a["last_seen"]
    return store.query(f"SELECT DISTINCT * FROM {store.water_source()} WHERE asset_id = ? AND ts > ? AND ts <= ? ORDER BY ts",
                       [asset_id, end - timedelta(days=days), end])


def bucket_water(rows, minutes=10, compress=False):
    """Bursty controller readings → continuous fixed slots (pump on if on at any time, last tank level,
    mean pH, phase voltages while pumping). Empty slots stay None so gaps show."""
    if not rows:
        return []
    step = timedelta(minutes=minutes)
    t0 = rows[0]["ts"].replace(second=0, microsecond=0)
    t0 -= timedelta(minutes=t0.minute % minutes)
    slots = defaultdict(list)
    for r in rows:
        slots[int((r["ts"] - t0) / step)].append(r)
    out = []
    keys = range(int((rows[-1]["ts"] - t0) / step) + 1)
    if compress:   # silent stretches collapse into a single gap marker (a break in the line)
        ks, prev = [], None
        for k in sorted(slots):
            if prev is not None and k - prev > 1:
                ks.append(("gap", prev + 1))
            ks.append(k)
            prev = k
        keys = ks
    for k in keys:
        if isinstance(k, tuple):
            out.append({"ts": t0 + k[1] * step, "gap": True, "pump": None, "tank": None, "ph": None, "dry": None, "float": None,
                        "v1": None, "v2": None, "v3": None})
            continue
        rs = slots.get(k, [])
        pumps = [r["pump"] for r in rs if r["pump"] is not None]
        tanks = [r["tank_pct"] for r in rs if r["tank_pct"] is not None]
        phs = [r["ph"] for r in rs if r["ph"] is not None and 0.5 < r["ph"] <= 14]
        on = [r for r in rs if r["pump"] == 1 and r["v1"] is not None] or [r for r in rs if r["v1"] is not None]
        flows = [r for r in rs if r["flow"] or r["sump"] or r["well"]]
        floats = [r["float_sw"] for r in rs if r["float_sw"]]
        out.append({"ts": t0 + k * step, "pump": max(pumps) if pumps else None, "tank": tanks[-1] if tanks else None,
                    "ph": round(sum(phs) / len(phs), 2) if phs else None,
                    # 1 = dry (flow "dry run" / sump or well empty), 0 = water present
                    "dry": (1 if any("dry" in (r["flow"] or "") or (r["sump"] or "") in ("empty", "dry", "no water")
                                     or (r["well"] or "") == "empty" for r in flows) else 0) if flows else None,
                    "float": (1 if floats[-1] in ("full", "true", "1", "on") else 0) if floats else None,
                    **{f"v{i}": (min(r[f"v{i}"] for r in on if r[f"v{i}"] is not None) if on else None) for i in (1, 2, 3)}})
    return out


def water_signals(asset_id):
    """Which signals this controller actually reports (counts over its whole history)."""
    r = store.query(f"""SELECT count(pump) AS pump, count(tank_pct) AS tank,
                               count(CASE WHEN ph > 0.5 AND ph <= 14 THEN 1 END) AS ph, count(ph) AS ph_any,
                               count(CASE WHEN coalesce(flow, sump, well) IS NOT NULL THEN 1 END) AS dry,
                               count(float_sw) AS float, count(CASE WHEN greatest(coalesce(v1, 0), coalesce(v2, 0), coalesce(v3, 0)) > 0 THEN 1 END) AS volts
                        FROM {store.water_source()} WHERE asset_id = ?""", [asset_id])[0]
    return {k: (v or 0) > 0 for k, v in r.items()}


def case_window_end(a):
    """End of the chart window: the most severe recent finding (so the chart shows the case), else the last reading."""
    timed = [f for f in a["findings"] if f["end"] and f["code"] not in ("OFFLINE",)]
    if not timed:
        return a["last_seen"]
    top = min(SEV_RANK[f["severity"]] for f in timed)
    return max(f["end"] for f in timed if SEV_RANK[f["severity"]] == top) + timedelta(hours=12)


# ── Common ───────────────────────────────────────────────────────────────────

def asset(asset_id):
    return energy()["assets"].get(asset_id) or water()["assets"].get(asset_id)


def evidence(asset_id, start, end):
    """Readings behind a finding (plus 1 h either side)."""
    a = asset(asset_id)
    if not a:
        return []
    s, e = start - timedelta(hours=1), end + timedelta(hours=1)
    if a["utility"] == "energy":
        return store.query(f"SELECT slot AS ts, supply, v_rn, v_yn, v_bn, i_r, i_y, i_b, i_n, kw, kva, "
                           f"kw / nullif(kva, 0) AS pf, thd_v, freq FROM {store.energy_source()} "
                           f"WHERE imei = ? AND slot BETWEEN ? AND ? ORDER BY slot LIMIT 400", [a["imei"], s, e])
    return store.query(f"SELECT DISTINCT ts, pump, tank_raw, tank_pct, sump, flow, well, ph, v1, v2, v3, src "
                       f"FROM {store.water_source()} WHERE asset_id = ? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 400",
                       [asset_id, s, e])


def meter_block():
    from . import meters
    return cached("meters", lambda: {"summary": meters.summary(), "findings": meters.findings()})


def balanced(fs, n=25):
    """Most severe first, rotating across utilities so one large fleet cannot crowd out the others."""
    out, pools = [], defaultdict(list)
    for f in fs:
        pools[(SEV_RANK[f["severity"]], f["utility"])].append(f)
    for sev in sorted({k[0] for k in pools}):
        queues = [pools[k] for k in sorted(pools) if k[0] == sev]
        while any(queues) and len(out) < n:
            for q in queues:
                if q and len(out) < n:
                    out.append(q.pop(0))
    return out


def xfmr_summary():
    """Native transformer-intelligence counts for the overview card."""
    from . import dtintel
    x = dtintel.fleet()["by_id"]
    vals = list(x.values())
    def soon(v, key):
        o = v.get("outlook") or {}
        return bool(o.get(key) and o[key]["days_warn"] and o[key]["days_warn"] <= 150)
    return {"n": len(vals),
            "risk7": sum(1 for v in vals if (v.get("forecast") or {}).get("max_p_overload", 0) >= 30),
            "over5": sum(1 for v in vals if soon(v, "load")),
            "hot5": sum(1 for v in vals if soon(v, "wti")),
            "imb": sum(1 for v in vals if v.get("phase") and v["phase"]["flagged"])}


def overview():
    e, w, mb = energy(), water(), meter_block()
    ea, wa = list(e["assets"].values()), list(w["assets"].values())
    from . import solar
    allf = sorted(e["findings"] + w["findings"] + mb["findings"] + solar.findings(), key=lambda f: SEV_RANK[f["severity"]])
    def count(fs, sev):
        return sum(1 for f in fs if f["severity"] == sev)
    pump_kwh = sum(a["pump_kwh"] for a in wa)
    return {
        "energy": {"n": len(ea), "online": sum(1 for a in ea if a["online"]), "as_of": e["as_of"],
                   "mwh_30d": sum(a.get("kwh") or 0 for a in ea) / 1000,
                   "peak_mw": sum(a.get("peak_kw") or 0 for a in ea) / 1000,
                   "critical": count(e["findings"], "critical"), "warning": count(e["findings"], "warning"),
                   "health": round(statistics.mean(a["health"] for a in ea)) if ea else None,
                   "loss_hint": sum(1 for f in e["findings"] if f["code"] in ("UNBALANCE", "NEUTRAL", "LOW_PF"))},
        "water": {"n": len(wa), "online": sum(1 for a in wa if a["online"]), "live": w["live_devices"],
                  "synced": w["synced"], "sync_error": w["sync_error"],
                  "runtime_h": sum(a["runtime_h"] for a in wa), "pump_kwh": pump_kwh,
                  "dry_runs": sum(a["dry_runs"] for a in wa), "overflows": sum(a["overflows"] for a in wa),
                  "critical": count(w["findings"], "critical"), "warning": count(w["findings"], "warning"),
                  "health": round(statistics.mean(a["health"] for a in wa)) if wa else None},
        "nexus": {"pump_kwh": pump_kwh, "pump_cost": pump_kwh * settings.TARIFF_RS_KWH,
                  "wasted_kwh": sum(a["waste_kwh"] for a in wa)},
        "meters": mb["summary"],
        "xfmr": xfmr_summary(),
        "solar": solar.summary(),
        "findings": allf,
        "top": balanced(allf),
        "by_code": _by_code(allf),
    }


def _by_code(fs):
    c = defaultdict(lambda: {"n": 0, "critical": 0, "utility": None, "title": None})
    for f in fs:
        x = c[f["code"]]
        x["n"] += 1
        x["critical"] += f["severity"] == "critical"
        x["utility"], x["title"] = f["utility"], f["title"]
    return sorted(({"code": k, **v} for k, v in c.items()), key=lambda x: (-x["n"], -x["critical"]))
