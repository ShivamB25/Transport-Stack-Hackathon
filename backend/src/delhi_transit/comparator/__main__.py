"""`python -m delhi_transit.comparator`: collect (planner + cached/paid Google) or render from frozen inputs."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from delhi_transit.comparator.client import (
    DEFAULT_FIELD_MASK,
    GoogleRoutesClient,
    upcoming_weekday_date,
)
from delhi_transit.comparator.compare import render_comparison, run_comparison
from delhi_transit.comparator.slots import TARGET_WEEKDAY_TUESDAY
from delhi_transit.core.config import (
    DATA_DIR,
    GOOGLE_DIR,
    GOOGLE_HARD_CALL_CAP,
    REPO_ROOT,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare our model-timed bus journeys against Google Routes API (bus-only)."
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Upcoming IST date YYYY-MM-DD (default: next upcoming Tuesday matching Oct 27 & brief example).",
    )
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:3100",
        help="Base URL of the local FastAPI journeys service.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="Data directory (raw Google responses cached under <data-dir>/google/).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=REPO_ROOT / "reports",
        help="Output directory for google-comparator.{md,csv} and google-manifest.json.",
    )
    parser.add_argument(
        "--hard-cap",
        type=int,
        default=GOOGLE_HARD_CALL_CAP,
        help="Mission-wide Google Routes API call hard cap (max 300).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--cache-only",
        action="store_true",
        help="During collection, use only cached Google responses and retain cache misses as unavailable rows.",
    )
    mode.add_argument(
        "--render-only",
        action="store_true",
        help="Render exclusively from frozen inputs; do not contact the planner or Google.",
    )
    parser.add_argument(
        "--inputs",
        type=Path,
        default=REPO_ROOT / "reports" / "google-comparator-inputs.json",
        help="Frozen comparison inputs for --render-only (default: reports/google-comparator-inputs.json).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.render_only:
        _rows, manifest = render_comparison(
            inputs_path=Path(args.inputs),
            report_dir=Path(args.report_dir),
        )
    else:
        if args.date:
            target_date = date.fromisoformat(args.date)
        else:
            target_date = upcoming_weekday_date(TARGET_WEEKDAY_TUESDAY, min_time_ist=(8, 30))

        client = GoogleRoutesClient(
            cache_dir=Path(args.data_dir) / GOOGLE_DIR,
            hard_cap=args.hard_cap,
            field_mask=DEFAULT_FIELD_MASK,
        )
        _rows, manifest = run_comparison(
            target_date=target_date,
            api_url=args.api_url,
            google_client=client,
            report_dir=Path(args.report_dir),
            cache_only=args.cache_only,
            inputs_path=Path(args.inputs),
        )
    acc = manifest["call_accounting"]
    corpus = manifest["corpus_summary"]
    print(
        json.dumps(
            {
                "status": "ok",
                "target_date_ist": manifest["target_date_ist"],
                "planned_slots": corpus["planned_slots"],
                "compared_rows": corpus["compared_rows"],
                "missing_or_error_rows": corpus["missing_or_error_rows"],
                "non_bus_itineraries_filtered": corpus["google_non_bus_itineraries_filtered"],
                "total_mission_calls": acc["total_mission_calls"],
                "hard_cap": acc["hard_cap"],
                "cache_hits_this_run": acc["cache_hits_this_run"],
                "network_calls_this_run": acc["network_calls_this_run"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
