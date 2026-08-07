"""V7 discovery-frozen, single-hypothesis ensemble selection.

This module deliberately does not enumerate component subsets.  It applies one
pre-registered score order, one greedy low-correlation pass, one fixed component
count, and one equal-weight definition.  The complete decision trace is hashed
so Validation can confirm exactly one frozen ensemble hypothesis.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .ensemble import EnsembleDefinition, build_ensemble, equal_weight_definition
from .generation import FactorCandidate
from .selection import factor_similarity


V7_SELECTION_RULE = "score_ordered_greedy_low_correlation_v1"
V7_WEIGHTING_RULE = "equal_weight_cs_zscore_v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class EnsembleFirstPolicy:
    component_count: int
    candidate_cap: int
    correlation_cap: float
    minimum_joint_coverage: float
    selection_rule: str = V7_SELECTION_RULE
    weighting_rule: str = V7_WEIGHTING_RULE
    maximum_ensemble_trials: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.component_count, bool)
            or not isinstance(self.component_count, int)
            or self.component_count < 2
        ):
            raise ValueError("V7 component_count must be an integer >= 2")
        if (
            isinstance(self.candidate_cap, bool)
            or not isinstance(self.candidate_cap, int)
            or self.candidate_cap < self.component_count
        ):
            raise ValueError("V7 candidate_cap must be >= component_count")
        if (
            isinstance(self.correlation_cap, bool)
            or not isinstance(self.correlation_cap, (int, float))
            or not math.isfinite(self.correlation_cap)
            or not 0.0 <= self.correlation_cap < 1.0
        ):
            raise ValueError("V7 correlation_cap must be finite and in [0, 1)")
        if (
            isinstance(self.minimum_joint_coverage, bool)
            or not isinstance(self.minimum_joint_coverage, (int, float))
            or not math.isfinite(self.minimum_joint_coverage)
            or not 0.0 < self.minimum_joint_coverage <= 1.0
        ):
            raise ValueError("V7 minimum_joint_coverage must be in (0, 1]")
        if self.selection_rule != V7_SELECTION_RULE:
            raise ValueError(f"unsupported V7 selection_rule: {self.selection_rule!r}")
        if self.weighting_rule != V7_WEIGHTING_RULE:
            raise ValueError(f"unsupported V7 weighting_rule: {self.weighting_rule!r}")
        if self.maximum_ensemble_trials != 1:
            raise ValueError("V7 currently permits exactly one preregistered ensemble trial")

    def to_audit_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["subset_enumeration_allowed"] = False
        payload["policy_hash"] = hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()
        return payload


@dataclass(frozen=True)
class EnsembleFirstSelection:
    definition: EnsembleDefinition | None
    selected_component_ids: tuple[str, ...]
    ordered_candidate_ids: tuple[str, ...]
    selection_trace: tuple[dict[str, Any], ...]
    joint_coverage: float
    policy: EnsembleFirstPolicy
    selection_hash: str

    @property
    def ready(self) -> bool:
        return self.definition is not None

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "algorithm": "v7_ensemble_first_single_hypothesis",
            "definition": asdict(self.definition) if self.definition is not None else None,
            "ordered_candidate_ids": list(self.ordered_candidate_ids),
            "selected_component_ids": list(self.selected_component_ids),
            "selection_trace": list(self.selection_trace),
            "joint_coverage": self.joint_coverage,
            "policy": self.policy.to_audit_dict(),
            "defined_ensemble_count": int(self.definition is not None),
            "subset_enumeration_count": 0,
            "validation_hypothesis_count": int(self.definition is not None),
            "selection_hash": self.selection_hash,
        }


def select_single_equal_weight_ensemble(
    candidates: Sequence[FactorCandidate],
    scores: Mapping[str, float],
    values: Mapping[str, np.ndarray] | Callable[[str], np.ndarray],
    indices: np.ndarray,
    eligible_mask: np.ndarray,
    policy: EnsembleFirstPolicy,
) -> EnsembleFirstSelection:
    """Select at most one fixed-size ensemble with no subset search.

    Candidate scores and all correlations are read only on ``indices``.  Ties
    are broken by stable candidate id, so input order and hash-map order cannot
    change the frozen hypothesis.
    """

    def load(identifier: str) -> np.ndarray:
        return values(identifier) if callable(values) else values[identifier]

    identifiers = [candidate.candidate_id for candidate in candidates]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("V7 ensemble candidates must have unique ids")
    mask = np.asarray(eligible_mask, dtype=bool)
    discovery_indices = np.asarray(indices, dtype=np.int64)
    if mask.ndim != 2 or discovery_indices.ndim != 1 or len(discovery_indices) == 0:
        raise ValueError("V7 selection requires a non-empty 1-D index set and 2-D eligible mask")
    if (
        int(discovery_indices.min()) < 0
        or int(discovery_indices.max()) >= mask.shape[0]
        or len(np.unique(discovery_indices)) != len(discovery_indices)
    ):
        raise ValueError("V7 discovery indices must be unique and in bounds")

    clean_scores: dict[str, float] = {}
    for identifier in identifiers:
        raw_score = scores.get(identifier)
        if (
            isinstance(raw_score, bool)
            or not isinstance(raw_score, (int, float))
            or not math.isfinite(raw_score)
        ):
            raise ValueError(f"V7 candidate score must be finite: {identifier}")
        matrix = np.asarray(load(identifier))
        if matrix.shape != mask.shape:
            raise ValueError(f"V7 factor matrix shape mismatch for {identifier}")
        clean_scores[identifier] = float(raw_score)

    ordered = sorted(
        candidates,
        key=lambda candidate: (-clean_scores[candidate.candidate_id], candidate.candidate_id),
    )
    admitted = ordered[: policy.candidate_cap]
    excluded_by_cap = ordered[policy.candidate_cap :]
    membership = mask[discovery_indices]
    denominator = max(1, int(membership.sum()))
    finite_masks = {
        candidate.candidate_id: membership
        & np.isfinite(np.asarray(load(candidate.candidate_id))[discovery_indices])
        for candidate in admitted
    }

    selected: list[FactorCandidate] = []
    common = membership.copy()
    trace: list[dict[str, Any]] = []
    similarity_cache: dict[tuple[str, str], float] = {}

    def similarity(left: FactorCandidate, right: FactorCandidate) -> float:
        key = tuple(sorted((left.candidate_id, right.candidate_id)))
        if key not in similarity_cache:
            similarity_cache[key] = factor_similarity(
                load(left.candidate_id),
                load(right.candidate_id),
                discovery_indices,
                mask,
            )
        return similarity_cache[key]

    for rank, candidate in enumerate(admitted, start=1):
        identifier = candidate.candidate_id
        if len(selected) >= policy.component_count:
            trace.append(
                {
                    "rank": rank,
                    "candidate_id": identifier,
                    "score": clean_scores[identifier],
                    "decision": "REJECTED_TARGET_COUNT_REACHED",
                    "pairwise_absolute_rank_correlations": {},
                    "proposed_joint_coverage": None,
                }
            )
            continue
        pairwise = {
            existing.candidate_id: float(similarity(candidate, existing))
            for existing in selected
        }
        proposed_common = common & finite_masks[identifier]
        proposed_coverage = float(proposed_common.sum() / denominator)
        if any(not math.isfinite(value) for value in pairwise.values()):
            decision = "REJECTED_INSUFFICIENT_CORRELATION_OVERLAP"
        elif any(value >= policy.correlation_cap for value in pairwise.values()):
            decision = "REJECTED_CORRELATION_CAP"
        elif proposed_coverage < policy.minimum_joint_coverage:
            decision = "REJECTED_JOINT_COVERAGE"
        else:
            decision = "SELECTED"
            selected.append(candidate)
            common = proposed_common
        trace.append(
            {
                "rank": rank,
                "candidate_id": identifier,
                "score": clean_scores[identifier],
                "decision": decision,
                "pairwise_absolute_rank_correlations": {
                    key: (value if math.isfinite(value) else None)
                    for key, value in sorted(pairwise.items())
                },
                "proposed_joint_coverage": proposed_coverage,
            }
        )
    for offset, candidate in enumerate(excluded_by_cap, start=len(admitted) + 1):
        trace.append(
            {
                "rank": offset,
                "candidate_id": candidate.candidate_id,
                "score": clean_scores[candidate.candidate_id],
                "decision": "REJECTED_PREREGISTERED_CANDIDATE_CAP",
                "pairwise_absolute_rank_correlations": {},
                "proposed_joint_coverage": None,
            }
        )

    selected_ids = tuple(candidate.candidate_id for candidate in selected)
    definition = (
        equal_weight_definition(selected_ids)
        if len(selected_ids) == policy.component_count
        else None
    )
    joint_coverage = float(common.sum() / denominator) if selected else 0.0
    audit_core = {
        "definition": asdict(definition) if definition is not None else None,
        "ordered_candidate_ids": [candidate.candidate_id for candidate in ordered],
        "selected_component_ids": list(selected_ids),
        "selection_trace": trace,
        "joint_coverage": joint_coverage,
        "policy": policy.to_audit_dict(),
        "subset_enumeration_count": 0,
    }
    selection_hash = hashlib.sha256(_canonical_json(audit_core).encode("utf-8")).hexdigest()
    return EnsembleFirstSelection(
        definition=definition,
        selected_component_ids=selected_ids,
        ordered_candidate_ids=tuple(candidate.candidate_id for candidate in ordered),
        selection_trace=tuple(trace),
        joint_coverage=joint_coverage,
        policy=policy,
        selection_hash=selection_hash,
    )


def build_selected_ensemble(
    selection: EnsembleFirstSelection,
    values: Mapping[str, np.ndarray] | Callable[[str], np.ndarray],
    eligible_mask: np.ndarray,
) -> np.ndarray:
    if selection.definition is None:
        raise ValueError("V7 ensemble selection is incomplete and cannot be built")

    def load(identifier: str) -> np.ndarray:
        return values(identifier) if callable(values) else values[identifier]

    return build_ensemble(
        tuple(load(identifier) for identifier in selection.definition.component_ids),
        selection.definition.weights,
        eligible_mask,
    )
