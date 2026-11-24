"""Runtime guard enforcing disjoint, ordered whole-day splits."""
from __future__ import annotations

import pytest

from delhi_transit.model.evaluate import (
    CALIBRATION_DAYS,
    FIT_DAYS,
    HOLDOUT_DAYS,
    validate_three_way_partitions,
)


def test_three_way_whole_day_partitions_are_disjoint_and_ordered() -> None:
    validate_three_way_partitions(FIT_DAYS, CALIBRATION_DAYS, HOLDOUT_DAYS)

    with pytest.raises(ValueError, match="both fit and calibration"):
        validate_three_way_partitions(("2026-10-05", "2026-10-15"), ("2026-10-15",), HOLDOUT_DAYS)
    with pytest.raises(ValueError, match="both calibration and holdout"):
        validate_three_way_partitions(FIT_DAYS, ("2026-10-22",), HOLDOUT_DAYS)
    with pytest.raises(ValueError, match="strictly after fit"):
        validate_three_way_partitions(("2026-10-05", "2026-10-15"), ("2026-10-14",), HOLDOUT_DAYS)
