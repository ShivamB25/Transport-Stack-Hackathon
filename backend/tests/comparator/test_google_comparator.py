"""Unit tests for Google Routes API comparator client, bus-only filter, cap, and reporting."""
from __future__ import annotations

import csv
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from delhi_transit.comparator import compare as compare_mod
from delhi_transit.comparator.client import (
    DEFAULT_FIELD_MASK,
    IST,
    GoogleBudgetExceededError,
    GoogleClientError,
    GoogleRoutesClient,
    InvalidDepartureTimeError,
    build_compute_routes_payload,
    extract_bus_only_itineraries,
    parse_google_duration_s,
    upcoming_weekday_date,
    validate_departure_time,
)
from delhi_transit.comparator.slots import (
    DEFAULT_OD_PAIRS,
    DEFAULT_TIME_SLOTS,
    TimeSlot,
)


def _google_route(duration_s: int, vehicle_types: list[str], line_names: list[str] | None = None) -> dict:
    names = line_names or [f"L{i}" for i in range(len(vehicle_types))]
    steps = [{"travelMode": "WALK", "staticDuration": "120s", "distanceMeters": 150}]
    for vtype, name in zip(vehicle_types, names, strict=True):
        steps.append(
            {
                "travelMode": "TRANSIT",
                "staticDuration": f"{max(60, duration_s - 240)}s",
                "distanceMeters": 4000,
                "transitDetails": {
                    "stopDetails": {
                        "departureStop": {"name": "Stop A"},
                        "arrivalStop": {"name": "Stop B"},
                        "departureTime": "2026-11-24T03:05:00Z",
                        "arrivalTime": "2026-11-24T03:45:00Z",
                    },
                    "transitLine": {
                        "nameShort": name,
                        "vehicle": {"type": vtype},
                    },
                    "stopCount": 12,
                },
            }
        )
    steps.append({"travelMode": "WALK", "staticDuration": "120s", "distanceMeters": 150})
    return {
        "duration": f"{duration_s}s",
        "distanceMeters": 4300,
        "legs": [{"steps": steps}],
    }


def test_rejects_october_and_past_departure_times() -> None:
    now = datetime(2026, 11, 24, 1, 0, tzinfo=IST)
    october_dt = datetime(2026, 10, 22, 8, 30, tzinfo=IST)
    past_dt = datetime(2026, 11, 23, 8, 30, tzinfo=IST)
    future_dt = datetime(2026, 11, 24, 8, 30, tzinfo=IST)

    with pytest.raises(InvalidDepartureTimeError, match="October 2026"):
        validate_departure_time(october_dt, now=now, allow_past_for_cache=False)
    with pytest.raises(InvalidDepartureTimeError, match="October 2026"):
        validate_departure_time(october_dt, now=now, allow_past_for_cache=True)
    with pytest.raises(InvalidDepartureTimeError, match="upcoming future"):
        validate_departure_time(past_dt, now=now, allow_past_for_cache=False)

    # Past non-October date is allowed only when loading previously cached files
    assert validate_departure_time(past_dt, now=now, allow_past_for_cache=True) == past_dt
    assert validate_departure_time(future_dt, now=now, allow_past_for_cache=False) == future_dt


def test_upcoming_weekday_date_picks_future_matching_weekday() -> None:
    early_tue = datetime(2026, 11, 24, 1, 10, tzinfo=IST)
    late_tue = datetime(2026, 11, 24, 9, 0, tzinfo=IST)

    assert upcoming_weekday_date(1, min_time_ist=(8, 30), now=early_tue) == date(2026, 11, 24)
    assert upcoming_weekday_date(1, min_time_ist=(8, 30), now=late_tue) == date(2026, 12, 1)


