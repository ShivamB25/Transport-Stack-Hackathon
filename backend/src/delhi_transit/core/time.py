"""Time formatting helpers shared across packages (Asia/Kolkata and UTC)."""
from __future__ import annotations

from datetime import UTC, datetime

from delhi_transit.core.config import IST


def iso_ist(ts: float) -> str:
    """Epoch seconds as an ISO 8601 string in Asia/Kolkata (+05:30)."""
    return datetime.fromtimestamp(ts, IST).isoformat()


def hhmmss_ist(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S")


def utc_z(moment: datetime | None = None) -> str:
    """A timezone-aware datetime (default: now) as `YYYY-MM-DDTHH:MM:SSZ`."""
    return (moment or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
