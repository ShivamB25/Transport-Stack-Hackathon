"""Extract one October 2026 GPS day from the remote 7z and write clean Parquet.

Range-reads a single day's SQLite member from the 16 GB archive (User-Agent
curl/8.5.0), deduplicates on (vehicle_id, ts, lat, lng) keeping one row per
group, casts route_id REAL -> INTEGER (NULL when unparseable), keeps epoch
seconds UTC as ts, filters to the Delhi bbox, and deletes the SQLite file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import time
from pathlib import Path

import duckdb
import py7zr

from ps1.config import (
    ARCHIVE_URL,
    BBOX_LAT_MAX,
    BBOX_LAT_MIN,
    BBOX_LNG_MAX,
    BBOX_LNG_MIN,
    DATA_DIR,
    GPS_DIR,
    WORK_DIR,
)
from ps1.range_reader import HTTPRangeFile

MIN_PLAUSIBLE_DB_BYTES = 1_000_000_000
MIN_FREE_BYTES = 9_000_000_000
IST_OFFSET_SECONDS = 5 * 3600 + 30 * 60


def day_key(day: int) -> str:
    return f"2026-10-{day:02d}"


def parse_day(value: str) -> int:
    """Accept a day of month (1-31) or an ISO date inside October 2026."""
    if "-" in value:
        parsed = dt.date.fromisoformat(value)
        if parsed.year != 2026 or parsed.month != 10:
            raise ValueError(f"date outside October 2026: {value}")
        return parsed.day
    day = int(value)
    if not 1 <= day <= 31:
        raise ValueError(f"day out of range for October 2026: {value}")
    return day


def day_db_name(day: int) -> str:
    return f"bus_movements_2026_10_{day:02d}.db"


def default_output_path(data_dir: Path, day: int) -> Path:
    return data_dir / GPS_DIR / f"day={day_key(day)}.parquet"


def extract_day_db(
    day: int, db_path: Path, work_dir: Path, archive_url: str = ARCHIVE_URL
) -> tuple[float, int]:
    """Range-extract one day's SQLite member; returns (seconds, bytes fetched)."""
    started = time.monotonic()
    remote = HTTPRangeFile(archive_url)
    try:
        with py7zr.SevenZipFile(remote, mode="r") as archive:
            archive.extract(path=work_dir, targets=[day_db_name(day)])
    finally:
        remote.close()
    if not db_path.is_file() or db_path.stat().st_size < MIN_PLAUSIBLE_DB_BYTES:
        raise OSError(f"extraction did not create a plausible SQLite file: {db_path}")
    return time.monotonic() - started, remote.nbytes


def connect_duckdb(temp_dir: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("LOAD sqlite")
    con.execute("SET threads=2")
    con.execute("SET memory_limit='2GB'")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory='{temp_dir}'")
    return con


def convert_day(db_path: Path, output_path: Path, temp_dir: Path) -> tuple[int, int]:
    """Dedupe and clean one raw SQLite day into Parquet; returns (rows_in, rows_out)."""
    con = connect_duckdb(temp_dir)
    tmp_output = output_path.with_suffix(".tmp.parquet")
    escaped_db = str(db_path).replace("'", "''")
    source = f"sqlite_scan('{escaped_db}', 'vehicle_feed')"
    try:
        rows_in = con.execute(f"SELECT count(*) FROM {source}").fetchone()[0]
        tmp_output.unlink(missing_ok=True)
        query = f"""
        COPY (
            WITH cleaned AS (
                SELECT
                    vehicle_id,
                    TRY_CAST(route_id AS INTEGER) AS route_id,
                    trip_id,
                    TRY_CAST(timestamp AS BIGINT) AS ts,
                    TRY_CAST(lat AS DOUBLE) AS lat,
                    TRY_CAST(lng AS DOUBLE) AS lng,
                    TRY_CAST(speed AS DOUBLE) AS speed
                FROM {source}
            ), bounded AS (
                SELECT * FROM cleaned
                WHERE ts IS NOT NULL
                  AND lat BETWEEN {BBOX_LAT_MIN} AND {BBOX_LAT_MAX}
                  AND lng BETWEEN {BBOX_LNG_MIN} AND {BBOX_LNG_MAX}
            ), ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY vehicle_id, ts, lat, lng
                ) AS duplicate_rank
                FROM bounded
            )
            SELECT vehicle_id, route_id, trip_id, ts, lat, lng, speed
            FROM ranked WHERE duplicate_rank = 1
        ) TO '{tmp_output}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 1000000)
        """
        con.execute(query)
        rows_out = con.execute(f"SELECT count(*) FROM read_parquet('{tmp_output}')").fetchone()[0]
        if rows_out < 1:
            raise ValueError("conversion produced no rows")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_output, output_path)
        return rows_in, rows_out
    finally:
        tmp_output.unlink(missing_ok=True)
        con.close()


