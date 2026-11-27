from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

import pytest

from delhi_transit.model.features import StretchError
from delhi_transit.planner.causal_context import TimingRecord
from delhi_transit.planner.provenance import TimingEstimate, with_replay_delay
from delhi_transit.planner.rides import RetimeModel
from delhi_transit.planner.risk import scenario_windows, simulate_joint_scenarios

IST = timezone(timedelta(hours=5, minutes=30))


def test_joint_windows_do_not_add_leg_quantiles() -> None:
    scenarios = [[10.0, 20.0], [20.0, 10.0], [15.0, 15.0]]
    windows = scenario_windows(scenarios)
    assert windows["total_window_p90_s"] == 30.0
    assert windows["total_window_p10_s"] == 30.0
    assert sum(leg["window_p90_s"] for leg in windows["legs"]) == 38.0


def _ride_event(duration_s: float, p10_s: float, p90_s: float, delay: float = 0.0) -> dict:
    return {
        "leg": {},
        "kind": "ride",
        "estimate": TimingEstimate(
            duration_s=duration_s,
            p10_s=p10_s,
            p90_s=p90_s,
            source="live",
            observed_coverage=1.0,
        ),
        "delay": delay,
    }


def test_joint_scenarios_never_draw_a_physically_impossible_ride() -> None:
    """A wide ride interval must not put scenarios near zero.

    Regression: ``simulate_joint_scenarios`` floored the undelayed draw at 1.0 s, so
    on a wide interval the lower Gaussian tail (8-10% of the 192 scenarios) landed at
    ~1 s. Those draws set the p10 of the distribution, which dragged every reported
    ride p10 towards zero and made the published window far wider than the calibrated
    interval. A 51.8 min ride was reported as [0.4, 104.5] min.
    """
    estimate = _ride_event(duration_s=3108.0, p10_s=24.0, p90_s=6270.0)["estimate"]
    scenarios, _ = simulate_joint_scenarios([_ride_event(3108.0, 24.0, 6270.0)])
    durations = [scenario[0] for scenario in scenarios]
    # No scenario may fall below the same 35% floor the ratio path already uses.
    assert min(durations) >= 0.35 * estimate.duration_s
    # And the p10 of the distribution must be a usable ride time, not ~1 s.
    assert sorted(durations)[int(0.1 * (len(durations) - 1))] >= 0.35 * estimate.duration_s


def test_joint_scenarios_translate_exactly_with_an_injected_replay_delay() -> None:
    """The floor must not break the replay delay contract: the window shifts by exactly the delay."""
    base = _ride_event(duration_s=600.0, p10_s=480.0, p90_s=780.0)
    delayed_estimate = with_replay_delay(base["estimate"], 300.0)
    delayed = {"leg": {}, "kind": "ride", "estimate": delayed_estimate, "delay": 300.0}

    plain_scenarios, _ = simulate_joint_scenarios([base])
    shifted_scenarios, _ = simulate_joint_scenarios([delayed])

    assert len(plain_scenarios) == len(shifted_scenarios)
    for plain, shifted in zip(plain_scenarios, shifted_scenarios):
        assert shifted[0] - plain[0] == pytest.approx(300.0)


def _record(hour: int, actual: int, scheduled: int = 100) -> TimingRecord:
    query = datetime(2026, 10, 21, hour, 0, tzinfo=IST)
    return TimingRecord(
        StretchError(
            day="2026-10-21",
            route_id=1,
            direction="UP",
            vehicle_id="v",
            run_id=f"run-{hour}",
            from_sequence=1,
            to_sequence=2,
            from_stop_id="a",
            to_stop_id="b",
            query_ts=int(query.timestamp()),
            to_ts=int((query + timedelta(seconds=actual)).timestamp()),
            scheduled_duration_s=scheduled,
            actual_duration_s=actual,
            error_s=actual - scheduled,
            from_observed=True,
            to_observed=True,
        )
    )


def _model(records: list[TimingRecord], tmp_path: Path) -> RetimeModel:
    model = RetimeModel(tmp_path, selection_path=tmp_path / "no-selection.json")
    model.index_records(records)
    return model


