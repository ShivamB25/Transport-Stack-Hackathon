"""Apply the declared model-selection ladder rule on held-out observed-only metrics."""
from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from delhi_transit.core.config import REPO_ROOT

LADDER_ORDER: tuple[str, ...] = ("0", "1", "1h", "2", "4")
SHIPPABLE_RUNGS: tuple[str, ...] = ("1", "2", "4")
SHIPPABLE_ABOVE_PERSISTENCE: tuple[str, ...] = ("4", "2")
SHIP_STRATUM = "observed-only"


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _to_int(value: Any) -> int:
    return int(float(value))


_EVAL_COLUMN_TYPES: dict[str, Callable[[Any], Any]] = {
    "day": str,
    "direction": str,
    "route_id": _to_int,
    "rung": str,
    "stratum": str,
    "queries": _to_int,
    "mae_s": float,
    "median_ae_s": float,
    "p90_ae_s": float,
    "share_gt_60_pct": float,
    "observed_queries": _to_int,
    "interpolated_queries": _to_int,
    "observed_coverage_pct": float,
    "interpolated_coverage_pct": float,
    "history_fallback_queries": _to_int,
}
# Interval columns are empty for rungs without quantiles.
_OPTIONAL_EVAL_COLUMNS = ("interval_coverage_pct", "mean_interval_width_s", "median_interval_width_s")


def load_eval_csv(csv_path: Path) -> list[dict[str, Any]]:
    """Read an evaluation CSV into typed metric row dicts."""
    with Path(csv_path).open(newline="", encoding="utf-8") as handle:
        return [
            {
                **{column: cast(raw[column]) for column, cast in _EVAL_COLUMN_TYPES.items()},
                **{column: _to_float(raw.get(column)) for column in _OPTIONAL_EVAL_COLUMNS},
            }
            for raw in csv.DictReader(handle)
        ]


