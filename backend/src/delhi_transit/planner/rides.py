"""Ride timing: the selected-rung artifact on covered routes, then the causal rung-1 fallback chain."""
from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from delhi_transit.core.config import IST, REPO_ROOT
from delhi_transit.live.freshness import effective_entity_max_age_s, refresh_route_stat
from delhi_transit.live.poller import read_live_route_stats
from delhi_transit.model.artifact import LoadedModelArtifact, load_model_artifact
from delhi_transit.model.features import (
    CausalFeatures,
    PassageRow,
    RouteGeometry,
    build_route_geometry,
    build_stretch_errors,
    load_passages,
)
from delhi_transit.model.selection import load_selected_rung
from delhi_transit.planner.causal_context import (
    TimingRecord,
    causal_features_for_stretch,
    visible,
)
from delhi_transit.planner.provenance import (
    TimingEstimate,
    WaitEstimate,
    fresh_vehicle_count,
    live_coverage,
    observed_coverage,
    with_replay_delay,
)
from delhi_transit.planner.risk import (
    clamp_ratio,
    correlated_leg_interval,
    ratio_interval,
)
from delhi_transit.planner.waits import fallback_wait, headway_wait, recent_stop_events

# (from_sequence, to_sequence, from_stop_id, to_stop_id, scheduled_s)
Stretch = tuple[int, int, str, str, float]


