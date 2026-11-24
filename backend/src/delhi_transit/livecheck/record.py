"""`livecheck run`: record every live poll, scheduled live /journeys responses and a capped Google sample."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from delhi_transit.comparator.client import (
    GoogleBudgetExceededError,
    GoogleClientError,
    GoogleRoutesClient,
)
from delhi_transit.comparator.compare import query_our_api
from delhi_transit.comparator.slots import DEFAULT_OD_PAIRS, DOWN_717A, UP_717A
from delhi_transit.core.config import IST
from delhi_transit.core.io import atomic_write_json
from delhi_transit.core.time import iso_ist
from delhi_transit.data.gtfs_patterns import load_route_patterns
from delhi_transit.live.poller import LivePoller

ROUTES_717A = (2171, 2170)
# Boarding and alighting positions along each 717A pattern, as fractions of its stop list.
STOP_PAIR_FRACTIONS = ((0.1, 0.45), (0.35, 0.7), (0.55, 0.95))
GOOGLE_DEPART_LEAD_S = 300


@dataclass(frozen=True)
class Query:
    query_id: str
    group: str
    label: str
    from_lat: float
    from_lng: float
    to_lat: float
    to_lng: float

    def params(self) -> dict[str, str]:
        return {
            "from": f"{self.from_lat:.5f},{self.from_lng:.5f}",
            "to": f"{self.to_lat:.5f},{self.to_lng:.5f}",
            "mode": "live",
        }


def build_query_set(gtfs_dir: Path) -> list[Query]:
    """The 30 comparator OD pairs plus 717A stop pairs in both directions."""
    queries = [
        Query(od.od_id, od.group, od.label, od.from_lat, od.from_lng, od.to_lat, od.to_lng)
        for od in DEFAULT_OD_PAIRS
    ]
    patterns = load_route_patterns(gtfs_dir, ROUTES_717A)
    for route_id in ROUTES_717A:
        pattern = patterns[route_id]
        last = len(pattern.stops) - 1
        for number, (start, end) in enumerate(STOP_PAIR_FRACTIONS, start=1):
            board = pattern.stops[round(start * last)]
            alight = pattern.stops[round(end * last)]
            queries.append(
                Query(
                    f"s717a_{pattern.direction.lower()}_{number}",
                    f"stops_717a_{pattern.direction.lower()}",
                    f"717A {pattern.direction}: {board.stop_name} -> {alight.stop_name}",
                    board.lat,
                    board.lng,
                    alight.lat,
                    alight.lng,
                )
            )
    return queries


@dataclass
class GoogleBudget:
    """Paid Google calls allowed in this run: below `max_calls` and below the ledger limit."""

    max_calls: int
    ledger_limit: int
    used: int = 0

    def allows(self, ledger_total: int) -> bool:
        return self.used < self.max_calls and ledger_total < self.ledger_limit


def parse_clock(value: str, start: datetime) -> datetime:
    """`HH:MM` (IST, on the start date) or `+Nm` relative to the start."""
    if value.startswith("+"):
        return start + timedelta(minutes=float(value[1:].rstrip("m")))
    hour, minute = (int(part) for part in value.split(":"))
    return start.replace(hour=hour, minute=minute, second=0, microsecond=0)


@dataclass
class RunSettings:
    out_dir: Path
    api_url: str
    until: datetime
    journeys_until: datetime
    slot_s: float
    poll_interval_s: float
    google_slots: list[datetime]
    google_max: int
    google_ledger_limit: int
    gtfs_dir: Path


@dataclass
class _PollStats:
    attempts: int = 0
    ok: int = 0
    repeated: int = 0
    snapshots_saved: int = 0
    errors: Counter = field(default_factory=Counter)


class Recorder:
    def __init__(
        self,
        settings: RunSettings,
        *,
        poller: LivePoller | None = None,
        google_client: GoogleRoutesClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.s = settings
        self.out = Path(settings.out_dir)
        self.gps_dir = self.out / "gps"
        self.journeys_dir = self.out / "journeys"
        self.google_dir = self.out / "google"
        for directory in (self.gps_dir, self.journeys_dir, self.google_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.poller = poller or LivePoller(self.out / "poller")
        self.google = google_client
        self.clock = clock
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self.polls = _PollStats()
        self.slots: list[dict[str, Any]] = []
        self.google_budget = GoogleBudget(settings.google_max, settings.google_ledger_limit)
        self.google_summary: dict[str, Any] = {
            "max_calls": settings.google_max,
            "ledger_limit": settings.google_ledger_limit,
            "slots": [moment.isoformat() for moment in settings.google_slots],
            "calls_made": 0,
            "cache_hits": 0,
            "unavailable": 0,
            "stopped_reason": None,
        }
        self.started_at = self.clock()

    def log(self, message: str) -> None:
        line = f"{iso_ist(self.clock())} {message}"
        with self._lock, (self.out / "run.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Live feed -----------------------------------------------------------------
    def _save_snapshot(self, state: dict[str, Any]) -> bool:
        source = Path(state.get("snapshot_path") or "")
        target = self.gps_dir / source.name
        if not source.name or target.exists() or not source.exists():
            return False
        tmp = target.with_name(f".{target.name}.tmp")
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
        return True

    def poll(self, client: httpx.Client | None = None) -> dict[str, Any]:
        attempted_at = self.clock()
        state = self.poller.poll_once(client)
        ok = state.get("status") == "ok" and state.get("last_error") is None
        saved = self._save_snapshot(state) if ok else False
        record = {
            "attempted_at": round(attempted_at, 3),
            "attempted_at_iso": iso_ist(attempted_at),
            "ok": ok,
            "error": None if ok else (state.get("last_error") or state.get("status")),
            "header_timestamp": state.get("header_timestamp") if ok else None,
            "vehicle_count": state.get("vehicle_count") if ok else None,
            "fresh_vehicle_count": state.get("fresh_vehicle_count") if ok else None,
            "feed_age_seconds_at_poll": state.get("feed_age_seconds_at_poll") if ok else None,
            "repeated_snapshot": bool(state.get("repeated_snapshot")) if ok else None,
            "snapshot_saved": saved,
        }
        self._append_jsonl(self.out / "polls.jsonl", record)
        with self._lock:
            self.polls.attempts += 1
            if ok:
                self.polls.ok += 1
                self.polls.repeated += bool(state.get("repeated_snapshot"))
                self.polls.snapshots_saved += saved
            else:
                self.polls.errors[record["error"]] += 1
        return record

    def poll_loop(self) -> None:
        with httpx.Client(timeout=self.poller.timeout_s) as client:
            while not self.stop.is_set() and self.clock() < self.s.until.timestamp():
                started = self.clock()
                try:
                    self.poll(client)
                except Exception as exc:  # noqa: BLE001 - one bad poll must not end the recording
                    self.log(f"poll failed: {type(exc).__name__}")
                self.stop.wait(max(1.0, self.s.poll_interval_s - (self.clock() - started)))

    # Journeys ------------------------------------------------------------------
    def query_journeys(self, api: httpx.Client, query: Query, slot: int) -> dict[str, Any]:
        requested_at = self.clock()
        record: dict[str, Any] = {
            "slot": slot,
            "query": asdict(query),
            "requested_at": round(requested_at, 3),
            "requested_at_iso": iso_ist(requested_at),
        }
        try:
            response = api.get(f"{self.s.api_url.rstrip('/')}/journeys", params=query.params())
            record["http_status"] = response.status_code
            body = response.json() if response.status_code == 200 else None
            record["error"] = None if body is not None else response.text[:300]
        except (httpx.HTTPError, ValueError) as exc:
            record["http_status"] = None
            body = None
            record["error"] = type(exc).__name__
        received_at = self.clock()
        record["received_at"] = round(received_at, 3)
        record["elapsed_s"] = round(received_at - requested_at, 3)
        record["status"] = (body or {}).get("status", "error")
        record["feed_age_seconds"] = (body or {}).get("feed_age_seconds")
        record["abstention_code"] = ((body or {}).get("abstention") or {}).get("code")
        record["response"] = body
        return record

    def live_status(self, api: httpx.Client) -> dict[str, Any] | None:
        try:
            response = api.get(f"{self.s.api_url.rstrip('/')}/live/status")
            return response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def run_slot(self, api: httpx.Client, queries: list[Query], slot: int) -> dict[str, Any]:
        started = self.clock()
        status = self.live_status(api) or {}
        path = self.journeys_dir / f"slot_{slot:03d}.jsonl"
        statuses: Counter = Counter()
        for query in queries:
            record = self.query_journeys(api, query, slot)
            record["slot_live_status"] = {
                key: status.get(key)
                for key in ("current_feed_age_seconds", "stale_threshold_seconds", "is_fresh", "paused")
            }
            self._append_jsonl(path, record)
            statuses[record["status"]] += 1
        finished = self.clock()
        summary = {
            "slot": slot,
            "started_at": iso_ist(started),
            "finished_at": iso_ist(finished),
            "queries": len(queries),
            "statuses": dict(statuses),
            "feed_age_at_slot_start_s": status.get("current_feed_age_seconds"),
            "file": str(path),
        }
        self.slots.append(summary)
        self.log(f"slot {slot}: {len(queries)} queries {dict(statuses)} in {finished - started:.0f}s")
        return summary

    # Google ----------------------------------------------------------------------
    def run_google(self, api: httpx.Client, slot: int) -> None:
        if self.google is None or self.google_summary["stopped_reason"]:
            return
        corridor = [od for od in DEFAULT_OD_PAIRS if od.group in (UP_717A, DOWN_717A)]
        lead = datetime.fromtimestamp(self.clock() + GOOGLE_DEPART_LEAD_S, IST)
        depart = (lead + timedelta(minutes=1)).replace(second=0, microsecond=0)
        for od in corridor:
            ledger_total = int(self.google.read_call_counter()["total_mission_calls"])
            if not self.google_budget.allows(ledger_total):
                self.google_summary["stopped_reason"] = (
                    f"budget reached: {self.google_budget.used} calls this run, ledger {ledger_total}"
                )
                self.log(f"google stopped: {self.google_summary['stopped_reason']}")
                return
            requested_at = self.clock()
            our = query_our_api(api, self.s.api_url, od, depart)
            record: dict[str, Any] = {
                "slot": slot,
                "od_id": od.od_id,
                "label": od.label,
                "requested_at_iso": iso_ist(requested_at),
                "departure_time": depart.isoformat(),
                "our": our,
            }
            try:
                result = self.google.compute_bus_routes(
                    (od.from_lat, od.from_lng),
                    (od.to_lat, od.to_lng),
                    depart,
                    od_id=od.od_id,
                    slot_label=f"livecheck_slot_{slot:03d}",
                )
            except GoogleBudgetExceededError as exc:
                self.google_summary["stopped_reason"] = f"ledger cap: {exc}"
                self.log("google stopped: ledger cap reached")
                return
            except (GoogleClientError, ValueError) as exc:
                self.google_summary["unavailable"] += 1
                record["google"] = {"unavailable_reason": type(exc).__name__, "error": str(exc)[:300]}
                self._append_jsonl(self.google_dir / "calls.jsonl", record)
                continue
            if result["from_cache"]:
                self.google_summary["cache_hits"] += 1
            else:
                self.google_budget.used += 1
                self.google_summary["calls_made"] += 1
            record["google"] = {
                key: result.get(key)
                for key in ("cache_key", "cache_path", "from_cache", "fetched_at", "http_status", "error", "request")
            }
            record["google"]["parsed"] = result.get("parsed")
            self._append_jsonl(self.google_dir / "calls.jsonl", record)
        self.log(f"google slot {slot}: {self.google_budget.used} calls used this run")

    # Orchestration -------------------------------------------------------------
    def write_summary(self, status: str) -> None:
        with self._lock:
            polls = {
                "attempts": self.polls.attempts,
                "ok": self.polls.ok,
                "failed": self.polls.attempts - self.polls.ok,
                "repeated_snapshots": self.polls.repeated,
                "snapshots_saved": self.polls.snapshots_saved,
                "errors": dict(self.polls.errors),
            }
        ledger = self.google.read_call_counter()["total_mission_calls"] if self.google else None
        atomic_write_json(
            self.out / "run-summary.json",
            {
                "status": status,
                "day": datetime.fromtimestamp(self.started_at, IST).date().isoformat(),
                "started_at": iso_ist(self.started_at),
                "updated_at": iso_ist(self.clock()),
                "until": self.s.until.isoformat(),
                "journeys_until": self.s.journeys_until.isoformat(),
                "api_url": self.s.api_url,
                "poll_interval_s": self.s.poll_interval_s,
                "slot_minutes": self.s.slot_s / 60,
                "polls": polls,
                "slots": self.slots,
                "google": {**self.google_summary, "ledger_total_now": ledger},
            },
        )

    def run(self) -> dict[str, Any]:
        queries = build_query_set(self.s.gtfs_dir)
        if self.google is not None:
            self.google_summary["ledger_before"] = self.google.read_call_counter()["total_mission_calls"]
        self.log(
            f"start: until {self.s.until.isoformat()}, {len(queries)} queries per slot every "
            f"{self.s.slot_s / 60:g} min until {self.s.journeys_until.isoformat()}, "
            f"google max {self.s.google_max}"
        )
        self.write_summary("running")
        poll_thread = threading.Thread(target=self.poll_loop, name="livecheck-poll", daemon=True)
        poll_thread.start()
        pending_google = sorted(self.s.google_slots)
        next_slot = self.clock()
        slot = 0
        try:
            with httpx.Client(timeout=180.0) as api:
                while not self.stop.is_set() and self.clock() < self.s.until.timestamp():
                    now = self.clock()
                    if now >= next_slot and now <= self.s.journeys_until.timestamp():
                        self.run_slot(api, queries, slot)
                        if pending_google and pending_google[0].timestamp() <= self.clock():
                            while pending_google and pending_google[0].timestamp() <= self.clock():
                                pending_google.pop(0)
                            self.run_google(api, slot)
                        slot += 1
                        while next_slot <= self.clock():
                            next_slot += self.s.slot_s
                        self.write_summary("running")
                    self.stop.wait(min(5.0, max(0.5, self.s.until.timestamp() - self.clock())))
        finally:
            self.stop.set()
            poll_thread.join(timeout=60)
            if self.google is not None:
                self.google_summary["ledger_after"] = self.google.read_call_counter()["total_mission_calls"]
            self.write_summary("finished")
            self.log(f"finished: {self.polls.ok}/{self.polls.attempts} polls ok, {len(self.slots)} slots")
        return json.loads((self.out / "run-summary.json").read_text(encoding="utf-8"))
