from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ps1.features import StretchError
from ps1.retime import RetimeModel, TimingRecord, scenario_windows

IST = timezone(timedelta(hours=5, minutes=30))


def test_joint_windows_do_not_add_leg_quantiles() -> None:
    scenarios = [[10.0, 20.0], [20.0, 10.0], [15.0, 15.0]]
    windows = scenario_windows(scenarios)
    assert windows["total_window_p90_s"] == 30.0
    assert windows["total_window_p10_s"] == 30.0
    assert sum(leg["window_p90_s"] for leg in windows["legs"]) == 38.0
    assert windows["total_window_p90_s"] != sum(
        leg["window_p90_s"] for leg in windows["legs"]
    )


def _record(hour: int, actual: int, scheduled: int = 100) -> TimingRecord:
    query = datetime(2026, 10, 21, hour, 0, tzinfo=IST)
    return TimingRecord(
        StretchError(
            day="2026-10-21",
            route_id=1,
            direction="UP",
            vehicle_id="v",
            run_id=f"run-{hour}",
            from_sequence=1,
            to_sequence=2,
            from_stop_id="a",
            to_stop_id="b",
            query_ts=int(query.timestamp()),
            to_ts=int((query + timedelta(seconds=actual)).timestamp()),
            scheduled_duration_s=scheduled,
            actual_duration_s=actual,
            error_s=actual - scheduled,
            from_observed=True,
            to_observed=True,
        )
    )


def _model(records: list[TimingRecord]) -> RetimeModel:
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
    return model


def test_observed_historical_profile_changes_time_of_day() -> None:
    model = _model([_record(9, 140), _record(23, 70)])
    peak = model.predict_ride(99, "x", "y", 1000, datetime(2026, 10, 22, 9, tzinfo=IST), "replay")
    quiet = model.predict_ride(99, "x", "y", 1000, datetime(2026, 10, 22, 23, tzinfo=IST), "replay")
    assert peak.duration_s == 1400
    assert quiet.duration_s == 700
    assert peak.source == quiet.source == "historical"
    assert peak.observed_coverage == quiet.observed_coverage == 0.0


def test_timetable_is_explicit_last_fallback() -> None:
    model = _model([])
    estimate = model.predict_ride(1, "a", "b", 500, datetime(2026, 10, 22, 9, tzinfo=IST), "replay")
    assert estimate.duration_s == 500
    assert estimate.source == "timetable"
    assert estimate.observed_coverage == 0.0


