"""Time-only splits with explicit purge gaps."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TimeSplits:
    discovery: np.ndarray
    validation: np.ndarray
    holdout: np.ndarray
    purge: int


def three_way_time_split(
    n_dates: int,
    *,
    purge: int,
    discovery_fraction: float = 0.60,
    validation_fraction: float = 0.20,
    minimum_segment: int = 20,
) -> TimeSplits:
    if n_dates < minimum_segment * 3 + purge * 2:
        raise ValueError(
            f"need at least {minimum_segment * 3 + purge * 2} dates for three purged segments; found {n_dates}"
        )
    discovery_end = max(minimum_segment, int(n_dates * discovery_fraction))
    validation_start = discovery_end + purge
    validation_end = max(validation_start + minimum_segment, int(n_dates * (discovery_fraction + validation_fraction)))
    holdout_start = validation_end + purge
    if n_dates - holdout_start < minimum_segment:
        shortage = minimum_segment - (n_dates - holdout_start)
        discovery_end -= (shortage + 1) // 2
        validation_start = discovery_end + purge
        validation_end -= shortage // 2
        holdout_start = validation_end + purge
    if min(discovery_end, validation_end - validation_start, n_dates - holdout_start) < minimum_segment:
        raise ValueError("unable to construct sufficiently large purged time segments")
    return TimeSplits(
        discovery=np.arange(0, discovery_end, dtype=np.int64),
        validation=np.arange(validation_start, validation_end, dtype=np.int64),
        holdout=np.arange(holdout_start, n_dates, dtype=np.int64),
        purge=purge,
    )


def blocked_folds(indices: np.ndarray, count: int = 3) -> tuple[np.ndarray, ...]:
    clean = np.asarray(indices, dtype=np.int64)
    return tuple(fold for fold in np.array_split(clean, max(1, count)) if len(fold))
