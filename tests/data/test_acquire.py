"""Unit tests for the acquisition orchestrator's pure logic."""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import duckdb
import pytest

from ps1 import acquire
from ps1.acquire import (
    DEFAULT_DAYS,
    process_day,
    recover_rows_in,
    validate_days,
    write_status,
)
from ps1.extract_day import IST, default_output_path


def test_default_days_priority_first_then_rest():
    assert DEFAULT_DAYS[:9] == [15, 22, 27, 28, 5, 6, 8, 13, 14]
    assert sorted(DEFAULT_DAYS) == list(range(1, 32))


def test_validate_days_rejects_duplicates_and_range():
    with pytest.raises(ValueError, match="duplicates"):
        validate_days([1, 1])
    with pytest.raises(ValueError, match="1..31"):
        validate_days([0])
    with pytest.raises(ValueError, match="1..31"):
        validate_days([32])
    assert validate_days([3, 1]) == [3, 1]


def test_recover_rows_in_last_wins_and_ignores_failures(tmp_path):
    log = tmp_path / "download.log"
    log.write_text(
        "day=2026-10-15 status=ok rows_in=100 rows_out=60 seconds=1.0\n"
        'day=2026-10-16 status=failed rows_in= rows_out= seconds=2.0 error="x"\n'
        "day=2026-10-15 status=ok rows_in=200 rows_out=120 seconds=1.0\n"
    )
    assert recover_rows_in(log) == {"2026-10-15": 200}
    assert recover_rows_in(tmp_path / "missing.log") == {}


def test_write_status_accounts_for_every_day(tmp_path):
    (tmp_path / "gps").mkdir()
    acquire.RESULTS.clear()
    acquire.RESULTS["2026-10-15"] = {
        "day": "2026-10-15", "status": "ok", "rows_in": 100, "rows_out": 62,
        "vehicles": 10, "min_ts": 1, "max_ts": 2, "seconds": 1.0,
    }
    try:
        write_status(tmp_path, list(range(1, 32)), finished=True)
        text = (tmp_path / "STATUS.md").read_text()
        assert "Completed OK: 1/31" in text
        assert "| 2026-10-15 | ok | 100 | 62 | 10 | 1 | 2 |" in text
        assert "| 2026-10-02 | pending |" in text
    finally:
        acquire.RESULTS.clear()


def test_process_day_failed_overwrite_never_deletes_canonical_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data_dir = tmp_path / "data"
    work_dir = tmp_path / "work"
    (data_dir / "gps").mkdir(parents=True)
    work_dir.mkdir(parents=True)
    canonical = default_output_path(data_dir, 15)
    sentinel = b"EXISTING-VALID-DAY-15-PARQUET"
    canonical.write_bytes(sentinel)

    monkeypatch.setattr(
        acquire,
        "run_day",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("network error during refresh")),
    )
    args = argparse.Namespace(
        data_dir=data_dir,
        work_dir=work_dir,
        overwrite=True,
        rows_in_log={},
    )
    result = process_day(15, args)
    assert result["status"] == "failed"
    assert canonical.exists()
    assert canonical.read_bytes() == sentinel


def test_process_day_resume_rejects_corrupt_and_wrong_day_parquet(tmp_path: Path):
    data_dir = tmp_path / "data"
    work_dir = tmp_path / "work"
    (data_dir / "gps").mkdir(parents=True)
    work_dir.mkdir(parents=True)
    args = argparse.Namespace(
        data_dir=data_dir,
        work_dir=work_dir,
        overwrite=False,
        rows_in_log={},
    )

    # Corrupt file must not be marked OK on resume
    corrupt_path = default_output_path(data_dir, 15)
    corrupt_path.write_bytes(b"corrupt-parquet-bytes")
    corrupt_result = process_day(15, args)
    assert corrupt_result["status"] == "failed"
    assert "verification failed" in corrupt_result["error"]
    assert corrupt_path.exists()

    # Wrong-day Parquet (Oct 14 + Oct 16) must not be marked OK for day 15 on resume
    wrong_day_path = default_output_path(data_dir, 15)
    d14 = int(dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=IST).timestamp())
    d16 = int(dt.datetime(2026, 10, 16, 12, 0, 0, tzinfo=IST).timestamp())
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (vehicle_id VARCHAR, route_id INTEGER, trip_id VARCHAR, "
        "ts BIGINT, lat DOUBLE, lng DOUBLE, speed DOUBLE)"
    )
    con.executemany(
        "INSERT INTO t VALUES (?,?,?,?,?,?,?)",
        [
            ("V1", 2171, "t1", d14, 28.6, 77.2, 5.0),
            ("V2", 2171, "t2", d16, 28.6, 77.2, 5.0),
        ],
    )
    con.execute(f"COPY t TO '{wrong_day_path}' (FORMAT PARQUET)")
    con.close()
    wrong_day_result = process_day(15, args)
    assert wrong_day_result["status"] == "failed"
    assert "outside archive-day window" in wrong_day_result["error"]
