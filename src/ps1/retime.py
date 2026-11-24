"""Model re-timing and joint door-to-door replay scenarios."""
from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

from ps1.config import IST
from ps1.features import (
    CausalFeatures,
    PassageRow,
    RouteGeometry,
    StretchError,
    build_route_geometry,
    build_stretch_errors,
    day_type_for_timestamp,
    load_passages,
)
from ps1.live_poller import (
    effective_entity_max_age_s,
    read_live_route_stats,
    refresh_route_stat,
)
from ps1.model import LoadedModelArtifact, Prediction, load_model_artifact
from ps1.selection import SHIP_STRATUM

SCENARIOS = 192


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def scenario_windows(scenario_leg_durations: list[list[float]]) -> dict[str, Any]:
    """Summarize complete joint scenarios; leg quantiles are never added."""
    if not scenario_leg_durations:
        raise ValueError("at least one joint scenario is required")
    width = len(scenario_leg_durations[0])
    if any(len(scenario) != width for scenario in scenario_leg_durations):
        raise ValueError("joint scenarios must have identical legs")
    totals = [sum(scenario) for scenario in scenario_leg_durations]
    legs = [
        {
            "window_p10_s": percentile([scenario[index] for scenario in scenario_leg_durations], 0.1),
            "window_p90_s": percentile([scenario[index] for scenario in scenario_leg_durations], 0.9),
            "median_s": percentile([scenario[index] for scenario in scenario_leg_durations], 0.5),
        }
        for index in range(width)
    ]
    return {
        "total_duration_s": percentile(totals, 0.5),
        "total_window_p10_s": percentile(totals, 0.1),
        "total_window_p90_s": percentile(totals, 0.9),
        "legs": legs,
    }


@dataclass(frozen=True)
class TimingEstimate:
    duration_s: float
    p10_s: float
    p90_s: float
    source: str
    observed_coverage: float
    timing_source: str | None = None
    observation_count: int = 0
    coverage_note: str | None = None


def _with_replay_delay(estimate: TimingEstimate, delay_s: float) -> TimingEstimate:
    """Shift a ride estimate by an injected replay delay; the interval width is unchanged."""
    if not delay_s:
        return estimate
    return replace(
        estimate,
        duration_s=estimate.duration_s + delay_s,
        p10_s=estimate.p10_s + delay_s,
        p90_s=estimate.p90_s + delay_s,
        coverage_note=f"{estimate.coverage_note}; +{int(delay_s)}s replay delay injected",
    )


@dataclass(frozen=True)
class WaitEstimate:
    duration_s: float
    p90_s: float
    headway_s: float
    source: str
    observed_coverage: float
    timing_source: str | None = None
    observation_count: int = 0
    coverage_note: str | None = None
    departure_delay_s: float = 0.0


def _default_timing_source(source: str) -> str:
    return {
        "live": "model_live",
        "replay": "model_replay",
        "historical": "historical_fallback",
        "timetable": "timetable_fallback",
    }.get(source, source)


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