def test_fallback_chain_historical_then_timetable(tmp_path: Path) -> None:
    model = _model([_record(9, 140), _record(23, 70)], tmp_path)
    peak = model.predict_ride(99, "x", "y", 1000, datetime(2026, 10, 22, 9, tzinfo=IST), "replay")
    quiet = model.predict_ride(99, "x", "y", 1000, datetime(2026, 10, 22, 23, tzinfo=IST), "replay")
    assert peak.duration_s == 1400
    assert quiet.duration_s == 700
    assert peak.source == quiet.source == "historical"
    assert peak.observed_coverage == quiet.observed_coverage == 0.0

    empty = _model([], tmp_path)
    estimate = empty.predict_ride(1, "a", "b", 500, datetime(2026, 10, 22, 9, tzinfo=IST), "replay")
    assert estimate.duration_s == 500
    assert estimate.source == "timetable"
    assert estimate.observed_coverage == 0.0


def test_retime_model_loads_rung4_artifact_and_times_covered_vs_uncovered_routes(tmp_path: Path) -> None:
    import json

    import pyarrow as pa
    import pyarrow.parquet as pq

    from delhi_transit.data.stop_passages import PASSAGE_SCHEMA
    from delhi_transit.model.artifact import (
        artifact_cutoff_metadata,
        save_model_artifact,
    )
    from delhi_transit.model.features import (
        PassageRow,
        build_replay_queries,
        build_stretch_errors,
    )
    from delhi_transit.model.lgbm import Rung4QuantileLightGBMModel

    def make_day(day: str, err: int, runs: int = 8) -> list[PassageRow]:
        y, m, d = (int(p) for p in day.split("-"))
        t0 = int(datetime(y, m, d, 8, 0, tzinfo=IST).timestamp())
        coords = [(28.500, 77.250), (28.505, 77.250), (28.510, 77.250), (28.515, 77.250)]
        rows: list[PassageRow] = []
        for r in range(runs):
            ts = t0 + r * 600
            sched = ts
            for seq, (lat, lng) in enumerate(coords):
                rows.append(
                    PassageRow(
                        day=day,
                        route_id=2171,
                        direction="UP",
                        vehicle_id=f"V{r}",
                        run_id=f"{day}_R{r}",
                        stop_sequence=seq,
                        stop_id=f"S{seq}",
                        observed=True,
                        ts=ts,
                        scheduled_ts=sched,
                        stop_lat=lat,
                        stop_lng=lng,
                        schedule_error_s=ts - sched,
                        distance_m=10.0,
                    )
                )
                sched += 100
                ts += 100 + err
        return rows

    fit_p = make_day("2026-10-05", 45, runs=10) + make_day("2026-10-06", 55, runs=10)
    cal_p = make_day("2026-10-15", 50, runs=8)
    eval_p = make_day("2026-10-22", 60, runs=6)

    fit_s = build_stretch_errors(fit_p)
    fit_q = build_replay_queries(fit_s, fit_p)
    cal_q = build_replay_queries(build_stretch_errors(cal_p), cal_p)
    rung4 = Rung4QuantileLightGBMModel(n_estimators=20, num_leaves=7, min_child_samples=5).fit(
        fit_s, fit_q, cal_q
    )

    passages_dir = tmp_path / "passages"
    passages_dir.mkdir(parents=True)
    records = [
        {
            "day": p.day,
            "route_id": p.route_id,
            "direction": p.direction,
            "vehicle_id": p.vehicle_id,
            "run_id": p.run_id,
            "stop_sequence": p.stop_sequence,
            "stop_id": p.stop_id,
            "stop_name": p.stop_id,
            "stop_lat": p.stop_lat,
            "stop_lng": p.stop_lng,
            "source": "observed",
            "observed": p.observed,
            "ts": p.ts,
            "scheduled_ts": p.scheduled_ts,
            "schedule_error_s": p.effective_schedule_error_s,
            "distance_m": p.distance_m,
            "available_at": p.source_available_at,
        }
        for p in eval_p
    ]
    pq.write_table(pa.Table.from_pylist(records, schema=PASSAGE_SCHEMA), passages_dir / "day=2026-10-22.parquet")

    save_model_artifact(
        rung4,
        tmp_path / "model",
        covered_routes=[2171],
        extra_metadata=artifact_cutoff_metadata(
            training_days=("2026-10-05", "2026-10-06"),
            calibration_days=("2026-10-15",),
            latest_training_available_at=max(p.source_available_at for p in fit_p),
            latest_calibration_available_at=max(p.source_available_at for p in cal_p),
        ),
    )
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps({"selected_rung": "4", "stratum": "all-stop"}), encoding="utf-8")
    with pytest.raises(ValueError, match="all-stop"):
        RetimeModel(tmp_path, selection_path=selection_path)
    selection_path.write_text(json.dumps({"selected_rung": "4", "stratum": "observed-only"}), encoding="utf-8")

    # Also write live route stats for route 2171 and uncovered route 1920
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True)
    seen_at = int(time.time()) - 10
    (live_dir / "route_stats.json").write_text(
        json.dumps(
            {
                "routes": {
                    "2171": {"route_id": 2171, "vehicle_count": 5, "fresh_entities": [[seen_at, 4.2]] * 5},
                    "1920": {"route_id": 1920, "vehicle_count": 4, "fresh_entities": [[seen_at, 5.0]] * 4},
                }
            }
        ),
        encoding="utf-8",
    )

    model = RetimeModel(tmp_path, selection_path=selection_path)
    assert model.selected_rung == "4"

    # 1. Covered route 2171 multi-stop leg in October replay -> actually timed by rung 4
    depart_replay = datetime(2026, 10, 22, 9, 15, tzinfo=IST)
    est_covered = model.predict_ride(2171, "S0", "S3", 300.0, depart_replay, "replay")
    assert est_covered.source == "replay"
    assert est_covered.timing_source == "model_replay"
    assert est_covered.coverage_note is not None
    assert "rung 4 LightGBM" in est_covered.coverage_note
    assert est_covered.p10_s <= est_covered.duration_s <= est_covered.p90_s
    assert est_covered.duration_s > 360.0  # learned +50s/stretch error across 3 stretches

    # 2. Covered route 2171 in live mode -> actually timed by rung 4 + live VehiclePositions
    depart_live = datetime(2026, 11, 24, 9, 0, tzinfo=IST)
    est_live_cov = model.predict_ride(2171, "S0", "S2", 200.0, depart_live, "live")
    assert est_live_cov.source == "live"
    assert est_live_cov.timing_source == "model_live"
    assert est_live_cov.coverage_note is not None
    assert "rung 4 LightGBM" in est_live_cov.coverage_note
    assert est_live_cov.p10_s <= est_live_cov.duration_s <= est_live_cov.p90_s

    # 3. Uncovered route 1920 in live mode -> live + corridor historical fallback (never claims rung 4 timed it)
    est_live_uncov = model.predict_ride(1920, "X", "Y", 600.0, depart_live, "live")
    assert est_live_uncov.source == "live"
    assert est_live_uncov.timing_source == "model_live"
    assert est_live_uncov.coverage_note is not None
    assert "outside rung 4 model scope" in est_live_uncov.coverage_note
    assert "rung 4 LightGBM" not in est_live_uncov.coverage_note

    # 4. Uncovered route 1920 in replay mode -> historical fallback at hour 8, timetable fallback at hour 23
    est_replay_uncov = model.predict_ride(1920, "X", "Y", 600.0, datetime(2026, 10, 22, 8, 45, tzinfo=IST), "replay")
    assert est_replay_uncov.source == "historical"
    assert est_replay_uncov.timing_source == "historical_fallback"
    assert est_replay_uncov.coverage_note is not None
    assert "outside rung 4 model scope" in est_replay_uncov.coverage_note

    est_replay_tt = model.predict_ride(1920, "X", "Y", 600.0, datetime(2026, 10, 22, 23, 0, tzinfo=IST), "replay")
    assert est_replay_tt.source == "timetable"
    assert est_replay_tt.timing_source == "timetable_fallback"


