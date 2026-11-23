from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ps1.features import PassageRow, StretchError
from ps1.retime import (
    RetimeModel,
    TimingEstimate,
    TimingRecord,
    WaitEstimate,
    retime_itinerary,
)

IST = timezone(timedelta(hours=5, minutes=30))
REQUEST = datetime(2026, 10, 22, 8, 30, tzinfo=IST)


def _place(name: str, stop_id: str, lat: float, lng: float) -> dict:
    return {
        "name": name,
        "lat": lat,
        "lon": lng,
        "stop": {"gtfsId": f"DTC:{stop_id}"},
    }


def _ride(
    start: str,
    end: str,
    route_id: int,
    from_place: dict,
    to_place: dict,
) -> dict:
    return {
        "mode": "BUS",
        "transitLeg": True,
        "duration": 600,
        "start": {"scheduledTime": start},
        "end": {"scheduledTime": end},
        "from": from_place,
        "to": to_place,
        "route": {"gtfsId": f"DTC:{route_id}", "shortName": f"{route_id}UP"},
        "trip": {"gtfsId": f"DTC:{route_id}_08_30"},
        "legGeometry": {"points": f"ride-{route_id}"},
    }


def _walk(start: str, end: str, duration: int, from_place: dict, to_place: dict) -> dict:
    return {
        "mode": "WALK",
        "transitLeg": False,
        "duration": duration,
        "start": {"scheduledTime": start},
        "end": {"scheduledTime": end},
        "from": from_place,
        "to": to_place,
        "route": None,
        "trip": None,
        "legGeometry": {"points": "transfer-walk"},
    }


class DeterministicModel:
    selected_rung = "1"

    def __init__(self, connection_delay_s: float = 0.0) -> None:
        self.connection_delay_s = connection_delay_s

    def predict_wait(
        self, route_id, stop_id, scheduled, depart, mode, as_of=None
    ) -> WaitEstimate:
        return WaitEstimate(
            scheduled,
            0.0,
            600.0,
            "timetable",
            0.0,
            departure_delay_s=self.connection_delay_s,
        )

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
    ) -> TimingEstimate:
        duration = scheduled + replay_delay_s
        return TimingEstimate(duration, duration, duration, "timetable", 0.0)


def _transfer_candidate(with_walk: bool) -> dict:
    sunder = _place("Sunder Nagar", "50", 28.6000, 77.2350)
    sunder_bay = _place("Sunder Nagar Bay", "51", 28.6005, 77.2355)
    origin = _place("Origin stop", "40", 28.6100, 77.2350)
    destination = _place("Nehru Place", "2559", 28.5486, 77.2491)
    legs = [
        _ride(
            "2026-10-22T08:30:00+05:30",
            "2026-10-22T08:40:00+05:30",
            100,
            origin,
            sunder,
        )
    ]
    if with_walk:
        legs.append(
            _walk(
                "2026-10-22T08:40:00+05:30",
                "2026-10-22T08:41:00+05:30",
                60,
                sunder,
                sunder_bay,
            )
        )
    legs.append(
        _ride(
            "2026-10-22T08:42:00+05:30",
            "2026-10-22T08:52:00+05:30",
            200,
            sunder_bay,
            destination,
        )
    )
    return {"legs": legs}


def test_first_board_fallback_includes_request_to_otp_walk_start() -> None:
    origin = _place("Origin", "40", 28.6675, 77.2282)
    boarding = _place("Boarding", "41", 28.6670, 77.2280)
    alighting = _place("Alighting", "42", 28.6000, 77.2350)
    itinerary = {
        "legs": [
            _walk(
                "2026-10-22T08:38:10+05:30",
                "2026-10-22T08:40:00+05:30",
                110,
                origin,
                boarding,
            ),
            _ride(
                "2026-10-22T08:40:00+05:30",
                "2026-10-22T08:50:00+05:30",
                100,
                boarding,
                alighting,
            ),
        ]
    }

    option = retime_itinerary(itinerary, REQUEST, "replay", DeterministicModel())
    board = next(leg for leg in option["legs"] if leg["type"] == "board")

    assert board["scheduled_duration_s"] == 490
    assert board["estimated_duration_s"] == 490
    assert option["total_duration_s"] == 110 + 490 + 600


@pytest.mark.parametrize("with_walk", [False, True])
def test_connection_clock_next_bus_penalty_walk_and_endpoints(with_walk: bool) -> None:
    model = DeterministicModel(connection_delay_s=60.0)
    baseline = retime_itinerary(_transfer_candidate(with_walk), REQUEST, "replay", model)
    delayed = retime_itinerary(
        _transfer_candidate(with_walk),
        REQUEST,
        "replay",
        model,
        replay_delay_s=300.0,
    )
    baseline_transfer = next(leg for leg in baseline["legs"] if leg["type"] == "transfer")
    delayed_transfer = next(leg for leg in delayed["legs"] if leg["type"] == "transfer")

    assert baseline_transfer["from_stop"] == "Sunder Nagar"
    assert baseline_transfer["to_stop"] == "Sunder Nagar Bay"
    assert baseline_transfer["to_stop"] != "Nehru Place"
    assert baseline_transfer["from_lat"] == 28.6000
    assert baseline_transfer["to_lat"] == 28.6005
    assert baseline_transfer["geometry"] is None
    assert baseline_transfer["scheduled_duration_s"] == (60 if with_walk else 120)
    assert baseline_transfer["connection_missed_probability"] == 0.0
    assert delayed_transfer["connection_missed_probability"] == 1.0

    # Candidate departure is 08:43 with its own 60 s delay. The +300 s inbound
    # delay does not move that cutoff, and the 60 s walk is counted only once.
    assert baseline_transfer["estimated_duration_s"] == (120 if with_walk else 180)
    assert delayed_transfer["estimated_duration_s"] == (420 if with_walk else 480)
    assert baseline["total_duration_s"] == 1380
    assert delayed["total_duration_s"] == 1980


