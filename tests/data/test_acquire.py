"""Unit tests for the acquisition orchestrator's pure logic."""
from __future__ import annotations

import pytest

from ps1 import acquire
from ps1.acquire import DEFAULT_DAYS, recover_rows_in, validate_days, write_status


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
        assert "State: finished" in text
        assert "Completed OK: 1/31" in text
        assert "| 2026-10-15 | ok | 100 | 62 | 10 | 1 | 2 |" in text
        assert "| 2026-10-02 | pending |" in text
        assert "Failed days: none" in text
    finally:
        acquire.RESULTS.clear()
