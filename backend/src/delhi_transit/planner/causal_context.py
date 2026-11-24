"""Timing records and the strictly causal per-stretch features a planner query can see."""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

from delhi_transit.core.config import IST
from delhi_transit.model.features import (
    CausalFeatures,
    PassageRow,
    RouteGeometry,
    StretchError,
    day_type_for_timestamp,
)


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
    as_of_ts = int((as_of or depart).timestamp())
    hour = depart.hour
    minute_of_day = hour * 60 + depart.minute
    angle = 2.0 * math.pi * minute_of_day / 1440.0
    dow, day_type, coarse_day_type, is_weekend = day_type_for_timestamp(int(depart.timestamp()))

    if geom is not None:
        remaining_stops = max(0, geom.max_sequence - from_seq)
        stretch_distance_m = geom.stretch_distances_m.get((from_seq, to_seq), math.nan)
        remaining_distance_m = geom.remaining_distances_m.get(from_seq, math.nan)
    else:
        remaining_stops = 0
        stretch_distance_m = math.nan
        remaining_distance_m = math.nan

    previous_error_s = math.nan
    previous_error_age_s = math.nan
    previous_observed = 0.0
    recent_speed = math.nan
    recent_pace = math.nan
    headway_s = math.nan
    ping_age_s = math.nan
    from_observed = 0.0
    last_stop_delay_s = math.nan
    last_stretch_error_s = math.nan
    recent_veh_progress = math.nan
    recent_veh_speed = math.nan

    if mode == "replay":
        day_str = depart.date().isoformat()
        exact_visible = [r for r in visible(exact_records, as_of_ts) if r.stretch.day == day_str]
        obs_exact = [r for r in exact_visible if r.stretch.target_observed]
        prev_pool = obs_exact or exact_visible
        if prev_pool:
            prev = max(prev_pool, key=lambda r: (r.stretch.query_ts, r.stretch.run_id))
            previous_error_s = float(prev.stretch.error_s)
            previous_error_age_s = float(max(0, as_of_ts - prev.stretch.available_at))
            previous_observed = 1.0 if prev.stretch.target_observed else 0.0

        recent_30m = [r for r in same_day_route if r.stretch.available_at >= as_of_ts - 1800]
        route_pool = recent_30m or same_day_route
        if route_pool:
            paces = [
                max(0.2, min(5.0, r.ratio))
                for r in route_pool
                if r.stretch.scheduled_duration_s > 0
            ]
            if paces:
                recent_pace = float(median(paces))
            if geom is not None:
                speeds = []
                for r in route_pool:
                    d = geom.stretch_distances_m.get((r.stretch.from_sequence, r.stretch.to_sequence))
                    if d is not None and d > 0 and r.stretch.actual_duration_s > 0:
                        speeds.append(max(0.1, min(30.0, d / r.stretch.actual_duration_s)))
                if speeds:
                    recent_speed = float(median(speeds))

        visible_stop_rows = [
            p
            for p in stop_rows
            if p.day == day_str and p.ts < as_of_ts and p.source_available_at < as_of_ts
        ]
        if visible_stop_rows:
            last_p = visible_stop_rows[-1]
            gap = as_of_ts - last_p.source_available_at
            if 0 < gap <= 14_400:
                headway_s = float(gap)
            if gap <= 1800:
                ping_age_s = float(gap)
                from_observed = 1.0 if last_p.observed else 0.0
                raw_delay = last_p.effective_schedule_error_s
                if abs(raw_delay) <= 21_600:
                    last_stop_delay_s = float(raw_delay)
                run_prior = [
                    r.stretch
                    for r in same_day_route
                    if r.stretch.run_id == last_p.run_id
                    and r.stretch.to_sequence <= from_seq
                    and r.stretch.to_ts <= as_of_ts
                    and r.stretch.available_at < as_of_ts
                ]
                if run_prior:
                    run_prior.sort(key=lambda s: s.from_sequence)
                    last_stretch_error_s = float(run_prior[-1].error_s)
                    recent_runs = run_prior[-3:]
                    sched_sum = sum(s.scheduled_duration_s for s in recent_runs)
                    act_sum = sum(s.actual_duration_s for s in recent_runs)
                    if sched_sum > 0:
                        recent_veh_progress = max(0.2, min(5.0, act_sum / sched_sum))
                    if geom is not None and act_sum > 0:
                        dists = [
                            (geom.stretch_distances_m.get((s.from_sequence, s.to_sequence)), s.actual_duration_s)
                            for s in recent_runs
                        ]
                        valid = [(d, t) for d, t in dists if d is not None and d > 0 and t > 0]
                        if valid:
                            recent_veh_speed = max(
                                0.1,
                                min(30.0, sum(d for d, _ in valid) / sum(t for _, t in valid)),
                            )
    elif mode == "live" and live_stat is not None:
        live_fresh = int(live_stat.get("fresh_vehicle_count", 0))
        median_speed = float(live_stat.get("median_speed_mps", 0.0))
        if median_speed > 0.5:
            recent_speed = max(0.1, min(30.0, median_speed))
            recent_pace = max(0.5, min(2.5, 5.5 / median_speed))
        if live_fresh >= 1:
            headway_s = max(120.0, min(1800.0, 3600.0 / max(1, live_fresh)))
            ping_age_s = 0.0
            from_observed = 1.0

    return CausalFeatures(
        remaining_stops=remaining_stops,
        remaining_distance_m=remaining_distance_m,
        stretch_distance_m=stretch_distance_m,
        scheduled_duration_s=float(scheduled_s),
        ping_age_s=ping_age_s,
        from_observed=from_observed,
        recent_vehicle_progress_ratio=recent_veh_progress,
        recent_vehicle_speed_mps=recent_veh_speed,
        last_stop_delay_s=last_stop_delay_s,
        last_stretch_error_s=last_stretch_error_s,
        previous_error_s=previous_error_s,
        previous_error_age_s=previous_error_age_s,
        previous_observed=previous_observed,
        recent_same_direction_speed_mps=recent_speed,
        recent_same_direction_pace_ratio=recent_pace,
        headway_s=headway_s,
        hour=hour,
        minute_of_day=minute_of_day,
        tod_sin=math.sin(angle),
        tod_cos=math.cos(angle),
        day_of_week=dow,
        day_type=day_type,
        coarse_day_type=coarse_day_type,
        is_weekend=is_weekend,
        route_id=route_id,
        from_sequence=from_seq,
    )
