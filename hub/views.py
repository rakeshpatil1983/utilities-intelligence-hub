from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from . import ai, analytics, meters, water_live


def _jsonable(d):
    """Plain JSON-safe structure for json_script (dates → strings)."""
    return json.loads(json.dumps(d, default=str))


def _iso(v):
    return v.isoformat(timespec="minutes") if isinstance(v, datetime) else v


def _filter(fs, request):
    u, s, c = request.GET.get("utility"), request.GET.get("severity"), request.GET.get("code")
    return [f for f in fs if (not u or f["utility"] == u) and (not s or f["severity"] == s) and (not c or f["code"] == c)]


def _solar_plants():
    from . import solar
    sol = solar.fleet()
    if not sol:
        return None
    return {"p": [p["plant"] for p in sol["plants"]], "mwh": [round((p.get("e30_kwh") or 0) / 1000, 1) for p in sol["plants"]]}


def overview(request):
    ov = analytics.overview()
    daily = analytics.energy_daily(days=120)
    runtime = {}
    for a in analytics.water()["assets"].values():
        for d, h in a["run_daily"]:
            runtime[d] = runtime.get(d, 0) + h
    rings = [{"label": "Energy health", "value": ov["energy"]["health"], "color": "energy", "url": "/energy/"},
             {"label": "Meter health (MHI)", "value": (ov["meters"] or {}).get("mhi"), "color": "meters", "url": "/meters/"},
             {"label": "Solar health", "value": (ov.get("solar") or {}).get("health"), "color": "solar", "url": "/solar/"},
             {"label": "Water health", "value": ov["water"]["health"], "color": "water", "url": "/water/"}]
    return render(request, "hub/overview.html", {
        "nav": "overview", "ov": ov, "findings": ov["top"], "rings": rings,
        "charts": _jsonable({
            "energy_daily": {"day": [str(r["day"]) for r in daily], "mwh": [round(r["mwh"], 2) for r in daily]},
            "profile": {"h": [r["h"] for r in analytics.energy_profile()], "mw": [round(r["mw"], 3) for r in analytics.energy_profile()]},
            "issues": {"label": [b["title"][:42] for b in ov["by_code"][:14]], "n": [b["n"] for b in ov["by_code"][:14]],
                       "utility": [b["utility"] for b in ov["by_code"][:14]]},
            "runtime": {"day": [str(d) for d in sorted(runtime)], "h": [round(runtime[d], 2) for d in sorted(runtime)]},
            "solar": _solar_plants(),
        }),
    })


def energy_view(request):
    """Energy · transformer intelligence: one list of the monitored DTs (health, phase balance, predictions)
    with the fleet's energy charts and a peak-load-vs-nameplate histogram."""
    from . import dtintel
    e, x = analytics.energy(), dtintel.fleet()
    rows = []
    for a in sorted(e["assets"].values(), key=lambda a: (a["health"], a["asset_id"])):
        d = x["by_id"].get(a["asset_id"]) or {}
        tags = _xfmr_tags(a, d) + [f["code"].lower() for f in a["findings"]] + ([] if a["online"] else ["offline"])
        if {"risk7", "over5", "hot5"} & set(tags):
            tags.append("predicted")
        rows.append({"a": a, "d": d, "tags": " ".join(dict.fromkeys(tags))})
    has = lambda r, t: t in r["tags"].split()
    k = {"n": len(rows), "online": sum(1 for r in rows if r["a"]["online"]),
         "critical": sum(1 for r in rows if has(r, "critical")),
         "warning": sum(1 for r in rows if any(f["severity"] == "warning" for f in r["a"]["findings"])),
         **{t: sum(1 for r in rows if has(r, t)) for t in ("risk7", "over5", "hot5", "predicted")},
         "imb": sum(1 for r in rows if has(r, "imbalance") or has(r, "unbalance")),
         "mwh_30d": sum(r["a"].get("kwh") or 0 for r in rows) / 1000}
    peak = sorted(({"id": r["a"]["asset_id"], "now": round((r["a"].get("load_pk") or 0) * 100, 1),
                    "proj": ((r["d"].get("outlook") or {}).get("load") or {}).get("proj_peak"),
                    "kva": r["a"]["rating_kva"]} for r in rows), key=lambda p: -(p["proj"] or p["now"]))
    daily = analytics.energy_daily(days=365)
    return render(request, "hub/energy.html", {
        "nav": "energy", "e": e, "rows": rows, "k": k, "as_of": e["as_of"],
        "charts": _jsonable({"energy_daily": {"day": [str(r["day"]) for r in daily], "mwh": [round(r["mwh"], 2) for r in daily]},
                              "profile": {"h": [r["h"] for r in analytics.energy_profile()],
                                          "mw": [round(r["mw"], 3) for r in analytics.energy_profile()]},
                              "peak": {"id": [p["id"] for p in peak], "now": [p["now"] for p in peak],
                                       "proj": [round(p["proj"], 1) if p["proj"] is not None else None for p in peak]}}),
    })


