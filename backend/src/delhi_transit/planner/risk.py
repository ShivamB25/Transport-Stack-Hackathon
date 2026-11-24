"""Per-leg windows and joint door-to-door scenarios (leg quantiles are never added)."""
from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Any

from delhi_transit.model.rungs import Prediction
from delhi_transit.planner.provenance import TimingEstimate, WaitEstimate

SCENARIOS = 192


def clamp_ratio(ratio: float) -> float:
    """Bound an observed actual/scheduled ride ratio before it times a leg."""
    return max(0.35, min(3.0, ratio))


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


def ratio_interval(ratios: Sequence[float], duration: float) -> tuple[float, float]:
    """Ride window from reference ratios, or a fixed spread when there are too few."""
    clamped = [clamp_ratio(ratio) for ratio in ratios]
    if len(clamped) >= 4:
        low = percentile(clamped, 0.1) * duration
        high = percentile(clamped, 0.9) * duration
    else:
        low = duration - max(45.0, duration * 0.12)
        high = duration + max(90.0, duration * 0.25)
    return max(1.0, low), max(duration + 1.0, high)


def correlated_leg_interval(
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


def simulate_joint_scenarios(events: list[dict[str, Any]]) -> tuple[list[list[float]], list[int]]:
    """Draw whole-journey scenarios; a late arrival misses the connection and waits for the next bus.

    Returns the per-scenario leg durations and, per event, how many scenarios missed it.
    """
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
                delay = event["delay"]
                spread = (estimate.p90_s - estimate.p10_s) / 2.563
                undelayed = estimate.duration_s - delay + spread * (0.8 * shared + 0.6 * rng.gauss(0.0, 1.0))
                # Floor the undelayed draw so an injected delay translates the window exactly.
                duration = max(1.0, undelayed) + delay
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
    return scenarios, missed
