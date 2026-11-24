"""Stretch error tables and strictly time-ordered causal replay features."""
from __future__ import annotations

import math
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from statistics import median

import duckdb

from delhi_transit.core.config import IST
from delhi_transit.data.stop_passages import haversine_m

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


class CausalityViolationError(ValueError):
    """Raised when feature construction receives non-causal future observations."""


def day_type_for_timestamp(ts: int) -> tuple[int, str, str, float]:
    """Return (day_of_week, day_type, coarse_day_type, is_weekend) in IST."""
    dt = datetime.fromtimestamp(ts, IST)
    dow = dt.weekday()
    day_type = DAY_NAMES[dow]
    if dow < 5:
        coarse = "weekday"
        is_weekend = 0.0
    elif dow == 5:
        coarse = "saturday"
        is_weekend = 1.0
    else:
        coarse = "sunday"
        is_weekend = 1.0
    return dow, day_type, coarse, is_weekend


@dataclass(frozen=True)
class PassageRow:
    day: str
    route_id: int
    direction: str
    vehicle_id: str
    run_id: str
    stop_sequence: int
    stop_id: str
    observed: bool
    ts: int
    scheduled_ts: int
    stop_lat: float | None = None
    stop_lng: float | None = None
    schedule_error_s: int | None = None
    distance_m: float | None = None
    available_at: int | None = None

    @property
    def source_available_at(self) -> int:
        """Return source-GPS availability; synthetic legacy rows default to observed time."""
        return self.ts if self.available_at is None else self.available_at

    @property
    def run_key(self) -> tuple[str, int, str, str]:
        return self.day, self.route_id, self.vehicle_id, self.run_id

    @property
    def effective_schedule_error_s(self) -> int:
        if self.schedule_error_s is not None:
            return self.schedule_error_s
        return self.ts - self.scheduled_ts


@dataclass(frozen=True)
class StretchError:
    day: str
    route_id: int
    direction: str
    vehicle_id: str
    run_id: str
    from_sequence: int
    to_sequence: int
    from_stop_id: str
    to_stop_id: str
    query_ts: int
    to_ts: int
    scheduled_duration_s: int
    actual_duration_s: int
    error_s: int
    from_observed: bool
    to_observed: bool
    from_available_at: int | None = None
    to_available_at: int | None = None

    @property
    def available_at(self) -> int:
        """Return when both endpoint observations made this stretch knowable."""
        from_available = self.query_ts if self.from_available_at is None else self.from_available_at
        to_available = self.to_ts if self.to_available_at is None else self.to_available_at
        return max(from_available, to_available)

    @property
    def run_key(self) -> tuple[str, int, str, str]:
        return self.day, self.route_id, self.vehicle_id, self.run_id

    @property
    def stretch_key(self) -> tuple[int, str, str, str]:
        return self.route_id, self.direction, self.from_stop_id, self.to_stop_id

    @property
    def query_hour(self) -> int:
        return datetime.fromtimestamp(self.query_ts, IST).hour

    @property
    def day_of_week(self) -> int:
        return datetime.fromtimestamp(self.query_ts, IST).weekday()

    @property
    def day_type(self) -> str:
        return DAY_NAMES[self.day_of_week]

    @property
    def coarse_day_type(self) -> str:
        dow = self.day_of_week
        if dow < 5:
            return "weekday"
        return "saturday" if dow == 5 else "sunday"

    @property
    def target_observed(self) -> bool:
        return self.from_observed and self.to_observed


@dataclass(frozen=True)
class CausalFeatures:
    """Strictly causal feature vector known at or before query_ts."""

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
        return [
            float(self.remaining_stops),
            float(self.remaining_distance_m),
            float(self.stretch_distance_m),
            float(self.scheduled_duration_s),
            float(self.ping_age_s),
            float(self.from_observed),
            float(self.recent_vehicle_progress_ratio),
            float(self.recent_vehicle_speed_mps),
            float(self.last_stop_delay_s),
            float(self.last_stretch_error_s),
            float(self.previous_error_s),
            float(self.previous_error_age_s),
            float(self.previous_observed),
            float(self.recent_same_direction_speed_mps),
            float(self.recent_same_direction_pace_ratio),
            float(self.headway_s),
            float(self.hour),
            float(self.minute_of_day),
            float(self.tod_sin),
            float(self.tod_cos),
            float(self.day_of_week),
            float(self.is_weekend),
            float(self.route_id),
            float(self.from_sequence),
        ]


