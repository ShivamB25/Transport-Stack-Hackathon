"""Timing records and the strictly causal per-stretch features a planner query can see."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from delhi_transit.core.config import IST
from delhi_transit.model.causal_core import (
    RECENT_WINDOW_S,
    CausalFeatures,
    RouteGeometry,
    assemble_features,
    headway_s,
    pace_and_speed,
    schedule_delay_s,
    vehicle_progress,
)
from delhi_transit.model.features import PassageRow, StretchError


@dataclass(frozen=True)
class TimingRecord:
    stretch: StretchError

    @property
    def hour(self) -> int:
        return datetime.fromtimestamp(self.stretch.query_ts, IST).hour

    @property
    def ratio(self) -> float:
        scheduled = self.stretch.scheduled_duration_s
        return self.stretch.actual_duration_s / scheduled if scheduled > 0 else 1.0


def visible(records: Sequence[TimingRecord], as_of_ts: int) -> list[TimingRecord]:
    """Stretches queried, completed and knowable before the cutoff."""
    return [
        record
        for record in records
        if record.stretch.query_ts < as_of_ts
        and record.stretch.to_ts <= as_of_ts
        and record.stretch.available_at < as_of_ts
    ]


def _replay_observations(
    *,
    geom: RouteGeometry | None,
    exact_records: Sequence[TimingRecord],
    stop_rows: Sequence[PassageRow],
    from_seq: int,
    day_str: str,
    as_of_ts: int,
    same_day_route: list[TimingRecord],
) -> dict[str, float]:
    observed: dict[str, float] = {}
    exact_visible = [r for r in visible(exact_records, as_of_ts) if r.stretch.day == day_str]
    prev_pool = [r for r in exact_visible if r.stretch.target_observed] or exact_visible
    if prev_pool:
        prev = max(prev_pool, key=lambda r: (r.stretch.query_ts, r.stretch.run_id)).stretch
        observed["previous_error_s"] = float(prev.error_s)
        observed["previous_error_age_s"] = float(max(0, as_of_ts - prev.available_at))
        observed["previous_observed"] = 1.0 if prev.target_observed else 0.0

    recent = [r for r in same_day_route if r.stretch.available_at >= as_of_ts - RECENT_WINDOW_S]
    (
        observed["recent_same_direction_pace_ratio"],
        observed["recent_same_direction_speed_mps"],
    ) = pace_and_speed([r.stretch for r in recent or same_day_route], geom)

    visible_stop_rows = [
        p
        for p in stop_rows
        if p.day == day_str and p.ts < as_of_ts and p.source_available_at < as_of_ts
    ]
    if not visible_stop_rows:
        return observed
    last_p = visible_stop_rows[-1]
    gap = as_of_ts - last_p.source_available_at
    observed["headway_s"] = headway_s(gap)
    if gap > RECENT_WINDOW_S:
        return observed
    observed["ping_age_s"] = float(gap)
    observed["from_observed"] = 1.0 if last_p.observed else 0.0
    observed["last_stop_delay_s"] = schedule_delay_s(last_p.effective_schedule_error_s)
    run_prior = sorted(
        (
            r.stretch
            for r in same_day_route
            if r.stretch.run_id == last_p.run_id
            and r.stretch.to_sequence <= from_seq
            and r.stretch.to_ts <= as_of_ts
            and r.stretch.available_at < as_of_ts
        ),
        key=lambda s: s.from_sequence,
    )
    if run_prior:
        observed["last_stretch_error_s"] = float(run_prior[-1].error_s)
        (
            observed["recent_vehicle_progress_ratio"],
            observed["recent_vehicle_speed_mps"],
        ) = vehicle_progress(run_prior[-3:], geom)
    return observed


def _live_observations(live_stat: dict[str, Any]) -> dict[str, float]:
    observed: dict[str, float] = {}
    median_speed = float(live_stat.get("median_speed_mps", 0.0))
    if median_speed > 0.5:
        observed["recent_same_direction_speed_mps"] = max(0.1, min(30.0, median_speed))
        observed["recent_same_direction_pace_ratio"] = max(0.5, min(2.5, 5.5 / median_speed))
    live_fresh = int(live_stat.get("fresh_vehicle_count", 0))
    if live_fresh >= 1:
        observed["headway_s"] = max(120.0, min(1800.0, 3600.0 / max(1, live_fresh)))
        observed["ping_age_s"] = 0.0
        observed["from_observed"] = 1.0
    return observed


def causal_features_for_stretch(
    *,
    geom: RouteGeometry | None,
    exact_records: Sequence[TimingRecord],
    stop_rows: Sequence[PassageRow],
    route_id: int,
    from_seq: int,
    to_seq: int,
    scheduled_s: float,
    depart: datetime,
    as_of: datetime | None,
    mode: str,
    same_day_route: list[TimingRecord],
    live_stat: dict[str, Any] | None,
) -> CausalFeatures:
    """Features for one stretch from replay passages before the cutoff, or from live route stats.

    `exact_records` are the timing records of this exact stretch and `stop_rows` the passages at
    its from-stop (sorted by time); both are filtered to the cutoff here.
    """
    depart = depart.astimezone(IST)
    observed: dict[str, float] = {}
    if mode == "replay":
        observed = _replay_observations(
            geom=geom,
            exact_records=exact_records,
            stop_rows=stop_rows,
            from_seq=from_seq,
            day_str=depart.date().isoformat(),
            as_of_ts=int((as_of or depart).timestamp()),
            same_day_route=same_day_route,
        )
    elif mode == "live" and live_stat is not None:
        observed = _live_observations(live_stat)
    return assemble_features(
        when=depart,
        geometry=geom,
        route_id=route_id,
        from_sequence=from_seq,
        to_sequence=to_seq,
        scheduled_duration_s=scheduled_s,
        **observed,
    )
