"""Tests for the rung-1 fallback measurement on routes with no learned model."""
from __future__ import annotations

from delhi_transit.data.fallback_check import measure
from delhi_transit.model.features import PassageRow

TRAIN = "2026-10-22"
HOLDOUT = "2026-10-27"


def _day(day: str, route_id: int, ratio: float, runs: int = 4, stops: int = 4) -> list[PassageRow]:
    """One day of observed passages where every stretch takes `ratio` times its schedule."""
    rows: list[PassageRow] = []
    base = 8 * 3600
    for run in range(runs):
        sched = base + run * 900
        ts = sched
        for seq in range(stops):
            rows.append(
                PassageRow(
                    day=day,
                    route_id=route_id,
                    direction="UP",
                    vehicle_id=f"V{run}",
                    run_id=f"{day}_R{run}",
                    stop_sequence=seq,
                    stop_id=f"S{seq}",
                    observed=True,
                    ts=ts,
                    scheduled_ts=sched,
                    stop_lat=28.5 + seq * 0.001,
                    stop_lng=77.25,
                    schedule_error_s=ts - sched,
                    distance_m=10.0,
                )
            )
            sched += 120
            ts += int(120 * ratio)
    return rows


def test_fallback_beats_the_timetable_when_stretches_run_slow() -> None:
    """Every stretch runs 2x its schedule, so the stretch ratio recovers it exactly."""
    passages = _day(TRAIN, 1616, ratio=2.0) + _day(HOLDOUT, 1616, ratio=2.0)
    result = measure(passages, TRAIN, HOLDOUT)

    assert result["holdout_stretches"] == 12  # 4 runs x 3 consecutive pairs
    # timetable predicts zero error, so its MAE is the whole 120 s overrun
    assert result["timetable_mae_s"] == 120.0
    # the fallback learns the 2.0 ratio and lands exactly
    assert result["fallback_mae_s"] == 0.0
    assert result["gain_pct"] == 100.0
    assert result["levels"] == {"stretch_hour": 2, "kalman_route": 1, "kalman_exact": 9}


def test_fallback_is_no_worse_than_the_timetable_when_stretches_run_on_time() -> None:
    """A 1.0 ratio means the fallback reproduces the timetable rather than degrading it."""
    passages = _day(TRAIN, 1616, ratio=1.0) + _day(HOLDOUT, 1616, ratio=1.0)
    result = measure(passages, TRAIN, HOLDOUT)

    assert result["timetable_mae_s"] == 0.0
    assert result["fallback_mae_s"] == 0.0
    assert result["gain_pct"] == 0.0


def test_measure_reports_per_route_and_pooled_errors() -> None:
    slow = _day(TRAIN, 1616, ratio=2.0) + _day(HOLDOUT, 1616, ratio=2.0)
    on_time = _day(TRAIN, 2204, ratio=1.0) + _day(HOLDOUT, 2204, ratio=1.0)
    result = measure(slow + on_time, TRAIN, HOLDOUT)

    assert set(result["by_route"]) == {1616, 2204}
    assert result["by_route"][1616]["gain_pct"] == 100.0
    assert result["by_route"][2204]["gain_pct"] == 0.0
    # pooled: 12 stretches at 120 s timetable error, 12 at zero
    assert result["timetable_mae_s"] == 60.0
    assert result["fallback_mae_s"] == 0.0
    assert result["gain_pct"] == 100.0


def test_measure_requires_observed_stretches_on_both_days() -> None:
    passages = _day(TRAIN, 1616, ratio=1.5)
    try:
        measure(passages, TRAIN, HOLDOUT)
    except ValueError as exc:
        assert HOLDOUT in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("measure should reject a holdout day with no observations")


def test_fallback_adapts_causally_when_holdout_day_shifts_from_train() -> None:
    """When holdout runs slower than train, same-day Kalman updates beat the static train prior."""
    # Train day runs on time (ratio 1.0); holdout day runs at 2.0x schedule.
    passages = _day(TRAIN, 1616, ratio=1.0, runs=6) + _day(HOLDOUT, 1616, ratio=2.0, runs=6)
    result = measure(passages, TRAIN, HOLDOUT)

    # Static median from train would predict 1.0x (0% gain); causal Kalman adapts after run 0.
    assert result["timetable_mae_s"] == 120.0
    assert result["fallback_mae_s"] < 30.0
    assert result["gain_pct"] > 75.0
    assert result["levels"] == {"stretch_hour": 2, "kalman_route": 1, "kalman_exact": 15}
