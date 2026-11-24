"""Observed/interpolated coverage report for reconstructed 717A stop passages."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from delhi_transit.core.config import REPO_ROOT, portable_path

SCOPED_DAYS = tuple(
    f"2026-10-{day:02d}" for day in (5, 6, 7, 8, 13, 14, 15, 22, 27, 28)
)
CANONICAL_ROUTES = frozenset({2170, 2171})
CANONICAL_COVERAGE_REPORT = REPO_ROOT / "reports" / "coverage-717a.md"


def write_coverage_report(
    path: Path,
    rows: Sequence[dict],
    skipped_by_day: dict[str, int],
    outputs: dict[str, Path],
    radius_m: float,
) -> None:
    """Write aggregate and per-trip observed/interpolated coverage from actual rows."""
    groups = defaultdict(lambda: [0, 0, 0, 0])
    for row in rows:
        for key in ((row["day"], row["direction"]), ("ALL", row["direction"])):
            group = groups[key]
            group[0] += 1
            group[1] += row["stops"]
            group[2] += row["observed"]
            group[3] += row["interpolated"]
    lines = [
        "# 717A stop-passage coverage",
        "",
        (
            "Geometric reconstruction uses the dominant FULL GTFS pattern for 717AUP "
            "(route 2171) and 717ADOWN (route 2170). GPS trip IDs label runs only; "
            "they are not GTFS join keys. Candidate observations are within "
            f"{radius_m:g} m, with minimum distance per stop and a maximum-cardinality "
            "time-ordered anchor chain. Runs with fewer than two surviving anchors or an "
            "observed span over six hours are excluded. Gaps are schedule-proportionally "
            "interpolated; endpoints use schedule-rate extrapolation. Each passage's "
            "`available_at` is the latest of its own anchor rule, the run's second surviving "
            "anchor (when the run first qualifies), and the time after which every raw-GPS "
            "prefix reconstruction reproduces the published passage."
        ),
        "",
        (
            "Historical coverage on the older feed was approximately 46% UP and 42–48% "
            "DOWN. The percentages below are recomputed from this feed and these outputs."
        ),
        "",
        "## Summary",
        "",
        "| Day | Direction | Trips | Stops | Observed | Interpolated | Observed coverage |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for day, direction in sorted(groups, key=lambda key: (key[0] != "ALL", key)):
        trips, stops, observed, interpolated = groups[(day, direction)]
        lines.append(
            f"| {day} | {direction} | {trips} | {stops} | {observed} | "
            f"{interpolated} | {100 * observed / stops:.2f}% |"
        )
    lines.extend(["", "## Outputs and excluded candidate runs", ""])
    for day in sorted(outputs):
        lines.append(
            f"- {day}: `{portable_path(outputs[day])}`; excluded {skipped_by_day[day]} runs "
            "with too few anchors or an implausible observed span."
        )
    lines.extend(
        [
            "",
            (
                "For every reconstructed trip, `stops = observed + interpolated`. "
                "Timestamps and stop order are nondecreasing within each trip."
            ),
            "",
            "## Per-trip coverage",
            "",
            "| Day | Direction | Vehicle | GPS run label | Stops | Observed | Interpolated | Coverage |",
            "|---|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in sorted(
        rows,
        key=lambda r: (r["day"], r["direction"], r["first_ts"], r["vehicle_id"], r["run_id"]),
    ):
        lines.append(
            f"| {row['day']} | {row['direction']} | {row['vehicle_id']} | "
            f"{row['run_id']} | {row['stops']} | {row['observed']} | "
            f"{row['interpolated']} | {row['coverage_pct']:.2f}% |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def resolve_report_path(
    report: Path | None,
    days: Sequence[str],
    route_ids: set[int],
    output_dir: Path,
) -> Path:
    """Protect the canonical coverage report from being overwritten by scoped runs."""
    is_canonical = set(days) == set(SCOPED_DAYS) and route_ids == set(CANONICAL_ROUTES)
    if report is not None:
        if not is_canonical and report.resolve() == CANONICAL_COVERAGE_REPORT.resolve():
            raise ValueError(
                f"refusing to overwrite canonical coverage report {CANONICAL_COVERAGE_REPORT} "
                "from a scoped run; pass a distinct --report path"
            )
        return report
    if is_canonical:
        return CANONICAL_COVERAGE_REPORT
    sorted_days = sorted(set(days))
    label = (
        sorted_days[0]
        if len(sorted_days) == 1
        else f"{sorted_days[0]}_to_{sorted_days[-1]}"
    )
    return output_dir / f"coverage-scoped-{label}.md"
