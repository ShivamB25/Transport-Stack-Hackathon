"""Geometric GPS-to-stop-passage reconstruction for direction-specific GTFS patterns."""
from __future__ import annotations

import argparse
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ps1.config import DATA_DIR

EARTH_RADIUS_M = 6_371_000.0
DEFAULT_RADIUS_M = 60.0
MAX_OBSERVED_SPAN_S = 6 * 3600
IST = timezone(timedelta(hours=5, minutes=30))
SCOPED_DAYS = tuple(
    f"2026-10-{day:02d}" for day in (5, 6, 7, 8, 13, 14, 15, 22, 27, 28)
)


@dataclass(frozen=True)
class PatternStop:
    stop_id: str
    stop_name: str
    lat: float
    lng: float
    sequence: int
    schedule_s: int


@dataclass(frozen=True)
class RoutePattern:
    route_id: int
    direction: str
    shape_id: str
    stops: tuple[PatternStop, ...]
    timetable: dict[int, str]


@dataclass(frozen=True)
class Ping:
    ts: int
    lat: float
    lng: float


@dataclass(frozen=True)
class Anchor:
    stop_index: int
    ts: int
    distance_m: float


@dataclass(frozen=True)
class Passage:
    stop: PatternStop
    source: str
    ts: int
    scheduled_ts: int
    schedule_error_s: int
    distance_m: float | None

    @property
    def observed(self) -> bool:
        return self.source == "observed"


PASSAGE_SCHEMA = pa.schema(
    [
        ("day", pa.string()),
        ("route_id", pa.int32()),
        ("direction", pa.string()),
        ("vehicle_id", pa.string()),
        ("run_id", pa.string()),
        ("stop_sequence", pa.int32()),
        ("stop_id", pa.string()),
        ("stop_name", pa.string()),
        ("stop_lat", pa.float64()),
        ("stop_lng", pa.float64()),
        ("source", pa.string()),
        ("observed", pa.bool_()),
        ("ts", pa.int64()),
        ("scheduled_ts", pa.int64()),
        ("schedule_error_s", pa.int64()),
        ("distance_m", pa.float64()),
    ]
)


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def select_minimum_distance_anchors(
    pings: Sequence[Ping], stops: Sequence[PatternStop], radius_m: float = DEFAULT_RADIUS_M
) -> list[Anchor]:
    """Select each stop's closest ping within the radius, before time filtering."""
    if not pings or not stops:
        return []
    ping_lats = np.radians(np.array([p.lat for p in pings], dtype=np.float64))
    ping_lngs = np.radians(np.array([p.lng for p in pings], dtype=np.float64))
    stop_lats = np.radians(np.array([s.lat for s in stops], dtype=np.float64))
    stop_lngs = np.radians(np.array([s.lng for s in stops], dtype=np.float64))
    dp = stop_lats[:, None] - ping_lats[None, :]
    dl = stop_lngs[:, None] - ping_lngs[None, :]
    a = (
        np.sin(dp / 2) ** 2
        + np.cos(stop_lats[:, None])
        * np.cos(ping_lats[None, :])
        * np.sin(dl / 2) ** 2
    )
    distances = 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.minimum(1.0, a)))
    anchors = []
    for stop_index, stop_distances in enumerate(distances):
        within = np.flatnonzero(stop_distances <= radius_m)
        if not within.size:
            continue
        # Pings are sorted, so argmin chooses the earliest ping on an exact distance tie.
        ping_index = int(within[np.argmin(stop_distances[within])])
        anchors.append(
            Anchor(stop_index, pings[ping_index].ts, float(stop_distances[ping_index]))
        )
    return anchors


def drop_time_order_breaking_anchors(anchors: Sequence[Anchor]) -> list[Anchor]:
    """Keep a maximum-cardinality chain nondecreasing in stop order and time."""
    if not anchors:
        return []
    ordered = sorted(anchors, key=lambda a: a.stop_index)
    lengths = [1] * len(ordered)
    distances = [a.distance_m for a in ordered]
    previous = [-1] * len(ordered)
    for i, anchor in enumerate(ordered):
        for j in range(i):
            earlier = ordered[j]
            if earlier.stop_index >= anchor.stop_index or earlier.ts > anchor.ts:
                continue
            candidate_length = lengths[j] + 1
            candidate_distance = distances[j] + anchor.distance_m
            if candidate_length > lengths[i] or (
                candidate_length == lengths[i]
                and candidate_distance < distances[i] - 1e-9
            ):
                lengths[i] = candidate_length
                distances[i] = candidate_distance
                previous[i] = j
    best = min(
        range(len(ordered)),
        key=lambda i: (-lengths[i], distances[i], ordered[i].stop_index),
    )
    kept = []
    while best >= 0:
        kept.append(ordered[best])
        best = previous[best]
    return list(reversed(kept))


