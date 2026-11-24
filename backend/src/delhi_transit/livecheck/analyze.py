"""`livecheck analyze`: match predicted 717A ride legs to the rebuilt actual runs and summarise accuracy.

Matching rule. A predicted ride leg (route 2170/2171, boarding stop B, alighting stop A) has a
predicted boarding time: the request's `depart` plus the estimated durations of the legs before
the ride (walks and waits), and a stop-arrival time: the same without the wait. The actual run is
the first reconstructed run of the same route whose passage at B is at or after
max(stop arrival, predicted boarding - BOARD_TOLERANCE_S). The actual ride time is that run's
passage time at A minus its passage time at B. The leg is unmatched (with a reason) when a stop is
not on the reconstructed pattern, every reconstructed run passed B before that bound, no run passes
B within MAX_BOARD_DELAY_S of the predicted boarding, or either passage of the chosen run lies outside its observed span (extrapolated).
"""
from __future__ import annotations

import itertools
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from delhi_transit.core.config import LIVE_STALE_THRESHOLD_S
from delhi_transit.core.time import iso_ist
from delhi_transit.livecheck.record import ROUTES_717A

BOARD_TOLERANCE_S = 120
MAX_BOARD_DELAY_S = 1800
SMALL_SAMPLE = 30


@dataclass(frozen=True)
class StopPassage:
    ts: int
    observed: bool
    extrapolated: bool
    sequence: int


@dataclass(frozen=True)
class ActualRun:
    route_id: int
    vehicle_id: str
    run_id: str
    stops: dict[str, StopPassage]


def load_runs(passages_path: Path | None) -> dict[int, list[ActualRun]]:
    """Runs per route; a passage is extrapolated when it lies outside the run's observed stops."""
    if passages_path is None or not Path(passages_path).exists():
        return {}
    con = duckdb.connect()
    try:
        rows = con.execute(
            """
            select route_id, vehicle_id, run_id, stop_id, stop_sequence, ts, observed
            from read_parquet(?) order by route_id, vehicle_id, run_id, stop_sequence
            """,
            [str(passages_path)],
        ).fetchall()
    finally:
        con.close()
    grouped: dict[tuple[int, str, str], list[tuple]] = defaultdict(list)
    for route_id, vehicle_id, run_id, stop_id, sequence, ts, observed in rows:
        grouped[(int(route_id), vehicle_id, run_id)].append((stop_id, int(sequence), int(ts), bool(observed)))
    runs: dict[int, list[ActualRun]] = defaultdict(list)
    for (route_id, vehicle_id, run_id), stops in grouped.items():
        observed_seq = [sequence for _, sequence, _, observed in stops if observed]
        low, high = min(observed_seq), max(observed_seq)
        runs[route_id].append(
            ActualRun(
                route_id,
                vehicle_id,
                run_id,
                {
                    stop_id: StopPassage(ts, observed, not low <= sequence <= high, sequence)
                    for stop_id, sequence, ts, observed in stops
                },
            )
        )
    return dict(runs)


