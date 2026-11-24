"""`livecheck actuals`: recorded live positions -> archive GPS schema -> existing 717A passage reconstruction."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from delhi_transit.core.io import atomic_write_json, atomic_write_parquet
from delhi_transit.data.gtfs_patterns import load_route_patterns
from delhi_transit.data.stop_passages import DEFAULT_RADIUS_M, reconstruct_day
from delhi_transit.livecheck.record import ROUTES_717A


def recorded_day(in_dir: Path) -> str:
    return json.loads((Path(in_dir) / "run-summary.json").read_text(encoding="utf-8"))["day"]


def write_archive_gps(in_dir: Path, output: Path) -> dict[str, int]:
    """Union every saved snapshot and keep the first copy of each (vehicle_id, ts, lat, lng), as the archive does."""
    snapshots = sorted((Path(in_dir) / "gps").glob("vp_*.parquet"))
    if not snapshots:
        raise FileNotFoundError(f"no recorded snapshots under {Path(in_dir) / 'gps'}")
    con = duckdb.connect()
    try:
        raw_rows = con.execute(
            "select count(*) from read_parquet(?)", [[str(path) for path in snapshots]]
        ).fetchone()[0]
        table = con.execute(
            """
            select vehicle_id, route_id, trip_id, ts, lat, lng, speed
            from read_parquet(?, filename = true)
            where in_bbox and ts > 0
            qualify row_number() over (
                partition by vehicle_id, ts, lat, lng order by filename, trip_id
            ) = 1
            order by vehicle_id, ts
            """,
            [[str(path) for path in snapshots]],
        ).to_arrow_table()
    finally:
        con.close()
    atomic_write_parquet(output, table, compression="zstd")
    return {"snapshots": len(snapshots), "raw_rows": int(raw_rows), "clean_rows": table.num_rows}


def _direction_summary(coverage: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for row in coverage:
        item = summary.setdefault(row["direction"], {"route_id": row["route_id"], "runs": 0, "passages": 0, "observed": 0})
        item["runs"] += 1
        item["passages"] += row["stops"]
        item["observed"] += row["observed"]
    for item in summary.values():
        item["observed_coverage"] = round(item["observed"] / item["passages"], 4) if item["passages"] else None
    return summary


def build_actuals(in_dir: Path, gtfs_dir: Path, radius_m: float = DEFAULT_RADIUS_M) -> dict[str, Any]:
    in_dir = Path(in_dir)
    day = recorded_day(in_dir)
    gps_path = in_dir / "archive-gps" / f"day={day}.parquet"
    gps = write_archive_gps(in_dir, gps_path)
    output = in_dir / "passages" / f"day={day}.parquet"
    patterns = load_route_patterns(gtfs_dir, ROUTES_717A)
    try:
        coverage, skipped = reconstruct_day(day, gps_path, patterns, output, radius_m=radius_m, overwrite=True)
    except ValueError as exc:
        output.unlink(missing_ok=True)
        coverage, skipped, note = [], None, str(exc)
    else:
        note = None
    summary = {
        "day": day,
        "gps": {**gps, "path": str(gps_path)},
        "passages_path": str(output) if coverage else None,
        "radius_m": radius_m,
        "runs": len(coverage),
        "excluded_runs": skipped,
        "by_direction": _direction_summary(coverage),
        "note": note,
    }
    atomic_write_json(in_dir / "passages" / "actuals-summary.json", summary)
    return summary
