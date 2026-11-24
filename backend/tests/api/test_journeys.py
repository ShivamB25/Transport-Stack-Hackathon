from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from delhi_transit.api.app import create_app
from delhi_transit.core.config import IST
from delhi_transit.planner.provenance import TimingEstimate, WaitEstimate


def _walk(start: str, end: str, destination: bool = False) -> dict:
    return {
        "mode": "WALK",
        "transitLeg": False,
        "duration": 60,
        "start": {"scheduledTime": start},
        "end": {"scheduledTime": end},
        "from": {"name": "Origin", "stop": None},
        "to": {"name": "Destination" if destination else "ISBT Kashmere Gate Terminal", "stop": None if destination else {"gtfsId": "DTC:4109"}},
        "route": None,
        "trip": None,
        "legGeometry": {"points": "walk-geometry"},
    }


def _ride(start: str, end: str, route: str = "100", second: bool = False) -> dict:
    return {
        "mode": "BUS",
        "transitLeg": True,
        "duration": 600,
        "start": {"scheduledTime": start},
        "end": {"scheduledTime": end},
        "from": {
            "name": "ISBT Kashmere Gate Terminal" if not second else "Central",
            "lat": 28.66696 if not second else 28.6,
            "lon": 77.227456 if not second else 77.22,
            "stop": {"gtfsId": "DTC:4109" if not second else "DTC:50"},
        },
        "to": {
            "name": "Central" if not second else "Nehru Place",
            "lat": 28.6 if not second else 28.5485761,
            "lon": 77.22 if not second else 77.2491332,
            "stop": {"gtfsId": "DTC:50" if not second else "DTC:2559"},
        },
        "route": {"gtfsId": f"DTC:{route}", "shortName": f"{route}UP"},
        "trip": {"gtfsId": f"DTC:{route}_FULL_08_00"},
        "legGeometry": {"points": f"ride-{route}"},
    }


def _candidate(transfer: bool = True) -> dict:
    legs = [
        _walk("2026-10-22T08:29:00+05:30", "2026-10-22T08:30:00+05:30"),
        _ride("2026-10-22T08:30:00+05:30", "2026-10-22T08:40:00+05:30"),
    ]
    if transfer:
        legs.append(_ride("2026-10-22T08:42:00+05:30", "2026-10-22T08:52:00+05:30", "200", True))
    legs.append(_walk("2026-10-22T08:52:00+05:30", "2026-10-22T08:53:00+05:30", True))
    return {"legs": legs}


class FakeOTP:
    async def plan(self, origin, destination, depart):
        return [deepcopy(_candidate()), deepcopy(_candidate(False))]


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
        factor = 1.4 if 7 <= depart.hour <= 10 else 0.8
        duration = scheduled * factor + replay_delay_s
        return TimingEstimate(duration, duration - 60, duration + 120, "replay" if mode == "replay" else "historical", 0.5)

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


def _client(tmp_path: Path) -> TestClient:
    gtfs = tmp_path / "gtfs"
    gtfs.mkdir()
    (gtfs / "stops.txt").write_text(
        """stop_id,stop_name,stop_lat,stop_lon
4109,ISBT Kashmere Gate Terminal,28.66696,77.227456
4730,ISBT Maharana Pratap Bus Terminal,28.6681691,77.2285861
2559,Nehru Place,28.5485761,77.2491332
2562,Nehru Place,28.5473227,77.2538969
10032,"BLOCK 43,45 SOUTH PATEL NAGAR",28.6512,77.1694
"""
    )
    app = create_app(tmp_path, otp_url="http://unused", otp=FakeOTP(), model=StubModel())
    return TestClient(app)


