from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from delhi_transit.api.app import create_app
from delhi_transit.live.poller import (
    LivePoller,
    decode_vehicle_positions,
    persist_decoded_feed,
)
from delhi_transit.model.features import StretchError
from delhi_transit.planner.causal_context import TimingRecord
from delhi_transit.planner.rides import RetimeModel

IST = timezone(timedelta(hours=5, minutes=30))


def _make_stops(data_dir: Path) -> None:
    gtfs = data_dir / "gtfs"
    gtfs.mkdir(parents=True, exist_ok=True)
    (gtfs / "stops.txt").write_text(
        """stop_id,stop_name,stop_lat,stop_lon
4109,ISBT Kashmere Gate Terminal,28.66696,77.227456
4730,ISBT Maharana Pratap Bus Terminal,28.6681691,77.2285861
50,Central,28.6000,77.2350
2559,Nehru Place,28.5485761,77.2491332
""",
        encoding="utf-8",
    )


def _candidate(depart_iso: str) -> dict:
    start = datetime.fromisoformat(depart_iso)
    t1 = (start + timedelta(minutes=1)).isoformat()
    t2 = (start + timedelta(minutes=11)).isoformat()
    t3 = (start + timedelta(minutes=13)).isoformat()
    t4 = (start + timedelta(minutes=23)).isoformat()
    t5 = (start + timedelta(minutes=24)).isoformat()
    return {
        "legs": [
            {
                "mode": "WALK",
                "transitLeg": False,
                "duration": 60,
                "start": {"scheduledTime": start.isoformat()},
                "end": {"scheduledTime": t1},
                "from": {"name": "Origin", "stop": None},
                "to": {"name": "ISBT Kashmere Gate Terminal", "stop": {"gtfsId": "DTC:4109"}},
                "route": None,
                "trip": None,
                "legGeometry": {"points": "walk-1"},
            },
            {
                "mode": "BUS",
                "transitLeg": True,
                "duration": 600,
                "start": {"scheduledTime": t1},
                "end": {"scheduledTime": t2},
                "from": {"name": "ISBT Kashmere Gate Terminal", "stop": {"gtfsId": "DTC:4109"}},
                "to": {"name": "Central", "stop": {"gtfsId": "DTC:50"}},
                "route": {"gtfsId": "DTC:100", "shortName": "100UP"},
                "trip": {"gtfsId": "DTC:100_08_00"},
                "legGeometry": {"points": "ride-100"},
            },
            {
                "mode": "BUS",
                "transitLeg": True,
                "duration": 600,
                "start": {"scheduledTime": t3},
                "end": {"scheduledTime": t4},
                "from": {"name": "Central", "stop": {"gtfsId": "DTC:50"}},
                "to": {"name": "Nehru Place", "stop": {"gtfsId": "DTC:2559"}},
                "route": {"gtfsId": "DTC:200", "shortName": "200UP"},
                "trip": {"gtfsId": "DTC:200_08_15"},
                "legGeometry": {"points": "ride-200"},
            },
            {
                "mode": "WALK",
                "transitLeg": False,
                "duration": 60,
                "start": {"scheduledTime": t4},
                "end": {"scheduledTime": t5},
                "from": {"name": "Nehru Place", "stop": {"gtfsId": "DTC:2559"}},
                "to": {"name": "Destination", "stop": None},
                "route": None,
                "trip": None,
                "legGeometry": {"points": "walk-2"},
            },
        ]
    }


class FakeOTP:
    async def plan(self, origin, destination, depart):
        return [deepcopy(_candidate(depart.isoformat()))]


def _build_retime_model(data_dir: Path, records: list[TimingRecord]) -> RetimeModel:
    model = RetimeModel(data_dir, selection_path=data_dir / "no-selection.json")
    model.index_records(records)
    return model


def _stretch_record(
    day: str,
    hour: int,
    minute: int,
    route_id: int,
    from_stop: str,
    to_stop: str,
    actual_s: int,
    scheduled_s: int = 600,
) -> TimingRecord:
    y, m, d = (int(part) for part in day.split("-"))
    query = datetime(y, m, d, hour, minute, tzinfo=IST)
    return TimingRecord(
        StretchError(
            day=day,
            route_id=route_id,
            direction="UP",
            vehicle_id="DL1PD0001",
            run_id=f"{route_id}_{hour}_{minute}",
            from_sequence=1,
            to_sequence=2,
            from_stop_id=from_stop,
            to_stop_id=to_stop,
            query_ts=int(query.timestamp()),
            to_ts=int((query + timedelta(seconds=actual_s)).timestamp()),
            scheduled_duration_s=scheduled_s,
            actual_duration_s=actual_s,
            error_s=actual_s - scheduled_s,
            from_observed=True,
            to_observed=True,
        )
    )


