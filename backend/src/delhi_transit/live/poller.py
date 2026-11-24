"""GTFS-RT VehiclePositions poller, snapshot persistence, and feed-age state."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

from delhi_transit.core.config import (
    BBOX_LAT_MAX,
    BBOX_LAT_MIN,
    BBOX_LNG_MAX,
    BBOX_LNG_MIN,
    DATA_DIR,
    LIVE_DIR,
    LIVE_FEED_URL,
    LIVE_POLL_INTERVAL_S,
    LIVE_SNAPSHOT_KEEP,
    LIVE_STALE_THRESHOLD_S,
    load_env_key,
)
from delhi_transit.core.io import atomic_write_json, atomic_write_parquet
from delhi_transit.core.time import iso_ist
from delhi_transit.live.freshness import (
    effective_entity_max_age_s,
    is_fresh,
    live_feed_age_seconds,
    route_summary,
)

SNAPSHOT_SCHEMA = pa.schema(
    [
        ("vehicle_id", pa.string()),
        ("route_id", pa.int64()),
        ("trip_id", pa.string()),
        ("ts", pa.int64()),
        ("lat", pa.float64()),
        ("lng", pa.float64()),
        ("speed", pa.float64()),
        ("start_time", pa.string()),
        ("start_date", pa.string()),
        ("in_bbox", pa.bool_()),
        ("fresh", pa.bool_()),
    ]
)


class LiveFeedError(RuntimeError):
    """A fetch or decode failure carrying the fixed public category that may be persisted."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


_ERROR_CATEGORIES = {
    "auth_not_configured",
    "empty_upstream_payload",
    "upstream_non_protobuf_payload",
    "protobuf_decode_error",
    "missing_feed_timestamps",
    "upstream_http_error",
    "transport_timeout",
    "transport_error",
    "live_feed_error",
    "poll_error",
}


def sanitize_poll_error(error: BaseException | str) -> str:
    """Return a fixed public error category, never raw exception text."""
    if isinstance(error, httpx.TimeoutException):
        return "transport_timeout"
    if isinstance(error, httpx.HTTPError):
        return "transport_error"
    if isinstance(error, LiveFeedError):
        return error.category
    if isinstance(error, str) and (
        error in _ERROR_CATEGORIES or re.fullmatch(r"upstream_http_[0-9]{3}", error)
    ):
        return error
    return "poll_error"


@dataclass(frozen=True)
class DecodedFeed:
    header_timestamp: int
    polled_at: float
    records: list[dict[str, Any]]
    route_stats: dict[str, dict[str, Any]]
    vehicle_count: int
    fresh_vehicle_count: int
    entity_max_age_s: float
    in_bbox_count: int
    route_count: int


def _live_dir(data_dir: Path) -> Path:
    return Path(data_dir) / LIVE_DIR


def _pause_path(data_dir: Path) -> Path:
    return _live_dir(data_dir) / "poller.paused"


def _write_state_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload, sort_keys=True)


def _append_log(data_dir: Path, entry: dict[str, Any]) -> None:
    log_path = _live_dir(data_dir) / "poller.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = {k: v for k, v in entry.items() if "key" not in k.lower()}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sanitized, sort_keys=True) + "\n")


def is_feed_paused(data_dir: Path) -> bool:
    return _pause_path(data_dir).exists()


def pause_feed(data_dir: Path, reason: str = "manual_pause") -> dict[str, Any]:
    now = time.time()
    payload = {"paused": True, "paused_at": now, "paused_at_iso": iso_ist(now), "reason": reason}
    _write_state_json(_pause_path(data_dir), payload)
    _append_log(data_dir, {"event": "paused", "ts": now, "ts_iso": iso_ist(now), "reason": reason})
    return payload


def resume_feed(data_dir: Path) -> dict[str, Any]:
    now = time.time()
    _pause_path(data_dir).unlink(missing_ok=True)
    _append_log(data_dir, {"event": "resumed", "ts": now, "ts_iso": iso_ist(now)})
    return {"paused": False, "resumed_at": now, "resumed_at_iso": iso_ist(now)}