def _stretch(
    route_id: int,
    from_stop: str,
    to_stop: str,
    query: datetime,
    actual_s: int,
    available_at: datetime | None = None,
) -> TimingRecord:
    return TimingRecord(
        StretchError(
            day=query.date().isoformat(),
            route_id=route_id,
            direction="UP",
            vehicle_id="v",
            run_id=f"{route_id}-{query:%H%M}",
            from_sequence=1,
            to_sequence=2,
            from_stop_id=from_stop,
            to_stop_id=to_stop,
            query_ts=int(query.timestamp()),
            to_ts=int((query + timedelta(seconds=actual_s)).timestamp()),
            scheduled_duration_s=600,
            actual_duration_s=actual_s,
            error_s=actual_s - 600,
            from_observed=True,
            to_observed=True,
            from_available_at=int(query.timestamp()),
            to_available_at=int((available_at or query + timedelta(seconds=actual_s)).timestamp()),
        )
    )


def _passage(route_id: int, stop_id: str, ts: datetime, available_at: datetime) -> PassageRow:
    return PassageRow(
        day=ts.date().isoformat(),
        route_id=route_id,
        direction="UP",
        vehicle_id="v",
        run_id=f"{route_id}-{ts:%H%M}",
        stop_sequence=1,
        stop_id=stop_id,
        observed=True,
        ts=int(ts.timestamp()),
        scheduled_ts=int(ts.timestamp()),
        schedule_error_s=0,
        available_at=int(available_at.timestamp()),
    )


def _causal_model(records: list[TimingRecord], passages: list[PassageRow]) -> RetimeModel:
    model = RetimeModel.__new__(RetimeModel)
    model.data_dir = Path("/unused")
    model.selected_rung = "1"
    model.records = records
    model.exact = defaultdict(list)
    model.route_hour = defaultdict(list)
    model.global_hour = defaultdict(list)
    model.stop_events = defaultdict(list)
    for record in records:
        stretch = record.stretch
        model.exact[(stretch.route_id, stretch.from_stop_id, stretch.to_stop_id)].append(record)
        model.route_hour[(stretch.route_id, record.hour)].append(record)
        model.global_hour[record.hour].append(record)
    for passage in passages:
        model.stop_events[(passage.route_id, passage.stop_id)].append(passage)
    return model


def test_future_observations_cannot_change_ride_wait_or_quiet_hour() -> None:
    past = datetime(2026, 10, 22, 8, 0, tzinfo=IST)
    future_event = datetime(2026, 10, 22, 8, 20, tzinfo=IST)
    future_available = datetime(2026, 10, 22, 8, 35, tzinfo=IST)
    base_records = [
        _stretch(100, "40", "50", past, 720),
        _stretch(200, "51", "2559", past, 660),
    ]
    base_passages = [
        _passage(100, "40", past, past),
        _passage(200, "51", past, past),
    ]
    baseline_model = _causal_model(base_records, base_passages)
    changed_model = _causal_model(
        base_records
        + [
            _stretch(100, "40", "50", future_event, 1200, future_available),
            _stretch(200, "51", "2559", future_event, 1200, future_available),
        ],
        base_passages
        + [
            _passage(100, "40", future_event, future_available),
            _passage(200, "51", future_event, future_available),
        ],
    )

    baseline = retime_itinerary(_transfer_candidate(True), REQUEST, "replay", baseline_model)
    changed = retime_itinerary(_transfer_candidate(True), REQUEST, "replay", changed_model)

    assert changed == baseline


def test_ineligible_model_artifact_uses_labelled_causal_fallback() -> None:
    model = _causal_model([], [])
    model.selected_rung = "4"

    class IneligibleArtifact:
        rung = "4"
        covered_routes = frozenset({100})

        @staticmethod
        def is_eligible_as_of(as_of_ts: int) -> bool:
            return False

    model._artifact = IneligibleArtifact()
    estimate = model.predict_ride(100, "40", "50", 600, REQUEST, "replay", as_of=REQUEST)

    assert estimate.timing_source == "timetable_fallback"
    assert estimate.coverage_note is not None
    assert "selected artifact ineligible as of 2026-10-22T08:30:00+05:30" in estimate.coverage_note
    assert "causal rung 1 fallback" in estimate.coverage_note