def test_live_mode_reports_fresh_age_and_per_leg_provenance_including_sparse_leg(tmp_path: Path, make_protobuf) -> None:
    _make_stops(tmp_path)
    now = int(time.time())
    header_ts = now - 24
    # Route 100 has 12 active fresh vehicles; route 200 has 0 (sparse leg!)
    vehicles = [
        (f"DL1PD{1000 + i}", "100", 28.65, 77.22, 4.5, header_ts - 10)
        for i in range(12)
    ]
    decoded = decode_vehicle_positions(make_protobuf(header_ts, vehicles), polled_at=now)
    persist_decoded_feed(tmp_path, decoded)

    # Historical profile exists at hour 8, NOT at hour 3
    model = _build_retime_model(
        tmp_path,
        [_stretch_record("2026-10-15", 8, 10, 2171, "4109", "50", actual_s=780, scheduled_s=600)],
    )
    app = create_app(
        tmp_path,
        otp_url="http://unused",
        otp=FakeOTP(),
        model=model,
        enable_poller=False,
        stale_threshold_s=600,
        min_live_vehicles=5,
    )
    client = TestClient(app)

    morning = client.get(
        "/journeys",
        params={"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-11-24T08:30:00+05:30"},
    )
    assert morning.status_code == 200
    body = morning.json()
    assert body["status"] == "ok"
    assert body["mode"] == "live"
    assert 20.0 <= body["feed_age_seconds"] <= 40.0
    assert body["feed_summary"]["vehicle_count"] == 12

    rides = [leg for leg in body["options"][0]["legs"] if leg["type"] == "ride"]
    assert len(rides) == 2
    # Route 100 has live vehicles -> model_live
    assert rides[0]["route_id"] == 100
    assert rides[0]["source"] == "live"
    assert rides[0]["timing_source"] == "model_live"
    assert rides[0]["observation_count"] == 12
    assert rides[0]["observed_coverage"] == 1.0
    # Route 200 has 0 live vehicles, falls back to historical profile at hour 8
    assert rides[1]["route_id"] == 200
    assert rides[1]["source"] == "historical"
    assert rides[1]["timing_source"] == "historical_fallback"
    assert rides[1]["observed_coverage"] == 0.0

    # At hour 3 (no live vehicles on route 200 AND no historical observations) -> explicit timetable_fallback
    night = client.get(
        "/journeys",
        params={"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-11-24T03:00:00+05:30"},
    ).json()
    night_rides = [leg for leg in night["options"][0]["legs"] if leg["type"] == "ride"]
    assert night_rides[0]["timing_source"] == "model_live"
    assert night_rides[1]["source"] == "timetable"
    assert night_rides[1]["timing_source"] == "timetable_fallback"
    assert night_rides[1]["observation_count"] == 0
    assert night_rides[1]["observed_coverage"] == 0.0