def interpolate_stop_times(
    stops: Sequence[PatternStop], anchors: Sequence[Anchor]
) -> list[int]:
    """Interpolate all gaps by elapsed time per scheduled second between anchors."""
    if len(anchors) < 2:
        raise ValueError("at least two monotonic anchors are required")
    by_stop = {anchor.stop_index: anchor for anchor in anchors}
    anchor_indices = sorted(by_stop)
    result = [0] * len(stops)

    def pace(left: Anchor, right: Anchor) -> float:
        scheduled_delta = stops[right.stop_index].schedule_s - stops[left.stop_index].schedule_s
        if scheduled_delta <= 0:
            return 1.0
        return max(0.0, (right.ts - left.ts) / scheduled_delta)

    first = anchors[0]
    for index in range(first.stop_index):
        scheduled_delta = stops[first.stop_index].schedule_s - stops[index].schedule_s
        result[index] = first.ts - scheduled_delta

    for left_index, right_index in pairwise(anchor_indices):
        left, right = by_stop[left_index], by_stop[right_index]
        result[left_index] = left.ts
        gap_pace = pace(left, right)
        for index in range(left_index + 1, right_index):
            scheduled_delta = stops[index].schedule_s - stops[left_index].schedule_s
            result[index] = round(left.ts + scheduled_delta * gap_pace)
    result[anchor_indices[-1]] = anchors[-1].ts

    last = anchors[-1]
    for index in range(last.stop_index + 1, len(stops)):
        scheduled_delta = stops[index].schedule_s - stops[last.stop_index].schedule_s
        result[index] = last.ts + scheduled_delta
    return result


def build_passages(
    stops: Sequence[PatternStop], anchors: Sequence[Anchor], schedule_start_ts: int
) -> list[Passage]:
    """Combine observed anchors and interpolated gaps, adding timetable errors."""
    times = interpolate_stop_times(stops, anchors)
    observed = {anchor.stop_index: anchor for anchor in anchors}
    passages = []
    for index, (stop, ts) in enumerate(zip(stops, times, strict=True)):
        scheduled_ts = schedule_start_ts + stop.schedule_s
        anchor = observed.get(index)
        passages.append(
            Passage(
                stop=stop,
                source="observed" if anchor else "interpolated",
                ts=ts,
                scheduled_ts=scheduled_ts,
                schedule_error_s=ts - scheduled_ts,
                distance_m=anchor.distance_m if anchor else None,
            )
        )
    return passages


def _parse_trip_start(trip_id: str) -> int | None:
    match = re.search(r"(?:^|_)(\d{2})_(\d{2})(?:_\d+)?$", trip_id)
    if not match:
        return None
    hour, minute = (int(value) for value in match.groups())
    if minute >= 60:
        return None
    return hour * 3600 + minute * 60


def _parse_gtfs_time(value: str) -> int:
    hour, minute, second = (int(part) for part in value.split(":"))
    return hour * 3600 + minute * 60 + second


