"""Causality and future-data rejection tests for feature extraction."""
from __future__ import annotations

import math
from datetime import datetime

import pytest

from delhi_transit.model.causal_core import build_route_geometry
from delhi_transit.model.features import (
    IST,
    CausalityViolationError,
    PassageRow,
    build_replay_queries,
    build_stretch_errors,
    extract_causal_features_for_query,
)


def _passage(
    *,
    day: str = "2026-10-22",
    route_id: int = 2171,
    direction: str = "UP",
    vehicle_id: str = "V1",
    run_id: str = "R1",
    seq: int,
    stop_id: str,
    ts: int,
    scheduled_ts: int,
    observed: bool = True,
    lat: float = 28.50,
    lng: float = 77.25,
    available_at: int | None = None,
) -> PassageRow:
    return PassageRow(
        day=day,
        route_id=route_id,
        direction=direction,
        vehicle_id=vehicle_id,
        run_id=run_id,
        stop_sequence=seq,
        stop_id=stop_id,
        observed=observed,
        ts=ts,
        scheduled_ts=scheduled_ts,
        stop_lat=lat,
        stop_lng=lng,
        schedule_error_s=ts - scheduled_ts,
        distance_m=12.0 if observed else None,
        available_at=ts if available_at is None else available_at,
    )


def _base_scenario() -> list[PassageRow]:
    t0 = int(datetime(2026, 10, 22, 8, 0, tzinfo=IST).timestamp())
    coords = [(28.500, 77.250), (28.504, 77.250), (28.508, 77.250), (28.512, 77.250)]
    passages = []
    # Earlier completed run R1 (starts at t0)
    for seq, (lat, lng) in enumerate(coords):
        passages.append(
            _passage(
                vehicle_id="V1",
                run_id="R1",
                seq=seq,
                stop_id=f"S{seq}",
                ts=t0 + seq * 120,
                scheduled_ts=t0 + seq * 100,
                observed=True,
                lat=lat,
                lng=lng,
            )
        )
    # Current run R2 (starts 10 min later at t0 + 600, running late by +40s per stretch)
    for seq, (lat, lng) in enumerate(coords):
        passages.append(
            _passage(
                vehicle_id="V2",
                run_id="R2",
                seq=seq,
                stop_id=f"S{seq}",
                ts=t0 + 600 + seq * 140,
                scheduled_ts=t0 + 600 + seq * 100,
                observed=True,
                lat=lat,
                lng=lng,
            )
        )
    return passages


def test_all_required_causal_features_computed() -> None:
    passages = _base_scenario()
    stretches = build_stretch_errors(passages)
    queries = build_replay_queries(stretches, passages)

    # Find R2's query at stop_sequence 2 -> 3 (one earlier stretch is fully available).
    target = next(
        q for q in queries if q.stretch.run_id == "R2" and q.stretch.from_sequence == 2
    )
    f = target.features
    assert f is not None
    assert f.remaining_stops == 1  # max_seq=3, from_seq=2
    assert f.remaining_distance_m > 350.0
    assert f.stretch_distance_m > 350.0
    assert f.scheduled_duration_s == 100.0
    # The current observed passage is available exactly at query time and is excluded.
    assert f.ping_age_s == 140.0
    assert f.from_observed == 0.0
    assert math.isclose(f.recent_vehicle_progress_ratio, 1.4, rel_tol=1e-6)
    assert f.recent_vehicle_speed_mps > 0.0
    assert f.last_stop_delay_s == 40.0
    assert f.last_stretch_error_s == 40.0
    assert f.previous_error_s == 20.0
    assert f.previous_error_age_s > 0.0
    assert f.previous_observed == 1.0
    assert f.recent_same_direction_speed_mps > 0.0
    assert math.isclose(f.recent_same_direction_pace_ratio, 1.2, rel_tol=1e-6)
    assert f.headway_s == (600 + 280) - 240
    assert f.hour == 8
    assert f.day_of_week == 3  # 2026-10-22 is Thursday
    assert f.day_type == "thu"
    assert f.coarse_day_type == "weekday"
    assert f.is_weekend == 0.0


