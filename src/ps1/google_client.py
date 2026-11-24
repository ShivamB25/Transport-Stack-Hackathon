"""Cached Google Routes API v2 transit (bus-only) comparator client."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from ps1.config import (
    DATA_DIR,
    GOOGLE_DIR,
    GOOGLE_HARD_CALL_CAP,
    GOOGLE_MISSION_LEDGER_PATH,
    IST,
)

GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
DEFAULT_FIELD_MASK = (
    "routes.duration,"
    "routes.distanceMeters,"
    "routes.legs.steps.travelMode,"
    "routes.legs.steps.transitDetails,"
    "routes.legs.steps.staticDuration,"
    "routes.legs.steps.distanceMeters,"
    "routes.legs.localizedValues"
)
DEFAULT_LANGUAGE_CODE = "en"
PRIOR_MISSION_CALLS = 2  # 2 probe calls recorded in ps1-scratch/probe-report.md
PRIOR_MISSION_NOTE = (
    "2 initial feasibility probe calls recorded in /home/factory-user/ps1-scratch/probe-report.md"
)
REPO_ROOT = Path(__file__).resolve().parents[2]
_DURATION_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)s\s*$")


class GoogleClientError(RuntimeError):
    """Controlled failure from the Google Routes comparator client."""


class GoogleBudgetExceededError(GoogleClientError):
    """Raised before issuing a request that would exceed the 300-call mission cap."""


class InvalidDepartureTimeError(ValueError):
    """Raised when a past or October 2026 departure time is requested."""


def load_google_api_key(env_path: Path | None = None) -> str | None:
    """Load GOOGLE_MAPS_API_KEY from environment or repo .env without logging it."""
    env_val = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if env_val:
        return env_val
    dotenv_file = env_path or (REPO_ROOT / ".env")
    if dotenv_file.exists():
        val = (dotenv_values(dotenv_file).get("GOOGLE_MAPS_API_KEY") or "").strip()
        if val:
            return val
    return None


def validate_departure_time(
    depart: datetime,
    *,
    now: datetime | None = None,
    allow_past_for_cache: bool = False,
) -> datetime:
    """Validate that a Google transit departure is timezone-aware, non-October, and upcoming."""
    if depart.tzinfo is None or depart.utcoffset() is None:
        raise InvalidDepartureTimeError("departure_time must include a timezone offset")
    depart_ist = depart.astimezone(IST)
    if depart_ist.year == 2026 and depart_ist.month == 10:
        raise InvalidDepartureTimeError(
            "October 2026 departure times are forbidden for Google Routes TRANSIT "
            "(Google returns zero routes for past transit; use upcoming matching weekday slots)"
        )
    if not allow_past_for_cache:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if depart.astimezone(UTC) <= current:
            raise InvalidDepartureTimeError(
                f"Google Routes TRANSIT requires an upcoming future departure time "
                f"(got {depart.isoformat()}, now is {current.isoformat()})"
            )
    return depart


def upcoming_weekday_date(
    target_weekday: int,
    *,
    min_time_ist: tuple[int, int] = (8, 30),
    now: datetime | None = None,
    min_lead_minutes: int = 5,
) -> date:
    """Return the nearest upcoming IST date matching target_weekday (0=Mon..6=Sun)."""
    if not 0 <= target_weekday <= 6:
        raise ValueError("target_weekday must be in 0..6 (Mon..Sun)")
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    candidate = now_ist.date()
    days_ahead = (target_weekday - candidate.weekday()) % 7
    candidate = candidate + timedelta(days=days_ahead)
    first_slot = datetime.combine(candidate, time(min_time_ist[0], min_time_ist[1]), tzinfo=IST)
    if first_slot <= now_ist + timedelta(minutes=min_lead_minutes):
        candidate = candidate + timedelta(days=7)
    if candidate.year == 2026 and candidate.month == 10:
        raise InvalidDepartureTimeError("Refusing to select an October 2026 date for Google comparator")
    return candidate


def build_compute_routes_payload(
    origin: tuple[float, float],
    destination: tuple[float, float],
    depart: datetime,
    *,
    language_code: str = DEFAULT_LANGUAGE_CODE,
    compute_alternatives: bool = True,
) -> dict[str, Any]:
    """Construct the Google Routes v2 computeRoutes JSON body for bus TRANSIT."""
    if language_code != "en":
        raise ValueError("Google comparator requests must specify languageCode='en'")
    return {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": round(float(origin[0]), 6),
                    "longitude": round(float(origin[1]), 6),
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": round(float(destination[0]), 6),
                    "longitude": round(float(destination[1]), 6),
                }
            }
        },
        "travelMode": "TRANSIT",
        "departureTime": depart.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "computeAlternativeRoutes": bool(compute_alternatives),
        "languageCode": language_code,
        "transitPreferences": {
            "allowedTravelModes": ["BUS"],
        },
    }


def make_cache_key(payload: dict[str, Any], field_mask: str = DEFAULT_FIELD_MASK) -> str:
    """Deterministic cache key covering the exact request payload and field mask."""
    canonical = json.dumps(
        {"payload": payload, "field_mask": field_mask},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def parse_google_duration_s(raw: str | float | None) -> int | None:
    """Parse Google protobuf Duration JSON strings such as '3846s' into integer seconds."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return round(float(raw))
    match = _DURATION_RE.match(str(raw))
    if not match:
        return None
    return round(float(match.group(1)))