def _same_day_record(
    day: str,
    hour: int,
    minute: int,
    actual: int,
    *,
    scheduled: int = 100,
    route_id: int = 1616,
    from_stop: str = "A",
    to_stop: str = "B",
    available_at: datetime | None = None,
    run_id: str | None = None,
) -> TimingRecord:
    y, m, d = (int(p) for p in day.split("-"))
    query = datetime(y, m, d, hour, minute, tzinfo=IST)
    to_dt = query + timedelta(seconds=actual)
    avail_dt = available_at or to_dt
    return TimingRecord(
        StretchError(
            day=day,
            route_id=route_id,
            direction="UP",
            vehicle_id="v1",
            run_id=run_id or f"{day}-{hour:02d}{minute:02d}",
            from_sequence=1,
            to_sequence=2,
            from_stop_id=from_stop,
            to_stop_id=to_stop,
            query_ts=int(query.timestamp()),
            to_ts=int(to_dt.timestamp()),
            scheduled_duration_s=scheduled,
            actual_duration_s=actual,
            error_s=actual - scheduled,
            from_observed=True,
            to_observed=True,
            from_available_at=int(query.timestamp()),
            to_available_at=int(avail_dt.timestamp()),
        )
    )


def test_kalman_filter_converges_monotonically_toward_observations(tmp_path: Path) -> None:
    """A stretch seeded at 1.0 with observations at 1.5 converges monotonically toward 1.5."""
    records = [
        _same_day_record("2026-10-22", 8, 5 * i, actual=150, scheduled=100, run_id=f"r{i}")
        for i in range(6)
    ]
    model = _model(records, tmp_path)

    # Query before any observation is available: no-data fallback (ratio 1.0).
    ratios: list[float] = [1.0]
    for i in range(6):
        query_time = datetime(2026, 10, 22, 8, 5 * i, tzinfo=IST) + timedelta(seconds=200)
        est = model.predict_ride(1616, "A", "B", 100.0, query_time, "replay")
        assert est.source == "replay"
        assert est.timing_source == "model_replay"
        assert est.observation_count == i + 1
        assert est.coverage_note is not None
        assert "Kalman ratio filter" in est.coverage_note
        assert f"{i + 1} same-day" in est.coverage_note
        ratios.append(est.duration_s / 100.0)

    for prev_ratio, next_ratio in pairwise(ratios):
        assert prev_ratio < next_ratio < 1.5
    assert ratios[-1] == pytest.approx(1.5, abs=0.01)