@dataclass(frozen=True)
class ReplayQuery:
    stretch: StretchError
    previous_error_s: int
    previous_run_id: str
    previous_to_ts: int | None = None
    previous_available_at: int | None = None
    previous_observed: bool = False
    features: CausalFeatures | None = None


@dataclass(frozen=True)
class RouteGeometry:
    """Static route stop-sequence geometry known before any run starts."""

    max_sequence: int
    stretch_distances_m: dict[tuple[int, int], float]
    remaining_distances_m: dict[int, float]


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


def load_passages(path: Path) -> list[PassageRow]:
    """Load passage fields needed for stretch errors and causal features from Parquet."""
    path = Path(path)
    con = duckdb.connect()
    try:
        cols = {
            row[0]
            for row in con.execute(
                "describe select * from read_parquet(?)",
                [str(path)],
            ).fetchall()
        }
        optional_selects = [
            "stop_lat" if "stop_lat" in cols else "NULL::DOUBLE as stop_lat",
            "stop_lng" if "stop_lng" in cols else "NULL::DOUBLE as stop_lng",
            "schedule_error_s" if "schedule_error_s" in cols else "NULL::BIGINT as schedule_error_s",
            "distance_m" if "distance_m" in cols else "NULL::DOUBLE as distance_m",
            "available_at",
        ]
        query = f"""
            select day, route_id, direction, vehicle_id, run_id, stop_sequence,
                   stop_id, observed, ts, scheduled_ts,
                   {", ".join(optional_selects)}
            from read_parquet(?)
            order by day, route_id, vehicle_id, run_id, stop_sequence
        """
        rows = con.execute(query, [str(path)]).fetchall()
    finally:
        con.close()
    passages = [PassageRow(*row) for row in rows]
    missing = [p for p in passages if p.available_at is None]
    if missing:
        raise ValueError(f"passage source available_at is required: {path}")
    return passages


def build_stretch_errors(passages: Iterable[PassageRow]) -> list[StretchError]:
    """Convert adjacent stop passages into run-level scheduled travel errors."""
    runs: dict[tuple[str, int, str, str], list[PassageRow]] = defaultdict(list)
    for passage in passages:
        runs[passage.run_key].append(passage)

    stretches = []
    for run_passages in runs.values():
        ordered = sorted(run_passages, key=lambda row: row.stop_sequence)
        for upstream, downstream in pairwise(ordered):
            scheduled_duration = downstream.scheduled_ts - upstream.scheduled_ts
            actual_duration = downstream.ts - upstream.ts
            if scheduled_duration < 0 or actual_duration < 0:
                continue
            stretches.append(
                StretchError(
                    day=upstream.day,
                    route_id=upstream.route_id,
                    direction=upstream.direction,
                    vehicle_id=upstream.vehicle_id,
                    run_id=upstream.run_id,
                    from_sequence=upstream.stop_sequence,
                    to_sequence=downstream.stop_sequence,
                    from_stop_id=upstream.stop_id,
                    to_stop_id=downstream.stop_id,
                    query_ts=upstream.ts,
                    to_ts=downstream.ts,
                    scheduled_duration_s=scheduled_duration,
                    actual_duration_s=actual_duration,
                    error_s=actual_duration - scheduled_duration,
                    from_observed=upstream.observed,
                    to_observed=downstream.observed,
                    from_available_at=upstream.source_available_at,
                    to_available_at=downstream.source_available_at,
                )
            )
    return sorted(
        stretches,
        key=lambda row: (
            row.day,
            row.route_id,
            row.query_ts,
            row.vehicle_id,
            row.run_id,
            row.from_sequence,
        ),
    )


