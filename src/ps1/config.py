"""Environment-driven configuration shared by all PS1 modules."""
from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

DATA_DIR = Path(os.environ.get("PS1_DATA_DIR", "/home/factory-user/ps1-scratch/data"))
WORK_DIR = Path(os.environ.get("PS1_WORK_DIR", "/home/factory-user/ps1-scratch/work"))
OTP_URL = os.environ.get("PS1_OTP_URL", "http://localhost:3102/otp/gtfs/v1")

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

LIVE_FEED_URL = os.environ.get(
    "PS1_LIVE_FEED_URL",
    "https://dts-backend.transportstack.in/api/dataset/otd/get-file"
    "?agency=delhi-buses&category=realtime_gtfs&filename=VehiclePositions.pb",
)
LIVE_POLL_INTERVAL_S = float(os.environ.get("PS1_LIVE_POLL_INTERVAL_S", "25"))
LIVE_STALE_THRESHOLD_S = float(os.environ.get("PS1_LIVE_STALE_THRESHOLD_S", "600"))
LIVE_ENTITY_MAX_AGE_S = float(os.environ.get("PS1_LIVE_ENTITY_MAX_AGE_S", "900"))
LIVE_MIN_VEHICLES = int(os.environ.get("PS1_LIVE_MIN_VEHICLES", "10"))
LIVE_SNAPSHOT_KEEP = int(os.environ.get("PS1_LIVE_SNAPSHOT_KEEP", "30"))