def water_view(request):
    from collections import defaultdict
    from . import store
    w = analytics.water()
    assets = sorted(w["assets"].values(), key=lambda a: a["health"])
    src = store.water_source()
    runtime = defaultdict(float)
    for a in assets:
        for d, h in a["run_daily"]:
            runtime[d] += h
    got = {r["k"]: r["n"] for r in store.query(f"SELECT date_trunc('month', ts)::DATE AS k, count(*) AS n FROM {src} GROUP BY 1")}
    monthly = []
    if got:   # every month in the span, zero where nothing arrived
        y, mth = min(got).year, min(got).month
        while (y, mth) <= (max(got).year, max(got).month):
            d = date(y, mth, 1)
            monthly.append({"m": d.strftime("%b %Y"), "n": got.get(d, 0)})
            y, mth = (y + 1, 1) if mth == 12 else (y, mth + 1)
    tank = {r["b"]: r["n"] for r in store.query(f"SELECT CASE WHEN tank_pct <= 0 THEN 'Empty' WHEN tank_pct < 50 THEN 'Low' "
                                                 f"WHEN tank_pct < 100 THEN 'Part full' ELSE 'Full' END AS b, count(*) AS n "
                                                 f"FROM {src} WHERE tank_pct IS NOT NULL GROUP BY 1")}
    daily = defaultdict(list)
    for r in store.query(f"SELECT asset_id, ts::DATE AS d, count(*) AS n FROM {src} GROUP BY 1, 2 ORDER BY 1, 2"):
        daily[r["asset_id"]].append(r["n"])
    codes = defaultdict(int)
    for f in w["findings"]:
        codes[f["title"]] += 1
    band = lambda h: "Healthy" if h >= 80 else "Needs attention" if h >= 50 else "Critical"
    ptags = {}
    for a in assets:
        pr = _pred_water(a)
        ptags[a["asset_id"]] = "predicted" if (pr["dry30"] >= 1 or pr["over30"] >= 1 or pr["waste30"] > 0) else ""
    return render(request, "hub/water.html", {
        "nav": "water", "w": w, "assets": assets, "ptags": ptags,
        "configured": water_live.configured(), "api_base": settings.WATER_API_BASE, "pump_kw": settings.PUMP_KW,
        "k": {"n": len(assets), "online": sum(a["online"] for a in assets), "runtime": sum(a["runtime_h"] for a in assets),
              "dry": sum(a["dry_runs"] for a in assets), "over": sum(a["overflows"] for a in assets),
              "kwh": sum(a["pump_kwh"] for a in assets), "readings": sum(a["readings"] for a in assets),
              "critical": sum(1 for f in w["findings"] if f["severity"] == "critical")},
        "charts": _jsonable({
            "runtime": {"day": [str(d) for d in sorted(runtime)], "h": [round(runtime[d], 2) for d in sorted(runtime)]},
            "health": {"label": [f"{a['asset_id']} · {band(a['health'])}" for a in assets], "v": [a["health"] for a in assets]},
            "issues": {"label": sorted(codes, key=lambda k: -codes[k]), "n": sorted(codes.values(), reverse=True)},
            "tank": {"label": [b for b in ("Empty", "Low", "Part full", "Full") if b in tank], "n": [tank[b] for b in ("Empty", "Low", "Part full", "Full") if b in tank]},
            "monthly": {"m": [r["m"] for r in monthly], "n": [r["n"] for r in monthly]},
            "spark": {a["asset_id"]: daily[a["asset_id"]][-60:] for a in assets},
        }),
    })


