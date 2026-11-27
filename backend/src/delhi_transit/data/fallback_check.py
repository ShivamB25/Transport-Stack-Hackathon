"""Measure the causal Kalman ratio fallback against the timetable on routes with no learned model.

The README says only route 717A has a trained model. This reports what the *fallback*
actually achieves on other routes, so that limitation carries a number instead of an
adjective.

It reproduces the causal fallback path in `planner/rides.py`: a scalar Kalman filter
over the stretch ratio (`actual / scheduled`) per `(route_id, from_stop, to_stop)` per
service day, seeded from the historical median prior (`stretch_hour`, then `stretch`,
then `route_hour`, then `corridor_hour`, else `1.0`) and updated strictly causally from
same-day observed stretches (`available_at < query_ts`, `to_ts <= query_ts`).
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

from delhi_transit.core.config import DATA_DIR, IST
from delhi_transit.model.features import PassageRow, load_passages
from delhi_transit.planner.rides import KALMAN_INIT_VAR, kalman_step
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
                    "hour": datetime.fromtimestamp(left.ts, IST).hour,
                    "key": (route_id, left.stop_id, right.stop_id),
                    "run_id": left.run_id,
                    "from_sequence": left.stop_sequence,
                    "query_ts": left.ts,
                    "to_ts": right.ts,
                    "available_at": max(left.source_available_at, right.source_available_at),
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

    def prior_for(s: dict[str, Any]) -> tuple[float, str]:
        values = by_stretch_hour.get((s["key"], s["hour"]))
        if values:
            return median(values), "stretch_hour"
        values = by_stretch.get(s["key"])
        if values:
            return median(values), "stretch"
        values = by_route_hour.get((s["route_id"], s["hour"]))
        if values:
            return median(values), "route_hour"
        values = by_hour.get(s["hour"])
        if values:
            return median(values), "corridor_hour"
        return 1.0, "timetable"

    holdout.sort(key=lambda s: (s["query_ts"], s["run_id"], s["from_sequence"]))

    by_key_avail: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    by_route_avail: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for s in holdout:
        by_key_avail[s["key"]].append(s)
        by_route_avail[s["route_id"]].append(s)
    for group in by_key_avail.values():
        group.sort(key=lambda s: (s["available_at"], s["query_ts"], s["run_id"], s["from_sequence"]))
    for group in by_route_avail.values():
        group.sort(key=lambda s: (s["available_at"], s["query_ts"], s["run_id"], s["from_sequence"]))
    key_avail_ts = {k: [s["available_at"] for s in group] for k, group in by_key_avail.items()}
    route_avail_ts = {rid: [s["available_at"] for s in group] for rid, group in by_route_avail.items()}

    kf_state: dict[tuple, float] = {}
    kf_var: dict[tuple, float] = {}
    kf_applied: dict[tuple, int] = defaultdict(int)

    def predict(s: dict[str, Any]) -> tuple[float, str]:
        as_of_ts = s["query_ts"]
        key = s["key"]
        prior, hist_level = prior_for(s)

        k_idx = bisect_left(key_avail_ts[key], as_of_ts)
        same_day = [
            row
            for row in by_key_avail[key][:k_idx]
            if row["query_ts"] < as_of_ts and row["to_ts"] <= as_of_ts
        ]
        if same_day:
            if key not in kf_state:
                kf_state[key] = prior
                kf_var[key] = KALMAN_INIT_VAR
            state = kf_state[key]
            var = kf_var[key]
            for row in same_day[kf_applied[key] :]:
                state, var = kalman_step(state, var, row["actual"] / row["scheduled"])
            kf_state[key] = state
            kf_var[key] = var
            kf_applied[key] = len(same_day)
            return max(1.0, s["scheduled"] * state), "kalman_exact"

        route_id = s["route_id"]
        r_hi = bisect_left(route_avail_ts[route_id], as_of_ts)
        r_lo = bisect_left(route_avail_ts[route_id], as_of_ts - 7200)
        same_day_route = [
            row
            for row in by_route_avail[route_id][r_lo:r_hi]
            if row["query_ts"] < as_of_ts and row["to_ts"] <= as_of_ts
        ]
        if same_day_route:
            state = prior
            var = KALMAN_INIT_VAR
            for row in same_day_route[-20:]:
                state, var = kalman_step(state, var, row["actual"] / row["scheduled"])
            return max(1.0, s["scheduled"] * state), "kalman_route"

        if hist_level == "timetable":
            return float(s["scheduled"]), "timetable"
        return max(1.0, s["scheduled"] * prior), hist_level

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
