"""Unit tests for the one-day extraction/cleaning logic on tiny synthetic fixtures."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import duckdb
import pytest

from ps1 import config, extract_day
from ps1.extract_day import (
    IST,
    convert_day,
    day_db_name,
    day_key,
    default_output_path,
    parse_day,
    run_day,
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
    retained = query_one(
        f"SELECT route_id, trip_id, speed FROM read_parquet('{out}') "
        "WHERE vehicle_id = 'DL1' AND ts = 1792000000"
    )
    assert retained == (2171, "t1", 5.0)


def test_convert_dedupe_keeps_first_source_row_deterministically(tmp_path: Path):
    """Disagreeing non-key fields (route_id, trip_id, speed, NULLs) must always keep row 1."""
    rows_forward = [
        # Group A: disagreeing route_id, trip_id, speed
        ("V1", "28.6000", "77.2000", 1.5, 2171.0, "trip_first", "1792000100", "2026-10-15", "10:00:00"),
        ("V1", "28.6000", "77.2000", 9.5, 2170.0, "trip_second", "1792000100", "2026-10-15", "10:00:00"),
        ("V1", "28.6000", "77.2000", 4.5, 525.0, "trip_third", "1792000100", "2026-10-15", "10:00:00"),
        # Group B: first row has NULL route_id while later row has non-NULL
        ("V2", "28.6100", "77.2100", 2.0, None, "trip_null_route", "1792000200", "2026-10-15", "10:00:00"),
        ("V2", "28.6100", "77.2100", 8.0, 2171.0, "trip_non_null", "1792000200", "2026-10-15", "10:00:00"),
    ]
    db_fwd = tmp_path / "fwd.db"
    make_raw_db(db_fwd, rows_forward)
    out_fwd = tmp_path / "fwd.parquet"
    convert_day(db_fwd, out_fwd, tmp_path)

    assert query_one(
        f"SELECT route_id, trip_id, speed FROM read_parquet('{out_fwd}') WHERE vehicle_id = 'V1'"
    ) == (2171, "trip_first", 1.5)
    assert query_one(
        f"SELECT route_id, trip_id, speed FROM read_parquet('{out_fwd}') WHERE vehicle_id = 'V2'"
    ) == (None, "trip_null_route", 2.0)

    # Reverse source insertion order -> the new first row must win for both groups
    db_rev = tmp_path / "rev.db"
    make_raw_db(db_rev, list(reversed(rows_forward)))
    out_rev = tmp_path / "rev.parquet"
    convert_day(db_rev, out_rev, tmp_path)

    assert query_one(
        f"SELECT route_id, trip_id, speed FROM read_parquet('{out_rev}') WHERE vehicle_id = 'V1'"
    ) == (525, "trip_third", 4.5)
    assert query_one(
        f"SELECT route_id, trip_id, speed FROM read_parquet('{out_rev}') WHERE vehicle_id = 'V2'"
    ) == (2171, "trip_non_null", 8.0)


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


def _write_parquet_rows(path: Path, rows: list[tuple]) -> None:
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (vehicle_id VARCHAR, route_id INTEGER, trip_id VARCHAR, "
            "ts BIGINT, lat DOUBLE, lng DOUBLE, speed DOUBLE)"
        )
        con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?)", rows)
        con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    finally:
        con.close()


def _valid_day_rows(day: int, *, include_prev_evening: bool = True) -> list[tuple]:
    d0 = int(dt.datetime(2026, 10, day, 0, 0, 0, tzinfo=IST).timestamp())
    rows: list[tuple] = []
    if include_prev_evening:
        rows.append(("V0", 2171, "t0", d0 - 1500, 28.60, 77.20, 5.0))
    # 19 hourly points across nominal IST day (01:00..23:00, or 02:05..23:00 for day 8, 01:00..19:30 for day 7)
    if day == 7:
        offsets = [h * 3600 for h in range(1, 20)] + [19 * 3600 + 1800]
    elif day == 8:
        offsets = [2 * 3600 + 300] + [h * 3600 for h in range(3, 24)]
    else:
        offsets = [h * 3600 for h in range(1, 24)]
    for idx, off in enumerate(offsets, start=1):
        rows.append((f"V{idx}", 2171, f"t{idx}", d0 + off, 28.60 + idx * 0.001, 77.20, 6.0))
    return rows


def test_verify_day_rejects_undersized_or_short_day(tmp_path: Path):
    out = tmp_path / "tiny.parquet"
    _write_parquet_rows(out, [("DL1", 2171, "t", 1792000000, 28.6, 77.2, 1.0)])
    with pytest.raises(ValueError, match="verification failed"):
        verify_day(15, out)


def test_verify_day_accepts_previous_evening_overlap_and_oct7_oct8_gaps(tmp_path: Path):
    for day, overlap in ((15, True), (7, True), (8, False)):
        out = tmp_path / f"day=2026-10-{day:02d}.parquet"
        rows = _valid_day_rows(day, include_prev_evening=overlap)
        _write_parquet_rows(out, rows)
        verified = verify_day(day, out, min_rows=10, min_vehicles=10)
        assert verified["rows"] == len(rows)


def test_verify_day_rejects_wrong_day_observations(tmp_path: Path):
    """Oct 14 + Oct 16 rows with no Oct 15 rows must fail verification for day 15."""
    out = tmp_path / "wrong-day.parquet"
    d14 = int(dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=IST).timestamp())
    d16 = int(dt.datetime(2026, 10, 16, 12, 0, 0, tzinfo=IST).timestamp())
    rows = [(f"V{i}", 2171, "t", d14 + i * 60, 28.6, 77.2, 5.0) for i in range(10)]
    rows += [(f"V{10 + i}", 2171, "t", d16 + i * 60, 28.6, 77.2, 5.0) for i in range(10)]
    _write_parquet_rows(out, rows)
    with pytest.raises(ValueError, match="outside archive-day window"):
        verify_day(15, out, min_rows=10, min_vehicles=10)


def test_verify_day_rejects_missing_or_wrong_schema(tmp_path: Path):
    d15 = int(dt.datetime(2026, 10, 15, 10, 0, 0, tzinfo=IST).timestamp())
    missing_col = tmp_path / "missing-route-id.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT 'V1' AS vehicle_id, 't' AS trip_id, {d15}::BIGINT AS ts, "
        f"28.6::DOUBLE AS lat, 77.2::DOUBLE AS lng, 1.0::DOUBLE AS speed) "
        f"TO '{missing_col}' (FORMAT PARQUET)"
    )
    con.close()
    with pytest.raises(ValueError, match="missing columns: route_id"):
        verify_day(15, missing_col, min_rows=1, min_vehicles=1)

    wrong_type = tmp_path / "double-route-id.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT 'V1' AS vehicle_id, 2171.5::DOUBLE AS route_id, 't' AS trip_id, "
        f"{d15}::BIGINT AS ts, 28.6::DOUBLE AS lat, 77.2::DOUBLE AS lng, 1.0::DOUBLE AS speed) "
        f"TO '{wrong_type}' (FORMAT PARQUET)"
    )
    con.close()
    with pytest.raises(ValueError, match="route_id has type DOUBLE"):
        verify_day(15, wrong_type, min_rows=1, min_vehicles=1)


def test_verify_day_rejects_duplicates_and_bbox_and_corrupt(tmp_path: Path):
    rows = _valid_day_rows(15)
    dup_out = tmp_path / "dups.parquet"
    _write_parquet_rows(dup_out, [*rows, rows[1]])
    with pytest.raises(ValueError, match="duplicate"):
        verify_day(15, dup_out, min_rows=10, min_vehicles=10)

    bbox_out = tmp_path / "bbox.parquet"
    bad_rows = [*rows, ("VBAD", 2171, "tbad", rows[1][3], 30.5, 77.2, 5.0)]
    _write_parquet_rows(bbox_out, bad_rows)
    with pytest.raises(ValueError, match="bbox"):
        verify_day(15, bbox_out, min_rows=10, min_vehicles=10)

    corrupt = tmp_path / "corrupt.parquet"
    corrupt.write_bytes(b"not-a-valid-parquet-file")
    with pytest.raises(ValueError, match="unreadable Parquet"):
        verify_day(15, corrupt, min_rows=1, min_vehicles=1)


def test_run_day_preserves_canonical_parquet_on_extraction_and_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data_dir = tmp_path / "data"
    work_dir = tmp_path / "work"
    (data_dir / "gps").mkdir(parents=True)
    canonical = default_output_path(data_dir, 15)
    sentinel_bytes = b"VALID-CANONICAL-PARQUET-SENTINEL"
    canonical.write_bytes(sentinel_bytes)

    # 1. Extraction failure must not touch canonical Parquet
    def fail_extract(*_args, **_kwargs):
        raise OSError("simulated HEAD timeout")

    monkeypatch.setattr(extract_day, "extract_day_db", fail_extract)
    with pytest.raises(OSError, match="simulated HEAD timeout"):
        run_day(15, data_dir, work_dir)
    assert canonical.read_bytes() == sentinel_bytes
    assert list((data_dir / "gps").glob("*.staged.parquet")) == []

    # 2. Verification failure after conversion must not touch canonical Parquet
    def fake_extract(_day: int, db_path: Path, _work_dir: Path, _url: str):
        make_raw_db(db_path, ROWS)
        return 0.1, 100

    monkeypatch.setattr(extract_day, "extract_day_db", fake_extract)
    with pytest.raises(ValueError, match="verification failed"):
        run_day(15, data_dir, work_dir)
    assert canonical.read_bytes() == sentinel_bytes
    assert list((data_dir / "gps").glob("*.staged.parquet")) == []
    assert not (work_dir / day_db_name(15)).exists()

    # 3. Successful verification atomically replaces canonical Parquet
    monkeypatch.setattr(
        extract_day,
        "verify_day",
        lambda day, path: verify_day(day, path, min_rows=1, min_vehicles=1),
    )
    d15 = int(dt.datetime(2026, 10, 15, 0, 0, 0, tzinfo=IST).timestamp())
    good_raw = [
        ("V1", "28.6", "77.2", 5.0, 2171.0, "t1", str(d15 + 3600), "2026-10-15", "01:00:00"),
        ("V2", "28.6", "77.2", 5.0, 2171.0, "t2", str(d15 + 20 * 3600), "2026-10-15", "20:00:00"),
    ]
    monkeypatch.setattr(
        extract_day,
        "extract_day_db",
        lambda _day, db_path, _work_dir, _url: (make_raw_db(db_path, good_raw) or (0.1, 100)),
    )
    outcome = run_day(15, data_dir, work_dir)
    assert outcome["status"] == "ok"
    assert outcome["rows_out"] == 2
    assert canonical.read_bytes() != sentinel_bytes
    assert list((data_dir / "gps").glob("*.staged.parquet")) == []