def test_retime_model_loads_rung4_artifact_and_times_covered_vs_uncovered_routes(tmp_path: Path) -> None:
    import json

    import pyarrow as pa
    import pyarrow.parquet as pq

    from ps1.features import PassageRow, build_replay_queries, build_stretch_errors
    from ps1.model import Rung4QuantileLightGBMModel, save_model_artifact
    from ps1.stop_passages import PASSAGE_SCHEMA

    def make_day(day: str, err: int, runs: int = 8) -> list[PassageRow]:
        y, m, d = (int(p) for p in day.split("-"))
        t0 = int(datetime(y, m, d, 8, 0, tzinfo=IST).timestamp())
        coords = [(28.500, 77.250), (28.505, 77.250), (28.510, 77.250), (28.515, 77.250)]
        rows: list[PassageRow] = []
        for r in range(runs):
            ts = t0 + r * 600
            sched = ts
            for seq, (lat, lng) in enumerate(coords):
                rows.append(
                    PassageRow(
                        day=day,
                        route_id=2171,
                        direction="UP",
                        vehicle_id=f"V{r}",
                        run_id=f"{day}_R{r}",
                        stop_sequence=seq,
                        stop_id=f"S{seq}",
                        observed=True,
                        ts=ts,
                        scheduled_ts=sched,
                        stop_lat=lat,
                        stop_lng=lng,
                        schedule_error_s=ts - sched,
                        distance_m=10.0,
                    )
                )
                sched += 100
                ts += 100 + err
        return rows

    fit_p = make_day("2026-10-05", 45, runs=10) + make_day("2026-10-06", 55, runs=10)
    cal_p = make_day("2026-10-15", 50, runs=8)
    eval_p = make_day("2026-10-22", 60, runs=6)

    fit_s = build_stretch_errors(fit_p)
    fit_q = build_replay_queries(fit_s, fit_p)
    cal_q = build_replay_queries(build_stretch_errors(cal_p), cal_p)
    rung4 = Rung4QuantileLightGBMModel(n_estimators=20, num_leaves=7, min_child_samples=5).fit(
        fit_s, fit_q, cal_q
    )

    passages_dir = tmp_path / "passages"
    passages_dir.mkdir(parents=True)
    records = [
        {
            "day": p.day,
            "route_id": p.route_id,
            "direction": p.direction,
            "vehicle_id": p.vehicle_id,
            "run_id": p.run_id,
            "stop_sequence": p.stop_sequence,
            "stop_id": p.stop_id,
            "stop_name": p.stop_id,
            "stop_lat": p.stop_lat,
            "stop_lng": p.stop_lng,
            "source": "observed",
            "observed": p.observed,
            "ts": p.ts,
            "scheduled_ts": p.scheduled_ts,
            "schedule_error_s": p.effective_schedule_error_s,
            "distance_m": p.distance_m,
            "available_at": p.source_available_at,
        }
        for p in eval_p
    ]
    pq.write_table(pa.Table.from_pylist(records, schema=PASSAGE_SCHEMA), passages_dir / "day=2026-10-22.parquet")

    save_model_artifact(rung4, tmp_path / "model", covered_routes=[2171])
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps({"selected_rung": "4"}), encoding="utf-8")

    # Also write live route stats for route 2171 and uncovered route 1920
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True)
    (live_dir / "route_stats.json").write_text(
        json.dumps(
            {
                "routes": {
                    "2171": {"fresh_vehicle_count": 5, "median_speed_mps": 4.2, "observed_coverage": 0.9},
                    "1920": {"fresh_vehicle_count": 4, "median_speed_mps": 5.0, "observed_coverage": 0.85},
                }
            }
        ),
        encoding="utf-8",
    )

    model = RetimeModel(tmp_path, selection_path=selection_path)
    assert model.selected_rung == "4"
    assert model._artifact is not None and model._artifact.rung == "4"

    # 1. Covered route 2171 multi-stop leg in October replay -> actually timed by rung 4
    depart_replay = datetime(2026, 10, 22, 9, 15, tzinfo=IST)
    est_covered = model.predict_ride(2171, "S0", "S3", 300.0, depart_replay, "replay")
    assert est_covered.source == "replay"
    assert est_covered.timing_source == "model_replay"
    assert est_covered.coverage_note is not None
    assert "rung 4 LightGBM" in est_covered.coverage_note
    assert "3 pattern stretch" in est_covered.coverage_note
    assert est_covered.p10_s <= est_covered.duration_s <= est_covered.p90_s
    assert est_covered.duration_s > 360.0  # learned +50s/stretch error across 3 stretches

    # 2. Covered route 2171 in live mode -> actually timed by rung 4 + live VehiclePositions
    depart_live = datetime(2026, 11, 24, 9, 0, tzinfo=IST)
    est_live_cov = model.predict_ride(2171, "S0", "S2", 200.0, depart_live, "live")
    assert est_live_cov.source == "live"
    assert est_live_cov.timing_source == "model_live"
    assert est_live_cov.coverage_note is not None
    assert "live VehiclePositions" in est_live_cov.coverage_note
    assert "rung 4 LightGBM" in est_live_cov.coverage_note
    assert est_live_cov.p10_s <= est_live_cov.duration_s <= est_live_cov.p90_s

    # 3. Uncovered route 1920 in live mode -> live + corridor historical fallback (never claims rung 4 timed it)
    est_live_uncov = model.predict_ride(1920, "X", "Y", 600.0, depart_live, "live")
    assert est_live_uncov.source == "live"
    assert est_live_uncov.timing_source == "model_live"
    assert est_live_uncov.coverage_note is not None
    assert "outside rung 4 model scope" in est_live_uncov.coverage_note
    assert "rung 4 LightGBM" not in est_live_uncov.coverage_note

    # 4. Uncovered route 1920 in replay mode -> historical fallback at hour 8, timetable fallback at hour 23
    est_replay_uncov = model.predict_ride(1920, "X", "Y", 600.0, datetime(2026, 10, 22, 8, 45, tzinfo=IST), "replay")
    assert est_replay_uncov.source == "historical"
    assert est_replay_uncov.timing_source == "historical_fallback"
    assert est_replay_uncov.coverage_note is not None
    assert "outside rung 4 model scope" in est_replay_uncov.coverage_note

    est_replay_tt = model.predict_ride(1920, "X", "Y", 600.0, datetime(2026, 10, 22, 23, 0, tzinfo=IST), "replay")
    assert est_replay_tt.source == "timetable"
    assert est_replay_tt.timing_source == "timetable_fallback"

