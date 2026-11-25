"""Field gateway → oneM2M: post each device's latest reading as a contentInstance.

AE FieldGateway (originator CFieldGateway) / <energy|water|solar> / <device> / readings / cin
Target CSE: ONEM2M_INFLOW_CSE (default: the hub's own oneM2M endpoint, http://127.0.0.1:8030/onem2m/cse-in).
Run every 15 minutes (systemd timer teknovate-onem2m-gateway).
"""
import json
import uuid
from datetime import datetime

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from hub import analytics, solar, store

ORIGIN, AE = "CFieldGateway", "FieldGateway"
E_COLS = ("kw", "kva", "v_rn", "v_yn", "v_bn", "i_r", "i_y", "i_b", "i_n", "freq", "thd_v")
W_COLS = ("pump", "tank_pct", "sump", "flow", "ph", "v1", "v2", "v3")


def _post(s, url, ty, key, body):
    h = {"X-M2M-Origin": ORIGIN, "X-M2M-RI": uuid.uuid4().hex[:12], "X-M2M-RVI": "3",
         "Content-Type": f"application/json;ty={ty}", "Accept": "application/json"}
    r = s.post(url, headers=h, data=json.dumps({key: body}), timeout=15)
    if r.status_code not in (200, 201, 409):
        raise CommandError(f"{url}: HTTP {r.status_code} {r.headers.get('X-M2M-RSC')} {r.text[:200]}")
    return r.headers.get("X-M2M-RSC")


def _clean(v):
    if isinstance(v, float):
        return round(v, 3)
    if isinstance(v, datetime):
        return v.isoformat(timespec="minutes")
    return v


class Command(BaseCommand):
    help = "Post every device's latest reading into a oneM2M CSE as contentInstances (field-gateway role)."

    def handle(self, *args, **o):
        base = (settings.ONEM2M_INFLOW_CSE or "").rstrip("/")
        s, n = requests.Session(), 0
        _post(s, base, 2, "m2m:ae", {"rn": AE, "api": "Nuih.fieldgw", "rr": False, "srv": ["3"], "lbl": ["gateway"]})
        readings = {"energy": {}, "water": {}, "solar": {}}
        e = analytics.energy()
        by_imei = {a["imei"]: a["asset_id"] for a in e["assets"].values() if a.get("imei")}
        src = store.energy_source()
        if src:
            cols = ", ".join(E_COLS)
            for r in store.query(f"SELECT imei, slot, extended, {cols} FROM {src} QUALIFY row_number() OVER (PARTITION BY imei ORDER BY slot DESC) = 1"):
                if r["imei"] in by_imei:
                    readings["energy"][by_imei[r["imei"]]] = {"ts": _clean(r["slot"]), **{c: _clean(r[c]) for c in E_COLS},
                                                              "source": "profile-extended" if r.get("extended") else "measured"}
        wcols = ", ".join(W_COLS)
        for r in store.query(f"SELECT asset_id, ts, {wcols} FROM {store.water_source()} AS w QUALIFY row_number() OVER (PARTITION BY asset_id ORDER BY ts DESC) = 1"):
            readings["water"][r["asset_id"]] = {"ts": _clean(r["ts"]), **{c: _clean(r[c]) for c in W_COLS}}
        for b in (solar.fleet() or {}).get("blocks", []):
            readings["solar"][b["inv_id"]] = {"ts": _clean(b["last_seen"]), "status": b["status"], "kw_now": b["kw_now"],
                                              "today_kwh": b["today_kwh"], "pr7": b["pr7"]}
        for u, devs in readings.items():
            _post(s, f"{base}/{AE}", 3, "m2m:cnt", {"rn": u})
            for dev, doc in devs.items():
                _post(s, f"{base}/{AE}/{u}", 3, "m2m:cnt", {"rn": dev})
                _post(s, f"{base}/{AE}/{u}/{dev}", 3, "m2m:cnt", {"rn": "readings", "mni": 200})
                _post(s, f"{base}/{AE}/{u}/{dev}/readings", 4, "m2m:cin", {"cnf": "application/json:0", "con": json.dumps(doc)})
                n += 1
            self.stdout.write(f"{u}: {len(devs)} readings")
        self.stdout.write(self.style.SUCCESS(f"posted {n} readings to {base}"))
