"""Regenerate the frozen 717A held-out baseline evaluation."""
from __future__ import annotations

import argparse
import csv
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from ps1.config import DATA_DIR
from ps1.features import (
    ReplayQuery,
    build_replay_queries,
    build_stretch_errors,
    load_passages,
)
from ps1.model import BaselineModel, Prediction

TRAIN_DAYS = tuple(
    f"2026-10-{day:02d}" for day in (5, 6, 7, 8, 13, 14, 15)
)
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


def validate_day_partitions(train_days: Sequence[str], holdout_days: Sequence[str]) -> None:
    """Reject overlapping or duplicate whole-day partitions."""
    if len(train_days) != len(set(train_days)) or len(holdout_days) != len(set(holdout_days)):
        raise ValueError("duplicate day in an evaluation partition")
    overlap = set(train_days) & set(holdout_days)
    if overlap:
        raise ValueError(f"days appear in training and holdout: {sorted(overlap)}")


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
    """Compute paired point accuracy and target-provenance coverage."""
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
    return {
        "day": day,
        "direction": ROUTES[route_id],
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
        "interval_coverage_pct": None,
        "mean_interval_width_s": None,
        "median_interval_width_s": None,
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
            "All rungs use the same causal persistence-eligible queries within each stratum and group."
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
    (output_dir / "eval-baselines.md").write_text(chr(10).join(lines), encoding="utf-8")


def run_evaluation(data_dir: Path, output_dir: Path) -> dict[str, bool]:
    validate_day_partitions(TRAIN_DAYS, HOLDOUT_DAYS)
    training_stretches = []
    for day in TRAIN_DAYS:
        passages = load_passages(data_dir / "passages" / f"day={day}.parquet")
        training_stretches.extend(build_stretch_errors(passages))
    models = {rung: BaselineModel(rung).fit(training_stretches) for rung in RUNGS}

    holdout_queries = {}
    for day in HOLDOUT_DAYS:
        passages = load_passages(data_dir / "passages" / f"day={day}.parquet")
        holdout_queries[day] = build_replay_queries(build_stretch_errors(passages))

    rows = []
    for stratum in STRATA:
        for day in HOLDOUT_DAYS:
            for route_id in sorted(ROUTES):
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
                for rung in RUNGS:
                    predictions = [models[rung].predict(query) for query in scoped]
                    rows.append(
                        summarize(
                            day, route_id, rung, scoped, predictions, stratum=stratum
                        )
                    )
    write_reports(rows, output_dir)
    return persistence_gate(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)
    gate = run_evaluation(args.data_dir, args.output_dir)
    for direction, passed in gate.items():
        print(f"2026-10-22 {direction} persistence gate: {'PASS' if passed else 'FAIL'}")
    print(f"Reports: {args.output_dir / 'eval-baselines.csv'}")
    print(f"Reports: {args.output_dir / 'eval-baselines.md'}")
    return 0 if all(gate.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
