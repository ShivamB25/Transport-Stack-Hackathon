"""Pure causal feature arithmetic shared by replay evaluation and the journey planner.

Callers decide which observations are visible before their cutoff; everything here only
combines already-visible values, so it cannot introduce future data.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from statistics import median
from typing import TYPE_CHECKING

from delhi_transit.core.geo import haversine_m

if TYPE_CHECKING:
    from delhi_transit.model.features import PassageRow, StretchError

RECENT_WINDOW_S = 1800
MAX_VALID_SCHEDULE_DELAY_S = 21_600
MAX_HEADWAY_S = 14_400

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

NUMERIC_FEATURE_NAMES: tuple[str, ...] = (
    "remaining_stops",
    "remaining_distance_m",
    "stretch_distance_m",
    "scheduled_duration_s",
    "ping_age_s",
    "from_observed",
    "recent_vehicle_progress_ratio",
    "recent_vehicle_speed_mps",
    "last_stop_delay_s",
    "last_stretch_error_s",
    "previous_error_s",
    "previous_error_age_s",
    "previous_observed",
    "recent_same_direction_speed_mps",
    "recent_same_direction_pace_ratio",
    "headway_s",
    "hour",
    "minute_of_day",
    "tod_sin",
    "tod_cos",
    "day_of_week",
    "is_weekend",
    "route_id",
    "from_sequence",
)

# Features that depend on observations; NaN (or 0.0 for the flags) when nothing was visible.
_OBSERVED_DEFAULTS: dict[str, float] = {
    "ping_age_s": math.nan,
    "from_observed": 0.0,
    "recent_vehicle_progress_ratio": math.nan,
    "recent_vehicle_speed_mps": math.nan,
    "last_stop_delay_s": math.nan,
    "last_stretch_error_s": math.nan,
    "previous_error_s": math.nan,
    "previous_error_age_s": math.nan,
    "previous_observed": 0.0,
    "recent_same_direction_speed_mps": math.nan,
    "recent_same_direction_pace_ratio": math.nan,
    "headway_s": math.nan,
}


@dataclass(frozen=True)
class CausalFeatures:
    """Strictly causal feature vector known at or before the query cutoff."""

    remaining_stops: int
    remaining_distance_m: float
    stretch_distance_m: float
    scheduled_duration_s: float
    ping_age_s: float
    from_observed: float
    recent_vehicle_progress_ratio: float
    recent_vehicle_speed_mps: float
    last_stop_delay_s: float
    last_stretch_error_s: float
    previous_error_s: float
    previous_error_age_s: float
    previous_observed: float
    recent_same_direction_speed_mps: float
    recent_same_direction_pace_ratio: float
    headway_s: float
    hour: int
    minute_of_day: int
    tod_sin: float
    tod_cos: float
    day_of_week: int
    day_type: str
    coarse_day_type: str
    is_weekend: float
    route_id: int
    from_sequence: int

    def as_numeric_list(self) -> list[float]:
        return [float(getattr(self, name)) for name in NUMERIC_FEATURE_NAMES]


@dataclass(frozen=True)
class RouteGeometry:
    """Static route stop-sequence geometry known before any run starts."""

    max_sequence: int
    stretch_distances_m: dict[tuple[int, int], float]
    remaining_distances_m: dict[int, float]

    def stretch_distance(self, stretch: StretchError) -> float | None:
        return self.stretch_distances_m.get((stretch.from_sequence, stretch.to_sequence))


def build_route_geometry(passages: Iterable[PassageRow]) -> dict[tuple[int, str], RouteGeometry]:
    """Extract static per-route inter-stop and remaining distances from stop coordinates."""
    coords_by_route: dict[tuple[int, str], dict[int, tuple[float, float]]] = defaultdict(dict)
    max_seq_by_route: dict[tuple[int, str], int] = defaultdict(int)
    for row in passages:
        key = (row.route_id, row.direction)
        max_seq_by_route[key] = max(max_seq_by_route[key], row.stop_sequence)
        if (
            row.stop_sequence not in coords_by_route[key]
            and row.stop_lat is not None
            and row.stop_lng is not None
        ):
            coords_by_route[key][row.stop_sequence] = (float(row.stop_lat), float(row.stop_lng))

    geometries: dict[tuple[int, str], RouteGeometry] = {}
    for key, max_seq in max_seq_by_route.items():
        seq_coords = coords_by_route.get(key, {})
        ordered_seqs = sorted(seq_coords)
        stretch_distances: dict[tuple[int, int], float] = {}
        for s1, s2 in pairwise(ordered_seqs):
            lat1, lng1 = seq_coords[s1]
            lat2, lng2 = seq_coords[s2]
            stretch_distances[(s1, s2)] = haversine_m(lat1, lng1, lat2, lng2)
        remaining: dict[int, float] = {}
        running = 0.0
        if ordered_seqs:
            remaining[ordered_seqs[-1]] = 0.0
            for idx in range(len(ordered_seqs) - 2, -1, -1):
                s1, s2 = ordered_seqs[idx], ordered_seqs[idx + 1]
                running += stretch_distances.get((s1, s2), 0.0)
                remaining[s1] = running
        geometries[key] = RouteGeometry(
            max_sequence=max_seq,
            stretch_distances_m=stretch_distances,
            remaining_distances_m=remaining,
        )
    return geometries


def coarse_day_type(day_of_week: int) -> str:
    if day_of_week < 5:
        return "weekday"
    return "saturday" if day_of_week == 5 else "sunday"


def schedule_delay_s(raw_delay_s: int) -> float:
    """Schedule delay of the last stop, NaN when it is implausibly large."""
    return float(raw_delay_s) if abs(raw_delay_s) <= MAX_VALID_SCHEDULE_DELAY_S else math.nan


def headway_s(gap_s: int) -> float:
    return float(gap_s) if 0 < gap_s <= MAX_HEADWAY_S else math.nan


def vehicle_progress(
    recent: Sequence[StretchError], geometry: RouteGeometry | None
) -> tuple[float, float]:
    """Clamped pace ratio and speed of one vehicle over its latest completed stretches."""
    sched_sum = sum(s.scheduled_duration_s for s in recent)
    act_sum = sum(s.actual_duration_s for s in recent)
    ratio = max(0.2, min(5.0, act_sum / sched_sum)) if sched_sum > 0 else math.nan
    speed = math.nan
    if geometry is not None and act_sum > 0:
        valid = [
            (distance, s.actual_duration_s)
            for s in recent
            if (distance := geometry.stretch_distance(s)) is not None
            and distance > 0
            and s.actual_duration_s > 0
        ]
        if valid:
            speed = max(0.1, min(30.0, sum(d for d, _ in valid) / sum(t for _, t in valid)))
    return ratio, speed


def pace_and_speed(
    pool: Sequence[StretchError], geometry: RouteGeometry | None
) -> tuple[float, float]:
    """Median clamped pace ratio and speed over recent stretches of other runs."""
    paces = [
        max(0.2, min(5.0, s.actual_duration_s / s.scheduled_duration_s))
        for s in pool
        if s.scheduled_duration_s > 0
    ]
    speeds = []
    if geometry is not None:
        speeds = [
            max(0.1, min(30.0, distance / s.actual_duration_s))
            for s in pool
            if (distance := geometry.stretch_distance(s)) is not None
            and distance > 0
            and s.actual_duration_s > 0
        ]
    return (
        float(median(paces)) if paces else math.nan,
        float(median(speeds)) if speeds else math.nan,
    )


def assemble_features(
    *,
    when: datetime,
    geometry: RouteGeometry | None,
    route_id: int,
    from_sequence: int,
    to_sequence: int,
    scheduled_duration_s: float,
    **observed: float,
) -> CausalFeatures:
    """Combine static geometry, the IST calendar of `when` and the visible observations."""
    minute_of_day = when.hour * 60 + when.minute
    angle = 2.0 * math.pi * minute_of_day / 1440.0
    day_of_week = when.weekday()
    if geometry is not None:
        remaining_stops = max(0, geometry.max_sequence - from_sequence)
        stretch_distance_m = geometry.stretch_distances_m.get((from_sequence, to_sequence), math.nan)
        remaining_distance_m = geometry.remaining_distances_m.get(from_sequence, math.nan)
    else:
        remaining_stops, stretch_distance_m, remaining_distance_m = 0, math.nan, math.nan
    return CausalFeatures(
        remaining_stops=remaining_stops,
        remaining_distance_m=remaining_distance_m,
        stretch_distance_m=stretch_distance_m,
        scheduled_duration_s=float(scheduled_duration_s),
        **{**_OBSERVED_DEFAULTS, **observed},
        hour=when.hour,
        minute_of_day=minute_of_day,
        tod_sin=math.sin(angle),
        tod_cos=math.cos(angle),
        day_of_week=day_of_week,
        day_type=DAY_NAMES[day_of_week],
        coarse_day_type=coarse_day_type(day_of_week),
        is_weekend=1.0 if day_of_week >= 5 else 0.0,
        route_id=route_id,
        from_sequence=from_sequence,
    )