def test_kalman_filter_strict_causality_blocks_future_available_at(tmp_path: Path) -> None:
    """An observation with available_at after the query time must not affect the prediction."""
    past = _same_day_record("2026-10-22", 8, 0, actual=120, scheduled=100, run_id="past")
    # Completed physically at 08:13 but only knowable from GPS at 08:40 (> 08:30 query).
    future_avail = _same_day_record(
        "2026-10-22",
        8,
        10,
        actual=250,
        scheduled=100,
        available_at=datetime(2026, 10, 22, 8, 40, tzinfo=IST),
        run_id="delayed",
    )
    query_t = datetime(2026, 10, 22, 8, 30, tzinfo=IST)

    base_model = _model([past], tmp_path)
    with_future_model = _model([past, future_avail], tmp_path)

    est_base = base_model.predict_ride(1616, "A", "B", 100.0, query_t, "replay")
    est_future = with_future_model.predict_ride(1616, "A", "B", 100.0, query_t, "replay")
    assert est_future.duration_s == est_base.duration_s
    assert est_future.observation_count == 1

    # Querying a later time (08:45) sees the delayed observation, and then querying 08:30
    # again on the same RetimeModel instance must still not leak the 08:40 observation.
    est_later = with_future_model.predict_ride(
        1616, "A", "B", 100.0, datetime(2026, 10, 22, 8, 45, tzinfo=IST), "replay"
    )
    assert est_later.duration_s > est_base.duration_s
    assert est_later.observation_count == 2

    est_back = with_future_model.predict_ride(1616, "A", "B", 100.0, query_t, "replay")
    assert est_back.duration_s == est_base.duration_s
    assert est_back.observation_count == 1