def test_payload_and_bus_only_filter() -> None:
    depart = datetime(2026, 11, 24, 8, 30, tzinfo=IST)
    payload = build_compute_routes_payload((28.6675, 77.2282), (28.5494, 77.2510), depart)
    assert payload["travelMode"] == "TRANSIT"
    assert payload["languageCode"] == "en"
    assert payload["transitPreferences"]["allowedTravelModes"] == ["BUS"]
    assert payload["departureTime"] == "2026-11-24T03:00:00Z"

    with pytest.raises(ValueError, match="languageCode='en'"):
        build_compute_routes_payload((28.6675, 77.2282), (28.5494, 77.2510), depart, language_code="bn")

    response = {
        "routes": [
            _google_route(1800, ["SUBWAY"], ["Yellow Line"]),
            _google_route(2100, ["BUS", "SUBWAY"], ["425", "Magenta Line"]),
            _google_route(3400, ["BUS", "BUS"], ["419", "529"]),
            _google_route(2950, ["BUS"], ["425"]),
            {"duration": "900s", "legs": [{"steps": [{"travelMode": "WALK", "staticDuration": "900s"}]}]},
        ]
    }
    parsed = extract_bus_only_itineraries(response)
    assert parsed["raw_route_count"] == 5
    assert parsed["bus_only_count"] == 2
    assert parsed["excluded_non_bus_count"] == 3
    assert set(parsed["excluded_vehicle_types"]) == {"SUBWAY", "WALK_ONLY"}
    assert parsed["best_bus_route"]["duration_s"] == 2950
    assert parsed["best_bus_route"]["route_names"] == ["425"]
    assert parsed["best_bus_route"]["vehicle_types"] == ["BUS"]
    assert parse_google_duration_s("3846s") == 3846
    assert parse_google_duration_s("12.6s") == 13


def test_client_caches_before_call_redacts_key_and_enforces_hard_cap(tmp_path: Path) -> None:
    calls_seen: list[dict] = []
    secret_key = "SUPER_SECRET_TEST_GOOGLE_KEY_999"

    def handler(request: httpx.Request) -> httpx.Response:
        calls_seen.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        if len(calls_seen) == 2:
            raise httpx.ConnectError(f"rejected header X-Goog-Api-Key: {secret_key}", request=request)
        return httpx.Response(200, json={"routes": [_google_route(2500, ["BUS"], ["717A"])]})

    transport = httpx.MockTransport(handler)
    ledger_path = tmp_path / "mission-ledger.json"
    client = GoogleRoutesClient(
        cache_dir=tmp_path / "google",
        mission_ledger_path=ledger_path,
        api_key=secret_key,
        hard_cap=4,
        prior_mission_calls=2,
        transport=transport,
    )
    now = datetime(2026, 11, 24, 1, 0, tzinfo=IST)
    depart1 = datetime(2026, 11, 24, 8, 30, tzinfo=IST)
    depart2 = datetime(2026, 11, 24, 14, 0, tzinfo=IST)
    depart3 = datetime(2026, 11, 24, 18, 0, tzinfo=IST)

    # First call hits network (total calls: 2 prior + 1 = 3)
    res1 = client.compute_bus_routes(
        (28.49313, 77.30373), (28.52143, 77.08203), depart1, od_id="od_02", slot_label="morning_peak", now=now
    )
    assert res1["from_cache"] is False
    assert res1["parsed"]["best_bus_route"]["duration_s"] == 2500
    assert res1["call_accounting"]["total_mission_calls"] == 3
    assert len(calls_seen) == 1
    assert calls_seen[0]["headers"]["x-goog-fieldmask"] == DEFAULT_FIELD_MASK
    assert calls_seen[0]["body"]["languageCode"] == "en"
    assert calls_seen[0]["body"]["transitPreferences"]["allowedTravelModes"] == ["BUS"]

    # Second call for the SAME slot MUST hit cache and NOT fail budget cap or issue HTTP
    res1_cached = client.compute_bus_routes(
        (28.49313, 77.30373), (28.52143, 77.08203), depart1, od_id="od_02", slot_label="morning_peak", now=now
    )
    assert res1_cached["from_cache"] is True
    assert len(calls_seen) == 1
    assert res1_cached["call_accounting"]["total_mission_calls"] == 3

    # A transport error whose text carries the key is stored as its class name only
    failed = client.compute_bus_routes(
        (28.49313, 77.30373), (28.52143, 77.08203), depart2, od_id="od_02", slot_label="midday", now=now
    )
    assert failed["error"] == "network error: ConnectError"
    assert secret_key not in json.dumps(failed)
    assert failed["call_accounting"]["total_mission_calls"] == 4

    # Ensure API key never appears in the cache or the ledger
    persisted = [ledger_path, *(tmp_path / "google").rglob("*.json")]
    assert len(persisted) == 3
    for path in persisted:
        assert secret_key not in path.read_text(encoding="utf-8")

    # Uncached call exceeding hard_cap MUST raise BEFORE sending HTTP
    with pytest.raises(GoogleBudgetExceededError, match="4/4"):
        client.compute_bus_routes(
            (28.49313, 77.30373), (28.52143, 77.08203), depart3, od_id="od_02", slot_label="evening", now=now
        )
    assert len(calls_seen) == 2

    # Cache-only mode rejects uncached slots without network
    with pytest.raises(GoogleClientError, match="cache_only"):
        client.compute_bus_routes(
            (28.49313, 77.30373),
            (28.52143, 77.08203),
            depart2 + timedelta(hours=1),
            now=now,
            cache_only=True,
        )


