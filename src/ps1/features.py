"""Stretch error tables and strictly time-ordered replay features."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

import duckdb

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class PassageRow:
    day: str
    route_id: int
    direction: str
    vehicle_id: str
    run_id: str
    stop_sequence: int
    stop_id: str
    observed: bool
    ts: int
    scheduled_ts: int

    @property
    def run_key(self) -> tuple[str, int, str, str]:
        return self.day, self.route_id, self.vehicle_id, self.run_id


@dataclass(frozen=True)
class StretchError:
    day: str
    route_id: int
    direction: str
    vehicle_id: str
    run_id: str
    from_sequence: int
    to_sequence: int
    from_stop_id: str
    to_stop_id: str
    query_ts: int
    to_ts: int
    scheduled_duration_s: int
    actual_duration_s: int
    error_s: int
    from_observed: bool
    to_observed: bool

    @property
    def run_key(self) -> tuple[str, int, str, str]:
        return self.day, self.route_id, self.vehicle_id, self.run_id

    @property
    def stretch_key(self) -> tuple[int, str, str, str]:
        return self.route_id, self.direction, self.from_stop_id, self.to_stop_id

    @property
    def query_hour(self) -> int:
        return datetime.fromtimestamp(self.query_ts, IST).hour

    @property
    def target_observed(self) -> bool:
        return self.from_observed and self.to_observed


@dataclass(frozen=True)
class ReplayQuery:
    stretch: StretchError
    previous_error_s: int
    previous_vehicle_id: str
    previous_run_id: str


def load_passages(path: Path) -> list[PassageRow]:
    """Load only fields needed for stretch errors from one passage Parquet file."""
    path = Path(path)
    con = duckdb.connect()
    try:
        rows = con.execute(
            """
            select day, route_id, direction, vehicle_id, run_id, stop_sequence,
                   stop_id, observed, ts, scheduled_ts
            from read_parquet(?)
            order by day, route_id, vehicle_id, run_id, stop_sequence
            """,
            [str(path)],
        ).fetchall()
    finally:
        con.close()
    return [PassageRow(*row) for row in rows]


def build_stretch_errors(passages: Iterable[PassageRow]) -> list[StretchError]:
    """Convert adjacent stop passages into run-level scheduled travel errors."""
    runs: dict[tuple[str, int, str, str], list[PassageRow]] = defaultdict(list)
    for passage in passages:
        runs[passage.run_key].append(passage)

    stretches = []
    for run_passages in runs.values():
        ordered = sorted(run_passages, key=lambda row: row.stop_sequence)
        for upstream, downstream in pairwise(ordered):
            scheduled_duration = downstream.scheduled_ts - upstream.scheduled_ts
            actual_duration = downstream.ts - upstream.ts
            if scheduled_duration < 0 or actual_duration < 0:
                continue
            stretches.append(
                StretchError(
                    day=upstream.day,
                    route_id=upstream.route_id,
                    direction=upstream.direction,
                    vehicle_id=upstream.vehicle_id,
                    run_id=upstream.run_id,
                    from_sequence=upstream.stop_sequence,
                    to_sequence=downstream.stop_sequence,
                    from_stop_id=upstream.stop_id,
                    to_stop_id=downstream.stop_id,
                    query_ts=upstream.ts,
                    to_ts=downstream.ts,
                    scheduled_duration_s=scheduled_duration,
                    actual_duration_s=actual_duration,
                    error_s=actual_duration - scheduled_duration,
                    from_observed=upstream.observed,
                    to_observed=downstream.observed,
                )
            )
    return sorted(
        stretches,
        key=lambda row: (
            row.day,
            row.route_id,
            row.query_ts,
            row.vehicle_id,
            row.run_id,
            row.from_sequence,
        ),
    )


def build_replay_queries(stretches: Iterable[StretchError]) -> list[ReplayQuery]:
    """Attach the latest fully visible previous run error on the same stretch."""
    groups: dict[tuple[str, int, str, str, str], list[StretchError]] = defaultdict(list)
    for stretch in stretches:
        groups[(stretch.day, *stretch.stretch_key)].append(stretch)

    queries = []
    for group in groups.values():
        ordered = sorted(
            group,
            key=lambda row: (row.query_ts, row.vehicle_id, row.run_id),
        )
        for current in ordered:
            visible = (
                candidate
                for candidate in ordered
                if candidate.run_key != current.run_key
                and candidate.query_ts < current.query_ts
                and candidate.to_ts < current.query_ts
            )
            previous = max(
                visible,
                key=lambda row: (row.query_ts, row.vehicle_id, row.run_id),
                default=None,
            )
            if previous is not None:
                queries.append(
                    ReplayQuery(
                        stretch=current,
                        previous_error_s=previous.error_s,
                        previous_vehicle_id=previous.vehicle_id,
                        previous_run_id=previous.run_id,
                    )
                )
    return sorted(
        queries,
        key=lambda query: (
            query.stretch.day,
            query.stretch.route_id,
            query.stretch.query_ts,
            query.stretch.vehicle_id,
            query.stretch.run_id,
            query.stretch.from_sequence,
        ),
    )
