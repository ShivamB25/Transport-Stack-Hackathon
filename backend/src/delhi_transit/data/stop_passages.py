"""Geometric GPS-to-stop-passage reconstruction for direction-specific GTFS patterns."""
from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa

from delhi_transit.core.config import DATA_DIR, IST
from delhi_transit.core.geo import EARTH_RADIUS_M
from delhi_transit.core.io import atomic_write_parquet
from delhi_transit.data.coverage_report import (
    CANONICAL_ROUTES,
    SCOPED_DAYS,
    resolve_report_path,
    write_coverage_report,
)
from delhi_transit.data.gtfs_patterns import (
    PatternStop,
    RoutePattern,
    load_route_patterns,
    parse_trip_start,
)

DEFAULT_RADIUS_M = 60.0
MAX_OBSERVED_SPAN_S = 6 * 3600


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
    available_at: int
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
        ("available_at", pa.int64()),
        ("scheduled_ts", pa.int64()),
        ("schedule_error_s", pa.int64()),
        ("distance_m", pa.float64()),
    ]
)


def _stop_ping_distances(pings: Sequence[Ping], stops: Sequence[PatternStop]) -> np.ndarray:
    """Return a (stops x pings) great-circle distance matrix in metres."""
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
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.minimum(1.0, a)))


def select_minimum_distance_anchors(
    pings: Sequence[Ping], stops: Sequence[PatternStop], radius_m: float = DEFAULT_RADIUS_M
) -> list[Anchor]:
    """Select each stop's closest ping within the radius, before time filtering."""
    if not pings or not stops:
        return []
    distances = _stop_ping_distances(pings, stops)
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


def iter_prefix_anchor_sets(
    pings: Sequence[Ping], stops: Sequence[PatternStop], radius_m: float = DEFAULT_RADIUS_M
) -> Iterator[tuple[int, list[Anchor]]]:
    """Yield (last_ping_index, anchors) for each raw-GPS prefix whose closest-ping set changes.

    Pings must be sorted by ts. A prefix ends on the last ping sharing a timestamp, so it is
    exactly the GPS visible to a query strictly after that timestamp. The anchors match
    `select_minimum_distance_anchors` applied to that prefix (earliest ping on exact ties).
    """
    if not pings or not stops:
        return
    distances = _stop_ping_distances(pings, stops)
    distances = np.where(distances <= radius_m, distances, np.inf)
    best = np.minimum.accumulate(distances, axis=1)
    before = np.concatenate([np.full((len(stops), 1), np.inf), best[:, :-1]], axis=1)
    improved = distances < before
    columns = np.arange(len(pings))
    best_index = np.maximum.accumulate(np.where(improved, columns[None, :], -1), axis=1)
    ts = np.array([p.ts for p in pings], dtype=np.int64)
    changed = np.flatnonzero(improved.any(axis=0))
    cutoffs = sorted({int(np.searchsorted(ts, ts[j], side="right")) - 1 for j in changed})
    for cutoff in cutoffs:
        anchors = []
        for stop_index, ping_index in enumerate(best_index[:, cutoff]):
            if ping_index >= 0:
                anchors.append(
                    Anchor(stop_index, int(ts[ping_index]), float(distances[stop_index, ping_index]))
                )
        yield cutoff, anchors


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