def test_end_to_end_compare_report_retains_missing_rows_and_honesty_notice(tmp_path: Path) -> None:
    od_pairs = DEFAULT_OD_PAIRS[:2]  # brief_example + 717A UP
    slots = DEFAULT_TIME_SLOTS

    def fake_our_api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "mode": "live",
                "selected_rung": "4",
                "options": [
                    {
                        "total_duration_s": 3600,
                        "total_window_p10_s": 3000,
                        "total_window_p90_s": 4300,
                        "legs": [
                            {"type": "walk", "estimated_duration_s": 200},
                            {"type": "board", "estimated_duration_s": 180},
                            {
                                "type": "ride",
                                "route_name": "717AUP",
                                "estimated_duration_s": 3100,
                                "source": "historical",
                                "timing_source": "historical_fallback",
                                "observed_coverage": 1.0,
                            },
                            {"type": "walk", "estimated_duration_s": 120},
                        ],
                    }
                ],
            },
        )

    call_idx = {"n": 0}

    def fake_google_api(request: httpx.Request) -> httpx.Response:
        call_idx["n"] += 1
        # Make slot 3 return only SUBWAY (so bus-only filter excludes it -> missing row)
        if call_idx["n"] == 3:
            return httpx.Response(200, json={"routes": [_google_route(1900, ["SUBWAY"], ["Violet Line"])]})
        # Make slot 6 return HTTP 500 error
        if call_idx["n"] == 6:
            return httpx.Response(500, json={"error": {"status": "INTERNAL", "message": "temporary upstream error"}})
        return httpx.Response(
            200,
            json={
                "routes": [
                    _google_route(1700, ["SUBWAY"], ["Yellow Line"]),
                    _google_route(3300, ["BUS"], ["425"]),
                ]
            },
        )

    google_client = GoogleRoutesClient(
        cache_dir=tmp_path / "data" / "google",
        mission_ledger_path=tmp_path / "mission-ledger.json",
        api_key="DUMMY_TEST_KEY",
        hard_cap=300,
        prior_mission_calls=2,
        transport=httpx.MockTransport(fake_google_api),
    )
    now = datetime(2026, 11, 24, 1, 0, tzinfo=IST)
    report_dir = tmp_path / "reports"

    rows, manifest = compare_mod.run_comparison(
        target_date=date(2026, 11, 24),
        api_url="http://127.0.0.1:3100",
        google_client=google_client,
        report_dir=report_dir,
        od_pairs=od_pairs,
        time_slots=slots,
        api_transport=httpx.MockTransport(fake_our_api),
        now=now,
    )

    assert len(rows) == 6
    assert manifest["corpus_summary"]["planned_slots"] == 6
    assert manifest["corpus_summary"]["compared_rows"] == 4
    assert manifest["corpus_summary"]["missing_or_error_rows"] == 2
    assert manifest["corpus_summary"]["google_non_bus_itineraries_filtered"] == 5
    assert manifest["call_accounting"]["total_mission_calls"] == 8  # 2 prior + 6 calls

    # Every compared row must be strictly BUS-only
    for entry in manifest["entries"]:
        assert not entry["departure_time_ist"].startswith("2026-10")
        if entry["row_status"] == "compared":
            assert entry["google_vehicle_types_compared"] == ["BUS"]

    # CSV contains all 6 rows (missing/error rows NOT omitted)
    with (report_dir / "google-comparator.csv").open(encoding="utf-8") as handle:
        csv_rows = list(csv.DictReader(handle))
    assert len(csv_rows) == 6
    statuses = {r["row_status"] for r in csv_rows}
    assert "compared" in statuses
    assert "google_no_bus_only_route" in statuses
    assert "google_http_500" in statuses

    # Markdown report contains explicit caveats, our duration, Google duration, difference, and missing rows
    md_text = (report_dir / "google-comparator.md").read_text(encoding="utf-8")
    assert "NOT ground truth" in md_text
    assert "+300 s" in md_text
    assert "google_no_bus_only_route" in md_text
    assert "google_http_500" in md_text

    # Re-running with --cache-only issues 0 additional Google calls
    rows_cached, manifest_cached = compare_mod.run_comparison(
        target_date=date(2026, 11, 24),
        api_url="http://127.0.0.1:3100",
        google_client=google_client,
        report_dir=report_dir,
        od_pairs=od_pairs,
        time_slots=slots,
        cache_only=True,
        api_transport=httpx.MockTransport(fake_our_api),
        now=now,
    )
    assert len(rows_cached) == 6
    assert manifest_cached["call_accounting"]["network_calls_this_run"] == 0
    assert manifest_cached["call_accounting"]["cache_hits_this_run"] == 6
    assert manifest_cached["call_accounting"]["total_mission_calls"] == 8

    # Rendering is pure: two independent output directories are byte-identical.
    inputs_path = report_dir / "google-comparator-inputs.json"
    render_a = tmp_path / "render-a"
    render_b = tmp_path / "render-b"
    compare_mod.render_comparison(inputs_path=inputs_path, report_dir=render_a)
    compare_mod.render_comparison(inputs_path=inputs_path, report_dir=render_b)
    for filename in ("google-comparator.md", "google-comparator.csv", "google-manifest.json"):
        assert (render_a / filename).read_bytes() == (render_b / filename).read_bytes()


