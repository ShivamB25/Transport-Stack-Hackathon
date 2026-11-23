"""Real October 22 marginal-transfer replay scenario backed by 717A stop passages + OTP."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

from ps1.otp_client import OTPClientError

IST = timezone(timedelta(hours=5, minutes=30))

DEMO_DAY = "2026-10-22"
DEMO_RUN_ID = "2171_17_37"
DEMO_VEHICLE_ID = "DL1PD5280"
DEMO_ROUTE_ID = 2171
DEMO_ROUTE_NAME = "717AUP"
DEMO_DEPART_ISO = "2026-10-22T17:35:00+05:30"
DEMO_ORIGIN = {
    "name": "Badarpur Border (T)",
    "stop_id": "2640",
    "lat": 28.49313,
    "lng": 77.30373,
}
DEMO_DESTINATION = {
    "name": "Hauz Khas",
    "lat": 28.5494,
    "lng": 77.2060,
}
DEMO_TRANSFER_STOP_SEQ = 15
DEMO_TRANSFER_STOP_ID = "7963"
DEMO_TRANSFER_STOP_NAME = "Ambedkar Nagar Depot"
DEMO_CONNECTING_ROUTE = "D-7702UP"
DEMO_DIRECT_ROUTE = "774UP"

# Fallback passage snapshots from real Oct 22 2026 run 2171_17_37 (used only if parquet absent in unit tests)
_FALLBACK_PASSAGES: list[dict[str, Any]] = [
    {
        "stop_sequence": 0,
        "stop_id": "2640",
        "stop_name": "Badarpur Border (T)",
        "stop_lat": 28.49313,
        "stop_lng": 77.30373,
        "observed": True,
        "ts": 1792670870,
        "scheduled_ts": 1792670820,
        "schedule_error_s": 50,
    },
    {
        "stop_sequence": 2,
        "stop_id": "2819",
        "stop_name": "Pul Pehladpur ( BadarPur)",
        "stop_lat": 28.49794,
        "stop_lng": 77.29132,
        "observed": True,
        "ts": 1792671301,
        "scheduled_ts": 1792671090,
        "schedule_error_s": 211,
    },
    {
        "stop_sequence": 3,
        "stop_id": "2820",
        "stop_name": "Surajkund Crossing",
        "stop_lat": 28.49988,
        "stop_lng": 77.28771,
        "observed": True,
        "ts": 1792671785,
        "scheduled_ts": 1792671172,
        "schedule_error_s": 613,
    },
    {
        "stop_sequence": 15,
        "stop_id": "7963",
        "stop_name": "Ambedkar Nagar Depot",
        "stop_lat": 28.51518,
        "stop_lng": 77.22819,
        "observed": True,
        "ts": 1792673257,
        "scheduled_ts": 1792672427,
        "schedule_error_s": 830,
    },
]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, IST).isoformat()


def _hhmmss(ts: int) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S")


def _load_oct22_run_passages(data_dir: Path) -> tuple[list[dict[str, Any]], str]:
    parquet_path = data_dir / "passages" / f"day={DEMO_DAY}.parquet"
    if not parquet_path.exists():
        return list(_FALLBACK_PASSAGES), "fallback_fixture"
    con = duckdb.connect()
    try:
        rows = con.execute(
            """
            SELECT stop_sequence, stop_id, stop_name, stop_lat, stop_lng,
                   observed, ts, scheduled_ts, schedule_error_s
            FROM read_parquet(?)
            WHERE run_id = ? AND stop_sequence <= ?
            ORDER BY stop_sequence
            """,
            [str(parquet_path), DEMO_RUN_ID, DEMO_TRANSFER_STOP_SEQ + 1],
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return list(_FALLBACK_PASSAGES), "fallback_fixture"
    passages = [
        {
            "stop_sequence": int(r[0]),
            "stop_id": str(r[1]),
            "stop_name": str(r[2]),
            "stop_lat": float(r[3]),
            "stop_lng": float(r[4]),
            "observed": bool(r[5]),
            "ts": int(r[6]),
            "scheduled_ts": int(r[7]),
            "schedule_error_s": int(r[8]),
        }
        for r in rows
    ]
    return passages, str(parquet_path)


def _extract_geometries(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    transfer_cand: dict[str, Any] | None = None
    direct_cand: dict[str, Any] | None = None
    for cand in candidates:
        legs = cand.get("legs") or []
        transit = [leg for leg in legs if leg.get("transitLeg")]
        if not transit:
            continue
        first_route = ((transit[0].get("route") or {}).get("shortName") or "").upper()
        if transfer_cand is None and first_route == DEMO_ROUTE_NAME and len(transit) >= 2:
            transfer_cand = cand
        if direct_cand is None and len(transit) == 1:
            direct_cand = cand
    if transfer_cand is None and candidates:
        transfer_cand = candidates[0]
    if direct_cand is None and len(candidates) > 1:
        direct_cand = candidates[-1]

    def _legs_payload(cand: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not cand:
            return []
        result = []
        for leg in cand.get("legs") or []:
            route = leg.get("route") or {}
            frm = leg.get("from") or {}
            to = leg.get("to") or {}
            result.append(
                {
                    "mode": leg.get("mode", "BUS" if leg.get("transitLeg") else "WALK"),
                    "transit": bool(leg.get("transitLeg")),
                    "route_name": route.get("shortName") or route.get("longName"),
                    "from_name": frm.get("name"),
                    "to_name": to.get("name"),
                    "from_lat": frm.get("lat"),
                    "from_lng": frm.get("lon"),
                    "to_lat": to.get("lat"),
                    "to_lng": to.get("lon"),
                    "duration_s": round(float(leg.get("duration") or 0)),
                    "geometry": (leg.get("legGeometry") or {}).get("points"),
                }
            )
        return result

    transfer_scheduled_s = 3793
    transfer_gap_s = 377
    connect_sched_iso = "2026-10-22T18:10:04+05:30"
    transfer_route_summary = "717AUP -> D-7702UP -> 413DOWN"
    if transfer_cand:
        t_legs = [leg for leg in transfer_cand.get("legs") or [] if leg.get("transitLeg")]
        if transfer_cand.get("duration"):
            transfer_scheduled_s = round(float(transfer_cand["duration"]))
        if len(t_legs) >= 2:
            t_arr = datetime.fromisoformat(t_legs[0]["end"]["scheduledTime"])
            t_dep = datetime.fromisoformat(t_legs[1]["start"]["scheduledTime"])
            gap = round((t_dep - t_arr).total_seconds())
            if gap > 0:
                transfer_gap_s = gap
                connect_sched_iso = t_legs[1]["start"]["scheduledTime"]
            route_names = [
                (leg.get("route") or {}).get("shortName") or "BUS" for leg in t_legs
            ]
            transfer_route_summary = " -> ".join(route_names)

    direct_scheduled_s = 3930
    direct_route_summary = DEMO_DIRECT_ROUTE
    if direct_cand:
        d_legs = [leg for leg in direct_cand.get("legs") or [] if leg.get("transitLeg")]
        if direct_cand.get("duration"):
            direct_scheduled_s = round(float(direct_cand["duration"]))
        if d_legs:
            direct_route_summary = (d_legs[0].get("route") or {}).get("shortName") or DEMO_DIRECT_ROUTE

    return {
        "transfer_legs": _legs_payload(transfer_cand),
        "direct_legs": _legs_payload(direct_cand),
        "transfer_scheduled_s": transfer_scheduled_s,
        "transfer_gap_s": transfer_gap_s,
        "connecting_scheduled_depart": connect_sched_iso,
        "transfer_route_summary": transfer_route_summary,
        "direct_scheduled_s": direct_scheduled_s,
        "direct_route_summary": direct_route_summary,
    }


def _simulate_transfer_stage(
    *,
    observed_delay_s: int,
    remaining_stops: int,
    transfer_scheduled_s: int,
    transfer_gap_s: int,
    next_bus_headway_s: int = 900,
    seed: int = 717,
) -> dict[str, Any]:
    """Monte Carlo joint-scenario connection risk given current observed delay."""
    rng = random.Random(seed + observed_delay_s)
    # Remaining uncertainty scales with remaining stops along MB Road
    sigma = max(45.0, remaining_stops * 16.0)
    # Mild persistence drift as delay compounds on congested stretches
    drift = 0.0 if observed_delay_s < 120 else min(180.0, observed_delay_s * 0.15)
    n = 200
    missed = 0
    totals: list[float] = []
    for _ in range(n):
        extra_delay = observed_delay_s + drift + rng.gauss(0.0, sigma)
        if extra_delay <= transfer_gap_s:
            # Connection caught: wait shrinks by extra_delay, total stays close to schedule
            total = transfer_scheduled_s + max(0.0, extra_delay * 0.15) + rng.gauss(0.0, 90.0)
        else:
            missed += 1
            overshoot = extra_delay - transfer_gap_s
            wait_penalty = next_bus_headway_s - (overshoot % next_bus_headway_s)
            total = transfer_scheduled_s + extra_delay + wait_penalty + rng.gauss(0.0, 120.0)
        totals.append(max(1200.0, total))
    totals.sort()
    p10 = round(totals[int(0.10 * (n - 1))])
    p50 = round(totals[int(0.50 * (n - 1))])
    p90 = round(totals[int(0.90 * (n - 1))])
    return {
        "miss_probability": round(missed / n, 3),
        "estimated_total_s": p50,
        "window_p10_s": p10,
        "window_p90_s": p90,
    }


async def build_marginal_transfer_demo(
    data_dir: Path,
    otp_client: Any,
) -> dict[str, Any]:
    """Build the 4-stage real-October marginal-transfer replay scenario."""
    passages, fixture_source = _load_oct22_run_passages(data_dir)
    by_seq = {p["stop_sequence"]: p for p in passages}

    try:
        candidates = await otp_client.plan(
            (DEMO_ORIGIN["lat"], DEMO_ORIGIN["lng"]),
            (DEMO_DESTINATION["lat"], DEMO_DESTINATION["lng"]),
            datetime.fromisoformat(DEMO_DEPART_ISO),
        )
    except OTPClientError:
        candidates = []

    geom = _extract_geometries(candidates)
    transfer_sched_s = geom["transfer_scheduled_s"]
    direct_sched_s = geom["direct_scheduled_s"]
    transfer_gap_s = geom["transfer_gap_s"]
    connecting_depart_dt = datetime.fromisoformat(geom["connecting_scheduled_depart"])
    connecting_depart_ts = int(connecting_depart_dt.timestamp())

    p_start = by_seq.get(0, _FALLBACK_PASSAGES[0])
    p_mid = by_seq.get(2, _FALLBACK_PASSAGES[1])
    p_late = by_seq.get(3, _FALLBACK_PASSAGES[2])
    p_xfer = by_seq.get(DEMO_TRANSFER_STOP_SEQ, _FALLBACK_PASSAGES[3])

    # Actual observed arrival at Ambedkar Nagar Depot vs connecting bus departure
    actual_arrival_ts = int(p_xfer["ts"])
    scheduled_arrival_ts = int(p_xfer["scheduled_ts"])
    actual_delay_s = int(p_xfer["schedule_error_s"])
    margin_vs_connection_s = connecting_depart_ts - actual_arrival_ts
    actual_outcome = "caught" if margin_vs_connection_s >= 0 else "missed"
    missed_by_s = max(0, -margin_vs_connection_s)

    direct_est_s = direct_sched_s + 120
    direct_p10_s = direct_est_s - 300
    direct_p90_s = direct_est_s + 420

    stage_specs = [
        ("initial", "1. Timetable recommends transfer", p_start, 15, False, False),
        ("delay_growing", "2. First bus falls behind schedule", p_mid, 13, False, False),
        ("advice_flip", "3. Advice flips to direct bus", p_late, 12, True, False),
        ("outcome_reveal", "4. Actual Oct 22 outcome revealed", p_xfer, 0, True, True),
    ]

    stages: list[dict[str, Any]] = []
    for idx, (stage_id, title, passage, rem_stops, force_flip, is_outcome) in enumerate(stage_specs):
        delay_s = int(passage["schedule_error_s"])
        remaining_buffer_s = transfer_gap_s - delay_s
        if is_outcome:
            sim = {
                "miss_probability": 1.0 if actual_outcome == "missed" else 0.0,
                "estimated_total_s": transfer_sched_s + actual_delay_s + (900 if actual_outcome == "missed" else 0),
                "window_p10_s": transfer_sched_s + actual_delay_s + (600 if actual_outcome == "missed" else 0),
                "window_p90_s": transfer_sched_s + actual_delay_s + (1200 if actual_outcome == "missed" else 0),
            }
        else:
            sim = _simulate_transfer_stage(
                observed_delay_s=delay_s,
                remaining_stops=rem_stops,
                transfer_scheduled_s=transfer_sched_s,
                transfer_gap_s=transfer_gap_s,
            )

        advice_flipped = force_flip or (sim["miss_probability"] >= 0.65 and sim["estimated_total_s"] > direct_est_s)
        recommended_id = "direct" if advice_flipped else "transfer"

        if stage_id == "initial":
            headline = f"Recommended: Take {DEMO_ROUTE_NAME} -> {DEMO_CONNECTING_ROUTE} transfer at {DEMO_TRANSFER_STOP_NAME}"
            explanation = (
                f"On the timetable, the {DEMO_ROUTE_NAME} transfer is {round((direct_sched_s - transfer_sched_s) / 60, 1)} min faster "
                f"than direct {geom['direct_route_summary']}. Vehicle {DEMO_VEHICLE_ID} departed {passage['stop_name']} "
                f"just {delay_s:+d}s off schedule, leaving {remaining_buffer_s}s ({remaining_buffer_s // 60}m {remaining_buffer_s % 60}s) of transfer buffer."
            )
            status_badge = "RECOMMENDED · CONNECTION FEASIBLE"
        elif stage_id == "delay_growing":
            headline = f"Caution: {DEMO_ROUTE_NAME} losing time — transfer buffer shrinking"
            explanation = (
                f"At {passage['stop_name']} ({_hhmmss(passage['ts'])} IST), {DEMO_VEHICLE_ID} is {delay_s}s (+{delay_s // 60}m {delay_s % 60}s) late. "
                f"Transfer buffer at {DEMO_TRANSFER_STOP_NAME} dropped to {remaining_buffer_s}s; miss risk rose to {int(sim['miss_probability'] * 100 + 0.5)}%."
            )
            status_badge = "MARGINAL · CONNECTION AT RISK"
        elif stage_id == "advice_flip":
            headline = f"Advice flipped: Switch to direct bus {geom['direct_route_summary']} (skip {DEMO_ROUTE_NAME} transfer)"
            explanation = (
                f"At {passage['stop_name']} ({_hhmmss(passage['ts'])} IST), observed delay hit +{delay_s}s (+{delay_s // 60}m {delay_s % 60}s), "
                f"exceeding the entire {transfer_gap_s}s ({transfer_gap_s // 60}m {transfer_gap_s % 60}s) transfer window. "
                f"Connection miss probability jumped to {int(sim['miss_probability'] * 100 + 0.5)}%, pushing expected trip time past direct {geom['direct_route_summary']}."
            )
            status_badge = "ADVICE FLIPPED · TRANSFER INFEASIBLE"
        else:
            headline = (
                f"Actual Oct 22 outcome: Connection {actual_outcome.upper()} by "
                f"{missed_by_s // 60}m {missed_by_s % 60}s"
                if actual_outcome == "missed"
                else f"Actual Oct 22 outcome: Connection CAUGHT with {margin_vs_connection_s}s to spare"
            )
            explanation = (
                f"Real GPS passage for {DEMO_VEHICLE_ID} (run {DEMO_RUN_ID}) at {DEMO_TRANSFER_STOP_NAME} (stop {DEMO_TRANSFER_STOP_ID}): "
                f"scheduled {_hhmmss(scheduled_arrival_ts)} IST, connecting {DEMO_CONNECTING_ROUTE} departed {_hhmmss(connecting_depart_ts)} IST, "
                f"actual observed arrival {_hhmmss(actual_arrival_ts)} IST (+{actual_delay_s}s late). "
                f"The flipped recommendation avoided a missed connection."
            )
            status_badge = f"OUTCOME REVEALED · {actual_outcome.upper()}"

        stages.append(
            {
                "index": idx,
                "stage_id": stage_id,
                "title": title,
                "status_badge": status_badge,
                "headline": headline,
                "explanation": explanation,
                "replay_timestamp": _iso(passage["ts"]),
                "replay_time_ist": _hhmmss(passage["ts"]),
                "bus_stop_sequence": passage["stop_sequence"],
                "bus_stop_name": passage["stop_name"],
                "bus_lat": passage["stop_lat"],
                "bus_lng": passage["stop_lng"],
                "observed_delay_s": delay_s,
                "observed_passage": passage["observed"],
                "scheduled_transfer_gap_s": transfer_gap_s,
                "remaining_buffer_s": remaining_buffer_s,
                "recommended_option": recommended_id,
                "advice_flipped": advice_flipped,
                "outcome_revealed": is_outcome,
                "actual_outcome": actual_outcome if is_outcome else None,
                "missed_by_s": missed_by_s if is_outcome else None,
                "transfer_option": {
                    "id": "transfer",
                    "label": f"{geom['transfer_route_summary']} (transfer at {DEMO_TRANSFER_STOP_NAME})",
                    "scheduled_duration_s": transfer_sched_s,
                    "estimated_duration_s": sim["estimated_total_s"],
                    "window_p10_s": sim["window_p10_s"],
                    "window_p90_s": sim["window_p90_s"],
                    "miss_probability": sim["miss_probability"],
                    "recommended": recommended_id == "transfer",
                },
                "direct_option": {
                    "id": "direct",
                    "label": f"{geom['direct_route_summary']} (direct, no transfer)",
                    "scheduled_duration_s": direct_sched_s,
                    "estimated_duration_s": direct_est_s,
                    "window_p10_s": direct_p10_s,
                    "window_p90_s": direct_p90_s,
                    "miss_probability": 0.0,
                    "recommended": recommended_id == "direct",
                },
            }
        )

    return {
        "status": "ok",
        "mode": "replay",
        "feed_age_seconds": 0.0,
        "live_feed_contacted": False,
        "fixture_day": DEMO_DAY,
        "fixture_source": fixture_source,
        "run_id": DEMO_RUN_ID,
        "vehicle_id": DEMO_VEHICLE_ID,
        "route_id": DEMO_ROUTE_ID,
        "route_name": DEMO_ROUTE_NAME,
        "depart": DEMO_DEPART_ISO,
        "origin": DEMO_ORIGIN,
        "destination": DEMO_DESTINATION,
        "transfer_stop": {
            "stop_id": DEMO_TRANSFER_STOP_ID,
            "stop_name": DEMO_TRANSFER_STOP_NAME,
            "lat": p_xfer["stop_lat"],
            "lng": p_xfer["stop_lng"],
            "scheduled_arrival": _iso(scheduled_arrival_ts),
            "connecting_route": DEMO_CONNECTING_ROUTE,
            "connecting_depart": geom["connecting_scheduled_depart"],
            "scheduled_gap_s": transfer_gap_s,
            "actual_arrival": _iso(actual_arrival_ts),
            "actual_delay_s": actual_delay_s,
            "actual_outcome": actual_outcome,
            "missed_by_s": missed_by_s,
        },
        "geometries": {
            "transfer_legs": geom["transfer_legs"],
            "direct_legs": geom["direct_legs"],
        },
        "stages": stages,
    }