def validate_causal_observations(
    *,
    query_ts: int,
    run_key: tuple[str, int, str, str],
    from_sequence: int,
    same_run_passages: Sequence[PassageRow] = (),
    same_run_stretches: Sequence[StretchError] = (),
    other_run_passages: Sequence[PassageRow] = (),
    other_run_stretches: Sequence[StretchError] = (),
) -> None:
    """Reject any observation that occurs at/after query_ts or downstream of from_sequence."""
    for p in same_run_passages:
        if p.run_key != run_key:
            raise CausalityViolationError(f"unexpected run_key in same_run_passages: {p.run_key}")
        if (
            p.ts >= query_ts
            or p.source_available_at >= query_ts
            or p.stop_sequence > from_sequence
        ):
            raise CausalityViolationError(
                f"future same-run passage leaked: ts={p.ts}, "
                f"available_at={p.source_available_at} >= query_ts={query_ts} "
                f"or seq={p.stop_sequence} > from_sequence={from_sequence}"
            )
    for s in same_run_stretches:
        if s.run_key != run_key:
            raise CausalityViolationError(f"unexpected run_key in same_run_stretches: {s.run_key}")
        if (
            s.to_ts >= query_ts
            or s.query_ts >= query_ts
            or s.available_at >= query_ts
            or s.to_sequence > from_sequence
        ):
            raise CausalityViolationError(
                f"future same-run stretch leaked: to_ts={s.to_ts}, "
                f"available_at={s.available_at} >= query_ts={query_ts} "
                f"or to_sequence={s.to_sequence} > from_sequence={from_sequence}"
            )
    for p in other_run_passages:
        if p.run_key == run_key:
            raise CausalityViolationError("current run supplied in other_run_passages")
        if p.ts >= query_ts or p.source_available_at >= query_ts:
            raise CausalityViolationError(
                f"future other-run passage leaked: ts={p.ts}, "
                f"available_at={p.source_available_at} >= query_ts={query_ts}"
            )
    for s in other_run_stretches:
        if s.run_key == run_key:
            raise CausalityViolationError("current run supplied in other_run_stretches")
        if s.query_ts >= query_ts or s.to_ts >= query_ts or s.available_at >= query_ts:
            raise CausalityViolationError(
                f"future other-run stretch leaked: query_ts={s.query_ts}, to_ts={s.to_ts}, "
                f"available_at={s.available_at} >= {query_ts}"
            )


