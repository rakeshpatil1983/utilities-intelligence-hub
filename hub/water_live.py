"""Live water data from the IoT platform API (FastAPI, bearer token).

Credentials come only from .env (WATER_API_USERNAME / WATER_API_PASSWORD) and are never
logged or shown. Pulls every device whose readings carry water keys, backfills the last
N days through /api/v1/reports, then keeps up incrementally.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta

import requests
from django.conf import settings

from . import store
from .water_dump import normalise

log = logging.getLogger(__name__)
WATER_TYPE_HINTS = ("pump", "tank", "oht", "water", "level", "sump", "well", "starter", "motor")
_session = {"token": None, "at": 0.0}


class NotConfigured(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.WATER_API_USERNAME and settings.WATER_API_PASSWORD)


def _token(force=False):
    if not configured():
        raise NotConfigured("WATER_API_USERNAME / WATER_API_PASSWORD are not set in .env")
    if _session["token"] and not force and time.time() - _session["at"] < 1500:
        return _session["token"]
    r = requests.post(f"{settings.WATER_API_BASE}/api/v1/auth/login", timeout=15,
                      json={"username": settings.WATER_API_USERNAME, "password": settings.WATER_API_PASSWORD})
    if r.status_code != 200:
        raise RuntimeError(f"water platform login failed (HTTP {r.status_code})")
    _session.update(token=r.json()["data"]["tokens"]["accessToken"], at=time.time())
    return _session["token"]


def _get(path, **params):
    for attempt in (0, 1):
        r = requests.get(f"{settings.WATER_API_BASE}{path}", params=params, timeout=30,
                         headers={"Authorization": f"Bearer {_token(force=attempt == 1)}"})
        if r.status_code == 401 and attempt == 0:
            continue
        r.raise_for_status()
        return r.json()


def _kind(r):
    if r.get("ph") is not None:
        return "Water quality"
    if r.get("tank_pct") is not None or r.get("tank_raw"):
        return "Pump + tank"
    if r.get("pump") is not None:
        return "Pump"
    return "Level switch"


def list_devices():
    out, offset = [], 0
    while True:
        d = _get("/api/v1/devices", limit=500, offset=offset)["data"]
        rows = d.get("devices", d) if isinstance(d, dict) else d
        out += rows
        if len(rows) < 500:
            return out
        offset += 500


def sync(backfill_days=30, max_devices=400) -> dict:
    """Pull new water readings; returns counts. Raises NotConfigured without credentials."""
    now = datetime.now().replace(microsecond=0)
    devices = [d for d in list_devices()
               if any(h in (d.get("device_type_name") or "").lower() for h in WATER_TYPE_HINTS)][:max_devices]
    with store.connect() as con:
        last = dict(con.execute("SELECT device_id, max(ts) FROM water_live GROUP BY 1").fetchall())
    added, touched = 0, 0
    for dev in devices:
        did = int(dev["id"])
        start = (last.get(did) or now - timedelta(days=backfill_days)) + timedelta(seconds=1)
        rows = _get("/api/v1/reports", device_id=did, from_date=start.strftime("%Y-%m-%dT%H:%M"),
                    to_date=now.strftime("%Y-%m-%dT%H:%M"))["data"]
        batch, latest = [], None
        for row in rows:
            js = row.get("data")
            if isinstance(js, str):
                try:
                    js = json.loads(js)
                except ValueError:
                    continue
            if not isinstance(js, dict):
                continue
            r = normalise(js)
            if not r:
                continue
            ts = datetime.fromisoformat(str(row["time"]).replace("Z", ""))
            if last.get(did) and ts <= last[did]:
                continue
            latest = r | {"ts": ts}
            batch.append((f"W{did}", ts, did, dev.get("device_type_name") or "", r["pump"], r["tank_raw"], r["tank_pct"],
                          r["sump"], r["flow"], r["well"], r["ph"], r["float_sw"], r["v1"], r["v2"], r["v3"], "live"))
        if batch:
            with store.connect() as con:
                con.executemany("INSERT INTO water_live VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
                con.execute("INSERT OR REPLACE INTO water_devices VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [did, dev.get("device_type_name") or "", _kind(latest), latest["tank_pct"],
                             None if latest["pump"] is None else ("ON" if latest["pump"] else "OFF"), latest["ts"], now])
            added += len(batch)
            touched += 1
    store.set_meta("water_live_version", f"{now.isoformat()}:{added}")
    store.set_meta("water_live_synced", now.isoformat())
    return {"devices": len(devices), "updated": touched, "rows": added}


def start_background():
    """Periodic sync while the server runs (only when credentials are configured)."""
    if not configured():
        return

    def loop():
        while True:
            try:
                res = sync()
                log.info("water sync: %s", res)
            except Exception as exc:  # network down, auth changed … keep serving the data we have
                store.set_meta("water_live_error", f"{datetime.now():%d %b %H:%M} {type(exc).__name__}: {exc}")
                log.warning("water sync failed: %s", exc)
            time.sleep(settings.WATER_SYNC_MINUTES * 60)

    threading.Thread(target=loop, name="water-sync", daemon=True).start()
