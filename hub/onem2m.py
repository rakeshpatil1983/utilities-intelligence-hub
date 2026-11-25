"""oneM2M interworking (TS-0001 resource model, TS-0004 primitives, TS-0009 HTTP binding).

The hub is a oneM2M Application Entity. ``push()`` registers it in any CSE (ACME, OM2M, Mobius ...)
and publishes every device's health, top issues and prediction as content instances:

    <CSE base>
      └─ UtilitiesHub                    AE   (api Nuih.teknovate, rr=true, poa = this hub's /onem2m/notify)
           ├─ energy / water / solar / meters      container per utility
           │    └─ DT-10, W4, PV-2-INV01 ...         container per device (lbl = utility, health band)
           │         └─ status  → contentInstance   {health, issues, prediction, as_of}  (mni 50)
           └─ subscription on any device container a field gateway writes to → notifications

``ingest()`` takes oneM2M notifications (m2m:sgn) sent to /onem2m/notify by a CSE subscription, so
readings that field devices post into the CSE reach the hub through oneM2M.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import requests
from django.conf import settings

TY = {"ae": 2, "cnt": 3, "cin": 4, "sub": 23}
UTILITIES = ("energy", "water", "solar", "meters")


def configured():
    return bool(settings.ONEM2M_CSE)


def _headers(ty=None):
    h = {"X-M2M-Origin": settings.ONEM2M_ORIGIN, "X-M2M-RI": uuid.uuid4().hex[:12], "X-M2M-RVI": settings.ONEM2M_RVI,
         "Accept": "application/json"}
    if ty:
        h["Content-Type"] = f"application/json;ty={TY[ty]}"
    return h


def _create(parent_url, ty, body, session):
    """POST a resource; an existing one (409 / RSC 4105) is fine. Returns the oneM2M response status code."""
    r = session.post(parent_url, headers=_headers(ty), data=json.dumps(body), timeout=10)
    rsc = r.headers.get("X-M2M-RSC") or ("2001" if r.status_code == 201 else "4105" if r.status_code == 409 else str(r.status_code))
    if rsc not in ("2001", "4105") and r.status_code not in (200, 201, 409):
        raise RuntimeError(f"{ty} under {parent_url}: HTTP {r.status_code} RSC {rsc} {r.text[:200]}")
    return rsc


def device_status():
    """One status document per device, from the hub's analytics (what the AE publishes)."""
    from . import analytics, dtintel, meters, solar
    out = {u: {} for u in UTILITIES}
    x = dtintel.fleet()["by_id"]
    for a in analytics.energy()["assets"].values():
        d = x.get(a["asset_id"]) or {}
        fc, o = d.get("forecast") or {}, (d.get("outlook") or {}).get("load") or {}
        out["energy"][a["asset_id"]] = {
            "health": a["health"], "online": a["online"], "rating_kva": a["rating_kva"],
            "issues": [f["title"] for f in a["findings"][:3]],
            "prediction": {"p_overload_7d_pct": fc.get("max_p_overload"), "days_to_nameplate": o.get("days_warn"),
                           "growth_pct_yr": fc.get("growth_pct")}}
    for a in analytics.water()["assets"].values():
        out["water"][a["asset_id"]] = {"health": a["health"], "online": a["online"], "kind": a["kind"],
                                       "issues": [f["title"] for f in a["findings"][:3]],
                                       "dry_runs": a["dry_runs"], "overflows": a["overflows"]}
    for b in (solar.fleet() or {}).get("blocks", []):
        out["solar"][b["inv_id"]] = {"status": b["status"], "pr30": b["pr30"], "pr7": b["pr7"],
                                     "issues": [i["label"] for i in b["issues"][:3]]}
    rows, _ = meters.search(band="Critical", limit=settings.ONEM2M_METERS)
    for m in rows:
        out["meters"][m["meter_id"]] = {"mhi": m["mhi_score"], "band": m["mhi_band"], "failure_window": m["mhi_failure_window"],
                                        "issues": m["categories"][:3]}
    return out


def push(log=print):
    """Register the AE, create the container tree and post one status contentInstance per device."""
    if not configured():
        raise RuntimeError("ONEM2M_CSE is not set (e.g. http://127.0.0.1:8080/cse-in)")
    base, ae = settings.ONEM2M_CSE.rstrip("/"), settings.ONEM2M_AE
    data, now = device_status(), datetime.now().isoformat(timespec="seconds")
    s, n = requests.Session(), 0
    _create(base, "ae", {"m2m:ae": {"rn": ae, "api": "Nuih.teknovate", "rr": True, "srv": [settings.ONEM2M_RVI],
                                    "poa": [settings.ONEM2M_POA] if settings.ONEM2M_POA else [],
                                    "lbl": ["utilities", "teknovate"]}}, s)
    for u, devices in data.items():
        if not devices:
            continue
        _create(f"{base}/{ae}", "cnt", {"m2m:cnt": {"rn": u, "lbl": [f"utility:{u}"]}}, s)
        for dev, doc in devices.items():
            dev_url = f"{base}/{ae}/{u}/{dev}"
            _create(f"{base}/{ae}/{u}", "cnt", {"m2m:cnt": {"rn": dev, "lbl": [f"utility:{u}"]}}, s)
            _create(dev_url, "cnt", {"m2m:cnt": {"rn": "status", "mni": 50}}, s)
            _create(f"{dev_url}/status", "cin", {"m2m:cin": {"cnf": "application/json:0", "lbl": [f"as_of:{now}"],
                                                             "con": json.dumps(doc | {"as_of": now})}}, s)
            n += 1
        log(f"{u}: {len(devices)} devices")
    from . import store
    store.set_meta("onem2m_push", json.dumps({"at": now, "cse": base, "ae": ae, "instances": n}))
    return n


def subscribe(target_path, log=print):
    """Subscribe this hub to a container in the CSE (e.g. one a field gateway writes readings to)."""
    base = settings.ONEM2M_CSE.rstrip("/")
    if not settings.ONEM2M_POA:
        raise RuntimeError("ONEM2M_POA (this hub's reachable /onem2m/notify URL) is not set")
    rsc = _create(f"{base}/{target_path.strip('/')}", "sub",
                  {"m2m:sub": {"rn": "uih-sub", "nu": [f"{base}/{settings.ONEM2M_AE}"], "nct": 1, "enc": {"net": [3]}}},
                  requests.Session())
    log(f"subscription on {target_path}: RSC {rsc}")
    return rsc


def ingest(body, headers):
    """Store one oneM2M notification. Returns the oneM2M response status code to send back."""
    from . import store
    sgn = (body or {}).get("m2m:sgn") or {}
    if sgn.get("vrq"):                         # subscription verification request
        return "2000"
    rep = ((sgn.get("nev") or {}).get("rep")) or {}
    cin = rep.get("m2m:cin") or {}
    con = cin.get("con")
    if isinstance(con, (dict, list)):
        con = json.dumps(con)
    with store.connect() as c:
        c.execute("INSERT INTO onem2m_inbox VALUES (?, ?, ?, ?, ?)",
                  [datetime.now(), sgn.get("sur") or "", headers.get("X-M2M-Origin", ""), cin.get("ri") or "", con or json.dumps(rep)])
    return "2000"


def inbox(limit=50):
    from . import store
    return store.query("SELECT * FROM onem2m_inbox ORDER BY received DESC LIMIT ?", [limit])


def last_push():
    from . import store
    v = store.get_meta("onem2m_push")
    return json.loads(v) if v else None
