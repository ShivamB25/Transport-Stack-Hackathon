from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from delhi_transit.api import replay_demo
from delhi_transit.api.app import create_app
from delhi_transit.core import config
from delhi_transit.planner.provenance import TimingEstimate, WaitEstimate
from delhi_transit.planner.risk import (
    percentile,
    ratio_interval,
    simulate_joint_scenarios,
)


def _candidate_with_coords() -> dict:
    return {
        "duration": 3793,
        "legs": [
            {
                "mode": "BUS",
                "transitLeg": True,
                "duration": 1607,
                "start": {"scheduledTime": "2026-10-22T17:09:00+05:30"},
                "end": {"scheduledTime": "2026-10-22T17:28:47+05:30"},
                "from": {
                    "name": "Badarpur Border (T)",
                    "lat": 28.49313,
                    "lon": 77.30373,
                    "stop": {"gtfsId": "DTC:2640"},
                },
                "to": {
                    "name": "Hamdard Nagar / Sangam Vihar",
                    "lat": 28.511924,
                    "lon": 77.249214,
                    "stop": {"gtfsId": "DTC:2300"},
                },
                "route": {"gtfsId": "DTC:2171", "shortName": "717AUP"},
                "trip": {"gtfsId": "DTC:2171_FULL_17_09"},
                "legGeometry": {"points": "encoded-717a"},
            },
            {
                "mode": "BUS",
                "transitLeg": True,
                "duration": 751,
                "start": {"scheduledTime": "2026-10-22T17:32:42+05:30"},
                "end": {"scheduledTime": "2026-10-22T18:22:35+05:30"},
                "from": {
                    "name": "Hamdard Nagar / Sangam Vihar",
                    "lat": 28.511924,
                    "lon": 77.249214,
                    "stop": {"gtfsId": "DTC:2300"},
                },
                "to": {
                    "name": "Hauz Khas",
                    "lat": 28.5494,
                    "lon": 77.2060,
                    "stop": {"gtfsId": "DTC:2559"},
                },
                "route": {"gtfsId": "DTC:1416", "shortName": "OMS(+)BURARI-DHAULAKUAN"},
                "trip": {"gtfsId": "DTC:1416_FULL_15_08"},
                "legGeometry": {"points": "encoded-d7702"},
            },
        ],
    }


def _direct_candidate() -> dict:
    return {
        "duration": 3930,
        "legs": [
            {
                "mode": "BUS",
                "transitLeg": True,
                "duration": 2612,
                "start": {"scheduledTime": "2026-10-22T17:43:33+05:30"},
                "end": {"scheduledTime": "2026-10-22T18:27:05+05:30"},
                "from": {
                    "name": "Jaitpur Crossing",
                    "lat": 28.498,
                    "lon": 77.300,
                    "stop": {"gtfsId": "DTC:2640"},
                },
                "to": {
                    "name": "Hauz Khas",
                    "lat": 28.5494,
                    "lon": 77.2060,
                    "stop": {"gtfsId": "DTC:2559"},
                },
                "route": {"gtfsId": "DTC:785", "shortName": "774UP"},
                "trip": {"gtfsId": "DTC:785_17_43"},
                "legGeometry": {"points": "encoded-774"},
            }
        ],
    }


def _substitute_candidate() -> dict:
    """Same first bus and transfer stop, different connecting trip (448DOWN)."""
    cand = _candidate_with_coords()
    cand["duration"] = 3700
    connecting = cand["legs"][1]
    connecting["route"] = {"gtfsId": "DTC:1957", "shortName": "448DOWN"}
    connecting["trip"] = {"gtfsId": "DTC:1957_FULL_17_30"}
    connecting["start"] = {"scheduledTime": "2026-10-22T17:30:00+05:30"}
    return cand


class StubOTP:
    def __init__(self, candidates: list[dict] | None = None) -> None:
        self.candidates = candidates or [_candidate_with_coords(), _direct_candidate()]

    async def plan(self, origin, destination, depart):
        return deepcopy(self.candidates)


class StubModel:
    selected_rung = "1"

    def predict_ride(
        self,
        route_id,
        from_stop,
        to_stop,
        scheduled,
        depart,
        mode,
        replay_delay_s=0,
        as_of=None,
        stale_threshold_s=None,
    ):
        return TimingEstimate(scheduled + replay_delay_s, scheduled * 0.9, scheduled * 1.2, "replay", 0.75)

    def predict_wait(self, route_id, stop_id, scheduled, depart, mode, as_of=None, stale_threshold_s=None):
        return WaitEstimate(60.0, 300.0, 300.0, "historical", 0.5)

    def replay_reference_info(self, depart):
        return {
            "replay_data_available": True,
            "same_day_data_available": True,
            "feed_age_seconds": 30.0,
            "feed_timestamp": depart.isoformat(),
            "reference_timestamp": depart.isoformat(),
            "fixture_path": None,
            "loaded_passage_files": [],
            "visible_observations": 1,
            "same_day_observations": 1,
            "observed_same_day_stretches": 1,
        }


def _make_client(tmp_path: Path, otp: StubOTP | None = None) -> TestClient:
    gtfs = tmp_path / "gtfs"
    gtfs.mkdir(parents=True, exist_ok=True)
    (gtfs / "stops.txt").write_text(
        """stop_id,stop_name,stop_lat,stop_lon
4730,ISBT Maharana Pratap Bus Terminal,28.6681691,77.2285861
2559,Nehru Place,28.5485761,77.2491332
2640,Badarpur Border (T),28.49313,77.30373
2300,Hamdard Nagar / Sangam Vihar,28.511924,77.249214
""",
        encoding="utf-8",
    )
    app = create_app(tmp_path, otp_url="http://unused", otp=otp or StubOTP(), model=StubModel(), enable_poller=False)
    return TestClient(app)


