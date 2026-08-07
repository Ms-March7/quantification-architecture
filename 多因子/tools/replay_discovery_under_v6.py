"""Strict Discovery-only replay of the five frozen V6 event candidates.

This is a pre-registered diagnostic, not another factor search.  It accepts one
exact committed source run, reconstructs five exact expression trees, loads no
feature value after the committed Discovery boundary, and changes only the
portfolio breadth policy from 5/15 to 10/30 (top_k/retention_k).

The output is an independent registry-v2 run whose manifest explicitly states
that Validation was not consumed, Holdout was not opened, and promotion is
forbidden regardless of the diagnostic result.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

if __package__ in {None, ""}:
    # Keep both supported entry styles deterministic:
    # ``python -m tools.replay_discovery_under_v6`` and direct script execution.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factor_factory.accelerator import accelerator_signature
from factor_factory.cli import _config_from_mapping
from factor_factory.expressions import Expression, evaluate_expression
from factor_factory.generation import make_candidate
from factor_factory.pipeline import FactorFactory, FactorFactoryConfig
from factor_factory.qlib_store import QlibBinaryStore
from factor_factory.registry import (
    ResearchRegistry,
    ResearchRunLock,
    file_sha256,
)


EXPECTED_SOURCE_MANIFEST_SHA256 = (
    "8f0790bb06342a59b009d504becc66873e11f622e30d7776e43b90e3661a6dd7"
)
EXPECTED_SNAPSHOT_SHA256 = (
    "ac965ce44b0b27163f6b775c04b1a61c95bb79b62c8915e397a7b33feb81f62e"
)
EXPECTED_PROTOCOL_ID = "a-share-factor-v6-discovery-first-20260716"
EXPECTED_ENGINE = "factor_factory_v6_discovery_first_gpu"
EXPECTED_DISCOVERY_START = "2018-01-02"
EXPECTED_DISCOVERY_END = "2024-10-29"
EXPECTED_DISCOVERY_DATES = 1654
BASELINE_TOP_K = 5
BASELINE_RETENTION_K = 15
REPLAY_TOP_K = 10
REPLAY_RETENTION_K = 30


class DiscoveryReplayError(RuntimeError):
    """Base class for a fail-closed replay error."""


class DiscoveryBoundaryError(DiscoveryReplayError):
    """A feature read or panel date crossed the frozen Discovery boundary."""


@dataclass(frozen=True)
class FrozenCandidate:
    candidate_id: str
    formula: str
    expression: Expression


def _field(name: str) -> Expression:
    return Expression.terminal(name)


def _frozen_candidates() -> tuple[FrozenCandidate, ...]:
    high = _field("high")
    low = _field("low")
    close = _field("close")
    volume = _field("volume")

    expressions = (
        Expression(
            "ts_min",
            (
                Expression(
                    "ts_corr",
                    (
                        high,
                        Expression(
                            "ts_mean",
                            (Expression("ts_rank", (low,), window=20),),
                            window=20,
                        ),
                    ),
                    window=60,
                ),
            ),
            window=5,
        ),
        Expression(
            "neg",
            (
                Expression(
                    "ts_corr",
                    (
                        low,
                        Expression(
                            "ts_std",
                            (Expression("ts_std", (close,), window=2),),
                            window=40,
                        ),
                    ),
                    window=60,
                ),
            ),
        ),
        Expression(
            "ts_min",
            (
                Expression(
                    "ts_corr",
                    (
                        Expression(
                            "ts_corr",
                            (high, Expression("ts_mean", (volume,), window=60)),
                            window=3,
                        ),
                        Expression(
                            "ts_mean",
                            (Expression("ts_rank", (low,), window=20),),
                            window=20,
                        ),
                    ),
                    window=60,
                ),
            ),
            window=5,
        ),
        Expression(
            "ts_min",
            (
                Expression(
                    "ts_corr",
                    (
                        high,
                        Expression(
                            "add",
                            (
                                Expression("ts_corr", (volume, close), window=20),
                                Expression("ts_rank", (low,), window=40),
                            ),
                        ),
                    ),
                    window=60,
                ),
            ),
            window=5,
        ),
        Expression(
            "ts_corr",
            (
                close,
                Expression(
                    "neg",
                    (
                        Expression(
                            "ts_rank",
                            (Expression("ts_rank", (volume,), window=20),),
                            window=60,
                        ),
                    ),
                ),
            ),
            window=40,
        ),
    )
    expected = (
        (
            "8dc3c00a5ab4ef92",
            "ts_min(ts_corr($high,ts_mean(ts_rank($low,20),20),60),5)",
        ),
        (
            "6c4e4aeaac7161e5",
            "neg(ts_corr($low,ts_std(ts_std($close,2),40),60))",
        ),
        (
            "c2b190cc632bf902",
            "ts_min(ts_corr(ts_corr($high,ts_mean($volume,60),3),"
            "ts_mean(ts_rank($low,20),20),60),5)",
        ),
        (
            "cb27e427b3c027fb",
            "ts_min(ts_corr($high,add(ts_corr($volume,$close,20),"
            "ts_rank($low,40)),60),5)",
        ),
        (
            "a6a4e4acca49ff0f",
            "ts_corr($close,neg(ts_rank(ts_rank($volume,20),60)),40)",
        ),
    )
    frozen: list[FrozenCandidate] = []
    for expression, (candidate_id, formula) in zip(expressions, expected):
        candidate = make_candidate(expression)
        if candidate.formula != formula or candidate.candidate_id != candidate_id:
            raise AssertionError("frozen expression tree no longer matches its committed identity")
        frozen.append(FrozenCandidate(candidate_id, formula, expression))
    return tuple(frozen)


FROZEN_CANDIDATES = _frozen_candidates()
FROZEN_BY_ID = {candidate.candidate_id: candidate for candidate in FROZEN_CANDIDATES}


class DiscoveryOnlyQlibStore(QlibBinaryStore):
    """Qlib reader with a hard feature-content ceiling.

    ``QlibBinaryStore`` normally fingerprints complete feature files.  That is
    useful for governed research, but it would physically read bytes belonging
    to later periods.  This subclass replaces that fingerprint with a bounded
    metadata identity and guards every feature slice.  The materialized panel
    itself receives a content hash after loading.
    """

    def __init__(self, root: str | Path, *, discovery_end: str) -> None:
        super().__init__(root)
        if discovery_end not in self._calendar_index:
            raise DiscoveryBoundaryError("Discovery end is not an exact Qlib date")
        self.discovery_end = discovery_end
        self.discovery_end_index = self._calendar_index[discovery_end]
        self.feature_slice_requests = 0
        self.minimum_feature_start_index: int | None = None
        self.maximum_feature_end_index: int | None = None

    def _read_feature_slice(
        self,
        symbol: str,
        field: str,
        start_index: int,
        end_index: int,
    ) -> np.ndarray:
        if end_index > self.discovery_end_index:
            attempted = self.calendar[min(end_index, len(self.calendar) - 1)]
            raise DiscoveryBoundaryError(
                f"feature read for {symbol}/{field} crossed Discovery: {attempted}"
            )
        self.feature_slice_requests += 1
        self.minimum_feature_start_index = (
            start_index
            if self.minimum_feature_start_index is None
            else min(self.minimum_feature_start_index, start_index)
        )
        self.maximum_feature_end_index = (
            end_index
            if self.maximum_feature_end_index is None
            else max(self.maximum_feature_end_index, end_index)
        )
        return super()._read_feature_slice(symbol, field, start_index, end_index)

    def _raw_tree_fingerprint(
        self,
        *,
        symbols: Sequence[str],
        fields: Sequence[str],
        universe: str,
    ) -> str:
        """Hash bounded identity metadata without reading later feature bytes."""

        digest = hashlib.sha256(b"qlib-discovery-bounded-identity-v1")
        tokens: Iterable[str] = (
            str(self.provenance.get("sha256", "")),
            self.discovery_end,
            universe,
            *self.calendar[: self.discovery_end_index + 1],
            *sorted(str(item) for item in fields),
            *sorted(str(item) for item in symbols),
        )
        for token in tokens:
            payload = token.encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
        return digest.hexdigest()

    def access_evidence(self) -> dict[str, Any]:
        maximum = self.maximum_feature_end_index
        return {
            "guard": "DiscoveryOnlyQlibStore._read_feature_slice",
            "feature_slice_requests": self.feature_slice_requests,
            "minimum_feature_global_index": self.minimum_feature_start_index,
            "maximum_feature_global_index": maximum,
            "maximum_feature_date": self.calendar[maximum] if maximum is not None else None,
            "forbidden_after_date": self.discovery_end,
            "boundary_enforced": maximum is not None and maximum <= self.discovery_end_index,
            "full_feature_tree_hash_skipped": True,
            "note": (
                "calendar/instrument/provenance metadata may describe later dates; "
                "feature values after Discovery were not requested"
            ),
        }


def _read_candidate_rows(source: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with (source / "candidates.jsonl").open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise DiscoveryReplayError(
                    f"source candidate row {line_number} is not an object"
                )
            rows.append(value)
    return rows


def validate_source_contract(
    manifest: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    source_manifest_sha256: str,
    *,
    expected_manifest_sha256: str = EXPECTED_SOURCE_MANIFEST_SHA256,
) -> None:
    """Reject any source other than the exact pre-registered V6 run."""

    if source_manifest_sha256 != expected_manifest_sha256:
        raise DiscoveryReplayError("source manifest is not the pre-registered V6 commit")
    if manifest.get("engine") != EXPECTED_ENGINE:
        raise DiscoveryReplayError("source engine differs from frozen V6")
    config = manifest.get("config")
    if not isinstance(config, Mapping):
        raise DiscoveryReplayError("source manifest has no config object")
    if (
        config.get("protocol_id") != EXPECTED_PROTOCOL_ID
        or config.get("protocol_version") != 6
        or config.get("discovery_first_selection") is not True
    ):
        raise DiscoveryReplayError("source is not the frozen V6 discovery-first protocol")
    if config.get("top_k") != BASELINE_TOP_K or config.get("retention_k") != BASELINE_RETENTION_K:
        raise DiscoveryReplayError("source V6 portfolio breadth differs from pre-registration")

    splits = manifest.get("splits")
    discovery = splits.get("discovery") if isinstance(splits, Mapping) else None
    if not isinstance(discovery, Mapping) or (
        discovery.get("start") != EXPECTED_DISCOVERY_START
        or discovery.get("end") != EXPECTED_DISCOVERY_END
        or discovery.get("count") != EXPECTED_DISCOVERY_DATES
    ):
        raise DiscoveryBoundaryError("source Discovery interval differs from pre-registration")
    panel = manifest.get("panel")
    metadata = panel.get("metadata") if isinstance(panel, Mapping) else None
    if not isinstance(metadata, Mapping) or metadata.get("snapshot_sha256") != EXPECTED_SNAPSHOT_SHA256:
        raise DiscoveryReplayError("source data snapshot differs from pre-registration")

    selection = manifest.get("selection_protocol")
    if not isinstance(selection, Mapping) or (
        selection.get("discovery_event_backtest_count") != len(FROZEN_CANDIDATES)
        or selection.get("validation_candidate_count") != 0
        or selection.get("prevalidation_component_ids") != []
    ):
        raise DiscoveryReplayError("source selection state is not the frozen five-candidate state")
    ensemble = manifest.get("ensemble")
    if not isinstance(ensemble, Mapping) or ensemble.get("holdout_was_opened") is not False:
        raise DiscoveryReplayError("source Holdout state is not unopened")

    event_rows = {
        str(row.get("candidate_id")): row
        for row in rows
        if isinstance(row.get("discovery_backtest"), Mapping)
    }
    if set(event_rows) != set(FROZEN_BY_ID):
        raise DiscoveryReplayError("committed Discovery event candidates differ from frozen five")
    for identifier, candidate in FROZEN_BY_ID.items():
        row = event_rows[identifier]
        if row.get("formula") != candidate.formula:
            raise DiscoveryReplayError(f"formula mismatch for frozen candidate {identifier}")
    for row in rows:
        if any(
            row.get(name) is not None
            for name in ("validation", "validation_backtest", "holdout", "holdout_backtest")
        ):
            raise DiscoveryReplayError(
                "source contains Validation/Holdout candidate content; replay refuses it"
            )


def _replay_config(source_config: FactorFactoryConfig) -> FactorFactoryConfig:
    if source_config.top_k != BASELINE_TOP_K or source_config.retention_k != BASELINE_RETENTION_K:
        raise DiscoveryReplayError("source portfolio parameters are not the frozen baseline")
    replay = replace(
        source_config,
        top_k=REPLAY_TOP_K,
        retention_k=REPLAY_RETENTION_K,
    )
    before = asdict(source_config)
    after = asdict(replay)
    changed = {key for key in before if before[key] != after[key]}
    if changed != {"top_k", "retention_k"}:
        raise DiscoveryReplayError(f"unexpected replay config changes: {sorted(changed)}")
    if (
        replay.initial_cash != 100_000.0
        or replay.horizon != 5
        or replay.rebalance_phase_count != 5
        or replay.require_no_unverified_terminal_events is not True
    ):
        raise DiscoveryReplayError("frozen capital/cycle/phase/data-quality contract changed")
    return replay


def _panel_sha256(panel: Any) -> str:
    return FactorFactory._panel_cache_fingerprint(panel)


def load_discovery_only_panel(
    qlib_root: str | Path,
    manifest: Mapping[str, Any],
) -> tuple[Any, DiscoveryOnlyQlibStore, str]:
    panel_manifest = manifest["panel"]
    metadata = panel_manifest["metadata"]
    store = DiscoveryOnlyQlibStore(
        qlib_root,
        discovery_end=EXPECTED_DISCOVERY_END,
    )
    if (
        store.provenance.get("sha256") != EXPECTED_SNAPSHOT_SHA256
        or store.provenance.get("hash_verified") is not True
        or metadata.get("snapshot_sha256") != EXPECTED_SNAPSHOT_SHA256
    ):
        raise DiscoveryReplayError("Qlib provenance does not match the frozen snapshot")

    panel = store.load_panel(
        start_date=EXPECTED_DISCOVERY_START,
        end_date=EXPECTED_DISCOVERY_END,
        universe=str(metadata["universe"]),
        exchanges=tuple(metadata["exchanges"]),
        minimum_history_dates=int(metadata["minimum_history_dates"]),
        stock_only=bool(metadata["stock_only"]),
        seal_after_date=None,
    )
    if (
        panel.dates[0] != EXPECTED_DISCOVERY_START
        or panel.dates[-1] != EXPECTED_DISCOVERY_END
        or len(panel.dates) != EXPECTED_DISCOVERY_DATES
        or store.maximum_feature_end_index != store.discovery_end_index
    ):
        raise DiscoveryBoundaryError("materialized panel exceeded or missed Discovery boundary")
    # ``load_panel`` calls an unsealed request "holdout_content_loaded".  In
    # this bounded request there is no Holdout row at all, so replace that
    # request-local label with the global research meaning used by this replay.
    panel.metadata.update(
        {
            "content_loaded_through": EXPECTED_DISCOVERY_END,
            "validation_content_loaded": False,
            "holdout_content_loaded": False,
            "feature_content_scope": "DISCOVERY_ONLY",
            "content_seal_policy": (
                "request ends at committed Discovery boundary; strict reader rejects later feature indices"
            ),
        }
    )
    bounded_panel_sha256 = _panel_sha256(panel)
    return panel, store, bounded_panel_sha256


def _phase_failures(phase: Mapping[str, Any], config: FactorFactoryConfig) -> list[str]:
    failures: list[str] = []
    if config.require_positive_backtest_return and float(phase["total_return"]) <= 0.0:
        failures.append("ordinary_return")
    if float(phase["sharpe"]) < config.minimum_backtest_sharpe:
        failures.append("sharpe")
    if float(phase["max_drawdown"]) < config.maximum_backtest_drawdown:
        failures.append("drawdown")
    return failures


def event_gate_failures(summary: Mapping[str, Any], config: FactorFactoryConfig) -> dict[str, Any]:
    cohort = summary["staggered_cohort"]
    stress = summary["double_cost_stress"]
    activity = summary["market_timing"]
    data_quality = summary["data_quality_audit"]
    aggregate: list[str] = []
    if float(summary["pass_ratio"]) < config.minimum_phase_pass_ratio:
        aggregate.append("phase_pass_ratio")
    if config.require_positive_backtest_return and float(cohort["total_return"]) <= 0.0:
        aggregate.append("cohort_return")
    if float(cohort["sharpe"]) < config.minimum_backtest_sharpe:
        aggregate.append("cohort_sharpe")
    if float(cohort["max_drawdown"]) < config.maximum_backtest_drawdown:
        aggregate.append("cohort_drawdown")
    if not bool(summary["all_phase_drawdowns_pass"]):
        aggregate.append("all_phase_drawdowns")
    if config.require_positive_stressed_backtest_return and float(stress["total_return"]) <= 0.0:
        aggregate.append("double_cost_return")
    if float(stress["max_drawdown"]) < config.maximum_backtest_drawdown:
        aggregate.append("double_cost_drawdown")
    if float(stress["worst_phase_drawdown"]) < config.maximum_backtest_drawdown:
        aggregate.append("double_cost_worst_phase_drawdown")
    if float(stress["phase_pass_ratio"]) < config.minimum_phase_pass_ratio:
        aggregate.append("double_cost_phase_pass_ratio")
    if not bool(activity["activity_passed"]):
        aggregate.append("market_activity")
    if not bool(data_quality["passed"]):
        aggregate.append("data_quality")
    phases = [
        {
            "phase": int(phase["phase"]),
            "failures": _phase_failures(phase, config),
        }
        for phase in summary["phases"]
    ]
    if bool(summary["passed"]) != (not aggregate):
        raise DiscoveryReplayError("reported event pass disagrees with explicit gate audit")
    return {"aggregate": aggregate, "phases": phases}


def _compact_backtest(summary: Mapping[str, Any], config: FactorFactoryConfig) -> dict[str, Any]:
    cohort = summary["staggered_cohort"]
    stress = summary["double_cost_stress"]
    return {
        "passed": bool(summary["passed"]),
        "gate_failures": event_gate_failures(summary, config),
        "selection_score": summary["selection_score"],
        "pass_count": summary["pass_count"],
        "phase_count": summary["phase_count"],
        "pass_ratio": summary["pass_ratio"],
        "cohort": {
            "total_return": cohort["total_return"],
            "annualized_return": cohort["annualized_return"],
            "sharpe": cohort["sharpe"],
            "max_drawdown": cohort["max_drawdown"],
        },
        "double_cost_stress": {
            "total_return": stress["total_return"],
            "max_drawdown": stress["max_drawdown"],
            "worst_phase_drawdown": stress["worst_phase_drawdown"],
            "phase_pass_ratio": stress["phase_pass_ratio"],
        },
        "data_quality_audit": summary["data_quality_audit"],
        "aggregate_turnover": summary["aggregate_turnover"],
        "aggregate_commission": summary["aggregate_commission"],
        "aggregate_stamp_tax": summary["aggregate_stamp_tax"],
        "aggregate_slippage_cost": summary["aggregate_slippage_cost"],
        "phases": [
            {
                "phase": phase["phase"],
                "passed": phase["passed"],
                "total_return": phase["total_return"],
                "sharpe": phase["sharpe"],
                "max_drawdown": phase["max_drawdown"],
                "average_turnover": phase["average_turnover"],
            }
            for phase in summary["phases"]
        ],
    }


def run_replay(
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

    manifest = ResearchRegistry(source).verify_completed()
    source_manifest_sha256 = file_sha256(source / "manifest.json")
    source_rows = _read_candidate_rows(source)
    validate_source_contract(manifest, source_rows, source_manifest_sha256)
    # Registry manifests store the validated config body, while the original
    # config-file transport adds its own schema_version envelope.
    source_config = _config_from_mapping(
        {"schema_version": 1, **dict(manifest["config"])}
    )
    config = _replay_config(source_config)

    with ResearchRunLock(output):
        panel, store, bounded_panel_sha256 = load_discovery_only_panel(qlib_root, manifest)
        indices = np.arange(len(panel.dates), dtype=np.int64)
        runtime_signature = accelerator_signature(config.accelerator)
        backtests: dict[str, dict[str, Any]] = {}
        candidate_rows: list[dict[str, Any]] = []
        memo: dict[str, np.ndarray] = {}
        for index, candidate in enumerate(FROZEN_CANDIDATES, start=1):
            values = evaluate_expression(
                candidate.expression,
                panel.fields,
                memo,
                panel.eligible,
                accelerator=config.accelerator,
                gpu_column_chunk=config.gpu_column_chunk,
            )
            values = np.asarray(values, dtype=np.float32)
            factor_sha256 = hashlib.sha256(
                memoryview(np.ascontiguousarray(values)).cast("B")
            ).hexdigest()
            summary = FactorFactory._phase_backtest_summary(panel, values, indices, config)
            backtests[candidate.candidate_id] = summary
            compact = _compact_backtest(summary, config)
            candidate_rows.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "formula": candidate.formula,
                    "factor_discovery_sha256": factor_sha256,
                    "status": "DIAGNOSTIC_EVENT_PASSED"
                    if summary["passed"]
                    else "DIAGNOSTIC_EVENT_FAILED",
                    "diagnostic_only": True,
                    "event_backtest": compact,
                }
            )
            print(
                f"Discovery-only event replay {index}/{len(FROZEN_CANDIDATES)} "
                f"{candidate.candidate_id} passed={bool(summary['passed'])}",
                flush=True,
            )

        event_passed = sum(bool(value["passed"]) for value in backtests.values())
        result_classification = (
            "DIAGNOSTIC_EVENT_GATE_PASSED_NO_PROMOTION"
            if event_passed
            else "DIAGNOSTIC_NO_EVENT_GATE_PASS"
        )
        replay_manifest = {
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
            "source_engine": manifest.get("engine"),
            "source_protocol_id": manifest["config"]["protocol_id"],
            "pre_registration": {
                "candidate_policy": "EXACT_FROZEN_FIVE",
                "candidate_ids": [item.candidate_id for item in FROZEN_CANDIDATES],
                "mining_performed": False,
                "permutations": 0,
                "validation_selection_performed": False,
                "changed_parameters": {
                    "top_k": {"source": BASELINE_TOP_K, "replay": REPLAY_TOP_K},
                    "retention_k": {
                        "source": BASELINE_RETENTION_K,
                        "replay": REPLAY_RETENTION_K,
                    },
                },
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
                "discovery_start": panel.dates[0],
                "discovery_end": panel.dates[-1],
                "discovery_dates": len(panel.dates),
                "symbols_intersecting_discovery": len(panel.symbols),
                "bounded_panel_sha256": bounded_panel_sha256,
                **store.access_evidence(),
                "validation_content_loaded": False,
                "holdout_content_loaded": False,
            },
            "runtime_signature": runtime_signature,
            "counts": {
                "frozen_candidates": len(FROZEN_CANDIDATES),
                "event_tested": len(backtests),
                "event_passed": event_passed,
                "promoted": 0,
            },
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        ResearchRegistry(output).write_run(
            manifest=replay_manifest,
            candidates=candidate_rows,
            selected=[],
            backtests={
                "scope": "DISCOVERY_ONLY",
                "diagnostic_only": True,
                "validation_consumed": False,
                "holdout_opened": False,
                "components": backtests,
            },
        )
    committed = ResearchRegistry(output).verify_completed()
    return committed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly replay the exact five V6 candidates on Discovery only with "
            "pre-registered top_k=10 and retention_k=30."
        )
    )
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--qlib-root", required=True)
    parser.add_argument("--output-run", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = run_replay(
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
