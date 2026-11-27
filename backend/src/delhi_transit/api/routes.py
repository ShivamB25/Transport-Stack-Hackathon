"""Journey, live-feed, health and replay-demo routes with endpoint parsing and abstention."""
from __future__ import annotations

import csv
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query

from delhi_transit.api.replay_demo import build_marginal_transfer_demo
from delhi_transit.api.schemas import JourneyResponse, ResolvedEndpoint
from delhi_transit.core.config import (
    BBOX_LAT_MAX,
    BBOX_LAT_MIN,
    BBOX_LNG_MAX,
    BBOX_LNG_MIN,
    IST,
    live_min_vehicles,
    live_stale_threshold_s,
    min_route_coverage,
    runtime_mode,
)
from delhi_transit.live.freshness import (
    effective_entity_max_age_s,
    fresh_fleet_count,
    live_feed_age_seconds,
)
from delhi_transit.live.poller import (
    LivePoller,
    is_feed_paused,
    pause_feed,
    read_live_state,
    resume_feed,
)
from delhi_transit.planner.itinerary import retime_itinerary
from delhi_transit.planner.otp_client import OTPClientError
from delhi_transit.planner.provenance import (
    option_has_supported_route,
    option_ride_coverage,
)

DEFAULT_REPLAY_DEPART = datetime(2026, 10, 22, 8, 30, tzinfo=IST)


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


def _explicit_mode(requested_mode: str) -> str | None:
    if requested_mode != "auto":
        return requested_mode
    env_mode = runtime_mode()
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


def _abstain(code: str, reason: str, **detail: Any) -> dict[str, Any]:
    """The `abstention` body; the UI badge keys off `code`."""
    return {"label": "not enough data", "code": code, "reason": reason, **detail}


def _evaluate_live_feed(
    data_dir: Path,
    *,
    stale_threshold_s: float,
    min_vehicles: int,
) -> tuple[dict[str, Any] | None, float | None, dict[str, Any] | None]:
    state = read_live_state(data_dir)
    age = live_feed_age_seconds(state)
    threshold = round(stale_threshold_s, 1)
    if not state or state.get("header_timestamp") is None:
        return state, None, _abstain(
            "feed_unavailable",
            "Not enough data: live GPS feed unavailable (no VehiclePositions snapshot ingested).",
            feed_age_seconds=None,
            threshold_seconds=threshold,
        )
    if age is not None and age > stale_threshold_s:
        return state, age, _abstain(
            "stale_feed",
            f"Not enough data: live GPS feed is stale ({round(age, 1)}s old, threshold {threshold}s).",
            feed_age_seconds=round(age, 1),
            threshold_seconds=threshold,
            last_feed_timestamp=state.get("header_timestamp_iso"),
        )
    fresh_count = fresh_fleet_count(
        data_dir, now=time.time(), max_age_s=effective_entity_max_age_s(stale_threshold_s)
    )
    state = {**state, "fresh_vehicle_count": fresh_count}
    if fresh_count < min_vehicles:
        return state, age, _abstain(
            "insufficient_coverage",
            (
                f"Not enough data: live GPS fleet coverage is insufficient "
                f"({fresh_count} fresh vehicles, minimum {min_vehicles} required)."
            ),
            feed_age_seconds=round(age, 1) if age is not None else None,
            threshold_seconds=threshold,
            vehicle_count=int(state.get("vehicle_count", 0)),
            fresh_vehicle_count=fresh_count,
            min_vehicles=min_vehicles,
        )
    return state, age, None


def _option_passes_gate(option: dict[str, Any], coverage_floor: float) -> bool:
    return option_has_supported_route(option) and option_ride_coverage(option) >= coverage_floor


def _replay_feed_summary(replay_info: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "fixture_path",
        "loaded_passage_files",
        "replay_data_available",
        "same_day_data_available",
        "visible_observations",
        "same_day_observations",
        "observed_same_day_stretches",
    )
    return {**{key: replay_info[key] for key in keys}, "live_feed_contacted": False}


@dataclass(frozen=True)
class ApiRuntime:
    data_dir: Path
    resolver: StopResolver
    otp_client: Any
    otp_url: str
    timing_model: Any
    live_poller: LivePoller
    poller_enabled: bool
    stale_threshold_override: float | None = None
    min_vehicles_override: int | None = None
    min_route_coverage_override: float | None = None

    def stale_threshold(self) -> float:
        if self.stale_threshold_override is not None:
            return float(self.stale_threshold_override)
        return live_stale_threshold_s()

    def min_vehicles(self) -> int:
        if self.min_vehicles_override is not None:
            return int(self.min_vehicles_override)
        return live_min_vehicles()

    def coverage_floor(self, request_override: float | None) -> float:
        override = request_override if request_override is not None else self.min_route_coverage_override
        return float(override) if override is not None else min_route_coverage()

    def evaluate_live_feed(self) -> tuple[dict[str, Any] | None, float | None, dict[str, Any] | None]:
        return _evaluate_live_feed(
            self.data_dir,
            stale_threshold_s=self.stale_threshold(),
            min_vehicles=self.min_vehicles(),
        )