def test_mission_ledger_survives_cache_dir_change_and_counts_interruption(tmp_path: Path) -> None:
    ledger_path = tmp_path / "fixed-mission-ledger.json"
    now = datetime(2026, 11, 24, 1, 0, tzinfo=IST)
    depart = datetime(2026, 11, 24, 8, 30, tzinfo=IST)

    def interrupt(_request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    interrupted_client = GoogleRoutesClient(
        cache_dir=tmp_path / "cache-a",
        mission_ledger_path=ledger_path,
        api_key="DUMMY_TEST_KEY",
        hard_cap=3,
        prior_mission_calls=2,
        transport=httpx.MockTransport(interrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        interrupted_client.compute_bus_routes(
            (28.1, 77.1), (28.2, 77.2), depart, now=now, od_id="interrupted"
        )

    counter = interrupted_client.read_call_counter()
    assert counter["total_mission_calls"] == 3
    assert counter["calls"][0]["status"] == "reserved"
    assert counter["calls"][0]["http_status"] is None

    other_cache_client = GoogleRoutesClient(
        cache_dir=tmp_path / "cache-b",
        mission_ledger_path=ledger_path,
        api_key="DUMMY_TEST_KEY",
        hard_cap=3,
        prior_mission_calls=2,
        transport=httpx.MockTransport(lambda _request: pytest.fail("network must not be called")),
    )
    with pytest.raises(GoogleBudgetExceededError, match="3/3"):
        other_cache_client.compute_bus_routes(
            (28.3, 77.3),
            (28.4, 77.4),
            depart + timedelta(hours=1),
            now=now,
            od_id="must-not-reset",
        )


def test_concurrent_network_reservations_stop_at_mission_cap(tmp_path: Path) -> None:
    network_count = 0
    network_lock = threading.Lock()
    ledger_path = tmp_path / "fixed-mission-ledger.json"
    now = datetime(2026, 11, 24, 1, 0, tzinfo=IST)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_count
        with network_lock:
            network_count += 1
        return httpx.Response(200, json={"routes": [_google_route(2500, ["BUS"], ["717A"])]})

    client = GoogleRoutesClient(
        cache_dir=tmp_path / "cache",
        mission_ledger_path=ledger_path,
        api_key="DUMMY_TEST_KEY",
        hard_cap=5,
        prior_mission_calls=2,
        transport=httpx.MockTransport(handler),
    )

    def request(index: int) -> str:
        try:
            client.compute_bus_routes(
                (28.0 + index / 1000, 77.0),
                (28.5, 77.5),
                datetime(2026, 11, 24, 8, 30, tzinfo=IST),
                now=now,
                od_id=f"od_{index}",
            )
        except GoogleBudgetExceededError:
            return "budget"
        return "network"

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(request, range(12)))

    assert outcomes.count("network") == 3
    assert outcomes.count("budget") == 9
    assert network_count == 3
    counter = client.read_call_counter()
    assert counter["total_mission_calls"] == 5
    assert counter["api_calls_made"] == 3
    assert len(counter["calls"]) == 3
    assert all(call["status"] == "completed" for call in counter["calls"])

def test_missing_fixed_ledger_is_seeded_from_canonical_counter(tmp_path: Path, monkeypatch) -> None:
    from delhi_transit.comparator import client as google_client

    ledger_path = tmp_path / "fixed-mission-ledger.json"
    legacy_path = tmp_path / "canonical" / "call_counter.json"
    legacy_path.parent.mkdir()
    legacy_path.write_text(
        json.dumps({"hard_cap": 300, "prior_mission_calls": 2, "api_calls_made": 1, "calls": [{"call_number": 3}]})
    )
    monkeypatch.setattr(google_client, "GOOGLE_MISSION_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(google_client, "GOOGLE_LEGACY_COUNTER_PATH", legacy_path)
    client = GoogleRoutesClient(
        cache_dir=tmp_path / "other-data" / "google",
        api_key="DUMMY_TEST_KEY",
        hard_cap=3,
        transport=httpx.MockTransport(lambda _request: pytest.fail("network must not be called")),
    )
    with pytest.raises(GoogleBudgetExceededError, match="3/3"):
        client.compute_bus_routes(
            (28.1, 77.1),
            (28.2, 77.2),
            datetime(2026, 11, 24, 8, 30, tzinfo=IST),
            now=datetime(2026, 11, 24, 1, 0, tzinfo=IST),
        )
    assert json.loads(ledger_path.read_text())["total_mission_calls"] == 3


def test_paid_call_is_refused_when_no_ledger_establishes_prior_usage(tmp_path: Path, monkeypatch) -> None:
    from delhi_transit.comparator import client as google_client

    ledger_path = tmp_path / "fixed-mission-ledger.json"
    monkeypatch.setattr(google_client, "GOOGLE_MISSION_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(google_client, "GOOGLE_LEGACY_COUNTER_PATH", tmp_path / "missing" / "call_counter.json")
    client = GoogleRoutesClient(
        cache_dir=tmp_path / "google",
        api_key="DUMMY_TEST_KEY",
        transport=httpx.MockTransport(lambda _request: pytest.fail("network must not be called")),
    )
    now = datetime(2026, 11, 24, 1, 0, tzinfo=IST)
    cached_depart = datetime(2026, 11, 24, 8, 30, tzinfo=IST)
    payload = build_compute_routes_payload((28.1, 77.1), (28.2, 77.2), cached_depart)
    cache_file = client.responses_dir / f"{google_client.make_cache_key(payload)}.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(json.dumps({"http_status": 200, "request": payload, "response": {"routes": []}}))

    assert client.compute_bus_routes((28.1, 77.1), (28.2, 77.2), cached_depart, now=now)["from_cache"]
    with pytest.raises(GoogleClientError, match="prior usage"):
        client.compute_bus_routes((28.1, 77.1), (28.2, 77.2), cached_depart + timedelta(hours=1), now=now)
    assert not ledger_path.exists()


def test_wrong_weekday_or_slot_is_rejected_before_any_network(tmp_path: Path) -> None:
    calls = {"google": 0, "planner": 0}

    def google_handler(_request: httpx.Request) -> httpx.Response:
        calls["google"] += 1
        return httpx.Response(200, json={"routes": []})

    def planner_handler(_request: httpx.Request) -> httpx.Response:
        calls["planner"] += 1
        return httpx.Response(500)

    client = GoogleRoutesClient(
        cache_dir=tmp_path / "cache",
        mission_ledger_path=tmp_path / "ledger.json",
        api_key="DUMMY_TEST_KEY",
        transport=httpx.MockTransport(google_handler),
    )
    common = {
        "api_url": "http://127.0.0.1:3100",
        "google_client": client,
        "report_dir": tmp_path / "reports",
        "od_pairs": DEFAULT_OD_PAIRS[:1],
        "api_transport": httpx.MockTransport(planner_handler),
        "now": datetime(2026, 11, 24, 1, 0, tzinfo=IST),
    }
    with pytest.raises(ValueError, match="must be a Tuesday"):
        compare_mod.run_comparison(target_date=date(2026, 11, 25), **common)
    bad_slot = TimeSlot("morning_peak", "09:00 IST", 9, 0)
    with pytest.raises(ValueError, match="declared weekday time"):
        compare_mod.run_comparison(
            target_date=date(2026, 11, 24),
            time_slots=(bad_slot,),
            **common,
        )
    assert calls == {"google": 0, "planner": 0}
    assert client.read_call_counter()["total_mission_calls"] == 2


def _planner_success(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "ok",
            "mode": "live",
            "selected_rung": "4",
            "options": [
                {
                    "total_duration_s": 3600,
                    "total_window_p10_s": 3000,
                    "total_window_p90_s": 4300,
                    "legs": [
                        {"type": "walk", "estimated_duration_s": 200},
                        {"type": "board", "estimated_duration_s": 180},
                        {
                            "type": "ride",
                            "route_name": "717AUP",
                            "estimated_duration_s": 3100,
                            "timing_source": "model_live",
                            "observed_coverage": 0.8,
                        },
                        {"type": "walk", "estimated_duration_s": 120},
                    ],
                }
            ],
        },
    )


def test_cache_miss_retains_unavailable_row_and_existing_results(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    now = datetime(2026, 11, 24, 1, 0, tzinfo=IST)
    client = GoogleRoutesClient(
        cache_dir=tmp_path / "cache",
        mission_ledger_path=ledger_path,
        api_key="DUMMY_TEST_KEY",
        hard_cap=3,
        prior_mission_calls=2,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"routes": [_google_route(3300, ["BUS"], ["717A"])]})
        ),
    )
    compare_mod.run_comparison(
        target_date=date(2026, 11, 24),
        api_url="http://127.0.0.1:3100",
        google_client=client,
        report_dir=tmp_path / "seed",
        od_pairs=DEFAULT_OD_PAIRS[:1],
        time_slots=DEFAULT_TIME_SLOTS[:1],
        api_transport=httpx.MockTransport(_planner_success),
        now=now,
    )

    rows, manifest = compare_mod.run_comparison(
        target_date=date(2026, 11, 24),
        api_url="http://127.0.0.1:3100",
        google_client=client,
        report_dir=tmp_path / "partial",
        od_pairs=DEFAULT_OD_PAIRS[:2],
        time_slots=DEFAULT_TIME_SLOTS[:1],
        cache_only=True,
        api_transport=httpx.MockTransport(_planner_success),
        now=now,
    )
    assert len(rows) == 2
    assert rows[0]["row_status"] == "compared"
    assert rows[1]["row_status"] == "google_unavailable_cache_miss"
    assert rows[1]["our_total_duration_s"] == 3600
    assert manifest["corpus_summary"]["planned_slots"] == 2
    assert manifest["corpus_summary"]["compared_rows"] == 1
    assert manifest["call_accounting"]["total_mission_calls"] == 3


def test_cap_exhaustion_retains_collected_and_unavailable_rows(tmp_path: Path) -> None:
    client = GoogleRoutesClient(
        cache_dir=tmp_path / "cache",
        mission_ledger_path=tmp_path / "ledger.json",
        api_key="DUMMY_TEST_KEY",
        hard_cap=4,
        prior_mission_calls=2,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"routes": [_google_route(3300, ["BUS"], ["717A"])]})
        ),
    )
    rows, manifest = compare_mod.run_comparison(
        target_date=date(2026, 11, 24),
        api_url="http://127.0.0.1:3100",
        google_client=client,
        report_dir=tmp_path / "reports",
        od_pairs=DEFAULT_OD_PAIRS[:3],
        time_slots=DEFAULT_TIME_SLOTS[:1],
        api_transport=httpx.MockTransport(_planner_success),
        now=datetime(2026, 11, 24, 1, 0, tzinfo=IST),
    )
    assert len(rows) == 3
    assert [row["row_status"] for row in rows] == [
        "compared",
        "compared",
        "google_unavailable_quota_exhausted",
    ]
    assert all(row["our_total_duration_s"] == 3600 for row in rows)
    assert manifest["corpus_summary"]["planned_slots"] == 3
    assert manifest["call_accounting"]["total_mission_calls"] == 4
    assert manifest["call_accounting"]["network_calls_this_run"] == 2