def ride_legs(record: dict[str, Any]) -> list[dict[str, Any]]:
    """717A ride legs of one saved response, with predicted boarding and stop-arrival times."""
    response = record.get("response") or {}
    if response.get("status") != "ok":
        return []
    depart_ts = datetime.fromisoformat(response["depart"]).timestamp()
    legs: dict[tuple, dict[str, Any]] = {}
    for option_index, option in enumerate(response.get("options") or []):
        offset = 0.0
        at_stop = 0.0
        for leg in option["legs"]:
            if leg["type"] == "ride" and leg.get("route_id") in ROUTES_717A:
                row = {
                    "slot": record["slot"],
                    "query_id": record["query"]["query_id"],
                    "requested_at_iso": record["requested_at_iso"],
                    "depart": response["depart"],
                    "option_index": option_index,
                    "route_id": leg["route_id"],
                    "from_stop_id": leg.get("from_stop_id"),
                    "to_stop_id": leg.get("to_stop_id"),
                    "from_stop": leg.get("from_stop"),
                    "to_stop": leg.get("to_stop"),
                    "predicted_board_ts": round(depart_ts + offset),
                    "stop_arrival_ts": round(depart_ts + at_stop),
                    "predicted_s": leg["estimated_duration_s"],
                    "scheduled_s": leg["scheduled_duration_s"],
                    "p10_s": leg.get("window_p10_s"),
                    "p90_s": leg.get("window_p90_s"),
                    "timing_source": leg.get("timing_source"),
                    "observed_coverage": leg.get("observed_coverage"),
                    "feed_age_seconds": response.get("feed_age_seconds"),
                }
                key = (row["route_id"], row["from_stop_id"], row["to_stop_id"], row["predicted_board_ts"])
                legs.setdefault(key, row)
            offset += float(leg["estimated_duration_s"])
            if leg["type"] in ("walk", "ride"):
                at_stop = offset
    return list(legs.values())


def match_leg(leg: dict[str, Any], runs: dict[int, list[ActualRun]]) -> dict[str, Any]:
    """Apply the module's matching rule; returns the leg with actual fields or an unmatched reason."""
    out = {**leg, "matched": False, "unmatched_reason": None}
    route_runs = runs.get(leg["route_id"], [])
    if not route_runs:
        return {**out, "unmatched_reason": "no_actual_runs_for_route"}
    pattern = route_runs[0].stops
    board, alight = leg["from_stop_id"], leg["to_stop_id"]
    if board not in pattern:
        return {**out, "unmatched_reason": "board_stop_not_in_pattern"}
    if alight not in pattern:
        return {**out, "unmatched_reason": "alight_stop_not_in_pattern"}
    if pattern[board].sequence >= pattern[alight].sequence:
        return {**out, "unmatched_reason": "stop_order_mismatch"}
    earliest = max(leg["stop_arrival_ts"], leg["predicted_board_ts"] - BOARD_TOLERANCE_S)
    candidates = [run for run in route_runs if run.stops[board].ts >= earliest]
    if not candidates:
        return {**out, "unmatched_reason": "boarding_after_recorded_runs"}
    run = min(candidates, key=lambda item: (item.stops[board].ts, item.vehicle_id, item.run_id))
    start, end = run.stops[board], run.stops[alight]
    out.update(
        actual_vehicle_id=run.vehicle_id,
        actual_run_id=run.run_id,
        actual_board_ts=start.ts,
        board_delay_s=start.ts - leg["predicted_board_ts"],
    )
    if start.ts - leg["predicted_board_ts"] > MAX_BOARD_DELAY_S:
        return {**out, "unmatched_reason": "no_run_within_max_board_delay"}
    if start.extrapolated or end.extrapolated:
        return {**out, "unmatched_reason": "actual_endpoint_extrapolated"}
    actual = end.ts - start.ts
    if actual <= 0:
        return {**out, "unmatched_reason": "non_positive_actual"}
    p10, p90 = leg.get("p10_s"), leg.get("p90_s")
    return {
        **out,
        "matched": True,
        "actual_s": actual,
        "error_s": leg["predicted_s"] - actual,
        "scheduled_error_s": leg["scheduled_s"] - actual,
        "in_p10_p90": p10 is not None and p90 is not None and p10 <= actual <= p90,
        "observed_only": start.observed and end.observed,
    }


def accuracy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [row["error_s"] for row in rows]
    if not errors:
        return {"count": 0}
    return {
        "count": len(errors),
        "mae_s": round(statistics.fmean(abs(e) for e in errors), 1),
        "bias_s": round(statistics.fmean(errors), 1),
        "median_ae_s": round(statistics.median(abs(e) for e in errors), 1),
        "timetable_mae_s": round(statistics.fmean(abs(row["scheduled_error_s"]) for row in rows), 1),
        "p10_p90_coverage": round(sum(row["in_p10_p90"] for row in rows) / len(rows), 4),
        "small_sample": len(errors) < SMALL_SAMPLE,
    }


