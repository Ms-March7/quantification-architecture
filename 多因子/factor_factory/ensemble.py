"""Frozen, auditable multi-factor ensemble construction."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class EnsembleDefinition:
    ensemble_id: str
    component_ids: tuple[str, ...]
    weights: tuple[float, ...]
    formula: str


def equal_weight_definition(component_ids: Sequence[str]) -> EnsembleDefinition:
    identifiers = tuple(component_ids)
    if len(identifiers) < 2:
        raise ValueError("a multi-factor ensemble needs at least two components")
    weight = 1.0 / len(identifiers)
    weights = tuple(weight for _ in identifiers)
    formula = "equal_weight_cs_zscore(" + ",".join(identifiers) + ")"
    identifier = "ensemble-" + hashlib.sha256(formula.encode("utf-8")).hexdigest()[:16]
    return EnsembleDefinition(identifier, identifiers, weights, formula)


def build_ensemble(
    matrices: Sequence[np.ndarray],
    weights: Sequence[float],
    eligible: np.ndarray,
) -> np.ndarray:
    """Combine cross-sectionally standardized components without future data."""

    if len(matrices) < 2 or len(matrices) != len(weights):
        raise ValueError("ensemble matrices and weights must have the same length >= 2")
    shape = np.asarray(matrices[0]).shape
    mask = np.asarray(eligible, dtype=bool)
    if mask.shape != shape or any(np.asarray(values).shape != shape for values in matrices):
        raise ValueError("all ensemble inputs must share the eligible-mask shape")
    clean_weights = np.asarray(weights, dtype=np.float64)
    if not np.isfinite(clean_weights).all() or np.any(clean_weights <= 0.0):
        raise ValueError("every ensemble component must have a finite positive weight")
    clean_weights /= clean_weights.sum()

    weighted_sum = np.zeros(shape, dtype=np.float64)
    valid_result = mask.copy()
    sources = tuple(np.asarray(values) for values in matrices)
    for source in sources:
        valid_result &= np.isfinite(source)
    for date in range(shape[0]):
        valid = valid_result[date]
        if int(valid.sum()) < 2:
            valid_result[date] = False
            continue
        row_total = np.zeros(int(valid.sum()), dtype=np.float64)
        row_valid = True
        for source, weight in zip(sources, clean_weights):
            row = source[date, valid].astype(np.float64, copy=False)
            scale = float(np.std(row, ddof=1))
            if scale <= 1e-12:
                row_valid = False
                break
            standardized = (row - float(np.mean(row))) / scale
            row_total += weight * standardized
        if row_valid:
            weighted_sum[date, valid] = row_total
        else:
            valid_result[date] = False
    result = np.full(shape, np.nan, dtype=np.float32)
    result[valid_result] = weighted_sum[valid_result].astype(np.float32)
    return result
