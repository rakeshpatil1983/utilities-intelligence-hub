# Utilities Intelligence Hub

Team Teknovate · 2026 oneM2M Hackathon

One Django dashboard for **distribution transformers**, **consumer smart meters**, **solar plants** and **drinking-water supply**: every asset is scored from its own
readings, issues are listed with the evidence rows behind them, and AI suggestions turn the findings into a work plan.
**No login** — open workspace for the hackathon, plus an open JSON API for teams.

## Run
```bash
.venv/bin/python manage.py runserver 0.0.0.0:8030      # or ./run.sh
```
Pages: `/` overview · `/energy/` (energy · transformers) · `/transformer/<DT-xx>/` · `/meters/` · `/meter/<no>/` · `/solar/` · `/solar/block/<id>/` · `/water/` · `/asset/<id>/` · `/alerts/` · `/ai/` · `/api/`.

Every section lists its devices worst-first with quick-filter chips (current and predicted issues); a device opens on the right with tabs (summary, readings, forecast, predictive, evidence, AI).

`manage.py roll_to_today` brings the demo data up to today (energy carried forward on each unit's own weekday profile and flagged `extended`; meter, water and solar history replayed), and the sidebar says so.

## Data
| Utility | Source | Where |
|---|---|---|
| Energy | 31 distribution transformers, STMU smart meters, 15-min (V, I, kW, kVA, PF, THD, neutral, Hz), Nov 2025 – Nov 2026 | `data/source/energy_15m.parquet` + `energy_units.json` |
| Smart meters | 61,062 consumer meters: meter health index, predicted failure window, event / block-load / daily-reading faults, feeder → DT → meter topology, DT digital twin, interval logs | `manage.py import_meters` snapshots the predictive meter-health workspace (`teknovate/AMR/predictive_demo_hackathon_dev/media`) → `data/meters/*.parquet` |
| Transformer intelligence | From the energy readings: phase balance, 7-day load forecast with overload probability, 12-month capacity plan, 5-month outlook of load and estimated top-oil / hot-spot temperature (IEC 60076-7) | `hub/dtintel.py` |
| Solar | 5 plants, 12 inverter blocks: status, PR, availability, trips and issues | `data/source/solar_fleet.json` |
| Water — history | Pump controllers, pump + overhead-tank controllers (pump, tank, sump, flow / dry-run, phase voltages), pH monitor, 2022–2023 | `manage.py import_water_dump <all_databases.sql>` → `data/water_history.parquet` |
| Water — live | Water platform API at `192.168.0.26:8000` (FastAPI, bearer token): `/api/v1/devices`, `/api/v1/reports` | `manage.py sync_water`; also every 15 min while the server runs |

The live water sync needs an account on that platform: set `WATER_API_USERNAME` / `WATER_API_PASSWORD` in `.env`
(the MQTT broker on :1883 also requires credentials). Without it the water pages use the history.

`data/` and `.env` are git-ignored (customer data, keys).

## Analytics
* **Energy** (last 30 days per transformer): overload / loading vs rating, power factor, current unbalance, neutral current,
  low / high voltage (230 V ± 6 %), voltage THD (median, mis-scaled channels flagged), supply availability, frequency,
  consumption jumps / drops (z-score vs own baseline), 7-day kWh forecast, health 0–100.
* **Water** (per controller): pump run-time and starts per day, **dry runs** (pump on with dry flow / empty sump or well),
  **overflow** (pump on, tank full), **single-phasing** and low voltage while pumping, short-cycling, long runs,
  pH out of 6.5–8.5, dead pH probe, level-sensor errors, offline.
* **Smart meters**: health bands, failure windows, fault types (current circuit open / bypass, tamper or wrong wiring,
  communication gaps, over-current, over-voltage, register anomalies, clock drift, loose contact) with a physical field action
  per type, feeder and DT hotspots (meters below each DT + the DT digital twin's balance check). Critical meters and DT twin
  alarms join the common alert feed.
* **Water–energy link**: pump energy and cost (run-time × `PUMP_KW`, `TARIFF_RS_KWH`), energy wasted in dry runs / overflows.
* **AI suggestions**: OpenRouter (`OPENROUTER_API_KEY`, `OPENROUTER_MODEL` in `.env`) over the KPIs and findings — brief per
  utility / asset and free questions; rule-based fallback when unavailable.

## oneM2M interworking
The hub is a oneM2M **Application Entity** (TS-0001 resource model, TS-0004 primitives, TS-0009 HTTP binding), see `hub/onem2m.py`:

```
<CSE base>/UtilitiesHub (AE)/<energy|water|solar|meters> (cnt)/<device> (cnt)/status (cnt, mni 50)/<cin>
cin.con = {"health", "issues", "prediction"/"dry_runs"/"pr30"/"failure_window", "as_of"}
```

* `manage.py onem2m_push` registers the AE in `ONEM2M_CSE` (ACME, OM2M, Mobius …), creates the containers and posts one
  contentInstance per device (31 transformers, 7 water controllers, 12 solar blocks, critical meters). Re-run on a schedule.
* `manage.py onem2m_push --subscribe <path>` subscribes the hub to a container a field gateway writes to; the CSE sends
  `m2m:sgn` notifications to `POST /onem2m/notify` (answered with `X-M2M-RSC: 2000`), listed on the `/oneM2M` page.
* `/api/v1/onem2m` shows the resource tree and the status documents.

Settings (`.env`): `ONEM2M_CSE` (e.g. `http://127.0.0.1:8080/cse-in`), `ONEM2M_ORIGIN` (`CUtilitiesHub`), `ONEM2M_AE`,
`ONEM2M_RVI` (`3`), `ONEM2M_POA` (this hub's `/onem2m/notify` URL as the CSE sees it), `ONEM2M_METERS`.

## Open API
`/api/v1/summary` · `/api/v1/assets?utility=` · `/api/v1/findings?utility=&severity=&code=` · `/api/v1/assets/<id>/series?days=7` · `/api/v1/meters?band=&cat=&feeder=&dt=&q=` · `/api/v1/meters/<no>` · `/api/v1/transformers` · `/api/v1/solar` · `/api/v1/onem2m`

## Tests
```bash
.venv/bin/python manage.py test hub
```
