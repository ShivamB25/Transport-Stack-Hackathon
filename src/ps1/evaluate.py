"""Regenerate the frozen 717A held-out baseline and full rungs 0-4 evaluation."""
from __future__ import annotations

import argparse
import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ps1.config import DATA_DIR
from ps1.features import (
    ReplayQuery,
    build_replay_queries,
    build_stretch_errors,
    load_passages,
)
from ps1.model import (
    ALL_RUNGS,
    BaselineModel,
    Prediction,
    Rung2DayTypeModel,
    Rung3LastDelayModel,
    Rung4QuantileLightGBMModel,
    artifact_cutoff_metadata,
    save_model_artifact,
)
from ps1.selection import select_shipped_rung, write_selection

FIT_DAYS = tuple(f"2026-10-{day:02d}" for day in (5, 6, 7, 8, 13, 14))
CALIBRATION_DAYS = ("2026-10-15",)
TRAIN_DAYS = FIT_DAYS + CALIBRATION_DAYS
HOLDOUT_DAYS = tuple(f"2026-10-{day:02d}" for day in (22, 27, 28))
ROUTES = {2170: "DOWN", 2171: "UP"}
RUNGS = ("0", "1", "1h")
STRATA = ("all-stop", "observed-only")
METRIC_FIELDS = (
    "day",
    "direction",
    "route_id",
    "rung",
    "stratum",
    "queries",
    "mae_s",
    "median_ae_s",
    "p90_ae_s",
    "share_gt_60_pct",
    "observed_queries",
    "interpolated_queries",
    "observed_coverage_pct",
    "interpolated_coverage_pct",
    "history_fallback_queries",
    "interval_coverage_pct",
    "mean_interval_width_s",
    "median_interval_width_s",
)

PREDICTION_SCHEMA = pa.schema(
    [
        ("day", pa.string()),
        ("route_id", pa.int32()),
        ("direction", pa.string()),
        ("vehicle_id", pa.string()),
        ("run_id", pa.string()),
        ("from_sequence", pa.int32()),
        ("to_sequence", pa.int32()),
        ("from_stop_id", pa.string()),
        ("to_stop_id", pa.string()),
        ("query_ts", pa.int64()),
        ("target_observed", pa.bool_()),
        ("actual_error_s", pa.float64()),
        ("pred_p10_s", pa.float64()),
        ("pred_p50_s", pa.float64()),
        ("pred_p90_s", pa.float64()),
        ("covered", pa.bool_()),
        ("width_s", pa.float64()),
    ]
)


def validate_three_way_partitions(
    fit_days: Sequence[str],
    calibration_days: Sequence[str],
    holdout_days: Sequence[str],
) -> None:
    """Enforce disjoint, chronologically ordered whole-day fit / calibration / holdout splits."""
    for name, days in (
        ("fit", fit_days),
        ("calibration", calibration_days),
        ("holdout", holdout_days),
    ):
        if not days:
            raise ValueError(f"{name} day partition must not be empty")
        if len(days) != len(set(days)):
            raise ValueError(f"duplicate day in {name} partition")
    fit_set, cal_set, hold_set = set(fit_days), set(calibration_days), set(holdout_days)
    if fit_set & cal_set:
        raise ValueError(f"days appear in both fit and calibration: {sorted(fit_set & cal_set)}")
    if fit_set & hold_set:
        raise ValueError(f"days appear in both fit and holdout: {sorted(fit_set & hold_set)}")
    if cal_set & hold_set:
        raise ValueError(f"days appear in both calibration and holdout: {sorted(cal_set & hold_set)}")
    if min(cal_set) <= max(fit_set):
        raise ValueError(
            f"calibration days {sorted(cal_set)} must be strictly after fit days {sorted(fit_set)}"
        )
    if min(hold_set) <= max(cal_set):
        raise ValueError(
            f"holdout days {sorted(hold_set)} must be strictly after calibration days {sorted(cal_set)}"
        )


def _rounded(value: float) -> float:
    return round(float(value), 6)


def select_stratum(
    queries: Sequence[ReplayQuery], stratum: str
) -> list[ReplayQuery]:
    """Select target ground truth without changing the paired query protocol."""
    if stratum not in STRATA:
        raise ValueError(f"unknown evaluation stratum: {stratum}")
    if stratum == "observed-only":
        return [query for query in queries if query.stretch.target_observed]
    return list(queries)