def verify_day(day: int, output_path: Path) -> dict:
    """Check row/vehicle counts and the IST time span of a finished Parquet day."""
    con = duckdb.connect()
    try:
        con.execute("SET threads=2")
        rows, vehicles, min_ts, max_ts = con.execute(
            f"SELECT count(*), count(DISTINCT vehicle_id), min(ts), max(ts) "
            f"FROM read_parquet('{output_path}')"
        ).fetchone()
    finally:
        con.close()
    day_start = int(dt.datetime(2026, 10, day, tzinfo=dt.UTC).timestamp())
    six_ist = day_start - IST_OFFSET_SECONDS + 6 * 3600
    nineteen_ist = day_start - IST_OFFSET_SECONDS + 19 * 3600
    problems = []
    if day != 7 and rows <= 10_000_000:
        problems.append(f"rows {rows} <= 10000000")
    if vehicles <= 4000:
        problems.append(f"vehicles {vehicles} <= 4000")
    if min_ts is None or min_ts > six_ist:
        problems.append(f"min_ts {min_ts} after 06:00 IST")
    if max_ts is None or max_ts < nineteen_ist:
        problems.append(f"max_ts {max_ts} before 19:00 IST")
    if problems:
        raise ValueError("verification failed: " + "; ".join(problems))
    return {"rows": rows, "vehicles": vehicles, "min_ts": min_ts, "max_ts": max_ts}


def remove_sqlite(db_path: Path) -> None:
    for suffix in ("", "-shm", "-wal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def run_day(
    day: int,
    data_dir: Path,
    work_dir: Path,
    output_path: Path | None = None,
    archive_url: str = ARCHIVE_URL,
) -> dict:
    """Extract, convert and verify one day; always removes the intermediate SQLite."""
    output_path = output_path or default_output_path(data_dir, day)
    db_path = work_dir / day_db_name(day)
    free_gb = shutil.disk_usage(data_dir).free / 1e9
    if shutil.disk_usage(data_dir).free < MIN_FREE_BYTES:
        raise OSError(f"only {free_gb:.1f} GB free; refusing extraction")
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "duckdb-tmp").mkdir(exist_ok=True)
    try:
        remove_sqlite(db_path)
        extract_seconds, fetched = extract_day_db(day, db_path, work_dir, archive_url)
        rows_in, rows_out = convert_day(db_path, output_path, work_dir / "duckdb-tmp")
        verified = verify_day(day, output_path)
        return {
            "day": day_key(day),
            "status": "ok",
            "rows_in": rows_in,
            "rows_out": rows_out,
            "extract_seconds": round(extract_seconds, 1),
            "fetched_bytes": fetched,
            "output": str(output_path),
            **verified,
        }
    finally:
        remove_sqlite(db_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, help="day of month (1-31) or ISO date in October 2026")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--work-dir", type=Path, default=WORK_DIR)
    parser.add_argument("--output", type=Path, default=None, help="override the output Parquet path")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    day = parse_day(args.day)
    output = args.output or default_output_path(args.data_dir, day)
    if output.exists() and not args.overwrite:
        raise SystemExit(f"output exists (use --overwrite): {output}")
    started = time.monotonic()
    result = run_day(day, args.data_dir, args.work_dir, output_path=output)
    result["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
