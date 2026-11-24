"""Evaluation CSV and Markdown report writers (byte formats are frozen)."""
from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from delhi_transit.core.config import portable_path
from delhi_transit.model.selection import write_selection

GATE_DAY = "2026-10-22"
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


def _gate_rows_by_rung(rows: Sequence[dict[str, object]], direction: str) -> dict[str, dict[str, object]]:
    return {
        str(row["rung"]): row
        for row in rows
        if row["day"] == GATE_DAY
        and row["direction"] == direction
        and row["stratum"] == "observed-only"
    }


def persistence_gate(rows: Sequence[dict[str, object]]) -> dict[str, bool]:
    """Return the strict October 22 rung-1-versus-rung-0 result by direction."""
    result = {}
    for direction in ("UP", "DOWN"):
        scoped = _gate_rows_by_rung(rows, direction)
        result[direction] = float(scoped["1"]["mae_s"]) < float(scoped["0"]["mae_s"])
    return result


def _write_metrics_csv(rows: Sequence[dict[str, object]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=METRIC_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _metric_cells(row: dict[str, object]) -> str:
    """Markdown cells shared by the baseline and full tables, up to history fallbacks."""
    return (
        f"| {row['day']} | {row['direction']} | {row['rung']} | {row['stratum']} | "
        f"{row['queries']} | {row['mae_s']:.3f} | {row['median_ae_s']:.3f} | {row['p90_ae_s']:.3f} | "
        f"{row['share_gt_60_pct']:.3f} | {row['observed_coverage_pct']:.3f} | "
        f"{row['interpolated_coverage_pct']:.3f} | {row['history_fallback_queries']} |"
    )


def _fmt_opt(value: object) -> str:
    if value is None or value == "":
        return "-"
    return f"{float(value):.3f}"


def write_baseline_reports(rows: Sequence[dict[str, object]], output_dir: Path) -> None:
    """Write the frozen baseline CSV and markdown reports (rungs 0, 1)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_metrics_csv(rows, output_dir / "eval-baselines.csv")

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
    lines.extend(_metric_cells(row) for row in rows)
    lines.extend(["", "## October 22 observed-only persistence gate", ""])
    for direction in ("UP", "DOWN"):
        scoped = _gate_rows_by_rung(rows, direction)
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


def write_full_reports(
    rows: Sequence[dict[str, object]],
    decision: dict[str, Any],
    output_dir: Path,
    *,
    partitions: dict[str, Sequence[str]],
    predictions_path: Path | None = None,
) -> None:
    """Write full rungs 0-4 CSV, markdown report, and selection.json.

    ``partitions`` holds the declared ``train``, ``fit``, ``calibration`` and ``holdout`` days.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_metrics_csv(rows, output_dir / "eval-full.csv")
    write_selection(decision, output_dir / "selection.json")

    lines = [
        "# 717A held-out full ladder evaluation (rungs 0–4)",
        "",
        (
            "Protocol: [docs/eval-protocol.md](../docs/eval-protocol.md). "
            "All rungs (0, 1, 2, 4) are scored on identical causal persistence-eligible "
            "queries within each stratum, day, and direction. Every source passage has event "
            "time and available_at strictly before the query."
        ),
        "",
        "## Whole-day partitions",
        "",
        f"- **Training days (rung 2):** {', '.join(partitions['train'])}",
        f"- **Rung 4 LightGBM fit days:** {', '.join(partitions['fit'])}",
        f"- **Rung 4 split-conformal calibration day:** {', '.join(partitions['calibration'])} (strictly later training day; never touches held-out days)",
        f"- **Held-out evaluation days:** {', '.join(partitions['holdout'])}",
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
    lines.extend(
        f"{_metric_cells(row)} {_fmt_opt(row['interval_coverage_pct'])} | "
        f"{_fmt_opt(row['mean_interval_width_s'])} | {_fmt_opt(row['median_interval_width_s'])} |"
        for row in rows
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
            lines.append(f"- **Saved held-out predictions:** `{portable_path(predictions_path)}`")

    lines.append("")
    (output_dir / "eval-full.md").write_text("\n".join(lines), encoding="utf-8")