def asset_view(request, asset_id):
    a = analytics.asset(asset_id)
    if not a:
        raise Http404("asset")
    if a["utility"] == "energy":
        from django.shortcuts import redirect
        q = request.META.get("QUERY_STRING")
        return redirect(f"/transformer/{asset_id}/" + (f"?{q}" if q else ""))
    if a["utility"] == "energy":
        s = analytics.energy_series(asset_id, days=7)
        d = analytics.energy_daily(asset_id, days=365)
        chart = {"ts": [r["ts"].strftime("%d %b %H:%M") for r in s], "kw": [r["kw"] for r in s],
                 "vr": [r["v_rn"] for r in s], "vy": [r["v_yn"] for r in s], "vb": [r["v_bn"] for r in s],
                 "ir": [r["i_r"] for r in s], "iy": [r["i_y"] for r in s], "ib": [r["i_b"] for r in s], "in": [r["i_n"] for r in s],
                 "day": [str(r["day"]) for r in d], "kwh": [round(r["kwh"], 1) for r in d],
                 "fc_day": [str(f["day"]) for f in a["forecast"]], "fc_kwh": [f["kwh"] for f in a["forecast"]],
                 "rated_a": a["rated_a"], "v_lo": analytics.V_LO, "v_hi": analytics.V_HI}
        from . import dtintel
        d = dtintel.get(asset_id) or {}
        chart["outlook"] = _outlook_chart(d)
        outlook = [(k, d["outlook"][k]) for k in ("load", "oti", "wti")] if d.get("outlook") else []
        return render(request, "hub/asset.html", {"nav": "energy", "a": a, "d": d, "fc": d.get("forecast"), "outlook": outlook,
                                                  "chart": _jsonable(chart)})
    else:
        sig = analytics.water_signals(asset_id)
        if sig["pump"]:      # pump controllers: the week around the latest case
            end, days = min(analytics.case_window_end(a), a["last_seen"]), 7
        else:                # sensors only: their whole recorded span (max a year)
            end = a["last_seen"]
            days = min(365, max(1, (a["last_seen"] - a["first_seen"]).total_seconds() / 86400 + 0.05))
        s = analytics.water_recent(asset_id, end) if sig["pump"] else analytics.water_series(asset_id, days=days, end=end)
        for minutes in (1, 2, 5, 10, 15, 30, 60, 120, 240, 1440):
            b = analytics.bucket_water(s, minutes=minutes, compress=True)
            if len(b) <= 400:
                break
        compressed = any(r.get("gap") for r in b)
        chart = {"ts": [r["ts"].strftime("%d %b %H:%M") for r in b], "pump": [r["pump"] for r in b],
                 "tank": [r["tank"] for r in b], "ph": [r["ph"] for r in b], "dry": [r["dry"] for r in b],
                 "float": [r["float"] for r in b],
                 "v1": [r["v1"] for r in b], "v2": [r["v2"] for r in b], "v3": [r["v3"] for r in b],
                 "run_day": [str(d) for d, _ in a["run_daily"]], "run_h": [round(h, 2) for _, h in a["run_daily"]],
                 "sig": sig, "bucket": minutes, "compressed": compressed,
                 "window": f"{s[0]['ts']:%d %b %Y %H:%M} – {s[-1]['ts']:%d %b %Y %H:%M}" if s else ""}
        # draw a chart only when the window actually holds that signal
        sig = {k: v and (k not in chart or any(x is not None for x in chart.get({"tank": "tank", "ph_any": "ph", "volts": "v1"}.get(k, k), [])))
               for k, v in sig.items()} | {"history": sig}
        chart["sig"] = sig
        measures = [n for k, n in (("pump", "pump"), ("tank", "tank level"), ("dry", "flow / sump"), ("volts", "phase voltages"),
                                   ("ph_any", "pH"), ("float", "float switch")) if sig["history"][k]]
        pred = _pred_water(a)
        chart["pred"] = pred["chart"]
        return render(request, "hub/asset.html", {"nav": "water", "a": a, "sig": sig, "measures": measures, "pred": pred,
                                                  "pump_kw": settings.PUMP_KW, "tariff": settings.TARIFF_RS_KWH, "chart": _jsonable(chart)})
    return render(request, "hub/asset.html", {"nav": a["utility"], "a": a, "chart": _jsonable(chart)})