def test_root_serves_built_frontend_and_assets_stay_inside_dist(tmp_path: Path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    monkeypatch.setattr(config, "FRONTEND_DIR", dist)
    client = _make_client(tmp_path)

    unbuilt = client.get("/")
    assert unbuilt.status_code == 200
    assert unbuilt.headers["content-type"].startswith("text/plain")

    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    (dist / "assets" / "app.js").write_text("export {}", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("not public", encoding="utf-8")

    page = client.get("/")
    assert "text/html" in page.headers["content-type"]
    assert 'id="root"' in page.text
    script = client.get("/assets/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert client.get("/assets/%2e%2e/%2e%2e/outside.txt").status_code == 404
    assert client.get("/favicon.ico").status_code == 204

def test_replay_demo_shows_initial_recommendation_flip_and_outcome(tmp_path: Path, monkeypatch) -> None:
    joint_runs: list[tuple[list[dict], list[list[float]]]] = []

    def recording_simulation(events):
        scenarios, missed = simulate_joint_scenarios(events)
        joint_runs.append((events, scenarios))
        return scenarios, missed

    monkeypatch.setattr(replay_demo, "simulate_joint_scenarios", recording_simulation)
    client = _make_client(tmp_path)
    resp = client.get("/replay/demo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "replay"
    assert body["live_feed_contacted"] is False

    stages = body["stages"]
    assert len(stages) == 4
    assert stages[0]["recommended_option"] == "transfer"
    assert stages[0]["advice_flipped"] is False
    assert stages[2]["recommended_option"] == "direct"
    assert stages[2]["advice_flipped"] is True
    assert stages[2]["observed_delay_s"] > stages[2]["scheduled_transfer_gap_s"]
    assert stages[3]["outcome_revealed"] is True
    assert stages[3]["actual_outcome"] == "missed"
    assert stages[3]["missed_by_s"] > 0

    delays = [s["observed_delay_s"] for s in stages]
    # The first three stages grow monotonically; the observed final stop can recover slightly.
    assert delays[0] < delays[1] < delays[2]
    assert delays[3] > delays[0]

    # The direct window is the p10/p90 of joint scenario totals over the direct itinerary
    # (walks fixed, the 774UP ride drawn from the planner's ride window), not fixed offsets.
    [(events, scenarios)] = joint_runs
    ride_estimate_s = 2612 + replay_demo.DIRECT_RIDE_ALLOWANCE_S
    assert [event["kind"] for event in events] == ["fixed", "ride"]
    assert events[0]["duration"] == 3930 - 2612
    ride = events[1]["estimate"]
    assert (ride.duration_s, ride.p10_s, ride.p90_s) == (ride_estimate_s, *ratio_interval([], ride_estimate_s))
    totals = [sum(scenario) for scenario in scenarios]
    for stage in stages:
        direct = stage["direct_option"]
        assert (direct["window_p10_s"], direct["window_p90_s"]) == (
            round(percentile(totals, 0.1)),
            round(percentile(totals, 0.9)),
        )
        assert (direct["window_p10_s"], direct["window_p90_s"]) != (
            direct["estimated_duration_s"] - 300,
            direct["estimated_duration_s"] + 420,
        )
        assert direct["window_p10_s"] < direct["estimated_duration_s"] < direct["window_p90_s"]

    # The revealed outcome is a single time, not an invented window.
    actual = stages[3]["transfer_option"]
    assert actual["window_p10_s"] == actual["window_p90_s"] == actual["estimated_duration_s"]


def test_replay_demo_selects_scripted_pair_regardless_of_candidate_order(tmp_path: Path) -> None:
    reordered = StubOTP([_substitute_candidate(), _direct_candidate(), _candidate_with_coords()])
    body = _make_client(tmp_path, reordered).get("/replay/demo").json()
    assert body["status"] == "ok"
    assert body["transfer_stop"]["connecting_scheduled_depart"].endswith("17:32:42+05:30")
    assert [leg["route_name"] for leg in body["geometries"]["transfer_legs"]] == [
        "717AUP",
        "OMS(+)BURARI-DHAULAKUAN",
    ]

    substitute_only = StubOTP([_substitute_candidate(), _direct_candidate()])
    resp = _make_client(tmp_path, substitute_only).get("/replay/demo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["stages"] == []
    assert body["message"]


def test_replay_demo_uses_real_oct22_parquet_when_available() -> None:
    real_data = Path("/home/factory-user/ps1-scratch/data")
    if not (real_data / "passages" / "day=2026-10-22.parquet").exists():
        pytest.skip("real October 22 passages are not available")
    app = create_app(real_data, otp_url="http://unused", otp=StubOTP(), model=StubModel(), enable_poller=False)
    client = TestClient(app)
    body = client.get("/replay/demo").json()
    assert body["fixture_source"].endswith("day=2026-10-22.parquet")
    assert body["connecting_fixture_source"].endswith("day=2026-10-22.parquet")
    transfer = body["transfer_stop"]
    assert transfer["actual_outcome"] == "missed"
    assert transfer["actual_delay_s"] == 2070
    assert transfer["missed_by_s"] == 1663
    assert transfer["actual_arrival_observed"] is True
    assert transfer["connecting_observed"] is True
    assert transfer["connecting_vehicle_id"] == "DL51EV4293"
    assert transfer["connecting_observed_depart"].endswith("17:35:34+05:30")
    assert transfer["connecting_scheduled_depart"].endswith("17:32:42+05:30")
    assert [stage["observed_delay_s"] for stage in body["stages"]] == [165, 1768, 2133, 2070]

