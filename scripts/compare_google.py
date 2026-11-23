#!/usr/bin/env python3
"""Collect and compare our model-timed bus journeys against Google Routes API (bus-only)."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ps1.config import DATA_DIR, GOOGLE_DIR, GOOGLE_HARD_CALL_CAP
from ps1.google_client import (
    DEFAULT_FIELD_MASK,
    DEFAULT_LANGUAGE_CODE,
    GOOGLE_ROUTES_URL,
    IST,
    GoogleRoutesClient,
    upcoming_weekday_date,
    validate_departure_time,
)

TARGET_WEEKDAY_TUESDAY = 1  # Matches held-out Tuesday 2026-10-27 and brief example Tuesday 08:30


@dataclass(frozen=True)
class ODPair:
    od_id: str
    group: str
    label: str
    from_name: str
    from_lat: float
    from_lng: float
    to_name: str
    to_lat: float
    to_lng: float


@dataclass(frozen=True)
class TimeSlot:
    slot_id: str
    label: str
    hour: int
    minute: int


DEFAULT_TIME_SLOTS: tuple[TimeSlot, ...] = (
    TimeSlot("morning_peak", "08:30 IST", 8, 30),
    TimeSlot("midday", "14:00 IST", 14, 0),
    TimeSlot("evening_peak", "18:30 IST", 18, 30),
)

DEFAULT_OD_PAIRS: tuple[ODPair, ...] = (
    # 1. Brief example
    ODPair(
        "od_01",
        "brief_example",
        "Kashmere Gate ISBT -> Nehru Place",
        "Kashmere Gate ISBT",
        28.66750,
        77.22820,
        "Nehru Place",
        28.54940,
        77.25100,
    ),
    # 2–6. 717A UP corridor (route 2171: Badarpur Border -> Kapashera Border)
    ODPair(
        "od_02",
        "corridor_717a_up",
        "Badarpur Border -> Kapashera Border",
        "Badarpur Border (T)",
        28.49313,
        77.30373,
        "Kapashera Border",
        28.52143,
        77.08203,
    ),
    ODPair(
        "od_03",
        "corridor_717a_up",
        "Badarpur Border -> Lado Sarai Crossing",
        "Badarpur Border (T)",
        28.49313,
        77.30373,
        "Lado Sarai Crossing",
        28.52283,
        77.19190,
    ),
    ODPair(
        "od_04",
        "corridor_717a_up",
        "Air Force Station -> Vasant Kunj Sec A",
        "Air Force Station",
        28.51140,
        77.25648,
        "Vasant Kunj Sec A Pkt B/C",
        28.51034,
        77.16976,
    ),
    ODPair(
        "od_05",
        "corridor_717a_up",
        "Ambedkar Nagar Depot -> Masood Pur Village",
        "Ambedkar Nagar Depot",
        28.51518,
        77.22819,
        "Masood Pur Village",
        28.52548,
        77.15433,
    ),
    ODPair(
        "od_06",
        "corridor_717a_up",
        "Maidan Garhi Crossing -> Rajokari Xing",
        "Maidan Garhi Crossing (SDM Court)",
        28.51907,
        77.20880,
        "Rajokari Xing",
        28.53492,
        77.11141,
    ),
    # 7–11. 717A DOWN corridor (route 2170: Kapashera Border -> Badarpur Border)
    ODPair(
        "od_07",
        "corridor_717a_down",
        "Kapashera Border -> Badarpur Border",
        "Kapashera Border",
        28.52143,
        77.08203,
        "Badarpur Border (T)",
        28.49313,
        77.30373,
    ),
    ODPair(
        "od_08",
        "corridor_717a_down",
        "Kapashera Border -> Maidan Garhi Crossing",
        "Kapashera Border",
        28.52143,
        77.08203,
        "Maidan Garhi Crossing (SDM Court)",
        28.51947,
        77.20777,
    ),
    ODPair(
        "od_09",
        "corridor_717a_down",
        "Shiv Murti NH8 -> Ambedkar Nagar Depot",
        "Shiv Murti 2 (NH8)",
        28.53520,
        77.10868,
        "Ambedkar Nagar Depot",
        28.51527,
        77.22840,
    ),
    ODPair(
        "od_10",
        "corridor_717a_down",
        "Vasant Kunj Sec-A -> Air Force Station",
        "Vasant Kunj Sec-A / LBS",
        28.51165,
        77.16678,
        "Air Force Station",
        28.51168,
        77.25547,
    ),
    ODPair(
        "od_11",
        "corridor_717a_down",
        "Ahinsa Sthal -> Lal Kuan",
        "Ahinsa Sthal",
        28.52044,
        77.18940,
        "Lal Kuan",
        28.50252,
        77.28455,
    ),
    # 12–30. Diverse Delhi city-wide bus corridors
    ODPair(
        "od_12",
        "city_wide",
        "Azadpur Terminal -> Kashmere Gate ISBT",
        "Azadpur Terminal",
        28.70768,
        77.17816,
        "Kashmere Gate ISBT",
        28.66750,
        77.22820,
    ),
    ODPair(
        "od_13",
        "city_wide",
        "Kashmere Gate ISBT -> AIIMS / Safdarjung",
        "Kashmere Gate ISBT",
        28.66750,
        77.22820,
        "Safdarjung Hospital / AIIMS",
        28.56725,
        77.20773,
    ),
    ODPair(
        "od_14",
        "city_wide",
        "Shivaji Stadium -> Nehru Place",
        "Shivaji Stadium Terminal",
        28.63306,
        77.21546,
        "Nehru Place",
        28.54940,
        77.25100,
    ),
    ODPair(
        "od_15",
        "city_wide",
        "Anand Vihar ISBT -> ITO",
        "Anand Vihar ISBT Main Road",
        28.64701,
        77.31796,
        "ITO BSZ Marg",
        28.62909,
        77.24142,
    ),
    ODPair(
        "od_16",
        "city_wide",
        "Anand Vihar ISBT -> Kashmere Gate ISBT",
        "Anand Vihar ISBT Main Road",
        28.64701,
        77.31796,
        "Kashmere Gate ISBT",
        28.66750,
        77.22820,
    ),
    ODPair(
        "od_17",
        "city_wide",
        "Sarai Kale Khan ISBT -> Dhaula Kuan",
        "ISBT Sarai Kale Khan",
        28.59087,
        77.25817,
        "ARSD College (Dhaula Kuan)",
        28.59077,
        77.16838,
    ),
    ODPair(
        "od_18",
        "city_wide",
        "Dhaula Kuan -> AIIMS / Safdarjung",
        "ARSD College (Dhaula Kuan)",
        28.59077,
        77.16838,
        "Safdarjung Hospital / AIIMS",
        28.56725,
        77.20773,
    ),
    ODPair(
        "od_19",
        "city_wide",
        "Karol Bagh Terminal -> Lajpat Nagar",
        "Karol Bagh Terminal",
        28.64837,
        77.20066,
        "Lajpat Nagar Crossing",
        28.56418,
        77.25037,
    ),
    ODPair(
        "od_20",
        "city_wide",
        "Uttam Nagar Terminal -> Shivaji Stadium",
        "Uttam Nagar Terminal",
        28.62547,
        77.06628,
        "Shivaji Stadium Terminal",
        28.63306,
        77.21546,
    ),
    ODPair(
        "od_21",
        "city_wide",
        "Uttam Nagar Terminal -> Dhaula Kuan",
        "Uttam Nagar Terminal",
        28.62547,
        77.06628,
        "ARSD College (Dhaula Kuan)",
        28.59077,
        77.16838,
    ),
    ODPair(
        "od_22",
        "city_wide",
        "New Delhi Railway Station -> Hauz Khas",
        "New Delhi Railway Station Gate 2",
        28.64177,
        77.22359,
        "Hauz Khas",
        28.55300,
        77.20490,
    ),
    ODPair(
        "od_23",
        "city_wide",
        "Mehrauli Terminal -> AIIMS / Safdarjung",
        "Mehrauli Terminal",
        28.52132,
        77.18305,
        "Safdarjung Hospital / AIIMS",
        28.56725,
        77.20773,
    ),
    ODPair(
        "od_24",
        "city_wide",
        "Punjabi Bagh Terminal -> Karol Bagh Terminal",
        "Punjabi Bagh Terminal",
        28.67548,
        77.14046,
        "Karol Bagh Terminal",
        28.64837,
        77.20066,
    ),
    ODPair(
        "od_25",
        "city_wide",
        "Shahdara -> Red Fort",
        "Shahdara (Shyam Lal College)",
        28.67367,
        77.28359,
        "Red Fort",
        28.65801,
        77.23696,
    ),
    ODPair(
        "od_26",
        "city_wide",
        "Lajpat Nagar -> Badarpur Border",
        "Lajpat Nagar Crossing",
        28.56418,
        77.25037,
        "Badarpur Border (T)",
        28.49313,
        77.30373,
    ),
    ODPair(
        "od_27",
        "city_wide",
        "ITO -> Nehru Place",
        "ITO BSZ Marg",
        28.62909,
        77.24142,
        "Nehru Place",
        28.54940,
        77.25100,
    ),
    ODPair(
        "od_28",
        "city_wide",
        "Azadpur Terminal -> Punjabi Bagh Terminal",
        "Azadpur Terminal",
        28.70768,
        77.17816,
        "Punjabi Bagh Terminal",
        28.67548,
        77.14046,
    ),
    ODPair(
        "od_29",
        "city_wide",
        "Hauz Khas -> Kalkaji Mandir",
        "Hauz Khas",
        28.55300,
        77.20490,
        "Kalkaji Mandir",
        28.55219,
        77.26450,
    ),
    ODPair(
        "od_30",
        "city_wide",
        "Nehru Place -> Sarai Kale Khan ISBT",
        "Nehru Place",
        28.54940,
        77.25100,
        "ISBT Sarai Kale Khan",
        28.59087,
        77.25817,
    ),
)

CSV_COLUMNS: tuple[str, ...] = (
    "od_id",
    "group",
    "label",
    "slot_id",
    "slot_label",
    "departure_time_ist",
    "departure_time_utc",
    "row_status",
    "our_status",
    "our_mode",
    "our_selected_rung",
    "our_total_duration_s",
    "our_window_p10_s",
    "our_window_p90_s",
    "our_board_wait_s",
    "our_ride_walk_duration_s",
    "our_routes",
    "our_transfers",
    "our_timing_sources",
    "google_status",
    "google_http_status",
    "google_duration_s",
    "google_distance_m",
    "google_routes",
    "google_transfers",
    "google_raw_route_count",
    "google_bus_only_count",
    "google_excluded_non_bus_count",
    "google_excluded_vehicle_types",
    "diff_s",
    "diff_pct",
    "abs_diff_s",
    "diff_excl_board_s",
    "google_in_our_p10_p90",
    "google_from_cache",
    "google_cache_key",
    "note",
)


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

    top = options[0]
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
        "our_option_count": len(options),
        "our_error": None,
    }


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

    if google_err or google_http != 200:
        google_status = f"http_{google_http}" if google_http and google_http != 200 else "error"
    elif best_google is not None:
        google_status = "ok"
    elif int(parsed.get("raw_route_count") or 0) > 0:
        google_status = "no_bus_only_route"
    else:
        google_status = "no_routes"

    our_dur = our_res.get("our_total_duration_s")
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
        if our_dur is None and google_dur is None:
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
    depart_utc = depart.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

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
        "cache_path": google_res.get("cache_path"),
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
        "row_status": row_status,
        "note": "; ".join(note_parts),
    }
    return row, manifest_entry


def _fmt_min(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds / 60.0:.1f} min"


def _fmt_dur(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds} s ({seconds / 60.0:.1f} m)"


def _fmt_diff(diff_s: int | None, diff_pct: float | None) -> str:
    if diff_s is None:
        return "—"
    sign = "+" if diff_s > 0 else ""
    pct_str = f" ({sign}{diff_pct:.1f}%)" if diff_pct is not None else ""
    return f"{sign}{diff_s} s{pct_str}"


def _group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    compared = [r for r in rows if r["row_status"] == "compared"]
    if not compared:
        return {
            "planned": len(rows),
            "compared": 0,
            "missing": len(rows),
            "mean_our_s": None,
            "mean_google_s": None,
            "mean_diff_s": None,
            "median_diff_s": None,
            "mae_s": None,
            "within_p10_p90_pct": None,
        }
    ours = [int(r["our_total_duration_s"]) for r in compared]
    goog = [int(r["google_duration_s"]) for r in compared]
    diffs = [int(r["diff_s"]) for r in compared]
    abs_diffs = [int(r["abs_diff_s"]) for r in compared]
    in_win = [r for r in compared if r["google_in_our_p10_p90"] is True]
    return {
        "planned": len(rows),
        "compared": len(compared),
        "missing": len(rows) - len(compared),
        "mean_our_s": round(statistics.mean(ours), 1),
        "mean_google_s": round(statistics.mean(goog), 1),
        "mean_diff_s": round(statistics.mean(diffs), 1),
        "median_diff_s": round(statistics.median(diffs), 1),
        "mae_s": round(statistics.mean(abs_diffs), 1),
        "within_p10_p90_pct": round(100.0 * len(in_win) / len(compared), 1),
    }


def render_markdown_report(
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    csv_rel_path: str = "reports/google-comparator.csv",
    manifest_rel_path: str = "reports/google-manifest.json",
) -> str:
    """Render reports/google-comparator.md with full honesty caveats and all rows."""
    call_acc = manifest["call_accounting"]
    corpus = manifest["corpus_summary"]
    overall = _group_stats(rows)
    missing_rows = [r for r in rows if r["row_status"] != "compared"]

    lines: list[str] = [
        "# Google Routes API (Bus-Only) Comparator Report",
        "",
        "## Methodological Notice: Comparator Only, Not Ground Truth",
        "",
        (
            "- **Google Maps Routes API is a live/future comparator, NOT ground truth.** "
            "It reflects Google's own bus schedule and traffic model, not observed GPS vehicle arrivals on Delhi roads. "
            "Disagreement between our model and Google does not imply either system is right or wrong; real ground-truth "
            "accuracy is measured strictly against observed October 2026 GPS stop passages in `reports/eval-full.md`."
        ),
        (
            "- **October 2026 backtesting against Google is impossible.** Google Routes API `TRANSIT` does not support "
            "past departure times (verified during our feasibility probe: querying `2026-10-20T08:30:00+05:30` returns "
            "HTTP 200 with `routes: []`). All comparator queries therefore use **upcoming matching weekday/time slots** "
            f"(`{manifest['target_date_ist']}`, {manifest['target_weekday']}, matching held-out Tuesday `2026-10-27` and "
            "the brief's Tuesday `08:30` example)."
        ),
        (
            "- **Definition differences to keep in mind:** Our `total_duration_s` includes walk-to-stop, estimated "
            "first boarding wait, model-timed bus ride(s), joint-scenario transfer risk/wait, and egress walk. "
            "Google's `routes.duration` measures door-to-door travel from leaving origin to arriving at destination "
            "(excluding pre-departure waiting at origin before walking), and OTP vs Google may select different bus "
            "lines or transfer points. Both total duration and wait-excluded differences are tracked in "
            f"`{csv_rel_path}`."
        ),
        "",
        "## Collection Contract & Call Accounting",
        "",
        f"- **Target weekday / date (IST):** `{manifest['target_date_ist']}` ({manifest['target_weekday']}; upcoming, zero October/past dates)",
        f"- **Time slots per OD pair:** {', '.join(manifest['time_slots'])}",
        (
            f"- **Corpus size:** {corpus['od_pair_count']} OD pairs × {corpus['time_slots_per_pair']} time slots = **{corpus['planned_slots']} planned slots** "
            f"({corpus['compared_rows']} compared, {corpus['missing_or_error_rows']} missing/error, 0 silently omitted)"
        ),
        (
            f"- **Google request settings:** `travelMode={manifest['travel_mode']}`, "
            f"`transitPreferences.allowedTravelModes={manifest['allowed_travel_modes']}`, "
            f"`languageCode=\"{manifest['language_code']}\"`, `computeAlternativeRoutes=true`, field-masked (`X-Goog-FieldMask`)"
        ),
        (
            f"- **Strict bus-only post-filtering:** Every compared Google itinerary is verified to contain >=1 `TRANSIT` step "
            f"with vehicle type `BUS` only (and `WALK` access/egress/transfers). "
            f"**{corpus['google_non_bus_itineraries_filtered']}** non-bus Google itinerary(ies) "
            "(e.g., Delhi Metro / `SUBWAY` or `RAIL`) were filtered out across the corpus."
        ),
        (
            f"- **Mission-wide Google call budget:** **{call_acc['total_mission_calls']} / {call_acc['hard_cap']}** "
            f"(prior probe calls: {call_acc['prior_mission_calls']}, comparator network calls cached: {call_acc['api_calls_made']}, "
            f"remaining budget: {call_acc['remaining_budget']}; cache hits on this run: {call_acc['cache_hits_this_run']})"
        ),
        (
            f"- **Artifacts:** Report `reports/google-comparator.md`, CSV table `{csv_rel_path}`, "
            f"request manifest `{manifest_rel_path}` (mirrored at `{manifest['scratch_manifest_path']}`), "
            f"raw field-masked cache directory `{manifest['cache_dir']}`."
        ),
        "",
        "## Aggregate Summary",
        "",
        (
            "Difference is defined as **Our Total Duration − Google Bus-Only Duration** "
            "(positive = our model estimates longer door-to-door time; negative = shorter)."
        ),
        "",
        "| Slice | Planned | Compared | Missing/Err | Mean Ours | Mean Google | Mean Diff (Ours − Google) | Median Diff | Mean Abs Diff | Google in Our [p10, p90] |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def _add_summary_row(name: str, stats: dict[str, Any]) -> None:
        if stats["compared"] == 0:
            lines.append(
                f"| {name} | {stats['planned']} | 0 | {stats['missing']} | — | — | — | — | — | — |"
            )
            return
        sign_m = "+" if stats["mean_diff_s"] > 0 else ""
        sign_med = "+" if stats["median_diff_s"] > 0 else ""
        lines.append(
            f"| {name} | {stats['planned']} | {stats['compared']} | {stats['missing']} | "
            f"{_fmt_min(stats['mean_our_s'])} | {_fmt_min(stats['mean_google_s'])} | "
            f"{sign_m}{stats['mean_diff_s']:.0f} s ({sign_m}{stats['mean_diff_s'] / 60.0:.1f} m) | "
            f"{sign_med}{stats['median_diff_s']:.0f} s | "
            f"{stats['mae_s']:.0f} s ({stats['mae_s'] / 60.0:.1f} m) | "
            f"{stats['within_p10_p90_pct']:.1f}% |"
        )

    _add_summary_row("**All OD × Time Slots**", overall)
    for grp_id, grp_label in (
        ("brief_example", "Brief Example (Kashmere Gate -> Nehru Place)"),
        ("corridor_717a_up", "717A Corridor UP (Route 2171, Rung 4)"),
        ("corridor_717a_down", "717A Corridor DOWN (Route 2170, Rung 4)"),
        ("city_wide", "City-Wide Corridors (19 OD pairs)"),
    ):
        grp_rows = [r for r in rows if r["group"] == grp_id]
        if grp_rows:
            _add_summary_row(grp_label, _group_stats(grp_rows))

    lines.extend(
        [
            "",
            "### Breakdown by Time of Day",
            "",
            "| Time Slot (IST) | Planned | Compared | Missing/Err | Mean Ours | Mean Google | Mean Diff (Ours − Google) | Median Diff | Mean Abs Diff | Google in Our [p10, p90] |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for slot_id, slot_label in (("morning_peak", "08:30 IST (Morning Peak)"), ("midday", "14:00 IST (Midday)"), ("evening_peak", "18:30 IST (Evening Peak)")):
        slot_rows = [r for r in rows if r["slot_id"] == slot_id]
        if slot_rows:
            _add_summary_row(slot_label, _group_stats(slot_rows))

    lines.extend(["", "## Missing / Error / Non-Bus-Only Rows", ""])
    if not missing_rows:
        lines.append(
            "All **90 / 90** planned OD × time slots returned both a model-timed itinerary from our API and at least "
            "one strictly bus-only itinerary from Google Routes API (0 missing or error rows)."
        )
    else:
        lines.append(
            f"**{len(missing_rows)} / {len(rows)}** planned slots could not be numerically compared and are explicitly "
            "retained below (never silently omitted):"
        )
        lines.extend(
            [
                "",
                "| OD ID | Corridor | Slot (IST) | Row Status | Our Status | Google Status | Reason / Note |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for r in missing_rows:
            lines.append(
                f"| `{r['od_id']}` | {r['label']} | {r['slot_label']} | `{r['row_status']}` | "
                f"`{r['our_status']}` | `{r['google_status']}` | {r['note'] or '—'} |"
            )

    lines.extend(
        [
            "",
            "## Full Per-OD / Per-Time Comparison Table (All Planned Slots)",
            "",
            "| OD | Group | Origin -> Destination | Slot | Status | Our Duration | Our [p10, p90] | Google Bus Duration | Diff (Ours − Google) | Our Route(s) | Google Bus Route(s) | Our Timing Source | Note |",
            "|---|---|---|---|---|---:|---:|---:|---:|---|---|---|---|",
        ]
    )
    for r in rows:
        win_str = (
            f"[{r['our_window_p10_s'] / 60.0:.1f}, {r['our_window_p90_s'] / 60.0:.1f}] m"
            if r["our_window_p10_s"] is not None and r["our_window_p90_s"] is not None
            else "—"
        )
        lines.append(
            f"| `{r['od_id']}` | `{r['group']}` | {r['label']} | {r['slot_label']} | `{r['row_status']}` | "
            f"{_fmt_dur(r['our_total_duration_s'])} | {win_str} | "
            f"{_fmt_dur(r['google_duration_s'])} | {_fmt_diff(r['diff_s'], r['diff_pct'])} | "
            f"{r['our_routes'] or '—'} | {r['google_routes'] or '—'} | "
            f"`{r['our_timing_sources'] or '—'}` | {r['note'] or '—'} |"
        )

    lines.extend(
        [
            "",
            "## Reproducing From Cache (0 Additional Google API Calls)",
            "",
            "```bash",
            "cd /home/factory-user/repos/Transport-Stack-Hackathon && \\",
            "  /home/factory-user/ps1-scratch/venv/bin/python scripts/compare_google.py \\",
            f"  --date {manifest['target_date_ist']} --cache-only",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Execute the full OD × time comparison and write report, CSV, and manifests."""
    # Pre-validate every departure slot before doing any work.
    slot_datetimes: list[tuple[TimeSlot, datetime]] = []
    for slot in time_slots:
        depart = datetime.combine(target_date, time(slot.hour, slot.minute), tzinfo=IST)
        validate_departure_time(depart, now=now, allow_past_for_cache=cache_only)
        slot_datetimes.append((slot, depart))

    rows: list[dict[str, Any]] = []
    manifest_entries: list[dict[str, Any]] = []
    cache_hits_this_run = 0
    network_calls_this_run = 0

    with httpx.Client(timeout=45.0, transport=api_transport) as api_client:
        for od in od_pairs:
            for slot, depart in slot_datetimes:
                our_res = query_our_api(api_client, api_url, od, depart)
                google_res = google_client.compute_bus_routes(
                    (od.from_lat, od.from_lng),
                    (od.to_lat, od.to_lng),
                    depart,
                    od_id=od.od_id,
                    slot_label=slot.slot_id,
                    now=now,
                    cache_only=cache_only,
                )
                if google_res["from_cache"]:
                    cache_hits_this_run += 1
                else:
                    network_calls_this_run += 1
                row, entry = evaluate_slot_comparison(od, slot, depart, our_res, google_res)
                rows.append(row)
                manifest_entries.append(entry)

    counter = google_client.read_call_counter()
    compared_count = sum(1 for r in rows if r["row_status"] == "compared")
    non_bus_filtered = sum(int(r["google_excluded_non_bus_count"] or 0) for r in rows)

    manifest = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        "cache_dir": str(google_client.responses_dir.resolve()),
        "call_counter_path": str(google_client.counter_path.resolve()),
        "scratch_manifest_path": str(google_client.manifest_path.resolve()),
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
        "corpus_summary": {
            "od_pair_count": len(od_pairs),
            "time_slots_per_pair": len(time_slots),
            "planned_slots": len(rows),
            "compared_rows": compared_count,
            "missing_or_error_rows": len(rows) - compared_count,
            "google_non_bus_itineraries_filtered": non_bus_filtered,
        },
        "entries": manifest_entries,
    }

    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / "google-comparator.csv"
    md_path = report_dir / "google-comparator.md"
    repo_manifest_path = report_dir / "google-manifest.json"

    # Write CSV table
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k) for k in CSV_COLUMNS})

    # Write request manifest to both scratch cache dir and committed reports/
    manifest_json = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    google_client.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    google_client.manifest_path.write_text(manifest_json, encoding="utf-8")
    repo_manifest_path.write_text(manifest_json, encoding="utf-8")

    # Write Markdown comparator report
    md_text = render_markdown_report(rows, manifest)
    md_path.write_text(md_text, encoding="utf-8")

    return rows, manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare PS1 model-timed bus journeys against Google Routes API (bus-only)."
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Upcoming IST date YYYY-MM-DD (default: next upcoming Tuesday matching Oct 27 & brief example).",
    )
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:3100",
        help="Base URL of the local PS1 FastAPI journeys service.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="PS1 data directory (raw Google responses cached under <data-dir>/google/).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=REPO_ROOT / "reports",
        help="Output directory for google-comparator.{md,csv} and google-manifest.json.",
    )
    parser.add_argument(
        "--hard-cap",
        type=int,
        default=GOOGLE_HARD_CALL_CAP,
        help="Mission-wide Google Routes API call hard cap (max 300).",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Use only existing cached Google responses (0 network calls).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.date:
        target_date = date.fromisoformat(args.date)
    else:
        target_date = upcoming_weekday_date(TARGET_WEEKDAY_TUESDAY, min_time_ist=(8, 30))

    client = GoogleRoutesClient(
        cache_dir=Path(args.data_dir) / GOOGLE_DIR,
        hard_cap=args.hard_cap,
        field_mask=DEFAULT_FIELD_MASK,
    )
    _rows, manifest = run_comparison(
        target_date=target_date,
        api_url=args.api_url,
        google_client=client,
        report_dir=Path(args.report_dir),
        cache_only=args.cache_only,
    )
    acc = manifest["call_accounting"]
    corpus = manifest["corpus_summary"]
    print(
        json.dumps(
            {
                "status": "ok",
                "target_date_ist": manifest["target_date_ist"],
                "planned_slots": corpus["planned_slots"],
                "compared_rows": corpus["compared_rows"],
                "missing_or_error_rows": corpus["missing_or_error_rows"],
                "non_bus_itineraries_filtered": corpus["google_non_bus_itineraries_filtered"],
                "total_mission_calls": acc["total_mission_calls"],
                "hard_cap": acc["hard_cap"],
                "cache_hits_this_run": acc["cache_hits_this_run"],
                "network_calls_this_run": acc["network_calls_this_run"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