def interpolate_stop_times_and_availability(
    stops: Sequence[PatternStop], anchors: Sequence[Anchor]
) -> tuple[list[int], list[int]]:
    """Interpolate stop times and compute each passage's GPS availability epoch.

    Availability semantics (the later of the per-passage rule and the run release):
    - observed stop: its own anchor ping ts
    - interpolated stop between anchors: ts of the later anchor that enabled interpolation
    - extrapolated leading/trailing endpoints: ts of the anchor used for extrapolation
    - run release: a run qualifies only once its second anchor exists, so no passage
      is available before the second surviving anchor's ts
    """
    if len(anchors) < 2:
        raise ValueError("at least two monotonic anchors are required")
    by_stop = {anchor.stop_index: anchor for anchor in anchors}
    anchor_indices = sorted(by_stop)
    release_ts = by_stop[anchor_indices[1]].ts
    result = [0] * len(stops)
    available_at = [0] * len(stops)

    def pace(left: Anchor, right: Anchor) -> float:
        scheduled_delta = stops[right.stop_index].schedule_s - stops[left.stop_index].schedule_s
        if scheduled_delta <= 0:
            return 1.0
        return max(0.0, (right.ts - left.ts) / scheduled_delta)

    first = by_stop[anchor_indices[0]]
    for index in range(first.stop_index):
        scheduled_delta = stops[first.stop_index].schedule_s - stops[index].schedule_s
        result[index] = first.ts - scheduled_delta
        available_at[index] = first.ts

    for left_index, right_index in pairwise(anchor_indices):
        left, right = by_stop[left_index], by_stop[right_index]
        result[left_index] = left.ts
        available_at[left_index] = left.ts
        gap_pace = pace(left, right)
        for index in range(left_index + 1, right_index):
            scheduled_delta = stops[index].schedule_s - stops[left_index].schedule_s
            result[index] = round(left.ts + scheduled_delta * gap_pace)
            available_at[index] = right.ts

    last = by_stop[anchor_indices[-1]]
    result[last.stop_index] = last.ts
    available_at[last.stop_index] = last.ts
    for index in range(last.stop_index + 1, len(stops)):
        scheduled_delta = stops[index].schedule_s - stops[last.stop_index].schedule_s
        result[index] = last.ts + scheduled_delta
        available_at[index] = last.ts
    return result, [max(value, release_ts) for value in available_at]


def build_passages(
    stops: Sequence[PatternStop], anchors: Sequence[Anchor], schedule_start_ts: int
) -> list[Passage]:
    """Combine observed anchors and interpolated gaps, adding availability and timetable errors."""
    times, availabilities = interpolate_stop_times_and_availability(stops, anchors)
    observed = {anchor.stop_index: anchor for anchor in anchors}
    passages = []
    for index, (stop, ts, available_at) in enumerate(
        zip(stops, times, availabilities, strict=True)
    ):
        scheduled_ts = schedule_start_ts + stop.schedule_s
        anchor = observed.get(index)
        passages.append(
            Passage(
                stop=stop,
                source="observed" if anchor else "interpolated",
                ts=ts,
                available_at=available_at,
                scheduled_ts=scheduled_ts,
                schedule_error_s=ts - scheduled_ts,
                distance_m=anchor.distance_m if anchor else None,
            )
        )
    return passages


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
    pattern: RoutePattern,
    run_id: str,
    pings: Sequence[Ping],
    day: str,
    anchors: Sequence[Anchor] | None = None,
) -> int:
    """Infer timetable departure across adjacent IST service dates, never joining trip IDs."""
    day_date = date.fromisoformat(day)
    if anchors:
        first_anchor = min(anchors, key=lambda anchor: anchor.stop_index)
        ref_start = first_anchor.ts - pattern.stops[first_anchor.stop_index].schedule_s
    elif pings:
        ref_start = pings[0].ts
    else:
        raise ValueError(f"no pings or anchors available for {run_id}")

    midnights = [
        int(datetime.combine(day_date + timedelta(days=offset), datetime.min.time(), IST).timestamp())
        for offset in (-1, 0, 1)
    ]
    hinted_start = parse_trip_start(run_id)
    if hinted_start in pattern.timetable:
        candidates = [midnight + hinted_start for midnight in midnights]
    elif pattern.timetable:
        candidates = [
            midnight + start
            for midnight in midnights
            for start in pattern.timetable
        ]
    elif hinted_start is not None:
        candidates = [midnight + hinted_start for midnight in midnights]
    else:
        raise ValueError(f"no timetable start available for {run_id}")
    return min(candidates, key=lambda candidate: (abs(candidate - ref_start), candidate))


def _passage_key(passage: Passage) -> tuple[int, int, str]:
    return passage.ts, passage.scheduled_ts, passage.source


