"""Environment-driven configuration shared by all delhi_transit modules."""
from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values

IST = timezone(timedelta(hours=5, minutes=30))

# backend/src/delhi_transit/core/config.py -> repository root (reports/, .env).
REPO_ROOT = Path(__file__).resolve().parents[4]

DEFAULT_DATA_DIR = Path("/home/factory-user/ps1-scratch/data")
DATA_DIR = Path(os.environ.get("DELHI_TRANSIT_DATA_DIR", str(DEFAULT_DATA_DIR)))
WORK_DIR = Path(os.environ.get("DELHI_TRANSIT_WORK_DIR", "/home/factory-user/ps1-scratch/work"))
OTP_URL = os.environ.get("DELHI_TRANSIT_OTP_URL", "http://localhost:3102/otp/gtfs/v1")

BBOX_LAT_MIN = 27.5
BBOX_LAT_MAX = 29.0
BBOX_LNG_MIN = 76.7
BBOX_LNG_MAX = 77.6

ARCHIVE_URL = (
    "https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/"
    "delhi-dtc-bus-gtfs-rt-2026-10.7z"
)
ARCHIVE_USER_AGENT = "curl/8.5.0"

GPS_DIR = "gps"
LIVE_DIR = "live"
GOOGLE_DIR = "google"
GOOGLE_HARD_CALL_CAP = 300
GOOGLE_MISSION_LEDGER_PATH = Path("/home/factory-user/ps1-scratch/google-mission-call-ledger.json")
# Pre-ledger call counter; seeds the fixed ledger if that file is lost, whatever DELHI_TRANSIT_DATA_DIR says.
GOOGLE_LEGACY_COUNTER_PATH = DEFAULT_DATA_DIR / GOOGLE_DIR / "call_counter.json"

LIVE_FEED_URL = os.environ.get(
    "DELHI_TRANSIT_LIVE_FEED_URL",
    "https://dts-backend.transportstack.in/api/dataset/otd/get-file"
    "?agency=delhi-buses&category=realtime_gtfs&filename=VehiclePositions.pb",
)
LIVE_POLL_INTERVAL_S = float(os.environ.get("DELHI_TRANSIT_LIVE_POLL_INTERVAL_S", "25"))
LIVE_STALE_THRESHOLD_S = float(os.environ.get("DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S", "600"))
LIVE_ENTITY_MAX_AGE_S = float(os.environ.get("DELHI_TRANSIT_LIVE_ENTITY_MAX_AGE_S", "900"))
LIVE_MIN_VEHICLES = int(os.environ.get("DELHI_TRANSIT_LIVE_MIN_VEHICLES", "10"))
LIVE_SNAPSHOT_KEEP = int(os.environ.get("DELHI_TRANSIT_LIVE_SNAPSHOT_KEEP", "30"))


def load_env_key(name: str, env_path: Path | None = None) -> str | None:
    """Read a secret from the environment, else from the repo .env. Never log the value."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    dotenv_file = env_path or (REPO_ROOT / ".env")
    if dotenv_file.exists():
        value = (dotenv_values(dotenv_file).get(name) or "").strip()
        if value:
            return value
    return None