def test_replay_mode_uses_october_observations_and_never_contacts_live_feed(tmp_path: Path) -> None:
    _make_stops(tmp_path)
    records = [
        _stretch_record("2026-10-22", 8, 5, 100, "4109", "50", actual_s=720, scheduled_s=600),
        _stretch_record("2026-10-22", 17, 5, 100, "4109", "50", actual_s=900, scheduled_s=600),
    ]
    model = _build_retime_model(tmp_path, records)

    class ExplodingPoller(LivePoller):
        def poll_once(self, client=None):
            raise AssertionError("replay mode must never contact live feed")

        async def apoll_once(self, client=None):
            raise AssertionError("replay mode must never contact live feed")

    app = create_app(
        tmp_path,
        otp_url="http://unused",
        otp=FakeOTP(),
        model=model,
        enable_poller=False,
        poller=ExplodingPoller(tmp_path, api_key="unused"),
    )
    client = TestClient(app)

    r_am = client.get(
        "/journeys",
        params={"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-10-22T08:30:00+05:30"},
    )
    r_pm = client.get(
        "/journeys",
        params={"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-10-22T17:30:00+05:30"},
    )
    assert r_am.status_code == 200
    assert r_pm.status_code == 200
    am = r_am.json()
    pm = r_pm.json()

    assert am["status"] == pm["status"] == "ok"
    assert am["mode"] == pm["mode"] == "replay"
    assert am["reference_timestamp"].startswith("2026-10-22T08:")
    assert pm["reference_timestamp"].startswith("2026-10-22T17:")
    assert am["reference_timestamp"] != pm["reference_timestamp"]
    assert am["feed_summary"]["live_feed_contacted"] is False
    assert pm["feed_summary"]["same_day_observations"] > am["feed_summary"]["same_day_observations"]

    am_rides = [leg for leg in am["options"][0]["legs"] if leg["type"] == "ride"]
    pm_rides = [leg for leg in pm["options"][0]["legs"] if leg["type"] == "ride"]
    # Route 100 has same-day October replay observations -> model_replay, different duration AM vs PM
    assert am_rides[0]["source"] == "replay"
    assert am_rides[0]["timing_source"] == "model_replay"
    assert pm_rides[0]["estimated_duration_s"] > am_rides[0]["estimated_duration_s"]
    # Route 200 has no same-day observations -> historical_fallback (sparse leg in replay)
    assert am_rides[1]["source"] == "historical"
    assert am_rides[1]["timing_source"] == "historical_fallback"

    # Explicit mode=replay with non-October date is cleanly rejected
    bad = client.get(
        "/journeys",
        params={
            "from": "Kashmere Gate ISBT",
            "to": "Nehru Place",
            "depart": "2026-11-24T08:30:00+05:30",
            "mode": "replay",
        },
    )
    assert bad.status_code == 422


def test_stale_feed_causes_explicit_abstention_and_recovers_when_resumed(tmp_path: Path, make_protobuf) -> None:
    _make_stops(tmp_path)
    now = int(time.time())
    fresh_ts = now - 3
    fresh_vehicles = [
        (f"DL1PD{2000 + i}", "100", 28.65, 77.22, 5.5, fresh_ts - 2)
        for i in range(15)
    ]
    decoded_fresh = decode_vehicle_positions(make_protobuf(fresh_ts, fresh_vehicles), polled_at=now)
    persist_decoded_feed(tmp_path, decoded_fresh)

    class MockResumePoller(LivePoller):
        async def apoll_once(self, client=None):
            t = int(time.time())
            pb = make_protobuf(
                t - 2,
                [(f"DL1PD{3000 + i}", "100", 28.65, 77.22, 6.0, t - 2) for i in range(15)],
            )
            return persist_decoded_feed(self.data_dir, decode_vehicle_positions(pb, polled_at=t))

    model = _build_retime_model(
        tmp_path,
        [_stretch_record("2026-10-15", 8, 10, 2171, "4109", "50", actual_s=720, scheduled_s=600)],
    )
    app = create_app(
        tmp_path,
        otp_url="http://unused",
        otp=FakeOTP(),
        model=model,
        enable_poller=False,
        poller=MockResumePoller(tmp_path, api_key="mock-key"),
        stale_threshold_s=10.0,
        min_live_vehicles=10,
    )
    client = TestClient(app)
    params = {"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-11-24T08:30:00+05:30"}

    # 1. Fresh feed -> live options returned
    ok_resp = client.get("/journeys", params=params).json()
    assert ok_resp["status"] == "ok"
    assert ok_resp["mode"] == "live"
    assert len(ok_resp["options"]) >= 1
    assert ok_resp["abstention"] is None

    # 2. Pause feed + age snapshot beyond 10s threshold -> explicit not enough data abstention
    pause_resp = client.post("/live/pause")
    assert pause_resp.status_code == 200
    assert pause_resp.json()["paused"] is True

    stale_ts = int(time.time()) - 45
    stale_vehicles = [
        (f"DL1PD{2000 + i}", "100", 28.65, 77.22, 5.5, stale_ts - 2)
        for i in range(15)
    ]
    persist_decoded_feed(tmp_path, decode_vehicle_positions(make_protobuf(stale_ts, stale_vehicles), polled_at=stale_ts + 2))

    abstain_resp = client.get("/journeys", params=params)
    assert abstain_resp.status_code == 200
    abstained = abstain_resp.json()
    assert abstained["status"] == "insufficient_data"
    assert abstained["mode"] == "live"
    assert abstained["options"] == []
    assert abstained["feed_age_seconds"] >= 40.0
    assert abstained["abstention"]["code"] == "stale_feed"
    assert abstained["abstention"]["feed_age_seconds"] >= 40.0

    # 3. Restore feed via /live/resume -> immediate recovery to live options
    resume_resp = client.post("/live/resume")
    assert resume_resp.status_code == 200
    assert resume_resp.json()["paused"] is False

    recovered = client.get("/journeys", params=params).json()
    assert recovered["status"] == "ok"
    assert recovered["mode"] == "live"
    assert len(recovered["options"]) >= 1
    assert recovered["feed_age_seconds"] <= 10.0
    assert recovered["abstention"] is None


