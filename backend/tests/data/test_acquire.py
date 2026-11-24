"""Unit tests for the acquisition orchestrator's pure logic."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from delhi_transit.data import acquire
from delhi_transit.data.acquire import (
    process_day,
    recover_rows_in,
)
from delhi_transit.data.extract_day import default_output_path


def test_recover_rows_in_last_wins_and_ignores_failures(tmp_path):
    log = tmp_path / "download.log"
    log.write_text(
        "day=2026-10-15 status=ok rows_in=100 rows_out=60 seconds=1.0\n"
        'day=2026-10-16 status=failed rows_in= rows_out= seconds=2.0 error="x"\n'
        "day=2026-10-15 status=ok rows_in=200 rows_out=120 seconds=1.0\n"
    )
    assert recover_rows_in(log) == {"2026-10-15": 200}
    assert recover_rows_in(tmp_path / "missing.log") == {}


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


def test_process_day_resume_rejects_corrupt_parquet(tmp_path: Path):
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
