"""Re-time one OTP itinerary: walks from OTP, waits and rides from the model, joint-scenario windows."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from delhi_transit.core.config import IST
from delhi_transit.planner.provenance import leg_provenance
from delhi_transit.planner.rides import RetimeModel
from delhi_transit.planner.risk import (
    SCENARIOS,
    scenario_windows,
    simulate_joint_scenarios,
)


def place_stop_id(place: dict[str, Any] | None, *, fallback_to_name: bool = True) -> str:
    """GTFS stop id of an OTP place without the feed prefix (optionally falling back to its name)."""
    place = place or {}
    stop = place.get("stop") or {}
    value = stop.get("gtfsId") or (place.get("name") if fallback_to_name else None) or ""
    return str(value).split(":")[-1]


def _route_id(leg: dict[str, Any]) -> int:
    value = str((leg.get("route") or {}).get("gtfsId") or "0").split(":")[-1]
    return int(value) if value.isdigit() else 0


def _seconds_between(start: str, end: str) -> float:
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


def _base_leg(kind: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw or {}
    route = raw.get("route") or {}
    from_place = raw.get("from") or {}
    to_place = raw.get("to") or {}
    return {
        "type": kind,
        "route_id": _route_id(raw) if route else None,
        "route_name": route.get("shortName") or route.get("longName") if route else None,
        "from_stop": from_place.get("name"),
        "to_stop": to_place.get("name"),
        "from_stop_id": place_stop_id(from_place) or None,
        "to_stop_id": place_stop_id(to_place) or None,
        "from_lat": from_place.get("lat"),
        "from_lng": from_place.get("lon"),
        "to_lat": to_place.get("lat"),
        "to_lng": to_place.get("lon"),
        "scheduled_duration_s": 0,
        "estimated_duration_s": 0,
        "window_p10_s": 0,
        "window_p90_s": 0,
        "source": "timetable",
        "timing_source": "otp_walk" if kind == "walk" else "timetable_fallback",
        "observed_coverage": None,
        "observation_count": None,
        "coverage_note": "walk leg from OTP street graph" if kind == "walk" else None,
        "geometry": (raw.get("legGeometry") or {}).get("points"),
    }


def _fingerprint(itinerary: dict[str, Any]) -> str:
    identity = [
        {
            "trip": (leg.get("trip") or {}).get("gtfsId"),
            "from": place_stop_id(leg.get("from")),
            "to": place_stop_id(leg.get("to")),
            "geometry": (leg.get("legGeometry") or {}).get("points"),
        }
        for leg in itinerary.get("legs", [])
    ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]


def _walk_seconds(legs: list[dict[str, Any]]) -> float:
    return sum(float(leg.get("duration", 0)) for leg in legs if not leg.get("transitLeg"))


def _timed_events(
    raw_legs: list[dict[str, Any]],
    depart: datetime,
    mode: str,
    model: RetimeModel,
    replay_delay_s: float,
    stale_threshold_s: float | None,
) -> list[dict[str, Any]]:
    """One event per output leg, each carrying the estimate the joint scenarios draw from."""
    first_transit = True
    events: list[dict[str, Any]] = []
    previous_transit: dict[str, Any] | None = None
    previous_transit_index = -1
    for raw_index, raw in enumerate(raw_legs):
        if not raw.get("transitLeg"):
            leg = _base_leg("walk", raw)
            duration = float(raw.get("duration", 0))
            leg.update(scheduled_duration_s=round(duration), estimated_duration_s=round(duration))
            events.append({"leg": leg, "kind": "fixed", "duration": duration})
            continue
        scheduled_start = raw["start"]["scheduledTime"]
        scheduled_end = raw["end"]["scheduledTime"]
        transit_depart = datetime.fromisoformat(scheduled_start).astimezone(IST)
        route_id = _route_id(raw)
        from_stop = place_stop_id(raw.get("from"))
        to_stop = place_stop_id(raw.get("to"))
        scheduled_ride = float(raw.get("duration", _seconds_between(scheduled_start, scheduled_end)))
        if previous_transit is None:
            initial_walk_s = _walk_seconds(raw_legs[:raw_index])
            scheduled_wait = max(0.0, (transit_depart - depart).total_seconds() - initial_walk_s)
        else:
            scheduled_gap = _seconds_between(previous_transit["end"]["scheduledTime"], scheduled_start)
            transfer_walk_s = _walk_seconds(raw_legs[previous_transit_index + 1:raw_index])
            scheduled_wait = max(0.0, scheduled_gap - transfer_walk_s)
        wait = model.predict_wait(
            route_id,
            from_stop,
            scheduled_wait,
            transit_depart,
            mode,
            as_of=depart,
            stale_threshold_s=stale_threshold_s,
        )
        if previous_transit is not None:
            previous_place = previous_transit.get("to") or {}
            next_place = raw.get("from") or {}
            transfer = _base_leg("transfer", raw)
            transfer.update(
                from_stop=previous_place.get("name"),
                from_stop_id=place_stop_id(previous_place) or None,
                from_lat=previous_place.get("lat"),
                from_lng=previous_place.get("lon"),
                to_stop=next_place.get("name"),
                to_stop_id=place_stop_id(next_place) or None,
                to_lat=next_place.get("lat"),
                to_lng=next_place.get("lon"),
                geometry=None,
                scheduled_duration_s=round(scheduled_wait),
                **leg_provenance(wait),
            )
            events.append({
                "leg": transfer,
                "kind": "transfer",
                "scheduled_wait": scheduled_wait,
                "connection_departure_s": (
                    (transit_depart - depart).total_seconds() + wait.departure_delay_s
                ),
                "wait": wait,
            })
        board = _base_leg("board", raw)
        board_duration = wait.duration_s if first_transit else 0.0
        board.update(
            scheduled_duration_s=round(scheduled_wait if first_transit else 0),
            estimated_duration_s=round(board_duration),
            **leg_provenance(wait),
        )
        events.append({
            "leg": board,
            "kind": "first_wait" if first_transit else "fixed",
            "duration": board_duration,
            "wait": wait,
        })
        injected = replay_delay_s if first_transit else 0.0
        estimate = model.predict_ride(
            route_id,
            from_stop,
            to_stop,
            scheduled_ride,
            depart if first_transit else transit_depart,
            mode,
            injected,
            as_of=depart,
            stale_threshold_s=stale_threshold_s,
        )
        ride = _base_leg("ride", raw)
        ride.update(
            scheduled_duration_s=round(scheduled_ride),
            estimated_duration_s=round(estimate.duration_s),
            **leg_provenance(estimate),
        )
        events.append({"leg": ride, "kind": "ride", "estimate": estimate, "delay": injected})
        alight = _base_leg("alight", raw)
        alight.update(**leg_provenance(estimate))
        events.append({"leg": alight, "kind": "fixed", "duration": 0.0})
        previous_transit = raw
        previous_transit_index = raw_index
        first_transit = False
    return events


def _quieter_hour_total(
    events: list[dict[str, Any]],
    depart: datetime,
    mode: str,
    model: RetimeModel,
    stale_threshold_s: float | None,
) -> float:
    """The same journey with rides timed at 23:00, using the request's observation cutoff."""
    quiet_total = 0.0
    quiet_depart = depart.replace(hour=23, minute=0, second=0, microsecond=0)
    for event in events:
        if event["kind"] == "ride":
            quiet = model.predict_ride(
                event["leg"]["route_id"],
                event["leg"]["from_stop_id"],
                event["leg"]["to_stop_id"],
                event["leg"]["scheduled_duration_s"],
                quiet_depart,
                mode,
                as_of=depart,
                stale_threshold_s=stale_threshold_s,
            )
            quiet_total += quiet.duration_s
        elif event["kind"] == "transfer":
            quiet_total += event["scheduled_wait"]
        elif event["kind"] != "first_wait":
            quiet_total += float(event["leg"]["estimated_duration_s"])
        else:
            quiet_total += event["wait"].duration_s
    return quiet_total


