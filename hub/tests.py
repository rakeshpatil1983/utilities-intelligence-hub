import json
from datetime import datetime, timedelta
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from hub import analytics, store
from hub.water_dump import normalise, tank_pct


class ParsingTests(SimpleTestCase):
    def test_tank_strings(self):
        self.assertEqual(tank_pct("EMPTY"), 0.0)
        self.assertEqual(tank_pct("100%"), 100.0)
        self.assertEqual(tank_pct("FULL"), 100.0)
        self.assertEqual(tank_pct("50%"), 50.0)
        self.assertIsNone(tank_pct("SENSOR ERROR"))
        self.assertIsNone(tank_pct("ERROR"))
        self.assertEqual(tank_pct("LOW"), 25.0)

    def test_platform_payloads(self):
        r = normalise({"pump": "ON", "sump": "water", "flow": "dry run", "tank": "EMPTY", "Ph1": 231.0, "Ph2": 0.0, "Ph3": 229.5})
        self.assertEqual((r["pump"], r["flow"], r["tank_pct"], r["v2"]), (1, "dry run", 0.0, 0.0))
        r = normalise({"Pump Status": "OFF", "Well Status": "FULL"})
        self.assertEqual((r["pump"], r["well"]), (0, "full"))
        self.assertEqual(normalise({"PH": "7.2", "Float": "empty"})["ph"], 7.2)
        self.assertIsNone(normalise({"V1": "240", "KW1": "1.2"}))      # energy payloads are not water


def _rows(spec, start=datetime(2026, 1, 1, 6, 0)):
    """spec: list of (minutes offset, pump, flow, tank_pct, v1, v2, v3)."""
    out = []
    for m, pump, flow, tank, v1, v2, v3 in spec:
        out.append({"asset_id": "WT", "ts": start + timedelta(minutes=m), "device_id": 1, "device_type": "t", "pump": pump,
                    "tank_raw": None, "tank_pct": tank, "sump": "water", "flow": flow, "well": None, "ph": None,
                    "float_sw": None, "v1": v1, "v2": v2, "v3": v3, "src": "live"})
    return out


class WaterDetectionTests(SimpleTestCase):
    def build(self, rows):
        with mock.patch.object(store, "query", return_value=rows), mock.patch.object(store, "get_meta", return_value=None), \
             mock.patch("hub.analytics.datetime") as dt:
            dt.now.return_value = rows[-1]["ts"] + timedelta(minutes=5)
            return analytics._water_build()["assets"]["WT"]

    def test_dry_run_single_phase_and_overflow(self):
        rows = _rows([(0, 0, "wet run", 20, 230, 231, 229), (5, 1, "dry run", 20, 230, 231, 229), (10, 1, "dry run", 20, 230, 231, 229),
                      (15, 1, "wet run", 60, 230, 0, 229), (20, 1, "wet run", 100, 230, 231, 229), (25, 0, "wet run", 100, 230, 231, 229)])
        a = self.build(rows)
        codes = {f["code"] for f in a["findings"]}
        self.assertTrue({"DRY_RUN", "SINGLE_PHASING", "OVERFLOW"} <= codes, codes)
        self.assertAlmostEqual(a["runtime_h"], 20 / 60, places=2)
        self.assertEqual(a["pump_now"], 0)

    def test_healthy_pump(self):
        a = self.build(_rows([(0, 0, "wet run", 30, 230, 231, 229), (30, 1, "wet run", 40, 228, 229, 227), (60, 0, "wet run", 95, 230, 231, 229)]))
        self.assertEqual([f["code"] for f in a["findings"]], [])
        self.assertEqual(a["health"], 100)

    def test_bucketing_keeps_gaps_and_pump_on(self):
        rows = _rows([(0, 0, None, 10, 230, 230, 230), (1, 1, None, 12, 225, 228, 229), (35, 0, None, 50, 230, 230, 230)])
        b = analytics.bucket_water(rows, minutes=10)
        self.assertEqual(len(b), 4)
        self.assertEqual(b[0]["pump"], 1)              # on at any time in the slot
        self.assertEqual(b[0]["v1"], 225)              # voltage while pumping
        self.assertIsNone(b[1]["pump"])                # no reading → gap


