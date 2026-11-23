"""Synthetic and small real-data checks for geometric stop reconstruction."""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from ps1.stop_passages import (
    Anchor,
    PatternStop,
    Ping,
    build_passages,
    drop_time_order_breaking_anchors,
    haversine_m,
    load_route_patterns,
    reconstruct_day,
    select_minimum_distance_anchors,
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


def test_haversine_zero_and_local_scale():
    assert haversine_m(28.6, 77.2, 28.6, 77.2) == 0
    assert 97 < haversine_m(28.6, 77.2, 28.6, 77.201) < 99


def test_minimum_distance_observation_selected_per_stop():
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


def test_outside_radius_is_not_an_anchor():
    pings = [Ping(100, 28.61, 77.2)]
    assert select_minimum_distance_anchors(pings, [stop(0)], radius_m=60) == []


def test_time_order_breaking_anchor_is_dropped():
    anchors = [
        Anchor(0, 100, 5),
        Anchor(1, 500, 5),
        Anchor(2, 200, 5),
        Anchor(3, 300, 5),
    ]
    kept = drop_time_order_breaking_anchors(anchors)
    assert [anchor.stop_index for anchor in kept] == [0, 2, 3]
    assert [anchor.ts for anchor in kept] == sorted(anchor.ts for anchor in kept)


def test_maximum_anchor_chain_and_equal_timestamps_are_allowed():
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


def test_interpolation_is_monotonic_and_counts_reconcile():
    stops = [stop(i, i * 60) for i in range(5)]
    anchors = [Anchor(0, 100, 5), Anchor(4, 700, 5)]
    passages = build_passages(stops, anchors, schedule_start_ts=0)
    assert [p.ts for p in passages] == [100, 250, 400, 550, 700]
    assert sum(p.observed for p in passages) == 2
    assert sum(not p.observed for p in passages) == 3
    assert len(passages) == 5


def test_reconstruction_requires_two_ordered_anchors():
    with pytest.raises(ValueError, match="at least two"):
        build_passages([stop(0), stop(1, 60)], [Anchor(0, 100, 5)], 0)


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
    coverage, _ = reconstruct_day(
        "2026-10-22", source, patterns, tmp_path / "passages.parquet"
    )
    assert coverage
    for row in coverage:
        assert row["stops"] == row["observed"] + row["interpolated"]