# ── predictive tabs (each from the device's own data) ───────────────────────

def _outlook_chart(d):
    return {k: {**g["series"], "warn": g["warn"], "crit": g["crit"], "crit_label": g["crit_label"]}
            for k, g in (d.get("outlook") or {}).items()}


def _pred_water(a):
    days = max(1.0, float(a.get("window_days") or 1))
    per30 = lambda x: round((x or 0) / days * 30, 1)
    rd = a.get("run_daily") or []
    last = [h for _, h in rd[-14:]]
    avg = sum(last) / len(last) if last else 0
    base = a["last_seen"].date() if a.get("last_seen") else datetime.now().date()
    kwh30 = per30(a.get("pump_kwh"))
    return {"days": round(days), "dry30": per30(a.get("dry_runs")), "over30": per30(a.get("overflows")),
            "kwh30": kwh30, "cost30": round(kwh30 * settings.TARIFF_RS_KWH), "waste30": per30(a.get("waste_kwh")),
            "waste_cost30": round(per30(a.get("waste_kwh")) * settings.TARIFF_RS_KWH), "run_avg": round(avg, 2),
            "chart": {"day": [str(d) for d, _ in rd[-30:]] + [str(base + timedelta(days=k)) for k in range(1, 8)],
                      "h": [round(h, 2) for _, h in rd[-30:]] + [None] * 7,
                      "fc": [None] * len(rd[-30:]) + [round(avg, 2)] * 7}}


def _pred_solar(b):
    pr30, pr7 = b.get("pr30") or 0, b.get("pr7") or 0
    slope = (pr7 - pr30) / 11.5                          # % per day, 30-day vs 7-day averages
    proj = max(0, min(100, pr7 + slope * 30))
    days70 = round((pr7 - 70) / -slope) if slope < -0.01 and pr7 > 70 else None
    bench = 80.0
    lost = round(b["e30_kwh"] * (bench / pr30 - 1)) if pr30 and pr30 < bench and b.get("e30_kwh") else 0
    return {"slope_month": round(slope * 30, 1), "proj": round(proj, 1), "days70": days70, "trips30": len(b.get("episodes") or []),
            "lost30": lost, "bench": bench, "chart": {"x": ["30-day avg", "7-day avg", "in 30 days (trend)"], "pr": [pr30, pr7, round(proj, 1)]}}


def _pred_meter(m, dl):
    import re as _re
    w = (m.get("mhi_failure_window") or "").lower()
    nums = [int(x) for x in _re.findall(r"\d+", w)]
    unit = 30 if "month" in w else 7 if "week" in w else 1
    hi = (max(nums) * unit) if nums else None
    lo = 0 if ("immediate" in w or not nums) else min(nums) * unit
    today = datetime.now().date()
    flags = [(k, lbl) for k, lbl in (("is_meter_failure", "Meter hardware failure signs"), ("is_installation", "Installation / wiring fault"),
             ("is_over_voltage", "Over-voltage stress"), ("is_over_current", "Over-current"), ("is_kwh_jump", "kWh register jumps"),
             ("is_loose_contact", "Loose contact"), ("is_rtc_drift", "Clock (RTC) drift")) if str(m.get(k)) == "1"]
    last = [r["daily_kwh"] for r in dl[-14:] if r["daily_kwh"] is not None]
    avg = sum(last) / len(last) if last else None
    drops = sum(1 for a, b in zip(dl, dl[1:]) if a["reading_kwh"] is not None and b["reading_kwh"] is not None and b["reading_kwh"] < a["reading_kwh"])
    base = dl[-1]["ts"].date() if dl else today
    return {"from": today + timedelta(days=lo), "to": today + timedelta(days=hi) if hi else None, "drivers": [l for _, l in flags],
            "avg": round(avg, 2) if avg is not None else None, "drops": drops,
            "chart": {"day": [r["ts"].strftime("%d %b") for r in dl[-30:]] + [(base + timedelta(days=k)).strftime("%d %b") for k in range(1, 8)],
                      "kwh": [r["daily_kwh"] for r in dl[-30:]] + [None] * 7,
                      "fc": [None] * len(dl[-30:]) + ([round(avg, 2)] * 7 if avg is not None else [None] * 7)}}


