"""Model ladder (rungs 0, 1, 1h, 2, 3, 4) with conformal calibration and artifact persistence."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

import lightgbm as lgb
import numpy as np
from sklearn.linear_model import HuberRegressor

from ps1.features import (
    NUMERIC_FEATURE_NAMES,
    CausalFeatures,
    ReplayQuery,
    StretchError,
)

ALL_RUNGS: tuple[str, ...] = ("0", "1", "1h", "2", "3", "4")
LADDER_ORDER: tuple[str, ...] = ("0", "1", "1h", "2", "3", "4")

RUNG4_EXTRA_FEATURE_NAMES: tuple[str, ...] = (
    "hist_dow_hour_median_s",
    "hist_hour_median_s",
    "hist_stretch_median_s",
    "prev_minus_hist_s",
)
RUNG4_FEATURE_NAMES: tuple[str, ...] = NUMERIC_FEATURE_NAMES + RUNG4_EXTRA_FEATURE_NAMES


@dataclass(frozen=True)
class Prediction:
    error_s: float
    used_fallback: bool = False
    p10_s: float | None = None
    p50_s: float | None = None
    p90_s: float | None = None


class BaselineModel:
    """Timetable (0), previous-run persistence (1), or hourly median (1h)."""

    def __init__(self, rung: str) -> None:
        if rung not in {"0", "1", "1h"}:
            raise ValueError(f"unknown baseline rung: {rung}")
        self.rung = rung
        self._medians: dict[tuple[int, str, str, str, int], float] = {}

    def fit(self, training_stretches: Iterable[StretchError]) -> BaselineModel:
        """Fit historical medians using only the supplied whole training days."""
        if self.rung != "1h":
            return self
        errors: dict[tuple[int, str, str, str, int], list[int]] = defaultdict(list)
        for stretch in training_stretches:
            errors[(*stretch.stretch_key, stretch.query_hour)].append(stretch.error_s)
        self._medians = {key: float(median(values)) for key, values in errors.items()}
        return self

    def predict(self, query: ReplayQuery) -> Prediction:
        if self.rung == "0":
            return Prediction(0.0)
        if self.rung == "1":
            return Prediction(float(query.previous_error_s))
        key = (*query.stretch.stretch_key, query.stretch.query_hour)
        if key not in self._medians:
            return Prediction(0.0, used_fallback=True)
        return Prediction(self._medians[key])

    def predict_batch(self, queries: Sequence[ReplayQuery]) -> list[Prediction]:
        return [self.predict(q) for q in queries]

    def predict_from_features(
        self,
        stretch_key: tuple[int, str, str, str],
        features: CausalFeatures,
    ) -> Prediction:
        if self.rung == "0":
            return Prediction(0.0)
        if self.rung == "1":
            if math.isfinite(features.previous_error_s):
                return Prediction(float(features.previous_error_s))
            return Prediction(0.0, used_fallback=True)
        key = (*stretch_key, features.hour)
        if key not in self._medians:
            return Prediction(0.0, used_fallback=True)
        return Prediction(self._medians[key])

    def to_dict(self) -> dict[str, Any]:
        return {
            "rung": self.rung,
            "medians": [
                {"key": list(k), "value": v} for k, v in sorted(self._medians.items())
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BaselineModel:
        model = cls(str(payload["rung"]))
        model._medians = {
            (int(item["key"][0]), str(item["key"][1]), str(item["key"][2]), str(item["key"][3]), int(item["key"][4])): float(item["value"])
            for item in payload.get("medians", [])
        }
        return model


class HistoricalMedianTable:
    """Hierarchical observed-target median table conditioned on stretch, day-type, and hour."""

    def __init__(self, min_day_type_samples: int = 3) -> None:
        self.min_day_type_samples = min_day_type_samples
        self.dow_hour: dict[tuple[int, str, str, str, int, int], float] = {}
        self.coarse_hour: dict[tuple[int, str, str, str, str, int], float] = {}
        self.hour: dict[tuple[int, str, str, str, int], float] = {}
        self.stretch: dict[tuple[int, str, str, str], float] = {}
        self.route_hour: dict[tuple[int, str, int], float] = {}

    def fit(self, training_stretches: Iterable[StretchError]) -> HistoricalMedianTable:
        all_stretches = list(training_stretches)
        observed = [s for s in all_stretches if s.target_observed]
        pool = observed or all_stretches

        by_dow_hour: dict[tuple[int, str, str, str, int, int], list[int]] = defaultdict(list)
        by_coarse_hour: dict[tuple[int, str, str, str, str, int], list[int]] = defaultdict(list)
        by_hour: dict[tuple[int, str, str, str, int], list[int]] = defaultdict(list)
        by_stretch: dict[tuple[int, str, str, str], list[int]] = defaultdict(list)
        by_route_hour: dict[tuple[int, str, int], list[int]] = defaultdict(list)

        for s in pool:
            sk = s.stretch_key
            h = s.query_hour
            by_dow_hour[(*sk, s.day_of_week, h)].append(s.error_s)
            by_coarse_hour[(*sk, s.coarse_day_type, h)].append(s.error_s)
            by_hour[(*sk, h)].append(s.error_s)
            by_stretch[sk].append(s.error_s)
            by_route_hour[(s.route_id, s.direction, h)].append(s.error_s)

        self.dow_hour = {
            k: float(median(v))
            for k, v in by_dow_hour.items()
            if len(v) >= self.min_day_type_samples
        }
        self.coarse_hour = {
            k: float(median(v))
            for k, v in by_coarse_hour.items()
            if len(v) >= self.min_day_type_samples
        }
        self.hour = {k: float(median(v)) for k, v in by_hour.items()}
        self.stretch = {k: float(median(v)) for k, v in by_stretch.items()}
        self.route_hour = {k: float(median(v)) for k, v in by_route_hour.items()}
        return self

    def lookup(
        self,
        stretch_key: tuple[int, str, str, str],
        day_of_week: int,
        coarse_day_type: str,
        hour: int,
    ) -> tuple[float, bool]:
        """Return (median_error_s, used_fallback) using hierarchical day-type fallback."""
        dow_key = (*stretch_key, day_of_week, hour)
        if dow_key in self.dow_hour:
            return self.dow_hour[dow_key], False
        coarse_key = (*stretch_key, coarse_day_type, hour)
        if coarse_key in self.coarse_hour:
            return self.coarse_hour[coarse_key], False
        hour_key = (*stretch_key, hour)
        if hour_key in self.hour:
            return self.hour[hour_key], False
        if stretch_key in self.stretch:
            return self.stretch[stretch_key], False
        route_key = (stretch_key[0], stretch_key[1], hour)
        if route_key in self.route_hour:
            return self.route_hour[route_key], True
        return 0.0, True

    def lookup_levels(
        self,
        stretch_key: tuple[int, str, str, str],
        day_of_week: int,
        coarse_day_type: str,
        hour: int,
    ) -> tuple[float, float, float]:
        """Return (dow_hour_median, hour_median, stretch_median) for feature encoding."""
        dow_val, _ = self.lookup(stretch_key, day_of_week, coarse_day_type, hour)
        hour_key = (*stretch_key, hour)
        route_key = (stretch_key[0], stretch_key[1], hour)
        stretch_val = self.stretch.get(stretch_key, self.route_hour.get(route_key, 0.0))
        hour_val = self.hour.get(hour_key, stretch_val)
        return dow_val, hour_val, stretch_val

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_day_type_samples": self.min_day_type_samples,
            "dow_hour": [[*k, v] for k, v in sorted(self.dow_hour.items())],
            "coarse_hour": [[*k, v] for k, v in sorted(self.coarse_hour.items())],
            "hour": [[*k, v] for k, v in sorted(self.hour.items())],
            "stretch": [[*k, v] for k, v in sorted(self.stretch.items())],
            "route_hour": [[*k, v] for k, v in sorted(self.route_hour.items())],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HistoricalMedianTable:
        table = cls(min_day_type_samples=int(payload.get("min_day_type_samples", 3)))
        table.dow_hour = {
            (int(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4]), int(r[5])): float(r[6])
            for r in payload.get("dow_hour", [])
        }
        table.coarse_hour = {
            (int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), int(r[5])): float(r[6])
            for r in payload.get("coarse_hour", [])
        }
        table.hour = {
            (int(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4])): float(r[5])
            for r in payload.get("hour", [])
        }
        table.stretch = {
            (int(r[0]), str(r[1]), str(r[2]), str(r[3])): float(r[4])
            for r in payload.get("stretch", [])
        }
        table.route_hour = {
            (int(r[0]), str(r[1]), int(r[2])): float(r[3])
            for r in payload.get("route_hour", [])
        }
        return table


def _lodo_historical_tables(
    stretches: Sequence[StretchError],
    min_day_type_samples: int = 3,
) -> tuple[HistoricalMedianTable, dict[str, HistoricalMedianTable]]:
    """Build full-fit table plus whole-day leave-one-day-out tables for training features."""
    full_table = HistoricalMedianTable(min_day_type_samples=min_day_type_samples).fit(stretches)
    days = sorted({s.day for s in stretches})
    if len(days) <= 1:
        return full_table, {d: full_table for d in days}
    by_day: dict[str, list[StretchError]] = defaultdict(list)
    for s in stretches:
        by_day[s.day].append(s)
    lodo: dict[str, HistoricalMedianTable] = {}
    for held_day in days:
        other_stretches = [s for day, items in by_day.items() if day != held_day for s in items]
        lodo[held_day] = HistoricalMedianTable(min_day_type_samples=min_day_type_samples).fit(
            other_stretches
        )
    return full_table, lodo


class Rung2DayTypeModel:
    """Rung 2: historical stretch-hour median conditioned on day-type."""

    rung = "2"

    def __init__(self, min_day_type_samples: int = 3) -> None:
        self.table = HistoricalMedianTable(min_day_type_samples=min_day_type_samples)

    def fit(self, training_stretches: Iterable[StretchError]) -> Rung2DayTypeModel:
        self.table.fit(training_stretches)
        return self

    def predict(self, query: ReplayQuery) -> Prediction:
        s = query.stretch
        value, used_fallback = self.table.lookup(
            s.stretch_key, s.day_of_week, s.coarse_day_type, s.query_hour
        )
        return Prediction(error_s=value, used_fallback=used_fallback)

    def predict_batch(self, queries: Sequence[ReplayQuery]) -> list[Prediction]:
        return [self.predict(q) for q in queries]

    def predict_from_features(
        self,
        stretch_key: tuple[int, str, str, str],
        features: CausalFeatures,
    ) -> Prediction:
        value, used_fallback = self.table.lookup(
            stretch_key, features.day_of_week, features.coarse_day_type, features.hour
        )
        return Prediction(error_s=value, used_fallback=used_fallback)

    def to_dict(self) -> dict[str, Any]:
        return {"rung": self.rung, "table": self.table.to_dict()}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Rung2DayTypeModel:
        model = cls()
        model.table = HistoricalMedianTable.from_dict(payload["table"])
        return model


def _rung3_delay_vector(features: CausalFeatures | None, scheduled_duration_s: float) -> list[float]:
    if features is None:
        return [0.0, 0.0, 0.0, 0.0]
    last_stretch = (
        max(-600.0, min(1200.0, features.last_stretch_error_s))
        if math.isfinite(features.last_stretch_error_s)
        else 0.0
    )
    has_last_stretch = 1.0 if math.isfinite(features.last_stretch_error_s) else 0.0
    last_stop = (
        max(-1800.0, min(3600.0, features.last_stop_delay_s))
        if math.isfinite(features.last_stop_delay_s)
        else 0.0
    )
    if math.isfinite(features.recent_vehicle_progress_ratio):
        pace_delta = max(
            -600.0,
            min(1200.0, (features.recent_vehicle_progress_ratio - 1.0) * scheduled_duration_s),
        )
    else:
        pace_delta = 0.0
    return [last_stretch, last_stop, pace_delta, has_last_stretch]


class Rung3LastDelayModel:
    """Rung 3: Rung 2 day-type baseline + robust linear adjustment from this bus's causal delay."""

    rung = "3"

    def __init__(self, min_day_type_samples: int = 3) -> None:
        self.min_day_type_samples = min_day_type_samples
        self.table = HistoricalMedianTable(min_day_type_samples=min_day_type_samples)
        self.intercept: float = 0.0
        self.coef: list[float] = [0.0, 0.0, 0.0, 0.0]

    def fit(
        self,
        training_stretches: Iterable[StretchError],
        training_queries: Sequence[ReplayQuery],
    ) -> Rung3LastDelayModel:
        stretches = list(training_stretches)
        self.table, lodo_tables = _lodo_historical_tables(
            stretches, min_day_type_samples=self.min_day_type_samples
        )
        observed_q = [q for q in training_queries if q.stretch.target_observed]
        pool = observed_q or list(training_queries)
        if len(pool) < 10:
            self.intercept = 0.0
            self.coef = [0.0, 0.0, 0.0, 0.0]
            return self

        x_rows: list[list[float]] = []
        residuals: list[float] = []
        for q in pool:
            s = q.stretch
            base_table = lodo_tables.get(s.day, self.table)
            base_val, _ = base_table.lookup(
                s.stretch_key, s.day_of_week, s.coarse_day_type, s.query_hour
            )
            x_rows.append(_rung3_delay_vector(q.features, float(s.scheduled_duration_s)))
            residuals.append(float(s.error_s) - base_val)

        x_mat = np.asarray(x_rows, dtype=np.float64)
        y_vec = np.asarray(residuals, dtype=np.float64)
        reg = HuberRegressor(alpha=10.0, epsilon=1.25, max_iter=300)
        reg.fit(x_mat, y_vec)
        self.intercept = float(reg.intercept_)
        self.coef = [float(c) for c in reg.coef_]
        return self

    def _adjust(self, base_val: float, features: CausalFeatures | None, scheduled_s: float) -> float:
        vec = _rung3_delay_vector(features, scheduled_s)
        delta = self.intercept + sum(w * x for w, x in zip(self.coef, vec, strict=True))
        return base_val + delta

    def predict(self, query: ReplayQuery) -> Prediction:
        s = query.stretch
        base_val, used_fallback = self.table.lookup(
            s.stretch_key, s.day_of_week, s.coarse_day_type, s.query_hour
        )
        pred = self._adjust(base_val, query.features, float(s.scheduled_duration_s))
        return Prediction(error_s=pred, used_fallback=used_fallback)

    def predict_batch(self, queries: Sequence[ReplayQuery]) -> list[Prediction]:
        return [self.predict(q) for q in queries]

    def predict_from_features(
        self,
        stretch_key: tuple[int, str, str, str],
        features: CausalFeatures,
    ) -> Prediction:
        base_val, used_fallback = self.table.lookup(
            stretch_key, features.day_of_week, features.coarse_day_type, features.hour
        )
        pred = self._adjust(base_val, features, features.scheduled_duration_s)
        return Prediction(error_s=pred, used_fallback=used_fallback)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rung": self.rung,
            "table": self.table.to_dict(),
            "intercept": self.intercept,
            "coef": list(self.coef),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Rung3LastDelayModel:
        model = cls()
        model.table = HistoricalMedianTable.from_dict(payload["table"])
        model.intercept = float(payload["intercept"])
        model.coef = [float(c) for c in payload["coef"]]
        return model


