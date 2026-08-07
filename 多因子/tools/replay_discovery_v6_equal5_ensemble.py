"""Unique pre-registered Equal-5 multifactor diagnostic on V6 Discovery.

The five source candidates are fixed.  Each component is transformed to a
daily eligible-universe cross-sectional z-score (sample standard deviation),
then all five are averaged with exact 20% weights.  Missing any component makes
that security/date ineligible for the ensemble; there is no subset fallback or
weight renormalization.

No Validation/Holdout feature is loaded, no subset is enumerated, no weight is
fit, and no permutation or outcome-driven selection is performed.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factor_factory.accelerator import accelerator_signature
from factor_factory.cli import _config_from_mapping
from factor_factory.evaluation import evaluate_factor, forward_returns
from factor_factory.expressions import evaluate_expression
from factor_factory.pipeline import FactorFactory
from factor_factory.registry import ResearchRegistry, ResearchRunLock, file_sha256
from tools.replay_discovery_under_v6 import (
    EXPECTED_DISCOVERY_END,
    EXPECTED_DISCOVERY_START,
    EXPECTED_SNAPSHOT_SHA256,
    FROZEN_CANDIDATES,
    REPLAY_RETENTION_K,
    REPLAY_TOP_K,
    DiscoveryReplayError,
    _compact_backtest,
    _read_candidate_rows,
    _replay_config,
    load_discovery_only_panel,
    validate_source_contract,
)


ENSEMBLE_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "name": "V6_DISCOVERY_EQUAL5_CROSS_SECTIONAL_ZSCORE",
    "component_ids": [candidate.candidate_id for candidate in FROZEN_CANDIDATES],
    "component_order_semantic": False,
    "component_transform": "daily_cross_sectional_zscore",
    "zscore_standard_deviation": "sample_ddof_1",
    "eligible_universe_only": True,
    "combination": "arithmetic_mean_all_five",
    "weights": {
        candidate.candidate_id: 1.0 / len(FROZEN_CANDIDATES)
        for candidate in FROZEN_CANDIDATES
    },
    "missing_value_policy": "complete_case_all_five_no_renormalization",
    "subset_enumeration": False,
    "weight_fitting": False,
    "sign_fitting": False,
    "result_based_selection": False,
    "top_k": REPLAY_TOP_K,
    "retention_k": REPLAY_RETENTION_K,
}


def policy_sha256(policy: Mapping[str, Any]) -> str:
    payload = json.dumps(
        policy,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


ENSEMBLE_POLICY_SHA256 = policy_sha256(ENSEMBLE_POLICY)
ENSEMBLE_ID = hashlib.sha256(
    f"equal5-zscore-v1:{ENSEMBLE_POLICY_SHA256}".encode("utf-8")
).hexdigest()[:16]


def cross_sectional_zscore(values: np.ndarray, eligible: np.ndarray) -> np.ndarray:
    """Sample-z-score each date using only finite, eligible observations."""

    array = np.asarray(values)
    mask = np.asarray(eligible, dtype=bool)
    if array.shape != mask.shape or array.ndim != 2:
        raise ValueError("values and eligible must be identical two-dimensional matrices")
    result = np.full(array.shape, np.nan, dtype=np.float32)
    epsilon = 1e-12
    for date in range(len(array)):
        valid = mask[date] & np.isfinite(array[date])
        if int(valid.sum()) < 2:
            continue
        clean = np.asarray(array[date, valid], dtype=np.float64)
        scale = float(np.std(clean, ddof=1))
        if scale <= epsilon:
            result[date, valid] = 0.0
        else:
            result[date, valid] = np.asarray(
                (clean - float(np.mean(clean))) / scale,
                dtype=np.float32,
            )
    return result


def equal_weight_complete_case(
    component_zscores: Sequence[np.ndarray],
    eligible: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Average exactly five components; never renormalize a missing subset."""

    if len(component_zscores) != len(FROZEN_CANDIDATES):
        raise ValueError("Equal-5 diagnostic requires exactly five components")
    mask = np.asarray(eligible, dtype=bool)
    total = np.zeros(mask.shape, dtype=np.float64)
    complete = mask.copy()
    for values in component_zscores:
        array = np.asarray(values)
        if array.shape != mask.shape:
            raise ValueError("component z-score shape differs from eligible mask")
        finite = np.isfinite(array)
        complete &= finite
        total += np.where(finite, array, 0.0)
    ensemble = np.full(mask.shape, np.nan, dtype=np.float32)
    ensemble[complete] = np.asarray(
        total[complete] / float(len(FROZEN_CANDIDATES)),
        dtype=np.float32,
    )
    return ensemble, complete