def test_complete_options_and_ordered_legs(tmp_path: Path) -> None:
    client = _client(tmp_path)
    params = {"from": "28.6675,77.2282", "to": "28.5494,77.2510", "depart": "2026-10-22T08:30:00+05:30"}
    response = client.get("/journeys", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mode"] == "replay"
    assert len(body["options"]) == 2
    transferred = next(option for option in body["options"] if any(leg["type"] == "transfer" for leg in option["legs"]))
    kinds = [leg["type"] for leg in transferred["legs"]]
    assert kinds == ["walk", "board", "ride", "alight", "transfer", "board", "ride", "alight", "walk"]
    for option in body["options"]:
        assert option["total_window_p10_s"] <= option["total_duration_s"] <= option["total_window_p90_s"]
        assert option["quieter_hour_typical_s"] > 0
        for leg in option["legs"]:
            if leg["type"] == "ride":
                assert leg["route_name"]
                assert None not in (leg["from_lat"], leg["from_lng"], leg["to_lat"], leg["to_lng"])

    # A replay delay re-times rides but never changes the candidates or their geometry.
    delayed = client.get("/journeys", params={**params, "replay_delay_s": 300}).json()
    assert [o["candidate_id"] for o in delayed["options"]] == [o["candidate_id"] for o in body["options"]]
    assert [[leg["geometry"] for leg in o["legs"]] for o in delayed["options"]] == [
        [leg["geometry"] for leg in o["legs"]] for o in body["options"]
    ]


def test_exact_stop_name_with_comma_resolves_before_coordinate_parsing(tmp_path: Path) -> None:
    client = _client(tmp_path)
    params = {"to": "Nehru Place", "depart": "2026-10-22T08:30:00+05:30"}
    named = client.get("/journeys", params={"from": "Kashmere Gate ISBT", **params}).json()
    coordinates = client.get("/journeys", params={"from": "28.6675,77.2282", "to": "28.5494,77.2510", "depart": params["depart"]}).json()
    assert named["resolved_from"]["stop_id"] == coordinates["resolved_from"]["stop_id"] == "4730"
    assert named["resolved_to"]["stop_id"] == coordinates["resolved_to"]["stop_id"] == "2559"

    response = client.get(
        "/journeys",
        params={
            "from": "BLOCK 43,45 SOUTH PATEL NAGAR",
            "to": "Nehru Place",
            "depart": "2026-10-22T08:30:00+05:30",
        },
    )
    assert response.status_code == 200
    assert response.json()["resolved_from"]["stop_id"] == "10032"
    assert response.json()["resolved_from"]["stop_name"] == "BLOCK 43,45 SOUTH PATEL NAGAR"


def test_equivalent_offsets_use_identical_delhi_service_clock(tmp_path: Path) -> None:
    client = _client(tmp_path)
    params = {"from": "Kashmere Gate ISBT", "to": "Nehru Place"}
    utc = client.get(
        "/journeys",
        params={**params, "depart": "2026-09-30T19:00:00Z"},
    )
    delhi = client.get(
        "/journeys",
        params={**params, "depart": "2026-10-01T00:30:00+05:30"},
    )
    assert utc.status_code == delhi.status_code == 200
    assert utc.json() == delhi.json()
    assert utc.json()["mode"] == "replay"
    assert utc.json()["depart"] == "2026-10-01T00:30:00+05:30"


def test_invalid_inputs_are_controlled_and_service_recovers(tmp_path: Path) -> None:
    client = _client(tmp_path)
    base = {"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-10-22T09:00:00+05:30"}
    for override, status in [
        ({"from": "not a real stop"}, 404),
        ({"from": "28.6,not-longitude"}, 400),
        ({"depart": "yesterday"}, 422),
        ({"depart": "2026-10-22T09:00:00"}, 422),
        ({"depart": "9999-12-31T23:00:00Z"}, 422),
    ]:
        response = client.get("/journeys", params={**base, **override})
        assert response.status_code == status
        assert response.json()["detail"]
        assert "Traceback" not in response.text
    assert client.get("/journeys", params=base).status_code == 200


def test_missing_depart_defaults_to_now_in_delhi_live_mode(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    endpoints = {"from": "Kashmere Gate ISBT", "to": "Nehru Place"}
    before = datetime.now(IST).replace(microsecond=0)
    response = client.get("/journeys", params=endpoints)
    after = datetime.now(IST)
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "live"
    depart = datetime.fromisoformat(body["depart"])
    assert depart.utcoffset() == IST.utcoffset(None)
    assert before <= depart <= after

    assert client.get("/journeys", params={**endpoints, "mode": "replay"}).json()["depart"] == (
        "2026-10-22T08:30:00+05:30"
    )
    monkeypatch.setenv("DELHI_TRANSIT_MODE", "replay")
    replay = client.get("/journeys", params=endpoints)
    assert replay.status_code == 200
    assert replay.json()["mode"] == "replay"
    assert replay.json()["depart"] == "2026-10-22T08:30:00+05:30"
    assert client.get("/journeys", params={**endpoints, "mode": "live"}).json()["mode"] == "live"
