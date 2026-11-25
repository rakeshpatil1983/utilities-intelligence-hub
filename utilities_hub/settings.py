"""Utilities Hackathon — energy + water analytics hub (no login)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "hackathon-local-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

# No accounts, no admin, no sessions: an open hackathon workspace.
INSTALLED_APPS = ["django.contrib.staticfiles", "hub"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "utilities_hub.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request", "hub.context.common"]},
}]
WSGI_APPLICATION = "utilities_hub.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "data" / "django.sqlite3"}}

LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = False
USE_TZ = False
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
X_FRAME_OPTIONS = "SAMEORIGIN"

# ── Data ────────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("HUB_DATA_DIR", BASE_DIR / "data"))
HUB_DB = DATA_DIR / "hub.duckdb"
# Energy: 15-minute STMU readings (Parquet) + the unit summary.
ENERGY_PARQUET = Path(os.environ.get("ENERGY_PARQUET", DATA_DIR / "source" / "energy_15m.parquet"))
ENERGY_UNITS_JSON = Path(os.environ.get("ENERGY_UNITS_JSON", DATA_DIR / "source" / "energy_units.json"))
# Water: history imported from the platform's MySQL dump, plus the live platform API.
WATER_HISTORY = DATA_DIR / "water_history.parquet"
WATER_DUMP = os.environ.get("WATER_DUMP", "")
WATER_API_BASE = os.environ.get("WATER_API_BASE", "http://192.168.0.26:8000")
WATER_API_USERNAME = os.environ.get("WATER_API_USERNAME", "")
WATER_API_PASSWORD = os.environ.get("WATER_API_PASSWORD", "")
WATER_SYNC_MINUTES = int(os.environ.get("WATER_SYNC_MINUTES", "15"))

# Assumptions used for water–energy estimates (shown on the pages that use them).
PUMP_KW = float(os.environ.get("PUMP_KW", "3.7"))                 # 5 HP submersible / monoblock
TANK_KL = float(os.environ.get("TANK_KL", "50"))                  # overhead tank capacity, kL
TARIFF_RS_KWH = float(os.environ.get("TARIFF_RS_KWH", "7.5"))

# AI suggestions (OpenRouter). The model name stays in .env and is never shown in the UI.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "")

# Consumer smart meters: results of the predictive meter-health workspace (AMR), snapshotted to Parquet.
_AMR = "/run/media/rakesh/Workspace/flywheels_workspace/teknovate/AMR/predictive_demo_hackathon_dev/media"
METER_COMBINED_DB = os.environ.get("METER_COMBINED_DB", f"{_AMR}/combined_runs/combined_dashboard.duckdb")
METER_TWIN_DB = os.environ.get("METER_TWIN_DB", f"{_AMR}/digital_twin/digital_twin.duckdb")
METER_DIR = DATA_DIR / "meters"

# Solar plants and inverter blocks.
SOLAR_FLEET = Path(os.environ.get("SOLAR_FLEET", DATA_DIR / "source" / "solar_fleet.json"))
