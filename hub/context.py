from . import ai


def common(request):
    ctx = {"ai_on": ai.available(), "embed": request.GET.get("embed") == "1"}
    try:  # data status for the sidebar (cached analytics; never break a page over it)
        from . import analytics, water_live
        e, w, m = analytics.energy(), analytics.water(), analytics.meter_block()["summary"]
        ctx["status"] = {"energy_as_of": e["as_of"], "energy_n": len(e["assets"]), "water_n": len(w["assets"]),
                         "water_live": water_live.configured(), "water_synced": w["synced"],
                         "meters_n": m["total"] if m else 0}
    except Exception:
        ctx["status"] = None
    try:  # what roll_to_today did, so the page says which data is measured and which is extended / replayed
        import json
        from datetime import datetime
        from django.conf import settings
        c = json.loads((settings.DATA_DIR / "source" / "demo_clock.json").read_text())
        f = lambda x: datetime.fromisoformat(x)
        ctx["clock"] = {"energy_measured": f(c["energy"]["measured_until"]), "energy_to": f(c["energy"]["extended_until"]),
                        "meters": (f(c["meters"]["recorded_from"]), f(c["meters"]["recorded_to"])) if "meters" in c else None,
                        "water": (f(c["water"]["recorded_from"]), f(c["water"]["recorded_to"])) if "water" in c else None,
                        "solar": f(c["solar"]["recorded_as_of"]) if "solar" in c else None}
    except Exception:
        ctx["clock"] = None
    return ctx