def build_router(rt: ApiRuntime) -> APIRouter:
    router = APIRouter()

    @router.get("/journeys", response_model=JourneyResponse)
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
        origin = rt.resolver.resolve(from_, "from")
        destination = rt.resolver.resolve(to, "to")
        departure, effective_mode = _resolve_departure_and_mode(depart, mode)
        if replay_delay_s and effective_mode != "replay":
            raise HTTPException(422, "replay_delay_s is only valid for October replay queries")

        now_iso = datetime.fromtimestamp(time.time(), IST).isoformat()
        threshold = rt.stale_threshold()
        coverage_floor = rt.coverage_floor(min_coverage)

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
                selected_rung=rt.timing_model.selected_rung,
                depart=departure.isoformat(),
                resolved_from=origin,
                resolved_to=destination,
                options=options or [],
                abstention=abstention,
            )

        if effective_mode == "live":
            state, feed_age, abstention = _evaluate_live_feed(
                rt.data_dir, stale_threshold_s=threshold, min_vehicles=rt.min_vehicles()
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
                    "paused": is_feed_paused(rt.data_dir),
                }
            if abstention is not None:
                return respond("insufficient_data", abstention=abstention)
        else:
            replay_info = rt.timing_model.replay_reference_info(departure)
            feed_age = replay_info["feed_age_seconds"]
            feed_timestamp = replay_info["feed_timestamp"]
            reference_timestamp = replay_info["reference_timestamp"]
            feed_summary = _replay_feed_summary(replay_info)
            if not replay_info["replay_data_available"]:
                return respond("insufficient_data", abstention=_abstain(
                    "replay_data_unavailable",
                    (
                        "Not enough data: no reconstructed October observations were loaded "
                        "before the requested replay cutoff."
                    ),
                    requested_cutoff=departure.isoformat(),
                    visible_observations=0,
                ))

        try:
            candidates = await rt.otp_client.plan(
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
                rt.timing_model,
                replay_delay_s,
                stale_threshold_s=threshold,
            )
            for candidate in candidates
            if any(leg.get("transitLeg") for leg in candidate.get("legs", []))
        ]
        if not options:
            raise HTTPException(404, "no bus itineraries found for this request")
        # Options that pass the support gate come first, so options[0] (what the UI and the
        # comparator show) is never an unsupported journey on an "ok" response.
        options.sort(
            key=lambda option: (
                not _option_passes_gate(option, coverage_floor),
                option["total_duration_s"],
            )
        )

        if not any(option_has_supported_route(option) for option in options):
            return respond("insufficient_data", abstention=_abstain(
                "insufficient_route_coverage",
                (
                    "Not enough data: no candidate journey has a supported route with "
                    "usable route-level observations."
                ),
                feed_age_seconds=feed_age,
                observed_coverage=0.0,
                candidate_options=len(options),
                supported_options=0,
            ))

        if coverage_floor > 0.0:
            best_cov = max(option_ride_coverage(option) for option in options)
            if best_cov < coverage_floor:
                return respond("insufficient_data", abstention=_abstain(
                    "insufficient_route_coverage",
                    (
                        f"Not enough data: observed route coverage ({best_cov:.2f}) "
                        f"is below required threshold ({coverage_floor:.2f})."
                    ),
                    feed_age_seconds=feed_age,
                    observed_coverage=best_cov,
                    min_coverage=coverage_floor,
                ))

        return respond("ok", options)

    @router.get("/live/status")
    async def live_status() -> dict[str, Any]:
        state, age, abstention = rt.evaluate_live_feed()
        return {
            "paused": is_feed_paused(rt.data_dir),
            "poller_enabled": rt.poller_enabled,
            "auth_configured": rt.live_poller.auth_configured,
            "current_feed_age_seconds": age,
            "stale_threshold_seconds": round(rt.stale_threshold(), 1),
            "min_vehicles": rt.min_vehicles(),
            "is_fresh": abstention is None,
            "abstention": abstention,
            "state": state,
        }

    @router.post("/live/pause")
    async def live_pause(reason: str = Query(default="manual_pause")) -> dict[str, Any]:
        info = pause_feed(rt.data_dir, reason=reason)
        state = read_live_state(rt.data_dir)
        return {
            **info,
            "current_feed_age_seconds": live_feed_age_seconds(state),
            "stale_threshold_seconds": round(rt.stale_threshold(), 1),
        }

    @router.post("/live/resume")
    async def live_resume(poll_now: bool = Query(default=True)) -> dict[str, Any]:
        info = resume_feed(rt.data_dir)
        poll_result: dict[str, Any] | None = None
        if poll_now and rt.live_poller.auth_configured:
            poll_result = await rt.live_poller.apoll_once()
        state = read_live_state(rt.data_dir)
        return {
            **info,
            "poll_result": poll_result,
            "current_feed_age_seconds": live_feed_age_seconds(state),
            "stale_threshold_seconds": round(rt.stale_threshold(), 1),
        }

    @router.get("/health")
    async def health() -> dict[str, Any]:
        otp_status = "up"
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.post(
                    rt.otp_url,
                    json={"query": "{ __schema { queryType { name } } }"},
                )
                response.raise_for_status()
                if response.json().get("errors"):
                    otp_status = "down"
        except (httpx.HTTPError, ValueError):
            otp_status = "down"
        threshold = rt.stale_threshold()
        state, age, abstention = rt.evaluate_live_feed()
        replay_available = (rt.data_dir / "passages").exists()
        paused = is_feed_paused(rt.data_dir)
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
            "poller_enabled": rt.poller_enabled,
            "poller_paused": paused,
            "replay_available": replay_available,
            "selected_rung": rt.timing_model.selected_rung,
        }

    @router.get("/replay/demo")
    async def replay_demo() -> dict[str, Any]:
        return await build_marginal_transfer_demo(rt.data_dir, rt.otp_client)

    return router
