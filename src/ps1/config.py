"""Environment-driven configuration shared by all PS1 modules."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("PS1_DATA_DIR", "/home/factory-user/ps1-scratch/data"))
WORK_DIR = Path(os.environ.get("PS1_WORK_DIR", "/home/factory-user/ps1-scratch/work"))
OTP_URL = os.environ.get("PS1_OTP_URL", "http://localhost:3102/otp/gtfs/v1")

API_PORT = int(os.environ.get("PS1_API_PORT", "3100"))
OTP_PORT = int(os.environ.get("PS1_OTP_PORT", "3102"))

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