def _compute_causal_features(
    *,
    current: StretchError,
    previous_error_s: float,
    previous_to_ts: int | None,
    previous_observed: bool,
    geometry: RouteGeometry | None,
    same_run_passages: Sequence[PassageRow],
    same_run_stretches: Sequence[StretchError],
    recent_other_stretches: Sequence[StretchError],
    previous_stop_ts: int | None,
) -> CausalFeatures:
    """Compute CausalFeatures purely from validated causal inputs."""
    query_ts = current.query_ts
    dt = datetime.fromtimestamp(query_ts, IST)
    hour = dt.hour
    minute_of_day = hour * 60 + dt.minute
    angle = 2.0 * math.pi * minute_of_day / 1440.0
    dow, day_type, coarse_day_type, is_weekend = day_type_for_timestamp(query_ts)

    if geometry is not None:
        remaining_stops = max(0, geometry.max_sequence - current.from_sequence)
        stretch_distance_m = geometry.stretch_distances_m.get(
            (current.from_sequence, current.to_sequence), math.nan
        )
        remaining_distance_m = geometry.remaining_distances_m.get(
            current.from_sequence, math.nan
        )
    else:
        remaining_stops = 0
        stretch_distance_m = math.nan
        remaining_distance_m = math.nan

    observed_same_run = [p for p in same_run_passages if p.observed]
    if observed_same_run:
        last_obs = max(observed_same_run, key=lambda p: (p.stop_sequence, p.ts))
        ping_age_s = float(max(0, query_ts - last_obs.ts))
        raw_delay = last_obs.effective_schedule_error_s
        last_stop_delay_s = (
            float(raw_delay) if abs(raw_delay) <= MAX_VALID_SCHEDULE_DELAY_S else math.nan
        )
    elif (
        current.from_observed
        and (current.from_available_at if current.from_available_at is not None else current.query_ts)
        < query_ts
    ):
        ping_age_s = 0.0
        last_stop_delay_s = math.nan
    else:
        ping_age_s = math.nan
        last_stop_delay_s = math.nan

    if same_run_stretches:
        ordered_prior = sorted(same_run_stretches, key=lambda s: s.from_sequence)
        recent_prior = ordered_prior[-3:]
        obs_prior = [s for s in ordered_prior[-5:] if s.target_observed]
        last_stretch = obs_prior[-1] if obs_prior else ordered_prior[-1]
        last_stretch_error_s = float(last_stretch.error_s)

        sched_sum = sum(s.scheduled_duration_s for s in recent_prior)
        act_sum = sum(s.actual_duration_s for s in recent_prior)
        if sched_sum > 0:
            recent_vehicle_progress_ratio = max(0.2, min(5.0, act_sum / sched_sum))
        else:
            recent_vehicle_progress_ratio = math.nan

        if geometry is not None and act_sum > 0:
            dist_pairs = [
                (geometry.stretch_distances_m.get((s.from_sequence, s.to_sequence)), s.actual_duration_s)
                for s in recent_prior
            ]
            valid_pairs = [(d, t) for d, t in dist_pairs if d is not None and d > 0 and t > 0]
            if valid_pairs:
                total_d = sum(d for d, _ in valid_pairs)
                total_t = sum(t for _, t in valid_pairs)
                recent_vehicle_speed_mps = max(0.1, min(30.0, total_d / total_t))
            else:
                recent_vehicle_speed_mps = math.nan
        else:
            recent_vehicle_speed_mps = math.nan
    else:
        last_stretch_error_s = math.nan
        recent_vehicle_progress_ratio = math.nan
        recent_vehicle_speed_mps = math.nan

    if recent_other_stretches:
        obs_other = [s for s in recent_other_stretches if s.target_observed]
        pool = obs_other or recent_other_stretches
        paces = [
            max(0.2, min(5.0, s.actual_duration_s / s.scheduled_duration_s))
            for s in pool
            if s.scheduled_duration_s > 0
        ]
        recent_same_direction_pace_ratio = float(median(paces)) if paces else math.nan
        if geometry is not None:
            speeds = []
            for s in pool:
                dist = geometry.stretch_distances_m.get((s.from_sequence, s.to_sequence))
                if dist is not None and dist > 0 and s.actual_duration_s > 0:
                    speeds.append(max(0.1, min(30.0, dist / s.actual_duration_s)))
            recent_same_direction_speed_mps = float(median(speeds)) if speeds else math.nan
        else:
            recent_same_direction_speed_mps = math.nan
    else:
        recent_same_direction_pace_ratio = math.nan
        recent_same_direction_speed_mps = math.nan

    previous_error_age_s = (
        float(max(0, query_ts - previous_to_ts)) if previous_to_ts is not None else math.nan
    )
    if previous_stop_ts is not None:
        gap = query_ts - previous_stop_ts
        headway_s = float(gap) if 0 < gap <= MAX_HEADWAY_S else math.nan
    else:
        headway_s = math.nan

    return CausalFeatures(
        remaining_stops=remaining_stops,
        remaining_distance_m=remaining_distance_m,
        stretch_distance_m=stretch_distance_m,
        scheduled_duration_s=float(current.scheduled_duration_s),
        ping_age_s=ping_age_s,
        from_observed=(
            1.0
            if current.from_observed
            and (current.from_available_at if current.from_available_at is not None else current.query_ts)
            < query_ts
            else 0.0
        ),
        recent_vehicle_progress_ratio=recent_vehicle_progress_ratio,
        recent_vehicle_speed_mps=recent_vehicle_speed_mps,
        last_stop_delay_s=last_stop_delay_s,
        last_stretch_error_s=last_stretch_error_s,
        previous_error_s=float(previous_error_s),
        previous_error_age_s=previous_error_age_s,
        previous_observed=1.0 if previous_observed else 0.0,
        recent_same_direction_speed_mps=recent_same_direction_speed_mps,
        recent_same_direction_pace_ratio=recent_same_direction_pace_ratio,
        headway_s=headway_s,
        hour=hour,
        minute_of_day=minute_of_day,
        tod_sin=math.sin(angle),
        tod_cos=math.cos(angle),
        day_of_week=dow,
        day_type=day_type,
        coarse_day_type=coarse_day_type,
        is_weekend=is_weekend,
        route_id=current.route_id,
        from_sequence=current.from_sequence,
    )