def test_insufficient_fleet_and_route_coverage_abstention(tmp_path: Path, make_protobuf) -> None:
    _make_stops(tmp_path)
    model = _build_retime_model(tmp_path, [])
    app = create_app(
        tmp_path,
        otp_url="http://unused",
        otp=FakeOTP(),
        model=model,
        enable_poller=False,
        stale_threshold_s=600.0,
        min_live_vehicles=10,
    )
    client = TestClient(app)
    params = {"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-11-24T08:30:00+05:30"}

    # 1. Missing feed state -> feed_unavailable abstention
    missing = client.get("/journeys", params=params).json()
    assert missing["status"] == "insufficient_data"
    assert missing["options"] == []
    assert missing["abstention"]["code"] == "feed_unavailable"

    # 2. Fresh feed but only 3 vehicles (< min_live_vehicles=10) -> insufficient_coverage abstention
    now = int(time.time())
    tiny = make_protobuf(now - 5, [(f"DL1PD{i}", "100", 28.65, 77.22, 5.0, now - 5) for i in range(3)])
    persist_decoded_feed(tmp_path, decode_vehicle_positions(tiny, polled_at=now))
    low_fleet = client.get("/journeys", params=params).json()
    assert low_fleet["status"] == "insufficient_data"
    assert low_fleet["options"] == []
    assert low_fleet["abstention"]["code"] == "insufficient_coverage"

    # 3. Adequate fleet on route 100 (12 vehicles) + sparse route 200 (0 vehicles) => avg coverage 0.5
    adequate = make_protobuf(now - 5, [(f"DL1PD{i}", "100", 28.65, 77.22, 5.0, now - 5) for i in range(12)])
    persist_decoded_feed(tmp_path, decode_vehicle_positions(adequate, polled_at=now))
    high_floor = client.get("/journeys", params={**params, "min_coverage": 0.9}).json()
    assert high_floor["status"] == "insufficient_data"
    assert high_floor["options"] == []
    assert high_floor["abstention"]["code"] == "insufficient_route_coverage"

    # Stock settings still allow this journey because route 100 is supported and route 200 is labelled sparse.
    supported = client.get("/journeys", params=params).json()
    assert supported["status"] == "ok"
    sparse = next(
        leg for leg in supported["options"][0]["legs"] if leg["type"] == "ride" and leg["route_id"] == 200
    )
    assert sparse["timing_source"] in {"historical_fallback", "timetable_fallback"}
    assert sparse["observed_coverage"] == 0.0


def test_fresh_header_cannot_revive_stale_route_positions_at_default_settings(tmp_path: Path, make_protobuf) -> None:
    _make_stops(tmp_path)
    now = int(time.time())
    payload = make_protobuf(
        now,
        [(f"OLD{i}", "100", 28.65, 77.22, 5.0, now - 660) for i in range(12)]
        + [(f"FRESH{i}", "200", 28.65, 77.22, 5.0, now - 5) for i in range(10)],
    )
    decoded = decode_vehicle_positions(payload, polled_at=now)
    state = persist_decoded_feed(tmp_path, decoded)

    assert state["fresh_vehicle_count"] == 10
    route_stats = json.loads((tmp_path / "live" / "route_stats.json").read_text())
    assert route_stats["routes"]["100"]["fresh_vehicle_count"] == 0
    assert route_stats["routes"]["100"]["median_speed_mps"] == 0.0
    assert route_stats["routes"]["100"]["latest_ts"] == 0

    app = create_app(
        tmp_path,
        otp_url="http://unused",
        otp=FakeOTP(),
        model=_build_retime_model(tmp_path, []),
        enable_poller=False,
        min_live_vehicles=10,
    )
    client = TestClient(app)
    params = {"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-11-24T08:30:00+05:30"}
    body = client.get("/journeys", params=params).json()
    assert body["status"] == "ok"
    rides = [leg for leg in body["options"][0]["legs"] if leg["type"] == "ride"]
    assert rides[0]["route_id"] == 100
    assert rides[0]["timing_source"] == "timetable_fallback"
    assert rides[1]["route_id"] == 200
    assert rides[1]["timing_source"] == "model_live"

    # Move the fresh fleet to an unrelated route: stock settings now abstain because neither candidate leg is supported.
    unsupported = make_protobuf(
        now,
        [(f"OLD{i}", "100", 28.65, 77.22, 5.0, now - 660) for i in range(12)]
        + [(f"FRESH{i}", "999", 28.65, 77.22, 5.0, now - 5) for i in range(10)],
    )
    persist_decoded_feed(tmp_path, decode_vehicle_positions(unsupported, polled_at=now + 1))
    body = client.get("/journeys", params=params).json()
    assert body["status"] == "insufficient_data"
    assert body["abstention"]["code"] == "insufficient_route_coverage"
    assert body["abstention"]["supported_options"] == 0
    assert body["options"] == []