def test_future_data_rejected_strict() -> None:
    passages = _base_scenario()
    stretches = build_stretch_errors(passages)
    geometries = build_route_geometry(passages)
    geom = geometries[(2171, "UP")]

    current = next(s for s in stretches if s.run_id == "R2" and s.from_sequence == 1)
    previous = next(s for s in stretches if s.run_id == "R1" and s.from_sequence == 1)
    future_same_p = next(p for p in passages if p.run_id == "R2" and p.stop_sequence == 2)
    future_same_s = next(s for s in stretches if s.run_id == "R2" and s.from_sequence == 1)

    # 1. Downstream same-run passage (ts > query_ts & seq > from_sequence) MUST raise
    with pytest.raises(CausalityViolationError):
        extract_causal_features_for_query(
            current=current,
            previous=previous,
            geometry=geom,
            same_run_passages=[future_same_p],
        )

    # 2. Target/downstream same-run stretch MUST raise
    with pytest.raises(CausalityViolationError):
        extract_causal_features_for_query(
            current=current,
            previous=previous,
            geometry=geom,
            same_run_stretches=[future_same_s],
        )

    # 3. Other-run passage at equal or later timestamp MUST raise
    equal_ts_other = _passage(
        vehicle_id="V9",
        run_id="R9",
        seq=1,
        stop_id="S1",
        ts=current.query_ts,
        scheduled_ts=current.query_ts,
    )
    with pytest.raises(CausalityViolationError):
        extract_causal_features_for_query(
            current=current,
            previous=previous,
            geometry=geom,
            other_run_passages=[equal_ts_other],
        )

    # 4. Other-run stretch finishing at or after query_ts MUST raise
    future_other_s = build_stretch_errors(
        [
            _passage(vehicle_id="V9", run_id="R9", seq=0, stop_id="S0", ts=current.query_ts - 50, scheduled_ts=current.query_ts - 50),
            _passage(vehicle_id="V9", run_id="R9", seq=1, stop_id="S1", ts=current.query_ts, scheduled_ts=current.query_ts),
        ]
    )[0]
    with pytest.raises(CausalityViolationError):
        extract_causal_features_for_query(
            current=current,
            previous=previous,
            geometry=geom,
            other_run_stretches=[future_other_s],
        )


def test_future_perturbation_leaves_causal_features_identical() -> None:
    base_passages = _base_scenario()
    base_queries = build_replay_queries(build_stretch_errors(base_passages), base_passages)
    base_target = next(
        q for q in base_queries if q.stretch.run_id == "R2" and q.stretch.from_sequence == 1
    )

    # Mutate R2's downstream stops (seq 2, 3) wildly AND add a chaotic future run R3 after query_ts
    t0 = int(datetime(2026, 10, 22, 8, 0, tzinfo=IST).timestamp())
    perturbed: list[PassageRow] = []
    for p in base_passages:
        if p.run_id == "R2" and p.stop_sequence >= 2:
            perturbed.append(
                _passage(
                    vehicle_id=p.vehicle_id,
                    run_id=p.run_id,
                    seq=p.stop_sequence,
                    stop_id=p.stop_id,
                    ts=p.ts + 900 * p.stop_sequence,
                    scheduled_ts=p.scheduled_ts,
                    observed=p.observed,
                    lat=p.stop_lat or 28.5,
                    lng=p.stop_lng or 77.25,
                )
            )
        else:
            perturbed.append(p)
    # Add future run starting right at base_target.stretch.query_ts
    q_ts = base_target.stretch.query_ts
    for seq in range(4):
        perturbed.append(
            _passage(
                vehicle_id="V99",
                run_id="R99",
                seq=seq,
                stop_id=f"S{seq}",
                ts=q_ts + seq * 500,
                scheduled_ts=t0 + 600 + seq * 100,
                observed=True,
                lat=28.500 + 0.004 * seq,
                lng=77.250,
            )
        )

    perturbed_queries = build_replay_queries(build_stretch_errors(perturbed), perturbed)
    perturbed_target = next(
        q for q in perturbed_queries if q.stretch.run_id == "R2" and q.stretch.from_sequence == 1
    )

    assert base_target.features is not None and perturbed_target.features is not None
    for a, b in zip(
        base_target.features.as_numeric_list(),
        perturbed_target.features.as_numeric_list(),
        strict=True,
    ):
        if math.isnan(a):
            assert math.isnan(b)
        else:
            assert a == b
    assert base_target.previous_error_s == perturbed_target.previous_error_s