@override_settings(OPENROUTER_API_KEY="")
class PageTests(SimpleTestCase):
    def test_pages_and_api(self):
        for url in ("/", "/energy/", "/water/", "/alerts/", "/alerts/?utility=water", "/ai/", "/api/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        summary = self.client.get("/api/v1/summary").json()
        self.assertIn("energy", summary)
        assets = self.client.get("/api/v1/assets").json()["assets"]
        self.assertTrue(assets)
        for a in assets:
            self.assertEqual(self.client.get(f"/asset/{a['asset_id']}/", follow=True).status_code, 200, a["asset_id"])
        fs = self.client.get("/api/v1/findings").json()["findings"]
        f = next(x for x in fs if x["start"])
        r = self.client.get("/fragment/evidence/", {"asset": f["asset_id"], "start": f["start"], "end": f["end"], "title": f["title"]})
        self.assertContains(r, "Readings")
        self.assertEqual(self.client.get("/asset/NOPE/").status_code, 404)

    def test_ai_falls_back_without_key(self):
        r = self.client.post("/ai/ask.json", data=json.dumps({"question": "what first?"}), content_type="application/json")
        self.assertEqual(r.json()["source"], "rules")
        b = self.client.get("/ai/brief.json?scope=all").json()
        self.assertFalse(b["pending"])
        self.assertIn(b["brief"]["source"], ("rules", "ai"))   # a brief cached earlier is still served

    def test_smart_meters(self):
        from hub import meters
        if not meters.available():
            self.skipTest("meter snapshot not imported")
        for url in ("/meters/", "/meters/?band=Critical", "/meters/?cat=ct_open", "/meters/?cat=rtc", "/alerts/?utility=meters"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        d = self.client.get("/api/v1/meters?band=Critical&limit=5").json()
        self.assertEqual(d["total"], d["summary"]["bands"][0]["n"])
        m = d["meters"][0]
        self.assertEqual(m["mhi_band"], "Critical")
        self.assertNotIn("Open the linked source dashboard", m["action"])   # generic text replaced by a field action
        self.assertContains(self.client.get(f"/meter/{m['meter_id']}/"), "Field action")
        self.assertEqual(self.client.get("/meter/NOPE/").status_code, 404)
        ov = analytics.overview()
        self.assertGreaterEqual(len({f["utility"] for f in ov["top"][:12]}), 4)   # top list covers every utility

    def test_meter_categories(self):
        from hub import meters
        cats = [c[0] for c in meters.categorise({"event_faults": "Current circuit open under live voltage",
                                                 "blp_faults": "Missing BLP intervals", "dlp_faults": "Reverse cumulative kWh reading"})]
        self.assertEqual(cats, ["ct_open", "comms", "register"])

    def test_transformer_intelligence(self):
        from . import dtintel
        x = dtintel.fleet()["by_id"]
        self.assertGreater(len(x), 20)
        with_fc = [v for v in x.values() if v.get("forecast")]
        self.assertTrue(with_fc)
        v = with_fc[0]
        self.assertEqual(len(v["forecast"]["days"]), 7)
        self.assertTrue(v["months"])
        self.assertEqual(set(v["outlook"]), {"load", "oti", "wti"})
        self.assertTrue(any(v.get("phase") and v["phase"]["flagged"] for v in x.values()))
        aid = with_fc[0]["asset_id"]
        for url in ("/energy/", f"/transformer/{aid}/", f"/transformer/{aid}/?embed=1", "/api/v1/transformers", "/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_solar(self):
        from . import solar
        s = solar.summary()
        self.assertGreater(s["plants"], 0)
        self.assertTrue(any(f["utility"] == "solar" for f in analytics.overview()["findings"]))
        b = solar.fleet()["blocks"][0]["inv_id"]
        for url in ("/solar/", f"/solar/?sel={b}", f"/solar/block/{b}/", f"/solar/block/{b}/?embed=1", "/alerts/?utility=solar", "/api/v1/solar"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_no_login_anywhere(self):
        self.assertNotIn("django.contrib.auth", settings.INSTALLED_APPS)
        self.assertNotIn("/login", self.client.get("/").content.decode())
