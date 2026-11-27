from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx

from delhi_transit.core.config import IST
from delhi_transit.livecheck.analyze import (
    SMALL_SAMPLE,
    ActualRun,
    StopPassage,
    match_leg,
    run_status,
    window_coverage,
)
from delhi_transit.livecheck.record import Recorder, RunSettings
from delhi_transit.livecheck.report import render_markdown, run_status_line

BOARD_TS = 1_800_000_000


def _run(vehicle: str, board_ts: int, ride_s: int, *, observed: bool = True) -> ActualRun:
    return ActualRun(
        2171,
        vehicle,
        f"{vehicle}_run",
        {
            "A": StopPassage(board_ts - 120, True, False, 1),
            "B": StopPassage(board_ts, observed, False, 2),
            "C": StopPassage(board_ts + ride_s, True, False, 3),
        },
    )


def _leg(**overrides) -> dict:
    leg = {
        "route_id": 2171,
        "from_stop_id": "B",
        "to_stop_id": "C",
        "predicted_board_ts": BOARD_TS,
        "stop_arrival_ts": BOARD_TS - 300,
        "predicted_s": 600,
        "scheduled_s": 700,
        "p10_s": 500,
        "p90_s": 650,
    }
    return {**leg, **overrides}


def test_leg_matches_first_run_at_or_after_boarding_minus_tolerance():
    runs = {
        2171: [
            _run("too_early", BOARD_TS - 200, 400),
            _run("first_in_window", BOARD_TS - 60, 630, observed=False),
            _run("later", BOARD_TS + 300, 500),
        ]
    }
    result = match_leg(_leg(), runs)
    assert result["matched"]
    assert result["actual_vehicle_id"] == "first_in_window"
    assert result["actual_s"] == 630
    assert result["error_s"] == -30
    assert result["in_p10_p90"] is True
    assert result["observed_only"] is False


def test_leg_never_matches_a_bus_that_left_before_the_rider_reached_the_stop():
    runs = {2171: [_run("left_before_arrival", BOARD_TS - 60, 600), _run("next", BOARD_TS + 900, 610)]}
    result = match_leg(_leg(stop_arrival_ts=BOARD_TS - 30), runs)
    assert result["actual_vehicle_id"] == "next"


def test_leg_unmatched_reasons():
    runs = {2171: [_run("only", BOARD_TS + 2400, 600)]}
    assert match_leg(_leg(), runs)["unmatched_reason"] == "no_run_within_max_board_delay"
    assert match_leg(_leg(to_stop_id="Z"), runs)["unmatched_reason"] == "alight_stop_not_in_pattern"
    assert match_leg(_leg(from_stop_id="C", to_stop_id="B"), runs)["unmatched_reason"] == "stop_order_mismatch"
    extrapolated = ActualRun(2171, "x", "x_run", {**_run("x", BOARD_TS, 600).stops,
                                                   "C": StopPassage(BOARD_TS + 600, False, True, 3)})
    assert match_leg(_leg(), {2171: [extrapolated]})["unmatched_reason"] == "actual_endpoint_extrapolated"
    assert match_leg(_leg(route_id=2170), runs)["unmatched_reason"] == "no_actual_runs_for_route"


class _FakeGoogle:
    def __init__(self, ledger_total: int) -> None:
        self.total = ledger_total
        self.calls = 0

    def read_call_counter(self) -> dict:
        return {"total_mission_calls": self.total}

    def compute_bus_routes(self, origin, destination, depart, **_kwargs) -> dict:
        assert depart > datetime.now(IST)
        self.calls += 1
        self.total += 1
        return {"from_cache": False, "request": {"languageCode": "en"}, "parsed": {"best_bus_route": None}}


def _recorder(tmp_path: Path, google: _FakeGoogle, *, google_max: int, ledger_limit: int) -> Recorder:
    now = datetime.now(IST)
    settings = RunSettings(
        out_dir=tmp_path,
        api_url="http://api.test",
        until=now + timedelta(hours=1),
        journeys_until=now + timedelta(hours=1),
        slot_s=1200,
        poll_interval_s=25,
        google_slots=[now],
        google_max=google_max,
        google_ledger_limit=ledger_limit,
        gtfs_dir=tmp_path,
    )
    return Recorder(settings, poller=object(), google_client=google)


