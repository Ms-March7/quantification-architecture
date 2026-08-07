"""Hard research gates and factor redundancy removal."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Mapping, Sequence

import numpy as np

from .evaluation import FactorMetrics, discovery_score
from .generation import FactorCandidate

HARD_MINIMUM_EVALUATION_DATES = 20


@dataclass(frozen=True)
class SelectionGates:
    minimum_evaluation_dates: int = 60
    minimum_coverage: float = 0.70
    minimum_evaluable_date_ratio: float = 0.70
    minimum_fold_evaluable_ratio: float = 0.50
    minimum_mean_ic: float = 0.01
    minimum_icir: float = 0.08
    minimum_positive_fold_ratio: float = 2.0 / 3.0
    minimum_worst_fold_ic: float = -0.01
    maximum_q_value: float = 0.20
    require_positive_stressed_return: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.minimum_evaluation_dates, bool) or not isinstance(self.minimum_evaluation_dates, int) or self.minimum_evaluation_dates < 1:
            raise ValueError("minimum_evaluation_dates must be a positive integer")
        for name in (
            "minimum_coverage", "minimum_evaluable_date_ratio",
            "minimum_fold_evaluable_ratio", "minimum_positive_fold_ratio",
            "maximum_q_value",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")
        for name in ("minimum_mean_ic", "minimum_icir", "minimum_worst_fold_ic"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not isinstance(self.require_positive_stressed_return, bool):
            raise ValueError("require_positive_stressed_return must be boolean")


def gate_failures(metrics: FactorMetrics, gates: SelectionGates) -> list[str]:
    reasons: list[str] = []
    if metrics.n_dates < max(HARD_MINIMUM_EVALUATION_DATES, gates.minimum_evaluation_dates):
        reasons.append("evaluation_dates")
    if metrics.coverage < gates.minimum_coverage:
        reasons.append("coverage")
    if metrics.evaluable_date_ratio < gates.minimum_evaluable_date_ratio:
        reasons.append("evaluable_dates")
    if metrics.minimum_fold_evaluable_ratio < gates.minimum_fold_evaluable_ratio:
        reasons.append("fold_coverage")
    if metrics.mean_ic < gates.minimum_mean_ic:
        reasons.append("mean_ic")
    if metrics.icir < gates.minimum_icir:
        reasons.append("icir")
    if metrics.positive_fold_ratio < gates.minimum_positive_fold_ratio:
        reasons.append("fold_stability")
    if metrics.worst_fold_ic < gates.minimum_worst_fold_ic:
        reasons.append("worst_fold")
    if metrics.q_value > gates.maximum_q_value:
        reasons.append("fdr")
    if gates.require_positive_stressed_return and metrics.stressed_cost_adjusted_top_excess <= 0.0:
        reasons.append("double_cost_stress")
    return reasons


def _tied_ranks(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    starts = np.flatnonzero(np.r_[True, sorted_values[1:] != sorted_values[:-1]])
    ends = np.r_[starts[1:], len(values)]
    ranks = np.empty(len(values), dtype=np.float64)
    ranks[order] = np.repeat((starts + ends - 1) / 2.0, ends - starts)
    return ranks


def factor_similarity(
    left: np.ndarray,
    right: np.ndarray,
    indices: np.ndarray,
    eligible_mask: np.ndarray,
) -> float:
    x = np.asarray(left[indices], dtype=np.float64)
    y = np.asarray(right[indices], dtype=np.float64)
    membership = np.asarray(eligible_mask[indices], dtype=bool)
    if x.shape != y.shape or membership.shape != x.shape:
        raise ValueError("eligible mask and factor values must share a shape")
    ranked_left: list[np.ndarray] = []
    ranked_right: list[np.ndarray] = []
    for date in range(len(indices)):
        common = membership[date] & np.isfinite(x[date]) & np.isfinite(y[date])
        if int(common.sum()) < 3:
            continue
        left_rank = _tied_ranks(x[date, common])
        right_rank = _tied_ranks(y[date, common])
        if np.std(left_rank) <= 1e-12 or np.std(right_rank) <= 1e-12:
            continue
        ranked_left.append(left_rank)
        ranked_right.append(right_rank)
    if sum(len(value) for value in ranked_left) < 20:
        return float("nan")
    flattened_left = np.concatenate(ranked_left)
    flattened_right = np.concatenate(ranked_right)
    return float(abs(np.corrcoef(flattened_left, flattened_right)[0, 1]))


def select_diversified(
    candidates: Sequence[FactorCandidate],
    metrics: Mapping[str, FactorMetrics],
    values: Mapping[str, np.ndarray] | Callable[[str], np.ndarray],
    indices: np.ndarray,
    *,
    gates: SelectionGates,
    maximum_selected: int = 8,
    correlation_cap: float = 0.75,
    rank_scores: Mapping[str, float] | None = None,
    eligible_mask: np.ndarray | None = None,
    minimum_selected: int = 2,
    minimum_joint_coverage: float = 0.70,
) -> tuple[list[FactorCandidate], dict[str, list[str]]]:
    def load(identifier: str) -> np.ndarray:
        return values(identifier) if callable(values) else values[identifier]

    failures = {candidate.candidate_id: gate_failures(metrics[candidate.candidate_id], gates) for candidate in candidates}
    if eligible_mask is None:
        raise ValueError("point-in-time eligible_mask is required for factor redundancy checks")
    if not (0.0 <= correlation_cap < 1.0):
        raise ValueError("correlation_cap must be in [0, 1)")
    if not (1 <= minimum_selected <= maximum_selected):
        raise ValueError("component limits must satisfy 1 <= minimum <= maximum")
    if not (0.0 < minimum_joint_coverage <= 1.0):
        raise ValueError("minimum_joint_coverage must be in (0, 1]")
    eligible = [candidate for candidate in candidates if not failures[candidate.candidate_id]]
    eligible.sort(
        key=lambda candidate: (
            rank_scores[candidate.candidate_id]
            if rank_scores is not None
            else discovery_score(metrics[candidate.candidate_id])
        ),
        reverse=True,
    )
    if not eligible:
        return [], failures

    membership = np.asarray(eligible_mask[indices], dtype=bool)
    denominator = max(1, int(membership.sum()))
    finite_masks = {
        candidate.candidate_id: membership & np.isfinite(load(candidate.candidate_id)[indices])
        for candidate in eligible
    }
    scores = {
        candidate.candidate_id: float(
            rank_scores[candidate.candidate_id]
            if rank_scores is not None
            else discovery_score(metrics[candidate.candidate_id])
        )
        for candidate in eligible
    }
    similarity_cache: dict[tuple[str, str], float] = {}

    def similarity(first: FactorCandidate, second: FactorCandidate) -> float:
        key = tuple(sorted((first.candidate_id, second.candidate_id)))
        if key not in similarity_cache:
            similarity_cache[key] = factor_similarity(
                load(first.candidate_id), load(second.candidate_id), indices, eligible_mask
            )
        return similarity_cache[key]

    def coverage(items: tuple[FactorCandidate, ...]) -> float:
        common = membership.copy()
        for item in items:
            common &= finite_masks[item.candidate_id]
        return float(common.sum() / denominator)

    # Bounded subset search avoids the old greedy false-negative.  Validation
    # gates normally leave a small set; the beam bound prevents pathological
    # all-pass families from creating combinatorial runtime.
    beam: list[tuple[FactorCandidate, ...]] = [()]
    beam_width = 2048
    for candidate in eligible:
        extensions: list[tuple[FactorCandidate, ...]] = []
        for current in beam:
            if len(current) >= maximum_selected:
                continue
            pair_values = [similarity(candidate, existing) for existing in current]
            if any(not np.isfinite(value) or value >= correlation_cap for value in pair_values):
                continue
            proposed = current + (candidate,)
            if coverage(proposed) >= minimum_joint_coverage:
                extensions.append(proposed)
        unique = {tuple(item.candidate_id for item in state): state for state in beam + extensions}
        beam = sorted(
            unique.values(),
            key=lambda state: (
                len(state),
                sum(scores[item.candidate_id] for item in state),
                coverage(state),
            ),
            reverse=True,
        )[:beam_width]

    feasible = [state for state in beam if len(state) >= minimum_selected]
    if not feasible:
        for candidate in eligible:
            failures[candidate.candidate_id] = ["no_feasible_ensemble_subset"]
        return [], failures
    selected_tuple = max(
        feasible,
        key=lambda state: (
            len(state),
            sum(scores[item.candidate_id] for item in state),
            coverage(state),
        ),
    )
    selected_ids = {candidate.candidate_id for candidate in selected_tuple}
    for candidate in eligible:
        if candidate.candidate_id in selected_ids:
            continue
        pair_values = [similarity(candidate, existing) for existing in selected_tuple]
        if any(not np.isfinite(value) for value in pair_values):
            failures[candidate.candidate_id] = ["insufficient_overlap"]
        elif any(value >= correlation_cap for value in pair_values):
            failures[candidate.candidate_id] = ["redundant"]
        else:
            failures[candidate.candidate_id] = ["capacity_or_joint_coverage_not_selected"]
    return list(selected_tuple), failures
