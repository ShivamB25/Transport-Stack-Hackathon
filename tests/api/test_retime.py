from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ps1.features import StretchError
from ps1.retime import RetimeModel, TimingRecord, scenario_windows

IST = timezone(timedelta(hours=5, minutes=30))


def test_joint_windows_do_not_add_leg_quantiles() -> None:
    scenarios = [[10.0, 20.0], [20.0, 10.0], [15.0, 15.0]]
    windows = scenario_windows(scenarios)
    assert windows["total_window_p90_s"] == 30.0
    assert windows["total_window_p10_s"] == 30.0
    assert sum(leg["window_p90_s"] for leg in windows["legs"]) == 38.0
    assert windows["total_window_p90_s"] != sum(
        leg["window_p90_s"] for leg in windows["legs"]
    )


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


def _model(records: list[TimingRecord]) -> RetimeModel:
    model = RetimeModel.__new__(RetimeModel)
    model.data_dir = Path("/unused")
    model.selected_rung = "1"
    model.records = records
    model.exact = defaultdict(list)
    model.route_hour = defaultdict(list)
    model.global_hour = defaultdict(list)
    model.stop_events = defaultdict(list)
    for record in records:
        stretch = record.stretch
        model.exact[(stretch.route_id, stretch.from_stop_id, stretch.to_stop_id)].append(record)
        model.route_hour[(stretch.route_id, record.hour)].append(record)
        model.global_hour[record.hour].append(record)
    return model


def test_observed_historical_profile_changes_time_of_day() -> None:
    model = _model([_record(9, 140), _record(23, 70)])
    peak = model.predict_ride(99, "x", "y", 1000, datetime(2026, 10, 22, 9, tzinfo=IST), "replay")
    quiet = model.predict_ride(99, "x", "y", 1000, datetime(2026, 10, 22, 23, tzinfo=IST), "replay")
    assert peak.duration_s == 1400
    assert quiet.duration_s == 700
    assert peak.source == quiet.source == "historical"
    assert peak.observed_coverage == quiet.observed_coverage == 0.0


def test_timetable_is_explicit_last_fallback() -> None:
    model = _model([])
    estimate = model.predict_ride(1, "a", "b", 500, datetime(2026, 10, 22, 9, tzinfo=IST), "replay")
    assert estimate.duration_s == 500
    assert estimate.source == "timetable"
    assert estimate.observed_coverage == 0.0
