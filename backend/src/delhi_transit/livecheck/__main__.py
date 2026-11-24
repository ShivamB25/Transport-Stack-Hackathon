"""`python -m delhi_transit.livecheck {run,actuals,analyze}`: the one-day live accuracy check."""
from __future__ import annotations

import argparse
import json
import signal
from datetime import datetime
from pathlib import Path

from delhi_transit.comparator.client import GoogleRoutesClient
from delhi_transit.core.config import DATA_DIR, GOOGLE_DIR, IST, REPO_ROOT
from delhi_transit.livecheck.actuals import build_actuals
from delhi_transit.livecheck.analyze import analyze
from delhi_transit.livecheck.record import Recorder, RunSettings, parse_clock
from delhi_transit.livecheck.report import write_reports


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="record the live feed, live /journeys responses and a Google sample")
    run.add_argument("--until", required=True, help="end time: HH:MM IST today or +Nm")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--api-url", default="http://127.0.0.1:3120")
    run.add_argument("--journeys-until", default="23:00", help="last slot start: HH:MM IST or +Nm")
    run.add_argument("--slot-minutes", type=float, default=20.0)
    run.add_argument("--poll-interval", type=float, default=25.0)
    run.add_argument("--google-slots", default="21:45,22:15,22:45", help="comma-separated HH:MM IST; empty for none")
    run.add_argument("--google-max", type=int, default=30, help="paid Google calls allowed in this run")
    run.add_argument("--google-ledger-limit", type=int, default=122, help="never let the mission ledger exceed this")
    run.add_argument("--data-dir", type=Path, default=DATA_DIR)
    actuals = sub.add_parser("actuals", help="rebuild 717A passages from the recorded positions")
    actuals.add_argument("--in", dest="in_dir", type=Path, required=True)
    actuals.add_argument("--data-dir", type=Path, default=DATA_DIR)
    report = sub.add_parser("analyze", help="match predicted 717A legs to actual runs and write the report")
    report.add_argument("--in", dest="in_dir", type=Path, required=True)
    report.add_argument("--report-dir", type=Path, default=REPO_ROOT / "reports")
    return parser.parse_args(argv)


def run_command(args: argparse.Namespace) -> int:
    start = datetime.now(IST)
    until = parse_clock(args.until, start)
    if until <= start:
        raise SystemExit(f"--until {args.until} is not in the future")
    journeys_until = min(parse_clock(args.journeys_until, start), until)
    google_slots = [parse_clock(value.strip(), start) for value in args.google_slots.split(",") if value.strip()]
    settings = RunSettings(
        out_dir=args.out,
        api_url=args.api_url,
        until=until,
        journeys_until=journeys_until,
        slot_s=args.slot_minutes * 60,
        poll_interval_s=args.poll_interval,
        google_slots=google_slots,
        google_max=args.google_max,
        google_ledger_limit=args.google_ledger_limit,
        gtfs_dir=args.data_dir / "gtfs",
    )
    google = GoogleRoutesClient(cache_dir=args.data_dir / GOOGLE_DIR)
    recorder = Recorder(settings, google_client=google)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: recorder.stop.set())
    summary = recorder.run()
    print(json.dumps({"polls": summary["polls"], "slots": len(summary["slots"]), "google": summary["google"]}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "run":
        return run_command(args)
    if args.command == "actuals":
        summary = build_actuals(args.in_dir, args.data_dir / "gtfs")
        print(json.dumps(summary, indent=2))
        return 0
    result = analyze(args.in_dir)
    paths = write_reports(result, args.report_dir)
    acc = result["accuracy"]
    print(json.dumps({
        "reports": {key: str(path) for key, path in paths.items()},
        "responses": result["responses"]["by_status"],
        "legs_total": acc["legs_total"],
        "legs_matched": acc["legs_matched"],
        "overall": acc["overall"],
        "observed_only": acc["observed_only"],
        "unmatched_by_reason": acc["unmatched_by_reason"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