def retime_itinerary(
    itinerary: dict[str, Any],
    depart: datetime,
    mode: str,
    model: RetimeModel,
    replay_delay_s: float = 0.0,
    *,
    stale_threshold_s: float | None = None,
) -> dict[str, Any]:
    """Replace OTP timing and produce correlated, transfer-aware scenarios."""
    depart = depart.astimezone(IST)
    raw_legs = itinerary.get("legs", [])
    if not any(leg.get("transitLeg") for leg in raw_legs):
        raise ValueError("candidate has no transit legs")
    events = _timed_events(raw_legs, depart, mode, model, replay_delay_s, stale_threshold_s)
    scenarios, missed = simulate_joint_scenarios(events)
    windows = scenario_windows(scenarios)
    output_legs = []
    for index, event in enumerate(events):
        leg = event["leg"]
        summary = windows["legs"][index]
        leg.update(
            estimated_duration_s=round(summary["median_s"]),
            window_p10_s=round(summary["window_p10_s"]),
            window_p90_s=round(summary["window_p90_s"]),
        )
        if event["kind"] == "transfer":
            leg["connection_missed_probability"] = round(missed[index] / SCENARIOS, 4)
            leg["next_bus_headway_s"] = round(event["wait"].headway_s)
        output_legs.append(leg)

    quiet_total = _quieter_hour_total(events, depart, mode, model, stale_threshold_s)
    return {
        "candidate_id": _fingerprint(itinerary),
        "legs": output_legs,
        "total_duration_s": round(windows["total_duration_s"]),
        "total_window_p10_s": round(windows["total_window_p10_s"]),
        "total_window_p90_s": round(windows["total_window_p90_s"]),
        "quieter_hour_typical_s": round(quiet_total),
        "scenario_count": SCENARIOS,
    }