def _reconstruct_from_anchors(
    pattern: RoutePattern,
    run_id: str,
    pings: Sequence[Ping],
    day: str,
    candidates: Sequence[Anchor],
) -> list[Passage] | None:
    """Apply the anchor-chain, qualification and interpolation rules; None if excluded."""
    anchors = drop_time_order_breaking_anchors(candidates)
    if len(anchors) < 2 or anchors[-1].ts - anchors[0].ts > MAX_OBSERVED_SPAN_S:
        return None
    schedule_start = schedule_start_for_run(pattern, run_id, pings, day, anchors=anchors)
    return build_passages(pattern.stops, anchors, schedule_start)


def prefix_stable_availability(
    pattern: RoutePattern,
    run_id: str,
    pings: Sequence[Ping],
    day: str,
    passages: Sequence[Passage],
    radius_m: float = DEFAULT_RADIUS_M,
) -> list[int]:
    """Return, per passage, the earliest ping ts after which every raw-GPS prefix reproduces it.

    A later anchor can revise an earlier reconstruction (for example the maximum-cardinality
    chain dropping a previously observed stop). Until the prefix reconstruction agrees with the
    published passage and keeps agreeing, the published value depends on future GPS.
    """
    prefixes = list(iter_prefix_anchor_sets(pings, pattern.stops, radius_m))
    last_mismatch = [-1] * len(passages)
    for position, (cutoff, candidates) in enumerate(prefixes):
        prefix = _reconstruct_from_anchors(pattern, run_id, pings[: cutoff + 1], day, candidates)
        for index, passage in enumerate(passages):
            if prefix is None or _passage_key(prefix[index]) != _passage_key(passage):
                last_mismatch[index] = position
    if not prefixes or max(last_mismatch) == len(prefixes) - 1:
        raise RuntimeError(f"full GPS prefix does not reproduce run {run_id}")
    return [pings[prefixes[position + 1][0]].ts for position in last_mismatch]


def reconstruct_run(
    pattern: RoutePattern,
    run_id: str,
    pings: Sequence[Ping],
    day: str,
    radius_m: float = DEFAULT_RADIUS_M,
) -> list[Passage] | None:
    """Reconstruct one GPS run (pings sorted by ts) with as-of availability; None if excluded."""
    candidates = select_minimum_distance_anchors(pings, pattern.stops, radius_m)
    passages = _reconstruct_from_anchors(pattern, run_id, pings, day, candidates)
    if passages is None:
        return None
    stable = prefix_stable_availability(pattern, run_id, pings, day, passages, radius_m)
    return [
        replace(passage, available_at=max(passage.available_at, stable_at))
        for passage, stable_at in zip(passages, stable, strict=True)
    ]


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
        passages = reconstruct_run(pattern, run_id, pings, day, radius_m)
        if passages is None:
            skipped += 1
            continue
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
                    "available_at": passage.available_at,
                    "scheduled_ts": passage.scheduled_ts,
                    "schedule_error_s": passage.schedule_error_s,
                    "distance_m": passage.distance_m,
                }
            )
    if not records:
        raise ValueError(f"no coherent 717A runs reconstructed for {day}")
    atomic_write_parquet(
        output_path,
        pa.Table.from_pylist(records, schema=PASSAGE_SCHEMA),
        compression="zstd",
    )
    return coverage, skipped


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
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help=(
            "coverage report output path (defaults to reports/coverage-717a.md only for "
            "full canonical runs; scoped runs default under --output-dir)"
        ),
    )
    parser.add_argument("--radius-m", type=float, default=DEFAULT_RADIUS_M)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    days = parse_days(args.day)
    route_ids = {int(value) for value in args.routes.split(",")}
    if not route_ids <= CANONICAL_ROUTES:
        parser.error("this scoped reconstruction supports routes 2170 and 2171 only")
    data_dir = args.data_dir
    gtfs_dir = args.gtfs_dir or data_dir / "gtfs"
    output_dir = args.output_dir or data_dir / "passages"
    try:
        report_path = resolve_report_path(args.report, days, route_ids, output_dir)
    except ValueError as exc:
        parser.error(str(exc))
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
    write_coverage_report(report_path, all_coverage, skipped_by_day, outputs, args.radius_m)
    print(f"Coverage report: {report_path}")


if __name__ == "__main__":
    main()