def summarize(
    day: str,
    route_id: int,
    rung: str,
    queries: Sequence[ReplayQuery],
    predictions: Sequence[Prediction],
    stratum: str = "all-stop",
) -> dict[str, object]:
    """Compute paired point accuracy, target-provenance coverage, and interval metrics."""
    if len(queries) != len(predictions) or not queries:
        raise ValueError("queries and predictions must have the same nonzero length")
    if stratum not in STRATA:
        raise ValueError(f"unknown evaluation stratum: {stratum}")
    if stratum == "observed-only" and not all(
        query.stretch.target_observed for query in queries
    ):
        raise ValueError("observed-only metrics require directly observed target endpoints")
    actual = np.array([query.stretch.error_s for query in queries], dtype=float)
    predicted = np.array([prediction.error_s for prediction in predictions], dtype=float)
    absolute = np.abs(predicted - actual)
    observed = sum(query.stretch.target_observed for query in queries)
    interpolated = len(queries) - observed

    has_intervals = any(p.p10_s is not None or p.p90_s is not None for p in predictions)
    interval_cov: float | None = None
    mean_width: float | None = None
    median_width: float | None = None
    if has_intervals:
        if not all(
            p.p10_s is not None and p.p50_s is not None and p.p90_s is not None
            for p in predictions
        ):
            raise ValueError("interval-producing rung must emit p10, p50, and p90 for every query")
        p10 = np.array([float(p.p10_s) for p in predictions], dtype=float)  # type: ignore[arg-type]
        p50 = np.array([float(p.p50_s) for p in predictions], dtype=float)  # type: ignore[arg-type]
        p90 = np.array([float(p.p90_s) for p in predictions], dtype=float)  # type: ignore[arg-type]
        if np.any(p10 > p50) or np.any(p50 > p90):
            raise ValueError(f"quantile ordering violation (p10 <= p50 <= p90) in rung {rung}")
        widths = p90 - p10
        interval_cov = _rounded(100.0 * np.mean((actual >= p10) & (actual <= p90)))
        mean_width = _rounded(np.mean(widths))
        median_width = _rounded(np.median(widths))

    return {
        "day": day,
        "direction": ROUTES.get(route_id, str(route_id)),
        "route_id": route_id,
        "rung": rung,
        "stratum": stratum,
        "queries": len(queries),
        "mae_s": _rounded(np.mean(absolute)),
        "median_ae_s": _rounded(np.median(absolute)),
        "p90_ae_s": _rounded(np.quantile(absolute, 0.9)),
        "share_gt_60_pct": _rounded(100 * np.mean(absolute > 60)),
        "observed_queries": observed,
        "interpolated_queries": interpolated,
        "observed_coverage_pct": _rounded(100 * observed / len(queries)),
        "interpolated_coverage_pct": _rounded(100 * interpolated / len(queries)),
        "history_fallback_queries": sum(p.used_fallback for p in predictions),
        "interval_coverage_pct": interval_cov,
        "mean_interval_width_s": mean_width,
        "median_interval_width_s": median_width,
    }


def persistence_gate(rows: Sequence[dict[str, object]]) -> dict[str, bool]:
    """Return the strict October 22 rung-1-versus-rung-0 result by direction."""
    result = {}
    for direction in ("UP", "DOWN"):
        scoped = {
            str(row["rung"]): row
            for row in rows
            if row["day"] == "2026-10-22"
            and row["direction"] == direction
            and row["stratum"] == "observed-only"
        }
        result[direction] = float(scoped["1"]["mae_s"]) < float(scoped["0"]["mae_s"])
    return result