def test_entities_that_expire_after_the_poll_cannot_drive_live_timing(tmp_path: Path, make_protobuf) -> None:
    _make_stops(tmp_path)
    now = int(time.time())
    polled_at = now - 30
    # Fresh when polled (590 s old) but 620 s old by request time; route 200 stays fresh.
    payload = make_protobuf(
        polled_at,
        [(f"AGING{i}", "100", 28.65, 77.22, 5.0, now - 620) for i in range(12)]
        + [(f"FRESH{i}", "200", 28.65, 77.22, 5.0, now - 35) for i in range(10)],
    )
    state = persist_decoded_feed(tmp_path, decode_vehicle_positions(payload, polled_at=polled_at))
    assert state["fresh_vehicle_count"] == 22

    app = create_app(
        tmp_path,
        otp_url="http://unused",
        otp=FakeOTP(),
        model=_build_retime_model(tmp_path, []),
        enable_poller=False,
        min_live_vehicles=10,
    )
    client = TestClient(app)
    params = {"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-11-24T08:30:00+05:30"}
    body = client.get("/journeys", params=params).json()
    assert body["status"] == "ok"
    assert body["feed_summary"]["fresh_vehicle_count"] == 10
    rides = [leg for leg in body["options"][0]["legs"] if leg["type"] == "ride"]
    assert rides[0]["route_id"] == 100
    assert rides[0]["timing_source"] == "timetable_fallback"
    assert rides[1]["route_id"] == 200
    assert rides[1]["timing_source"] == "model_live"

    # When the whole fleet has aged out since the poll, the request abstains on coverage.
    aging_only = make_protobuf(
        polled_at, [(f"AGING{i}", "100", 28.65, 77.22, 5.0, now - 620) for i in range(12)]
    )
    persist_decoded_feed(tmp_path, decode_vehicle_positions(aging_only, polled_at=polled_at + 1))
    body = client.get("/journeys", params=params).json()
    assert body["status"] == "insufficient_data"
    assert body["abstention"]["code"] == "insufficient_coverage"
    assert body["abstention"]["fresh_vehicle_count"] == 0

    # An app-level freshness threshold governs route timing as well as the fleet gate.
    now = int(time.time())
    short_limit = make_protobuf(
        now - 10,
        [(f"AGING{i}", "100", 28.65, 77.22, 5.0, now - 50) for i in range(12)]
        + [(f"FRESH{i}", "999", 28.65, 77.22, 5.0, now - 12) for i in range(10)],
    )
    persist_decoded_feed(tmp_path, decode_vehicle_positions(short_limit, polled_at=now - 10))
    shared_model = _build_retime_model(tmp_path, [])
    strict_app, lenient_app = (
        create_app(
            tmp_path,
            otp_url="http://unused",
            otp=FakeOTP(),
            model=shared_model,
            enable_poller=False,
            stale_threshold_s=threshold,
            min_live_vehicles=10,
        )
        for threshold in (45.0, 600.0)
    )
    lenient = TestClient(lenient_app).get("/journeys", params=params).json()
    assert lenient["status"] == "ok"
    lenient_rides = [leg for leg in lenient["options"][0]["legs"] if leg["type"] == "ride"]
    assert lenient_rides[0]["route_id"] == 100
    assert lenient_rides[0]["timing_source"] == "model_live"
    body = TestClient(strict_app).get("/journeys", params=params).json()
    assert body["feed_summary"]["fresh_vehicle_count"] == 10
    assert body["status"] == "insufficient_data"
    assert body["abstention"]["code"] == "insufficient_route_coverage"


