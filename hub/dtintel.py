"""Transformer intelligence on the hub's own STMU readings: phase balance, load forecast, capacity plan
and the growth outlook (load, top-oil and hot-spot temperature) — five months ahead.

Data: 15-minute per-phase readings of the 31 monitored distribution transformers (about one year).

Methods (simple and explainable):

* Phase balance — each phase's share of the average current over the last 7 days, and the heaviest
  phase's peak against its rated current (a phase can be overloaded while the transformer is not).
* 7-day forecast — median weekday × 15-minute profile of the last 4 weeks. The ±band and P(overload)
  come from how far real daily peaks strayed from the same-weekday median over the last 8 weeks.
* Year-on-year growth — the latest fortnight's mean daily peak against the first fortnight on record,
  one year earlier (same season), held to ±20 %/yr.
* Capacity plan and growth outlook — the transformer's own peak on the same month / date last year,
  scaled by its growth.
* Temperatures — not measured by the STMU, so they are ESTIMATED: IEC 60076-7 steady state at the
  day's 2-hour peak load, on a typical Indian monthly maximum ambient (ONAN distribution transformer:
  top-oil rise 55 K, hot-spot gradient 23 K, R 5, x 0.8, y 1.6).
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import analytics, store

MAX_GROWTH = 0.20
HORIZON = 150                # days drawn ahead on the growth outlook
HISTORY = 120                # days of measured history drawn before it
SCAN = 365

# IEC 60076-7 ONAN distribution transformer
TOP_OIL_RISE, HOTSPOT_GRAD, LOSS_RATIO, X_OIL, Y_WIND = 55.0, 23.0, 5.0, 0.8, 1.6
OTI_ALARM, OTI_TRIP, WTI_ALARM, WTI_TRIP = 85.0, 95.0, 100.0, 110.0
# Typical daily-maximum ambient in the Indian plains by month (°C) — an estimate, not a measurement.
AMBIENT_MAX = [23, 27, 33, 38, 41, 39, 34, 33, 33, 32, 28, 24]

PHASES = ("R", "Y", "B")
IDLE_SHARE, HEAVY_SHARE = 20.0, 45.0


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def ambient(d: date) -> float:
    """Monthly maximum ambient, interpolated between mid-months."""
    m = d.month - 1
    f = (d.day - 15) / 30.0
    a, b = (AMBIENT_MAX[m], AMBIENT_MAX[(m + 1) % 12]) if f >= 0 else (AMBIENT_MAX[(m - 1) % 12], AMBIENT_MAX[m])
    return a + (b - a) * (f if f >= 0 else 1 + f)


def temps(k: float, amb: float):
    """Steady-state top-oil and hot-spot (°C) at per-unit load k."""
    k = max(0.0, k)
    oti = amb + TOP_OIL_RISE * ((1 + LOSS_RATIO * k * k) / (1 + LOSS_RATIO)) ** X_OIL
    return oti, oti + HOTSPOT_GRAD * k ** Y_WIND


def _crossing(vals, limit):
    for k, v in enumerate(vals):
        if v is not None and v >= limit:
            return k
    return None


def phase_balance(a, row):
    if not row or not row.get("avg_r") or not (row["avg_r"] + row["avg_y"] + row["avg_b"]):
        return None
    avgs = [row["avg_r"], row["avg_y"], row["avg_b"]]
    total = sum(avgs)
    shares = [100 * x / total for x in avgs]
    hi, lo = max(range(3), key=lambda i: shares[i]), min(range(3), key=lambda i: shares[i])
    peaks = [row["max_r"] or 0, row["max_y"] or 0, row["max_b"] or 0]
    heavy_peak = 100 * peaks[hi] / a["rated_a"] if a.get("rated_a") else 0
    flagged = any(f["code"] == "UNBALANCE" for f in a.get("findings", []))
    if shares[lo] < IDLE_SHARE:
        pattern, text = "two-phase", f"two phases loaded, {PHASES[lo]} nearly idle"
    elif shares[hi] >= HEAVY_SHARE:
        pattern, text = "one-phase", f"{PHASES[hi]} phase carries the load"
    elif flagged:
        pattern, text = ("uneven", "uneven") if shares[hi] - shares[lo] >= 10 else ("shifting", "heavy phase keeps shifting")
    else:
        pattern, text = ("balanced", "balanced") if shares[hi] - shares[lo] < 10 else ("within", "within limit")
    return {"shares": [round(s, 1) for s in shares], "shares_text": " · ".join(f"{p} {s:.0f} %" for p, s in zip(PHASES, shares)),
            "pattern": pattern, "text": text, "flagged": flagged or pattern in ("two-phase", "one-phase"),
            "heavy": PHASES[hi], "light": PHASES[lo], "heavy_peak_pct": round(heavy_peak)}


def _build():
    e = analytics.energy()
    src = store.energy_source()
    if not src or not e["as_of"]:
        return {"by_id": {}, "as_of": None}
    as_of = e["as_of"]
    today = as_of.date()
    assets = e["assets"]
    by_imei = {a["imei"]: a for a in assets.values()}

    ph = {r["imei"]: r for r in store.query(f"""
        SELECT imei, avg(i_r) AS avg_r, avg(i_y) AS avg_y, avg(i_b) AS avg_b,
               max(i_r) AS max_r, max(i_y) AS max_y, max(i_b) AS max_b
        FROM {src} WHERE slot > ? AND supply = 1 AND (i_r + i_y + i_b) / 3 > 5 GROUP BY 1""", [as_of - timedelta(days=7)])}

    # Daily peak and daily 2-hour peak (rolling mean of 8 slots) per transformer.
    daily = defaultdict(dict)
    for r in store.query(f"""
        WITH r AS (SELECT imei, slot, kva,
                          avg(kva) OVER (PARTITION BY imei ORDER BY slot ROWS BETWEEN 7 PRECEDING AND CURRENT ROW) AS kva2h
                   FROM {src} WHERE supply = 1)
        SELECT imei, slot::DATE AS day, max(kva) AS peak, max(kva2h) AS peak2h, count(*) AS n
        FROM r GROUP BY 1, 2""", []):
        if r["n"] >= 48:
            daily[r["imei"]][r["day"]] = r

    prof = defaultdict(dict)
    for r in store.query(f"""
        SELECT imei, dayofweek(slot) AS dow, hour(slot) * 4 + minute(slot) // 15 AS q, median(kva) AS kva
        FROM {src} WHERE slot > ? AND supply = 1 GROUP BY 1, 2, 3""", [as_of - timedelta(days=28)]):
        prof[r["imei"]][(r["dow"], r["q"])] = r["kva"]

    out = {}
    for imei, a in by_imei.items():
        rating = a["rating_kva"]
        days = daily.get(imei, {})
        res = {"asset_id": a["asset_id"], "rating_kva": rating, "phase": phase_balance(a, ph.get(imei))}
        res.update(_forecast(a, days, prof.get(imei, {}), as_of))
        out[a["asset_id"]] = res
    return {"by_id": out, "as_of": as_of}


def _forecast(a, days, prof, as_of):
    rating = a["rating_kva"]
    today = as_of.date()
    ordered = sorted(days)
    if len(ordered) < 60 or not prof:
        return {"forecast": None, "months": [], "outlook": None}

    # Back-test spread: real daily peak / same-weekday median of the previous 4 weeks, last 8 weeks.
    by_dow, ratios = defaultdict(list), []
    for d in ordered:
        p = days[d]["peak"]
        prev = by_dow[d.weekday()][-4:]
        if len(prev) >= 3 and d > today - timedelta(days=56) and statistics.median(prev) > 0:
            ratios.append(p / statistics.median(prev))
        by_dow[d.weekday()].append(p)
    sigma = statistics.pstdev(ratios) if len(ratios) > 1 else 0.08
    mape = statistics.mean(abs(r - 1) for r in ratios) * 100 if ratios else None

    # Growth: latest fortnight vs the first fortnight on record (~a year earlier, same season).
    first = [days[d]["peak"] for d in ordered[:14]]
    last = [days[d]["peak"] for d in ordered[-14:]]
    span_years = max(0.5, (ordered[-1] - ordered[0]).days / 365)
    raw = (statistics.mean(last) / statistics.mean(first)) ** (1 / span_years) - 1 if first and statistics.mean(first) else 0.0
    growth = max(-MAX_GROWTH, min(MAX_GROWTH, raw))

    # 7-day path (DuckDB dayofweek: Sunday = 0).
    start = as_of.replace(minute=as_of.minute - as_of.minute % 15, second=0, microsecond=0) + timedelta(minutes=15)
    path_ts, path = [], []
    for i in range(7 * 96):
        ts = start + timedelta(minutes=15 * i)
        v = prof.get(((ts.weekday() + 1) % 7, ts.hour * 4 + ts.minute // 15))
        if v is not None:
            path_ts.append(ts)
            path.append(v)
    fdays = []
    for k in range(1, 8):
        d = today + timedelta(days=k)
        vals = [v for t, v in zip(path_ts, path) if t.date() == d]
        if not vals:
            continue
        pk = max(vals) / rating
        p_over = 1 - _norm_cdf((1 / pk - 1) / max(sigma, 0.03)) if pk > 0 else 0
        fdays.append({"day": d, "peak_pct": round(pk * 100, 1), "peak_kva": round(pk * rating),
                      "p_overload": round(p_over * 100), "overload_h": round(sum(0.25 for v in vals if v > rating), 1)})

    # Same date last year, smoothed over a week, scaled by growth.
    def last_year(d, key="peak"):
        vals = [days[x][key] for x in (d - timedelta(days=365 + j) for j in range(-3, 4)) if x in days]
        return statistics.median(vals) if vals else None

    # 12-month capacity plan.
    months = []
    for i in range(12):
        y, m = divmod(today.month - 1 + i, 12)
        y += today.year
        prev = [days[d]["peak"] for d in ordered if d.month == m + 1 and d.year == y - 1]
        this = [days[d]["peak"] for d in ordered if d.month == m + 1 and d.year == y]
        base = max(prev) * (1 + growth) if prev else None
        pk = max([x for x in (base, max(this) if this else None) if x is not None], default=None)
        if pk is None:
            continue
        months.append({"month": date(y, m + 1, 1), "peak_kva": round(pk), "peak_pct": round(pk / rating * 100, 1),
                       "headroom_kva": round(rating - pk),
                       "status": "critical" if pk >= 1.2 * rating else "warning" if pk >= rating else "ok"})

    # Growth outlook: history + projection of daily peak load and estimated temperatures.
    hist_days = [d for d in ordered if d > today - timedelta(days=HISTORY)]
    def series(k_of, amb_of, which):
        return [(temps(k, amb)[which] if k is not None else None) for k, amb in zip(k_of, amb_of)]
    h_load = [days[d]["peak"] / rating for d in hist_days]
    h_k2h = [days[d]["peak2h"] / rating for d in hist_days]
    h_amb = [ambient(d) for d in hist_days]
    proj_days = [today + timedelta(days=k) for k in range(1, SCAN + 1)]
    p_load, p_k2h = [], []
    for d in proj_days:
        ly, ly2 = last_year(d), last_year(d, "peak2h")
        p_load.append(ly * (1 + growth) / rating if ly else None)
        p_k2h.append(ly2 * (1 + growth) / rating if ly2 else None)
    p_amb = [ambient(d) for d in proj_days]
    p_oti, p_wti = series(p_k2h, p_amb, 0), series(p_k2h, p_amb, 1)

    def block(label, unit, hist, proj, warn, crit, crit_label):
        proj_pct = [round(v, 1) if v is not None else None for v in proj]
        dw, dc = _crossing(proj_pct, warn), _crossing(proj_pct, crit)
        shown = proj_pct[:HORIZON]
        peak = max((v for v in shown if v is not None), default=None)
        return {"label": label, "unit": unit, "warn": warn, "crit": crit, "crit_label": crit_label,
                "now": round(hist[-1], 1) if hist and hist[-1] is not None else None,
                "proj_peak": peak, "proj_peak_day": proj_days[shown.index(peak)] if peak is not None else None,
                "days_warn": dw + 1 if dw is not None else None, "days_crit": dc + 1 if dc is not None else None,
                "date_warn": proj_days[dw] if dw is not None else None, "date_crit": proj_days[dc] if dc is not None else None,
                "series": {"day": [d.isoformat() for d in hist_days], "actual": [round(v, 1) if v is not None else None for v in hist],
                           "proj_day": [d.isoformat() for d in proj_days[:HORIZON]], "proj": shown}}

    outlook = {
        "load": block("Peak load", "% of rating", [v * 100 for v in h_load], [v * 100 if v else None for v in p_load], 100.0, 120.0, "Critical"),
        "oti": block("Top-oil temperature (estimated)", "°C", series(h_k2h, h_amb, 0), p_oti, OTI_ALARM, OTI_TRIP, "Trip"),
        "wti": block("Winding hot-spot (estimated)", "°C", series(h_k2h, h_amb, 1), p_wti, WTI_ALARM, WTI_TRIP, "Trip"),
    }
    peaks = [d["peak_pct"] for d in fdays]
    return {
        "forecast": {"days": fdays, "sigma_pct": round(sigma * 100, 1), "mape_pct": round(mape, 1) if mape is not None else None,
                     "growth_pct": round(growth * 100, 1), "growth_raw_pct": round(raw * 100, 1),
                     "max_p_overload": max((d["p_overload"] for d in fdays), default=0),
                     "max_peak_pct": max(peaks) if peaks else None,
                     "path": {"ts": [t.isoformat() for t in path_ts], "pct": [round(v / rating * 100, 1) for v in path]}},
        "months": months,
        "first_over_month": next((m for m in months if m["status"] != "ok"), None),
        "outlook": outlook,
    }


def fleet():
    return analytics.cached("dtintel", _build)


def get(asset_id):
    return fleet()["by_id"].get(asset_id)
