"""Response models of the journeys API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