def decode_vehicle_positions(
    payload: bytes,
    *,
    polled_at: float | None = None,
    entity_max_age_s: float | None = None,
) -> DecodedFeed:
    """Decode a GTFS-RT VehiclePositions protobuf payload into records and route stats."""
    if not payload:
        raise LiveFeedError("empty_upstream_payload", "empty VehiclePositions response payload")
    prefix = payload.lstrip()[:32].lower()
    if prefix.startswith((b"<!doctype", b"<html", b"{\"error")):
        raise LiveFeedError(
            "upstream_non_protobuf_payload",
            "upstream returned non-protobuf HTML/JSON payload instead of VehiclePositions.pb",
        )

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(payload)
    except DecodeError as exc:
        raise LiveFeedError("protobuf_decode_error", f"failed to decode GTFS-RT protobuf: {exc}") from exc

    now = time.time() if polled_at is None else float(polled_at)
    max_entity_age = (
        effective_entity_max_age_s() if entity_max_age_s is None else float(entity_max_age_s)
    )
    header_ts = int(feed.header.timestamp) if feed.HasField("header") and feed.header.timestamp else 0

    records: list[dict[str, Any]] = []
    max_vehicle_ts = 0
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle
        vid = ""
        if vehicle.HasField("vehicle"):
            vid = vehicle.vehicle.id or vehicle.vehicle.label or ""
        if not vid:
            vid = entity.id or ""

        route_id: int | None = None
        trip_id: str | None = None
        start_time: str | None = None
        start_date: str | None = None
        if vehicle.HasField("trip"):
            raw_route = (vehicle.trip.route_id or "").strip()
            if raw_route.isdigit():
                route_id = int(raw_route)
            trip_id = vehicle.trip.trip_id or None
            start_time = vehicle.trip.start_time or None
            start_date = vehicle.trip.start_date or None

        if not vehicle.HasField("position"):
            continue
        lat = float(vehicle.position.latitude)
        lng = float(vehicle.position.longitude)
        speed_raw = float(vehicle.position.speed)
        speed = speed_raw if math.isfinite(speed_raw) and speed_raw >= 0.0 else 0.0
        ts = int(vehicle.timestamp) if vehicle.timestamp else 0
        max_vehicle_ts = max(max_vehicle_ts, ts)
        in_bbox = (
            math.isfinite(lat)
            and math.isfinite(lng)
            and BBOX_LAT_MIN <= lat <= BBOX_LAT_MAX
            and BBOX_LNG_MIN <= lng <= BBOX_LNG_MAX
        )
        records.append(
            {
                "vehicle_id": vid,
                "route_id": route_id,
                "trip_id": trip_id,
                "ts": ts,
                "lat": lat,
                "lng": lng,
                "speed": min(speed, 50.0),
                "start_time": start_time,
                "start_date": start_date,
                "in_bbox": in_bbox,
                "fresh": False,
            }
        )

    if header_ts <= 0:
        if max_vehicle_ts > 0:
            header_ts = max_vehicle_ts
        else:
            raise LiveFeedError(
                "missing_feed_timestamps", "VehiclePositions protobuf has no header or vehicle timestamps"
            )

    by_route: dict[int, list[dict[str, Any]]] = defaultdict(list)
    fresh_count = 0
    in_bbox_count = 0
    for row in records:
        row["fresh"] = is_fresh(row["ts"], now, max_entity_age)
        fresh_count += row["fresh"]
        in_bbox_count += row["in_bbox"]
        if row["route_id"] is not None:
            by_route[row["route_id"]].append(row)

    route_stats = {
        str(route_id): route_summary(
            route_id,
            len(rows),
            sum(1 for r in rows if r["in_bbox"]),
            [[r["ts"], r["speed"]] for r in rows if r["fresh"] and r["in_bbox"]],
        )
        for route_id, rows in by_route.items()
    }

    return DecodedFeed(
        header_timestamp=header_ts,
        polled_at=now,
        records=records,
        route_stats=route_stats,
        vehicle_count=len(records),
        fresh_vehicle_count=fresh_count,
        entity_max_age_s=max_entity_age,
        in_bbox_count=in_bbox_count,
        route_count=len(route_stats),
    )