def test_replay_without_loaded_observations_abstains_without_fresh_timestamp(tmp_path: Path) -> None:
    _make_stops(tmp_path)
    raw_gps = tmp_path / "gps" / "day=2026-10-01.parquet"
    raw_gps.parent.mkdir(parents=True)
    raw_gps.write_bytes(b"raw GPS is not a reconstructed passage fixture")
    model = _build_retime_model(tmp_path, [])
    app = create_app(
        tmp_path,
        otp_url="http://unused",
        otp=FakeOTP(),
        model=model,
        enable_poller=False,
    )

    body = TestClient(app).get(
        "/journeys",
        params={
            "from": "Kashmere Gate ISBT",
            "to": "Nehru Place",
            "depart": "2026-10-01T08:30:00+05:30",
            "mode": "replay",
        },
    ).json()
    assert body["status"] == "insufficient_data"
    assert body["abstention"]["code"] == "replay_data_unavailable"
    assert body["feed_timestamp"] is None
    assert body["reference_timestamp"] is None
    assert body["feed_age_seconds"] is None
    assert body["feed_summary"]["visible_observations"] == 0
    assert body["feed_summary"]["replay_data_available"] is False
    assert body["feed_summary"]["fixture_path"] is None
    assert body["feed_summary"]["loaded_passage_files"] == []


def test_live_ride_without_usable_speed_uses_fallback_provenance(tmp_path: Path, make_protobuf) -> None:
    _make_stops(tmp_path)
    now = int(time.time())
    vehicles = [(f"DL1PD{i}", "100", 28.65, 77.22, 0.0, now - 5) for i in range(12)]
    persist_decoded_feed(tmp_path, decode_vehicle_positions(make_protobuf(now, vehicles), polled_at=now))
    model = _build_retime_model(
        tmp_path,
        [_stretch_record("2026-10-15", 8, 10, 100, "4109", "50", actual_s=720, scheduled_s=600)],
    )
    app = create_app(
        tmp_path,
        otp_url="http://unused",
        otp=FakeOTP(),
        model=model,
        enable_poller=False,
        min_live_vehicles=10,
    )
    body = TestClient(app).get(
        "/journeys",
        params={"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-11-24T08:30:00+05:30"},
    ).json()

    assert body["status"] == "ok"
    legs = body["options"][0]["legs"]
    first_ride = next(leg for leg in legs if leg["type"] == "ride" and leg["route_id"] == 100)
    second_ride = next(leg for leg in legs if leg["type"] == "ride" and leg["route_id"] == 200)
    board = next(leg for leg in legs if leg["type"] == "board")
    assert first_ride["timing_source"] == "historical_fallback"
    assert first_ride["source"] == "historical"
    assert first_ride["observation_count"] == 1
    assert second_ride["timing_source"] == "historical_fallback"
    assert second_ride["observed_coverage"] == 0.0
    # Live headway can still time the wait independently of the ride fallback.
    assert board["timing_source"] == "model_live"


def test_transport_exception_never_persists_or_exposes_credential(tmp_path: Path, caplog) -> None:
    _make_stops(tmp_path)
    secret_key = "test-credential-with-line-break\nnever-expose-this-value"

    def failing_transport(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Illegal header value containing {secret_key}")

    poller = LivePoller(tmp_path, feed_url="https://example.test/vp.pb", api_key=secret_key)
    with httpx.Client(transport=httpx.MockTransport(failing_transport)) as http_client:
        result = poller.poll_once(client=http_client)

    assert result["last_error"] == "transport_error"
    app = create_app(
        tmp_path,
        otp_url="http://unused",
        otp=FakeOTP(),
        model=_build_retime_model(tmp_path, []),
        enable_poller=False,
        poller=poller,
    )
    endpoint = TestClient(app).get("/live/status")
    assert endpoint.status_code == 200
    assert endpoint.json()["state"]["last_error"] == "transport_error"

    exposed_text = "\n".join(
        [
            json.dumps(result),
            endpoint.text,
            caplog.text,
            (tmp_path / "live" / "feed_state.json").read_text(encoding="utf-8"),
            (tmp_path / "live" / "poller.log").read_text(encoding="utf-8"),
        ]
    )
    assert secret_key not in exposed_text
    assert "Illegal header value" not in exposed_text