def test_kalman_filter_rolls_over_daily_from_prior(tmp_path: Path) -> None:
    """A new service_day starts from the historical prior rather than yesterday's state."""
    # Day 1 (Oct 21): historical baseline at 1.2x.
    day1_prior = _same_day_record("2026-10-21", 8, 0, actual=120, scheduled=100, run_id="d1")
    # Day 2 (Oct 22): extreme congestion at 2.8x late in the day (hour 18).
    day2_spike = [
        _same_day_record("2026-10-22", 18, 5 * i, actual=280, scheduled=100, run_id=f"d2-{i}")
        for i in range(4)
    ]
    model = _model([day1_prior, *day2_spike], tmp_path)

    # Late on Oct 22, filter state tracks the 2.8x congestion.
    est_day2 = model.predict_ride(
        1616, "A", "B", 100.0, datetime(2026, 10, 22, 18, 30, tzinfo=IST), "replay"
    )
    assert est_day2.duration_s > 250.0

    # Next morning on Oct 23 (hour 8, no same-day obs yet): rolls over cleanly to the
    # hour-8 historical prior (1.2x), NOT Oct 22's 2.8x filter state.
    est_day3 = model.predict_ride(
        1616, "A", "B", 100.0, datetime(2026, 10, 23, 8, 30, tzinfo=IST), "replay"
    )
    assert est_day3.source == "historical"
    assert est_day3.timing_source == "historical_fallback"
    assert est_day3.duration_s == pytest.approx(120.0)
    assert model.kalman_state[(1616, "A", "B", "2026-10-23")] == pytest.approx(1.2)
    assert model.kalman_state[(1616, "A", "B", "2026-10-22")] > 2.5


def test_kalman_filter_no_data_degrades_to_timetable_fallback(tmp_path: Path) -> None:
    """With no same-day and no historical records, duration == scheduled and timetable_fallback."""
    model = _model([], tmp_path)
    est = model.predict_ride(1616, "A", "B", 420.0, datetime(2026, 10, 22, 9, 0, tzinfo=IST), "replay")
    assert est.duration_s == 420.0
    assert est.source == "timetable"
    assert est.timing_source == "timetable_fallback"
    assert est.observation_count == 0
    assert est.observed_coverage == 0.0
    assert (1616, "A", "B", "2026-10-22") not in model.kalman_state


def test_717a_artifact_path_is_unaffected_by_kalman_filter(tmp_path: Path) -> None:
    """Route 717A with an eligible artifact never invokes or populates the Kalman filter."""
    from delhi_transit.model.rungs import Prediction

    records = [
        _same_day_record(
            "2026-10-22", 8, 5, actual=200, scheduled=100, route_id=2171, from_stop="S0", to_stop="S1"
        )
    ]
    model = _model(records, tmp_path)
    model.selected_rung = "4"
    model.route_patterns[2171] = ("UP", [(1, "S0"), (2, "S1")])
    model.stretch_typical_scheduled[(2171, "S0", "S1")] = 100.0

    class Stub717Artifact:
        rung = "4"
        covered_routes = frozenset({2170, 2171})

        @staticmethod
        def is_eligible_as_of(as_of_ts: int) -> bool:
            return True

        @staticmethod
        def predict_batch_from_features(items: list) -> list[Prediction]:
            return [Prediction(error_s=35.0, p10_s=10.0, p50_s=35.0, p90_s=70.0) for _ in items]

    model._artifact = Stub717Artifact()
    est = model.predict_ride(2171, "S0", "S1", 100.0, datetime(2026, 10, 22, 8, 30, tzinfo=IST), "replay")
    assert est.duration_s == pytest.approx(135.0)
    assert est.timing_source == "model_replay"
    assert est.coverage_note is not None
    assert "rung 4 LightGBM" in est.coverage_note
    assert "Kalman" not in est.coverage_note
    assert model.kalman_state == {}