def _build_rung4_row(
    stretch_key: tuple[int, str, str, str],
    features: CausalFeatures,
    table: HistoricalMedianTable,
) -> list[float]:
    dow_med, hour_med, stretch_med = table.lookup_levels(
        stretch_key, features.day_of_week, features.coarse_day_type, features.hour
    )
    prev_minus_hist = (
        features.previous_error_s - hour_med
        if math.isfinite(features.previous_error_s)
        else math.nan
    )
    return features.as_numeric_list() + [dow_med, hour_med, stretch_med, prev_minus_hist]


def _order_and_calibrate(
    raw_p10: np.ndarray,
    raw_p50: np.ndarray,
    raw_p90: np.ndarray,
    conformal_q: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Guarantee p10 <= p50 <= p90 after CQR split-conformal calibration."""
    stacked = np.sort(np.vstack([raw_p10, raw_p50, raw_p90]), axis=0)
    lo, mid, hi = stacked[0], stacked[1], stacked[2]
    p50 = mid
    p10 = np.minimum(p50, lo - conformal_q)
    p90 = np.maximum(p50, hi + conformal_q)
    return p10, p50, p90


class Rung4QuantileLightGBMModel:
    """Rung 4: LightGBM quantile p10/p50/p90 with whole-day split-conformal calibration."""

    rung = "4"

    def __init__(
        self,
        *,
        nominal_coverage: float = 0.80,
        n_estimators: int = 220,
        learning_rate: float = 0.05,
        num_leaves: int = 31,
        min_child_samples: int = 40,
        n_jobs: int = 4,
        random_state: int = 42,
    ) -> None:
        self.nominal_coverage = nominal_coverage
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.num_leaves = num_leaves
        self.min_child_samples = min_child_samples
        self.n_jobs = n_jobs
        self.random_state = random_state

        self.table = HistoricalMedianTable()
        self._boosters: dict[str, lgb.Booster] = {}
        self.conformal_quantile_s: float = 0.0
        self.calibration_info: dict[str, Any] = {}

    def _fit_quantile_booster(
        self,
        x_fit: np.ndarray,
        y_fit: np.ndarray,
        alpha: float,
    ) -> lgb.Booster:
        min_child = min(self.min_child_samples, max(5, len(y_fit) // 10))
        reg = lgb.LGBMRegressor(
            objective="quantile",
            alpha=alpha,
            n_estimators=self.n_estimators,
            learning_rate=self.learning_rate,
            num_leaves=self.num_leaves,
            min_child_samples=min_child,
            subsample=0.8,
            subsample_freq=1,
            colsample_bytree=0.85,
            reg_alpha=1.0,
            reg_lambda=5.0,
            n_jobs=self.n_jobs,
            random_state=self.random_state,
            verbosity=-1,
        )
        reg.fit(x_fit, y_fit)
        return reg.booster_

    def fit(
        self,
        fit_stretches: Sequence[StretchError],
        fit_queries: Sequence[ReplayQuery],
        calibration_queries: Sequence[ReplayQuery],
    ) -> Rung4QuantileLightGBMModel:
        """Fit quantile models on whole fit_days and calibrate on disjoint later calibration_days."""
        fit_days = sorted({q.stretch.day for q in fit_queries})
        cal_days = sorted({q.stretch.day for q in calibration_queries})
        if not fit_days or not cal_days:
            raise ValueError("rung 4 requires non-empty fit_queries and calibration_queries")
        overlap = set(fit_days) & set(cal_days)
        if overlap:
            raise ValueError(f"fit and calibration days overlap: {sorted(overlap)}")
        if min(cal_days) <= max(fit_days):
            raise ValueError(
                f"calibration days {cal_days} must be strictly later than fit days {fit_days}"
            )

        self.table, lodo_tables = _lodo_historical_tables(fit_stretches)

        fit_obs = [q for q in fit_queries if q.stretch.target_observed and q.features is not None]
        fit_pool = fit_obs or [q for q in fit_queries if q.features is not None]
        cal_obs = [q for q in calibration_queries if q.stretch.target_observed and q.features is not None]
        cal_pool = cal_obs or [q for q in calibration_queries if q.features is not None]
        if not fit_pool or not cal_pool:
            raise ValueError("rung 4 requires ReplayQuery objects with populated CausalFeatures")

        x_fit = np.asarray(
            [
                _build_rung4_row(
                    q.stretch.stretch_key,
                    q.features,  # type: ignore[arg-type]
                    lodo_tables.get(q.stretch.day, self.table),
                )
                for q in fit_pool
            ],
            dtype=np.float64,
        )
        y_fit = np.asarray([float(q.stretch.error_s) for q in fit_pool], dtype=np.float64)

        self._boosters = {
            "p10": self._fit_quantile_booster(x_fit, y_fit, alpha=0.10),
            "p50": self._fit_quantile_booster(x_fit, y_fit, alpha=0.50),
            "p90": self._fit_quantile_booster(x_fit, y_fit, alpha=0.90),
        }

        # Split-conformal CQR calibration on strictly later calibration day(s)
        x_cal = np.asarray(
            [
                _build_rung4_row(
                    q.stretch.stretch_key,
                    q.features,  # type: ignore[arg-type]
                    self.table,
                )
                for q in cal_pool
            ],
            dtype=np.float64,
        )
        y_cal = np.asarray([float(q.stretch.error_s) for q in cal_pool], dtype=np.float64)
        raw_p10, raw_p50, raw_p90 = self._predict_raw_matrix(x_cal)
        raw_lo, _, raw_hi = _order_and_calibrate(raw_p10, raw_p50, raw_p90, 0.0)

        scores = np.maximum(raw_lo - y_cal, y_cal - raw_hi)
        n_cal = len(y_cal)
        alpha = 1.0 - self.nominal_coverage
        level = min(1.0, math.ceil((n_cal + 1) * (1.0 - alpha)) / n_cal)
        self.conformal_quantile_s = float(np.quantile(scores, level, method="higher"))

        cal_p10, _, cal_p90 = _order_and_calibrate(
            raw_p10, raw_p50, raw_p90, self.conformal_quantile_s
        )
        raw_cov = 100.0 * float(np.mean((y_cal >= raw_lo) & (y_cal <= raw_hi)))
        cal_cov = 100.0 * float(np.mean((y_cal >= cal_p10) & (y_cal <= cal_p90)))
        self.calibration_info = {
            "method": "split_conformal_cqr",
            "nominal_coverage": self.nominal_coverage,
            "alpha": round(alpha, 6),
            "fit_days": fit_days,
            "calibration_days": cal_days,
            "fit_queries": len(fit_pool),
            "calibration_queries": n_cal,
            "conformal_quantile_s": round(self.conformal_quantile_s, 6),
            "raw_calibration_coverage_pct": round(raw_cov, 6),
            "calibrated_calibration_coverage_pct": round(cal_cov, 6),
            "calibration_mean_width_s": round(float(np.mean(cal_p90 - cal_p10)), 6),
            "calibration_median_width_s": round(float(np.median(cal_p90 - cal_p10)), 6),
        }
        return self

    def _predict_raw_matrix(
        self, x_mat: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        p10 = np.asarray(self._boosters["p10"].predict(x_mat), dtype=np.float64)
        p50 = np.asarray(self._boosters["p50"].predict(x_mat), dtype=np.float64)
        p90 = np.asarray(self._boosters["p90"].predict(x_mat), dtype=np.float64)
        return p10, p50, p90

    def predict_batch(self, queries: Sequence[ReplayQuery]) -> list[Prediction]:
        if not queries:
            return []
        missing_features = [q for q in queries if q.features is None]
        if missing_features:
            raise ValueError("rung 4 prediction requires CausalFeatures on every ReplayQuery")
        x_mat = np.asarray(
            [
                _build_rung4_row(
                    q.stretch.stretch_key,
                    q.features,  # type: ignore[arg-type]
                    self.table,
                )
                for q in queries
            ],
            dtype=np.float64,
        )
        raw_p10, raw_p50, raw_p90 = self._predict_raw_matrix(x_mat)
        p10, p50, p90 = _order_and_calibrate(
            raw_p10, raw_p50, raw_p90, self.conformal_quantile_s
        )
        return [
            Prediction(
                error_s=float(mid),
                used_fallback=False,
                p10_s=float(lo),
                p50_s=float(mid),
                p90_s=float(hi),
            )
            for lo, mid, hi in zip(p10, p50, p90, strict=True)
        ]

    def predict(self, query: ReplayQuery) -> Prediction:
        return self.predict_batch([query])[0]

    def predict_from_features(
        self,
        stretch_key: tuple[int, str, str, str],
        features: CausalFeatures,
    ) -> Prediction:
        x_mat = np.asarray(
            [_build_rung4_row(stretch_key, features, self.table)],
            dtype=np.float64,
        )
        raw_p10, raw_p50, raw_p90 = self._predict_raw_matrix(x_mat)
        p10, p50, p90 = _order_and_calibrate(
            raw_p10, raw_p50, raw_p90, self.conformal_quantile_s
        )
        return Prediction(
            error_s=float(p50[0]),
            used_fallback=False,
            p10_s=float(p10[0]),
            p50_s=float(p50[0]),
            p90_s=float(p90[0]),
        )

    def predict_batch_from_features(
        self,
        items: Sequence[tuple[tuple[int, str, str, str], CausalFeatures]],
    ) -> list[Prediction]:
        if not items:
            return []
        x_mat = np.asarray(
            [_build_rung4_row(sk, feat, self.table) for sk, feat in items],
            dtype=np.float64,
        )
        raw_p10, raw_p50, raw_p90 = self._predict_raw_matrix(x_mat)
        p10, p50, p90 = _order_and_calibrate(
            raw_p10, raw_p50, raw_p90, self.conformal_quantile_s
        )
        return [
            Prediction(
                error_s=float(mid),
                used_fallback=False,
                p10_s=float(lo),
                p50_s=float(mid),
                p90_s=float(hi),
            )
            for lo, mid, hi in zip(p10, p50, p90, strict=True)
        ]

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for name, booster in self._boosters.items():
            booster.save_model(str(directory / f"lgbm_{name}.txt"))
        (directory / "rung4_state.json").write_text(
            json.dumps(
                {
                    "rung": self.rung,
                    "nominal_coverage": self.nominal_coverage,
                    "conformal_quantile_s": self.conformal_quantile_s,
                    "calibration_info": self.calibration_info,
                    "feature_names": list(RUNG4_FEATURE_NAMES),
                    "table": self.table.to_dict(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, directory: Path) -> Rung4QuantileLightGBMModel:
        state = json.loads((directory / "rung4_state.json").read_text(encoding="utf-8"))
        model = cls(nominal_coverage=float(state.get("nominal_coverage", 0.80)))
        model.conformal_quantile_s = float(state["conformal_quantile_s"])
        model.calibration_info = dict(state.get("calibration_info", {}))
        model.table = HistoricalMedianTable.from_dict(state["table"])
        model._boosters = {
            name: lgb.Booster(model_file=str(directory / f"lgbm_{name}.txt"))
            for name in ("p10", "p50", "p90")
        }
        return model


@dataclass
class LoadedModelArtifact:
    """Persisted selected-rung model and metadata loaded by the journeys API."""

    rung: str
    model: BaselineModel | Rung2DayTypeModel | Rung3LastDelayModel | Rung4QuantileLightGBMModel
    covered_routes: frozenset[int]
    metadata: dict[str, Any]

    def predict_from_features(
        self,
        stretch_key: tuple[int, str, str, str],
        features: CausalFeatures,
    ) -> Prediction:
        return self.model.predict_from_features(stretch_key, features)

    def predict_batch_from_features(
        self,
        items: Sequence[tuple[tuple[int, str, str, str], CausalFeatures]],
    ) -> list[Prediction]:
        if isinstance(self.model, Rung4QuantileLightGBMModel):
            return self.model.predict_batch_from_features(items)
        return [self.model.predict_from_features(sk, feat) for sk, feat in items]


def save_model_artifact(
    model: BaselineModel | Rung2DayTypeModel | Rung3LastDelayModel | Rung4QuantileLightGBMModel,
    model_dir: Path,
    *,
    covered_routes: Iterable[int],
    extra_metadata: dict[str, Any] | None = None,
) -> Path:
    """Persist the selected model artifact cleanly under data/model/."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    rung = str(model.rung)
    if isinstance(model, Rung4QuantileLightGBMModel):
        model.save(model_dir)
        artifact_type = "lgbm_quantile_conformal"
    else:
        (model_dir / f"rung_{rung}_state.json").write_text(
            json.dumps(model.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        artifact_type = f"rung_{rung}_table"

    metadata: dict[str, Any] = {
        "selected_rung": rung,
        "artifact_type": artifact_type,
        "covered_routes": sorted(int(r) for r in set(covered_routes)),
        "feature_names": list(RUNG4_FEATURE_NAMES if rung == "4" else NUMERIC_FEATURE_NAMES),
    }
    if isinstance(model, Rung4QuantileLightGBMModel):
        metadata["calibration"] = model.calibration_info
    if extra_metadata:
        metadata.update(extra_metadata)
    metadata_path = model_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata_path


def load_model_artifact(model_dir: Path) -> LoadedModelArtifact | None:
    """Load the persisted selected-rung model artifact if present."""
    model_dir = Path(model_dir)
    metadata_path = model_dir / "metadata.json"
    if not metadata_path.exists():
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    rung = str(metadata["selected_rung"]).removeprefix("rung")
    covered = frozenset(int(r) for r in metadata.get("covered_routes", []))
    model: BaselineModel | Rung2DayTypeModel | Rung3LastDelayModel | Rung4QuantileLightGBMModel
    if rung == "4":
        model = Rung4QuantileLightGBMModel.load(model_dir)
    elif rung == "3":
        payload = json.loads((model_dir / "rung_3_state.json").read_text(encoding="utf-8"))
        model = Rung3LastDelayModel.from_dict(payload)
    elif rung == "2":
        payload = json.loads((model_dir / "rung_2_state.json").read_text(encoding="utf-8"))
        model = Rung2DayTypeModel.from_dict(payload)
    elif rung in {"0", "1", "1h"}:
        state_path = model_dir / f"rung_{rung}_state.json"
        if state_path.exists():
            model = BaselineModel.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
        else:
            model = BaselineModel(rung)
    else:
        raise ValueError(f"unsupported persisted rung: {rung}")
    return LoadedModelArtifact(
        rung=rung,
        model=model,
        covered_routes=covered,
        metadata=metadata,
    )
