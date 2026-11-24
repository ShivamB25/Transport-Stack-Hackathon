"""Synthetic and small real-data checks for geometric stop reconstruction."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from delhi_transit.core.config import IST
from delhi_transit.data.coverage_report import (
    CANONICAL_COVERAGE_REPORT,
    SCOPED_DAYS,
    resolve_report_path,
)
from delhi_transit.data.gtfs_patterns import (
    PatternStop,
    RoutePattern,
    load_route_patterns,
)
from delhi_transit.data.stop_passages import (
    Anchor,
    Ping,
    build_passages,
    drop_time_order_breaking_anchors,
    reconstruct_day,
    reconstruct_run,
    schedule_start_for_run,
    select_minimum_distance_anchors,
)
from delhi_transit.model.features import (
    build_replay_queries,
    build_stretch_errors,
    load_passages,
)


def stop(index: int, schedule_s: int = 0, lat: float | None = None) -> PatternStop:
    return PatternStop(
        stop_id=str(index),
        stop_name=f"Stop {index}",
        lat=28.6 if lat is None else lat,
        lng=77.2 + index * 0.001,
        sequence=index,
        schedule_s=schedule_s,
    )


def test_anchor_selection_radius_and_min_distance():
    stops = [stop(0)]
    pings = [
        Ping(100, 28.6000, 77.20045),
        Ping(200, 28.6000, 77.20005),
        Ping(300, 28.6000, 77.20020),
    ]
    anchors = select_minimum_distance_anchors(pings, stops, radius_m=60)
    assert len(anchors) == 1
    assert anchors[0].ts == 200
    assert anchors[0].distance_m == pytest.approx(5, abs=0.2)

    pings = [Ping(100, 28.61, 77.2)]
    assert select_minimum_distance_anchors(pings, [stop(0)], radius_m=60) == []


def test_anchor_chain_ordering():
    anchors = [
        Anchor(0, 100, 5),
        Anchor(1, 500, 5),
        Anchor(2, 200, 5),
        Anchor(3, 300, 5),
    ]
    kept = drop_time_order_breaking_anchors(anchors)
    assert [anchor.stop_index for anchor in kept] == [0, 2, 3]

    # Maximum chain; equal timestamps are allowed.
    anchors = [
        Anchor(0, 100, 5),
        Anchor(1, 100, 5),
        Anchor(2, 150, 5),
        Anchor(3, 120, 5),
    ]
    kept = drop_time_order_breaking_anchors(anchors)
    assert [anchor.stop_index for anchor in kept] == [0, 1, 2]


def test_schedule_proportional_interpolation_and_endpoint_extrapolation():
    stops = [stop(0, 0), stop(1, 60), stop(2, 120), stop(3, 180)]
    anchors = [Anchor(1, 1_000, 5), Anchor(2, 1_180, 5)]
    passages = build_passages(stops, anchors, schedule_start_ts=500)
    assert [passage.ts for passage in passages] == [940, 1_000, 1_180, 1_240]
    assert [passage.source for passage in passages] == [
        "interpolated",
        "observed",
        "observed",
        "interpolated",
    ]
    assert passages[1].schedule_error_s == 440
    assert passages[0].schedule_error_s == 440

    interior = build_passages(
        [stop(i, i * 60) for i in range(5)],
        [Anchor(0, 100, 5), Anchor(4, 700, 5)],
        schedule_start_ts=0,
    )
    assert [p.ts for p in interior] == [100, 250, 400, 550, 700]
    assert [p.observed for p in interior] == [True, False, False, False, True]


def test_reconstruction_requires_two_ordered_anchors():
    with pytest.raises(ValueError, match="at least two"):
        build_passages([stop(0), stop(1, 60)], [Anchor(0, 100, 5)], 0)


def test_available_at_semantics_observed_interpolated_and_extrapolated():
    stops = [stop(i, i * 60) for i in range(6)]
    anchors = [Anchor(1, 1_000, 5.0), Anchor(4, 1_450, 5.0)]
    passages = build_passages(stops, anchors, schedule_start_ts=940)

    assert [p.source for p in passages] == [
        "interpolated",  # 0: leading extrapolated from anchor 1
        "observed",      # 1: observed anchor
        "interpolated",  # 2: interpolated between anchor 1 and anchor 4
        "interpolated",  # 3: interpolated between anchor 1 and anchor 4
        "observed",      # 4: observed anchor
        "interpolated",  # 5: trailing extrapolated from anchor 4
    ]
    assert [p.ts for p in passages] == [940, 1_000, 1_150, 1_300, 1_450, 1_510]
    # The run only qualifies once its second anchor (stop 4 at 1_450) exists.
    assert [p.available_at for p in passages] == [1_450] * 6

    anchors = [Anchor(1, 1_000, 5.0), Anchor(2, 1_100, 5.0), Anchor(4, 1_450, 5.0)]
    passages = build_passages(stops, anchors, schedule_start_ts=940)
    assert [p.available_at for p in passages] == [
        1_100,  # leading extrapolated from anchor 1, held until the second anchor
        1_100,  # observed at 1_000, held until the second anchor
        1_100,  # observed -> own ping ts
        1_450,  # interpolated -> later anchor (4) ts
        1_450,  # observed -> own ping ts
        1_450,  # trailing extrapolated endpoint -> anchor 4 used
    ]


def test_midnight_crossing_resolves_adjacent_service_date_oct22_regression():
    """Regression: Oct 22 UP run 2171_23_57_4191 stop 2 must not get a -80,064 s error."""
    stops = (
        stop(0, 0),
        stop(1, 120),
        stop(2, 270),
        stop(3, 352),
        stop(4, 5473),
    )
    pattern = RoutePattern(
        route_id=2171,
        direction="UP",
        shape_id="shp_3_271",
        stops=stops,
        timetable={
            4 * 3600 + 40 * 60: "2171_04_40",
            22 * 3600 + 10 * 60: "2171_22_10",
        },
    )

    # Exact Oct 22 counterexample: raw pings start 2026-10-21 23:59:44 IST, stop 2 at 2026-10-22 00:00:06 IST
    ping0_ts = int(datetime(2026, 10, 21, 23, 59, 44, tzinfo=IST).timestamp())
    stop2_ts = int(datetime(2026, 10, 22, 0, 0, 6, tzinfo=IST).timestamp())
    stop3_ts = int(datetime(2026, 10, 22, 0, 1, 1, tzinfo=IST).timestamp())
    pings = [Ping(ping0_ts, 28.6, 77.2)]
    anchors = [Anchor(2, stop2_ts, 11.0), Anchor(3, stop3_ts, 11.1)]

    sched_start = schedule_start_for_run(pattern, "2171_23_57_4191", pings, "2026-10-22", anchors=anchors)
    expected_start = int(datetime(2026, 10, 21, 22, 10, 0, tzinfo=IST).timestamp())
    assert sched_start == expected_start
    passages = build_passages(stops, anchors, sched_start)
    assert passages[2].schedule_error_s == 6_336

    # Hinted-start midnight crossing: run_id has exact timetable start 22:10 from previous service date
    stop4_ts = int(datetime(2026, 10, 28, 0, 0, 28, tzinfo=IST).timestamp())
    hinted_anchors = [Anchor(3, stop4_ts - 5100, 10.0), Anchor(4, stop4_ts, 10.0)]
    hinted_start = schedule_start_for_run(
        pattern, "2171_22_10_9999", [Ping(stop4_ts - 5200, 28.6, 77.2)], "2026-10-28", anchors=hinted_anchors
    )
    assert hinted_start == int(datetime(2026, 10, 27, 22, 10, 0, tzinfo=IST).timestamp())


def test_run_without_second_anchor_before_query_is_invisible_oct22_regression(tmp_path: Path):
    """Regression: Oct 22 DOWN 2170_06_09_7096 at q=1792636652 must not persist 2170_08_04.

    2170_08_04 had one anchor (1792636203) before q and its second (1792636760) after q, yet
    its leading-extrapolated stretch (error 0 s) was published as available at the first anchor.
    """
    query_ts = 1_792_636_652
    stops = tuple(stop(i, 60 * i) for i in range(4))
    pattern = RoutePattern(
        route_id=2170,
        direction="DOWN",
        shape_id="shp",
        stops=stops,
        timetable={6 * 3600 + 9 * 60: "a", 7 * 3600 + 50 * 60: "b", 8 * 3600 + 4 * 60: "c"},
    )
    older_start = query_ts - 1_000
    runs = {
        ("DL1PD0001", "2170_07_50"): [(0, older_start), (1, older_start + 145), (2, older_start + 205), (3, older_start + 265)],
        ("DL1PD8438", "2170_08_04"): [(2, 1_792_636_203), (3, 1_792_636_760)],
        ("DL1PD0002", "2170_06_09_7096"): [(0, query_ts), (1, query_ts + 150), (2, query_ts + 210), (3, query_ts + 270)],
    }
    gps_rows = [
        {"route_id": 2170, "vehicle_id": vehicle, "trip_id": run_id, "ts": ts,
         "lat": stops[index].lat, "lng": stops[index].lng}
        for (vehicle, run_id), observations in runs.items()
        for index, ts in observations
    ]
    gps_path = tmp_path / "gps.parquet"
    pq.write_table(pa.Table.from_pylist(gps_rows), gps_path)

    late_pings = [Ping(ts, stops[i].lat, stops[i].lng) for i, ts in runs[("DL1PD8438", "2170_08_04")]]
    prefix = [ping for ping in late_pings if ping.ts < query_ts]
    assert reconstruct_run(pattern, "2170_08_04", prefix, "2026-10-22") is None
    full = reconstruct_run(pattern, "2170_08_04", late_pings, "2026-10-22")
    assert full is not None and min(p.available_at for p in full) == 1_792_636_760

    passages_path = tmp_path / "passages.parquet"
    reconstruct_day("2026-10-22", gps_path, {2170: pattern}, passages_path)
    passages = load_passages(passages_path)
    queries = build_replay_queries(build_stretch_errors(passages), passages)
    target = next(
        q for q in queries
        if q.stretch.run_id == "2170_06_09_7096" and q.stretch.from_sequence == 0
    )
    assert target.stretch.query_ts == query_ts
    assert (target.previous_run_id, target.previous_error_s) == ("2170_07_50", 85)


def test_scoped_run_does_not_overwrite_canonical_coverage_report(tmp_path: Path):
    full_path = resolve_report_path(None, SCOPED_DAYS, {2170, 2171}, tmp_path)
    assert full_path == CANONICAL_COVERAGE_REPORT

    scoped_path = resolve_report_path(None, ["2026-10-22"], {2170, 2171}, tmp_path)
    assert scoped_path != CANONICAL_COVERAGE_REPORT
    assert scoped_path.parent == tmp_path

    route_scoped_path = resolve_report_path(None, SCOPED_DAYS, {2171}, tmp_path)
    assert route_scoped_path != CANONICAL_COVERAGE_REPORT

    with pytest.raises(ValueError, match="refusing to overwrite canonical coverage report"):
        resolve_report_path(CANONICAL_COVERAGE_REPORT, ["2026-10-22"], {2170, 2171}, tmp_path)

    explicit_custom = tmp_path / "custom.md"
    assert resolve_report_path(explicit_custom, ["2026-10-22"], {2170, 2171}, tmp_path) == explicit_custom


@pytest.mark.skipif(
    not Path("/home/factory-user/ps1-scratch/data/gps/day=2026-10-22.parquet").exists(),
    reason="real October Parquet is not available",
)
def test_small_real_data_smoke(tmp_path: Path):
    data_dir = Path("/home/factory-user/ps1-scratch/data")
    patterns = load_route_patterns(data_dir / "gtfs", [2170, 2171])
    assert patterns[2170].direction == "DOWN"
    assert patterns[2171].direction == "UP"
    assert len(patterns[2170].stops) == 46
    assert len(patterns[2171].stops) == 52

    con = duckdb.connect()
    run = con.execute(
        """
        select trip_id from read_parquet(?)
        where route_id = 2171
        group by trip_id having count(*) > 300
        order by trip_id limit 1
        """,
        [str(data_dir / "gps" / "day=2026-10-22.parquet")],
    ).fetchone()[0]
    con.close()

    source = tmp_path / "one-run.parquet"
    con = duckdb.connect()
    con.execute(
        "copy (select * from read_parquet(?) where route_id = 2171 and trip_id = ?) "
        "to ? (format parquet)",
        [str(data_dir / "gps" / "day=2026-10-22.parquet"), run, str(source)],
    )
    con.close()
    out_parquet = tmp_path / "passages.parquet"
    coverage, _ = reconstruct_day(
        "2026-10-22", source, patterns, out_parquet
    )
    assert coverage
    for row in coverage:
        assert row["stops"] == row["observed"] + row["interpolated"]

    con = duckdb.connect()
    try:
        null_avail, obs_mismatch = con.execute(
            """
            select
                count(*) filter (where available_at is null),
                count(*) filter (where observed and available_at < ts)
            from read_parquet(?)
            """,
            [str(out_parquet)],
        ).fetchone()
    finally:
        con.close()
    assert null_avail == 0
    assert obs_mismatch == 0