def load_route_patterns(gtfs_dir: Path, route_ids: Iterable[int]) -> dict[int, RoutePattern]:
    """Load the dominant FULL shape for each numeric route/direction."""
    gtfs_dir = Path(gtfs_dir)
    route_ids = sorted(set(route_ids))
    placeholders = ",".join("?" for _ in route_ids)
    con = duckdb.connect()
    try:
        routes = con.execute(
            f"select route_id, route_short_name from read_csv(?, header=true, all_varchar=true) "
            f"where route_id in ({placeholders})",
            [str(gtfs_dir / "routes.txt"), *map(str, route_ids)],
        ).fetchall()
        short_names = {int(route_id): name for route_id, name in routes}
        trips = con.execute(
            f"select trip_id, route_id, shape_id from read_csv(?, header=true, all_varchar=true) "
            f"where route_id in ({placeholders})",
            [str(gtfs_dir / "trips.txt"), *map(str, route_ids)],
        ).fetchall()
        patterns = {}
        for route_id in route_ids:
            route_trips = [row for row in trips if int(row[1]) == route_id]
            if not route_trips:
                raise ValueError(f"no GTFS trips for route {route_id}")
            full_counts = Counter(shape for trip, _, shape in route_trips if "_FULL_" in trip)
            counts = full_counts or Counter(shape for _, _, shape in route_trips)
            shape_id = min(counts, key=lambda shape: (-counts[shape], shape))
            shape_trips = sorted(row for row in route_trips if row[2] == shape_id)
            template_trip = shape_trips[0][0]
            stop_rows = con.execute(
                """
                select st.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
                       st.stop_sequence, st.arrival_time
                from read_csv(?, header=true, all_varchar=true) st
                join read_csv(?, header=true, all_varchar=true) s using (stop_id)
                where st.trip_id = ?
                order by cast(st.stop_sequence as integer)
                """,
                [str(gtfs_dir / "stop_times.txt"), str(gtfs_dir / "stops.txt"), template_trip],
            ).fetchall()
            if len(stop_rows) < 2:
                raise ValueError(f"route {route_id} pattern has fewer than two stops")
            first_schedule = _parse_gtfs_time(stop_rows[0][5])
            stops = tuple(
                PatternStop(
                    stop_id=stop_id,
                    stop_name=name,
                    lat=float(lat),
                    lng=float(lng),
                    sequence=int(sequence),
                    schedule_s=_parse_gtfs_time(arrival) - first_schedule,
                )
                for stop_id, name, lat, lng, sequence, arrival in stop_rows
            )
            timetable = {}
            for trip_id, _, _ in shape_trips:
                start = _parse_trip_start(trip_id)
                if start is not None:
                    timetable[start] = trip_id
            short_name = short_names[route_id]
            direction = "UP" if short_name.upper().endswith("UP") else "DOWN"
            patterns[route_id] = RoutePattern(
                route_id, direction, shape_id, stops, timetable
            )
        return patterns
    finally:
        con.close()