class RetimeModel:
    """Selected-rung adapter with causal historical and timetable fallbacks."""

    def __init__(
        self,
        data_dir: Path,
        selection_path: Path | None = None,
        model_dir: Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        root = Path(__file__).resolve().parents[2]
        self.selection_path = selection_path or root / "reports" / "selection.json"
        self.model_dir = Path(model_dir) if model_dir is not None else self.data_dir / "model"
        self.selected_rung = "1"
        if self.selection_path.exists():
            selection = json.loads(self.selection_path.read_text())
            if selection.get("stratum") != SHIP_STRATUM:
                raise ValueError(
                    f"refusing {self.selection_path}: stratum {selection.get('stratum')!r} is not "
                    f"{SHIP_STRATUM!r}"
                )
            self.selected_rung = str(selection["selected_rung"])
        self.records: list[TimingRecord] = []
        self.exact: dict[tuple[int, str, str], list[TimingRecord]] = defaultdict(list)
        self.route_hour: dict[tuple[int, int], list[TimingRecord]] = defaultdict(list)
        self.global_hour: dict[int, list[TimingRecord]] = defaultdict(list)
        self.stop_events: dict[tuple[int, str], list[PassageRow]] = defaultdict(list)
        self.route_geometries: dict[tuple[int, str], RouteGeometry] = {}
        self.route_patterns: dict[int, tuple[str, list[tuple[int, str]]]] = {}
        self.stretch_typical_scheduled: dict[tuple[int, str, str], float] = {}
        self._live_stats_mtime: float | None = None
        self._live_stats_cache: dict[int, dict[str, Any]] = {}
        self.stale_threshold_s: float | None = None
        self._loaded_passage_files: list[Path] = []
        loaded = load_model_artifact(self.model_dir)
        self._artifact: LoadedModelArtifact | None = (
            loaded
            if loaded is not None and loaded.rung == self.selected_rung and self.selected_rung != "1"
            else None
        )
        self._load()

    def _live_route_stat(self, route_id: int) -> dict[str, Any] | None:
        stats_path = self.data_dir / "live" / "route_stats.json"
        if not stats_path.exists():
            return None
        try:
            mtime = stats_path.stat().st_mtime
        except OSError:
            return None
        if self._live_stats_mtime != mtime:
            self._live_stats_cache = read_live_route_stats(self.data_dir)
            self._live_stats_mtime = mtime
        stat = self._live_stats_cache.get(route_id)
        if stat is None:
            return None
        return refresh_route_stat(
            stat, now=time.time(), max_age_s=effective_entity_max_age_s(self.stale_threshold_s)
        )

    def replay_reference_info(self, depart: datetime) -> dict[str, Any]:
        """Summarize only reconstructed observations actually loaded before the cutoff."""
        depart = depart.astimezone(IST)
        depart_ts = int(depart.timestamp())
        day_str = depart.date().isoformat()
        visible = self._visible(self.records, depart_ts)
        same_day = [r for r in visible if r.stretch.day == day_str]
        observed_same_day = [r for r in same_day if r.stretch.target_observed]
        candidate_pool = observed_same_day or same_day or visible
        latest_record = max(candidate_pool, key=lambda r: r.stretch.available_at, default=None)
        latest_stop = max(
            (
                row
                for rows in self.stop_events.values()
                for row in rows
                if row.ts < depart_ts and row.source_available_at < depart_ts
            ),
            key=lambda row: row.source_available_at,
            default=None,
        )
        candidates = [
            candidate
            for candidate in (
                (latest_record.stretch.available_at, latest_record.stretch.day)
                if latest_record is not None
                else None,
                (latest_stop.source_available_at, latest_stop.day) if latest_stop is not None else None,
            )
            if candidate is not None
        ]
        loaded_files = [str(path) for path in self._loaded_passage_files]
        base = {
            "visible_observations": len(visible),
            "same_day_observations": len(same_day),
            "observed_same_day_stretches": len(observed_same_day),
            "loaded_passage_files": loaded_files,
        }
        if not candidates:
            return {
                **base,
                "reference_timestamp": None,
                "feed_timestamp": None,
                "feed_age_seconds": None,
                "replay_data_available": False,
                "same_day_data_available": False,
                "fixture_path": None,
            }
        latest_ts, latest_day = max(candidates)
        expected_name = f"day={latest_day}.parquet"
        fixture_path = next((path for path in loaded_files if Path(path).name == expected_name), None)
        ref_iso = datetime.fromtimestamp(latest_ts, IST).isoformat()
        return {
            **base,
            "reference_timestamp": ref_iso,
            "feed_timestamp": ref_iso,
            "feed_age_seconds": round(max(0.0, float(depart_ts - latest_ts)), 2),
            "replay_data_available": True,
            "same_day_data_available": bool(same_day),
            "fixture_path": fixture_path,
        }

    def _load(self) -> None:
        files = sorted((self.data_dir / "passages").glob("*.parquet"))
        if not files:
            return
        self._loaded_passage_files = files
        passages = load_passages(self.data_dir / "passages" / "*.parquet")
        self.route_geometries = build_route_geometry(passages)
        route_stops: dict[int, tuple[str, dict[int, str]]] = {}
        for passage in passages:
            _, seq_map = route_stops.setdefault(passage.route_id, (passage.direction, {}))
            seq_map.setdefault(passage.stop_sequence, passage.stop_id)
        self.route_patterns = {
            rid: (direction, sorted(seq_map.items()))
            for rid, (direction, seq_map) in route_stops.items()
        }
        self.index_stop_events(passages)
        self.index_records([TimingRecord(row) for row in build_stretch_errors(passages)])

    def index_records(self, records: list[TimingRecord]) -> None:
        self.records.extend(records)
        sched_samples: dict[tuple[int, str, str], list[int]] = defaultdict(list)
        for record in records:
            stretch = record.stretch
            key = (stretch.route_id, stretch.from_stop_id, stretch.to_stop_id)
            self.exact[key].append(record)
            sched_samples[key].append(stretch.scheduled_duration_s)
            if stretch.target_observed:
                self.route_hour[(stretch.route_id, record.hour)].append(record)
                self.global_hour[record.hour].append(record)
        self.stretch_typical_scheduled.update(
            {key: float(median(vals)) for key, vals in sched_samples.items()}
        )

    def index_stop_events(self, passages: Iterable[PassageRow]) -> None:
        for passage in passages:
            self.stop_events[(passage.route_id, passage.stop_id)].append(passage)
        for group in self.stop_events.values():
            group.sort(key=lambda row: row.ts)

    @staticmethod
    def _visible(records: list[TimingRecord], as_of_ts: int) -> list[TimingRecord]:
        return [
            record
            for record in records
            if record.stretch.query_ts < as_of_ts
            and record.stretch.to_ts <= as_of_ts
            and record.stretch.available_at < as_of_ts
        ]

    @staticmethod
    def _coverage(records: list[TimingRecord]) -> float:
        if not records:
            return 0.0
        return round(sum(record.stretch.target_observed for record in records) / len(records), 4)

    @staticmethod
    def _ratio_interval(records: list[TimingRecord], duration: float) -> tuple[float, float]:
        ratios = [max(0.35, min(3.0, record.ratio)) for record in records]
        if len(ratios) >= 4:
            low = percentile(ratios, 0.1) * duration
            high = percentile(ratios, 0.9) * duration
        else:
            low = duration - max(45.0, duration * 0.12)
            high = duration + max(90.0, duration * 0.25)
        return max(1.0, low), max(duration + 1.0, high)

    @staticmethod
    def _correlated_leg_interval(
        duration: float,
        predictions: list[Prediction],
        rho: float = 0.35,
    ) -> tuple[float, float]:
        """Combine calibrated per-stretch half-widths within one ride leg using intra-leg correlation."""
        lows = [
            max(1.0, float(p.p50_s) - float(p.p10_s))
            for p in predictions
            if p.p10_s is not None and p.p50_s is not None
        ]
        highs = [
            max(1.0, float(p.p90_s) - float(p.p50_s))
            for p in predictions
            if p.p90_s is not None and p.p50_s is not None
        ]

        def combine(widths: list[float]) -> float:
            if not widths:
                return max(45.0, duration * 0.15)
            sq = sum(w * w for w in widths)
            cross = (sum(widths) ** 2) - sq
            return math.sqrt(max(1.0, sq + rho * cross))

        w_lo = combine(lows)
        w_hi = combine(highs)
        p10 = max(1.0, min(duration, duration - w_lo))
        p90 = max(duration + 1.0, duration + w_hi)
        return p10, p90

    def _causal_features_for_stretch(
        self,
        *,
        route_id: int,
        direction: str,
        from_seq: int,
        to_seq: int,
        from_stop: str,
        to_stop: str,
        scheduled_s: float,
        depart: datetime,
        as_of: datetime | None,
        mode: str,
        same_day_route: list[TimingRecord],
        live_stat: dict[str, Any] | None,
    ) -> CausalFeatures:
        depart = depart.astimezone(IST)
        as_of_ts = int((as_of or depart).timestamp())
        hour = depart.hour
        minute_of_day = hour * 60 + depart.minute
        angle = 2.0 * math.pi * minute_of_day / 1440.0
        dow, day_type, coarse_day_type, is_weekend = day_type_for_timestamp(int(depart.timestamp()))

        geom = self.route_geometries.get((route_id, direction))
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
            exact_visible = [
                r
                for r in self._visible(self.exact.get((route_id, from_stop, to_stop), []), as_of_ts)
                if r.stretch.day == day_str
            ]
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
                        d = geom.stretch_distances_m.get(
                            (r.stretch.from_sequence, r.stretch.to_sequence)
                        )
                        if d is not None and d > 0 and r.stretch.actual_duration_s > 0:
                            speeds.append(max(0.1, min(30.0, d / r.stretch.actual_duration_s)))
                    if speeds:
                        recent_speed = float(median(speeds))

            stop_rows = [
                p
                for p in self.stop_events.get((route_id, from_stop), [])
                if p.day == day_str
                and p.ts < as_of_ts
                and p.source_available_at < as_of_ts
            ]
            if stop_rows:
                last_p = stop_rows[-1]
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

    def _predict_covered_route_with_artifact(
        self,
        artifact: LoadedModelArtifact,
        route_id: int,
        from_stop: str,
        to_stop: str,
        scheduled: float,
        depart: datetime,
        as_of: datetime | None,
        mode: str,
        same_day_route: list[TimingRecord],
        hist_reference: list[TimingRecord],
        live_stat: dict[str, Any] | None,
        replay_delay_s: float,
    ) -> TimingEstimate | None:
        """Time a leg on a covered route using the loaded selected-rung artifact."""
        pattern_info = self.route_patterns.get(route_id)
        if pattern_info is None:
            return None
        direction, ordered_stops = pattern_info
        stop_ids = [sid for _, sid in ordered_stops]
        live_fresh = int(live_stat.get("fresh_vehicle_count", 0)) if live_stat else 0

        stretches: list[tuple[int, int, str, str, float]] = []
        if from_stop in stop_ids and to_stop in stop_ids:
            from_idx = stop_ids.index(from_stop)
            to_idx = stop_ids.index(to_stop)
            if from_idx < to_idx:
                raw_scheds = [
                    self.stretch_typical_scheduled.get((route_id, ordered_stops[i][1], ordered_stops[i + 1][1]), 60.0)
                    for i in range(from_idx, to_idx)
                ]
                total_raw = sum(raw_scheds) or float(len(raw_scheds))
                scale = scheduled / total_raw
                for offset, i in enumerate(range(from_idx, to_idx)):
                    seq_a, sid_a = ordered_stops[i]
                    seq_b, sid_b = ordered_stops[i + 1]
                    stretches.append((seq_a, seq_b, sid_a, sid_b, max(1.0, raw_scheds[offset] * scale)))

        items: list[tuple[tuple[int, str, str, str], CausalFeatures]] = []
        pattern_matched = bool(stretches)
        if pattern_matched:
            for seq_a, seq_b, sid_a, sid_b, stretch_sched in stretches:
                feat = self._causal_features_for_stretch(
                    route_id=route_id,
                    direction=direction,
                    from_seq=seq_a,
                    to_seq=seq_b,
                    from_stop=sid_a,
                    to_stop=sid_b,
                    scheduled_s=stretch_sched,
                    depart=depart,
                    as_of=as_of,
                    mode=mode,
                    same_day_route=same_day_route,
                    live_stat=live_stat,
                )
                items.append(((route_id, direction, sid_a, sid_b), feat))
            preds = artifact.predict_batch_from_features(items)
            total_error = sum(p.error_s for p in preds)
            duration = max(max(1.0, scheduled * 0.25), scheduled + total_error)
        else:
            # The leg's stops are not on the reconstructed pattern, so project the ratio the
            # model predicts for the route's middle stretch onto the whole leg.
            if len(ordered_stops) < 2:
                return None
            mid = len(ordered_stops) // 2
            seq_a, sid_a = ordered_stops[mid - 1]
            seq_b, sid_b = ordered_stops[mid]
            mid_sched = max(1.0, self.stretch_typical_scheduled.get((route_id, sid_a, sid_b), 60.0))
            feat = self._causal_features_for_stretch(
                route_id=route_id,
                direction=direction,
                from_seq=seq_a,
                to_seq=seq_b,
                from_stop=sid_a,
                to_stop=sid_b,
                scheduled_s=mid_sched,
                depart=depart,
                as_of=as_of,
                mode=mode,
                same_day_route=same_day_route,
                live_stat=live_stat,
            )
            preds = artifact.predict_batch_from_features([((route_id, direction, sid_a, sid_b), feat)])
            ratio = max(0.35, min(3.0, (mid_sched + preds[0].error_s) / mid_sched))
            duration = max(1.0, scheduled * ratio)

        model_desc = (
            f"rung {artifact.rung} LightGBM quantile+conformal model"
            if artifact.rung == "4"
            else f"rung {artifact.rung} model"
        )
        stretch_desc = (
            f"{len(stretches)} pattern stretch(es)"
            if pattern_matched
            else "route-level pattern projection"
        )

        if mode == "replay":
            if same_day_route:
                source = "replay"
                timing_source = "model_replay"
                reference = same_day_route
                observation_count = len(same_day_route)
                coverage = self._coverage(same_day_route)
                coverage_note = (
                    f"causal October replay {model_desc} "
                    f"({len(same_day_route)} same-day observed stretches on route {route_id}; {stretch_desc})"
                )
            else:
                source = "historical"
                timing_source = "historical_fallback"
                reference = hist_reference
                observation_count = len(hist_reference)
                coverage = self._coverage(hist_reference) if hist_reference else 0.0
                coverage_note = (
                    f"sparse replay observations (0 same-day obs on route {route_id}); "
                    f"{model_desc} time-of-day/historical profile ({stretch_desc})"
                )
        elif live_fresh >= 1 and live_stat is not None:
            median_speed = float(live_stat.get("median_speed_mps", 0.0))
            source = "live"
            timing_source = "model_live"
            reference = hist_reference
            observation_count = live_fresh
            coverage = max(0.0, min(1.0, round(float(live_stat.get("observed_coverage", 1.0)), 4)))
            coverage_note = (
                f"live VehiclePositions ({live_fresh} fresh vehicles on route {route_id}, "
                f"median {median_speed:.1f} m/s) + {model_desc} ({stretch_desc})"
            )
        else:
            source = "historical"
            timing_source = "historical_fallback"
            reference = hist_reference
            observation_count = len(hist_reference)
            coverage = self._coverage(hist_reference) if hist_reference else 0.0
            coverage_note = (
                f"sparse live observations (0 fresh vehicles on route {route_id}); "
                f"{model_desc} time-of-day/historical fallback ({stretch_desc})"
            )

        has_quantiles = all(p.p10_s is not None and p.p50_s is not None and p.p90_s is not None for p in preds)
        if has_quantiles and pattern_matched:
            p10, p90 = self._correlated_leg_interval(duration, preds)
        else:
            p10, p90 = self._ratio_interval(reference, duration)

        estimate = TimingEstimate(
            duration,
            p10,
            p90,
            source,
            coverage,
            timing_source=timing_source,
            observation_count=observation_count,
            coverage_note=coverage_note,
        )
        return _with_replay_delay(estimate, replay_delay_s)

    def predict_ride(
        self,
        route_id: int,
        from_stop: str,
        to_stop: str,
        scheduled_duration_s: float,
        depart: datetime,
        mode: str,
        replay_delay_s: float = 0.0,
        as_of: datetime | None = None,
    ) -> TimingEstimate:
        scheduled = max(1.0, scheduled_duration_s)
        depart = depart.astimezone(IST)
        observation_cutoff = (as_of or depart).astimezone(IST)
        as_of_ts = int(observation_cutoff.timestamp())
        service_day = depart.date().isoformat()
        exact = self._visible(self.exact.get((route_id, from_stop, to_stop), []), as_of_ts)
        same_day = [
            record
            for record in exact
            if record.stretch.day == service_day and record.stretch.target_observed
        ]
        historical_exact = [record for record in exact if record.stretch.target_observed]
        same_hour = [record for record in historical_exact if record.hour == depart.hour]
        route_records = self._visible(self.route_hour.get((route_id, depart.hour), []), as_of_ts)
        same_day_route = [
            record
            for record in self.records
            if record.stretch.route_id == route_id
            and record.stretch.day == service_day
            and record.stretch.target_observed
            and record.stretch.query_ts < as_of_ts
            and record.stretch.to_ts <= as_of_ts
            and record.stretch.available_at < as_of_ts
            and record.stretch.available_at >= as_of_ts - 7200
        ]
        global_records = self._visible(self.global_hour.get(depart.hour, []), as_of_ts)

        if same_hour:
            hist_reference = same_hour
            hist_level = "stretch_hour"
        elif historical_exact:
            hist_reference = historical_exact
            hist_level = "stretch"
        elif route_records:
            hist_reference = route_records
            hist_level = "route_hour"
        elif global_records:
            hist_reference = global_records
            hist_level = "corridor_hour"
        else:
            hist_reference = []
            hist_level = "none"

        live_stat = self._live_route_stat(route_id) if mode == "live" else None
        live_fresh = int(live_stat.get("fresh_vehicle_count", 0)) if live_stat else 0

        artifact = self._artifact
        artifact_covers_route = artifact is not None and route_id in artifact.covered_routes
        artifact_eligible = artifact_covers_route and (
            mode != "replay" or artifact.is_eligible_as_of(as_of_ts)
        )
        if artifact_eligible:
            estimate = self._predict_covered_route_with_artifact(
                artifact=artifact,
                route_id=route_id,
                from_stop=from_stop,
                to_stop=to_stop,
                scheduled=scheduled,
                depart=depart,
                as_of=observation_cutoff,
                mode=mode,
                same_day_route=same_day_route,
                hist_reference=hist_reference,
                live_stat=live_stat,
                replay_delay_s=replay_delay_s,
            )
            if estimate is not None:
                return estimate

        # Uncovered and ineligible-artifact routes use the causal rung-1 fallback chain.
        if artifact_covers_route and not artifact_eligible:
            fallback_suffix = (
                f" (selected artifact ineligible as of {observation_cutoff.isoformat()}; "
                "causal rung 1 fallback)"
            )
        elif self.selected_rung != "1" and not artifact_eligible:
            fallback_suffix = f" (route {route_id} outside rung {self.selected_rung} model scope)"
        else:
            fallback_suffix = ""

        if mode == "replay" and same_day:
            previous = max(same_day, key=lambda record: record.stretch.query_ts)
            duration = max(1.0, scheduled + previous.stretch.error_s)
            source = "replay"
            timing_source = "model_replay"
            reference = exact or [previous]
            observation_count = len(same_day)
            coverage = self._coverage(reference)
            coverage_note = (
                f"causal October replay rung 1 persistence "
                f"({len(same_day)} same-day observed runs on route {route_id}){fallback_suffix}"
            )
        elif mode == "replay" and same_day_route:
            recent = sorted(same_day_route, key=lambda record: record.stretch.query_ts)[-20:]
            duration = max(1.0, scheduled * median(max(0.35, min(3.0, row.ratio)) for row in recent))
            source = "replay"
            timing_source = "model_replay"
            reference = recent
            observation_count = len(same_day_route)
            coverage = self._coverage(same_day_route)
            coverage_note = (
                f"causal October replay rung 1 route-level persistence "
                f"({len(same_day_route)} same-day route-{route_id} observed stretches){fallback_suffix}"
            )
        elif (
            mode == "live"
            and live_fresh >= 1
            and live_stat is not None
            and float(live_stat.get("median_speed_mps", 0.0)) > 0.5
        ):
            reference = hist_reference
            base_ratio = (
                median(max(0.35, min(3.0, row.ratio)) for row in reference)
                if reference
                else 1.0
            )
            median_speed = float(live_stat.get("median_speed_mps", 0.0))
            live_factor = max(0.75, min(1.50, 5.5 / median_speed))
            blended_factor = 0.65 + 0.35 * live_factor
            duration = max(1.0, scheduled * base_ratio * blended_factor)
            source = "live"
            timing_source = "model_live"
            observation_count = live_fresh
            coverage = float(live_stat.get("observed_coverage", 1.0))
            coverage = max(0.0, min(1.0, round(coverage, 4)))
            profile_label = (
                f"historical {hist_level} fallback{fallback_suffix}"
                if self.selected_rung != "1"
                else f"rung-1 {hist_level} profile"
            )
            coverage_note = (
                f"live VehiclePositions ({live_fresh} fresh vehicles on route {route_id}, "
                f"median {median_speed:.1f} m/s) + {profile_label}"
            )
        elif hist_reference:
            reference = hist_reference
            duration = scheduled * median(max(0.35, min(3.0, row.ratio)) for row in reference)
            source = "historical"
            timing_source = "historical_fallback"
            observation_count = len(reference)
            coverage = (
                self._coverage(reference)
                if reference[0].stretch.route_id == route_id
                else 0.0
            )
            if mode == "live":
                live_note = (
                    f"live VehiclePositions have no usable speed ({live_fresh} fresh vehicles)"
                    if live_fresh >= 1 and live_stat is not None
                    else f"sparse live observations (0 fresh vehicles on route {route_id})"
                )
                coverage_note = (
                    f"{live_note}; historical {hist_level} fallback "
                    f"(n={len(reference)}){fallback_suffix}"
                )
            else:
                coverage_note = (
                    f"sparse replay observations (0 same-day exact obs on route {route_id}); "
                    f"historical {hist_level} fallback (n={len(reference)}){fallback_suffix}"
                )
        else:
            reference = []
            duration = scheduled
            source = "timetable"
            timing_source = "timetable_fallback"
            observation_count = 0
            coverage = 0.0
            live_note = (
                f"live VehiclePositions have no usable speed ({live_fresh} fresh vehicles); "
                if mode == "live" and live_fresh >= 1 and live_stat is not None
                else ""
            )
            coverage_note = (
                f"{live_note}no usable live-speed or historical ride observations for route {route_id} "
                f"at hour {depart.hour:02d}; timetable fallback{fallback_suffix}"
            )

        p10, p90 = self._ratio_interval(reference, duration)
        estimate = TimingEstimate(
            duration,
            p10,
            p90,
            source,
            coverage,
            timing_source=timing_source,
            observation_count=observation_count,
            coverage_note=coverage_note,
        )
        return _with_replay_delay(estimate, replay_delay_s)

    def predict_wait(
        self,
        route_id: int,
        stop_id: str,
        scheduled_wait_s: float,
        depart: datetime,
        mode: str,
        as_of: datetime | None = None,
    ) -> WaitEstimate:
        depart = depart.astimezone(IST)
        as_of_ts = int((as_of or depart).timestamp())
        events = [
            row
            for row in self.stop_events.get((route_id, stop_id), [])
            if row.ts < as_of_ts
            and row.source_available_at < as_of_ts
            and row.source_available_at >= as_of_ts - 6 * 3600
        ]
        scheduled_wait = max(0.0, scheduled_wait_s)
        if not events:
            live_stat = self._live_route_stat(route_id) if mode == "live" else None
            live_fresh = int(live_stat.get("fresh_vehicle_count", 0)) if live_stat else 0
            if mode == "live" and live_fresh >= 1 and live_stat is not None:
                headway = max(240.0, min(1200.0, 3600.0 / max(1, live_fresh)))
                wait = max(0.0, min(headway, scheduled_wait if scheduled_wait > 0 else headway / 2.0))
                cov = max(0.0, min(1.0, round(float(live_stat.get("observed_coverage", 1.0)), 4)))
                return WaitEstimate(
                    wait,
                    max(wait + headway, 300.0),
                    headway,
                    "live",
                    cov,
                    timing_source="model_live",
                    observation_count=live_fresh,
                    coverage_note=f"live fleet headway estimate ({live_fresh} fresh vehicles on route {route_id})",
                )
            return WaitEstimate(
                scheduled_wait,
                max(scheduled_wait + 300.0, 600.0),
                600.0,
                "timetable",
                0.0,
                timing_source="timetable_fallback",
                observation_count=0,
                coverage_note=f"no recent stop-headway observations for route {route_id} stop {stop_id}; timetable fallback",
            )
        latest_by_run: dict[str, PassageRow] = {}
        for event in events:
            latest_by_run[event.run_id] = event
        run_events = sorted(latest_by_run.values(), key=lambda row: row.ts)
        headways = [
            later.ts - earlier.ts
            for earlier, later in pairwise(run_events)
            if 30 <= later.ts - earlier.ts <= 3600
        ]
        headway = median(headways) if headways else 600.0
        delay = median(row.effective_schedule_error_s for row in run_events[-5:])
        wait = max(0.0, min(headway, headway / 2 + delay))
        source = "replay" if mode == "replay" else "historical"
        timing_source = "model_replay" if mode == "replay" else "historical_fallback"
        coverage = sum(row.observed for row in run_events) / len(run_events)
        note = (
            f"{'causal October replay' if mode == 'replay' else 'historical'} "
            f"stop headways ({len(run_events)} runs at stop {stop_id})"
        )
        return WaitEstimate(
            wait,
            max(wait + headway, 300.0),
            headway,
            source,
            round(coverage, 4),
            timing_source=timing_source,
            observation_count=len(run_events),
            coverage_note=note,
            departure_delay_s=float(delay),
        )


def _stop_id(place: dict[str, Any]) -> str:
    stop = place.get("stop") or {}
    return str(stop.get("gtfsId") or place.get("name") or "").split(":")[-1]


def _route_id(leg: dict[str, Any]) -> int:
    value = str((leg.get("route") or {}).get("gtfsId") or "0").split(":")[-1]
    return int(value) if value.isdigit() else 0


def _seconds_between(start: str, end: str) -> float:
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


def _base_leg(kind: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw or {}
    route = raw.get("route") or {}
    from_place = raw.get("from") or {}
    to_place = raw.get("to") or {}
    return {
        "type": kind,
        "route_id": _route_id(raw) if route else None,
        "route_name": route.get("shortName") or route.get("longName") if route else None,
        "from_stop": from_place.get("name"),
        "to_stop": to_place.get("name"),
        "from_stop_id": _stop_id(from_place) or None,
        "to_stop_id": _stop_id(to_place) or None,
        "from_lat": from_place.get("lat"),
        "from_lng": from_place.get("lon"),
        "to_lat": to_place.get("lat"),
        "to_lng": to_place.get("lon"),
        "scheduled_duration_s": 0,
        "estimated_duration_s": 0,
        "window_p10_s": 0,
        "window_p90_s": 0,
        "source": "timetable",
        "timing_source": "otp_walk" if kind == "walk" else "timetable_fallback",
        "observed_coverage": None,
        "observation_count": None,
        "coverage_note": "walk leg from OTP street graph" if kind == "walk" else None,
        "geometry": (raw.get("legGeometry") or {}).get("points"),
    }


def _fingerprint(itinerary: dict[str, Any]) -> str:
    identity = [
        {
            "trip": (leg.get("trip") or {}).get("gtfsId"),
            "from": _stop_id(leg.get("from") or {}),
            "to": _stop_id(leg.get("to") or {}),
            "geometry": (leg.get("legGeometry") or {}).get("points"),
        }
        for leg in itinerary.get("legs", [])
    ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]


def retime_itinerary(
    itinerary: dict[str, Any],
    depart: datetime,
    mode: str,
    model: RetimeModel,
    replay_delay_s: float = 0.0,
) -> dict[str, Any]:
    """Replace OTP timing and produce correlated, transfer-aware scenarios."""
    depart = depart.astimezone(IST)
    raw_legs = itinerary.get("legs", [])
    transit_legs = [leg for leg in raw_legs if leg.get("transitLeg")]
    if not transit_legs:
        raise ValueError("candidate has no transit legs")
    first_transit = True
    events: list[dict[str, Any]] = []
    previous_transit: dict[str, Any] | None = None
    previous_transit_index = -1
    for raw_index, raw in enumerate(raw_legs):
        if not raw.get("transitLeg"):
            leg = _base_leg("walk", raw)
            duration = float(raw.get("duration", 0))
            leg.update(scheduled_duration_s=round(duration), estimated_duration_s=round(duration))
            events.append({"leg": leg, "kind": "fixed", "duration": duration})
            continue
        scheduled_start = raw["start"]["scheduledTime"]
        scheduled_end = raw["end"]["scheduledTime"]
        transit_depart = datetime.fromisoformat(scheduled_start).astimezone(IST)
        route_id = _route_id(raw)
        from_stop = _stop_id(raw.get("from") or {})
        to_stop = _stop_id(raw.get("to") or {})
        scheduled_ride = float(raw.get("duration", _seconds_between(scheduled_start, scheduled_end)))
        if previous_transit is None:
            initial_walk_s = sum(
                float(previous.get("duration", 0))
                for previous in raw_legs[:raw_index]
                if not previous.get("transitLeg")
            )
            scheduled_wait = max(
                0.0,
                (transit_depart - depart).total_seconds() - initial_walk_s,
            )
            wait = model.predict_wait(
                route_id,
                from_stop,
                scheduled_wait,
                transit_depart,
                mode,
                as_of=depart,
            )
        else:
            scheduled_gap = _seconds_between(
                previous_transit["end"]["scheduledTime"],
                scheduled_start,
            )
            transfer_walk_s = sum(
                float(previous.get("duration", 0))
                for previous in raw_legs[previous_transit_index + 1:raw_index]
                if not previous.get("transitLeg")
            )
            scheduled_wait = max(0.0, scheduled_gap - transfer_walk_s)
            wait = model.predict_wait(
                route_id,
                from_stop,
                scheduled_wait,
                transit_depart,
                mode,
                as_of=depart,
            )
            previous_place = previous_transit.get("to") or {}
            next_place = raw.get("from") or {}
            transfer = _base_leg("transfer", raw)
            transfer.update(
                from_stop=previous_place.get("name"),
                from_stop_id=_stop_id(previous_place) or None,
                from_lat=previous_place.get("lat"),
                from_lng=previous_place.get("lon"),
                to_stop=next_place.get("name"),
                to_stop_id=_stop_id(next_place) or None,
                to_lat=next_place.get("lat"),
                to_lng=next_place.get("lon"),
                geometry=None,
                scheduled_duration_s=round(scheduled_wait),
                source=wait.source,
                timing_source=wait.timing_source or _default_timing_source(wait.source),
                observed_coverage=wait.observed_coverage,
                observation_count=wait.observation_count,
                coverage_note=wait.coverage_note,
            )
            events.append({
                "leg": transfer,
                "kind": "transfer",
                "scheduled_wait": scheduled_wait,
                "connection_departure_s": (
                    (transit_depart - depart).total_seconds() + wait.departure_delay_s
                ),
                "wait": wait,
            })
        board = _base_leg("board", raw)
        board_duration = wait.duration_s if first_transit else 0.0
        board.update(
            scheduled_duration_s=round(scheduled_wait if first_transit else 0),
            estimated_duration_s=round(board_duration),
            source=wait.source,
            timing_source=wait.timing_source or _default_timing_source(wait.source),
            observed_coverage=wait.observed_coverage,
            observation_count=wait.observation_count,
            coverage_note=wait.coverage_note,
        )
        events.append({
            "leg": board,
            "kind": "first_wait" if first_transit else "fixed",
            "duration": board_duration,
            "wait": wait,
        })
        injected = replay_delay_s if first_transit else 0.0
        estimate = model.predict_ride(
            route_id,
            from_stop,
            to_stop,
            scheduled_ride,
            depart if first_transit else transit_depart,
            mode,
            injected,
            as_of=depart,
        )
        ride = _base_leg("ride", raw)
        ride_timing_source = estimate.timing_source or _default_timing_source(estimate.source)
        ride.update(
            scheduled_duration_s=round(scheduled_ride),
            estimated_duration_s=round(estimate.duration_s),
            source=estimate.source,
            timing_source=ride_timing_source,
            observed_coverage=estimate.observed_coverage,
            observation_count=estimate.observation_count,
            coverage_note=estimate.coverage_note,
        )
        events.append({"leg": ride, "kind": "ride", "estimate": estimate})
        alight = _base_leg("alight", raw)
        alight.update(
            source=estimate.source,
            timing_source=ride_timing_source,
            observed_coverage=estimate.observed_coverage,
            observation_count=estimate.observation_count,
            coverage_note=estimate.coverage_note,
        )
        events.append({"leg": alight, "kind": "fixed", "duration": 0.0})
        previous_transit = raw
        previous_transit_index = raw_index
        first_transit = False

    rng = random.Random(717)
    scenarios: list[list[float]] = []
    missed = [0] * len(events)
    for _ in range(SCENARIOS):
        shared = rng.gauss(0.0, 1.0)
        elapsed = 0.0
        durations: list[float] = []
        for index, event in enumerate(events):
            kind = event["kind"]
            if kind == "fixed":
                duration = float(event["duration"])
            elif kind == "first_wait":
                wait: WaitEstimate = event["wait"]
                duration = max(0.0, wait.duration_s + rng.gauss(0.0, wait.p90_s / 3.0))
            elif kind == "ride":
                estimate: TimingEstimate = event["estimate"]
                spread = (estimate.p90_s - estimate.p10_s) / 2.563
                duration = max(1.0, estimate.duration_s + spread * (0.8 * shared + 0.6 * rng.gauss(0.0, 1.0)))
            else:
                wait = event["wait"]
                connection_departure = event["connection_departure_s"]
                if elapsed <= connection_departure:
                    duration = connection_departure - elapsed
                else:
                    missed[index] += 1
                    duration = wait.headway_s - ((elapsed - connection_departure) % wait.headway_s)
            durations.append(duration)
            elapsed += duration
        scenarios.append(durations)

    windows = scenario_windows(scenarios)
    output_legs = []
    for index, event in enumerate(events):
        leg = event["leg"]
        summary = windows["legs"][index]
        leg.update(
            estimated_duration_s=round(summary["median_s"]),
            window_p10_s=round(summary["window_p10_s"]),
            window_p90_s=round(summary["window_p90_s"]),
        )
        if event["kind"] == "transfer":
            leg["connection_missed_probability"] = round(missed[index] / SCENARIOS, 4)
            leg["next_bus_headway_s"] = round(event["wait"].headway_s)
        output_legs.append(leg)

    quiet_total = 0.0
    quiet_depart = depart.replace(hour=23, minute=0, second=0, microsecond=0)
    for event in events:
        if event["kind"] == "ride":
            quiet = model.predict_ride(
                event["leg"]["route_id"],
                event["leg"]["from_stop_id"],
                event["leg"]["to_stop_id"],
                event["leg"]["scheduled_duration_s"],
                quiet_depart,
                mode,
                as_of=depart,
            )
            quiet_total += quiet.duration_s
        elif event["kind"] == "transfer":
            quiet_total += event["scheduled_wait"]
        elif event["kind"] != "first_wait":
            quiet_total += float(event["leg"]["estimated_duration_s"])
        else:
            quiet_total += event["wait"].duration_s
    return {
        "candidate_id": _fingerprint(itinerary),
        "legs": output_legs,
        "total_duration_s": round(windows["total_duration_s"]),
        "total_window_p10_s": round(windows["total_window_p10_s"]),
        "total_window_p90_s": round(windows["total_window_p90_s"]),
        "quieter_hour_typical_s": round(quiet_total),
        "scenario_count": SCENARIOS,
    }