class RetimeModel:
    """Selected-rung adapter with causal historical and timetable fallbacks."""

    def __init__(
        self,
        data_dir: Path,
        selection_path: Path | None = None,
        model_dir: Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.selection_path = selection_path or REPO_ROOT / "reports" / "selection.json"
        self.model_dir = Path(model_dir) if model_dir is not None else self.data_dir / "model"
        self.selected_rung = load_selected_rung(self.selection_path)
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
        self._loaded_passage_files: list[Path] = []
        loaded = load_model_artifact(self.model_dir)
        self._artifact: LoadedModelArtifact | None = (
            loaded
            if loaded is not None and loaded.rung == self.selected_rung and self.selected_rung != "1"
            else None
        )
        self._load()

    def _live_route_stat(
        self, route_id: int, stale_threshold_s: float | None
    ) -> dict[str, Any] | None:
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
            stat, now=time.time(), max_age_s=effective_entity_max_age_s(stale_threshold_s)
        )

    def replay_reference_info(self, depart: datetime) -> dict[str, Any]:
        """Summarize only reconstructed observations actually loaded before the cutoff."""
        depart = depart.astimezone(IST)
        depart_ts = int(depart.timestamp())
        day_str = depart.date().isoformat()
        visible_records = visible(self.records, depart_ts)
        same_day = [r for r in visible_records if r.stretch.day == day_str]
        observed_same_day = [r for r in same_day if r.stretch.target_observed]
        candidate_pool = observed_same_day or same_day or visible_records
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
            "visible_observations": len(visible_records),
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

    def _stretch_features(
        self,
        route_id: int,
        direction: str,
        stretches: list[Stretch],
        **context: Any,
    ) -> list[tuple[tuple[int, str, str, str], CausalFeatures]]:
        geom = self.route_geometries.get((route_id, direction))
        return [
            (
                (route_id, direction, sid_a, sid_b),
                causal_features_for_stretch(
                    geom=geom,
                    exact_records=self.exact.get((route_id, sid_a, sid_b), []),
                    stop_rows=self.stop_events.get((route_id, sid_a), []),
                    route_id=route_id,
                    from_seq=seq_a,
                    to_seq=seq_b,
                    scheduled_s=sched,
                    **context,
                ),
            )
            for seq_a, seq_b, sid_a, sid_b, sched in stretches
        ]

    def _pattern_stretches(
        self, route_id: int, ordered_stops: list[tuple[int, str]], from_stop: str, to_stop: str, scheduled: float
    ) -> list[Stretch]:
        """Leg stretches on the reconstructed pattern, scheduled in proportion to typical stretch times."""
        stop_ids = [sid for _, sid in ordered_stops]
        if from_stop not in stop_ids or to_stop not in stop_ids:
            return []
        from_idx = stop_ids.index(from_stop)
        to_idx = stop_ids.index(to_stop)
        if from_idx >= to_idx:
            return []
        raw_scheds = [
            self.stretch_typical_scheduled.get((route_id, ordered_stops[i][1], ordered_stops[i + 1][1]), 60.0)
            for i in range(from_idx, to_idx)
        ]
        total_raw = sum(raw_scheds) or float(len(raw_scheds))
        scale = scheduled / total_raw
        stretches: list[Stretch] = []
        for offset, i in enumerate(range(from_idx, to_idx)):
            seq_a, sid_a = ordered_stops[i]
            seq_b, sid_b = ordered_stops[i + 1]
            stretches.append((seq_a, seq_b, sid_a, sid_b, max(1.0, raw_scheds[offset] * scale)))
        return stretches

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
        live_fresh = fresh_vehicle_count(live_stat)
        context = {
            "depart": depart,
            "as_of": as_of,
            "mode": mode,
            "same_day_route": same_day_route,
            "live_stat": live_stat,
        }

        stretches = self._pattern_stretches(route_id, ordered_stops, from_stop, to_stop, scheduled)
        pattern_matched = bool(stretches)
        if pattern_matched:
            preds = artifact.predict_batch_from_features(
                self._stretch_features(route_id, direction, stretches, **context)
            )
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
            preds = artifact.predict_batch_from_features(
                self._stretch_features(route_id, direction, [(seq_a, seq_b, sid_a, sid_b, mid_sched)], **context)
            )
            ratio = clamp_ratio((mid_sched + preds[0].error_s) / mid_sched)
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

        if mode == "replay" and same_day_route:
            source, timing_source = "replay", "model_replay"
            reference = same_day_route
            observation_count = len(same_day_route)
            coverage = observed_coverage(same_day_route)
            coverage_note = (
                f"causal October replay {model_desc} "
                f"({len(same_day_route)} same-day observed stretches on route {route_id}; {stretch_desc})"
            )
        elif mode != "replay" and live_fresh >= 1 and live_stat is not None:
            median_speed = float(live_stat.get("median_speed_mps", 0.0))
            source, timing_source = "live", "model_live"
            reference = hist_reference
            observation_count = live_fresh
            coverage = live_coverage(live_stat)
            coverage_note = (
                f"live VehiclePositions ({live_fresh} fresh vehicles on route {route_id}, "
                f"median {median_speed:.1f} m/s) + {model_desc} ({stretch_desc})"
            )
        else:
            source, timing_source = "historical", "historical_fallback"
            reference = hist_reference
            observation_count = len(hist_reference)
            coverage = observed_coverage(hist_reference)
            if mode == "replay":
                coverage_note = (
                    f"sparse replay observations (0 same-day obs on route {route_id}); "
                    f"{model_desc} time-of-day/historical profile ({stretch_desc})"
                )
            else:
                coverage_note = (
                    f"sparse live observations (0 fresh vehicles on route {route_id}); "
                    f"{model_desc} time-of-day/historical fallback ({stretch_desc})"
                )

        has_quantiles = all(p.p10_s is not None and p.p50_s is not None and p.p90_s is not None for p in preds)
        if has_quantiles and pattern_matched:
            p10, p90 = correlated_leg_interval(duration, preds)
        else:
            p10, p90 = ratio_interval([record.ratio for record in reference], duration)

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
        return with_replay_delay(estimate, replay_delay_s)

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
        stale_threshold_s: float | None = None,
    ) -> TimingEstimate:
        scheduled = max(1.0, scheduled_duration_s)
        depart = depart.astimezone(IST)
        observation_cutoff = (as_of or depart).astimezone(IST)
        as_of_ts = int(observation_cutoff.timestamp())
        service_day = depart.date().isoformat()
        exact = visible(self.exact.get((route_id, from_stop, to_stop), []), as_of_ts)
        same_day = [
            record
            for record in exact
            if record.stretch.day == service_day and record.stretch.target_observed
        ]
        historical_exact = [record for record in exact if record.stretch.target_observed]
        same_hour = [record for record in historical_exact if record.hour == depart.hour]
        route_records = visible(self.route_hour.get((route_id, depart.hour), []), as_of_ts)
        same_day_route = [
            record
            for record in visible(self.records, as_of_ts)
            if record.stretch.route_id == route_id
            and record.stretch.day == service_day
            and record.stretch.target_observed
            and record.stretch.available_at >= as_of_ts - 7200
        ]
        global_records = visible(self.global_hour.get(depart.hour, []), as_of_ts)

        if same_hour:
            hist_reference, hist_level = same_hour, "stretch_hour"
        elif historical_exact:
            hist_reference, hist_level = historical_exact, "stretch"
        elif route_records:
            hist_reference, hist_level = route_records, "route_hour"
        elif global_records:
            hist_reference, hist_level = global_records, "corridor_hour"
        else:
            hist_reference, hist_level = [], "none"

        live_stat = self._live_route_stat(route_id, stale_threshold_s) if mode == "live" else None
        live_fresh = fresh_vehicle_count(live_stat)

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
            source, timing_source = "replay", "model_replay"
            reference = exact or [previous]
            observation_count = len(same_day)
            coverage = observed_coverage(reference)
            coverage_note = (
                f"causal October replay rung 1 persistence "
                f"({len(same_day)} same-day observed runs on route {route_id}){fallback_suffix}"
            )
        elif mode == "replay" and same_day_route:
            recent = sorted(same_day_route, key=lambda record: record.stretch.query_ts)[-20:]
            duration = max(1.0, scheduled * median(clamp_ratio(row.ratio) for row in recent))
            source, timing_source = "replay", "model_replay"
            reference = recent
            observation_count = len(same_day_route)
            coverage = observed_coverage(same_day_route)
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
            base_ratio = median(clamp_ratio(row.ratio) for row in reference) if reference else 1.0
            median_speed = float(live_stat.get("median_speed_mps", 0.0))
            live_factor = max(0.75, min(1.50, 5.5 / median_speed))
            blended_factor = 0.65 + 0.35 * live_factor
            duration = max(1.0, scheduled * base_ratio * blended_factor)
            source, timing_source = "live", "model_live"
            observation_count = live_fresh
            coverage = live_coverage(live_stat)
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
            duration = scheduled * median(clamp_ratio(row.ratio) for row in reference)
            source, timing_source = "historical", "historical_fallback"
            observation_count = len(reference)
            # Corridor-hour references come from other routes, so they say nothing about this route.
            coverage = observed_coverage(reference) if reference[0].stretch.route_id == route_id else 0.0
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
            source, timing_source = "timetable", "timetable_fallback"
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

        p10, p90 = ratio_interval([record.ratio for record in reference], duration)
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
        return with_replay_delay(estimate, replay_delay_s)

    def predict_wait(
        self,
        route_id: int,
        stop_id: str,
        scheduled_wait_s: float,
        depart: datetime,
        mode: str,
        as_of: datetime | None = None,
        stale_threshold_s: float | None = None,
    ) -> WaitEstimate:
        as_of_ts = int((as_of or depart).timestamp())
        events = recent_stop_events(self.stop_events.get((route_id, stop_id), []), as_of_ts)
        if events:
            return headway_wait(events, stop_id, mode)
        live_stat = self._live_route_stat(route_id, stale_threshold_s) if mode == "live" else None
        return fallback_wait(route_id, stop_id, max(0.0, scheduled_wait_s), mode, live_stat)
