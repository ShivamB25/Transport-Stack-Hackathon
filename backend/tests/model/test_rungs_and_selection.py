"""Tests for rungs 2-4, conformal calibration, artifact roundtrip, and selection rule."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from delhi_transit.core.config import DATA_DIR, REPO_ROOT
from delhi_transit.model.artifact import (
    artifact_cutoff_metadata,
    load_model_artifact,
    model_artifact_eligible_as_of,
    save_model_artifact,
)
from delhi_transit.model.features import (
    IST,
    PassageRow,
    build_replay_queries,
    build_stretch_errors,
)
from delhi_transit.model.lgbm import Rung4QuantileLightGBMModel
from delhi_transit.model.rungs import Rung2DayTypeModel
from delhi_transit.model.selection import (
    load_eval_csv,
    select_shipped_rung,
    write_selection,
)
from delhi_transit.model.selection import main as selection_main


def _make_day_passages(day: str, base_error: int, runs: int = 8) -> list[PassageRow]:
    y, m, d = (int(p) for p in day.split("-"))
    start_ts = int(datetime(y, m, d, 8, 0, tzinfo=IST).timestamp())
    coords = [(28.500, 77.250), (28.505, 77.250), (28.510, 77.250), (28.515, 77.250)]
    passages: list[PassageRow] = []
    for r in range(runs):
        run_delay = base_error + (r % 3) * 15
        run_t0 = start_ts + r * 600
        ts = run_t0
        sched = run_t0
        for seq, (lat, lng) in enumerate(coords):
            passages.append(
                PassageRow(
                    day=day,
                    route_id=2171,
                    direction="UP",
                    vehicle_id=f"V{r}",
                    run_id=f"{day}_R{r}",
                    stop_sequence=seq,
                    stop_id=f"S{seq}",
                    observed=True,
                    ts=ts,
                    scheduled_ts=sched,
                    stop_lat=lat,
                    stop_lng=lng,
                    schedule_error_s=ts - sched,
                    distance_m=10.0,
                )
            )
            sched += 100
            ts += 100 + run_delay + seq * 10
    return passages


def test_rung2_day_type_hierarchy_and_fallback() -> None:
    # Tuesday (Oct 6) has +50s error, Thursday (Oct 8) has +120s error
    tue_p = _make_day_passages("2026-10-06", base_error=50, runs=6)
    thu_p = _make_day_passages("2026-10-08", base_error=120, runs=6)
    stretches = build_stretch_errors(tue_p + thu_p)

    model = Rung2DayTypeModel(min_day_type_samples=3).fit(stretches)

    tue_test = build_replay_queries(build_stretch_errors(_make_day_passages("2026-10-27", 50, runs=3)), _make_day_passages("2026-10-27", 50, runs=3))
    thu_test = build_replay_queries(build_stretch_errors(_make_day_passages("2026-10-22", 120, runs=3)), _make_day_passages("2026-10-22", 120, runs=3))
    tue_pred = model.predict(tue_test[0])
    thu_pred = model.predict(thu_test[0])
    assert not tue_pred.used_fallback
    assert not thu_pred.used_fallback
    assert thu_pred.error_s > tue_pred.error_s + 30.0


def test_rung4_quantile_ordering_calibration_and_artifact_roundtrip(tmp_path: Path) -> None:
    fit_p = (
        _make_day_passages("2026-10-05", 20, runs=12)
        + _make_day_passages("2026-10-06", 35, runs=12)
        + _make_day_passages("2026-10-07", 25, runs=12)
    )
    cal_p = _make_day_passages("2026-10-15", 30, runs=10)
    eval_p = _make_day_passages("2026-10-22", 30, runs=6)

    fit_s = build_stretch_errors(fit_p)
    fit_q = build_replay_queries(fit_s, fit_p)
    cal_q = build_replay_queries(build_stretch_errors(cal_p), cal_p)
    eval_q = build_replay_queries(build_stretch_errors(eval_p), eval_p)

    model = Rung4QuantileLightGBMModel(n_estimators=25, num_leaves=7, min_child_samples=5).fit(
        fit_s, fit_q, cal_q
    )
    preds = model.predict_batch(eval_q)
    assert len(preds) == len(eval_q)
    for pred in preds:
        assert pred.p10_s is not None and pred.p50_s is not None and pred.p90_s is not None
        assert pred.p10_s <= pred.p50_s <= pred.p90_s
        assert pred.error_s == pred.p50_s
    assert model.calibration_info["calibration_days"] == ["2026-10-15"]
    assert "2026-10-22" not in model.calibration_info["fit_days"]
    assert "2026-10-22" not in model.calibration_info["calibration_days"]

    # Reject non-disjoint or earlier calibration days
    with pytest.raises(ValueError, match="overlap"):
        Rung4QuantileLightGBMModel(n_estimators=10).fit(fit_s, fit_q, fit_q)
    earlier_p = _make_day_passages("2026-10-04", 20, runs=6)
    earlier_q = build_replay_queries(build_stretch_errors(earlier_p), earlier_p)
    with pytest.raises(ValueError, match="strictly later"):
        Rung4QuantileLightGBMModel(n_estimators=10).fit(fit_s, fit_q, earlier_q)

    # Save and reload artifact without pickle; verify exact prediction parity
    save_model_artifact(model, tmp_path / "model", covered_routes=[2171])
    loaded = load_model_artifact(tmp_path / "model")
    assert loaded is not None
    assert loaded.rung == "4"
    assert loaded.covered_routes == frozenset({2171})
    for q, p_orig in zip(eval_q, preds, strict=True):
        assert q.features is not None
        p_loaded = loaded.predict_from_features(q.stretch.stretch_key, q.features)
        assert math.isclose(p_loaded.p10_s or 0.0, p_orig.p10_s or 0.0, abs_tol=1e-6)
        assert math.isclose(p_loaded.p50_s or 0.0, p_orig.p50_s or 0.0, abs_tol=1e-6)
        assert math.isclose(p_loaded.p90_s or 0.0, p_orig.p90_s or 0.0, abs_tol=1e-6)


def test_removed_rung_artifact_is_ignored(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    for rung in ("3", "1h"):
        model_dir = tmp_path / f"model-{rung}"
        model_dir.mkdir()
        (model_dir / "metadata.json").write_text(
            json.dumps({"selected_rung": rung, "covered_routes": [2171]}), encoding="utf-8"
        )
        with caplog.at_level("WARNING", logger="delhi_transit.model.artifact"):
            assert load_model_artifact(model_dir) is None
        assert f"unsupported rung '{rung}'" in caplog.text


def _metric_row(day: str, direction: str, rung: str, mae: float, queries: int = 100) -> dict:
    return {
        "day": day,
        "direction": direction,
        "route_id": 2171 if direction == "UP" else 2170,
        "rung": rung,
        "stratum": "observed-only",
        "queries": queries,
        "mae_s": mae,
        "median_ae_s": mae * 0.5,
        "p90_ae_s": mae * 2.0,
        "share_gt_60_pct": 20.0,
        "observed_queries": queries,
        "interpolated_queries": 0,
        "observed_coverage_pct": 100.0,
        "interpolated_coverage_pct": 0.0,
        "history_fallback_queries": 0,
        "interval_coverage_pct": 80.0 if rung == "4" else None,
        "mean_interval_width_s": 140.0 if rung == "4" else None,
        "median_interval_width_s": 80.0 if rung == "4" else None,
    }


def test_artifact_cutoff_metadata_and_eligibility() -> None:
    latest_training = int(datetime(2026, 10, 14, 23, 30, tzinfo=IST).timestamp())
    latest_calibration = int(datetime(2026, 10, 16, 0, 30, tzinfo=IST).timestamp())
    metadata = artifact_cutoff_metadata(
        training_days=("2026-10-05", "2026-10-14"),
        calibration_days=("2026-10-15",),
        latest_training_available_at=latest_training,
        latest_calibration_available_at=latest_calibration,
    )

    assert metadata["training_cutoff_day"] == "2026-10-14"
    assert metadata["calibration_cutoff_day"] == "2026-10-15"
    assert metadata["data_cutoff_day"] == "2026-10-15"
    # The late calibration anchor moves eligibility past the default next midnight.
    assert metadata["eligible_from_ts"] == latest_calibration + 1
    assert metadata["eligible_from_day"] == "2026-10-16"
    assert not model_artifact_eligible_as_of(metadata, latest_calibration)
    assert model_artifact_eligible_as_of(metadata, latest_calibration + 1)
    assert not model_artifact_eligible_as_of({}, latest_calibration + 1)

    persistence = artifact_cutoff_metadata(training_days=(), calibration_days=())
    assert persistence["training_cutoff_day"] is None
    assert persistence["eligible_from_ts"] == 0
    assert model_artifact_eligible_as_of(persistence, 0)


def test_selection_rule_ladder_and_coverage_match() -> None:
    # 1. Rung 4 beats rung 1 at same coverage -> ships rung 4
    rows = [
        _metric_row("2026-10-22", "UP", "0", 95.0),
        _metric_row("2026-10-22", "UP", "1", 85.0),
        _metric_row("2026-10-22", "UP", "2", 78.0),
        _metric_row("2026-10-22", "UP", "4", 68.0),
    ]
    assert select_shipped_rung(rows)["selected_rung"] == "4"

    # 2. Rung 4 has mismatched query coverage -> disqualified; rung 2 ships
    mismatch_rows = [
        _metric_row("2026-10-22", "UP", "1", 85.0, queries=100),
        _metric_row("2026-10-22", "UP", "2", 77.0, queries=100),
        _metric_row("2026-10-22", "UP", "4", 50.0, queries=90),
    ]
    decision = select_shipped_rung(mismatch_rows)
    assert decision["selected_rung"] == "2"
    assert not decision["candidates"]["4"]["coverage_matches_rung1"]

    # 3. No higher rung beats rung 1 -> ships rung 1
    worse_rows = [
        _metric_row("2026-10-22", "UP", "0", 95.0),
        _metric_row("2026-10-22", "UP", "1", 85.0),
        _metric_row("2026-10-22", "UP", "2", 87.0),
        _metric_row("2026-10-22", "UP", "4", 85.0),  # tie does not beat strictly
    ]
    assert select_shipped_rung(worse_rows)["selected_rung"] == "1"


def test_only_observed_only_decisions_are_written(tmp_path: Path) -> None:
    rows = [{**_metric_row("2026-10-22", "UP", "1", 85.0), "stratum": "all-stop"}]
    target = tmp_path / "selection.json"
    with pytest.raises(ValueError, match="all-stop"):
        write_selection(select_shipped_rung(rows, stratum="all-stop"), target)
    with pytest.raises(SystemExit):
        selection_main(["--stratum", "all-stop", "--write", str(target), str(tmp_path / "x.csv")])
    assert not target.exists()


def test_committed_selection_matches_fresh_rule_application() -> None:
    eval_csv = REPO_ROOT / "reports" / "eval-full.csv"
    selection_path = REPO_ROOT / "reports" / "selection.json"
    if not eval_csv.exists() or not selection_path.exists():
        pytest.skip("reports/eval-full.csv or reports/selection.json not generated yet")
    rows = load_eval_csv(eval_csv)
    fresh = select_shipped_rung(rows, stratum="observed-only")
    saved = json.loads(selection_path.read_text(encoding="utf-8"))
    assert fresh["selected_rung"] == saved["selected_rung"]
    assert fresh["baseline_overall_mae_s"] == saved["baseline_overall_mae_s"]
    assert fresh["selected_overall_mae_s"] == saved["selected_overall_mae_s"]
    assert fresh["candidates"] == saved["candidates"]


def test_saved_rung4_predictions_audit_ordering_and_coverage() -> None:
    eval_csv = REPO_ROOT / "reports" / "eval-full.csv"
    pred_parquet = DATA_DIR / "model" / "eval-predictions-rung4.parquet"
    if not eval_csv.exists() or not pred_parquet.exists():
        pytest.skip("saved rung 4 predictions not present")

    con = duckdb.connect()
    try:
        violations = con.execute(
            """
            SELECT count(*) FROM read_parquet(?)
            WHERE pred_p10_s > pred_p50_s OR pred_p50_s > pred_p90_s
            """,
            [str(pred_parquet)],
        ).fetchone()[0]
        assert violations == 0

        obs_stats = con.execute(
            """
            SELECT day, direction, count(*),
                   avg(abs(pred_p50_s - actual_error_s)),
                   100.0 * avg(CASE WHEN actual_error_s BETWEEN pred_p10_s AND pred_p90_s THEN 1.0 ELSE 0.0 END),
                   avg(pred_p90_s - pred_p10_s),
                   quantile_cont(pred_p90_s - pred_p10_s, 0.5)
            FROM read_parquet(?)
            WHERE target_observed
            GROUP BY day, direction
            """,
            [str(pred_parquet)],
        ).fetchall()
    finally:
        con.close()

    by_key = {(r["day"], r["direction"]): r for r in load_eval_csv(eval_csv) if r["rung"] == "4" and r["stratum"] == "observed-only"}
    assert len(obs_stats) == len(by_key) == 6
    for day, direction, n, mae, cov, mean_w, med_w in obs_stats:
        row = by_key[(day, direction)]
        assert n == row["queries"]
        assert math.isclose(mae, row["mae_s"], abs_tol=1e-4)
        assert math.isclose(cov, row["interval_coverage_pct"], abs_tol=1e-4)
        assert math.isclose(mean_w, row["mean_interval_width_s"], abs_tol=1e-4)
        assert math.isclose(med_w, row["median_interval_width_s"], abs_tol=1e-4)


def test_saved_pooled_interval_width_and_artifact_cutoffs() -> None:
    selection_path = REPO_ROOT / "reports" / "selection.json"
    model_dir = DATA_DIR / "model"
    pred_path = model_dir / "eval-predictions-rung4.parquet"
    if not selection_path.exists() or not pred_path.exists():
        pytest.skip("regenerated selection and saved predictions not present")

    decision = json.loads(selection_path.read_text(encoding="utf-8"))
    pooled = decision["pooled_interval_metrics"]
    con = duckdb.connect()
    try:
        count, coverage, mean_width, median_width = con.execute(
            """
            SELECT count(*),
                   100.0 * avg(CASE WHEN covered THEN 1.0 ELSE 0.0 END),
                   avg(width_s),
                   quantile_cont(width_s, 0.5)
            FROM read_parquet(?)
            WHERE target_observed
            """,
            [str(pred_path)],
        ).fetchone()
    finally:
        con.close()
    assert count == pooled["queries"]
    assert math.isclose(coverage, pooled["interval_coverage_pct"], abs_tol=1e-4)
    assert math.isclose(mean_width, pooled["mean_interval_width_s"], abs_tol=1e-4)
    assert math.isclose(median_width, pooled["median_interval_width_s"], abs_tol=1e-4)

    artifact = load_model_artifact(model_dir)
    assert artifact is not None
    metadata = artifact.metadata
    assert metadata["training_cutoff_day"] == "2026-10-14"
    assert metadata["calibration_cutoff_day"] == "2026-10-15"
    assert metadata["data_cutoff_day"] == "2026-10-15"
    assert metadata["eligible_from_ts"] > max(
        metadata["latest_training_available_at"],
        metadata["latest_calibration_available_at"],
    )
    assert not artifact.is_eligible_as_of(metadata["eligible_from_ts"] - 1)
    assert artifact.is_eligible_as_of(metadata["eligible_from_ts"])
    october_22 = int(datetime(2026, 10, 22, 8, 0, tzinfo=IST).timestamp())
    assert artifact.is_eligible_as_of(october_22)