def accuracy_tables(legs: list[dict[str, Any]]) -> dict[str, Any]:
    matched = [row for row in legs if row["matched"]]
    observed = [row for row in matched if row["observed_only"]]

    def by(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
        groups: dict[str, list] = defaultdict(list)
        for row in rows:
            groups[str(row[key])].append(row)
        return {name: accuracy(group) for name, group in sorted(groups.items())}

    return {
        "overall": accuracy(matched),
        "by_provenance": by(matched, "timing_source"),
        "by_route": by(matched, "route_id"),
        "observed_only": accuracy(observed),
        "observed_only_by_provenance": by(observed, "timing_source"),
        "legs_total": len(legs),
        "legs_matched": len(matched),
        "unmatched_by_reason": dict(Counter(row["unmatched_reason"] for row in legs if not row["matched"])),
    }


def _ride_rule_violations(response: dict[str, Any]) -> int:
    return sum(
        1
        for option in response.get("options") or []
        for leg in option["legs"]
        if leg["type"] == "ride"
        and (not leg.get("timing_source") or leg.get("observed_coverage") is None
             or leg.get("window_p10_s") is None or leg.get("window_p90_s") is None)
    )


def response_stats(records: list[dict[str, Any]], threshold_s: float = LIVE_STALE_THRESHOLD_S) -> dict[str, Any]:
    """Status and reason counts, plus checks every saved response must satisfy."""
    statuses = Counter(record["status"] for record in records)
    reasons = Counter(record["abstention_code"] for record in records if record["status"] == "insufficient_data")
    ok = [record for record in records if record["status"] == "ok"]
    ages = [record["feed_age_seconds"] for record in records if record.get("feed_age_seconds") is not None]
    by_slot: dict[int, Counter] = defaultdict(Counter)
    for record in records:
        by_slot[record["slot"]][record["status"]] += 1
    return {
        "responses": len(records),
        "by_status": dict(statuses),
        "by_reason_code": dict(reasons),
        "ok_not_live_mode": sum(1 for r in ok if r["response"].get("mode") != "live"),
        "ok_with_stale_feed": sum(1 for r in ok if (r.get("feed_age_seconds") or 0) > threshold_s),
        "ok_ride_legs_missing_fields": sum(_ride_rule_violations(r["response"]) for r in ok),
        "abstentions_without_code": sum(
            1 for r in records if r["status"] == "insufficient_data" and not r["abstention_code"]
        ),
        "feed_age_s": _spread(ages),
        "by_slot": {str(slot): dict(counts) for slot, counts in sorted(by_slot.items())},
        "stale_threshold_s": threshold_s,
    }


def _spread(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "median": round(statistics.median(values), 1),
        "max": round(max(values), 1),
    }


def feed_health(polls: list[dict[str, Any]], gap_limit_s: float = 120.0) -> dict[str, Any]:
    ok = [poll for poll in polls if poll["ok"]]
    times = [poll["attempted_at"] for poll in ok]
    gaps = [
        {"from": iso_ist(a), "to": iso_ist(b), "seconds": round(b - a, 1)}
        for a, b in itertools.pairwise(times)
        if b - a > gap_limit_s
    ]
    return {
        "polls": len(polls),
        "ok": len(ok),
        "success_rate": round(len(ok) / len(polls), 4) if polls else None,
        "errors": dict(Counter(poll["error"] for poll in polls if not poll["ok"])),
        "first_ok": iso_ist(times[0]) if times else None,
        "last_ok": iso_ist(times[-1]) if times else None,
        "repeated_snapshots": sum(1 for poll in ok if poll["repeated_snapshot"]),
        "gaps_over_2_min": gaps,
        "vehicles": _spread([poll["vehicle_count"] for poll in ok]),
        "fresh_vehicles": _spread([poll["fresh_vehicle_count"] for poll in ok]),
        "feed_age_at_poll_s": _spread([poll["feed_age_seconds_at_poll"] for poll in ok]),
    }


def google_rows(calls: list[dict[str, Any]], legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Our estimate, Google's bus-only duration, and the actual 717A ride for the same OD and slot."""
    actual_by_slot_od: dict[tuple[int, str], dict[str, Any]] = {}
    for leg in legs:
        if leg["matched"] and leg["option_index"] == 0:
            actual_by_slot_od.setdefault((leg["slot"], leg["query_id"]), leg)
    rows = []
    for call in calls:
        google = call.get("google") or {}
        best = ((google.get("parsed") or {}).get("best_bus_route")) or {}
        our = call.get("our") or {}
        bus_legs = best.get("bus_legs") or []
        google_717a = [leg for leg in bus_legs if "717A" in str(leg.get("route_name", "")).upper()]
        actual = actual_by_slot_od.get((call["slot"], call["od_id"]))
        p10, p90 = our.get("our_window_p10_s"), our.get("our_window_p90_s")
        google_s = best.get("duration_s")
        rows.append(
            {
                "slot": call["slot"],
                "od_id": call["od_id"],
                "label": call.get("label"),
                "departure_time": call["departure_time"],
                "from_cache": google.get("from_cache"),
                "google_status": "ok" if best else (google.get("unavailable_reason") or google.get("error") or "no_bus_only_route"),
                "language_code": (google.get("request") or {}).get("languageCode"),
                "our_status": our.get("our_status"),
                "our_total_s": our.get("our_total_duration_s"),
                "our_p10_s": p10,
                "our_p90_s": p90,
                "google_s": google_s,
                "google_in_our_window": (
                    None if google_s is None or p10 is None or p90 is None else p10 <= google_s <= p90
                ),
                "google_routes": " + ".join(best.get("route_names") or []),
                "google_717a_ride_s": sum(leg.get("static_duration_s") or 0 for leg in google_717a) or None,
                "our_717a_ride_s": actual["predicted_s"] if actual else None,
                "actual_717a_ride_s": actual["actual_s"] if actual else None,
            }
        )
    return rows


def _read_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    records = []
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            records.extend(json.loads(line) for line in handle if line.strip())
    return records


def analyze(in_dir: Path) -> dict[str, Any]:
    in_dir = Path(in_dir)
    summary = json.loads((in_dir / "run-summary.json").read_text(encoding="utf-8"))
    actuals_path = in_dir / "passages" / "actuals-summary.json"
    actuals = json.loads(actuals_path.read_text(encoding="utf-8")) if actuals_path.exists() else None
    passages = Path(actuals["passages_path"]) if actuals and actuals.get("passages_path") else None
    records = _read_jsonl(sorted((in_dir / "journeys").glob("slot_*.jsonl")))
    polls = _read_jsonl([in_dir / "polls.jsonl"]) if (in_dir / "polls.jsonl").exists() else []
    google_path = in_dir / "google" / "calls.jsonl"
    calls = _read_jsonl([google_path]) if google_path.exists() else []
    runs = load_runs(passages)
    legs = [match_leg(leg, runs) for record in records for leg in ride_legs(record)]
    return {
        "day": summary["day"],
        "run": {key: summary.get(key) for key in ("status", "started_at", "updated_at", "until", "journeys_until", "slot_minutes", "poll_interval_s")},
        "slots": summary.get("slots", []),
        "matching_rule": {
            "board_tolerance_s": BOARD_TOLERANCE_S,
            "max_board_delay_s": MAX_BOARD_DELAY_S,
            "text": (__doc__ or "").split("Matching rule.")[1].strip(),
        },
        "feed_health": feed_health(polls),
        "responses": response_stats(records),
        "actuals": actuals,
        "accuracy": accuracy_tables(legs),
        "google": {"summary": summary.get("google"), "rows": google_rows(calls, legs)},
        "legs": legs,
    }