def alerts_view(request):
    ov = analytics.overview()
    fs = _filter(ov["findings"], request)
    return render(request, "hub/alerts.html", {"nav": "alerts", "findings": fs, "codes": ov["by_code"],
                                               "q": request.GET})


def ai_view(request):
    return render(request, "hub/ai.html", {"nav": "ai", "ai_on": ai.available(),
                                           "examples": ["Which five assets should the field team visit first this week, and why?",
                                                        "How much pump energy is wasted on dry runs and overflows?",
                                                        "Which transformers have power-quality problems that could damage pumps?",
                                                        "Summarise energy losses we can fix without capital spend.",
                                                        "Which feeders have the most failing smart meters, and what should the meter team do first?"]})


# ── fragments / JSON used by the pages ───────────────────────────────────────

def evidence(request):
    aid = request.GET.get("asset")
    try:
        start, end = datetime.fromisoformat(request.GET["start"]), datetime.fromisoformat(request.GET["end"])
    except (KeyError, ValueError):
        raise Http404("window")
    a = analytics.asset(aid)
    if not a:
        raise Http404("asset")
    rows = analytics.evidence(aid, start, end)
    return render(request, "hub/partials/evidence.html", {"a": a, "rows": rows, "start": start, "end": end,
                                                          "title": request.GET.get("title", ""),
                                                          "v_lo": analytics.V_LO, "v_hi": analytics.V_HI})


def brief_json(request):
    b, pending = ai.brief(request.GET.get("scope", "all"), request.GET.get("asset") or None)
    return JsonResponse({"brief": b, "pending": pending})


@require_POST
def ask_json(request):
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        body = {}
    return JsonResponse(ai.ask(body.get("question"), body.get("scope", "all"), body.get("asset") or None))


# ── open API for hackathon teams ─────────────────────────────────────────────

def _clean(d):
    return {k: _iso(v) for k, v in d.items() if k not in ("findings", "run_daily", "forecast")}


def api_summary(request):
    ov = analytics.overview()
    return JsonResponse({"energy": {k: _iso(v) for k, v in ov["energy"].items()}, "water": ov["water"],
                         "nexus": ov["nexus"], "issues": ov["by_code"]}, json_dumps_params={"default": str})


def api_assets(request):
    out = [_clean(a) | {"health": a["health"], "issues": len(a["findings"])}
           for u in (analytics.energy(), analytics.water()) for a in u["assets"].values()]
    u = request.GET.get("utility")
    return JsonResponse({"assets": [a for a in out if not u or a["utility"] == u]}, json_dumps_params={"default": str})


def api_findings(request):
    fs = _filter(analytics.overview()["findings"], request)
    return JsonResponse({"findings": [{k: _iso(v) for k, v in f.items()} for f in fs]})


def api_series(request, asset_id):
    a = analytics.asset(asset_id)
    if not a:
        raise Http404("asset")
    days = min(60, max(1, int(request.GET.get("days", 7))))
    rows = analytics.energy_series(asset_id, days) if a["utility"] == "energy" else analytics.water_series(asset_id, days)
    return JsonResponse({"asset": _clean(a), "rows": [{k: _iso(v) for k, v in r.items()} for r in rows]},
                        json_dumps_params={"default": str})


def api_docs(request):
    return render(request, "hub/api.html", {"nav": "api"})


# ── consumer smart meters ────────────────────────────────────────────────────

