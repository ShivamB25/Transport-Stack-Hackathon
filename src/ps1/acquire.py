"""Orchestrate extraction of the 31 October 2026 GPS days, priority days first.

Resumable: days whose Parquet output already exists are verified in place and
their raw row counts recovered from download.log, so a rerun produces a complete
STATUS.md without re-downloading finished days. Appends to download.log.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import threading
import time
import traceback
from pathlib import Path

from ps1.config import DATA_DIR, GPS_DIR, WORK_DIR
from ps1.extract_day import day_key, default_output_path, run_day, verify_day

DEFAULT_DAYS = [
    15, 22, 27, 28, 5, 6, 8, 13, 14,
    1, 2, 3, 4, 7, 9, 10, 11, 12, 16, 17, 18, 19, 20, 21, 23, 24, 25, 26, 29, 30, 31,
]
LOG_LOCK = threading.Lock()
STATUS_LOCK = threading.Lock()
RESULTS: dict[str, dict] = {}

LOG_LINE = re.compile(r"day=(\S+) status=ok rows_in=(\d+)")


def validate_days(days: list[int]) -> list[int]:
    if sorted(set(days)) != sorted(days):
        raise ValueError("day list contains duplicates")
    if any(day < 1 or day > 31 for day in days):
        raise ValueError("days must be in October 2026 (1..31)")
    return days


def recover_rows_in(log_path: Path) -> dict[str, int]:
    """Last recorded raw row count per day from download.log (for resumed runs)."""
    recovered: dict[str, int] = {}
    if log_path.is_file():
        for line in log_path.read_text().splitlines():
            match = LOG_LINE.search(line)
            if match:
                recovered[match.group(1)] = int(match.group(2))
    return recovered


def detail(message: str) -> None:
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
    with LOG_LOCK:
        print(line, flush=True)


def final_log(data_dir: Path, result: dict) -> None:
    fields = [
        f"day={result['day']}",
        f"status={result['status']}",
        f"rows_in={result.get('rows_in', '')}",
        f"rows_out={result.get('rows_out', '')}",
        f"seconds={result['seconds']:.1f}",
    ]
    if result.get("error"):
        fields.append("error=" + json.dumps(result["error"]))
    with LOG_LOCK, (data_dir / "download.log").open("a") as handle:
        handle.write(" ".join(fields) + "\n")


def write_status(data_dir: Path, days: list[int], finished: bool = False) -> None:
    with STATUS_LOCK:
        snapshots = dict(RESULTS)
    parquet_files = list((data_dir / GPS_DIR).glob("day=*.parquet"))
    total_bytes = sum(p.stat().st_size for p in parquet_files)
    lines = [
        "# GPS acquisition status",
        "",
        f"Updated: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"State: {'finished' if finished else 'running'}",
        f"Completed OK: {sum(r['status'] == 'ok' for r in snapshots.values())}/{len(days)}",
        f"Total Parquet size: {total_bytes / 1e9:.3f} GB ({total_bytes / 2**30:.3f} GiB)",
        "",
        "| Day | Status | Rows in | Rows out | Vehicles | Min ts | Max ts | Seconds | Error |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for day in days:
        key = day_key(day)
        r = snapshots.get(key)
        if not r:
            lines.append(f"| {key} | pending | | | | | | | |")
        else:
            lines.append(
                f"| {key} | {r['status']} | {r.get('rows_in', '')} | {r.get('rows_out', '')} "
                f"| {r.get('vehicles', '')} | {r.get('min_ts', '')} | {r.get('max_ts', '')} "
                f"| {r['seconds']:.1f} | {str(r.get('error', '')).replace('|', '/')} |"
            )
    failed = [day for day, r in snapshots.items() if r["status"] != "ok"]
    lines.extend(["", "Failed days: " + (", ".join(failed) if failed else "none"), ""])
    tmp = data_dir / "STATUS.md.tmp"
    tmp.write_text("\n".join(lines))
    os.replace(tmp, data_dir / "STATUS.md")


def process_day(day: int, args: argparse.Namespace) -> dict:
    key = day_key(day)
    began = time.monotonic()
    output_path = default_output_path(args.data_dir, day)
    result = {"day": key, "status": "failed"}
    try:
        if output_path.exists() and not args.overwrite:
            verified = verify_day(day, output_path)
            result.update(
                status="ok",
                rows_out=verified["rows"],
                vehicles=verified["vehicles"],
                min_ts=verified["min_ts"],
                max_ts=verified["max_ts"],
            )
            rows_in = args.rows_in_log.get(key)
            if rows_in is not None:
                result["rows_in"] = rows_in
            detail(f"{key} existing output verified")
            return result
        for attempt in range(1, 3):
            try:
                detail(f"{key} attempt {attempt}: extraction started")
                outcome = run_day(day, args.data_dir, args.work_dir)
                result.update(
                    status="ok",
                    rows_in=outcome["rows_in"],
                    rows_out=outcome["rows_out"],
                    vehicles=outcome["vehicles"],
                    min_ts=outcome["min_ts"],
                    max_ts=outcome["max_ts"],
                    extract_seconds=outcome["extract_seconds"],
                )
                detail(
                    f"{key} verified: rows_in={outcome['rows_in']}, "
                    f"rows_out={outcome['rows_out']}, vehicles={outcome['vehicles']}"
                )
                break
            except Exception as exc:  # noqa: BLE001 - one day's failure must not kill the pool
                result["error"] = f"attempt {attempt}: {type(exc).__name__}: {exc}"
                detail(f"{key} attempt {attempt} failed: {exc}")
                if attempt == 2:
                    traceback.print_exc()
        return result
    except Exception as exc:  # noqa: BLE001 - record and continue with other days
        result["error"] = f"{type(exc).__name__}: {exc}"
        detail(f"{key} aborted: {exc}")
        return result
    finally:
        result["seconds"] = time.monotonic() - began


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", nargs="+", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--workers", type=int, default=2, choices=[1, 2])
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--work-dir", type=Path, default=WORK_DIR)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.days = validate_days(args.days)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    (args.data_dir / GPS_DIR).mkdir(exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    (args.work_dir / "duckdb-tmp").mkdir(exist_ok=True)
    args.rows_in_log = recover_rows_in(args.data_dir / "download.log")
    detail(f"pipeline started for {len(args.days)} days with {args.workers} workers")
    write_status(args.data_dir, args.days)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process_day, day, args) for day in args.days]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            with STATUS_LOCK:
                RESULTS[result["day"]] = result
            final_log(args.data_dir, result)
            write_status(args.data_dir, args.days)
    write_status(args.data_dir, args.days, finished=True)
    ok = sum(r["status"] == "ok" for r in RESULTS.values())
    detail(f"pipeline finished: {ok}/{len(args.days)} days OK")
    return 0 if ok == len(args.days) else 1


if __name__ == "__main__":
    raise SystemExit(main())