def _api() -> httpx.Client:
    body = {"status": "insufficient_data", "abstention": {"reason": "stale"}}
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))


def test_google_stops_at_run_budget_across_slots(tmp_path):
    google = _FakeGoogle(ledger_total=92)
    recorder = _recorder(tmp_path, google, google_max=12, ledger_limit=122)
    with _api() as api:
        recorder.run_google(api, slot=0)
        recorder.run_google(api, slot=1)
        recorder.run_google(api, slot=2)
    assert google.calls == 12
    assert google.total == 104
    assert "budget reached" in recorder.google_summary["stopped_reason"]


def test_google_stops_at_ledger_limit_and_zero_budget_makes_no_call(tmp_path):
    google = _FakeGoogle(ledger_total=119)
    recorder = _recorder(tmp_path / "a", google, google_max=30, ledger_limit=122)
    with _api() as api:
        recorder.run_google(api, slot=0)
    assert google.total == 122

    idle = _FakeGoogle(ledger_total=92)
    with _api() as api:
        _recorder(tmp_path / "b", idle, google_max=0, ledger_limit=122).run_google(api, slot=0)
    assert idle.calls == 0

def test_run_left_running_is_reported_as_stopped_before_completion():
    summary = {"status": "running", "started_at": "S", "updated_at": "U", "until": "E"}
    run = run_status(summary, {"last_ok": "L"})
    assert run["status"] == "stopped_before_completion"
    assert run["summary_status"] == "running"
    assert run["last_ok_poll"] == "L"
    line = run_status_line(run)
    assert "stopped before completion" in line
    assert "last summary update U, last successful poll L (planned end E)" in line


def test_window_coverage_orders_slots_numerically() -> None:
    polls = [
        {"ok": True, "attempted_at": BOARD_TS},
        {"ok": True, "attempted_at": BOARD_TS + 3600},
    ]
    legs = [
        {"slot": 10, "predicted_board_ts": BOARD_TS + 100, "predicted_s": 600},
        {"slot": 2, "predicted_board_ts": BOARD_TS + 100, "predicted_s": 600},
        {"slot": 0, "predicted_board_ts": BOARD_TS + 100, "predicted_s": 600},
    ]
    window = window_coverage(legs, polls)
    assert list(window["legs_by_slot"]) == ["0", "2", "10"]


def test_render_markdown_only_warns_small_sample_below_threshold() -> None:
    def _report(matched: int) -> str:
        return render_markdown(
            {
                "day": "2026-11-25",
                "run": {"status": "finished", "started_at": "S", "updated_at": "U", "until": "E"},
                "slots": [],
                "window": {
                    "first_ok": "2026-11-25T06:30:00+05:30",
                    "last_ok": "2026-11-25T11:30:00+05:30",
                    "minutes": 300.0,
                    "legs_by_slot": {"0": {"legs": matched, "end_after_window": 0}},
                },
                "matching_rule": {"board_tolerance_s": 120, "max_board_delay_s": 1800, "text": "rule"},
                "feed_health": {
                    "polls": 1,
                    "ok": 1,
                    "success_rate": 1.0,
                    "errors": {},
                    "first_ok": "2026-11-25T06:30:00+05:30",
                    "last_ok": "2026-11-25T11:30:00+05:30",
                    "repeated_snapshots": 0,
                    "gaps_over_2_min": [],
                    "vehicles": {},
                    "feed_age_at_poll_s": {},
                },
                "responses": {
                    "responses": 0,
                    "by_status": {},
                    "by_reason_code": {},
                    "feed_age_s": {},
                    "stale_threshold_s": 600,
                    "ok_not_live_mode": 0,
                    "ok_with_stale_feed": 0,
                    "ok_ride_legs_missing_fields": 0,
                    "abstentions_without_code": 0,
                },
                "actuals": None,
                "accuracy": {
                    "legs_total": matched,
                    "legs_matched": matched,
                    "unmatched_by_reason": {},
                    "overall": {"count": 0},
                    "by_provenance": {},
                    "by_route": {},
                    "observed_only": {"count": 0},
                    "observed_only_by_provenance": {},
                },
                "google": {"summary": {}, "calls_in_log": 0, "rows": []},
            }
        )

    assert "- Small sample:" in _report(SMALL_SAMPLE - 1)
    assert "- Small sample:" not in _report(SMALL_SAMPLE)
