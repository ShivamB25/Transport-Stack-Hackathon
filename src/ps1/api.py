"""FastAPI journey planning surface."""
from __future__ import annotations

import asyncio
import contextlib
import csv
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ps1.config import (
    BBOX_LAT_MAX,
    BBOX_LAT_MIN,
    BBOX_LNG_MAX,
    BBOX_LNG_MIN,
    DATA_DIR,
    IST,
    LIVE_MIN_VEHICLES,
    LIVE_POLL_INTERVAL_S,
    LIVE_STALE_THRESHOLD_S,
    OTP_URL,
)
from ps1.live_poller import (
    LivePoller,
    effective_entity_max_age_s,
    fresh_fleet_count,
    is_feed_paused,
    live_feed_age_seconds,
    pause_feed,
    read_live_state,
    resume_feed,
)
from ps1.otp_client import OTPClient, OTPClientError
from ps1.replay_demo import build_marginal_transfer_demo
from ps1.retime import RetimeModel, retime_itinerary

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_REPLAY_DEPART = datetime(2026, 10, 22, 8, 30, tzinfo=IST)


class Leg(BaseModel):
    type: str
    route_id: int | None = None
    route_name: str | None = None
    from_stop: str | None = None
    to_stop: str | None = None
    from_stop_id: str | None = None
    to_stop_id: str | None = None
    from_lat: float | None = None
    from_lng: float | None = None
    to_lat: float | None = None
    to_lng: float | None = None
    scheduled_duration_s: int
    estimated_duration_s: int
    window_p10_s: int | None = None
    window_p90_s: int | None = None
    source: str
    timing_source: str | None = None
    observed_coverage: float | None = Field(default=None, ge=0, le=1)
    observation_count: int | None = None
    coverage_note: str | None = None
    connection_missed_probability: float | None = Field(default=None, ge=0, le=1)
    next_bus_headway_s: int | None = None
    geometry: str | None = None


class JourneyOption(BaseModel):
    candidate_id: str
    legs: list[Leg]
    total_duration_s: int
    total_window_p10_s: int
    total_window_p90_s: int
    quieter_hour_typical_s: int
    scenario_count: int


class ResolvedEndpoint(BaseModel):
    requested: str
    stop_id: str
    stop_name: str
    lat: float
    lng: float
    routing_lat: float
    routing_lng: float


class JourneyResponse(BaseModel):
    status: str
    mode: str
    feed_age_seconds: float | None = None
    feed_timestamp: str | None = None
    reference_timestamp: str | None = None
    feed_summary: dict[str, Any] | None = None
    selected_rung: str
    depart: str
    resolved_from: ResolvedEndpoint
    resolved_to: ResolvedEndpoint
    options: list[JourneyOption]
    abstention: dict[str, Any] | None = None


@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    lat: float
    lng: float


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


class StopResolver:
    """Exact stop names plus explicit aliases; deliberately no fuzzy matching."""

    def __init__(self, stops_path: Path) -> None:
        self.stops: dict[str, Stop] = {}
        self.names: dict[str, list[str]] = {}
        with stops_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                stop = Stop(row["stop_id"], row["stop_name"], float(row["stop_lat"]), float(row["stop_lon"]))
                self.stops[stop.stop_id] = stop
                self.names.setdefault(_normalise(stop.name), []).append(stop.stop_id)
        self.aliases = {
            "kashmere gate isbt": "4730",
            "isbt kashmere gate": "4730",
            "kashmere gate": "4730",
            "nehru place": "2559",
        }

    def resolve(self, value: str, field: str) -> ResolvedEndpoint:
        value = value.strip()
        if not value:
            raise HTTPException(400, f"{field} must be lat,lng or an exact stop name")
        key = _normalise(value)
        stop_id = self.aliases.get(key)
        if stop_id is None:
            matches = self.names.get(key, [])
            stop_id = min(matches) if matches else None
        if stop_id is not None:
            stop = self.stops[stop_id]
            return ResolvedEndpoint(
                requested=value,
                stop_id=stop.stop_id,
                stop_name=stop.name,
                lat=stop.lat,
                lng=stop.lng,
                routing_lat=stop.lat,
                routing_lng=stop.lng,
            )
        if "," in value:
            parts = value.split(",")
            try:
                if len(parts) != 2:
                    raise ValueError
                lat, lng = float(parts[0]), float(parts[1])
                if not math.isfinite(lat) or not math.isfinite(lng):
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(400, f"{field} coordinates must be numeric lat,lng") from exc
            if not (BBOX_LAT_MIN <= lat <= BBOX_LAT_MAX and BBOX_LNG_MIN <= lng <= BBOX_LNG_MAX):
                raise HTTPException(400, f"{field} coordinates are outside the Delhi service area")
            stop = min(
                self.stops.values(),
                key=lambda candidate: (candidate.lat - lat) ** 2 + (candidate.lng - lng) ** 2,
            )
            return ResolvedEndpoint(
                requested=value,
                stop_id=stop.stop_id,
                stop_name=stop.name,
                lat=stop.lat,
                lng=stop.lng,
                routing_lat=lat,
                routing_lng=lng,
            )
        raise HTTPException(404, f"unknown stop name for {field}: {value}")