def extract_causal_features_for_query(
    *,
    current: StretchError,
    previous: StretchError,
    geometry: RouteGeometry | None = None,
    same_run_passages: Sequence[PassageRow] = (),
    same_run_stretches: Sequence[StretchError] = (),
    other_run_passages: Sequence[PassageRow] = (),
    other_run_stretches: Sequence[StretchError] = (),
) -> CausalFeatures:
    """Build CausalFeatures for one query; raises CausalityViolationError if future data appears."""
    validate_causal_observations(
        query_ts=current.query_ts,
        run_key=current.run_key,
        from_sequence=current.from_sequence,
        same_run_passages=same_run_passages,
        same_run_stretches=same_run_stretches,
        other_run_passages=other_run_passages,
        other_run_stretches=[previous, *other_run_stretches],
    )

    window_start = current.query_ts - RECENT_WINDOW_S
    recent_other = [
        s
        for s in other_run_stretches
        if s.day == current.day
        and s.route_id == current.route_id
        and s.direction == current.direction
        and s.to_ts >= window_start
    ]
    same_stop_passages = [
        p.ts
        for p in other_run_passages
        if p.day == current.day
        and p.route_id == current.route_id
        and p.direction == current.direction
        and p.stop_id == current.from_stop_id
    ]
    if not same_stop_passages:
        same_stop_passages = [
            s.query_ts
            for s in other_run_stretches
            if s.day == current.day
            and s.route_id == current.route_id
            and s.direction == current.direction
            and s.from_stop_id == current.from_stop_id
        ]
    previous_stop_ts = max(same_stop_passages, default=None)

    return _compute_causal_features(
        current=current,
        previous_error_s=float(previous.error_s),
        previous_to_ts=previous.to_ts,
        previous_observed=previous.target_observed,
        geometry=geometry,
        same_run_passages=same_run_passages,
        same_run_stretches=same_run_stretches,
        recent_other_stretches=recent_other,
        previous_stop_ts=previous_stop_ts,
    )


