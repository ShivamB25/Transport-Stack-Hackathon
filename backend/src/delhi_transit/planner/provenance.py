"""Timing estimates and the provenance fields every timed leg carries."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

TIMING_SOURCES = {
    "live": "model_live",
    "replay": "model_replay",
    "historical": "historical_fallback",
    "timetable": "timetable_fallback",
}


def timing_source_for(source: str) -> str:
    return TIMING_SOURCES.get(source, source)


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
    departure_delay_s: float = 0.0


def with_replay_delay(estimate: TimingEstimate, delay_s: float) -> TimingEstimate:
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


def leg_provenance(estimate: TimingEstimate | WaitEstimate) -> dict[str, Any]:
    """Response fields that say what timed a leg and how much of it was observed."""
    return {
        "source": estimate.source,
        "timing_source": estimate.timing_source or timing_source_for(estimate.source),
        "observed_coverage": estimate.observed_coverage,
        "observation_count": estimate.observation_count,
        "coverage_note": estimate.coverage_note,
    }


class _ObservedTarget(Protocol):
    @property
    def target_observed(self) -> bool: ...


class _HasStretch(Protocol):
    @property
    def stretch(self) -> _ObservedTarget: ...


def observed_coverage(records: Sequence[_HasStretch]) -> float:
    """Share of reference stretches whose both endpoints were directly observed."""
    if not records:
        return 0.0
    return round(sum(record.stretch.target_observed for record in records) / len(records), 4)


def fresh_vehicle_count(live_stat: dict[str, Any] | None) -> int:
    return int(live_stat.get("fresh_vehicle_count", 0)) if live_stat else 0


def live_coverage(live_stat: dict[str, Any]) -> float:
    return max(0.0, min(1.0, round(float(live_stat.get("observed_coverage", 1.0)), 4)))
