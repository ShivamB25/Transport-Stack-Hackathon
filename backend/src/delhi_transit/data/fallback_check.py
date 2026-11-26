"""Measure the rung-1 fallback against the timetable on routes with no learned model.

The README says only route 717A has a trained model. This reports what the *fallback*
actually achieves on other routes, so that limitation carries a number instead of an
adjective.

It reproduces the reference selection in `planner/rides.py`: `stretch_hour`, then
`stretch`, then `route_hour`, then `corridor_hour`, predicting
`scheduled * median(clamp_ratio(actual / scheduled))` over the chosen set.
"""
from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

from delhi_transit.core.config import DATA_DIR
from delhi_transit.model.features import PassageRow, load_passages
from delhi_transit.planner.risk import clamp_ratio

# Busiest non-717A routes by October GPS volume, as used for the committed measurement.
DEFAULT_ROUTES = (1616, 1615, 2204, 2203, 2183, 2182)
TRAIN_DAY = "2026-10-22"
HOLDOUT_DAY = "2026-10-27"


def _observed_stretches(passages: Sequence[PassageRow]) -> list[dict[str, Any]]:
    """Consecutive directly-observed stop pairs per run, with their scheduled duration."""
    runs: dict[tuple[str, int, str, str], list[PassageRow]] = defaultdict(list)
    for row in passages:
        if row.observed:
            runs[(row.day, row.route_id, row.vehicle_id, row.run_id)].append(row)

    stretches: list[dict[str, Any]] = []
    for (day, route_id, _, _), rows in runs.items():
        rows.sort(key=lambda row: row.stop_sequence)
        for left, right in pairwise(rows):
            if right.stop_sequence != left.stop_sequence + 1:
                continue
            scheduled = right.scheduled_ts - left.scheduled_ts
            actual = right.ts - left.ts
            if scheduled <= 0 or actual <= 0:
                continue
            stretches.append(
                {
                    "day": day,
                    "route_id": route_id,
                    "hour": (left.ts // 3600) % 24,
                    "key": (route_id, left.stop_id, right.stop_id),
                    "scheduled": scheduled,
                    "actual": actual,
                }
            )
    return stretches


def measure(passages: Sequence[PassageRow], train_day: str, holdout_day: str) -> dict[str, Any]:
    """Return per-route and pooled timetable-vs-fallback errors on the holdout day."""
    stretches = _observed_stretches(passages)
    train = [s for s in stretches if s["day"] == train_day]
    holdout = [s for s in stretches if s["day"] == holdout_day]
    if not train or not holdout:
        raise ValueError(f"need observed stretches on both {train_day} and {holdout_day}")

    by_stretch_hour: dict[tuple, list[float]] = defaultdict(list)
    by_stretch: dict[tuple, list[float]] = defaultdict(list)
    by_route_hour: dict[tuple, list[float]] = defaultdict(list)
    by_hour: dict[int, list[float]] = defaultdict(list)
    for s in train:
        ratio = clamp_ratio(s["actual"] / s["scheduled"])
        by_stretch_hour[(s["key"], s["hour"])].append(ratio)
        by_stretch[s["key"]].append(ratio)
        by_route_hour[(s["route_id"], s["hour"])].append(ratio)
        by_hour[s["hour"]].append(ratio)

    def predict(s: dict[str, Any]) -> tuple[float, str]:
        values = by_stretch_hour.get((s["key"], s["hour"]))
        if values:
            return s["scheduled"] * statistics.median(values), "stretch_hour"
        values = by_stretch.get(s["key"])
        if values:
            return s["scheduled"] * statistics.median(values), "stretch"
        values = by_route_hour.get((s["route_id"], s["hour"]))
        if values:
            return s["scheduled"] * statistics.median(values), "route_hour"
        values = by_hour.get(s["hour"])
        if values:
            return s["scheduled"] * statistics.median(values), "corridor_hour"
        return float(s["scheduled"]), "timetable"

    levels: dict[str, int] = defaultdict(int)
    per_route: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for s in holdout:
        predicted, level = predict(s)
        levels[level] += 1
        row = per_route[s["route_id"]]
        row[0] += abs(s["actual"] - s["scheduled"])
        row[1] += abs(s["actual"] - predicted)
        row[2] += 1

    timetable_total = sum(row[0] for row in per_route.values())
    fallback_total = sum(row[1] for row in per_route.values())
    return {
        "train_day": train_day,
        "holdout_day": holdout_day,
        "holdout_stretches": len(holdout),
        "timetable_mae_s": timetable_total / len(holdout),
        "fallback_mae_s": fallback_total / len(holdout),
        "gain_pct": 100 * (timetable_total - fallback_total) / timetable_total
        if timetable_total
        else 0.0,
        "levels": dict(levels),
        "by_route": {
            route_id: {
                "stretches": int(row[2]),
                "timetable_mae_s": row[0] / row[2],
                "fallback_mae_s": row[1] / row[2],
                # A perfect timetable leaves no error to remove, so the gain is undefined
                # rather than infinite. Report it as zero.
                "gain_pct": 100 * (row[0] - row[1]) / row[0] if row[0] else 0.0,
            }
            for route_id, row in sorted(per_route.items())
        },
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--routes", default=",".join(str(r) for r in DEFAULT_ROUTES))
    parser.add_argument("--train-day", default=TRAIN_DAY)
    parser.add_argument("--holdout-day", default=HOLDOUT_DAY)
    parser.add_argument(
        "--reconstruct",
        action="store_true",
        help=(
            "rebuild passages for --routes into --work-dir first. The committed "
            "data/passages/ holds route 717A only, and data.stop_passages refuses any "
            "other route, so a measurement on other routes needs this."
        ),
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="where --reconstruct writes passages (default: <data-dir>/fallback-check)",
    )
    args = parser.parse_args(argv)

    route_ids = {int(value) for value in args.routes.split(",")}
    passages_dir = args.data_dir / "passages"

    if args.reconstruct:
        from delhi_transit.data.gtfs_patterns import load_route_patterns
        from delhi_transit.data.stop_passages import reconstruct_day

        work_dir = args.work_dir or (args.data_dir / "fallback-check")
        work_dir.mkdir(parents=True, exist_ok=True)
        patterns = load_route_patterns(args.data_dir / "gtfs", sorted(route_ids))
        for day in (args.train_day, args.holdout_day):
            output = work_dir / f"day={day}.parquet"
            coverage, skipped = reconstruct_day(
                day, args.data_dir / "gps" / f"day={day}.parquet", patterns, output, overwrite=True
            )
            stops = sum(row["stops"] for row in coverage)
            observed = sum(row["observed"] for row in coverage)
            print(f"{day}: {len(coverage)} runs, {skipped} skipped, "
                  f"{100 * observed / stops:.1f}% observed")
        passages_dir = work_dir

    passages = [
        row for row in load_passages(passages_dir / "*.parquet") if row.route_id in route_ids
    ]
    if not passages:
        raise SystemExit(
            f"no passages for routes {sorted(route_ids)} under {passages_dir}; "
            "pass --reconstruct to build them"
        )
    result = measure(passages, args.train_day, args.holdout_day)

    print(f"train {result['train_day']} -> holdout {result['holdout_day']}  "
          f"({result['holdout_stretches']:,} observed stretches)")
    print(f"\n{'route':>7} {'n':>7} {'timetable':>10} {'fallback':>10} {'gain':>7}")
    for route_id, row in result["by_route"].items():
        print(f"{route_id:>7} {row['stretches']:>7} {row['timetable_mae_s']:>10.1f} "
              f"{row['fallback_mae_s']:>10.1f} {row['gain_pct']:>6.1f}%")
    print(f"\nPOOLED  timetable {result['timetable_mae_s']:.1f} s | "
          f"fallback {result['fallback_mae_s']:.1f} s | gain {result['gain_pct']:.1f}%")
    print(f"levels serving the prediction: {result['levels']}")


if __name__ == "__main__":
    main()