def build_replay_queries(
    stretches: Iterable[StretchError],
    passages: Iterable[PassageRow] | None = None,
) -> list[ReplayQuery]:
    """Attach the latest fully visible previous run error and strictly causal features."""
    stretch_list = list(stretches)
    passage_list = list(passages) if passages is not None else []
    geometries = build_route_geometry(passage_list) if passage_list else {}

    run_passages: dict[tuple[str, int, str, str], list[PassageRow]] = defaultdict(list)
    stop_arrivals: dict[
        tuple[str, int, str, str],
        list[tuple[int, tuple[str, int, str, str], int]],
    ] = defaultdict(list)
    for p in passage_list:
        run_passages[p.run_key].append(p)
        stop_arrivals[(p.day, p.route_id, p.direction, p.stop_id)].append(
            (p.ts, p.run_key, p.source_available_at)
        )
    for p_list in run_passages.values():
        p_list.sort(key=lambda p: p.stop_sequence)

    run_stretches: dict[tuple[str, int, str, str], list[StretchError]] = defaultdict(list)
    dir_stretches: dict[tuple[str, int, str], list[StretchError]] = defaultdict(list)
    groups: dict[tuple[str, int, str, str, str], list[StretchError]] = defaultdict(list)
    for stretch in stretch_list:
        run_stretches[stretch.run_key].append(stretch)
        dir_stretches[(stretch.day, stretch.route_id, stretch.direction)].append(stretch)
        groups[(stretch.day, *stretch.stretch_key)].append(stretch)
        if not passage_list:
            stop_arrivals[(stretch.day, stretch.route_id, stretch.direction, stretch.from_stop_id)].append(
                (
                    stretch.query_ts,
                    stretch.run_key,
                    stretch.query_ts if stretch.from_available_at is None else stretch.from_available_at,
                )
            )

    for r_list in run_stretches.values():
        r_list.sort(key=lambda s: s.from_sequence)

    dir_to_ts: dict[tuple[str, int, str], list[int]] = {}
    for key, s_list in dir_stretches.items():
        s_list.sort(key=lambda s: s.to_ts)
        dir_to_ts[key] = [s.to_ts for s in s_list]

    stop_ts_index: dict[tuple[str, int, str, str], list[int]] = {}
    for key, arr in stop_arrivals.items():
        arr.sort(key=lambda item: item[0])
        stop_ts_index[key] = [item[0] for item in arr]

    queries: list[ReplayQuery] = []
    for group in groups.values():
        ordered = sorted(
            group,
            key=lambda row: (row.query_ts, row.vehicle_id, row.run_id),
        )
        for current in ordered:
            visible = (
                candidate
                for candidate in ordered
                if candidate.run_key != current.run_key
                and candidate.query_ts < current.query_ts
                and candidate.to_ts < current.query_ts
                and candidate.available_at < current.query_ts
            )
            previous = max(
                visible,
                key=lambda row: (row.query_ts, row.vehicle_id, row.run_id),
                default=None,
            )
            if previous is None:
                continue

            q_ts = current.query_ts
            same_p = [
                p
                for p in run_passages.get(current.run_key, ())
                if (
                    p.stop_sequence <= current.from_sequence
                    and p.ts < q_ts
                    and p.source_available_at < q_ts
                )
            ]
            same_s = [
                s
                for s in run_stretches.get(current.run_key, ())
                if (
                    s.to_sequence <= current.from_sequence
                    and s.to_ts < q_ts
                    and s.query_ts < q_ts
                    and s.available_at < q_ts
                )
            ]
            dir_key = (current.day, current.route_id, current.direction)
            all_dir = dir_stretches.get(dir_key, ())
            ts_list = dir_to_ts.get(dir_key, ())
            hi = bisect_left(ts_list, q_ts)
            lo = bisect_left(ts_list, q_ts - RECENT_WINDOW_S, 0, hi)
            recent_other = [
                s
                for s in all_dir[lo:hi]
                if (
                    s.run_key != current.run_key
                    and s.query_ts < q_ts
                    and s.to_ts < q_ts
                    and s.available_at < q_ts
                )
            ]
            stop_key = (current.day, current.route_id, current.direction, current.from_stop_id)
            arr_list = stop_arrivals.get(stop_key, ())
            arr_ts = stop_ts_index.get(stop_key, ())
            pos = bisect_left(arr_ts, q_ts) - 1
            previous_stop_ts: int | None = None
            while pos >= 0:
                cand_ts, cand_run, cand_available_at = arr_list[pos]
                if cand_run != current.run_key and cand_available_at < q_ts:
                    previous_stop_ts = cand_ts
                    break
                pos -= 1

            feats = _compute_causal_features(
                current=current,
                previous_error_s=float(previous.error_s),
                previous_to_ts=previous.to_ts,
                previous_observed=previous.target_observed,
                geometry=geometries.get((current.route_id, current.direction)),
                same_run_passages=same_p,
                same_run_stretches=same_s,
                recent_other_stretches=recent_other,
                previous_stop_ts=previous_stop_ts,
            )
            queries.append(
                ReplayQuery(
                    stretch=current,
                    previous_error_s=previous.error_s,
                    previous_run_id=previous.run_id,
                    previous_to_ts=previous.to_ts,
                    previous_available_at=previous.available_at,
                    previous_observed=previous.target_observed,
                    features=feats,
                )
            )
    return sorted(
        queries,
        key=lambda query: (
            query.stretch.day,
            query.stretch.route_id,
            query.stretch.query_ts,
            query.stretch.vehicle_id,
            query.stretch.run_id,
            query.stretch.from_sequence,
        ),
    )
