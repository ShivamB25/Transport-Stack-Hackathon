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
import threading
import time
from pathlib import Path

import duckdb
import py7zr

from delhi_transit.core.config import (
    ARCHIVE_URL,
    BBOX_LAT_MAX,
    BBOX_LAT_MIN,
    BBOX_LNG_MAX,
    BBOX_LNG_MIN,
    DATA_DIR,
    GPS_DIR,
    IST,
    WORK_DIR,
)
from delhi_transit.data.range_reader import HTTPRangeFile

MIN_PLAUSIBLE_DB_BYTES = 1_000_000_000
MIN_FREE_BYTES = 9_000_000_000
PREVIOUS_EVENING_OVERLAP_S = 3600
MIN_IN_DAY_ROW_SHARE = 0.90

EXPECTED_SCHEMA: dict[str, tuple[str, ...]] = {
    "vehicle_id": ("VARCHAR",),
    "route_id": ("INTEGER", "INT32"),
    "trip_id": ("VARCHAR",),
    "ts": ("BIGINT", "INT64"),
    "lat": ("DOUBLE",),
    "lng": ("DOUBLE",),
    "speed": ("DOUBLE",),
}


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
    escaped_temp = str(temp_dir).replace("'", "''")
    con.execute(f"SET temp_directory='{escaped_temp}'")
    return con


