"""Baseline rungs behind one small fit/predict interface."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median

from ps1.features import ReplayQuery, StretchError


@dataclass(frozen=True)
class Prediction:
    error_s: float
    used_fallback: bool = False


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
