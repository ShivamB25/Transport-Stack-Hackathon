"""First-boarding and connection waits from causal stop headways, live fleet, or timetable."""
from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise
from statistics import median
from typing import Any

from delhi_transit.model.features import PassageRow
from delhi_transit.planner.provenance import (
    WaitEstimate,
    fresh_vehicle_count,
    live_coverage,
)

STOP_EVENT_LOOKBACK_S = 6 * 3600


def recent_stop_events(stop_events: Sequence[PassageRow], as_of_ts: int) -> list[PassageRow]:
    """Stop passages that happened and were knowable before the cutoff, within the lookback."""
    return [
        row
        for row in stop_events
        if row.ts < as_of_ts
        and row.source_available_at < as_of_ts
        and row.source_available_at >= as_of_ts - STOP_EVENT_LOOKBACK_S
    ]


def headway_wait(events: Sequence[PassageRow], stop_id: str, mode: str) -> WaitEstimate:
    """Wait from the headways and delays of the latest passage of each run at the stop."""
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


def fallback_wait(
    route_id: int,
    stop_id: str,
    scheduled_wait: float,
    mode: str,
    live_stat: dict[str, Any] | None,
) -> WaitEstimate:
    """Wait without stop observations: live fleet headway when fresh, else the timetable."""
    live_fresh = fresh_vehicle_count(live_stat)
    if mode == "live" and live_fresh >= 1 and live_stat is not None:
        headway = max(240.0, min(1200.0, 3600.0 / max(1, live_fresh)))
        wait = max(0.0, min(headway, scheduled_wait if scheduled_wait > 0 else headway / 2.0))
        return WaitEstimate(
            wait,
            max(wait + headway, 300.0),
            headway,
            "live",
            live_coverage(live_stat),
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
