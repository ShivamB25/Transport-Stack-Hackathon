"""Request-time freshness of live entities: each vehicle is judged by its own timestamp."""
from __future__ import annotations

import json
import time
from pathlib import Path
from statistics import median
from typing import Any

from delhi_transit.core.config import (
    LIVE_DIR,
    LIVE_ENTITY_MAX_AGE_S,
    LIVE_STALE_THRESHOLD_S,
)


def effective_entity_max_age_s(stale_threshold_s: float | None = None) -> float:
    threshold = LIVE_STALE_THRESHOLD_S if stale_threshold_s is None else stale_threshold_s
    return min(float(LIVE_ENTITY_MAX_AGE_S), float(threshold))


def is_fresh(ts: float, now: float, max_age_s: float) -> bool:
    return ts > 0 and 0.0 <= now - ts <= max_age_s


def route_summary(
    route_id: int,
    vehicle_count: int,
    in_bbox_count: int,
    fresh_entities: list[list[float]],
) -> dict[str, Any]:
    """Aggregate fresh in-bbox entities, each stored as [timestamp, speed]."""
    speeds = [speed for _, speed in fresh_entities if speed > 0.2]
    all_speeds = [speed for _, speed in fresh_entities]
    return {
        "route_id": route_id,
        "vehicle_count": vehicle_count,
        "fresh_vehicle_count": len(fresh_entities),
        "in_bbox_count": in_bbox_count,
        "moving_vehicle_count": len(speeds),
        "median_speed_mps": round(float(median(speeds or all_speeds)), 3) if all_speeds else 0.0,
        "latest_ts": int(max((ts for ts, _ in fresh_entities), default=0)),
        "observed_coverage": round(len(fresh_entities) / vehicle_count, 4) if vehicle_count else 0.0,
        "fresh_entities": fresh_entities,
    }


def refresh_route_stat(stat: dict[str, Any], *, now: float, max_age_s: float) -> dict[str, Any]:
    """Recompute a stored route aggregate from only the entities still fresh at `now`."""
    fresh = [entity for entity in stat.get("fresh_entities", []) if is_fresh(entity[0], now, max_age_s)]
    return route_summary(
        int(stat.get("route_id", 0)),
        int(stat.get("vehicle_count", len(fresh))),
        int(stat.get("in_bbox_count", len(fresh))),
        fresh,
    )


def fresh_fleet_count(data_dir: Path, *, now: float, max_age_s: float) -> int:
    """Count vehicles whose own timestamps are still fresh at `now`."""
    path = Path(data_dir) / LIVE_DIR / "route_stats.json"
    try:
        timestamps = json.loads(path.read_text(encoding="utf-8")).get("fresh_vehicle_ts", [])
    except (OSError, ValueError, AttributeError):
        return 0
    return sum(1 for ts in timestamps if is_fresh(ts, now, max_age_s))


def live_feed_age_seconds(state: dict[str, Any] | None, *, now: float | None = None) -> float | None:
    if not state:
        return None
    header_ts = state.get("header_timestamp")
    if header_ts is None:
        return None
    reference = time.time() if now is None else float(now)
    return round(max(0.0, reference - float(header_ts)), 2)