def extract_bus_only_itineraries(response_body: dict[str, Any]) -> dict[str, Any]:
    """Filter Google computeRoutes itineraries strictly to BUS-only transit routes.

    Google's transitPreferences.allowedTravelModes=['BUS'] is a preference rather than
    a hard filter and may still return Delhi Metro (SUBWAY) or rail itineraries.
    """
    routes = response_body.get("routes") or []
    bus_only_routes: list[dict[str, Any]] = []
    excluded_non_bus_count = 0
    excluded_vehicle_types: set[str] = set()

    for idx, route in enumerate(routes):
        steps = [
            step
            for leg in (route.get("legs") or [])
            for step in (leg.get("steps") or [])
        ]
        transit_steps = [s for s in steps if s.get("travelMode") == "TRANSIT"]
        other_modes = {
            str(s.get("travelMode") or "UNKNOWN").upper()
            for s in steps
            if s.get("travelMode") not in ("WALK", "TRANSIT")
        }
        vehicle_types = [
            str(
                (s.get("transitDetails") or {})
                .get("transitLine", {})
                .get("vehicle", {})
                .get("type", "UNKNOWN")
            ).upper()
            for s in transit_steps
        ]

        is_bus_only = (
            len(transit_steps) >= 1
            and len(other_modes) == 0
            and all(vt == "BUS" for vt in vehicle_types)
        )
        if not is_bus_only:
            excluded_non_bus_count += 1
            if not transit_steps:
                excluded_vehicle_types.add("WALK_ONLY")
            for vt in vehicle_types:
                if vt != "BUS":
                    excluded_vehicle_types.add(vt)
            for mode in other_modes:
                excluded_vehicle_types.add(mode)
            continue

        duration_s = parse_google_duration_s(route.get("duration"))
        walk_s = sum(
            parse_google_duration_s(s.get("staticDuration")) or 0
            for s in steps
            if s.get("travelMode") == "WALK"
        )
        transit_s = sum(
            parse_google_duration_s(s.get("staticDuration")) or 0
            for s in transit_steps
        )
        route_names: list[str] = []
        bus_legs: list[dict[str, Any]] = []
        for s in transit_steps:
            td = s.get("transitDetails") or {}
            tl = td.get("transitLine") or {}
            sd = td.get("stopDetails") or {}
            line_name = str(tl.get("nameShort") or tl.get("name") or "BUS")
            route_names.append(line_name)
            bus_legs.append(
                {
                    "route_name": line_name,
                    "vehicle_type": str((tl.get("vehicle") or {}).get("type", "BUS")).upper(),
                    "departure_stop": (sd.get("departureStop") or {}).get("name"),
                    "arrival_stop": (sd.get("arrivalStop") or {}).get("name"),
                    "departure_time": sd.get("departureTime"),
                    "arrival_time": sd.get("arrivalTime"),
                    "stop_count": td.get("stopCount"),
                    "static_duration_s": parse_google_duration_s(s.get("staticDuration")),
                    "distance_m": s.get("distanceMeters"),
                }
            )

        if duration_s is None:
            duration_s = walk_s + transit_s

        bus_only_routes.append(
            {
                "route_index": idx,
                "duration_s": duration_s,
                "distance_m": route.get("distanceMeters"),
                "walk_duration_s": walk_s,
                "transit_duration_s": transit_s,
                "transfers": max(0, len(transit_steps) - 1),
                "route_names": route_names,
                "vehicle_types": sorted(set(vehicle_types)),
                "bus_legs": bus_legs,
            }
        )

    bus_only_routes.sort(key=lambda item: (item["duration_s"], item["transfers"], item["route_index"]))
    return {
        "raw_route_count": len(routes),
        "bus_only_count": len(bus_only_routes),
        "excluded_non_bus_count": excluded_non_bus_count,
        "excluded_vehicle_types": sorted(excluded_vehicle_types),
        "bus_only_routes": bus_only_routes,
        "best_bus_route": bus_only_routes[0] if bus_only_routes else None,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


class GoogleRoutesClient:
    """Disk-cached Google Routes API client enforcing the 300-call mission-wide cap."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        api_key: str | None = None,
        hard_cap: int = GOOGLE_HARD_CALL_CAP,
        prior_mission_calls: int = PRIOR_MISSION_CALLS,
        field_mask: str = DEFAULT_FIELD_MASK,
        timeout_s: float = 45.0,
        transport: httpx.BaseTransport | None = None,
        mission_ledger_path: Path | None = None,
    ) -> None:
        if hard_cap > GOOGLE_HARD_CALL_CAP:
            raise ValueError(f"hard_cap cannot exceed mission cap of {GOOGLE_HARD_CALL_CAP}")
        self.cache_dir = Path(cache_dir or (DATA_DIR / GOOGLE_DIR))
        self.responses_dir = self.cache_dir / "responses"
        self.counter_path = Path(mission_ledger_path or GOOGLE_MISSION_LEDGER_PATH)
        self.counter_lock_path = self.counter_path.with_suffix(self.counter_path.suffix + ".lock")
        self.manifest_path = self.cache_dir / "manifest.json"
        self._api_key = api_key
        self.hard_cap = int(hard_cap)
        self.prior_mission_calls = int(prior_mission_calls)
        self.field_mask = field_mask
        self.timeout_s = float(timeout_s)
        self._transport = transport

    def _default_counter(self) -> dict[str, Any]:
        total = self.prior_mission_calls
        return {
            "hard_cap": self.hard_cap,
            "prior_mission_calls": self.prior_mission_calls,
            "prior_mission_note": PRIOR_MISSION_NOTE,
            "api_calls_made": 0,
            "total_mission_calls": total,
            "remaining_budget": max(0, self.hard_cap - total),
            "calls": [],
        }

    @contextmanager
    def _ledger_lock(self):
        self.counter_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.counter_lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _normalise_counter(self, data: dict[str, Any]) -> dict[str, Any]:
        calls = data.setdefault("calls", [])
        for call in calls:
            call.setdefault("status", "completed")
        prior = int(data.get("prior_mission_calls", self.prior_mission_calls))
        made = int(data.get("api_calls_made", len(calls)))
        made = max(made, len(calls))
        cap = min(self.hard_cap, int(data.get("hard_cap", self.hard_cap)))
        total = prior + made
        data["hard_cap"] = cap
        data["prior_mission_calls"] = prior
        data["api_calls_made"] = made
        data["total_mission_calls"] = total
        data["remaining_budget"] = max(0, cap - total)
        return data

    def _read_call_counter_unlocked(self) -> dict[str, Any]:
        if not self.counter_path.exists():
            legacy_path = DATA_DIR / GOOGLE_DIR / "call_counter.json"
            if self.counter_path == GOOGLE_MISSION_LEDGER_PATH and legacy_path.exists():
                legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
                counter = self._normalise_counter(legacy)
                _atomic_write_json(self.counter_path, counter)
                return counter
            return self._default_counter()
        data = json.loads(self.counter_path.read_text(encoding="utf-8"))
        return self._normalise_counter(data)

    def read_call_counter(self) -> dict[str, Any]:
        with self._ledger_lock():
            return self._read_call_counter_unlocked()

    def _reserve_network_call(
        self,
        *,
        cache_key: str,
        od_id: str,
        slot_label: str,
        departure_time_utc: str,
    ) -> tuple[dict[str, Any], int]:
        with self._ledger_lock():
            counter = self._read_call_counter_unlocked()
            if int(counter["total_mission_calls"]) + 1 > int(counter["hard_cap"]):
                raise GoogleBudgetExceededError(
                    f"Google Routes mission call cap reached: "
                    f"{counter['total_mission_calls']}/{counter['hard_cap']} calls used"
                )
            total = int(counter["total_mission_calls"]) + 1
            counter["api_calls_made"] = int(counter["api_calls_made"]) + 1
            counter["total_mission_calls"] = total
            counter["remaining_budget"] = max(0, int(counter["hard_cap"]) - total)
            reserved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            counter["calls"].append(
                {
                    "call_number": total,
                    "timestamp": reserved_at,
                    "reserved_at": reserved_at,
                    "status": "reserved",
                    "cache_key": cache_key,
                    "od_id": od_id,
                    "slot_label": slot_label,
                    "departure_time_utc": departure_time_utc,
                    "http_status": None,
                    "error": None,
                }
            )
            _atomic_write_json(self.counter_path, counter)
            return counter, total

    def _complete_network_call(
        self,
        call_number: int,
        *,
        http_status: int | None,
        error: str | None,
    ) -> dict[str, Any]:
        with self._ledger_lock():
            counter = self._read_call_counter_unlocked()
            call = next(c for c in counter["calls"] if int(c["call_number"]) == call_number)
            call["status"] = "completed"
            call["completed_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            call["http_status"] = http_status
            call["error"] = error
            _atomic_write_json(self.counter_path, counter)
            return counter

    def compute_bus_routes(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart: datetime,
        *,
        od_id: str = "",
        slot_label: str = "",
        now: datetime | None = None,
        cache_only: bool = False,
    ) -> dict[str, Any]:
        """Fetch or load from disk cache a bus-only Google Routes v2 response."""
        validate_departure_time(depart, now=now, allow_past_for_cache=True)
        payload = build_compute_routes_payload(
            origin,
            destination,
            depart,
            language_code=DEFAULT_LANGUAGE_CODE,
            compute_alternatives=True,
        )
        cache_key = make_cache_key(payload, self.field_mask)
        cache_file = self.responses_dir / f"{cache_key}.json"

        # Always check the disk cache BEFORE issuing any network request.
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            response_body = cached.get("response") or {}
            http_status = cached.get("http_status")
            error = cached.get("error")
            parsed = (
                extract_bus_only_itineraries(response_body)
                if http_status == 200 and not error and "error" not in response_body
                else {
                    "raw_route_count": 0,
                    "bus_only_count": 0,
                    "excluded_non_bus_count": 0,
                    "excluded_vehicle_types": [],
                    "bus_only_routes": [],
                    "best_bus_route": None,
                }
            )
            return {
                "cache_key": cache_key,
                "cache_path": str(cache_file),
                "from_cache": True,
                "fetched_at": cached.get("fetched_at"),
                "http_status": http_status,
                "error": error or (response_body.get("error", {}).get("message") if isinstance(response_body.get("error"), dict) else None),
                "request": cached.get("request", payload),
                "field_mask": cached.get("field_mask", self.field_mask),
                "parsed": parsed,
                "call_accounting": self.read_call_counter(),
            }

        if cache_only:
            raise GoogleClientError(f"Cache miss for {od_id or cache_key} ({slot_label}) in cache_only mode")

        # Live network requests strictly require an upcoming departure time.
        validate_departure_time(depart, now=now, allow_past_for_cache=False)

        api_key = self._api_key or load_google_api_key()
        if not api_key:
            raise GoogleClientError("GOOGLE_MAPS_API_KEY is not configured in environment or .env")

        headers = {
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": self.field_mask,
            "Content-Type": "application/json",
        }
        _reservation, call_number = self._reserve_network_call(
            cache_key=cache_key,
            od_id=od_id,
            slot_label=slot_label,
            departure_time_utc=payload["departureTime"],
        )
        fetched_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        http_status: int | None = None
        error_msg: str | None = None
        response_body: dict[str, Any] = {}

        try:
            with httpx.Client(timeout=self.timeout_s, transport=self._transport) as client:
                response = client.post(GOOGLE_ROUTES_URL, json=payload, headers=headers)
            http_status = int(response.status_code)
            try:
                response_body = response.json()
            except ValueError:
                response_body = {"raw_text": response.text[:1000]}
                error_msg = f"non-JSON response (HTTP {http_status})"
            if http_status != 200:
                api_err = response_body.get("error") if isinstance(response_body, dict) else None
                if isinstance(api_err, dict):
                    error_msg = f"HTTP {http_status}: {api_err.get('status', '')} {api_err.get('message', '')}".strip()
                else:
                    error_msg = error_msg or f"HTTP {http_status}"
        except httpx.HTTPError as exc:
            error_msg = f"network error: {type(exc).__name__}: {exc}"

        updated_counter = self._complete_network_call(
            call_number,
            http_status=http_status,
            error=error_msg,
        )

        # Persist sanitized response (never include API key).
        cache_record = {
            "cache_key": cache_key,
            "od_id": od_id,
            "slot_label": slot_label,
            "fetched_at": fetched_at,
            "endpoint": GOOGLE_ROUTES_URL,
            "field_mask": self.field_mask,
            "request": payload,
            "http_status": http_status,
            "error": error_msg,
            "response": response_body,
        }
        _atomic_write_json(cache_file, cache_record)

        parsed = (
            extract_bus_only_itineraries(response_body)
            if http_status == 200 and not error_msg and "error" not in response_body
            else {
                "raw_route_count": 0,
                "bus_only_count": 0,
                "excluded_non_bus_count": 0,
                "excluded_vehicle_types": [],
                "bus_only_routes": [],
                "best_bus_route": None,
            }
        )
        return {
            "cache_key": cache_key,
            "cache_path": str(cache_file),
            "from_cache": False,
            "fetched_at": fetched_at,
            "http_status": http_status,
            "error": error_msg,
            "request": payload,
            "field_mask": self.field_mask,
            "parsed": parsed,
            "call_accounting": updated_counter,
        }
