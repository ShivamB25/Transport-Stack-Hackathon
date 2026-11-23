"""Model re-timing and joint door-to-door replay scenarios."""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

from ps1.features import PassageRow, StretchError, build_stretch_errors, load_passages
from ps1.live_poller import read_live_route_stats

IST = timezone(timedelta(hours=5, minutes=30))
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

    def __init__(self, data_dir: Path, selection_path: Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        root = Path(__file__).resolve().parents[2]
        self.selection_path = selection_path or root / "reports" / "selection.json"
        self.selected_rung = "1"
        if self.selection_path.exists():
            selection = json.loads(self.selection_path.read_text())
            self.selected_rung = str(
                selection.get("selected_rung", selection.get("rung", selection.get("selected", "1")))
            ).removeprefix("rung")
        self.records: list[TimingRecord] = []
        self.exact: dict[tuple[int, str, str], list[TimingRecord]] = defaultdict(list)
        self.route_hour: dict[tuple[int, int], list[TimingRecord]] = defaultdict(list)
        self.global_hour: dict[int, list[TimingRecord]] = defaultdict(list)
        self.stop_events: dict[tuple[int, str], list[PassageRow]] = defaultdict(list)
        self._live_stats_mtime: float | None = None
        self._live_stats_cache: dict[int, dict[str, Any]] = {}
        self._load()

    def _live_route_stat(self, route_id: int) -> dict[str, Any] | None:
        stats_path = self.data_dir / "live" / "route_stats.json"
        if not stats_path.exists():
            return None
        try:
            mtime = stats_path.stat().st_mtime
        except OSError:
            return None
        if getattr(self, "_live_stats_mtime", None) != mtime:
            self._live_stats_cache = read_live_route_stats(self.data_dir)
            self._live_stats_mtime = mtime
        return getattr(self, "_live_stats_cache", {}).get(route_id)

    def replay_reference_info(self, depart: datetime) -> dict[str, Any]:
        """Summarize causal October observations visible at a replay departure."""
        depart_ts = int(depart.timestamp())
        day_str = depart.date().isoformat()
        visible = [r for r in self.records if r.stretch.to_ts <= depart_ts]
        same_day = [r for r in visible if r.stretch.day == day_str]
        observed_same_day = [r for r in same_day if r.stretch.target_observed]
        candidate_pool = observed_same_day or same_day or visible
        latest_ts = max((r.stretch.to_ts for r in candidate_pool), default=None)
        if latest_ts is None:
            latest_stop = max(
                (row.ts for rows in self.stop_events.values() for row in rows if row.ts <= depart_ts),
                default=None,
            )
            latest_ts = latest_stop
        passage_file = self.data_dir / "passages" / f"day={day_str}.parquet"
        gps_file = self.data_dir / "gps" / f"day={day_str}.parquet"
        fixture_path = (
            str(passage_file)
            if passage_file.exists()
            else (str(gps_file) if gps_file.exists() else str(self.data_dir / "passages"))
        )
        if latest_ts is None:
            return {
                "reference_timestamp": depart.isoformat(),
                "feed_timestamp": depart.isoformat(),
                "feed_age_seconds": 0.0,
                "visible_observations": 0,
                "same_day_observations": 0,
                "observed_same_day_stretches": 0,
                "fixture_path": fixture_path,
            }
        ref_iso = datetime.fromtimestamp(latest_ts, IST).isoformat()
        return {
            "reference_timestamp": ref_iso,
            "feed_timestamp": ref_iso,
            "feed_age_seconds": round(max(0.0, float(depart_ts - latest_ts)), 2),
            "visible_observations": len(visible),
            "same_day_observations": len(same_day),
            "observed_same_day_stretches": len(observed_same_day),
            "fixture_path": fixture_path,
        }

    def _load(self) -> None:
        files = sorted((self.data_dir / "passages").glob("*.parquet"))
        if not files:
            return
        passages = load_passages(self.data_dir / "passages" / "*.parquet")
        self.records = [TimingRecord(row) for row in build_stretch_errors(passages)]
        for passage in passages:
            self.stop_events[(passage.route_id, passage.stop_id)].append(passage)
        for record in self.records:
            stretch = record.stretch
            self.exact[(stretch.route_id, stretch.from_stop_id, stretch.to_stop_id)].append(record)
            if stretch.target_observed:
                self.route_hour[(stretch.route_id, record.hour)].append(record)
                self.global_hour[record.hour].append(record)
        for group in self.stop_events.values():
            group.sort(key=lambda row: row.ts)

    @staticmethod
    def _visible(records: list[TimingRecord], depart_ts: int) -> list[TimingRecord]:
        return [
            record
            for record in records
            if record.stretch.query_ts < depart_ts and record.stretch.to_ts <= depart_ts
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

    def predict_ride(
        self,
        route_id: int,
        from_stop: str,
        to_stop: str,
        scheduled_duration_s: float,
        depart: datetime,
        mode: str,
        replay_delay_s: float = 0.0,
    ) -> TimingEstimate:
        scheduled = max(1.0, scheduled_duration_s)
        depart_ts = int(depart.timestamp())
        exact = self._visible(self.exact.get((route_id, from_stop, to_stop), []), depart_ts)
        same_day = [
            record
            for record in exact
            if record.stretch.day == depart.date().isoformat() and record.stretch.target_observed
        ]
        historical_exact = [record for record in exact if record.stretch.target_observed]
        same_hour = [record for record in historical_exact if record.hour == depart.hour]
        route_records = self._visible(self.route_hour.get((route_id, depart.hour), []), depart_ts)
        same_day_route = [
            record
            for record in self.records
            if record.stretch.route_id == route_id
            and record.stretch.day == depart.date().isoformat()
            and record.stretch.target_observed
            and record.stretch.query_ts < depart_ts
            and record.stretch.to_ts <= depart_ts
            and record.stretch.to_ts >= depart_ts - 7200
        ]
        global_records = self._visible(self.global_hour.get(depart.hour, []), depart_ts)

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

        if mode == "replay" and self.selected_rung == "1" and same_day:
            previous = max(same_day, key=lambda record: record.stretch.query_ts)
            duration = max(1.0, scheduled + previous.stretch.error_s)
            source = "replay"
            timing_source = "model_replay"
            reference = exact or [previous]
            observation_count = len(same_day)
            coverage = self._coverage(reference)
            coverage_note = (
                f"causal October replay rung {self.selected_rung} "
                f"({len(same_day)} same-day observed runs on route {route_id})"
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
                f"causal October replay rung {self.selected_rung} "
                f"({len(same_day_route)} same-day route-{route_id} observed stretches)"
            )
        elif mode == "live" and live_fresh >= 1 and live_stat is not None:
            reference = hist_reference
            base_ratio = (
                median(max(0.35, min(3.0, row.ratio)) for row in reference)
                if reference
                else 1.0
            )
            median_speed = float(live_stat.get("median_speed_mps", 0.0))
            if median_speed > 0.5:
                live_factor = max(0.75, min(1.50, 5.5 / median_speed))
                blended_factor = 0.65 + 0.35 * live_factor
            else:
                blended_factor = 1.0
            duration = max(1.0, scheduled * base_ratio * blended_factor)
            source = "live"
            timing_source = "model_live"
            observation_count = live_fresh
            coverage = float(live_stat.get("observed_coverage", 1.0))
            coverage = max(0.0, min(1.0, round(coverage, 4)))
            coverage_note = (
                f"live VehiclePositions ({live_fresh} fresh vehicles on route {route_id}, "
                f"median {median_speed:.1f} m/s) + rung-{self.selected_rung} {hist_level} profile"
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
                coverage_note = (
                    f"sparse live observations (0 fresh vehicles on route {route_id}); "
                    f"historical {hist_level} fallback (n={len(reference)})"
                )
            else:
                coverage_note = (
                    f"sparse replay observations (0 same-day exact obs on route {route_id}); "
                    f"historical {hist_level} fallback (n={len(reference)})"
                )
        else:
            reference = []
            duration = scheduled
            source = "timetable"
            timing_source = "timetable_fallback"
            observation_count = 0
            coverage = 0.0
            coverage_note = (
                f"no live or historical observations for route {route_id} "
                f"at hour {depart.hour:02d}; timetable fallback"
            )

        if replay_delay_s:
            duration += replay_delay_s
            if mode == "replay":
                source = "replay"
                timing_source = "model_replay"
                coverage_note = f"{coverage_note}; +{int(replay_delay_s)}s replay delay injected"
        p10, p90 = self._ratio_interval(reference, duration)
        if replay_delay_s:
            p10 += replay_delay_s
            p90 += replay_delay_s
        return TimingEstimate(
            duration,
            p10,
            p90,
            source,
            coverage,
            timing_source=timing_source,
            observation_count=observation_count,
            coverage_note=coverage_note,
        )

    def predict_wait(
        self,
        route_id: int,
        stop_id: str,
        scheduled_wait_s: float,
        depart: datetime,
        mode: str,
    ) -> WaitEstimate:
        depart_ts = int(depart.timestamp())
        events = [
            row
            for row in self.stop_events.get((route_id, stop_id), [])
            if row.ts <= depart_ts and row.ts >= depart_ts - 6 * 3600
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
        delay = median(row.ts - row.scheduled_ts for row in run_events[-5:])
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
    return {
        "type": kind,
        "route_id": _route_id(raw) if route else None,
        "route_name": route.get("shortName") or route.get("longName") if route else None,
        "from_stop": (raw.get("from") or {}).get("name"),
        "to_stop": (raw.get("to") or {}).get("name"),
        "from_stop_id": _stop_id(raw.get("from") or {}) or None,
        "to_stop_id": _stop_id(raw.get("to") or {}) or None,
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
    raw_legs = itinerary.get("legs", [])
    transit_legs = [leg for leg in raw_legs if leg.get("transitLeg")]
    if not transit_legs:
        raise ValueError("candidate has no transit legs")
    first_transit = True
    events: list[dict[str, Any]] = []
    previous_transit: dict[str, Any] | None = None
    for raw in raw_legs:
        if not raw.get("transitLeg"):
            leg = _base_leg("walk", raw)
            duration = float(raw.get("duration", 0))
            leg.update(scheduled_duration_s=round(duration), estimated_duration_s=round(duration))
            events.append({"leg": leg, "kind": "fixed", "duration": duration})
            continue
        scheduled_start = raw["start"]["scheduledTime"]
        scheduled_end = raw["end"]["scheduledTime"]
        transit_depart = datetime.fromisoformat(scheduled_start)
        route_id = _route_id(raw)
        from_stop = _stop_id(raw.get("from") or {})
        to_stop = _stop_id(raw.get("to") or {})
        scheduled_ride = float(raw.get("duration", _seconds_between(scheduled_start, scheduled_end)))
        scheduled_wait = 0.0
        if previous_transit is None:
            raw_index = raw_legs.index(raw)
            if raw_index:
                scheduled_wait = _seconds_between(raw_legs[raw_index - 1]["end"]["scheduledTime"], scheduled_start)
        else:
            scheduled_wait = _seconds_between(previous_transit["end"]["scheduledTime"], scheduled_start)
            transfer = _base_leg("transfer", raw)
            transfer["from_stop"] = (previous_transit.get("to") or {}).get("name")
            transfer["from_stop_id"] = _stop_id(previous_transit.get("to") or {})
            wait = model.predict_wait(route_id, from_stop, scheduled_wait, transit_depart, mode)
            transfer.update(
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
                "scheduled_gap": scheduled_wait,
                "wait": wait,
            })
        board = _base_leg("board", raw)
        wait = model.predict_wait(route_id, from_stop, scheduled_wait, transit_depart, mode)
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
        first_transit = False

    rng = random.Random(717)
    scenarios: list[list[float]] = []
    missed = [0] * len(events)
    for _ in range(SCENARIOS):
        shared = rng.gauss(0.0, 1.0)
        elapsed = 0.0
        nominal = 0.0
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
                scheduled_departure = nominal + event["scheduled_gap"]
                if elapsed <= scheduled_departure:
                    duration = scheduled_departure - elapsed
                else:
                    missed[index] += 1
                    duration = wait.headway_s - ((elapsed - scheduled_departure) % wait.headway_s)
            durations.append(duration)
            elapsed += duration
            if kind != "transfer":
                nominal += event["leg"]["estimated_duration_s"]
            else:
                nominal += event["scheduled_gap"]
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
    for event in events:
        if event["kind"] == "ride":
            quiet = model.predict_ride(
                event["leg"]["route_id"],
                event["leg"]["from_stop_id"],
                event["leg"]["to_stop_id"],
                event["leg"]["scheduled_duration_s"],
                depart.replace(hour=23, minute=0),
                mode,
            )
            quiet_total += quiet.duration_s
        elif event["kind"] == "transfer":
            quiet_total += event["scheduled_gap"]
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
