"""Model ladder rungs 0, 1, 1h and 2 with their historical median tables."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

from delhi_transit.model.causal_core import CausalFeatures
from delhi_transit.model.features import ReplayQuery, StretchError

ALL_RUNGS: tuple[str, ...] = ("0", "1", "1h", "2", "4")
BASELINE_RUNGS: tuple[str, ...] = ("0", "1", "1h")

StretchKey = tuple[int, str, str, str]


@dataclass(frozen=True)
class Prediction:
    error_s: float
    used_fallback: bool = False
    p10_s: float | None = None
    p50_s: float | None = None
    p90_s: float | None = None


class RungModel:
    """Shared batch interfaces for rungs that predict one query at a time."""

    rung: str

    def predict(self, query: ReplayQuery) -> Prediction:
        raise NotImplementedError

    def predict_from_features(self, stretch_key: StretchKey, features: CausalFeatures) -> Prediction:
        raise NotImplementedError

    def predict_batch(self, queries: Sequence[ReplayQuery]) -> list[Prediction]:
        return [self.predict(q) for q in queries]

    def predict_batch_from_features(
        self, items: Sequence[tuple[StretchKey, CausalFeatures]]
    ) -> list[Prediction]:
        return [self.predict_from_features(sk, feat) for sk, feat in items]


def _parse_key(row: Sequence[Any], types: Sequence[Callable[[Any], Any]]) -> tuple[Any, ...]:
    return tuple(cast(value) for cast, value in zip(types, row, strict=False))


class BaselineModel(RungModel):
    """Timetable (0), previous-run persistence (1), or hourly median (1h)."""

    def __init__(self, rung: str) -> None:
        if rung not in BASELINE_RUNGS:
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

    def _hourly_median(self, stretch_key: StretchKey, hour: int) -> Prediction:
        key = (*stretch_key, hour)
        if key not in self._medians:
            return Prediction(0.0, used_fallback=True)
        return Prediction(self._medians[key])

    def predict(self, query: ReplayQuery) -> Prediction:
        if self.rung == "0":
            return Prediction(0.0)
        if self.rung == "1":
            return Prediction(float(query.previous_error_s))
        return self._hourly_median(query.stretch.stretch_key, query.stretch.query_hour)

    def predict_from_features(self, stretch_key: StretchKey, features: CausalFeatures) -> Prediction:
        if self.rung == "0":
            return Prediction(0.0)
        if self.rung == "1":
            if math.isfinite(features.previous_error_s):
                return Prediction(float(features.previous_error_s))
            return Prediction(0.0, used_fallback=True)
        return self._hourly_median(stretch_key, features.hour)

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
            _parse_key(item["key"], (int, str, str, str, int)): float(item["value"])
            for item in payload.get("medians", [])
        }
        return model


# Serialised level name -> key column types, in fallback order.
_TABLE_LEVELS: dict[str, tuple[Callable[[Any], Any], ...]] = {
    "dow_hour": (int, str, str, str, int, int),
    "coarse_hour": (int, str, str, str, str, int),
    "hour": (int, str, str, str, int),
    "stretch": (int, str, str, str),
    "route_hour": (int, str, int),
}


class HistoricalMedianTable:
    """Hierarchical observed-target median table conditioned on stretch, day-type, and hour."""

    def __init__(self, min_day_type_samples: int = 3) -> None:
        self.min_day_type_samples = min_day_type_samples
        self.dow_hour: dict[tuple[int, str, str, str, int, int], float] = {}
        self.coarse_hour: dict[tuple[int, str, str, str, str, int], float] = {}
        self.hour: dict[tuple[int, str, str, str, int], float] = {}
        self.stretch: dict[StretchKey, float] = {}
        self.route_hour: dict[tuple[int, str, int], float] = {}

    def fit(self, training_stretches: Iterable[StretchError]) -> HistoricalMedianTable:
        all_stretches = list(training_stretches)
        observed = [s for s in all_stretches if s.target_observed]
        pool = observed or all_stretches

        samples: dict[str, dict[tuple[Any, ...], list[int]]] = {
            level: defaultdict(list) for level in _TABLE_LEVELS
        }
        for s in pool:
            sk = s.stretch_key
            h = s.query_hour
            samples["dow_hour"][(*sk, s.day_of_week, h)].append(s.error_s)
            samples["coarse_hour"][(*sk, s.coarse_day_type, h)].append(s.error_s)
            samples["hour"][(*sk, h)].append(s.error_s)
            samples["stretch"][sk].append(s.error_s)
            samples["route_hour"][(s.route_id, s.direction, h)].append(s.error_s)

        min_samples = {"dow_hour": self.min_day_type_samples, "coarse_hour": self.min_day_type_samples}
        for level, by_key in samples.items():
            floor = min_samples.get(level, 0)
            setattr(
                self,
                level,
                {k: float(median(v)) for k, v in by_key.items() if len(v) >= floor},
            )
        return self

    def lookup(
        self,
        stretch_key: StretchKey,
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
        stretch_key: StretchKey,
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
            **{
                level: [[*k, v] for k, v in sorted(getattr(self, level).items())]
                for level in _TABLE_LEVELS
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HistoricalMedianTable:
        table = cls(min_day_type_samples=int(payload.get("min_day_type_samples", 3)))
        for level, types in _TABLE_LEVELS.items():
            setattr(
                table,
                level,
                {_parse_key(r, types): float(r[len(types)]) for r in payload.get(level, [])},
            )
        return table


def lodo_historical_tables(
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


class Rung2DayTypeModel(RungModel):
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

    def predict_from_features(self, stretch_key: StretchKey, features: CausalFeatures) -> Prediction:
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