def write_reports(rows: Sequence[dict[str, object]], output_dir: Path) -> None:
    """Write the frozen baseline CSV and markdown reports (rungs 0, 1, 1h)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "eval-baselines.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=METRIC_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    gate = persistence_gate(rows)
    lines = [
        "# 717A held-out baseline evaluation",
        "",
        (
            "Protocol: [docs/eval-protocol.md](../docs/eval-protocol.md). "
            "All rungs use the same causal persistence-eligible queries within each stratum and group. "
            "Every source passage has event time and available_at strictly before the query."
        ),
        "",
        (
            "**Ground-truth caveat:** all-stop metrics include schedule-interpolated "
            "pseudo-ground-truth derived from the timetable. This is circular and biases "
            "comparisons toward rung 0. Only observed-only metrics, whose target stretch "
            "endpoints are both directly observed, are used for the persistence gate."
        ),
        "",
        "| Day | Direction | Rung | Stratum | Queries | MAE s | Median AE s | p90 AE s | >60 s % | Observed % | Interpolated/partial % | History fallbacks |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['day']} | {row['direction']} | {row['rung']} | {row['stratum']} | "
            f"{row['queries']} | {row['mae_s']:.3f} | {row['median_ae_s']:.3f} | {row['p90_ae_s']:.3f} | "
            f"{row['share_gt_60_pct']:.3f} | {row['observed_coverage_pct']:.3f} | "
            f"{row['interpolated_coverage_pct']:.3f} | {row['history_fallback_queries']} |"
        )
    lines.extend(["", "## October 22 observed-only persistence gate", ""])
    for direction in ("UP", "DOWN"):
        scoped = {
            str(row["rung"]): row
            for row in rows
            if row["day"] == "2026-10-22"
            and row["direction"] == direction
            and row["stratum"] == "observed-only"
        }
        comparison = "PASS" if gate[direction] else "FAIL"
        lines.append(
            f"- {direction}: **{comparison}** — rung 1 MAE {scoped['1']['mae_s']:.3f} s "
            f"vs rung 0 MAE {scoped['0']['mae_s']:.3f} s; "
            f"{scoped['0']['queries']} observed-only paired queries in each rung."
        )
    if not all(gate.values()):
        lines.extend(
            [
                "",
                (
                    "**Negative result:** persistence did not beat the timetable in both "
                    "directions on directly observed ground truth. No held-out tuning was applied."
                ),
            ]
        )
    lines.extend(
        [
            "",
            (
                "The historical approximately 30% improvement is a reference only. "
                "The gate is the strict direction-by-direction MAE comparison above."
            ),
            "",
        ]
    )
    (output_dir / "eval-baselines.md").write_text("\n".join(lines), encoding="utf-8")


def _fmt_opt(value: object) -> str:
    if value is None or value == "":
        return "-"
    return f"{float(value):.3f}"


def write_full_reports(
    rows: Sequence[dict[str, object]],
    decision: dict[str, Any],
    output_dir: Path,
    *,
    predictions_path: Path | None = None,
) -> None:
    """Write full rungs 0-4 CSV, markdown report, and selection.json."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "eval-full.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=METRIC_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    write_selection(decision, output_dir / "selection.json")

    lines = [
        "# 717A held-out full ladder evaluation (rungs 0–4)",
        "",
        (
            "Protocol: [docs/eval-protocol.md](../docs/eval-protocol.md). "
            "All rungs (0, 1, 1h, 2, 3, 4) are scored on identical causal persistence-eligible "
            "queries within each stratum, day, and direction. Every source passage has event "
            "time and available_at strictly before the query."
        ),
        "",
        "## Whole-day partitions",
        "",
        f"- **Training days (rungs 1h, 2, 3):** {', '.join(TRAIN_DAYS)}",
        f"- **Rung 4 LightGBM fit days:** {', '.join(FIT_DAYS)}",
        f"- **Rung 4 split-conformal calibration day:** {', '.join(CALIBRATION_DAYS)} (strictly later training day; never touches held-out days)",
        f"- **Held-out evaluation days:** {', '.join(HOLDOUT_DAYS)}",
        "- **Excluded holidays:** 2026-10-02, 2026-10-20, 2026-10-26",
        "",
        (
            "**Ground-truth caveat:** `all-stop` rows include schedule-interpolated pseudo-ground-truth "
            "derived from the timetable (circular bias toward rung 0). The persistence gate and model "
            "selection rule are applied exclusively on the `observed-only` stratum."
        ),
        "",
        "## Per-day, per-direction metrics",
        "",
        "| Day | Direction | Rung | Stratum | Queries | MAE s | Median AE s | p90 AE s | >60 s % | Observed % | Interp % | Fallbacks | Interval cov % | Mean width s | Median width s |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['day']} | {row['direction']} | {row['rung']} | {row['stratum']} | "
            f"{row['queries']} | {row['mae_s']:.3f} | {row['median_ae_s']:.3f} | {row['p90_ae_s']:.3f} | "
            f"{row['share_gt_60_pct']:.3f} | {row['observed_coverage_pct']:.3f} | "
            f"{row['interpolated_coverage_pct']:.3f} | {row['history_fallback_queries']} | "
            f"{_fmt_opt(row['interval_coverage_pct'])} | {_fmt_opt(row['mean_interval_width_s'])} | "
            f"{_fmt_opt(row['median_interval_width_s'])} |"
        )

    lines.extend(["", "## Model selection (observed-only stratum)", ""])
    lines.append(f"- **Rule:** {decision['rule']}")
    lines.append(
        f"- **Selected rung:** **`{decision['selected_rung']}`** "
        f"(held-out observed-only MAE {decision['selected_overall_mae_s']:.3f} s vs "
        f"rung 1 persistence {decision['baseline_overall_mae_s']:.3f} s; "
        f"delta {decision['delta_vs_rung1_s']:+.3f} s across {decision['total_observed_queries']} observed-only queries)"
    )
    lines.extend(
        [
            "",
            "| Rung | Observed queries | Overall MAE s | Delta vs rung 1 s | Coverage matches rung 1 | Groups beating rung 1 | Interval cov % | Mean width s | Weighted mean group median width s |",
            "|---:|---:|---:|---:|---|---:|---:|---:|---:|",
        ]
    )
    for rung in decision["ladder_order"]:
        cand = decision["candidates"].get(rung)
        if not cand:
            continue
        lines.append(
            f"| {rung} | {cand['queries']} | {_fmt_opt(cand['overall_mae_s'])} | "
            f"{cand['delta_vs_rung1_s']:+.3f} | "
            f"{'yes' if cand['coverage_matches_rung1'] else 'no'} | "
            f"{cand['groups_beating_rung1']}/{cand['total_groups']} | "
            f"{_fmt_opt(cand['interval_coverage_pct'])} | "
            f"{_fmt_opt(cand['mean_interval_width_s'])} | "
            f"{_fmt_opt(cand['weighted_mean_group_median_width_s'])} |"
        )

    pooled = decision.get("pooled_interval_metrics")
    if pooled:
        lines.extend(
            [
                "",
                "## Pooled held-out observed-only rung 4 intervals",
                "",
                (
                    f"- Exact pooled query median width: **{pooled['median_interval_width_s']:.3f} s** "
                    f"across {pooled['queries']} queries (mean width {pooled['mean_interval_width_s']:.3f} s, "
                    f"coverage {pooled['interval_coverage_pct']:.3f}%)."
                ),
            ]
        )

    cal = decision.get("calibration")
    if cal:
        lines.extend(["", "## Rung 4 split-conformal calibration", ""])
        lines.append(f"- **Method:** {cal['method']} (nominal p10–p90 coverage {100 * cal['nominal_coverage']:.0f}%)")
        lines.append(f"- **Fit days ({cal['fit_queries']} observed queries):** {', '.join(cal['fit_days'])}")
        lines.append(f"- **Calibration day ({cal['calibration_queries']} observed queries):** {', '.join(cal['calibration_days'])}")
        lines.append(f"- **Conformal offset q_conf:** {cal['conformal_quantile_s']:.3f} s")
        lines.append(
            f"- **Calibration-day coverage:** {cal['raw_calibration_coverage_pct']:.3f}% raw -> "
            f"{cal['calibrated_calibration_coverage_pct']:.3f}% calibrated "
            f"(mean width {cal['calibration_mean_width_s']:.3f} s, median width {cal['calibration_median_width_s']:.3f} s)"
        )
        if predictions_path is not None:
            lines.append(f"- **Saved held-out predictions:** `{predictions_path}`")

    lines.append("")
    (output_dir / "eval-full.md").write_text("\n".join(lines), encoding="utf-8")