def persist_decoded_feed(
    data_dir: Path,
    decoded: DecodedFeed,
    *,
    feed_url: str = LIVE_FEED_URL,
    keep_snapshots: int = LIVE_SNAPSHOT_KEEP,
) -> dict[str, Any]:
    """Persist decoded VehiclePositions snapshot, route stats, and feed_state.json."""
    live_dir = _live_dir(data_dir)
    snapshots_dir = live_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    previous = read_live_state(data_dir) or {}
    poll_count = int(previous.get("poll_count", 0)) + 1
    repeated = previous.get("header_timestamp") == decoded.header_timestamp

    snapshot_path = snapshots_dir / f"vp_{decoded.header_timestamp}.parquet"
    if not snapshot_path.exists():
        table = pa.Table.from_pylist(decoded.records, schema=SNAPSHOT_SCHEMA)
        atomic_write_parquet(snapshot_path, table, compression="snappy")
        atomic_write_parquet(live_dir / "latest.parquet", table, compression="snappy")

        existing = sorted(snapshots_dir.glob("vp_*.parquet"))
        if keep_snapshots > 0 and len(existing) > keep_snapshots:
            for stale in existing[:-keep_snapshots]:
                stale.unlink(missing_ok=True)

    fresh_ts = [row["ts"] for row in decoded.records if row["fresh"]]
    _write_state_json(
        live_dir / "route_stats.json",
        {
            "header_timestamp": decoded.header_timestamp,
            "header_timestamp_iso": iso_ist(decoded.header_timestamp),
            "polled_at": decoded.polled_at,
            "polled_at_iso": iso_ist(decoded.polled_at),
            "fresh_vehicle_ts": sorted(fresh_ts),
            "routes": decoded.route_stats,
        },
    )

    age_at_poll = round(max(0.0, decoded.polled_at - decoded.header_timestamp), 2)
    state = {
        "status": "ok",
        "header_timestamp": decoded.header_timestamp,
        "header_timestamp_iso": iso_ist(decoded.header_timestamp),
        "timestamp": decoded.header_timestamp,
        "polled_at": round(decoded.polled_at, 3),
        "polled_at_iso": iso_ist(decoded.polled_at),
        "vehicle_count": decoded.vehicle_count,
        "fresh_vehicle_count": decoded.fresh_vehicle_count,
        "latest_fresh_vehicle_timestamp": max(fresh_ts, default=None),
        "entity_max_age_seconds": decoded.entity_max_age_s,
        "in_bbox_count": decoded.in_bbox_count,
        "route_count": decoded.route_count,
        "feed_age_seconds_at_poll": age_at_poll,
        "snapshot_path": str(snapshot_path),
        "poll_count": poll_count,
        "repeated_snapshot": repeated,
        "consecutive_failures": 0,
        "last_error": None,
        "auth_configured": True,
        "feed_url": feed_url,
    }
    _write_state_json(live_dir / "feed_state.json", state)
    _append_log(
        data_dir,
        {
            "event": "poll_ok",
            "polled_at": round(decoded.polled_at, 3),
            "polled_at_iso": state["polled_at_iso"],
            "header_timestamp": decoded.header_timestamp,
            "header_timestamp_iso": state["header_timestamp_iso"],
            "feed_age_seconds_at_poll": age_at_poll,
            "vehicle_count": decoded.vehicle_count,
            "fresh_vehicle_count": decoded.fresh_vehicle_count,
            "route_count": decoded.route_count,
            "repeated_snapshot": repeated,
            "poll_count": poll_count,
            "snapshot_path": str(snapshot_path),
        },
    )
    return state


def record_poll_failure(
    data_dir: Path,
    error: BaseException | str,
    *,
    feed_url: str = LIVE_FEED_URL,
) -> dict[str, Any]:
    """Record only a fixed error category and preserve the last good timestamp."""
    now = time.time()
    category = sanitize_poll_error(error)
    previous = read_live_state(data_dir) or {}
    failures = int(previous.get("consecutive_failures", 0)) + 1
    state = {
        **previous,
        "status": "error" if not previous.get("header_timestamp") else "degraded",
        "last_attempt_at": round(now, 3),
        "last_attempt_iso": iso_ist(now),
        "consecutive_failures": failures,
        "last_error": category,
        "feed_url": feed_url,
    }
    _write_state_json(_live_dir(data_dir) / "feed_state.json", state)
    _append_log(
        data_dir,
        {
            "event": "poll_error",
            "ts": round(now, 3),
            "ts_iso": iso_ist(now),
            "consecutive_failures": failures,
            "error": category,
        },
    )
    return state


def read_live_state(data_dir: Path) -> dict[str, Any] | None:
    path = _live_dir(data_dir) / "feed_state.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("last_error"):
            state["last_error"] = sanitize_poll_error(str(state["last_error"]))
        return state
    except (OSError, ValueError):
        return None


def read_live_route_stats(data_dir: Path) -> dict[int, dict[str, Any]]:
    path = _live_dir(data_dir) / "route_stats.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        routes = payload.get("routes", {})
        return {int(k): v for k, v in routes.items() if str(k).isdigit()}
    except (OSError, ValueError, TypeError):
        return {}