def _interpolated_anchor_scenario(right_anchor_ts: int = 1028) -> list[PassageRow]:
    rows: list[PassageRow] = []
    for run_id, vehicle_id, times, schedules in (
        ("R0", "V0", (400, 500, 650), (400, 500, 600)),
        ("R1", "V1", (700, 760, right_anchor_ts), (700, 800, 900)),
        ("R2", "V2", (880, 1000, 1150), (880, 1000, 1100)),
    ):
        for seq, (ts, scheduled_ts) in enumerate(zip(times, schedules, strict=True)):
            interpolated = run_id == "R1" and seq == 1
            rows.append(
                _passage(
                    vehicle_id=vehicle_id,
                    run_id=run_id,
                    seq=seq,
                    stop_id=f"S{seq}",
                    ts=ts,
                    scheduled_ts=scheduled_ts,
                    observed=not interpolated,
                    # R1 S1 is interpolated from the later observed S2 anchor.
                    available_at=right_anchor_ts if run_id == "R1" and seq in {1, 2} else ts,
                )
            )
    return rows


def test_interpolated_future_anchor_cannot_drive_any_replay_feature() -> None:
    passages = _interpolated_anchor_scenario()
    queries = build_replay_queries(build_stretch_errors(passages), passages)
    targets = {
        q.stretch.from_sequence: q
        for q in queries
        if q.stretch.run_id == "R2"
    }

    first = targets[0]
    assert first.previous_run_id == "R0"
    assert first.previous_error_s == 0
    assert first.previous_available_at == 500
    assert first.features is not None
    # R1's 0 -> 1 stretch has event time 760 but is not knowable until 1028.
    assert math.isclose(first.features.recent_same_direction_pace_ratio, 1.25)

    second = targets[1]
    assert second.previous_run_id == "R0"
    assert second.previous_error_s == 50
    assert second.previous_available_at == 650
    assert second.features is not None
    # R1 S1 arrived at 760 but its 1028 right anchor is after the 1000 query.
    assert second.features.headway_s == 500

    # Moving the raw right anchor later must not change either as-of prediction input.
    perturbed_passages = _interpolated_anchor_scenario(right_anchor_ts=2000)
    perturbed_queries = build_replay_queries(
        build_stretch_errors(perturbed_passages), perturbed_passages
    )
    perturbed_targets = {
        q.stretch.from_sequence: q
        for q in perturbed_queries
        if q.stretch.run_id == "R2"
    }
    for sequence, target in targets.items():
        perturbed = perturbed_targets[sequence]
        assert perturbed.previous_run_id == target.previous_run_id
        assert perturbed.previous_error_s == target.previous_error_s
        assert perturbed.previous_available_at == target.previous_available_at
        assert target.features is not None and perturbed.features is not None
        for before, after in zip(
            target.features.as_numeric_list(),
            perturbed.features.as_numeric_list(),
        ):
            assert (math.isnan(before) and math.isnan(after)) or before == after


def test_source_availability_at_or_after_query_is_rejected_strict() -> None:
    passages = _interpolated_anchor_scenario()
    stretches = build_stretch_errors(passages)
    current = next(s for s in stretches if s.run_id == "R2" and s.from_sequence == 1)
    previous = next(s for s in stretches if s.run_id == "R0" and s.from_sequence == 1)
    interpolated = next(p for p in passages if p.run_id == "R1" and p.stop_sequence == 1)
    current_passage = next(p for p in passages if p.run_id == "R2" and p.stop_sequence == 1)
    prior_current = next(p for p in passages if p.run_id == "R2" and p.stop_sequence == 0)
    earlier = next(p for p in passages if p.run_id == "R0" and p.stop_sequence == 1)

    with pytest.raises(CausalityViolationError, match="available_at=1028"):
        extract_causal_features_for_query(
            current=current,
            previous=previous,
            other_run_passages=[interpolated],
        )
    with pytest.raises(CausalityViolationError, match="available_at=1000"):
        extract_causal_features_for_query(
            current=current,
            previous=previous,
            same_run_passages=[current_passage],
        )

    features = extract_causal_features_for_query(
        current=current,
        previous=previous,
        other_run_passages=[earlier],
        same_run_passages=[prior_current],
    )
    assert features.headway_s == 500
    assert features.ping_age_s == 120
