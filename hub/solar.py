"""Solar plants and their inverter blocks (data/source/solar_fleet.json).

Each inverter block carries live output, 30-day energy, performance ratio, availability and its issues.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

from django.conf import settings

_cache = {"mtime": None, "data": None}
_lock = threading.Lock()
PEN = {"critical": 25, "warning": 10, "watch": 3, "info": 2}


def _parse(d):
    for k, v in list(d.items()):
        if isinstance(v, str) and len(v) >= 16 and v[4:5] == "-" and v[10:11] == "T":
            try:
                d[k] = datetime.fromisoformat(v)
            except ValueError:
                pass
    return d


def fleet():
    p = settings.SOLAR_FLEET
    if not p.exists():
        return None
    m = p.stat().st_mtime_ns
    with _lock:
        if _cache["mtime"] != m:
            _cache["data"] = json.loads(p.read_text(), object_hook=_parse)["solar"]
            _cache["mtime"] = m
        return _cache["data"]


def version():
    p = settings.SOLAR_FLEET
    return str(p.stat().st_mtime_ns) if p.exists() else "-"


def health(blocks):
    if not blocks:
        return None
    return round(sum(max(0, 100 - sum(PEN.get(i["severity"], 5) for i in b["issues"])) for b in blocks) / len(blocks))


def summary():
    s = fleet()
    if not s:
        return None
    blocks = s["blocks"]
    rated = [b["pr30"] for b in blocks if b["pr30"]]
    return {"plants": len(s["plants"]), "blocks": len(blocks),
            "mw_now": sum(b["kw_now"] or 0 for b in blocks) / 1000,
            "mwh_today": sum(b["today_kwh"] or 0 for b in blocks) / 1000,
            "mw_ac": sum(b["kw_ac"] for b in blocks) / 1000,
            "issues": sum(len(b["issues"]) for b in blocks),
            "critical": sum(1 for b in blocks for i in b["issues"] if i["severity"] == "critical"),
            "running": sum(1 for b in blocks if b["status"] == "running"),
            "pr30": sum(rated) / max(1, len(rated)), "health": health(blocks)}


def findings():
    """Inverter issues in the hub's common finding format."""
    out = []
    for b in (fleet() or {}).get("blocks", []):
        for i in b["issues"]:
            out.append({"asset_id": b["inv_id"], "utility": "solar", "code": "SOLAR_" + i["code"],
                        "severity": i["severity"] if i["severity"] in ("critical", "warning", "info") else "info",
                        "title": i["label"], "value": i["value"], "why": f"{b['plant']} · {b['consumer']}",
                        "action": (i.get("corrective") or [""])[0], "start": None, "end": None,
                        "url": f"/solar/?sel={b['inv_id']}"})
    return out