def meters_view(request):
    g = request.GET
    page = max(1, int(g.get("page", 1) or 1))
    rows, total = meters.search(band=g.get("band") or None, cat=g.get("cat") or None, feeder=g.get("feeder") or None,
                                dt=g.get("dt") or None, q=(g.get("q") or "").strip() or None, limit=100, offset=(page - 1) * 100,
                                pred=g.get("pred") or None)
    s = analytics.meter_block()["summary"]
    board = analytics.cached("meter_board", _meter_board)
    return render(request, "hub/meters.html", {"board": board,
        "nav": "meters", "s": s, "rows": rows, "total": total, "page": page, "pages": (total + 99) // 100, "q": g,
        "feeders": meters.feeders()[:20], "hot_dts": meters.hot_dts(15),
        "qs": "&".join(f"{k}={g[k]}" for k in ("band", "cat", "feeder", "dt", "q", "pred") if g.get(k)),
        "charts": _jsonable({"bands": s["bands"] if s else [], "windows": s["windows"] if s else [],
                             "cats": s["categories"] if s else [], "board": board}),
    })



def _meter_board():
    """Smart-meter command-dashboard charts from the meter-health records."""
    if not meters.available():
        return None
    src = f"'{meters._p('meters')}'"
    q = meters._q
    lanes = q(f"SELECT sum(is_installation::INT) AS inst, sum(is_over_voltage::INT) AS ov, sum(is_over_current::INT) AS oc, "
              f"sum(is_meter_failure::INT) AS fail, sum(is_loose_contact::INT) AS loose, sum(is_rtc_drift::INT) AS rtc FROM {src}")[0]
    srcs = q(f"SELECT sum(CASE WHEN source_combo LIKE '%Events%' THEN 1 ELSE 0 END) AS ev, sum(CASE WHEN source_combo LIKE '%BLP%' THEN 1 ELSE 0 END) AS blp, "
             f"sum(CASE WHEN source_combo LIKE '%DLP%' THEN 1 ELSE 0 END) AS dlp FROM {src}")[0]
    combos = q(f"SELECT source_combo AS c, mhi_band AS b, count(*) AS n FROM {src} WHERE source_combo IS NOT NULL GROUP BY 1, 2")
    corder = [r["c"] for r in q(f"SELECT source_combo AS c FROM {src} WHERE source_combo IS NOT NULL GROUP BY 1 ORDER BY avg(mhi_score), count(*) DESC")]
    bands = ["Critical", "Degradation", "Watch", "Optimal"]
    cmap = {(r["c"], r["b"]): r["n"] for r in combos}
    events = q(f"SELECT event_faults AS e, count(*) AS n FROM {src} WHERE event_faults IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 8")
    piv = q(f"SELECT event_faults AS e, dlp_faults AS d, count(*) AS n FROM {src} WHERE event_faults IS NOT NULL AND dlp_faults IS NOT NULL "
            f"AND dlp_faults <> 'None' GROUP BY 1, 2")
    pe = sorted({r["e"] for r in piv}, key=lambda e: -sum(r["n"] for r in piv if r["e"] == e))[:8]
    pd = sorted({r["d"] for r in piv}, key=lambda d: -sum(r["n"] for r in piv if r["d"] == d))
    pmap = {(r["e"], r["d"]): r["n"] for r in piv}
    return {"lanes": {k: int(v or 0) for k, v in lanes.items()},
            "sources": {"label": ["Events", "Block load profile (BLP)", "Daily reading (DLP)"], "n": [int(srcs["ev"] or 0), int(srcs["blp"] or 0), int(srcs["dlp"] or 0)]},
            "combo": {"label": corder, "bands": bands, "n": {b: [cmap.get((c, b), 0) for c in corder] for b in bands}},
            "events": {"label": [r["e"] for r in events], "n": [r["n"] for r in events]},
            "pivot": {"label": pe, "series": pd, "n": {d: [pmap.get((e, d), 0) for e in pe] for d in pd}}}


