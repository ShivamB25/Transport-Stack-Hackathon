"""Unit tests for the one-day extraction/cleaning logic on tiny synthetic fixtures."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import duckdb
import pytest

from ps1 import config
from ps1.extract_day import (
    convert_day,
    day_db_name,
    day_key,
    default_output_path,
    parse_day,
    verify_day,
)

SCHEMA = (
    "CREATE TABLE vehicle_feed (vehicle_id TEXT, lat TEXT, lng TEXT, speed REAL, "
    "route_id REAL, trip_id TEXT, timestamp TEXT, date DATE, time TEXT)"
)


def make_raw_db(path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    con.executemany("INSERT INTO vehicle_feed VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def query_one(sql: str):
    con = duckdb.connect()
    try:
        return con.execute(sql).fetchone()
    finally:
        con.close()


# two exact-key duplicates (different trip_id), one distinct second ping for DL1,
# one out-of-bbox row, one bad-ts row, one route_id overflowing INTEGER (->NULL),
# one null-coord row
ROWS = [
    ("DL1", "28.6000", "77.2000", 5.0, 2171.0, "t1", "1792000000", "2026-10-15", "10:00:00"),
    ("DL1", "28.6000", "77.2000", 6.0, 2171.0, "t1dup", "1792000000", "2026-10-15", "10:00:00"),
    ("DL1", "28.6100", "77.2100", 4.0, 2171.0, "t1", "1792000030", "2026-10-15", "10:00:30"),
    ("DL2", "30.1000", "77.3000", 3.0, 2170.0, "t2", "1792000000", "2026-10-15", "10:00:00"),
    ("DL2", "28.7000", "77.3000", 3.0, 2170.0, "t2", "not-a-ts", "2026-10-15", "10:00:00"),
    ("DL3", "28.8000", "77.4000", 2.0, 1e18, "t3", "1792000000", "2026-10-15", "10:00:00"),
    ("DL4", None, "77.0000", 1.0, 100.0, "t4", "1792000000", "2026-10-15", "10:00:00"),
]


@pytest.fixture()
def converted(tmp_path: Path) -> tuple[Path, int, int]:
    db = tmp_path / "bus_movements_2026_10_15.db"
    make_raw_db(db, ROWS)
    out = tmp_path / "day=2026-10-15.parquet"
    rows_in, rows_out = convert_day(db, out, tmp_path)
    return out, rows_in, rows_out


def test_convert_dedupes_on_vehicle_ts_lat_lng(converted):
    out, rows_in, rows_out = converted
    assert rows_in == len(ROWS)
    # 7 raw - 1 duplicate - 1 out-of-bbox - 1 bad ts - 1 null coord = 3
    assert rows_out == 3
    duplicates = query_one(
        f"SELECT count(*) FROM (SELECT vehicle_id, ts, lat, lng FROM read_parquet('{out}') "
        "GROUP BY ALL HAVING count(*) > 1)"
    )[0]
    assert duplicates == 0


def test_convert_filters_bbox_and_bad_ts(converted):
    out, _, _ = converted
    violations = query_one(
        f"SELECT count(*) FROM read_parquet('{out}') WHERE ts IS NULL "
        f"OR lat IS NULL OR lng IS NULL "
        f"OR lat NOT BETWEEN {config.BBOX_LAT_MIN} AND {config.BBOX_LAT_MAX} "
        f"OR lng NOT BETWEEN {config.BBOX_LNG_MIN} AND {config.BBOX_LNG_MAX}"
    )[0]
    assert violations == 0


def test_convert_route_id_integer_or_null(converted):
    out, _, _ = converted
    con = duckdb.connect()
    try:
        schema = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{out}')").fetchall()
    finally:
        con.close()
    types = {name: dtype for name, dtype, *_ in schema}
    assert "INT" in types["route_id"].upper()
    assert "BIGINT" in types["ts"].upper()
    fractional = query_one(
        f"SELECT count(*) FROM read_parquet('{out}') "
        "WHERE route_id IS NOT NULL AND route_id <> CAST(route_id AS BIGINT)"
    )[0]
    assert fractional == 0
    null_route = query_one(
        f"SELECT count(*) FROM read_parquet('{out}') WHERE vehicle_id = 'DL3' AND route_id IS NULL"
    )[0]
    assert null_route == 1
    kept_route = query_one(
        f"SELECT DISTINCT route_id FROM read_parquet('{out}') WHERE vehicle_id = 'DL1'"
    )[0]
    assert kept_route == 2171


def test_convert_rejects_empty_output(tmp_path: Path):
    db = tmp_path / "empty.db"
    make_raw_db(db, [ROWS[3]])  # only an out-of-bbox row
    with pytest.raises(ValueError, match="no rows"):
        convert_day(db, tmp_path / "out.parquet", tmp_path)
    assert not (tmp_path / "out.parquet").exists()
    assert not (tmp_path / "out.tmp.parquet").exists()


def test_parse_day_variants():
    assert parse_day("5") == 5
    assert parse_day("2026-10-22") == 22
    for bad in ("0", "32", "2026-11-01", "2025-10-15"):
        with pytest.raises(ValueError):
            parse_day(bad)


def test_names_and_paths():
    assert day_key(5) == "2026-10-05"
    assert day_db_name(5) == "bus_movements_2026_10_05.db"
    assert default_output_path(Path("/x"), 5) == Path("/x/gps/day=2026-10-05.parquet")


def test_bbox_matches_spec():
    assert (config.BBOX_LAT_MIN, config.BBOX_LAT_MAX) == (27.5, 29.0)
    assert (config.BBOX_LNG_MIN, config.BBOX_LNG_MAX) == (76.7, 77.6)


def test_verify_day_rejects_undersized_or_short_day(tmp_path: Path):
    out = tmp_path / "tiny.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT 'DL1' AS vehicle_id, 2171 AS route_id, 't' AS trip_id, "
        "1792000000 AS ts, 28.6 AS lat, 77.2 AS lng, 1.0 AS speed) "
        f"TO '{out}' (FORMAT PARQUET)"
    )
    con.close()
    with pytest.raises(ValueError, match="verification failed"):
        verify_day(15, out)
