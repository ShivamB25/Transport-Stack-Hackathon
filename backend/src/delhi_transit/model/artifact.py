"""Selected-rung model artifact persistence and as-of eligibility."""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from delhi_transit.core.config import IST
from delhi_transit.model.features import NUMERIC_FEATURE_NAMES, CausalFeatures
from delhi_transit.model.lgbm import RUNG4_FEATURE_NAMES, Rung4QuantileLightGBMModel
from delhi_transit.model.rungs import (
    BASELINE_RUNGS,
    BaselineModel,
    Prediction,
    Rung2DayTypeModel,
    Rung3LastDelayModel,
    StretchKey,
)

ArtifactModel = BaselineModel | Rung2DayTypeModel | Rung3LastDelayModel | Rung4QuantileLightGBMModel

_TABLE_RUNG_TYPES: dict[str, type[BaselineModel | Rung2DayTypeModel | Rung3LastDelayModel]] = {
    **{rung: BaselineModel for rung in BASELINE_RUNGS},
    "2": Rung2DayTypeModel,
    "3": Rung3LastDelayModel,
}


def artifact_cutoff_metadata(
    *,
    training_days: Sequence[str],
    calibration_days: Sequence[str],
    latest_training_available_at: int | None = None,
    latest_calibration_available_at: int | None = None,
) -> dict[str, Any]:
    """Describe when an artifact's fitting and calibration data are fully available."""
    training = sorted(set(training_days))
    calibration = sorted(set(calibration_days))
    training_cutoff = max(training) if training else None
    calibration_cutoff = max(calibration) if calibration else None
    data_cutoff = max((day for day in (training_cutoff, calibration_cutoff) if day), default=None)
    availability = [
        value
        for value in (latest_training_available_at, latest_calibration_available_at)
        if value is not None
    ]
    if data_cutoff is None:
        eligible_from_ts = 0
        eligible_from_day = None
    else:
        next_day = datetime.fromisoformat(data_cutoff).replace(tzinfo=IST) + timedelta(days=1)
        eligible_from_ts = int(next_day.timestamp())
        if availability:
            eligible_from_ts = max(eligible_from_ts, max(availability) + 1)
        eligible_from_day = datetime.fromtimestamp(eligible_from_ts, IST).date().isoformat()
    return {
        "training_cutoff_day": training_cutoff,
        "calibration_cutoff_day": calibration_cutoff,
        "data_cutoff_day": data_cutoff,
        "latest_training_available_at": latest_training_available_at,
        "latest_calibration_available_at": latest_calibration_available_at,
        "eligible_from_day": eligible_from_day,
        "eligible_from_ts": eligible_from_ts,
    }


def model_artifact_eligible_as_of(metadata: dict[str, Any], as_of_ts: int) -> bool:
    """Return whether persisted metadata explicitly permits use at the replay cutoff."""
    eligible_from_ts = metadata.get("eligible_from_ts")
    if isinstance(eligible_from_ts, bool) or not isinstance(eligible_from_ts, int):
        return False
    return int(as_of_ts) >= eligible_from_ts


@dataclass
class LoadedModelArtifact:
    """Persisted selected-rung model and metadata loaded by the journeys API."""

    rung: str
    model: ArtifactModel
    covered_routes: frozenset[int]
    metadata: dict[str, Any]

    def is_eligible_as_of(self, as_of_ts: int) -> bool:
        return model_artifact_eligible_as_of(self.metadata, as_of_ts)

    def predict_from_features(self, stretch_key: StretchKey, features: CausalFeatures) -> Prediction:
        return self.model.predict_from_features(stretch_key, features)

    def predict_batch_from_features(
        self, items: Sequence[tuple[StretchKey, CausalFeatures]]
    ) -> list[Prediction]:
        return self.model.predict_batch_from_features(items)


def save_model_artifact(
    model: ArtifactModel,
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


def _load_model(rung: str, model_dir: Path) -> ArtifactModel:
    if rung == "4":
        return Rung4QuantileLightGBMModel.load(model_dir)
    model_type = _TABLE_RUNG_TYPES.get(rung)
    if model_type is None:
        raise ValueError(f"unsupported persisted rung: {rung}")
    state_path = model_dir / f"rung_{rung}_state.json"
    # Rungs 0 and 1 have no fitted state, so their state file is optional.
    if model_type is BaselineModel and not state_path.exists():
        return BaselineModel(rung)
    return model_type.from_dict(json.loads(state_path.read_text(encoding="utf-8")))


def load_model_artifact(model_dir: Path) -> LoadedModelArtifact | None:
    """Load the persisted selected-rung model artifact if present."""
    model_dir = Path(model_dir)
    metadata_path = model_dir / "metadata.json"
    if not metadata_path.exists():
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    rung = str(metadata["selected_rung"])
    return LoadedModelArtifact(
        rung=rung,
        model=_load_model(rung, model_dir),
        covered_routes=frozenset(int(r) for r in metadata.get("covered_routes", [])),
        metadata=metadata,
    )
