"""Rung 4: LightGBM quantile p10/p50/p90 with whole-day split-conformal calibration."""
from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np

from delhi_transit.model.features import (
    NUMERIC_FEATURE_NAMES,
    CausalFeatures,
    ReplayQuery,
    StretchError,
)
from delhi_transit.model.rungs import (
    HistoricalMedianTable,
    Prediction,
    StretchKey,
    lodo_historical_tables,
)

RUNG4_EXTRA_FEATURE_NAMES: tuple[str, ...] = (
    "hist_dow_hour_median_s",
    "hist_hour_median_s",
    "hist_stretch_median_s",
    "prev_minus_hist_s",
)
RUNG4_FEATURE_NAMES: tuple[str, ...] = NUMERIC_FEATURE_NAMES + RUNG4_EXTRA_FEATURE_NAMES
QUANTILES: dict[str, float] = {"p10": 0.10, "p50": 0.50, "p90": 0.90}


def _build_rung4_row(
    stretch_key: StretchKey,
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


def _query_matrix(queries: Sequence[ReplayQuery], tables: Any) -> np.ndarray:
    """Feature matrix for queries; ``tables`` maps a query to its historical table."""
    return np.asarray(
        [
            _build_rung4_row(q.stretch.stretch_key, q.features, tables(q))  # type: ignore[arg-type]
            for q in queries
        ],
        dtype=np.float64,
    )


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

        self.table, lodo_tables = lodo_historical_tables(fit_stretches)

        fit_obs = [q for q in fit_queries if q.stretch.target_observed and q.features is not None]
        fit_pool = fit_obs or [q for q in fit_queries if q.features is not None]
        cal_obs = [q for q in calibration_queries if q.stretch.target_observed and q.features is not None]
        cal_pool = cal_obs or [q for q in calibration_queries if q.features is not None]
        if not fit_pool or not cal_pool:
            raise ValueError("rung 4 requires ReplayQuery objects with populated CausalFeatures")

        x_fit = _query_matrix(fit_pool, lambda q: lodo_tables.get(q.stretch.day, self.table))
        y_fit = np.asarray([float(q.stretch.error_s) for q in fit_pool], dtype=np.float64)
        self._boosters = {
            name: self._fit_quantile_booster(x_fit, y_fit, alpha=alpha)
            for name, alpha in QUANTILES.items()
        }

        # Split-conformal CQR calibration on strictly later calibration day(s)
        x_cal = _query_matrix(cal_pool, lambda q: self.table)
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
        p10, p50, p90 = (
            np.asarray(self._boosters[name].predict(x_mat), dtype=np.float64)
            for name in QUANTILES
        )
        return p10, p50, p90

    def _predict_rows(self, x_mat: np.ndarray) -> list[Prediction]:
        p10, p50, p90 = _order_and_calibrate(
            *self._predict_raw_matrix(x_mat), self.conformal_quantile_s
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

    def predict_batch(self, queries: Sequence[ReplayQuery]) -> list[Prediction]:
        if not queries:
            return []
        if any(q.features is None for q in queries):
            raise ValueError("rung 4 prediction requires CausalFeatures on every ReplayQuery")
        return self._predict_rows(_query_matrix(queries, lambda q: self.table))

    def predict(self, query: ReplayQuery) -> Prediction:
        return self.predict_batch([query])[0]

    def predict_batch_from_features(
        self,
        items: Sequence[tuple[StretchKey, CausalFeatures]],
    ) -> list[Prediction]:
        if not items:
            return []
        x_mat = np.asarray(
            [_build_rung4_row(sk, feat, self.table) for sk, feat in items],
            dtype=np.float64,
        )
        return self._predict_rows(x_mat)

    def predict_from_features(self, stretch_key: StretchKey, features: CausalFeatures) -> Prediction:
        return self.predict_batch_from_features([(stretch_key, features)])[0]

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
            for name in QUANTILES
        }
        return model
