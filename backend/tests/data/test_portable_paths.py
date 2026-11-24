from __future__ import annotations

from pathlib import Path

import pytest

from delhi_transit.core import config
from delhi_transit.core.config import portable_path
from delhi_transit.data.coverage_report import write_coverage_report
from delhi_transit.livecheck.report import _portable_paths


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(config, "SCRATCH_DIR", scratch)
    monkeypatch.setattr(config, "DATA_DIR", scratch / "data")
    monkeypatch.setattr(config, "WORK_DIR", scratch / "work")
    monkeypatch.setattr(config, "OTP_DIR", scratch / "otp")
    monkeypatch.setattr(config, "REPO_ROOT", scratch / "worktrees" / "repo")
    return scratch


def test_portable_path_uses_the_most_specific_root(roots: Path) -> None:
    assert portable_path(roots / "data" / "google" / "responses") == "$DELHI_TRANSIT_DATA_DIR/google/responses"
    assert portable_path(roots / "otp" / "graph" / "graph.obj") == "$DELHI_TRANSIT_OTP_DIR/graph/graph.obj"
    assert portable_path(roots / "work") == "$DELHI_TRANSIT_WORK_DIR"
    assert portable_path(roots / "live-check" / "day" / "x.jsonl") == "$DELHI_TRANSIT_SCRATCH_DIR/live-check/day/x.jsonl"
    assert portable_path(roots / "worktrees" / "repo" / "reports" / "a.json") == "reports/a.json"


def test_portable_path_leaves_variables_unknown_roots_and_none(roots: Path) -> None:
    assert portable_path("$DELHI_TRANSIT_DATA_DIR/model") == "$DELHI_TRANSIT_DATA_DIR/model"
    assert portable_path("/elsewhere/file.parquet") == "/elsewhere/file.parquet"
    assert portable_path(None) is None


def test_coverage_report_writes_portable_output_paths(roots: Path, tmp_path: Path) -> None:
    report = tmp_path / "coverage.md"
    out = roots / "data" / "passages" / "day=2026-10-05.parquet"
    write_coverage_report(report, [], {"2026-10-05": 3}, {"2026-10-05": out}, 60.0)
    text = report.read_text(encoding="utf-8")
    assert "`$DELHI_TRANSIT_DATA_DIR/passages/day=2026-10-05.parquet`" in text
    assert str(roots) not in text


def test_live_check_report_paths_are_portable(roots: Path) -> None:
    result = {
        "slots": [{"file": str(roots / "live-check" / "d" / "journeys" / "slot_000.jsonl"), "count": 1}],
        "actuals": {"gps": {"path": str(roots / "data" / "x.parquet"), "rows": 2}, "passages_path": None},
        "note": str(roots / "not-a-path-key"),
    }
    out = _portable_paths(result)
    assert out["slots"][0]["file"] == "$DELHI_TRANSIT_SCRATCH_DIR/live-check/d/journeys/slot_000.jsonl"
    assert out["actuals"]["gps"]["path"] == "$DELHI_TRANSIT_DATA_DIR/x.parquet"
    assert out["actuals"]["passages_path"] is None
    assert out["note"] == result["note"]