def convert_day(db_path: Path, output_path: Path, temp_dir: Path) -> tuple[int, int]:
    """Dedupe and clean one raw SQLite day into Parquet; returns (rows_in, rows_out)."""
    con = connect_duckdb(temp_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output_path.with_name(
        f".{output_path.name}.{os.getpid()}.{threading.get_ident()}.tmp.parquet"
    )
    escaped_db = str(db_path).replace("'", "''")
    escaped_tmp = str(tmp_output).replace("'", "''")
    try:
        con.execute(f"ATTACH '{escaped_db}' AS src_db (TYPE sqlite, READ_ONLY)")
        rows_in = con.execute("SELECT count(*) FROM src_db.vehicle_feed").fetchone()[0]
        tmp_output.unlink(missing_ok=True)
        query = f"""
        COPY (
            WITH cleaned AS (
                SELECT
                    rowid AS _src_rowid,
                    vehicle_id,
                    TRY_CAST(route_id AS INTEGER) AS route_id,
                    trip_id,
                    TRY_CAST(timestamp AS BIGINT) AS ts,
                    TRY_CAST(lat AS DOUBLE) AS lat,
                    TRY_CAST(lng AS DOUBLE) AS lng,
                    TRY_CAST(speed AS DOUBLE) AS speed
                FROM src_db.vehicle_feed
            ), bounded AS (
                SELECT * FROM cleaned
                WHERE ts IS NOT NULL
                  AND lat BETWEEN {BBOX_LAT_MIN} AND {BBOX_LAT_MAX}
                  AND lng BETWEEN {BBOX_LNG_MIN} AND {BBOX_LNG_MAX}
            ), ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY vehicle_id, ts, lat, lng
                    ORDER BY _src_rowid ASC
                ) AS duplicate_rank
                FROM bounded
            )
            SELECT vehicle_id, route_id, trip_id, ts, lat, lng, speed
            FROM ranked WHERE duplicate_rank = 1
        ) TO '{escaped_tmp}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 1000000)
        """
        con.execute(query)
        rows_out = con.execute(
            "SELECT count(*) FROM read_parquet(?)", [str(tmp_output)]
        ).fetchone()[0]
        if rows_out < 1:
            raise ValueError("conversion produced no rows")
        os.replace(tmp_output, output_path)
        return rows_in, rows_out
    finally:
        tmp_output.unlink(missing_ok=True)
        con.close()


def verify_day(
    day: int,
    output_path: Path,
    *,
    min_rows: int | None = None,
    min_vehicles: int | None = None,
) -> dict:
    """Verify schema, counts, IST day window, bbox, and zero duplicates for a Parquet day."""
    if not output_path.is_file():
        raise ValueError(f"verification failed: file does not exist: {output_path}")

    day_start_ist = int(dt.datetime(2026, 10, day, 0, 0, 0, tzinfo=IST).timestamp())
    day_end_ist = day_start_ist + 86_400
    earliest_allowed = day_start_ist - PREVIOUS_EVENING_OVERLAP_S
    latest_allowed = day_end_ist
    six_ist = day_start_ist + 6 * 3600
    nineteen_ist = day_start_ist + 19 * 3600

    con = duckdb.connect()
    try:
        con.execute("SET threads=2")
        con.execute("SET preserve_insertion_order=false")
        try:
            schema_rows = con.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(output_path)]
            ).fetchall()
        except duckdb.Error as exc:
            raise ValueError(f"verification failed: unreadable Parquet: {exc}") from exc

        actual_schema = {name: dtype.upper() for name, dtype, *_ in schema_rows}
        schema_problems: list[str] = []
        missing_cols = sorted(set(EXPECTED_SCHEMA) - set(actual_schema))
        extra_cols = sorted(set(actual_schema) - set(EXPECTED_SCHEMA))
        if missing_cols:
            schema_problems.append(f"missing columns: {', '.join(missing_cols)}")
        if extra_cols:
            schema_problems.append(f"unexpected columns: {', '.join(extra_cols)}")
        for col, allowed_types in EXPECTED_SCHEMA.items():
            if col in actual_schema and actual_schema[col] not in allowed_types:
                schema_problems.append(
                    f"column {col} has type {actual_schema[col]}, expected one of {allowed_types}"
                )
        if schema_problems:
            raise ValueError("verification failed: " + "; ".join(schema_problems))

        try:
            (
                rows,
                vehicles,
                min_ts,
                max_ts,
                null_ts,
                out_of_window_ts,
                in_day_rows,
                bbox_violations,
            ) = con.execute(
                """
                SELECT
                    count(*),
                    count(DISTINCT vehicle_id),
                    min(ts),
                    max(ts),
                    count(*) FILTER (WHERE ts IS NULL),
                    count(*) FILTER (WHERE ts < ? OR ts > ?),
                    count(*) FILTER (WHERE ts >= ? AND ts < ?),
                    count(*) FILTER (
                        WHERE lat IS NULL OR lng IS NULL
                           OR lat < ? OR lat > ?
                           OR lng < ? OR lng > ?
                    )
                FROM read_parquet(?)
                """,
                [
                    earliest_allowed,
                    latest_allowed,
                    day_start_ist,
                    day_end_ist,
                    BBOX_LAT_MIN,
                    BBOX_LAT_MAX,
                    BBOX_LNG_MIN,
                    BBOX_LNG_MAX,
                    str(output_path),
                ],
            ).fetchone()
            has_duplicates = con.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM read_parquet(?)
                    GROUP BY vehicle_id, ts, lat, lng
                    HAVING count(*) > 1
                )
                """,
                [str(output_path)],
            ).fetchone()[0]
        except duckdb.Error as exc:
            raise ValueError(f"verification failed: corrupt Parquet: {exc}") from exc
    finally:
        con.close()

    expected_min_rows = (
        min_rows
        if min_rows is not None
        else (1_000_001 if day == 7 else 10_000_001)
    )
    expected_min_vehicles = min_vehicles if min_vehicles is not None else 4001

    problems: list[str] = []
    if rows < expected_min_rows:
        problems.append(f"rows {rows} < {expected_min_rows}")
    if vehicles < expected_min_vehicles:
        problems.append(f"vehicles {vehicles} < {expected_min_vehicles}")
    if null_ts > 0:
        problems.append(f"{null_ts} rows have NULL ts")
    if out_of_window_ts > 0:
        problems.append(
            f"{out_of_window_ts} rows outside archive-day window [{earliest_allowed}, {latest_allowed}]"
        )
    if min_ts is None or min_ts > six_ist:
        problems.append(f"min_ts {min_ts} after 06:00 IST")
    if max_ts is None or max_ts < nineteen_ist:
        problems.append(f"max_ts {max_ts} before 19:00 IST")
    if rows > 0 and (in_day_rows / rows) < MIN_IN_DAY_ROW_SHARE:
        problems.append(
            f"only {in_day_rows}/{rows} ({in_day_rows / rows:.2%}) rows inside nominal IST day {day_key(day)}"
        )
    if bbox_violations > 0:
        problems.append(f"{bbox_violations} bbox/coordinate violations")
    if has_duplicates:
        problems.append("duplicate (vehicle_id, ts, lat, lng) keys present")
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
    """Extract, convert, stage, verify, and atomically publish one day."""
    output_path = output_path or default_output_path(data_dir, day)
    db_path = work_dir / day_db_name(day)
    staged_path = output_path.with_name(
        f".{output_path.name}.{os.getpid()}.{threading.get_ident()}.staged.parquet"
    )
    free_bytes = shutil.disk_usage(data_dir).free
    if free_bytes < MIN_FREE_BYTES:
        raise OSError(f"only {free_bytes / 1e9:.1f} GB free; refusing extraction")
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "duckdb-tmp").mkdir(exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        remove_sqlite(db_path)
        staged_path.unlink(missing_ok=True)
        extract_seconds, fetched = extract_day_db(day, db_path, work_dir, archive_url)
        rows_in, rows_out = convert_day(db_path, staged_path, work_dir / "duckdb-tmp")
        verified = verify_day(day, staged_path)
        os.replace(staged_path, output_path)
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
        staged_path.unlink(missing_ok=True)
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
