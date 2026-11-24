"""Collect planner and Google observations, freeze them, and render the comparison from that snapshot."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from delhi_transit.comparator.client import (
    DEFAULT_LANGUAGE_CODE,
    GOOGLE_ROUTES_URL,
    GoogleBudgetExceededError,
    GoogleClientError,
    GoogleRoutesClient,
)
from delhi_transit.comparator.report import write_comparison_outputs
from delhi_transit.comparator.slots import (
    DEFAULT_OD_PAIRS,
    DEFAULT_TIME_SLOTS,
    ODPair,
    TimeSlot,
    validate_comparison_schedule,
)
from delhi_transit.core.config import IST, portable_path
from delhi_transit.core.time import utc_z
from delhi_transit.planner.provenance import option_has_supported_route


def query_our_api(
    client: httpx.Client,
    api_url: str,
    od: ODPair,
    depart: datetime,
) -> dict[str, Any]:
    """Query GET /journeys on our local FastAPI service for one OD and departure time."""
    url = f"{api_url.rstrip('/')}/journeys"
    params = {
        "from": f"{od.from_lat:.5f},{od.from_lng:.5f}",
        "to": f"{od.to_lat:.5f},{od.to_lng:.5f}",
        "depart": depart.isoformat(),
    }
    try:
        response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        return {"our_status": "error", "our_error": f"{type(exc).__name__}: {exc}"}

    if response.status_code != 200:
        return {
            "our_status": f"http_{response.status_code}",
            "our_error": response.text[:240],
        }

    body = response.json()
    status = body.get("status", "unknown")
    options = body.get("options") or []
    if status != "ok" or not options:
        abstention = body.get("abstention") or {}
        return {
            "our_status": status,
            "our_mode": body.get("mode"),
            "our_selected_rung": body.get("selected_rung"),
            "our_error": abstention.get("reason") or "no options returned",
        }

    # Compare the option the support gate accepts, never an unsupported one that merely
    # rides along with a supported alternative in the same response.
    supported = [index for index, option in enumerate(options) if option_has_supported_route(option)]
    if not supported:
        return {
            "our_status": "unsupported_options",
            "our_mode": body.get("mode"),
            "our_selected_rung": body.get("selected_rung"),
            "our_error": "status ok but no returned option has a supported route",
        }
    option_index = supported[0]
    top = options[option_index]
    legs = top.get("legs") or []
    ride_legs = [leg for leg in legs if leg.get("type") == "ride"]
    board_wait_s = sum(int(leg.get("estimated_duration_s") or 0) for leg in legs if leg.get("type") == "board")
    ride_walk_s = sum(
        int(leg.get("estimated_duration_s") or 0)
        for leg in legs
        if leg.get("type") in ("walk", "ride", "transfer")
    )
    routes = [str(leg.get("route_name") or leg.get("route_id") or "?") for leg in ride_legs]
    timing_sources = sorted({str(leg.get("timing_source") or leg.get("source") or "unknown") for leg in ride_legs})

    return {
        "our_status": "ok",
        "our_mode": body.get("mode"),
        "our_selected_rung": body.get("selected_rung"),
        "our_total_duration_s": int(top["total_duration_s"]),
        "our_window_p10_s": int(top["total_window_p10_s"]),
        "our_window_p90_s": int(top["total_window_p90_s"]),
        "our_board_wait_s": board_wait_s,
        "our_ride_walk_duration_s": ride_walk_s,
        "our_routes": routes,
        "our_transfers": max(0, len(ride_legs) - 1),
        "our_timing_sources": timing_sources,
        "our_ride_observed_coverage": [float(leg.get("observed_coverage") or 0.0) for leg in ride_legs],
        "our_option_index": option_index,
        "our_option_count": len(options),
        "our_error": None,
    }


# Inputs collected before per-ride coverage was frozen record only route names and timing
# sources. Stop passages exist only for 717A, so a ride there carries observed coverage in
# every non-timetable source; elsewhere only a model_live ride can (live vehicle share).
_LEGACY_OBSERVED_ROUTE_NAMES = frozenset({"717AUP", "717ADOWN"})


def reported_option_supported(our_res: dict[str, Any]) -> bool | None:
    """Whether the compared option passes the API support gate (None when nothing was compared)."""
    if our_res.get("our_status") != "ok":
        return None
    coverage = our_res.get("our_ride_observed_coverage")
    if coverage is not None:
        return any(float(value) > 0.0 for value in coverage)
    sources = set(our_res.get("our_timing_sources") or [])
    if "model_live" in sources:
        return True
    routes = set(our_res.get("our_routes") or [])
    return bool(routes & _LEGACY_OBSERVED_ROUTE_NAMES) and sources != {"timetable_fallback"}


def evaluate_slot_comparison(
    od: ODPair,
    slot: TimeSlot,
    depart: datetime,
    our_res: dict[str, Any],
    google_res: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Combine our API output and Google Routes bus-only output into a row + manifest entry."""
    parsed = google_res.get("parsed") or {}
    best_google = parsed.get("best_bus_route")
    google_http = google_res.get("http_status")
    google_err = google_res.get("error")
    unavailable_reason = google_res.get("unavailable_reason")

    if unavailable_reason:
        google_status = f"unavailable_{unavailable_reason}"
    elif google_err or google_http != 200:
        google_status = f"http_{google_http}" if google_http and google_http != 200 else "error"
    elif best_google is not None:
        google_status = "ok"
    elif int(parsed.get("raw_route_count") or 0) > 0:
        google_status = "no_bus_only_route"
    else:
        google_status = "no_routes"

    reported_supported = reported_option_supported(our_res)
    our_dur = our_res.get("our_total_duration_s") if reported_supported is not False else None
    google_dur = best_google.get("duration_s") if best_google else None

    note_parts: list[str] = []
    if our_dur is not None and google_dur is not None:
        row_status = "compared"
        diff_s = int(our_dur) - int(google_dur)
        diff_pct = round((diff_s / google_dur) * 100.0, 2) if google_dur > 0 else None
        abs_diff_s = abs(diff_s)
        our_rw = our_res.get("our_ride_walk_duration_s")
        diff_excl_board_s = int(our_rw) - int(google_dur) if our_rw is not None else None
        p10 = our_res.get("our_window_p10_s")
        p90 = our_res.get("our_window_p90_s")
        in_window = bool(p10 is not None and p90 is not None and p10 <= google_dur <= p90)
    else:
        diff_s = None
        diff_pct = None
        abs_diff_s = None
        diff_excl_board_s = None
        in_window = None
        if reported_supported is False:
            row_status = "our_unsupported_option"
        elif our_dur is None and google_dur is None:
            row_status = "both_missing"
        elif our_dur is None:
            row_status = f"our_{our_res.get('our_status', 'error')}"
        elif google_status == "no_bus_only_route":
            row_status = "google_no_bus_only_route"
        elif google_status == "no_routes":
            row_status = "google_no_routes"
        else:
            row_status = f"google_{google_status}"

    if our_res.get("our_error"):
        note_parts.append(f"our: {our_res['our_error']}")
    if reported_supported is False:
        note_parts.append(
            "our: reported option has no route-level observations (support gate); "
            "excluded from comparison"
        )
    if google_err:
        note_parts.append(f"google: {google_err}")
    elif google_status == "no_bus_only_route":
        vtypes = ",".join(parsed.get("excluded_vehicle_types") or [])
        note_parts.append(
            f"Google returned {parsed.get('raw_route_count', 0)} route(s) but 0 bus-only "
            f"(excluded: {vtypes or 'non-bus'})"
        )
    elif google_status == "no_routes":
        note_parts.append("Google returned 0 transit routes")
    elif int(parsed.get("excluded_non_bus_count") or 0) > 0:
        vtypes = ",".join(parsed.get("excluded_vehicle_types") or [])
        note_parts.append(
            f"filtered {parsed['excluded_non_bus_count']} non-bus Google itinerary(ies) ({vtypes})"
        )

    depart_ist = depart.astimezone(IST).isoformat()
    depart_utc = utc_z(depart)

    row = {
        "od_id": od.od_id,
        "group": od.group,
        "label": od.label,
        "slot_id": slot.slot_id,
        "slot_label": slot.label,
        "departure_time_ist": depart_ist,
        "departure_time_utc": depart_utc,
        "row_status": row_status,
        "our_status": our_res.get("our_status"),
        "our_mode": our_res.get("our_mode") or "",
        "our_selected_rung": our_res.get("our_selected_rung") or "",
        "our_total_duration_s": our_dur,
        "our_window_p10_s": our_res.get("our_window_p10_s"),
        "our_window_p90_s": our_res.get("our_window_p90_s"),
        "our_board_wait_s": our_res.get("our_board_wait_s"),
        "our_ride_walk_duration_s": our_res.get("our_ride_walk_duration_s"),
        "our_routes": " + ".join(our_res.get("our_routes") or []),
        "our_transfers": our_res.get("our_transfers"),
        "our_timing_sources": ",".join(our_res.get("our_timing_sources") or []),
        "our_option_index": our_res.get("our_option_index"),
        "our_option_supported": reported_supported,
        "google_status": google_status,
        "google_http_status": google_http,
        "google_duration_s": google_dur,
        "google_distance_m": best_google.get("distance_m") if best_google else None,
        "google_routes": " + ".join(best_google.get("route_names") or []) if best_google else "",
        "google_transfers": best_google.get("transfers") if best_google else None,
        "google_raw_route_count": int(parsed.get("raw_route_count") or 0),
        "google_bus_only_count": int(parsed.get("bus_only_count") or 0),
        "google_excluded_non_bus_count": int(parsed.get("excluded_non_bus_count") or 0),
        "google_excluded_vehicle_types": ",".join(parsed.get("excluded_vehicle_types") or []),
        "diff_s": diff_s,
        "diff_pct": diff_pct,
        "abs_diff_s": abs_diff_s,
        "diff_excl_board_s": diff_excl_board_s,
        "google_in_our_p10_p90": in_window,
        "google_from_cache": bool(google_res.get("from_cache")),
        "google_cache_key": google_res.get("cache_key") or "",
        "note": "; ".join(note_parts),
    }

    manifest_entry = {
        "od_id": od.od_id,
        "group": od.group,
        "label": od.label,
        "origin": {"name": od.from_name, "lat": od.from_lat, "lng": od.from_lng},
        "destination": {"name": od.to_name, "lat": od.to_lat, "lng": od.to_lng},
        "slot_id": slot.slot_id,
        "slot_label": slot.label,
        "departure_time_ist": depart_ist,
        "departure_time_utc": depart_utc,
        "language_code": (google_res.get("request") or {}).get("languageCode", DEFAULT_LANGUAGE_CODE),
        "travel_mode": (google_res.get("request") or {}).get("travelMode", "TRANSIT"),
        "allowed_travel_modes": (
            (google_res.get("request") or {})
            .get("transitPreferences", {})
            .get("allowedTravelModes", ["BUS"])
        ),
        "cache_key": google_res.get("cache_key"),
        "cache_path": portable_path(google_res.get("cache_path")),
        "fetched_at": google_res.get("fetched_at"),
        "from_cache": bool(google_res.get("from_cache")),
        "http_status": google_http,
        "google_status": google_status,
        "google_raw_route_count": int(parsed.get("raw_route_count") or 0),
        "google_bus_only_count": int(parsed.get("bus_only_count") or 0),
        "google_excluded_non_bus_count": int(parsed.get("excluded_non_bus_count") or 0),
        "google_excluded_vehicle_types": parsed.get("excluded_vehicle_types") or [],
        "google_vehicle_types_compared": best_google.get("vehicle_types", []) if best_google else [],
        "our_status": our_res.get("our_status"),
        "our_option_supported": reported_supported,
        "row_status": row_status,
        "note": "; ".join(note_parts),
    }
    return row, manifest_entry


