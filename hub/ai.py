"""AI suggestions over the energy + water analytics (OpenRouter; rule-based fallback).

The model only sees aggregated KPIs and findings — never raw identifiers of people.
The model name lives in .env only; the UI just says "AI suggestions".
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime

import requests
from django.conf import settings

from . import analytics, store

URL = "https://openrouter.ai/api/v1/chat/completions"
SYSTEM = (
    "You are a senior operations analyst for an Indian utility that runs both electricity distribution "
    "(distribution transformers, LT network, consumer smart meters / AMI, solar plants) and drinking-water supply (pump houses, overhead tanks, water quality). "
    "You are given analytics computed from 15-minute smart-meter readings and pump-controller readings. "
    "Be concrete and quantitative, use the numbers given, never invent data. Recommend physical field actions "
    "(what to check, measure, tighten, clean or replace) and operational decisions; no software steps. "
    "Point out water–energy links (pump energy, wasted pumping, power-quality effects on pumps) where relevant."
)
_lock = threading.Lock()
_pending = set()


def available():
    return bool(settings.OPENROUTER_API_KEY and settings.OPENROUTER_MODEL)


def _cache_path():
    return settings.DATA_DIR / "ai_cache.json"


def _load():
    try:
        return json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}


def _save(key, val):
    with _lock:
        c = _load()
        c[key] = val
        _cache_path().write_text(json.dumps(c, indent=1, default=str))


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat(timespec="minutes") if isinstance(o, datetime) else o.isoformat()
    return str(o)


def _xfmr_predictions(n=8):
    """Soonest predicted limit crossings (load forecast + five-month outlook) per transformer."""
    from . import dtintel
    rows = []
    for aid, v in dtintel.fleet()["by_id"].items():
        o, fc = v.get("outlook") or {}, v.get("forecast") or {}
        cand = [(o[k]["days_warn"], k) for k in ("load", "wti") if o.get(k) and o[k]["days_warn"]]
        if not cand and fc.get("max_p_overload", 0) < 30:
            continue
        days, key = min(cand) if cand else (None, None)
        rows.append({"asset_id": aid, "rating_kva": v["rating_kva"],
                     "overload_probability_next_7_days_pct": fc.get("max_p_overload"),
                     "days_until_peak_load_passes_nameplate": o.get("load", {}).get("days_warn"),
                     "days_until_estimated_hotspot_alarm": o.get("wti", {}).get("days_warn"),
                     "first_month_above_nameplate": str((v.get("first_over_month") or {}).get("month") or ""),
                     "growth_pct_per_year": fc.get("growth_pct"), "_sort": days if days is not None else 999})
    rows.sort(key=lambda r: r.pop("_sort"))
    return rows[:n]


def _meter_rollups():
    from . import meters
    keep_f = ("feeder_id", "feeder_name", "meters", "critical", "degradation", "mhi")
    keep_d = ("dt_id", "feeder_id", "meters", "critical", "degradation", "twin_status", "voltage_spread", "current_difference")
    return ([{k: f[k] for k in keep_f} for f in meters.feeders()[:8]], [{k: d[k] for k in keep_d} for d in meters.hot_dts(8)])


def context(scope="all", asset_id=None, max_findings=40):
    ov = analytics.overview()
    if asset_id and scope == "meters":
        from . import meters
        m = meters.meter(asset_id) or {}
        keep = ("meter_id", "mhi_score", "mhi_band", "mhi_failure_window", "event_faults", "blp_faults", "dlp_faults",
                "mhi_reasons", "diagnosis_en", "why_en", "evidence_en", "feeder_id", "dt_id")
        return {"meter": {k: m.get(k) for k in keep}, "categories": [c[1] for c in m.get("categories", [])],
                "suggested_action": m.get("action"),
                "recent_intervals": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
                                     for r in meters.logs(asset_id)[-48:]]}
    if asset_id:
        a = analytics.asset(asset_id)
        keep = {k: v for k, v in a.items() if k not in ("findings", "run_daily", "forecast")}
        return {"asset": keep, "findings": a["findings"], "forecast_next_7_days": a.get("forecast")}
    fs = analytics.balanced([f for f in ov["findings"] if scope == "all" or f["utility"] == scope], max_findings)
    return {"scope": scope,
            "energy_kpis": ov["energy"] if scope in ("all", "energy") else None,
            "water_kpis": ov["water"] if scope in ("all", "water") else None,
            "smart_meter_kpis": ov["meters"] if scope in ("all", "meters") else None,
            "transformer_intelligence": ov.get("xfmr") if scope in ("all", "energy") else None,
            "solar_kpis": ov.get("solar") if scope in ("all", "solar") else None,
            "transformer_predictions": _xfmr_predictions() if scope in ("all", "energy") else None,
            "water_energy_nexus": ov["nexus"] if scope == "all" else None,
            "assumptions": {"pump_kw": settings.PUMP_KW, "tariff_rs_per_kwh": settings.TARIFF_RS_KWH},
            "issue_counts": [b for b in ov["by_code"] if scope == "all" or b["utility"] == scope],
            "smart_meter_feeders_worst": _meter_rollups()[0] if scope in ("all", "meters") else None,
            "smart_meter_dt_hotspots": _meter_rollups()[1] if scope in ("all", "meters") else None, "top_findings": [{k: f[k] for k in ("asset_id", "utility", "severity", "title", "value")} for f in fs]}


def _call(messages, max_tokens=900):
    last = None
    for attempt in range(3):
        try:
            r = requests.post(URL, timeout=60, headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
                              json={"model": settings.OPENROUTER_MODEL, "messages": messages, "temperature": 0.2,
                                    "max_tokens": max_tokens})
            if r.status_code == 429:
                time.sleep(2 + 3 * attempt)
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except requests.RequestException as exc:
            last = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"AI service unavailable ({type(last).__name__ if last else 'rate limited'})")


def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else None


def _rule_brief(scope, asset_id=None):
    ctx = context(scope, asset_id)
    if asset_id and scope == "meters":
        m = ctx["meter"]
        return {"headline": f"Meter {m.get('meter_id')}: {m.get('diagnosis_en') or 'see signals'} (MHI {m.get('mhi_score')}, {m.get('mhi_band')}).",
                "insights": [x for x in (m.get("event_faults"), m.get("blp_faults"), m.get("dlp_faults"), m.get("mhi_failure_window")) if x],
                "actions": [ctx.get("suggested_action")], "source": "rules"}
    fs = ctx.get("findings") if asset_id else ctx["top_findings"]
    crit = [f for f in fs if f["severity"] == "critical"]
    lines = [f"{f['asset_id']}: {f['title']} — {f['value']}" for f in (crit or fs)[:6]]
    return {"headline": (f"{len(crit)} critical issues need field attention." if crit else
                         "No critical issues; work through the warnings below." if fs else "All monitored assets look healthy."),
            "insights": lines, "actions": [f.get("action") for f in (crit or fs)[:4] if f.get("action")], "source": "rules"}


def _generate_brief(scope, asset_id, key):
    try:
        ctx = context(scope, asset_id)
        prompt = ("Analyse the data below and reply with JSON only: {\"headline\": one sentence, "
                  "\"insights\": 4-6 short bullet strings with numbers, \"actions\": 3-5 prioritised field actions "
                  "(name the asset), \"risk\": one sentence on what happens if nothing is done}.\n\nDATA:\n"
                  + json.dumps(ctx, default=_json_default)[:14000])
        out = _parse_json(_call([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]))
        if not out or "headline" not in out:
            raise ValueError("unexpected answer")
        out["source"] = "ai"
    except Exception as exc:
        out = _rule_brief(scope, asset_id) | {"note": str(exc)[:120]}
    out["at"] = datetime.now().isoformat(timespec="minutes")
    _save(key, out)
    _pending.discard(key)


def brief(scope="all", asset_id=None, wait=False):
    """Cached per data version; generated in the background. Returns (brief|None, pending)."""
    key = f"brief:{scope}:{asset_id or '-'}:{store.data_version()}"
    c = _load().get(key)
    if c:
        return c, False
    if not available():
        return _rule_brief(scope, asset_id), False
    if key not in _pending:
        _pending.add(key)
        t = threading.Thread(target=_generate_brief, args=(scope, asset_id, key), daemon=True)
        t.start()
        if wait:
            t.join(90)
            return _load().get(key), key in _pending
    return None, True


def ask(question, scope="all", asset_id=None):
    question = (question or "").strip()[:600]
    if not question:
        return {"answer": "Ask a question about the energy or water assets."}
    ctx = context(scope, asset_id)
    if not available():
        rb = _rule_brief(scope, asset_id)
        return {"answer": "AI suggestions are not configured; summary from the rules:\n- " + "\n- ".join(rb["insights"]),
                "source": "rules"}
    try:
        ans = _call([{"role": "system", "content": SYSTEM + " Answer in under 180 words, plain text with short bullets."},
                     {"role": "user", "content": f"DATA:\n{json.dumps(ctx, default=_json_default)[:14000]}\n\nQUESTION: {question}"}],
                    max_tokens=600)
        return {"answer": ans.strip(), "source": "ai"}
    except Exception as exc:
        return {"answer": f"AI suggestions are unavailable right now ({exc}).", "source": "error"}
