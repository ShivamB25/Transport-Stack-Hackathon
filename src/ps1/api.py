"""FastAPI journey planning surface."""
from __future__ import annotations

import csv
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from ps1.config import (
    BBOX_LAT_MAX,
    BBOX_LAT_MIN,
    BBOX_LNG_MAX,
    BBOX_LNG_MIN,
    DATA_DIR,
    OTP_URL,
)
from ps1.otp_client import OTPClient, OTPClientError
from ps1.retime import RetimeModel, retime_itinerary


class Leg(BaseModel):
    type: str
    route_id: int | None = None
    route_name: str | None = None
    from_stop: str | None = None
    to_stop: str | None = None
    from_stop_id: str | None = None
    to_stop_id: str | None = None
    scheduled_duration_s: int
    estimated_duration_s: int
    window_p10_s: int | None = None
    window_p90_s: int | None = None
    source: str
    observed_coverage: float | None = Field(default=None, ge=0, le=1)
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
        key = _normalise(value)
        stop_id = self.aliases.get(key)
        if stop_id is None:
            matches = self.names.get(key, [])
            if not matches:
                raise HTTPException(404, f"unknown stop name for {field}: {value}")
            stop_id = min(matches)
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


def _parse_depart(value: str) -> datetime:
    try:
        depart = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, "depart must be an ISO 8601 date-time with an offset") from exc
    if depart.tzinfo is None or depart.utcoffset() is None:
        raise HTTPException(422, "depart must include a timezone offset")
    return depart


def _feed_age(data_dir: Path, mode: str, depart: datetime) -> float | None:
    if mode == "replay":
        return 0.0
    state_path = data_dir / "live" / "feed_state.json"
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text())
        timestamp = float(state.get("timestamp", state.get("feed_timestamp")))
        reference = depart.timestamp() if state.get("replay") else time.time()
        return max(0.0, reference - timestamp)
    except (OSError, ValueError, TypeError):
        return None


def create_app(
    data_dir: Path | None = None,
    otp_url: str | None = None,
    otp: Any | None = None,
    model: Any | None = None,
) -> FastAPI:
    data_dir = Path(data_dir or DATA_DIR)
    resolver = StopResolver(data_dir / "gtfs" / "stops.txt")
    otp_client = otp or OTPClient(otp_url or OTP_URL)
    timing_model = model or RetimeModel(data_dir)
    app = FastAPI(
        title="Delhi Model-Timed Journeys",
        version="1.0.0",
        description="OTP bus candidates re-timed from causal GPS replay with joint journey windows.",
    )

    @app.get("/journeys", response_model=JourneyResponse)
    async def journeys(
        from_: str = Query(alias="from", min_length=1),
        to: str = Query(min_length=1),
        depart: str = Query(min_length=1),
        replay_delay_s: float = Query(default=0, ge=0, le=3600),
    ) -> JourneyResponse:
        origin = resolver.resolve(from_, "from")
        destination = resolver.resolve(to, "to")
        departure = _parse_depart(depart)
        mode = "replay" if departure.date().isoformat().startswith("2026-10-") else "live"
        if mode == "replay" and departure.year == 2026 and departure.month != 10:
            raise HTTPException(422, "replay departures must be in October 2026")
        if replay_delay_s and mode != "replay":
            raise HTTPException(422, "replay_delay_s is only valid for October replay queries")
        try:
            candidates = await otp_client.plan(
                (origin.routing_lat, origin.routing_lng),
                (destination.routing_lat, destination.routing_lng),
                departure,
            )
        except OTPClientError as exc:
            raise HTTPException(502, str(exc)) from exc
        options = [
            retime_itinerary(candidate, departure, mode, timing_model, replay_delay_s)
            for candidate in candidates
            if any(leg.get("transitLeg") for leg in candidate.get("legs", []))
        ]
        if not options:
            raise HTTPException(404, "no bus itineraries found for this request")
        options.sort(key=lambda option: option["total_duration_s"])
        return JourneyResponse(
            status="ok",
            mode=mode,
            feed_age_seconds=_feed_age(data_dir, mode, departure),
            selected_rung=timing_model.selected_rung,
            depart=departure.isoformat(),
            resolved_from=origin,
            resolved_to=destination,
            options=options,
        )

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
        return {
            "status": "ok" if otp_status == "up" else "degraded",
            "service": "journeys-api",
            "otp": otp_status,
            "feed": "replay" if (data_dir / "passages").exists() else "unavailable",
            "selected_rung": timing_model.selected_rung,
        }

    return app


app = create_app()
