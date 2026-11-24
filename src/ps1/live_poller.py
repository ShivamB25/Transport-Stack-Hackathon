"""GTFS-RT VehiclePositions poller, snapshot persistence, and feed-age state."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import dotenv_values
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

from ps1.config import (
    BBOX_LAT_MAX,
    BBOX_LAT_MIN,
    BBOX_LNG_MAX,
    BBOX_LNG_MIN,
    DATA_DIR,
    IST,
    LIVE_DIR,
    LIVE_ENTITY_MAX_AGE_S,
    LIVE_FEED_URL,
    LIVE_POLL_INTERVAL_S,
    LIVE_SNAPSHOT_KEEP,
    LIVE_STALE_THRESHOLD_S,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

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
    """Raised when fetching or decoding VehiclePositions fails."""


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
        message = str(error).casefold()
        if "not configured" in message:
            return "auth_not_configured"
        if "empty vehiclepositions" in message:
            return "empty_upstream_payload"
        if "non-protobuf" in message:
            return "upstream_non_protobuf_payload"
        if "failed to decode" in message:
            return "protobuf_decode_error"
        if "no header or vehicle timestamps" in message:
            return "missing_feed_timestamps"
        if "upstream returned http" in message:
            return "upstream_http_error"
        return "live_feed_error"
    if isinstance(error, str):
        if error in _ERROR_CATEGORIES or re.fullmatch(r"upstream_http_[0-9]{3}", error):
            return error
        return "poll_error"
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


def _iso_ist(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).isoformat()


def _live_dir(data_dir: Path) -> Path:
    return Path(data_dir) / LIVE_DIR


def _pause_path(data_dir: Path) -> Path:
    return _live_dir(data_dir) / "poller.paused"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _append_log(data_dir: Path, entry: dict[str, Any]) -> None:
    log_path = _live_dir(data_dir) / "poller.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = {k: v for k, v in entry.items() if "key" not in k.lower()}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sanitized, sort_keys=True) + "\n")


def load_api_key(env_path: Path | None = None) -> str | None:
    """Load DELHI_TS_API_KEY from environment or repo .env without logging it."""
    env_value = os.environ.get("DELHI_TS_API_KEY")
    if env_value:
        return env_value.strip()
    candidate = env_path or (REPO_ROOT / ".env")
    if candidate.exists():
        values = dotenv_values(candidate)
        raw = values.get("DELHI_TS_API_KEY")
        if raw:
            return str(raw).strip()
    return None


def is_feed_paused(data_dir: Path) -> bool:
    return _pause_path(data_dir).exists()


def pause_feed(data_dir: Path, reason: str = "manual_pause") -> dict[str, Any]:
    path = _pause_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload = {"paused": True, "paused_at": now, "paused_at_iso": _iso_ist(now), "reason": reason}
    _atomic_write_json(path, payload)
    _append_log(data_dir, {"event": "paused", "ts": now, "ts_iso": _iso_ist(now), "reason": reason})
    return payload


def resume_feed(data_dir: Path) -> dict[str, Any]:
    path = _pause_path(data_dir)
    now = time.time()
    if path.exists():
        path.unlink()
    _append_log(data_dir, {"event": "resumed", "ts": now, "ts_iso": _iso_ist(now)})
    return {"paused": False, "resumed_at": now, "resumed_at_iso": _iso_ist(now)}


def effective_entity_max_age_s(stale_threshold_s: float | None = None) -> float:
    threshold = LIVE_STALE_THRESHOLD_S if stale_threshold_s is None else stale_threshold_s
    return min(float(LIVE_ENTITY_MAX_AGE_S), float(threshold))


def _is_fresh(ts: float, now: float, max_age_s: float) -> bool:
    return ts > 0 and 0.0 <= now - ts <= max_age_s


def _route_summary(
    route_id: int,
    vehicle_count: int,
    in_bbox_count: int,
    fresh_entities: list[list[float]],
) -> dict[str, Any]:
    """Aggregate fresh in-bbox entities, each stored as [timestamp, speed]."""
    speeds = [speed for _, speed in fresh_entities if speed > 0.2]
    all_speeds = [speed for _, speed in fresh_entities]
    return {
        "route_id": route_id,
        "vehicle_count": vehicle_count,
        "fresh_vehicle_count": len(fresh_entities),
        "in_bbox_count": in_bbox_count,
        "moving_vehicle_count": len(speeds),
        "median_speed_mps": round(float(median(speeds or all_speeds)), 3) if all_speeds else 0.0,
        "latest_ts": int(max((ts for ts, _ in fresh_entities), default=0)),
        "observed_coverage": round(len(fresh_entities) / vehicle_count, 4) if vehicle_count else 0.0,
        "fresh_entities": fresh_entities,
    }


def refresh_route_stat(stat: dict[str, Any], *, now: float, max_age_s: float) -> dict[str, Any]:
    """Recompute a stored route aggregate from only the entities still fresh at `now`."""
    fresh = [entity for entity in stat.get("fresh_entities", []) if _is_fresh(entity[0], now, max_age_s)]
    return _route_summary(
        int(stat.get("route_id", 0)),
        int(stat.get("vehicle_count", len(fresh))),
        int(stat.get("in_bbox_count", len(fresh))),
        fresh,
    )


def decode_vehicle_positions(
    payload: bytes,
    *,
    polled_at: float | None = None,
    entity_max_age_s: float | None = None,
) -> DecodedFeed:
    """Decode a GTFS-RT VehiclePositions protobuf payload into records and route stats."""
    if not payload:
        raise LiveFeedError("empty VehiclePositions response payload")
    prefix = payload.lstrip()[:32].lower()
    if prefix.startswith((b"<!doctype", b"<html", b"{\"error")):
        raise LiveFeedError("upstream returned non-protobuf HTML/JSON payload instead of VehiclePositions.pb")

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(payload)
    except DecodeError as exc:
        raise LiveFeedError(f"failed to decode GTFS-RT protobuf: {exc}") from exc

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
            raise LiveFeedError("VehiclePositions protobuf has no header or vehicle timestamps")

    by_route: dict[int, list[dict[str, Any]]] = defaultdict(list)
    fresh_count = 0
    in_bbox_count = 0
    for row in records:
        is_fresh = _is_fresh(row["ts"], now, max_entity_age)
        row["fresh"] = is_fresh
        if is_fresh:
            fresh_count += 1
        if row["in_bbox"]:
            in_bbox_count += 1
        if row["route_id"] is not None:
            by_route[row["route_id"]].append(row)

    route_stats = {
        str(route_id): _route_summary(
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
        tmp_pq = snapshot_path.with_name(f".{snapshot_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        pq.write_table(table, tmp_pq, compression="snappy")
        tmp_pq.replace(snapshot_path)

        latest_path = live_dir / "latest.parquet"
        tmp_latest = latest_path.with_name(f".latest.{os.getpid()}.{time.time_ns()}.tmp")
        pq.write_table(table, tmp_latest, compression="snappy")
        tmp_latest.replace(latest_path)

        existing = sorted(snapshots_dir.glob("vp_*.parquet"))
        if keep_snapshots > 0 and len(existing) > keep_snapshots:
            for stale in existing[:-keep_snapshots]:
                stale.unlink(missing_ok=True)

    _atomic_write_json(
        live_dir / "route_stats.json",
        {
            "header_timestamp": decoded.header_timestamp,
            "header_timestamp_iso": _iso_ist(decoded.header_timestamp),
            "polled_at": decoded.polled_at,
            "polled_at_iso": _iso_ist(decoded.polled_at),
            "fresh_vehicle_ts": sorted(row["ts"] for row in decoded.records if row["fresh"]),
            "routes": decoded.route_stats,
        },
    )

    age_at_poll = round(max(0.0, decoded.polled_at - decoded.header_timestamp), 2)
    state = {
        "status": "ok",
        "header_timestamp": decoded.header_timestamp,
        "header_timestamp_iso": _iso_ist(decoded.header_timestamp),
        "timestamp": decoded.header_timestamp,
        "polled_at": round(decoded.polled_at, 3),
        "polled_at_iso": _iso_ist(decoded.polled_at),
        "vehicle_count": decoded.vehicle_count,
        "fresh_vehicle_count": decoded.fresh_vehicle_count,
        "latest_fresh_vehicle_timestamp": max(
            (row["ts"] for row in decoded.records if row["fresh"]),
            default=None,
        ),
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
    _atomic_write_json(live_dir / "feed_state.json", state)
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
        "last_attempt_iso": _iso_ist(now),
        "consecutive_failures": failures,
        "last_error": category,
        "feed_url": feed_url,
    }
    _atomic_write_json(_live_dir(data_dir) / "feed_state.json", state)
    _append_log(
        data_dir,
        {
            "event": "poll_error",
            "ts": round(now, 3),
            "ts_iso": _iso_ist(now),
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


def fresh_fleet_count(data_dir: Path, *, now: float, max_age_s: float) -> int:
    """Count vehicles whose own timestamps are still fresh at `now`."""
    path = _live_dir(data_dir) / "route_stats.json"
    try:
        timestamps = json.loads(path.read_text(encoding="utf-8")).get("fresh_vehicle_ts", [])
    except (OSError, ValueError, AttributeError):
        return 0
    return sum(1 for ts in timestamps if _is_fresh(ts, now, max_age_s))


def live_feed_age_seconds(state: dict[str, Any] | None, *, now: float | None = None) -> float | None:
    if not state:
        return None
    raw = state.get("header_timestamp", state.get("timestamp", state.get("feed_timestamp")))
    if raw is None:
        return None
    try:
        ts = float(raw)
    except (ValueError, TypeError):
        return None
    reference = time.time() if now is None else float(now)
    return round(max(0.0, reference - ts), 2)


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
        self._api_key = api_key if api_key is not None else load_api_key(env_path)
        self.timeout_s = timeout_s
        self.interval_s = LIVE_POLL_INTERVAL_S if interval_s is None else float(interval_s)

    @property
    def auth_configured(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise LiveFeedError("DELHI_TS_API_KEY is not configured in environment or .env")
        return {"x-api-key": self._api_key}

    def poll_once(self, client: httpx.Client | None = None) -> dict[str, Any]:
        """Synchronously fetch, decode, and persist one VehiclePositions snapshot."""
        if is_feed_paused(self.data_dir):
            return {"status": "paused", "paused": True, "state": read_live_state(self.data_dir)}
        try:
            headers = self._headers()
            if client is not None:
                response = client.get(self.feed_url, headers=headers, timeout=self.timeout_s)
            else:
                with httpx.Client(timeout=self.timeout_s) as own_client:
                    response = own_client.get(self.feed_url, headers=headers)
            if response.status_code != 200:
                raise LiveFeedError(f"VehiclePositions upstream returned HTTP {response.status_code}")
            decoded = decode_vehicle_positions(response.content)
            return persist_decoded_feed(self.data_dir, decoded, feed_url=self.feed_url)
        except (httpx.HTTPError, LiveFeedError) as exc:
            return record_poll_failure(self.data_dir, exc, feed_url=self.feed_url)

    async def apoll_once(self, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
        """Asynchronously fetch, decode, and persist one VehiclePositions snapshot."""
        if is_feed_paused(self.data_dir):
            return {"status": "paused", "paused": True, "state": read_live_state(self.data_dir)}
        try:
            headers = self._headers()
            if client is not None:
                response = await client.get(self.feed_url, headers=headers, timeout=self.timeout_s)
            else:
                async with httpx.AsyncClient(timeout=self.timeout_s) as own_client:
                    response = await own_client.get(self.feed_url, headers=headers)
            if response.status_code != 200:
                raise LiveFeedError(f"VehiclePositions upstream returned HTTP {response.status_code}")
            decoded = decode_vehicle_positions(response.content)
            return persist_decoded_feed(self.data_dir, decoded, feed_url=self.feed_url)
        except (httpx.HTTPError, LiveFeedError) as exc:
            return record_poll_failure(self.data_dir, exc, feed_url=self.feed_url)

    async def run_loop(self, stop_event: asyncio.Event | None = None) -> None:
        """Continuously poll on interval_s; reacts quickly to resume after pause."""
        was_paused = False
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            while stop_event is None or not stop_event.is_set():
                if is_feed_paused(self.data_dir):
                    was_paused = True
                    await asyncio.sleep(1.0)
                    continue
                if was_paused:
                    was_paused = False
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
        info = resume_feed(args.data_dir)
        print(json.dumps(info, indent=2))
        return 0
    if args.status:
        state = read_live_state(args.data_dir)
        age = live_feed_age_seconds(state)
        threshold = float(os.environ.get("PS1_LIVE_STALE_THRESHOLD_S", str(LIVE_STALE_THRESHOLD_S)))
        print(
            json.dumps(
                {
                    "paused": is_feed_paused(args.data_dir),
                    "current_feed_age_seconds": age,
                    "stale_threshold_seconds": threshold,
                    "is_stale": age is None or age > threshold,
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
