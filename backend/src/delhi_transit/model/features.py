"""Stretch error tables and strictly time-ordered causal replay features."""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path

import duckdb

from delhi_transit.core.config import IST
from delhi_transit.model.causal_core import (
    RECENT_WINDOW_S,
    CausalFeatures,
    RouteGeometry,
    assemble_features,
    build_route_geometry,
    coarse_day_type,
    headway_s,
    pace_and_speed,
    schedule_delay_s,
    vehicle_progress,
)


class CausalityViolationError(ValueError):
    """Raised when feature construction receives non-causal future observations."""



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
    def coarse_day_type(self) -> str:
        return coarse_day_type(self.day_of_week)

    @property
    def target_observed(self) -> bool:
        return self.from_observed and self.to_observed


@dataclass(frozen=True)
class ReplayQuery:
    stretch: StretchError
    previous_error_s: int
    previous_run_id: str
    previous_to_ts: int | None = None
    previous_available_at: int | None = None
    previous_observed: bool = False
    features: CausalFeatures | None = None


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
    from_available_at = (
        current.query_ts if current.from_available_at is None else current.from_available_at
    )
    from_visible = current.from_observed and from_available_at < query_ts
    observed: dict[str, float] = {
        "from_observed": 1.0 if from_visible else 0.0,
        "previous_error_s": float(previous_error_s),
        "previous_observed": 1.0 if previous_observed else 0.0,
    }

    observed_same_run = [p for p in same_run_passages if p.observed]
    if observed_same_run:
        last_obs = max(observed_same_run, key=lambda p: (p.stop_sequence, p.ts))
        observed["ping_age_s"] = float(max(0, query_ts - last_obs.ts))
        observed["last_stop_delay_s"] = schedule_delay_s(last_obs.effective_schedule_error_s)
    elif from_visible:
        observed["ping_age_s"] = 0.0

    if same_run_stretches:
        ordered_prior = sorted(same_run_stretches, key=lambda s: s.from_sequence)
        obs_prior = [s for s in ordered_prior[-5:] if s.target_observed]
        observed["last_stretch_error_s"] = float((obs_prior or ordered_prior)[-1].error_s)
        (
            observed["recent_vehicle_progress_ratio"],
            observed["recent_vehicle_speed_mps"],
        ) = vehicle_progress(ordered_prior[-3:], geometry)

    other_pool = [s for s in recent_other_stretches if s.target_observed] or recent_other_stretches
    (
        observed["recent_same_direction_pace_ratio"],
        observed["recent_same_direction_speed_mps"],
    ) = pace_and_speed(other_pool, geometry)

    if previous_to_ts is not None:
        observed["previous_error_age_s"] = float(max(0, query_ts - previous_to_ts))
    if previous_stop_ts is not None:
        observed["headway_s"] = headway_s(query_ts - previous_stop_ts)

    return assemble_features(
        when=datetime.fromtimestamp(query_ts, IST),
        geometry=geometry,
        route_id=current.route_id,
        from_sequence=current.from_sequence,
        to_sequence=current.to_sequence,
        scheduled_duration_s=current.scheduled_duration_s,
        **observed,
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
