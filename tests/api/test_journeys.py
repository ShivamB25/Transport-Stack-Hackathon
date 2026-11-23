from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from ps1.api import create_app
from ps1.retime import TimingEstimate, WaitEstimate


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
        "from": {"name": "ISBT Kashmere Gate Terminal" if not second else "Central", "stop": {"gtfsId": "DTC:4109" if not second else "DTC:50"}},
        "to": {"name": "Central" if not second else "Nehru Place", "stop": {"gtfsId": "DTC:50" if not second else "DTC:2559"}},
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

    def predict_ride(self, route_id, from_stop, to_stop, scheduled, depart, mode, replay_delay_s=0):
        factor = 1.4 if 7 <= depart.hour <= 10 else 0.8
        duration = scheduled * factor + replay_delay_s
        return TimingEstimate(duration, duration - 60, duration + 120, "replay" if mode == "replay" else "historical", 0.5)

    def predict_wait(self, route_id, stop_id, scheduled, depart, mode):
        return WaitEstimate(60.0, 300.0, 300.0, "historical", 0.5)


def _client(tmp_path: Path) -> TestClient:
    gtfs = tmp_path / "gtfs"
    gtfs.mkdir()
    (gtfs / "stops.txt").write_text(
        """stop_id,stop_name,stop_lat,stop_lon
4109,ISBT Kashmere Gate Terminal,28.66696,77.227456
4730,ISBT Maharana Pratap Bus Terminal,28.6681691,77.2285861
2559,Nehru Place,28.5485761,77.2491332
2562,Nehru Place,28.5473227,77.2538969
"""
    )
    app = create_app(tmp_path, otp_url="http://unused", otp=FakeOTP(), model=StubModel())
    return TestClient(app)


def test_complete_options_and_ordered_legs(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get(
        "/journeys",
        params={"from": "28.6675,77.2282", "to": "28.5494,77.2510", "depart": "2026-10-22T08:30:00+05:30"},
    )
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
            assert leg["source"] in {"replay", "historical", "timetable"}
            if leg["type"] == "ride":
                assert leg["route_name"]
                assert leg["observed_coverage"] == 0.5


def test_stop_names_resolve_to_coordinate_endpoints(tmp_path: Path) -> None:
    client = _client(tmp_path)
    params = {"to": "Nehru Place", "depart": "2026-10-22T08:30:00+05:30"}
    named = client.get("/journeys", params={"from": "Kashmere Gate ISBT", **params}).json()
    coordinates = client.get("/journeys", params={"from": "28.6675,77.2282", "to": "28.5494,77.2510", "depart": params["depart"]}).json()
    assert named["resolved_from"]["stop_id"] == coordinates["resolved_from"]["stop_id"] == "4730"
    assert named["resolved_to"]["stop_id"] == coordinates["resolved_to"]["stop_id"] == "2559"


def test_time_of_day_and_replay_delay_change_ride_not_geometry(tmp_path: Path) -> None:
    client = _client(tmp_path)
    base = {"from": "Kashmere Gate ISBT", "to": "Nehru Place"}
    peak = client.get("/journeys", params={**base, "depart": "2026-10-22T09:00:00+05:30"}).json()
    quiet = client.get("/journeys", params={**base, "depart": "2026-10-22T23:00:00+05:30"}).json()
    peak_ride = next(leg for leg in peak["options"][0]["legs"] if leg["type"] == "ride")
    quiet_ride = next(leg for leg in quiet["options"][0]["legs"] if leg["type"] == "ride")
    assert peak_ride["estimated_duration_s"] != quiet_ride["estimated_duration_s"]
    delayed = client.get("/journeys", params={**base, "depart": "2026-10-22T09:00:00+05:30", "replay_delay_s": 300}).json()
    delayed_ride = next(leg for leg in delayed["options"][0]["legs"] if leg["type"] == "ride")
    assert delayed_ride["estimated_duration_s"] != peak_ride["estimated_duration_s"]
    assert delayed_ride["estimated_duration_s"] != delayed_ride["scheduled_duration_s"]
    assert [o["candidate_id"] for o in delayed["options"]] == [o["candidate_id"] for o in peak["options"]]
    assert delayed_ride["geometry"] == peak_ride["geometry"]


def test_invalid_inputs_are_controlled_and_service_recovers(tmp_path: Path) -> None:
    client = _client(tmp_path)
    base = {"from": "Kashmere Gate ISBT", "to": "Nehru Place", "depart": "2026-10-22T09:00:00+05:30"}
    for override, status in [
        ({"from": "not a real stop"}, 404),
        ({"from": "28.6,not-longitude"}, 400),
        ({"depart": "yesterday"}, 422),
        ({"depart": "2026-10-22T09:00:00"}, 422),
    ]:
        response = client.get("/journeys", params={**base, **override})
        assert response.status_code == status
        assert response.json()["detail"]
        assert "Traceback" not in response.text
    assert client.get("/journeys", params=base).status_code == 200


def test_openapi_documents_journey_contract(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    parameters = schema["paths"]["/journeys"]["get"]["parameters"]
    assert {parameter["name"] for parameter in parameters} >= {"from", "to", "depart"}
    leg = schema["components"]["schemas"]["Leg"]["properties"]
    assert {"source", "observed_coverage", "window_p10_s", "window_p90_s"} <= leg.keys()