def _parse_depart(value: str) -> datetime:
    try:
        depart = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, "depart must be an ISO 8601 date-time with an offset") from exc
    if depart.tzinfo is None or depart.utcoffset() is None:
        raise HTTPException(422, "depart must include a timezone offset")
    try:
        return depart.astimezone(IST)
    except OverflowError as exc:
        raise HTTPException(422, "depart is outside the supported date range") from exc


def _stale_threshold(override: float | None) -> float:
    if override is not None:
        return float(override)
    return float(os.environ.get("PS1_LIVE_STALE_THRESHOLD_S", str(LIVE_STALE_THRESHOLD_S)))


def _min_live_vehicles(override: int | None) -> int:
    if override is not None:
        return int(override)
    return int(os.environ.get("PS1_LIVE_MIN_VEHICLES", str(LIVE_MIN_VEHICLES)))


def _min_route_coverage(override: float | None) -> float:
    if override is not None:
        return float(override)
    return float(os.environ.get("PS1_MIN_ROUTE_COVERAGE", "0.0"))


def _explicit_mode(requested_mode: str) -> str | None:
    if requested_mode != "auto":
        return requested_mode
    env_mode = os.environ.get("PS1_MODE", "auto").strip().lower()
    return env_mode if env_mode in {"live", "replay"} else None


def _resolve_mode(requested_mode: str, departure: datetime) -> str:
    departure = departure.astimezone(IST)
    is_october_2026 = departure.year == 2026 and departure.month == 10
    mode = _explicit_mode(requested_mode) or ("replay" if is_october_2026 else "live")
    if mode == "replay" and not is_october_2026:
        raise HTTPException(422, "replay departures must be in October 2026")
    return mode


def _resolve_departure_and_mode(depart: str | None, requested_mode: str) -> tuple[datetime, str]:
    """A missing depart means now in live mode, or the scripted replay day when replay is explicit."""
    if depart is not None:
        departure = _parse_depart(depart)
        return departure, _resolve_mode(requested_mode, departure)
    if _explicit_mode(requested_mode) == "replay":
        return DEFAULT_REPLAY_DEPART, "replay"
    return datetime.now(IST).replace(microsecond=0), "live"


def _evaluate_live_feed(
    data_dir: Path,
    *,
    stale_threshold_s: float,
    min_vehicles: int,
) -> tuple[dict[str, Any] | None, float | None, dict[str, Any] | None]:
    state = read_live_state(data_dir)
    age = live_feed_age_seconds(state)
    if not state or state.get("header_timestamp") is None:
        return (
            state,
            None,
            {
                "label": "not enough data",
                "code": "feed_unavailable",
                "reason": "Not enough data: live GPS feed unavailable (no VehiclePositions snapshot ingested).",
                "feed_age_seconds": None,
                "threshold_seconds": round(stale_threshold_s, 1),
            },
        )
    if age is not None and age > stale_threshold_s:
        return (
            state,
            age,
            {
                "label": "not enough data",
                "code": "stale_feed",
                "reason": (
                    f"Not enough data: live GPS feed is stale "
                    f"({round(age, 1)}s old, threshold {round(stale_threshold_s, 1)}s)."
                ),
                "feed_age_seconds": round(age, 1),
                "threshold_seconds": round(stale_threshold_s, 1),
                "last_feed_timestamp": state.get("header_timestamp_iso"),
            },
        )
    fresh_count = fresh_fleet_count(
        data_dir, now=time.time(), max_age_s=effective_entity_max_age_s(stale_threshold_s)
    )
    state = {**state, "fresh_vehicle_count": fresh_count}
    if fresh_count < min_vehicles:
        return (
            state,
            age,
            {
                "label": "not enough data",
                "code": "insufficient_coverage",
                "reason": (
                    f"Not enough data: live GPS fleet coverage is insufficient "
                    f"({fresh_count} fresh vehicles, minimum {min_vehicles} required)."
                ),
                "feed_age_seconds": round(age, 1) if age is not None else None,
                "threshold_seconds": round(stale_threshold_s, 1),
                "vehicle_count": int(state.get("vehicle_count", 0)),
                "fresh_vehicle_count": fresh_count,
                "min_vehicles": min_vehicles,
            },
        )
    return state, age, None