def meter_view(request, meter_id):
    m = meters.meter(meter_id)
    if not m:
        raise Http404("meter")
    lg, dl = meters.logs(meter_id), meters.daily(meter_id)
    pred = _pred_meter(m, dl)
    return render(request, "hub/meter.html", {"nav": "meters", "m": m, "has_logs": bool(lg), "has_daily": bool(dl), "pred": pred,
        "chart": _jsonable({"ts": [r["ts"].strftime("%d %b %H:%M") for r in lg], "i": [r["current"] for r in lg],
                            "v": [r["voltage"] for r in lg], "pf": [r["power_factor"] for r in lg],
                            "kwh": [r["interval_kwh"] for r in lg],
                            "d_ts": [r["ts"].strftime("%d %b") for r in dl], "d_kwh": [r["daily_kwh"] for r in dl],
                            "d_reg": [r["reading_kwh"] for r in dl], "pred": pred["chart"]}),
        "rows": lg[-96:]})


def api_meters(request):
    g = request.GET
    rows, total = meters.search(band=g.get("band") or None, cat=g.get("cat") or None, feeder=g.get("feeder") or None,
                                dt=g.get("dt") or None, q=g.get("q") or None,
                                limit=min(500, int(g.get("limit", 100))), offset=int(g.get("offset", 0)))
    keep = ("meter_id", "mhi_score", "mhi_band", "mhi_failure_window", "feeder_id", "dt_id", "diagnosis_en", "categories", "action")
    return JsonResponse({"total": total, "summary": analytics.meter_block()["summary"],
                         "meters": [{k: r.get(k) for k in keep} for r in rows]}, json_dumps_params={"default": str})


def api_meter(request, meter_id):
    m = meters.meter(meter_id)
    if not m:
        raise Http404("meter")
    m["categories"] = [c[1] for c in m["categories"]]
    return JsonResponse({"meter": m, "logs": [{k: _iso(v) for k, v in r.items()} for r in meters.logs(meter_id)]},
                        json_dumps_params={"default": str})


# ── transformer intelligence (native: phase balance, forecast, capacity plan, outlook) ──

def _xfmr_tags(a, d):
    tags = []
    if any(f["severity"] == "critical" for f in a["findings"]):
        tags.append("critical")
    if d and d.get("phase") and d["phase"]["flagged"]:
        tags.append("imbalance")
    fc, o = (d or {}).get("forecast"), (d or {}).get("outlook")
    if fc and fc["max_p_overload"] >= 30:
        tags.append("risk7")
    if o and o["load"]["days_warn"] and o["load"]["days_warn"] <= 150:
        tags.append("over5")
    if o and o["wti"]["days_warn"] and o["wti"]["days_warn"] <= 150:
        tags.append("hot5")
    return tags


def transformers_view(request):
    from django.shortcuts import redirect
    q = request.META.get("QUERY_STRING")
    return redirect("/energy/" + (f"?{q}" if q else ""))


def transformer_view(request, asset_id):
    from . import dtintel
    a = analytics.energy()["assets"].get(asset_id)
    if not a:
        raise Http404("transformer")
    d = dtintel.get(asset_id) or {}
    fc = d.get("forecast")
    recent = analytics.energy_series(asset_id, days=7)
    chart = {"outlook": {k: {**g["series"], "warn": g["warn"], "crit": g["crit"], "crit_label": g["crit_label"]}
                         for k, g in (d.get("outlook") or {}).items()}}
    if fc:
        chart["fc"] = {"actual_ts": [r["ts"].isoformat() for r in recent],
                       "actual": [round((r["kva"] or 0) / a["rating_kva"] * 100, 1) if r["supply"] else None for r in recent],
                       "fc_ts": fc["path"]["ts"], "fc": fc["path"]["pct"], "sigma": fc["sigma_pct"] or 8}
        chart["mon"] = {"m": [m["month"].strftime("%b %Y") for m in d["months"]], "pct": [m["peak_pct"] for m in d["months"]]}
    outlook = [(k, d["outlook"][k]) for k in ("load", "oti", "wti")] if d.get("outlook") else []
    s7, dd = analytics.energy_series(asset_id, days=7), analytics.energy_daily(asset_id, days=365)
    chart["rd"] = {"ts": [r["ts"].strftime("%d %b %H:%M") for r in s7], "kw": [r["kw"] for r in s7],
                   "vr": [r["v_rn"] for r in s7], "vy": [r["v_yn"] for r in s7], "vb": [r["v_bn"] for r in s7],
                   "ir": [r["i_r"] for r in s7], "iy": [r["i_y"] for r in s7], "ib": [r["i_b"] for r in s7], "in": [r["i_n"] for r in s7],
                   "day": [str(r["day"]) for r in dd], "kwh": [round(r["kwh"], 1) for r in dd],
                   "fc_day": [str(f["day"]) for f in a["forecast"]], "fc_kwh": [f["kwh"] for f in a["forecast"]],
                   "rated_a": a["rated_a"], "v_lo": analytics.V_LO, "v_hi": analytics.V_HI}
    return render(request, "hub/transformer.html", {"nav": "energy", "a": a, "d": d, "fc": fc, "outlook": outlook,
                                                    "chart": _jsonable(chart)})