def save_rung4_predictions(
    holdout_queries_by_day: dict[str, list[ReplayQuery]],
    model: Rung4QuantileLightGBMModel,
    output_path: Path,
    route_ids: Sequence[int],
) -> Path:
    """Persist per-query held-out Rung 4 predictions for independent coverage/ordering audits."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    allowed = set(route_ids)
    for day in sorted(holdout_queries_by_day):
        scoped = [q for q in holdout_queries_by_day[day] if q.stretch.route_id in allowed]
        if not scoped:
            continue
        preds = model.predict_batch(scoped)
        for q, p in zip(scoped, preds, strict=True):
            actual = float(q.stretch.error_s)
            records.append(
                {
                    "day": q.stretch.day,
                    "route_id": int(q.stretch.route_id),
                    "direction": q.stretch.direction,
                    "vehicle_id": q.stretch.vehicle_id,
                    "run_id": q.stretch.run_id,
                    "from_sequence": int(q.stretch.from_sequence),
                    "to_sequence": int(q.stretch.to_sequence),
                    "from_stop_id": q.stretch.from_stop_id,
                    "to_stop_id": q.stretch.to_stop_id,
                    "query_ts": int(q.stretch.query_ts),
                    "target_observed": bool(q.stretch.target_observed),
                    "actual_error_s": actual,
                    "pred_p10_s": float(p.p10_s),
                    "pred_p50_s": float(p.p50_s),
                    "pred_p90_s": float(p.p90_s),
                    "covered": bool(p.p10_s <= actual <= p.p90_s),
                    "width_s": float(p.p90_s - p.p10_s),
                }
            )
    table = pa.Table.from_pylist(records, schema=PREDICTION_SCHEMA)
    pq.write_table(table, output_path)
    return output_path


def pooled_rung4_interval_metrics(predictions_path: Path) -> dict[str, Any]:
    """Compute exact pooled observed-only interval statistics from per-query predictions."""
    table = pq.read_table(
        predictions_path,
        columns=["target_observed", "covered", "width_s"],
    )
    observed = np.asarray(table["target_observed"], dtype=bool)
    if not np.any(observed):
        raise ValueError("saved rung 4 predictions contain no observed-only queries")
    covered = np.asarray(table["covered"], dtype=bool)[observed]
    widths = np.asarray(table["width_s"], dtype=float)[observed]
    return {
        "rung": "4",
        "stratum": "observed-only",
        "aggregation": "pooled_queries",
        "queries": int(np.sum(observed)),
        "interval_coverage_pct": _rounded(100.0 * np.mean(covered)),
        "mean_interval_width_s": _rounded(np.mean(widths)),
        "median_interval_width_s": _rounded(np.median(widths)),
    }


def run_evaluation(
    data_dir: Path,
    output_dir: Path,
    *,
    model_dir: Path | None = None,
    route_ids: Sequence[int] | None = None,
    holdout_days: Sequence[str] | None = None,
    baselines_only: bool = False,
) -> dict[str, bool]:
    """Run held-out evaluation (rungs 0-1h baselines and full rungs 0-4 + selection)."""
    active_routes = sorted(route_ids) if route_ids is not None else sorted(ROUTES)
    active_holdout = tuple(holdout_days) if holdout_days is not None else HOLDOUT_DAYS
    validate_three_way_partitions(FIT_DAYS, CALIBRATION_DAYS, active_holdout)

    fit_stretches = []
    fit_queries = []
    availability_by_day: dict[str, int] = {}
    cal_stretches = []
    cal_queries = []
    for day in TRAIN_DAYS:
        passages = load_passages(data_dir / "passages" / f"day={day}.parquet")
        availability_by_day[day] = max(p.source_available_at for p in passages)
        stretches = build_stretch_errors(passages)
        queries = build_replay_queries(stretches, passages)
        if day in FIT_DAYS:
            fit_stretches.extend(stretches)
            fit_queries.extend(queries)
        else:
            cal_stretches.extend(stretches)
            cal_queries.extend(queries)

    training_stretches = fit_stretches + cal_stretches
    training_queries = fit_queries + cal_queries

    holdout_queries: dict[str, list[ReplayQuery]] = {}
    for day in active_holdout:
        passages = load_passages(data_dir / "passages" / f"day={day}.parquet")
        stretches = build_stretch_errors(passages)
        holdout_queries[day] = build_replay_queries(stretches, passages)

    models: dict[str, Any] = {
        rung: BaselineModel(rung).fit(training_stretches) for rung in RUNGS
    }
    if not baselines_only:
        models["2"] = Rung2DayTypeModel().fit(training_stretches)
        models["3"] = Rung3LastDelayModel().fit(training_stretches, training_queries)
        models["4"] = Rung4QuantileLightGBMModel().fit(
            fit_stretches, fit_queries, cal_queries
        )

    active_rungs = RUNGS if baselines_only else ALL_RUNGS

    full_rows: list[dict[str, object]] = []
    baseline_rows: list[dict[str, object]] = []
    for stratum in STRATA:
        for day in active_holdout:
            for route_id in active_routes:
                route_queries = [
                    query
                    for query in holdout_queries[day]
                    if query.stretch.route_id == route_id
                ]
                scoped = select_stratum(route_queries, stratum)
                if not scoped:
                    raise ValueError(
                        f"no {stratum} paired replay queries for {day} route {route_id}"
                    )
                for rung in active_rungs:
                    predictions = models[rung].predict_batch(scoped)
                    row = summarize(
                        day, route_id, rung, scoped, predictions, stratum=stratum
                    )
                    full_rows.append(row)
                    if rung in RUNGS:
                        baseline_rows.append(row)

    write_reports(baseline_rows, output_dir)

    if not baselines_only:
        resolved_model_dir = Path(model_dir) if model_dir is not None else data_dir / "model"
        rung4: Rung4QuantileLightGBMModel = models["4"]
        pred_path = save_rung4_predictions(
            holdout_queries,
            rung4,
            resolved_model_dir / "eval-predictions-rung4.parquet",
            active_routes,
        )
        pooled_intervals = pooled_rung4_interval_metrics(pred_path)
        decision = select_shipped_rung(
            full_rows,
            stratum="observed-only",
            calibration=rung4.calibration_info,
            pooled_interval_metrics=pooled_intervals,
        )
        decision["persistence_gate_oct22"] = persistence_gate(full_rows)
        write_full_reports(full_rows, decision, output_dir, predictions_path=pred_path)

        selected_rung = str(decision["selected_rung"])
        if selected_rung == "4":
            artifact_training_days = FIT_DAYS
            artifact_calibration_days = CALIBRATION_DAYS
        elif selected_rung in {"1h", "2", "3"}:
            artifact_training_days = TRAIN_DAYS
            artifact_calibration_days = ()
        else:
            artifact_training_days = ()
            artifact_calibration_days = ()
        latest_training_available = max(
            (availability_by_day[day] for day in artifact_training_days),
            default=None,
        )
        latest_calibration_available = max(
            (availability_by_day[day] for day in artifact_calibration_days),
            default=None,
        )
        cutoffs = artifact_cutoff_metadata(
            training_days=artifact_training_days,
            calibration_days=artifact_calibration_days,
            latest_training_available_at=latest_training_available,
            latest_calibration_available_at=latest_calibration_available,
        )
        save_model_artifact(
            models[selected_rung],
            resolved_model_dir,
            covered_routes=active_routes,
            extra_metadata={
                "train_days": list(TRAIN_DAYS),
                "fit_days": list(FIT_DAYS),
                "calibration_days": list(CALIBRATION_DAYS),
                "holdout_days": list(active_holdout),
                "selection_overall_mae_s": decision["selected_overall_mae_s"],
                "baseline_overall_mae_s": decision["baseline_overall_mae_s"],
                "predictions_path": str(pred_path),
                **cutoffs,
            },
        )

    return persistence_gate(baseline_rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--model-dir", type=Path, default=None)
    parser.add_argument("--routes", type=str, default=None, help="Comma-separated route IDs")
    parser.add_argument("--holdout", type=str, default=None, help="Comma-separated YYYY-MM-DD days")
    parser.add_argument("--baselines-only", action="store_true")
    args = parser.parse_args(argv)

    route_ids = [int(r.strip()) for r in args.routes.split(",") if r.strip()] if args.routes else None
    holdout_days = [d.strip() for d in args.holdout.split(",") if d.strip()] if args.holdout else None

    gate = run_evaluation(
        args.data_dir,
        args.output_dir,
        model_dir=args.model_dir,
        route_ids=route_ids,
        holdout_days=holdout_days,
        baselines_only=args.baselines_only,
    )
    for direction, passed in gate.items():
        print(f"2026-10-22 {direction} persistence gate: {'PASS' if passed else 'FAIL'}")
    print(f"Reports: {args.output_dir / 'eval-baselines.csv'}")
    print(f"Reports: {args.output_dir / 'eval-baselines.md'}")
    if not args.baselines_only:
        print(f"Reports: {args.output_dir / 'eval-full.csv'}")
        print(f"Reports: {args.output_dir / 'eval-full.md'}")
        print(f"Reports: {args.output_dir / 'selection.json'}")
    return 0 if all(gate.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