def _option_has_supported_route(option: dict[str, Any]) -> bool:
    """A journey is supported when at least one ride has route-level observations."""
    return any(
        leg.get("type") == "ride" and float(leg.get("observed_coverage") or 0.0) > 0.0
        for leg in option.get("legs", [])
    )


def _max_option_ride_coverage(options: list[dict[str, Any]]) -> float:
    best = 0.0
    for option in options:
        rides = [leg for leg in option["legs"] if leg["type"] == "ride"]
        if not rides:
            continue
        avg_cov = sum(float(leg.get("observed_coverage") or 0.0) for leg in rides) / len(rides)
        best = max(best, avg_cov)
    return round(best, 4)


def create_app(
    data_dir: Path | None = None,
    otp_url: str | None = None,
    otp: Any | None = None,
    model: Any | None = None,
    *,
    enable_poller: bool | None = None,
    poller: LivePoller | None = None,
    stale_threshold_s: float | None = None,
    min_live_vehicles: int | None = None,
    min_route_coverage: float | None = None,
) -> FastAPI:
    using_default_runtime = data_dir is None and otp is None and model is None
    resolved_data_dir = Path(data_dir or DATA_DIR)
    resolver = StopResolver(resolved_data_dir / "gtfs" / "stops.txt")
    otp_client = otp or OTPClient(otp_url or OTP_URL)
    timing_model = model or RetimeModel(resolved_data_dir)

    if enable_poller is None:
        env_enabled = os.environ.get("PS1_ENABLE_LIVE_POLLER", "1").strip().lower() not in {"0", "false", "no"}
        env_replay_only = os.environ.get("PS1_MODE", "auto").strip().lower() == "replay"
        poller_enabled = using_default_runtime and env_enabled and not env_replay_only
    else:
        poller_enabled = enable_poller

    live_poller = poller or LivePoller(resolved_data_dir, interval_s=LIVE_POLL_INTERVAL_S)
    poller_stop = asyncio.Event()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task: asyncio.Task[None] | None = None
        if poller_enabled and live_poller.auth_configured:
            task = asyncio.create_task(live_poller.run_loop(poller_stop))
        try:
            yield
        finally:
            poller_stop.set()
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="Delhi Model-Timed Journeys",
        version="1.0.0",
        description="OTP bus candidates re-timed from causal GPS replay and live VehiclePositions with joint journey windows.",
        lifespan=lifespan,
    )

    @app.get("/journeys", response_model=JourneyResponse)
    async def journeys(
        from_: str = Query(alias="from", min_length=1),
        to: str = Query(min_length=1),
        depart: str | None = Query(
            default=None,
            min_length=1,
            description=(
                "ISO 8601 date-time with offset; defaults to now in Asia/Kolkata (live), "
                "or 2026-10-22T08:30+05:30 when replay mode is explicit."
            ),
        ),
        mode: Literal["auto", "live", "replay"] = Query(default="auto"),
        replay_delay_s: float = Query(default=0, ge=0, le=3600),
        min_coverage: float | None = Query(default=None, ge=0.0, le=1.0),
    ) -> JourneyResponse:
        origin = resolver.resolve(from_, "from")
        destination = resolver.resolve(to, "to")
        departure, effective_mode = _resolve_departure_and_mode(depart, mode)
        if replay_delay_s and effective_mode != "replay":
            raise HTTPException(422, "replay_delay_s is only valid for October replay queries")

        now_iso = datetime.fromtimestamp(time.time(), IST).isoformat()
        threshold = _stale_threshold(stale_threshold_s)
        min_veh = _min_live_vehicles(min_live_vehicles)
        coverage_floor = _min_route_coverage(min_coverage if min_coverage is not None else min_route_coverage)

        feed_age: float | None = None
        feed_timestamp: str | None = None
        reference_timestamp: str | None = None
        feed_summary: dict[str, Any] | None = None

        def respond(
            status: str,
            options: list[dict[str, Any]] | None = None,
            abstention: dict[str, Any] | None = None,
        ) -> JourneyResponse:
            return JourneyResponse(
                status=status,
                mode=effective_mode,
                feed_age_seconds=feed_age,
                feed_timestamp=feed_timestamp,
                reference_timestamp=reference_timestamp,
                feed_summary=feed_summary,
                selected_rung=timing_model.selected_rung,
                depart=departure.isoformat(),
                resolved_from=origin,
                resolved_to=destination,
                options=options or [],
                abstention=abstention,
            )

        if effective_mode == "live":
            state, feed_age, abstention = _evaluate_live_feed(
                resolved_data_dir,
                stale_threshold_s=threshold,
                min_vehicles=min_veh,
            )
            feed_timestamp = state.get("header_timestamp_iso") if state else None
            reference_timestamp = now_iso
            if state:
                feed_summary = {
                    "vehicle_count": state.get("vehicle_count"),
                    "fresh_vehicle_count": state.get("fresh_vehicle_count"),
                    "route_count": state.get("route_count"),
                    "poll_count": state.get("poll_count"),
                    "snapshot_path": state.get("snapshot_path"),
                    "stale_threshold_seconds": round(threshold, 1),
                    "paused": is_feed_paused(resolved_data_dir),
                }
            if abstention is not None:
                return respond("insufficient_data", abstention=abstention)
        else:
            replay_info = timing_model.replay_reference_info(departure)
            feed_age = replay_info["feed_age_seconds"]
            feed_timestamp = replay_info["feed_timestamp"]
            reference_timestamp = replay_info["reference_timestamp"]
            feed_summary = {
                "fixture_path": replay_info["fixture_path"],
                "loaded_passage_files": replay_info["loaded_passage_files"],
                "replay_data_available": replay_info["replay_data_available"],
                "same_day_data_available": replay_info["same_day_data_available"],
                "visible_observations": replay_info["visible_observations"],
                "same_day_observations": replay_info["same_day_observations"],
                "observed_same_day_stretches": replay_info["observed_same_day_stretches"],
                "live_feed_contacted": False,
            }
            if not replay_info["replay_data_available"]:
                return respond(
                    "insufficient_data",
                    abstention={
                        "label": "not enough data",
                        "code": "replay_data_unavailable",
                        "reason": (
                            "Not enough data: no reconstructed October observations were loaded "
                            "before the requested replay cutoff."
                        ),
                        "requested_cutoff": departure.isoformat(),
                        "visible_observations": 0,
                    },
                )

        try:
            candidates = await otp_client.plan(
                (origin.routing_lat, origin.routing_lng),
                (destination.routing_lat, destination.routing_lng),
                departure,
            )
        except OTPClientError as exc:
            raise HTTPException(502, str(exc)) from exc
        options = [
            retime_itinerary(
                candidate,
                departure,
                effective_mode,
                timing_model,
                replay_delay_s,
                stale_threshold_s=threshold,
            )
            for candidate in candidates
            if any(leg.get("transitLeg") for leg in candidate.get("legs", []))
        ]
        if not options:
            raise HTTPException(404, "no bus itineraries found for this request")
        options.sort(key=lambda option: option["total_duration_s"])

        if not any(_option_has_supported_route(option) for option in options):
            return respond(
                "insufficient_data",
                abstention={
                    "label": "not enough data",
                    "code": "insufficient_route_coverage",
                    "reason": (
                        "Not enough data: no candidate journey has a supported route with "
                        "usable route-level observations."
                    ),
                    "feed_age_seconds": feed_age,
                    "observed_coverage": 0.0,
                    "candidate_options": len(options),
                    "supported_options": 0,
                },
            )

        if coverage_floor > 0.0:
            best_cov = _max_option_ride_coverage(options)
            if best_cov < coverage_floor:
                return respond(
                    "insufficient_data",
                    abstention={
                        "label": "not enough data",
                        "code": "insufficient_route_coverage",
                        "reason": (
                            f"Not enough data: observed route coverage ({best_cov:.2f}) "
                            f"is below required threshold ({coverage_floor:.2f})."
                        ),
                        "feed_age_seconds": feed_age,
                        "observed_coverage": best_cov,
                        "min_coverage": coverage_floor,
                    },
                )

        return respond("ok", options)

    @app.get("/live/status")
    async def live_status() -> dict[str, Any]:
        state = read_live_state(resolved_data_dir)
        age = live_feed_age_seconds(state)
        threshold = _stale_threshold(stale_threshold_s)
        min_veh = _min_live_vehicles(min_live_vehicles)
        _, _, abstention = _evaluate_live_feed(
            resolved_data_dir,
            stale_threshold_s=threshold,
            min_vehicles=min_veh,
        )
        return {
            "paused": is_feed_paused(resolved_data_dir),
            "poller_enabled": poller_enabled,
            "auth_configured": live_poller.auth_configured,
            "current_feed_age_seconds": age,
            "stale_threshold_seconds": round(threshold, 1),
            "min_vehicles": min_veh,
            "is_fresh": abstention is None,
            "abstention": abstention,
            "state": state,
        }

    @app.post("/live/pause")
    async def live_pause(reason: str = Query(default="manual_pause")) -> dict[str, Any]:
        info = pause_feed(resolved_data_dir, reason=reason)
        state = read_live_state(resolved_data_dir)
        return {
            **info,
            "current_feed_age_seconds": live_feed_age_seconds(state),
            "stale_threshold_seconds": round(_stale_threshold(stale_threshold_s), 1),
        }

    @app.post("/live/resume")
    async def live_resume(poll_now: bool = Query(default=True)) -> dict[str, Any]:
        info = resume_feed(resolved_data_dir)
        poll_result: dict[str, Any] | None = None
        if poll_now and live_poller.auth_configured:
            poll_result = await live_poller.apoll_once()
        state = read_live_state(resolved_data_dir)
        return {
            **info,
            "poll_result": poll_result,
            "current_feed_age_seconds": live_feed_age_seconds(state),
            "stale_threshold_seconds": round(_stale_threshold(stale_threshold_s), 1),
        }

    @app.get("/health")
    async def health() -> dict[str, Any]:
        otp_status = "up"
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.post(
                    otp_url or OTP_URL,
                    json={"query": "{ __schema { queryType { name } } }"},
                )
                response.raise_for_status()
                if response.json().get("errors"):
                    otp_status = "down"
        except (httpx.HTTPError, ValueError):
            otp_status = "down"
        threshold = _stale_threshold(stale_threshold_s)
        min_veh = _min_live_vehicles(min_live_vehicles)
        state, age, abstention = _evaluate_live_feed(
            resolved_data_dir,
            stale_threshold_s=threshold,
            min_vehicles=min_veh,
        )
        replay_available = (resolved_data_dir / "passages").exists()
        paused = is_feed_paused(resolved_data_dir)
        if abstention is None:
            feed_status = "live"
        elif paused:
            feed_status = "live_paused"
        elif state and state.get("header_timestamp"):
            feed_status = "live_stale"
        elif replay_available:
            feed_status = "replay"
        else:
            feed_status = "unavailable"
        return {
            "status": "ok" if otp_status == "up" else "degraded",
            "service": "journeys-api",
            "otp": otp_status,
            "feed": feed_status,
            "feed_age_seconds": age,
            "stale_threshold_seconds": round(threshold, 1),
            "live_vehicle_count": state.get("vehicle_count") if state else 0,
            "live_fresh_vehicle_count": state.get("fresh_vehicle_count") if state else 0,
            "poller_enabled": poller_enabled,
            "poller_paused": paused,
            "replay_available": replay_available,
            "selected_rung": timing_model.selected_rung,
        }

    @app.get("/replay/demo")
    async def replay_demo() -> dict[str, Any]:
        return await build_marginal_transfer_demo(resolved_data_dir, otp_client)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    return app


app = create_app()