def api_transformers(request):
    from . import dtintel
    x = dtintel.fleet()
    out = []
    for a in analytics.energy()["assets"].values():
        d = x["by_id"].get(a["asset_id"]) or {}
        fc = d.get("forecast") or {}
        out.append({"asset_id": a["asset_id"], "rating_kva": a["rating_kva"], "health": a["health"], "online": a["online"],
                    "phase_balance": d.get("phase"),
                    "forecast_7d": {k: fc.get(k) for k in ("days", "sigma_pct", "mape_pct", "growth_pct", "max_p_overload")} if fc else None,
                    "capacity_plan": d.get("months"),
                    "outlook": {k: {kk: vv for kk, vv in g.items() if kk != "series"} for k, g in (d.get("outlook") or {}).items()}})
    return JsonResponse({"as_of": x["as_of"], "transformers": out}, json_dumps_params={"default": str})


# ── solar plants ─────────────────────────────────────────────────────────────

SEV_W = {"critical": 3, "warning": 1}

def solar_view(request):
    from . import solar
    sol = solar.fleet()
    blocks = sorted((sol or {}).get("blocks", []), key=lambda b: (-sum(SEV_W.get(i["severity"], 0) for i in b["issues"]), b["inv_id"]))
    focus = request.GET.get("block")
    fb = next((b for b in blocks if b["inv_id"] == focus), None)
    ptags = {}
    for b in blocks:
        pr = _pred_solar(b)
        ptags[b["inv_id"]] = "predicted" if (pr["slope_month"] <= -4 or (pr["days70"] and pr["days70"] <= 30) or pr["trips30"] >= 5) else ""
    return render(request, "hub/solar.html", {
        "nav": "solar", "sol": sol, "s": solar.summary(), "blocks": blocks, "fb": fb, "ptags": ptags,
        "charts": _jsonable({"pr": {"id": [b["inv_id"] for b in blocks], "pr30": [b["pr30"] for b in blocks],
                                    "sev": [b["severity"] for b in blocks]},
                             "plants": {"p": [p["plant"] for p in (sol or {}).get("plants", [])],
                                        "mwh": [round((p.get("e30_kwh") or 0) / 1000, 1) for p in (sol or {}).get("plants", [])]}}),
    })


def api_solar(request):
    from . import solar
    return JsonResponse({"summary": solar.summary(), **(solar.fleet() or {})}, json_dumps_params={"default": str})


def solar_block_view(request, inv_id):
    from . import solar
    sol = solar.fleet() or {}
    b = next((x for x in sol.get("blocks", []) if x["inv_id"] == inv_id), None)
    if not b:
        raise Http404("block")
    plant = next((p for p in sol.get("plants", []) if p["plant"] == b["plant"]), {})
    worst = next((i["severity"] for i in b["issues"]), None)
    pred = _pred_solar(b)
    return render(request, "hub/solar_block.html", {"nav": "solar", "b": b, "plant": plant, "pred": pred, "pred_json": _jsonable(pred["chart"]),
                  "health": max(0, 100 - sum(solar.PEN.get(i["severity"], 5) for i in b["issues"])), "worst": worst})
