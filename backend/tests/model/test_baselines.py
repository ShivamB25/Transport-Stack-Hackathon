from __future__ import annotations

from datetime import datetime

import pytest

from delhi_transit.model.evaluate import ROUTES, select_stratum, summarize
from delhi_transit.model.features import (
    IST,
    PassageRow,
    ReplayQuery,
    StretchError,
    build_replay_queries,
    build_stretch_errors,
)
from delhi_transit.model.reports import METRIC_FIELDS, persistence_gate
from delhi_transit.model.rungs import BaselineModel, Prediction


def passage(
    run_id: str,
    sequence: int,
    ts: int,
    scheduled_ts: int,
    observed: bool = True,
) -> PassageRow:
    return PassageRow(
        day="2026-10-22",
        route_id=2171,
        direction="UP",
        vehicle_id="vehicle",
        run_id=run_id,
        stop_sequence=sequence,
        stop_id=f"stop-{sequence}",
        observed=observed,
        ts=ts,
        scheduled_ts=scheduled_ts,
    )


def stretch(
    run_id: str,
    query_ts: int,
    to_ts: int,
    error_s: int,
    *,
    day: str = "2026-10-22",
    route_id: int = 2171,
    from_observed: bool = True,
    to_observed: bool = True,
) -> StretchError:
    return StretchError(
        day=day,
        route_id=route_id,
        direction=ROUTES[route_id],
        vehicle_id=f"vehicle-{run_id}",
        run_id=run_id,
        from_sequence=1,
        to_sequence=2,
        from_stop_id="origin",
        to_stop_id="destination",
        query_ts=query_ts,
        to_ts=to_ts,
        scheduled_duration_s=100,
        actual_duration_s=100 + error_s,
        error_s=error_s,
        from_observed=from_observed,
        to_observed=to_observed,
    )


def query(item: StretchError, previous_error_s: int = 0) -> ReplayQuery:
    return ReplayQuery(item, previous_error_s, "previous-run")


def test_stretch_error_table_uses_adjacent_stop_durations() -> None:
    rows = [
        passage("run", 1, 10, 0),
        passage("run", 2, 85, 60, observed=False),
        passage("run", 3, 150, 120),
    ]
    result = build_stretch_errors(reversed(rows))
    assert len(result) == 2
    assert result[0].scheduled_duration_s == 60
    assert result[0].actual_duration_s == 75
    assert result[0].error_s == 15
    assert not result[0].target_observed
    assert result[1].error_s == 5


def test_replay_uses_latest_completed_run_not_future_run() -> None:
    rows = [
        stretch("a", 100, 110, 10),
        stretch("b", 105, 115, 20),
        stretch("c", 120, 130, 30),
    ]
    result = build_replay_queries(rows)
    assert len(result) == 1
    assert result[0].stretch.run_id == "c"
    assert result[0].previous_run_id == "b"
    assert result[0].previous_error_s == 20


def test_replay_rejects_equal_timestamp_and_cross_day_leakage() -> None:
    rows = [
        stretch("a", 100, 110, 10),
        stretch("b", 110, 120, 20),
        stretch("other-day", 50, 60, 99, day="2026-10-21"),
    ]
    assert build_replay_queries(rows) == []


def test_baseline_rungs() -> None:
    hour_eight = int(datetime(2026, 10, 5, 8, tzinfo=IST).timestamp())
    training = [stretch("a", hour_eight, hour_eight + 100, 10)]
    item = stretch("target", hour_eight + 200, hour_eight + 300, 40)
    replay = query(item, previous_error_s=17)
    assert BaselineModel("0").fit(training).predict(replay).error_s == 0
    assert BaselineModel("1").fit(training).predict(replay).error_s == 17
    for removed in ("1h", "4"):
        with pytest.raises(ValueError, match="unknown baseline rung"):
            BaselineModel(removed)


def test_metric_values_and_coverage_columns() -> None:
    queries = [
        query(stretch("a", 100, 200, 10)),
        query(stretch("b", 300, 400, 70, to_observed=False)),
    ]
    row = summarize("2026-10-22", 2171, "0", queries, [Prediction(0), Prediction(0)])
    assert row["mae_s"] == 40
    assert row["median_ae_s"] == 40
    assert row["p90_ae_s"] == 64
    assert row["share_gt_60_pct"] == 50
    assert row["observed_queries"] == 1
    assert row["interpolated_queries"] == 1
    assert row["observed_coverage_pct"] == 50
    assert row["interpolated_coverage_pct"] == 50
    assert row["stratum"] == "all-stop"
    assert set(METRIC_FIELDS) == set(row)


def test_observed_only_stratum_excludes_interpolated_targets() -> None:
    queries = [
        query(stretch("a", 100, 200, 10)),
        query(stretch("b", 300, 400, 70, to_observed=False)),
    ]
    selected = select_stratum(queries, "observed-only")
    assert selected == queries[:1]
    row = summarize(
        "2026-10-22",
        2171,
        "0",
        selected,
        [Prediction(0)],
        stratum="observed-only",
    )
    assert row["queries"] == 1
    assert row["mae_s"] == 10
    assert row["observed_coverage_pct"] == 100
    assert row["interpolated_queries"] == 0
    with pytest.raises(ValueError, match="directly observed"):
        summarize(
            "2026-10-22",
            2171,
            "0",
            queries,
            [Prediction(0), Prediction(0)],
            stratum="observed-only",
        )


def test_gate_is_strict_and_independent_by_direction() -> None:
    rows = []
    for direction, route_id, rung, mae in [
        ("UP", 2171, "0", 50),
        ("UP", 2171, "1", 49),
        ("DOWN", 2170, "0", 50),
        ("DOWN", 2170, "1", 50),
    ]:
        rows.append(
            {
                "day": "2026-10-22",
                "direction": direction,
                "route_id": route_id,
                "rung": rung,
                "stratum": "observed-only",
                "mae_s": mae,
            }
        )
    assert persistence_gate(rows) == {"UP": True, "DOWN": False}


def test_gate_ignores_all_stop_metrics() -> None:
    rows = []
    for direction, route_id in (("UP", 2171), ("DOWN", 2170)):
        for stratum, timetable_mae, persistence_mae in (
            ("all-stop", 100, 1),
            ("observed-only", 50, 60 if direction == "UP" else 49),
        ):
            for rung, mae in (("0", timetable_mae), ("1", persistence_mae)):
                rows.append(
                    {
                        "day": "2026-10-22",
                        "direction": direction,
                        "route_id": route_id,
                        "rung": rung,
                        "stratum": stratum,
                        "mae_s": mae,
                    }
                )
    assert persistence_gate(rows) == {"UP": False, "DOWN": True}