def _planner_option(duration_s: int, route_name: str, timing_source: str, coverage: float) -> dict:
    return {
        "total_duration_s": duration_s,
        "total_window_p10_s": duration_s - 300,
        "total_window_p90_s": duration_s + 600,
        "legs": [
            {"type": "walk", "estimated_duration_s": 200},
            {"type": "board", "estimated_duration_s": 180},
            {
                "type": "ride",
                "route_name": route_name,
                "estimated_duration_s": duration_s - 500,
                "timing_source": timing_source,
                "observed_coverage": coverage,
            },
            {"type": "walk", "estimated_duration_s": 120},
        ],
    }


def _query_with_options(options: list[dict]) -> dict:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "mode": "live", "selected_rung": "4", "options": options})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return compare_mod.query_our_api(
            client, "http://127.0.0.1:3100", DEFAULT_OD_PAIRS[0], datetime(2026, 11, 24, 8, 30, tzinfo=IST)
        )


def test_comparator_compares_the_first_supported_option() -> None:
    res = _query_with_options(
        [
            _planner_option(1800, "505UP", "historical_fallback", 0.0),
            _planner_option(2400, "717AUP", "historical_fallback", 1.0),
        ]
    )
    assert res["our_status"] == "ok"
    assert res["our_option_index"] == 1
    assert res["our_routes"] == ["717AUP"]
    assert res["our_total_duration_s"] == 2400
    assert res["our_ride_observed_coverage"] == [1.0]
    assert compare_mod.reported_option_supported(res) is True

    unsupported = _query_with_options([_planner_option(1800, "505UP", "historical_fallback", 0.0)])
    assert unsupported["our_status"] == "unsupported_options"
    assert "our_total_duration_s" not in unsupported