def select_shipped_rung(
    rows: Sequence[dict[str, Any]],
    *,
    stratum: str = SHIP_STRATUM,
    calibration: dict[str, Any] | None = None,
    pooled_interval_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select the highest rung beating rung 1 MAE at identical coverage on the stratum."""
    scoped = [row for row in rows if str(row["stratum"]) == stratum]
    if not scoped:
        raise ValueError(f"no evaluation rows found for stratum {stratum!r}")

    by_rung: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for row in scoped:
        rung = str(row["rung"])
        group = (str(row["day"]), str(row["direction"]))
        by_rung.setdefault(rung, {})[group] = row

    if "1" not in by_rung:
        raise ValueError("rung '1' (persistence) rows are required for model selection")

    baseline_groups = by_rung["1"]
    ordered_groups = sorted(baseline_groups)
    total_queries = sum(_to_int(baseline_groups[g]["queries"]) for g in ordered_groups)
    if total_queries <= 0:
        raise ValueError("total observed-only queries must be positive")

    def weighted_mae(group_map: dict[tuple[str, str], dict[str, Any]]) -> float:
        num = sum(_to_int(group_map[g]["queries"]) * float(group_map[g]["mae_s"]) for g in ordered_groups)
        return round(num / total_queries, 6)

    def weighted_optional(
        group_map: dict[tuple[str, str], dict[str, Any]], field: str
    ) -> float | None:
        vals = [_to_float(group_map[g].get(field)) for g in ordered_groups]
        if any(v is None for v in vals):
            return None
        num = sum(
            _to_int(group_map[g]["queries"]) * float(group_map[g][field])
            for g in ordered_groups
        )
        return round(num / total_queries, 6)

    baseline_mae = weighted_mae(baseline_groups)

    candidates_summary: dict[str, dict[str, Any]] = {}
    for rung in LADDER_ORDER:
        if rung not in by_rung:
            continue
        group_map = by_rung[rung]
        complete = set(group_map.keys()) == set(ordered_groups)
        coverage_matches = complete and all(
            _to_int(group_map[g]["queries"]) == _to_int(baseline_groups[g]["queries"])
            and _to_int(group_map[g]["observed_queries"]) == _to_int(baseline_groups[g]["observed_queries"])
            and _to_int(group_map[g]["interpolated_queries"]) == _to_int(baseline_groups[g]["interpolated_queries"])
            and abs(
                float(group_map[g]["observed_coverage_pct"])
                - float(baseline_groups[g]["observed_coverage_pct"])
            )
            <= 1e-6
            for g in ordered_groups
        )
        if complete:
            rung_mae = weighted_mae(group_map)
            delta = round(rung_mae - baseline_mae, 6)
            wins = sum(
                float(group_map[g]["mae_s"]) < float(baseline_groups[g]["mae_s"])
                for g in ordered_groups
            )
            beats = coverage_matches and rung_mae < baseline_mae
            interval_cov = weighted_optional(group_map, "interval_coverage_pct")
            mean_width = weighted_optional(group_map, "mean_interval_width_s")
            weighted_median_width = weighted_optional(group_map, "median_interval_width_s")
        else:
            rung_mae = None
            delta = None
            wins = 0
            beats = False
            interval_cov = None
            mean_width = None
            weighted_median_width = None

        candidates_summary[rung] = {
            "rung": rung,
            "shippable": rung in SHIPPABLE_RUNGS,
            "queries": total_queries if complete else sum(_to_int(r["queries"]) for r in group_map.values()),
            "overall_mae_s": rung_mae,
            "delta_vs_rung1_s": delta,
            "coverage_matches_rung1": coverage_matches,
            "beats_rung1_overall": beats,
            "groups_beating_rung1": wins,
            "total_groups": len(ordered_groups),
            "beats_rung1_all_groups": complete and wins == len(ordered_groups),
            "interval_coverage_pct": interval_cov,
            "mean_interval_width_s": mean_width,
            "weighted_mean_group_median_width_s": weighted_median_width,
        }

    selected_rung = "1"
    for candidate in SHIPPABLE_ABOVE_PERSISTENCE:
        info = candidates_summary.get(candidate)
        if info and info["beats_rung1_overall"]:
            selected_rung = candidate
            break

    per_day_direction = []
    for day, direction in ordered_groups:
        entry: dict[str, Any] = {
            "day": day,
            "direction": direction,
            "route_id": _to_int(baseline_groups[(day, direction)]["route_id"]),
            "queries": _to_int(baseline_groups[(day, direction)]["queries"]),
            "observed_coverage_pct": float(baseline_groups[(day, direction)]["observed_coverage_pct"]),
            "mae_by_rung_s": {},
        }
        for rung in LADDER_ORDER:
            if rung in by_rung and (day, direction) in by_rung[rung]:
                row = by_rung[rung][(day, direction)]
                entry["mae_by_rung_s"][rung] = round(float(row["mae_s"]), 6)
                if _to_float(row.get("interval_coverage_pct")) is not None:
                    entry.setdefault("intervals_by_rung", {})[rung] = {
                        "interval_coverage_pct": round(float(row["interval_coverage_pct"]), 6),
                        "mean_interval_width_s": round(float(row["mean_interval_width_s"]), 6),
                        "median_interval_width_s": round(float(row["median_interval_width_s"]), 6),
                    }
        per_day_direction.append(entry)

    selected_info = candidates_summary[selected_rung]
    decision: dict[str, Any] = {
        "selected_rung": selected_rung,
        "stratum": stratum,
        "rule": (
            "Ship the highest shippable rung in (4, 2) whose held-out observed-only "
            "query-weighted MAE is strictly lower than rung 1 (persistence) at identical "
            "per-group observed-vs-interpolated coverage; otherwise ship rung 1. Rung 1h is "
            "a non-shippable historical baseline and never appears as selected_rung."
        ),
        "ladder_order": list(LADDER_ORDER),
        "shippable_rungs": list(SHIPPABLE_RUNGS),
        "holdout_days": sorted({day for day, _ in ordered_groups}),
        "total_observed_queries": total_queries,
        "baseline_rung": "1",
        "baseline_overall_mae_s": baseline_mae,
        "selected_overall_mae_s": selected_info["overall_mae_s"],
        "delta_vs_rung1_s": selected_info["delta_vs_rung1_s"],
        "candidates": candidates_summary,
        "per_day_direction": per_day_direction,
    }
    if calibration is not None:
        decision["calibration"] = calibration
    if pooled_interval_metrics is not None:
        decision["pooled_interval_metrics"] = pooled_interval_metrics
    return decision


def write_selection(decision: dict[str, Any], output_path: Path) -> Path:
    """Write a ship decision; only observed-only decisions may become selection.json."""
    if decision.get("stratum") != SHIP_STRATUM:
        raise ValueError(
            f"refusing to write a ship decision from stratum {decision.get('stratum')!r}; "
            f"only {SHIP_STRATUM!r} is shippable"
        )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    return output_path


def load_selected_rung(selection_path: Path) -> str:
    """Read the shipped rung; refuse a selection.json that was not decided on observed-only data."""
    if not selection_path.exists():
        return "1"
    selection = json.loads(selection_path.read_text())
    if selection.get("stratum") != SHIP_STRATUM:
        raise ValueError(
            f"refusing {selection_path}: stratum {selection.get('stratum')!r} is not "
            f"{SHIP_STRATUM!r}"
        )
    return str(selection["selected_rung"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("eval_csv", type=Path, default=REPO_ROOT / "reports" / "eval-full.csv", nargs="?")
    parser.add_argument("--stratum", default=SHIP_STRATUM)
    parser.add_argument("--write", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.write is not None and args.stratum != SHIP_STRATUM:
        parser.error(
            f"--write requires --stratum {SHIP_STRATUM}; other strata are exploratory only"
        )
    rows = load_eval_csv(args.eval_csv)
    decision = select_shipped_rung(rows, stratum=args.stratum)
    if args.write is not None:
        write_selection(decision, args.write)
    print(json.dumps(decision, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