def _run_without_loop(coro: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
    """Run a coroutine whose awaits never suspend (its only I/O is synchronous)."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise RuntimeError("poll coroutine suspended outside an event loop")


class LivePoller:
    """Polls Delhi VehiclePositions GTFS-RT feed and updates data/live/ state."""

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        feed_url: str | None = None,
        api_key: str | None = None,
        env_path: Path | None = None,
        timeout_s: float = 30.0,
        interval_s: float | None = None,
    ) -> None:
        self.data_dir = Path(data_dir or DATA_DIR)
        self.feed_url = feed_url or LIVE_FEED_URL
        self._api_key = api_key if api_key is not None else load_env_key("DELHI_TS_API_KEY", env_path)
        self.timeout_s = timeout_s
        self.interval_s = LIVE_POLL_INTERVAL_S if interval_s is None else float(interval_s)

    @property
    def auth_configured(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise LiveFeedError(
                "auth_not_configured", "DELHI_TS_API_KEY is not configured in environment or .env"
            )
        return {"x-api-key": self._api_key}

    async def _poll(self, get: Callable[[dict[str, str]], Awaitable[httpx.Response]]) -> dict[str, Any]:
        """Pause check, fetch, decode and persist one snapshot; failures persist only a category."""
        if is_feed_paused(self.data_dir):
            return {"status": "paused", "paused": True, "state": read_live_state(self.data_dir)}
        try:
            response = await get(self._headers())
            if response.status_code != 200:
                raise LiveFeedError(
                    "upstream_http_error",
                    f"VehiclePositions upstream returned HTTP {response.status_code}",
                )
            decoded = decode_vehicle_positions(response.content)
            return persist_decoded_feed(self.data_dir, decoded, feed_url=self.feed_url)
        except (httpx.HTTPError, LiveFeedError) as exc:
            return record_poll_failure(self.data_dir, exc, feed_url=self.feed_url)

    def poll_once(self, client: httpx.Client | None = None) -> dict[str, Any]:
        """Synchronously fetch, decode, and persist one VehiclePositions snapshot."""

        async def get(headers: dict[str, str]) -> httpx.Response:
            if client is not None:
                return client.get(self.feed_url, headers=headers, timeout=self.timeout_s)
            with httpx.Client(timeout=self.timeout_s) as own_client:
                return own_client.get(self.feed_url, headers=headers)

        return _run_without_loop(self._poll(get))

    async def apoll_once(self, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
        """Asynchronously fetch, decode, and persist one VehiclePositions snapshot."""

        async def get(headers: dict[str, str]) -> httpx.Response:
            if client is not None:
                return await client.get(self.feed_url, headers=headers, timeout=self.timeout_s)
            async with httpx.AsyncClient(timeout=self.timeout_s) as own_client:
                return await own_client.get(self.feed_url, headers=headers)

        return await self._poll(get)

    async def run_loop(self, stop_event: asyncio.Event | None = None) -> None:
        """Poll every interval_s; while paused, check once a second so a resume is picked up quickly."""
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            while stop_event is None or not stop_event.is_set():
                if is_feed_paused(self.data_dir):
                    await asyncio.sleep(1.0)
                    continue
                await self.apoll_once(client=client)
                slept = 0.0
                while slept < self.interval_s:
                    if stop_event is not None and stop_event.is_set():
                        return
                    if is_feed_paused(self.data_dir):
                        break
                    step = min(1.0, self.interval_s - slept)
                    await asyncio.sleep(step)
                    slept += step


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delhi GTFS-RT VehiclePositions live poller")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--interval", type=float, default=LIVE_POLL_INTERVAL_S)
    parser.add_argument("--once", action="store_true", help="Run a single poll cycle and exit")
    parser.add_argument("--cycles", type=int, default=0, help="Run N poll cycles then exit")
    parser.add_argument("--pause", action="store_true", help="Pause live polling (simulates stopped feed)")
    parser.add_argument("--resume", action="store_true", help="Resume live polling")
    parser.add_argument("--status", action="store_true", help="Print sanitized current feed state")
    args = parser.parse_args(argv)

    if args.pause:
        print(json.dumps(pause_feed(args.data_dir), indent=2))
        return 0
    if args.resume:
        print(json.dumps(resume_feed(args.data_dir), indent=2))
        return 0
    if args.status:
        state = read_live_state(args.data_dir)
        age = live_feed_age_seconds(state)
        print(
            json.dumps(
                {
                    "paused": is_feed_paused(args.data_dir),
                    "current_feed_age_seconds": age,
                    "stale_threshold_seconds": LIVE_STALE_THRESHOLD_S,
                    "is_stale": age is None or age > LIVE_STALE_THRESHOLD_S,
                    "state": state,
                },
                indent=2,
            )
        )
        return 0

    poller = LivePoller(args.data_dir, interval_s=args.interval)
    if args.once:
        result = poller.poll_once()
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") in {"ok", "paused"} else 1

    count = 0
    try:
        while True:
            result = poller.poll_once()
            print(json.dumps(result, sort_keys=True), flush=True)
            count += 1
            if args.cycles > 0 and count >= args.cycles:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