def load_gps_runs(
    parquet_path: Path, route_ids: Iterable[int]
) -> dict[tuple[int, str, str], list[Ping]]:
    """Read route-filtered pings, grouped by vehicle, numeric route and GPS run label."""
    route_ids = sorted(set(route_ids))
    placeholders = ",".join("?" for _ in route_ids)
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            select route_id, vehicle_id, trip_id, ts, lat, lng
            from read_parquet(?)
            where route_id in ({placeholders}) and trip_id is not null
            order by route_id, vehicle_id, trip_id, ts
            """,
            [str(parquet_path), *route_ids],
        ).fetchall()
    finally:
        con.close()
    runs = defaultdict(list)
    for route_id, vehicle_id, run_id, ts, lat, lng in rows:
        runs[(route_id, vehicle_id, run_id)].append(Ping(int(ts), lat, lng))
    return dict(runs)


def schedule_start_for_run(
    pattern: RoutePattern, run_id: str, pings: Sequence[Ping], day: str
) -> int:
    """Infer the timetable departure from run start time, never by joining trip IDs."""
    day_date = date.fromisoformat(day)
    midnight = int(datetime.combine(day_date, datetime.min.time(), IST).timestamp())
    hinted_start = _parse_trip_start(run_id)
    if hinted_start in pattern.timetable:
        return midnight + hinted_start
    if not pattern.timetable:
        if hinted_start is not None:
            return midnight + hinted_start
        raise ValueError(f"no timetable start available for {run_id}")
    local_first_ts = pings[0].ts + int(IST.utcoffset(None).total_seconds())
    seconds_into_day = local_first_ts % 86_400
    nearest = min(pattern.timetable, key=lambda start: abs(start - seconds_into_day))
    return midnight + nearest


def reconstruct_day(
    day: str,
    gps_path: Path,
    patterns: dict[int, RoutePattern],
    output_path: Path,
    radius_m: float = DEFAULT_RADIUS_M,
    overwrite: bool = False,
) -> tuple[list[dict], int]:
    """Reconstruct all eligible runs for one day and atomically write Parquet."""
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"{output_path} exists; pass --overwrite to replace it")
    runs = load_gps_runs(gps_path, patterns)
    records = []
    coverage = []
    skipped = 0
    for (route_id, vehicle_id, run_id), pings in sorted(runs.items()):
        pattern = patterns[route_id]
        candidates = select_minimum_distance_anchors(pings, pattern.stops, radius_m)
        anchors = drop_time_order_breaking_anchors(candidates)
        if len(anchors) < 2 or anchors[-1].ts - anchors[0].ts > MAX_OBSERVED_SPAN_S:
            skipped += 1
            continue
        schedule_start = schedule_start_for_run(pattern, run_id, pings, day)
        passages = build_passages(pattern.stops, anchors, schedule_start)
        observed_count = sum(passage.observed for passage in passages)
        coverage.append(
            {
                "day": day,
                "route_id": route_id,
                "direction": pattern.direction,
                "vehicle_id": vehicle_id,
                "run_id": run_id,
                "stops": len(passages),
                "observed": observed_count,
                "interpolated": len(passages) - observed_count,
                "coverage_pct": 100 * observed_count / len(passages),
                "first_ts": passages[0].ts,
                "last_ts": passages[-1].ts,
            }
        )
        for passage in passages:
            records.append(
                {
                    "day": day,
                    "route_id": route_id,
                    "direction": pattern.direction,
                    "vehicle_id": vehicle_id,
                    "run_id": run_id,
                    "stop_sequence": passage.stop.sequence,
                    "stop_id": passage.stop.stop_id,
                    "stop_name": passage.stop.stop_name,
                    "stop_lat": passage.stop.lat,
                    "stop_lng": passage.stop.lng,
                    "source": passage.source,
                    "observed": passage.observed,
                    "ts": passage.ts,
                    "scheduled_ts": passage.scheduled_ts,
                    "schedule_error_s": passage.schedule_error_s,
                    "distance_m": passage.distance_m,
                }
            )
    if not records:
        raise ValueError(f"no coherent 717A runs reconstructed for {day}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".tmp.parquet")
    try:
        pq.write_table(
            pa.Table.from_pylist(records, schema=PASSAGE_SCHEMA),
            temporary,
            compression="zstd",
        )
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return coverage, skipped


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
            "interpolated; endpoints use schedule-rate extrapolation."
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
    for day, direction in sorted(groups):
        if day != "ALL":
            continue
        trips, stops, observed, interpolated = groups[(day, direction)]
        lines.append(
            f"| {day} | {direction} | {trips} | {stops} | {observed} | "
            f"{interpolated} | {100 * observed / stops:.2f}% |"
        )
    for day, direction in sorted(groups):
        if day == "ALL":
            continue
        trips, stops, observed, interpolated = groups[(day, direction)]
        lines.append(
            f"| {day} | {direction} | {trips} | {stops} | {observed} | "
            f"{interpolated} | {100 * observed / stops:.2f}% |"
        )
    lines.extend(["", "## Outputs and excluded candidate runs", ""])
    for day in sorted(outputs):
        lines.append(
            f"- {day}: `{outputs[day]}`; excluded {skipped_by_day[day]} runs "
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


def parse_days(values: Sequence[str] | None) -> list[str]:
    if not values:
        return list(SCOPED_DAYS)
    days = [value for group in values for value in group.split(",") if value]
    return [date.fromisoformat(day).isoformat() for day in days]


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", action="append", help="ISO day or comma-separated days")
    parser.add_argument("--routes", default="2170,2171")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--gtfs-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--report", type=Path, default=Path("reports/coverage-717a.md"))
    parser.add_argument("--radius-m", type=float, default=DEFAULT_RADIUS_M)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    days = parse_days(args.day)
    route_ids = {int(value) for value in args.routes.split(",")}
    if not route_ids <= {2170, 2171}:
        parser.error("this scoped reconstruction supports routes 2170 and 2171 only")
    data_dir = args.data_dir
    gtfs_dir = args.gtfs_dir or data_dir / "gtfs"
    output_dir = args.output_dir or data_dir / "passages"
    patterns = load_route_patterns(gtfs_dir, route_ids)
    all_coverage = []
    skipped_by_day = {}
    outputs = {}
    for day in days:
        output = output_dir / f"day={day}.parquet"
        coverage, skipped = reconstruct_day(
            day,
            data_dir / "gps" / f"day={day}.parquet",
            patterns,
            output,
            radius_m=args.radius_m,
            overwrite=args.overwrite,
        )
        all_coverage.extend(coverage)
        skipped_by_day[day] = skipped
        outputs[day] = output
        observed = sum(row["observed"] for row in coverage)
        stops = sum(row["stops"] for row in coverage)
        print(
            f"{day}: {len(coverage)} runs, {stops} passages, "
            f"{observed / stops:.2%} observed, {skipped} excluded; {output}"
        )
    write_coverage_report(args.report, all_coverage, skipped_by_day, outputs, args.radius_m)
    print(f"Coverage report: {args.report}")


if __name__ == "__main__":
    main()