def test_unsupported_reported_option_is_not_compared() -> None:
    od, slot = DEFAULT_OD_PAIRS[0], DEFAULT_TIME_SLOTS[0]
    depart = datetime(2026, 11, 24, 8, 30, tzinfo=IST)
    google = {
        "http_status": 200,
        "parsed": {
            "raw_route_count": 1,
            "bus_only_count": 1,
            "best_bus_route": {"duration_s": 3000, "route_names": ["425"], "vehicle_types": ["BUS"]},
        },
    }
    base = {
        "our_status": "ok",
        "our_total_duration_s": 3300,
        "our_window_p10_s": 3000,
        "our_window_p90_s": 3900,
        "our_ride_walk_duration_s": 3100,
    }
    cases = [
        # Frozen before per-ride coverage was recorded: classified from routes and sources.
        ({"our_routes": ["505UP"], "our_timing_sources": ["historical_fallback"]}, False),
        ({"our_routes": ["717EDOWN", "544UP"], "our_timing_sources": ["historical_fallback"]}, False),
        ({"our_routes": ["717AUP"], "our_timing_sources": ["historical_fallback"]}, True),
        ({"our_routes": ["502UP"], "our_timing_sources": ["model_live"]}, True),
        ({"our_routes": ["717AUP"], "our_timing_sources": ["timetable_fallback"]}, False),
        # Recorded per-ride coverage wins over the route-name rule.
        ({"our_routes": ["505UP"], "our_timing_sources": ["model_live"], "our_ride_observed_coverage": [0.0]}, False),
        ({"our_routes": ["505UP"], "our_timing_sources": ["model_live"], "our_ride_observed_coverage": [0.4]}, True),
    ]
    for extra, supported in cases:
        row, entry = compare_mod.evaluate_slot_comparison(od, slot, depart, {**base, **extra}, google)
        assert row["our_option_supported"] is supported, extra
        assert entry["our_option_supported"] is supported
        if supported:
            assert row["row_status"] == "compared"
            assert row["diff_s"] == 300
        else:
            assert row["row_status"] == "our_unsupported_option"
            assert row["our_total_duration_s"] is None
            assert row["diff_s"] is None
            assert "support gate" in row["note"]
