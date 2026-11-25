"""Bring the demo data up to today, transparently.

* Energy (STMU 15-min readings): units still reporting at the end of the export are carried forward from
  their own last-4-weeks weekday x 15-minute median profile; every added row has ``extended = true``.
* Smart meters, water history and the solar fleet: the recorded readings are replayed so the latest one
  lands on today (timestamps shifted by a whole number of days).

Originals are kept in ``data/source/original/`` and every run starts from them, so it can be re-run any
day. What was done is written to ``data/source/demo_clock.json`` and shown in the sidebar.
"""
import json
import re
import shutil
from datetime import datetime, timedelta

import duckdb
from django.conf import settings
from django.core.management.base import BaseCommand

NUM = ["supply", "v_r", "v_y", "v_b", "v_rn", "v_yn", "v_bn", "i_r", "i_y", "i_b", "i_n",
       "kw", "kvar", "kva", "kw_r", "kw_y", "kw_b", "freq", "thd_v", "thd_i"]
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _orig(p):
    o = settings.DATA_DIR / "source" / "original" / p.name
    o.parent.mkdir(parents=True, exist_ok=True)
    if not o.exists() and p.exists():
        shutil.copy2(p, o)
    return o


def _shift_days(last, now):
    d = (now.date() - last.date()).days
    return d - 1 if last + timedelta(days=d) > now else d


class Command(BaseCommand):
    help = "Bring the demo data up to today (energy extended from profiles; meters, water, solar replayed)."

    def handle(self, *args, **o):
        now = datetime.now().replace(second=0, microsecond=0)
        clock = {"generated_at": now.isoformat(timespec="minutes")}
        con = duckdb.connect()

        # ── energy: extend from each unit's own weekday x slot profile ──
        ep = settings.ENERGY_PARQUET
        src = _orig(ep)
        m = con.execute(f"SELECT max(slot) FROM read_parquet('{src}')").fetchone()[0]
        end = now - timedelta(minutes=now.minute % 15)
        med = ", ".join(f"median({c}) AS {c}" for c in NUM)
        sel = ", ".join(f"p.{c}" for c in NUM)
        con.execute(f"""
          CREATE TEMP TABLE meas AS SELECT *, false AS extended FROM read_parquet('{src}');
          CREATE TEMP TABLE units AS SELECT imei, max(slot) AS last FROM meas GROUP BY 1 HAVING max(slot) >= TIMESTAMP '{m}' - INTERVAL 1 DAY;
          CREATE TEMP TABLE prof AS SELECT imei, isodow(slot) AS dow, strftime(slot, '%H:%M') AS tod, {med}
            FROM meas WHERE slot > TIMESTAMP '{m}' - INTERVAL 28 DAY AND imei IN (SELECT imei FROM units) GROUP BY 1, 2, 3;
          CREATE TEMP TABLE ext AS
            SELECT u.imei, g.slot, 1::BIGINT AS n, {sel}, true AS extended
            FROM units u, generate_series(TIMESTAMP '{m}' + INTERVAL 15 MINUTE, TIMESTAMP '{end}', INTERVAL 15 MINUTE) g(slot)
            JOIN prof p ON p.imei = u.imei AND p.dow = isodow(g.slot) AND p.tod = strftime(g.slot, '%H:%M')
            WHERE g.slot > u.last;
          UPDATE ext SET supply = round(supply);
        """)
        cols = ", ".join(["imei", "slot", "n"] + NUM + ["extended"])
        tmp = ep.with_suffix(".tmp.parquet")
        con.execute(f"COPY (SELECT {cols} FROM meas UNION ALL SELECT {cols} FROM ext ORDER BY imei, slot) TO '{tmp}' (FORMAT PARQUET)")
        tmp.replace(ep)
        n_ext, n_units = con.execute("SELECT count(*), count(DISTINCT imei) FROM ext").fetchone()
        clock["energy"] = {"measured_until": m.isoformat(timespec="minutes"), "extended_until": end.isoformat(timespec="minutes"),
                           "units_extended": n_units, "rows_added": n_ext}
        self.stdout.write(f"energy: measured to {m}, {n_units} units extended to {end} ({n_ext} rows)")

        # ── parquet replays (meters, water history) ──
        def replay(p, cols, key, d=None):
            if not p.exists():
                return
            s = _orig(p)
            last = con.execute(f"SELECT max({cols[0]}) FROM read_parquet('{s}')").fetchone()[0]
            first = con.execute(f"SELECT min({cols[0]}) FROM read_parquet('{s}')").fetchone()[0]
            d = _shift_days(last, now) if d is None else d
            repl = ", ".join(f"{c} + INTERVAL {d} DAY AS {c}" for c in cols)
            tmp = p.with_suffix(".tmp.parquet")
            con.execute(f"COPY (SELECT * REPLACE ({repl}) FROM read_parquet('{s}')) TO '{tmp}' (FORMAT PARQUET)")
            tmp.replace(p)
            clock.setdefault(key, {"recorded_from": first.isoformat(timespec="minutes"), "recorded_to": last.isoformat(timespec="minutes"),
                                   "shift_days": d})
            self.stdout.write(f"{p.name}: recorded {first:%d %b %Y} - {last:%d %b %Y}, replayed +{d} days")
            return d

        md = settings.METER_DIR
        dm = replay(md / "meter_logs.parquet", ["meter_timestamp"], "meters")
        replay(md / "meter_daily.parquet", ["meter_timestamp"], "meters_daily", dm)
        replay(md / "twin_dt.parquet", ["meter_timestamp", "landing_timestamp"], "meters_twin", dm)
        replay(settings.WATER_HISTORY, ["ts"], "water")

        # ── solar fleet json ──
        sp = settings.SOLAR_FLEET
        if sp.exists():
            s = json.loads(_orig(sp).read_text())
            last = datetime.fromisoformat(s["as_of"])
            d = _shift_days(last, now)

            def walk(x):
                if isinstance(x, dict):
                    return {k: walk(v) for k, v in x.items()}
                if isinstance(x, list):
                    return [walk(v) for v in x]
                if isinstance(x, str) and ISO.match(x):
                    try:
                        return (datetime.fromisoformat(x) + timedelta(days=d)).isoformat()
                    except ValueError:
                        return x
                return x
            sp.write_text(json.dumps(walk(s)))
            clock["solar"] = {"recorded_as_of": last.isoformat(timespec="minutes"), "shift_days": d}
            self.stdout.write(f"solar: as of {last} replayed +{d} days")

        (settings.DATA_DIR / "source" / "demo_clock.json").write_text(json.dumps(clock, indent=1))
        self.stdout.write(self.style.SUCCESS("demo clock written"))