def _array_sha256(values: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(values)
    return hashlib.sha256(memoryview(contiguous).cast("B")).hexdigest()


def _descriptive_metrics(metrics: Any) -> dict[str, Any]:
    raw = metrics.to_dict()
    raw.pop("p_value", None)
    raw.pop("q_value", None)
    return {
        **raw,
        "statistical_significance_evaluated": False,
        "p_value": None,
        "q_value": None,
        "permutations": 0,
        "interpretation": "DESCRIPTIVE_DISCOVERY_ONLY",
    }


def run_equal5_replay(
    *,
    source_run: str | Path,
    qlib_root: str | Path,
    output_run: str | Path,
) -> dict[str, Any]:
    source = Path(source_run).expanduser().resolve()
    output = Path(output_run).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"source run does not exist: {source}")
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise DiscoveryReplayError("output run must not be inside the immutable source run")
    if output == source:
        raise DiscoveryReplayError("output run must differ from the immutable source run")

    source_manifest = ResearchRegistry(source).verify_completed()
    source_manifest_sha256 = file_sha256(source / "manifest.json")
    source_rows = _read_candidate_rows(source)
    validate_source_contract(source_manifest, source_rows, source_manifest_sha256)
    source_config = _config_from_mapping(
        {"schema_version": 1, **dict(source_manifest["config"])}
    )
    config = _replay_config(source_config)

    with ResearchRunLock(output):
        panel, store, bounded_panel_sha256 = load_discovery_only_panel(
            qlib_root, source_manifest
        )
        runtime_signature = accelerator_signature(config.accelerator)
        component_zscores: list[np.ndarray] = []
        component_evidence: list[dict[str, Any]] = []
        for index, candidate in enumerate(FROZEN_CANDIDATES, start=1):
            raw = evaluate_expression(
                candidate.expression,
                panel.fields,
                {},
                panel.eligible,
                accelerator=config.accelerator,
                gpu_column_chunk=config.gpu_column_chunk,
            )
            zscore = cross_sectional_zscore(raw, panel.eligible)
            component_zscores.append(zscore)
            component_evidence.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "formula": candidate.formula,
                    "weight": 1.0 / len(FROZEN_CANDIDATES),
                    "zscore_discovery_sha256": _array_sha256(zscore),
                    "finite_observations": int(np.isfinite(zscore).sum()),
                }
            )
            print(
                f"Discovery-only z-score component {index}/{len(FROZEN_CANDIDATES)} "
                f"{candidate.candidate_id}",
                flush=True,
            )

        ensemble, complete_case = equal_weight_complete_case(
            component_zscores, panel.eligible
        )
        del component_zscores
        indices = np.arange(len(panel.dates), dtype=np.int64)
        targets = forward_returns(
            panel,
            config.horizon,
            decision_start=0,
            decision_end=len(panel.dates) - 1,
        )
        metrics = evaluate_factor(
            ensemble,
            targets,
            indices,
            minimum_assets=config.minimum_assets,
            quantile_fraction=config.quantile_fraction,
            tail_count=config.evaluation_top_k or None,
            round_trip_cost_bps=config.round_trip_cost_bps,
            fold_count=config.fold_count,
            permutations=0,
            permutation_block_length=config.horizon,
            seed=config.seed,
            accelerator=config.accelerator,
        )
        descriptive = _descriptive_metrics(metrics)
        backtest = FactorFactory._phase_backtest_summary(
            panel, ensemble, indices, config
        )
        compact = _compact_backtest(backtest, config)
        event_passed = bool(backtest["passed"])
        result_classification = (
            "DIAGNOSTIC_ENSEMBLE_EVENT_GATE_PASSED_NO_PROMOTION"
            if event_passed
            else "DIAGNOSTIC_ENSEMBLE_EVENT_GATE_FAILED"
        )
        eligible_count = int(panel.eligible.sum())
        complete_count = int(complete_case.sum())
        candidate_row = {
            "candidate_id": ENSEMBLE_ID,
            "formula": "equal_weight_all5(daily_cs_zscore(each_component))",
            "component_ids": [item.candidate_id for item in FROZEN_CANDIDATES],
            "weights": ENSEMBLE_POLICY["weights"],
            "ensemble_policy_sha256": ENSEMBLE_POLICY_SHA256,
            "ensemble_discovery_sha256": _array_sha256(ensemble),
            "status": "DIAGNOSTIC_ENSEMBLE_EVENT_PASSED"
            if event_passed
            else "DIAGNOSTIC_ENSEMBLE_EVENT_FAILED",
            "diagnostic_only": True,
            "descriptive_metrics": descriptive,
            "event_backtest": compact,
        }
        manifest = {
            "schema_version": 1,
            "status": "DIAGNOSTIC_ONLY",
            "terminal_state": "DIAGNOSTIC_COMPLETED",
            "result_classification": result_classification,
            "diagnostic_only": True,
            "promotion_eligible": False,
            "research_qualified": False,
            "validation_consumed": False,
            "holdout_opened": False,
            "source_run": str(source),
            "source_manifest_sha256": source_manifest_sha256,
            "source_engine": source_manifest.get("engine"),
            "source_protocol_id": source_manifest["config"]["protocol_id"],
            "pre_registration": {
                "diagnostic_sequence": 2,
                "unique_registered_diagnostic": (
                    "EQUAL5_ZSCORE_TOP10_RETENTION30_DISCOVERY_ONLY"
                ),
                "ensemble_policy": ENSEMBLE_POLICY,
                "ensemble_policy_sha256": ENSEMBLE_POLICY_SHA256,
                "ensemble_id": ENSEMBLE_ID,
                "subset_enumeration": False,
                "weight_fitting": False,
                "sign_fitting": False,
                "result_based_selection": False,
                "mining_performed": False,
                "permutations": 0,
                "statistical_significance_claimed": False,
                "validation_selection_performed": False,
                "unchanged_thresholds": {
                    "minimum_phase_pass_ratio": config.minimum_phase_pass_ratio,
                    "minimum_backtest_sharpe": config.minimum_backtest_sharpe,
                    "maximum_backtest_drawdown": config.maximum_backtest_drawdown,
                    "require_positive_backtest_return": config.require_positive_backtest_return,
                    "require_positive_stressed_backtest_return": (
                        config.require_positive_stressed_backtest_return
                    ),
                    "require_no_unverified_terminal_events": (
                        config.require_no_unverified_terminal_events
                    ),
                },
            },
            "config": asdict(config),
            "data_access": {
                "snapshot_sha256": EXPECTED_SNAPSHOT_SHA256,
                "snapshot_date": store.provenance.get("snapshot_date"),
                "qlib_root": str(Path(qlib_root).expanduser().resolve()),
                "discovery_start": EXPECTED_DISCOVERY_START,
                "discovery_end": EXPECTED_DISCOVERY_END,
                "discovery_dates": len(panel.dates),
                "symbols_intersecting_discovery": len(panel.symbols),
                "bounded_panel_sha256": bounded_panel_sha256,
                **store.access_evidence(),
                "validation_content_loaded": False,
                "holdout_content_loaded": False,
            },
            "runtime_signature": runtime_signature,
            "component_evidence": component_evidence,
            "ensemble_evidence": {
                "eligible_observations": eligible_count,
                "complete_case_observations": complete_count,
                "complete_case_ratio": complete_count / max(1, eligible_count),
                "ensemble_discovery_sha256": candidate_row[
                    "ensemble_discovery_sha256"
                ],
                "descriptive_metrics": descriptive,
            },
            "counts": {
                "frozen_components": len(FROZEN_CANDIDATES),
                "ensembles_constructed": 1,
                "event_tested": 1,
                "event_passed": int(event_passed),
                "promoted": 0,
            },
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        ResearchRegistry(output).write_run(
            manifest=manifest,
            candidates=[candidate_row],
            selected=[],
            backtests={
                "scope": "DISCOVERY_ONLY",
                "diagnostic_only": True,
                "validation_consumed": False,
                "holdout_opened": False,
                "ensemble_policy_sha256": ENSEMBLE_POLICY_SHA256,
                "ensemble": backtest,
            },
        )
    return ResearchRegistry(output).verify_completed()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the unique pre-registered V6 Discovery-only Equal-5 z-score "
            "multifactor diagnostic."
        )
    )
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--qlib-root", required=True)
    parser.add_argument("--output-run", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = run_equal5_replay(
        source_run=args.source_run,
        qlib_root=args.qlib_root,
        output_run=args.output_run,
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "result_classification": manifest["result_classification"],
                "counts": manifest["counts"],
                "descriptive_metrics": manifest["ensemble_evidence"][
                    "descriptive_metrics"
                ],
                "validation_consumed": manifest["validation_consumed"],
                "holdout_opened": manifest["holdout_opened"],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
