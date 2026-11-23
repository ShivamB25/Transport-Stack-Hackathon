"""Static and runtime guards enforcing whole-day splits without random row splitting."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ps1.evaluate import (
    CALIBRATION_DAYS,
    FIT_DAYS,
    HOLDOUT_DAYS,
    TRAIN_DAYS,
    validate_three_way_partitions,
)


def test_model_path_has_no_random_or_shuffled_split_calls() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "ps1"
    prohibited = {"train_test_split", "shuffle", "random_split"}
    offenders = []
    for name in ("features.py", "model.py", "evaluate.py", "selection.py"):
        tree = ast.parse((source_root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function_name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", "")
                )
                if function_name in prohibited:
                    offenders.append(f"{name}:{node.lineno}:{function_name}")
    assert offenders == []


def test_three_way_whole_day_partitions_are_disjoint_and_ordered() -> None:
    assert FIT_DAYS + CALIBRATION_DAYS == TRAIN_DAYS
    assert not (set(FIT_DAYS) & set(CALIBRATION_DAYS))
    assert not (set(FIT_DAYS) & set(HOLDOUT_DAYS))
    assert not (set(CALIBRATION_DAYS) & set(HOLDOUT_DAYS))
    validate_three_way_partitions(FIT_DAYS, CALIBRATION_DAYS, HOLDOUT_DAYS)

    with pytest.raises(ValueError, match="both fit and calibration"):
        validate_three_way_partitions(("2026-10-05", "2026-10-15"), ("2026-10-15",), HOLDOUT_DAYS)
    with pytest.raises(ValueError, match="both calibration and holdout"):
        validate_three_way_partitions(FIT_DAYS, ("2026-10-22",), HOLDOUT_DAYS)
    with pytest.raises(ValueError, match="strictly after fit"):
        validate_three_way_partitions(("2026-10-05", "2026-10-15"), ("2026-10-14",), HOLDOUT_DAYS)
