"""Dominant direction-specific stop pattern and timetable starts for a GTFS route."""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import duckdb


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


def parse_trip_start(trip_id: str) -> int | None:
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
                start = parse_trip_start(trip_id)
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