def _freeze_google_response(google_res: dict[str, Any]) -> dict[str, Any]:
    """Keep only renderer inputs, excluding volatile accounting and unused route details."""
    parsed = google_res.get("parsed") or {}
    best = parsed.get("best_bus_route")
    best_keys = ("duration_s", "distance_m", "route_names", "transfers", "vehicle_types")
    frozen = {
        key: google_res[key]
        for key in (
            "cache_key",
            "cache_path",
            "from_cache",
            "fetched_at",
            "http_status",
            "error",
            "unavailable_reason",
            "request",
            "field_mask",
        )
        if key in google_res
    }
    if "cache_path" in frozen:
        frozen["cache_path"] = portable_path(frozen["cache_path"])
    frozen["parsed"] = {
        key: parsed.get(key)
        for key in (
            "raw_route_count",
            "bus_only_count",
            "excluded_non_bus_count",
            "excluded_vehicle_types",
        )
    }
    frozen["parsed"]["best_bus_route"] = (
        {key: best.get(key) for key in best_keys} if best is not None else None
    )
    return frozen


def collect_comparison_inputs(
    *,
    target_date: date,
    api_url: str,
    google_client: GoogleRoutesClient,
    od_pairs: tuple[ODPair, ...] = DEFAULT_OD_PAIRS,
    time_slots: tuple[TimeSlot, ...] = DEFAULT_TIME_SLOTS,
    cache_only: bool = False,
    api_transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect planner and Google observations without rendering any reports."""
    slot_datetimes = validate_comparison_schedule(
        target_date,
        time_slots,
        now=now,
        cache_only=cache_only,
    )
    collected_slots: list[dict[str, Any]] = []
    cache_hits_this_run = 0
    network_calls_this_run = 0

    with httpx.Client(timeout=45.0, transport=api_transport) as api_client:
        for od in od_pairs:
            for slot, depart in slot_datetimes:
                our_res = query_our_api(api_client, api_url, od, depart)
                try:
                    google_res = google_client.compute_bus_routes(
                        (od.from_lat, od.from_lng),
                        (od.to_lat, od.to_lng),
                        depart,
                        od_id=od.od_id,
                        slot_label=slot.slot_id,
                        now=now,
                        cache_only=cache_only,
                    )
                except GoogleClientError as exc:
                    if isinstance(exc, GoogleBudgetExceededError):
                        reason = "quota_exhausted"
                    else:
                        reason = "cache_miss" if cache_only else "client_error"
                    google_res = google_client.unavailable_response(
                        (od.from_lat, od.from_lng),
                        (od.to_lat, od.to_lng),
                        depart,
                        error=str(exc),
                        reason=reason,
                    )
                if google_res["from_cache"]:
                    cache_hits_this_run += 1
                elif not google_res.get("unavailable_reason"):
                    network_calls_this_run += 1
                collected_slots.append(
                    {
                        "od": asdict(od),
                        "slot": asdict(slot),
                        "departure_time": depart.isoformat(),
                        "our": our_res,
                        "google": _freeze_google_response(google_res),
                    }
                )

    counter = google_client.read_call_counter()
    return {
        "schema_version": 1,
        "collected_at": utc_z(),
        "target_date_ist": target_date.isoformat(),
        "target_weekday": target_date.strftime("%A"),
        "matching_october_weekday_note": (
            "Upcoming matching weekday used because Google Routes TRANSIT returns zero routes "
            "for past/October 2026 dates (verified in feasibility probe)."
        ),
        "time_slots": [f"{s.slot_id} ({s.label})" for s in time_slots],
        "api_url": api_url,
        "google_endpoint": GOOGLE_ROUTES_URL,
        "google_field_mask": google_client.field_mask,
        "language_code": DEFAULT_LANGUAGE_CODE,
        "travel_mode": "TRANSIT",
        "allowed_travel_modes": ["BUS"],
        "bus_only_filter_applied": True,
        "cache_dir": portable_path(google_client.responses_dir),
        "call_counter_path": portable_path(google_client.counter_path),
        "scratch_manifest_path": portable_path(google_client.manifest_path),
        "call_accounting": {
            "hard_cap": counter["hard_cap"],
            "prior_mission_calls": counter["prior_mission_calls"],
            "prior_mission_note": counter.get("prior_mission_note", ""),
            "api_calls_made": counter["api_calls_made"],
            "total_mission_calls": counter["total_mission_calls"],
            "remaining_budget": counter["remaining_budget"],
            "cache_hits_this_run": cache_hits_this_run,
            "network_calls_this_run": network_calls_this_run,
        },
        "slots": collected_slots,
    }


def _render_frozen_inputs(
    inputs: dict[str, Any],
    *,
    inputs_path: Path,
    report_dir: Path,
    scratch_manifest_path: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    manifest_entries: list[dict[str, Any]] = []
    for collected in inputs["slots"]:
        od = ODPair(**collected["od"])
        slot = TimeSlot(**collected["slot"])
        depart = datetime.fromisoformat(collected["departure_time"])
        row, entry = evaluate_slot_comparison(
            od,
            slot,
            depart,
            collected["our"],
            collected["google"],
        )
        rows.append(row)
        manifest_entries.append(entry)

    compared_count = sum(1 for row in rows if row["row_status"] == "compared")
    non_bus_filtered = sum(int(row["google_excluded_non_bus_count"] or 0) for row in rows)
    od_pair_count = len({row["od_id"] for row in rows})
    slot_count = len({row["slot_id"] for row in rows})
    manifest = {
        key: inputs[key]
        for key in (
            "target_date_ist",
            "target_weekday",
            "matching_october_weekday_note",
            "time_slots",
            "api_url",
            "google_endpoint",
            "google_field_mask",
            "language_code",
            "travel_mode",
            "allowed_travel_modes",
            "bus_only_filter_applied",
            "cache_dir",
            "call_counter_path",
            "scratch_manifest_path",
            "call_accounting",
        )
    }
    manifest.update(
        {
            "generated_at": inputs["collected_at"],
            "rendered_from_frozen_inputs": True,
            "frozen_inputs_path": portable_path(inputs_path),
            "corpus_summary": {
                "od_pair_count": od_pair_count,
                "time_slots_per_pair": slot_count,
                "planned_slots": len(rows),
                "compared_rows": compared_count,
                "missing_or_error_rows": len(rows) - compared_count,
                "google_non_bus_itineraries_filtered": non_bus_filtered,
            },
            "entries": manifest_entries,
        }
    )
    write_comparison_outputs(rows, manifest, report_dir, scratch_manifest_path)
    return rows, manifest


def render_comparison(
    *,
    inputs_path: Path,
    report_dir: Path,
    scratch_manifest_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Render reports exclusively from committed frozen inputs, with no network clients."""
    inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    return _render_frozen_inputs(
        inputs,
        inputs_path=inputs_path,
        report_dir=report_dir,
        scratch_manifest_path=scratch_manifest_path,
    )


def run_comparison(
    *,
    target_date: date,
    api_url: str,
    google_client: GoogleRoutesClient,
    report_dir: Path,
    od_pairs: tuple[ODPair, ...] = DEFAULT_OD_PAIRS,
    time_slots: tuple[TimeSlot, ...] = DEFAULT_TIME_SLOTS,
    cache_only: bool = False,
    api_transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    inputs_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect fresh inputs, freeze them, then render the reports from that snapshot."""
    inputs = collect_comparison_inputs(
        target_date=target_date,
        api_url=api_url,
        google_client=google_client,
        od_pairs=od_pairs,
        time_slots=time_slots,
        cache_only=cache_only,
        api_transport=api_transport,
        now=now,
    )
    frozen_path = inputs_path or (report_dir / "google-comparator-inputs.json")
    frozen_path.parent.mkdir(parents=True, exist_ok=True)
    frozen_path.write_text(json.dumps(inputs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return _render_frozen_inputs(
        inputs,
        inputs_path=frozen_path,
        report_dir=report_dir,
        scratch_manifest_path=google_client.manifest_path,
    )
