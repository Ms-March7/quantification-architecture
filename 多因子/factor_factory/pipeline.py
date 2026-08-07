"""End-to-end factor mining, validation, promotion, and artifact pipeline."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import platform
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .accelerator import accelerator_signature
from .backtest import BacktestResult, CostModel, run_a_share_backtest
from .attempts import ResearchAttemptGovernance
from .evaluation import (
    FactorMetrics,
    benjamini_hochberg,
    benjamini_yekutieli,
    discovery_score,
    evaluate_factor,
    forward_returns,
)
from .ensemble import EnsembleDefinition, build_ensemble, equal_weight_definition
from .ensemble_first import (
    V7_SELECTION_RULE,
    EnsembleFirstPolicy,
    EnsembleFirstSelection,
    build_selected_ensemble,
    select_single_equal_weight_ensemble,
)
from .expressions import evaluate_expression, expression_from_dict, expression_to_dict
from .generation import FactorCandidate, FactorGenerator
from .governance import HoldoutGovernance
from .registry import ResearchRegistry, ResearchRunLock, atomic_write_json, file_sha256
from .schema import PanelData
from .selection import SelectionGates, gate_failures, select_diversified
from .validation import TimeSplits, three_way_time_split


CACHE_SCHEMA_VERSION = "factor-expression-v7-content-bound-cache"
ENGINE_MODULES = (
    "accelerator.py", "attempts.py", "backtest.py", "ensemble.py", "evaluation.py", "expressions.py",
    "ensemble_first.py", "generation.py", "governance.py", "pipeline.py", "qlib_store.py",
    "registry.py", "schema.py", "selection.py", "validation.py", "cli.py",
)


@dataclass(frozen=True)
class FactorFactoryConfig:
    protocol_version: int = 1
    protocol_id: str = "UNREGISTERED"
    lineage_manifest_sha256: str = ""
    seed: int = 20260715
    population_size: int = 64
    generations: int = 2
    elite_size: int = 12
    max_depth: int = 3
    horizon: int = 5
    minimum_assets: int = 8
    quantile_fraction: float = 0.20
    evaluation_top_k: int = 0
    round_trip_cost_bps: float = 15.0
    permutations: int = 1999
    fold_count: int = 3
    discovery_fraction: float = 0.60
    validation_fraction: float = 0.20
    discovery_first_selection: bool = False
    ensemble_first_validation: bool = False
    maximum_discovery_event_candidates: int = 64
    frozen_ensemble_component_count: int = 3
    maximum_ensemble_trials: int = 1
    ensemble_selection_rule: str = V7_SELECTION_RULE
    maximum_selected: int = 8
    minimum_ensemble_components: int = 2
    maximum_ensemble_components: int = 5
    minimum_ensemble_coverage: float = 0.70
    correlation_cap: float = 0.75
    rebalance_phase_count: int = 5
    minimum_phase_pass_ratio: float = 0.60
    complexity_penalty: float = 0.0005
    initial_cash: float = 100_000.0
    top_k: int = 10
    retention_k: int = 0
    portfolio_weighting: str = "equal"
    volatility_lookback: int = 20
    minimum_backtest_sharpe: float = 0.0
    maximum_backtest_drawdown: float = -0.25
    require_positive_backtest_return: bool = True
    require_positive_stressed_backtest_return: bool = True
    market_timing_enabled: bool = False
    market_trend_window: int = 60
    market_momentum_window: int = 20
    minimum_market_breadth: float = 0.50
    minimum_market_coverage: float = 0.80
    minimum_risk_on_ratio: float = 0.25
    minimum_risk_on_rebalances_per_phase: int = 20
    minimum_risk_on_rebalance_ratio_per_phase: float = 0.0
    require_no_unverified_terminal_events: bool = False
    accelerator: str = "cpu"
    gpu_column_chunk: int = 256
    selection_gates: SelectionGates = field(default_factory=SelectionGates)
    cost_model: CostModel = field(default_factory=CostModel)

    def __post_init__(self) -> None:
        integer_fields = (
            "protocol_version", "seed", "population_size", "generations", "elite_size", "max_depth",
            "horizon", "minimum_assets", "evaluation_top_k", "permutations", "fold_count",
            "maximum_discovery_event_candidates",
            "frozen_ensemble_component_count", "maximum_ensemble_trials",
            "maximum_selected", "minimum_ensemble_components",
            "maximum_ensemble_components", "rebalance_phase_count", "top_k", "retention_k",
            "volatility_lookback",
            "market_trend_window", "market_momentum_window",
            "minimum_risk_on_rebalances_per_phase", "gpu_column_chunk",
        )
        for name in integer_fields:
            value = getattr(self, name)
            minimum = 0 if name in {
                "generations", "permutations", "evaluation_top_k",
                "minimum_risk_on_rebalances_per_phase", "retention_k",
            } else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if self.horizon < 2:
            raise ValueError("horizon must be at least 2")
        if not isinstance(self.protocol_id, str) or not self.protocol_id.strip():
            raise ValueError("protocol_id must be a non-empty string")
        if not isinstance(self.lineage_manifest_sha256, str):
            raise ValueError("lineage_manifest_sha256 must be a string")
        if self.lineage_manifest_sha256:
            try:
                valid_lineage = (
                    len(self.lineage_manifest_sha256) == 64
                    and int(self.lineage_manifest_sha256, 16) >= 0
                )
            except ValueError:
                valid_lineage = False
            if not valid_lineage:
                raise ValueError("lineage_manifest_sha256 must be 64 hexadecimal characters")
        if self.elite_size > self.population_size:
            raise ValueError("elite_size cannot exceed population_size")
        if not (
            2 <= self.minimum_ensemble_components <= self.maximum_ensemble_components
            and self.maximum_selected >= self.minimum_ensemble_components
        ):
            raise ValueError(
                "ensemble limits require 2 <= minimum <= maximum and maximum_selected >= minimum"
            )
        if self.rebalance_phase_count > self.horizon:
            raise ValueError("rebalance_phase_count cannot exceed horizon")
        if self.retention_k and self.retention_k < self.top_k:
            raise ValueError("retention_k must be zero or greater than or equal to top_k")
        if self.portfolio_weighting not in {"equal", "inverse_volatility"}:
            raise ValueError(
                "portfolio_weighting must be 'equal' or 'inverse_volatility'"
            )
        if self.market_momentum_window > self.market_trend_window:
            raise ValueError("market_momentum_window cannot exceed market_trend_window")
        bounded = {
            "quantile_fraction": (0.0, 0.5),
            "minimum_ensemble_coverage": (0.0, 1.0),
            "correlation_cap": (0.0, 1.0),
            "minimum_phase_pass_ratio": (0.0, 1.0),
            "discovery_fraction": (0.0, 1.0),
            "validation_fraction": (0.0, 1.0),
            "minimum_market_breadth": (0.0, 1.0),
            "minimum_market_coverage": (0.0, 1.0),
            "minimum_risk_on_ratio": (0.0, 1.0),
        }
        for name, (lower, upper) in bounded.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if name == "correlation_cap":
                valid = lower <= value < upper
            else:
                valid = lower < value <= upper
            if not valid:
                raise ValueError(f"{name} is outside its allowed range")
        ratio = self.minimum_risk_on_rebalance_ratio_per_phase
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not np.isfinite(ratio)
            or not 0.0 <= ratio <= 1.0
        ):
            raise ValueError(
                "minimum_risk_on_rebalance_ratio_per_phase must be finite and in [0, 1]"
            )
        for name in (
            "round_trip_cost_bps", "complexity_penalty", "initial_cash",
            "minimum_backtest_sharpe", "maximum_backtest_drawdown",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.round_trip_cost_bps < 0.0 or self.complexity_penalty < 0.0 or self.initial_cash <= 0.0:
            raise ValueError("costs/penalty must be non-negative and initial_cash positive")
        if self.discovery_fraction + self.validation_fraction >= 1.0:
            raise ValueError("discovery_fraction + validation_fraction must be below 1")
        if not -1.0 <= self.maximum_backtest_drawdown <= 0.0:
            raise ValueError("maximum_backtest_drawdown must be in [-1, 0]")
        for name in (
            "require_positive_backtest_return", "require_positive_stressed_backtest_return",
            "require_no_unverified_terminal_events", "discovery_first_selection",
            "ensemble_first_validation",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        if not isinstance(self.market_timing_enabled, bool):
            raise ValueError("market_timing_enabled must be boolean")
        if self.accelerator not in {"cpu", "gpu"}:
            raise ValueError("accelerator must be 'cpu' or 'gpu'")
        if self.ensemble_first_validation:
            if self.protocol_version < 7 or not self.discovery_first_selection:
                raise ValueError(
                    "ensemble_first_validation requires protocol_version >= 7 and discovery_first_selection=true"
                )
            if self.maximum_ensemble_trials != 1:
                raise ValueError("V7 permits exactly one preregistered ensemble trial")
            if not (
                self.minimum_ensemble_components
                <= self.frozen_ensemble_component_count
                <= min(self.maximum_ensemble_components, self.maximum_selected)
            ):
                raise ValueError(
                    "frozen_ensemble_component_count must be within preregistered ensemble limits"
                )
            if self.maximum_discovery_event_candidates < self.frozen_ensemble_component_count:
                raise ValueError(
                    "V7 discovery candidate cap must cover the frozen component count"
                )
            if self.ensemble_selection_rule != V7_SELECTION_RULE:
                raise ValueError(
                    f"unsupported V7 ensemble_selection_rule: {self.ensemble_selection_rule!r}"
                )


@dataclass(frozen=True)
class PipelineResult:
    status: str
    run_directory: str
    generated_count: int
    validation_pass_count: int
    shadow_count: int
    selected_ids: tuple[str, ...]
    best_formula: str | None
    best_holdout_metrics: dict[str, Any] | None
    best_backtest: dict[str, Any] | None


class FactorFactory:
    """Automated research factory with a hard ``SHADOW_ONLY`` ceiling."""

    def __init__(
        self,
        config: FactorFactoryConfig | None = None,
        progress: Callable[[str], None] | None = None,
        governance_path: str | Path | None = None,
        attempt_governance_path: str | Path | None = None,
    ) -> None:
        self.config = config or FactorFactoryConfig()
        self.progress = progress
        self.governance_path = Path(governance_path) if governance_path is not None else None
        self.attempt_governance_path = (
            Path(attempt_governance_path) if attempt_governance_path is not None else None
        )

    def _emit(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def _verified_v3_research_attempt(
        self,
        panel: PanelData,
        output_path: Path,
        splits: TimeSplits,
    ) -> tuple[ResearchAttemptGovernance, dict[str, Any]] | None:
        """Fail before factor materialization unless the V3 attempt is exact."""

        if panel.metadata.get("source") != "qlib_binary_store":
            return None
        if self.config.protocol_version < 3:
            return None
        metadata = panel.metadata.get("research_attempt")
        if not isinstance(metadata, dict):
            raise RuntimeError("V3 Qlib research has no pre-consumption attempt credential")
        required = ("protocol_id", "protocol_hash", "database", "validation_evidence")
        missing = [name for name in required if not metadata.get(name)]
        if missing:
            raise RuntimeError(f"incomplete V3 research attempt credential: {missing}")
        if metadata["protocol_id"] != self.config.protocol_id:
            raise RuntimeError("panel attempt protocol_id differs from config")
        frozen_database = Path(str(metadata["database"]))
        governance_path = self.attempt_governance_path or frozen_database
        if Path(governance_path).resolve() != frozen_database.resolve():
            raise RuntimeError("research attempt database path mismatch")
        governance = ResearchAttemptGovernance(governance_path)
        record = governance.require_state(
            str(metadata["protocol_id"]),
            str(metadata["protocol_hash"]),
            {"VALIDATION_CONSUMED"},
        )
        payload = record.payload or {}
        checks = {
            "protocol_version": self.config.protocol_version,
            "protocol_id": self.config.protocol_id,
            "run_directory": str(output_path.resolve()),
            "config": asdict(self.config),
            "engine_fingerprint": self._engine_fingerprint(),
            "runtime_signature": self._runtime_signature(self.config.accelerator),
            "lineage_manifest_sha256": self.config.lineage_manifest_sha256,
            "splits": self._split_manifest(panel, splits),
        }
        for name, expected in checks.items():
            if payload.get(name) != expected:
                raise RuntimeError(f"V3 attempt immutable field mismatch: {name}")
        identity = payload.get("snapshot_identity")
        if not isinstance(identity, dict):
            raise RuntimeError("V3 attempt has no snapshot identity")
        panel_identity = {
            "store": str(Path(str(panel.metadata.get("store"))).resolve()),
            "start": panel.dates[0],
            "end": panel.dates[-1],
            "calendar_dates": len(panel.dates),
            "symbols": len(panel.symbols),
            "snapshot_sha256": panel.metadata.get("snapshot_sha256"),
            "raw_tree_fingerprint": panel.metadata.get("raw_tree_fingerprint"),
            "universe": panel.metadata.get("universe"),
            "exchanges": panel.metadata.get("exchanges"),
            "fields": sorted(set(panel.fields) | {"change"}),
        }
        for name, expected in panel_identity.items():
            if identity.get(name) != expected:
                raise RuntimeError(f"V3 panel/snapshot identity mismatch: {name}")
        content_seal = payload.get("content_seal")
        if not isinstance(content_seal, dict) or content_seal.get(
            "loaded_through"
        ) != panel.metadata.get("content_loaded_through"):
            raise RuntimeError("V3 panel content seal differs from registered attempt")
        if content_seal.get("holdout_content_start") != self._split_manifest(
            panel, splits
        )["holdout"]["start"]:
            raise RuntimeError("V3 holdout start differs from registered content seal")
        panel_request = payload.get("panel_request")
        expected_request = {
            "start_date": panel.dates[0],
            "end_date": panel.dates[-1],
            "universe": panel.metadata.get("universe"),
            "exchanges": panel.metadata.get("exchanges"),
            "minimum_history_dates": panel.metadata.get("minimum_history_dates"),
        }
        if panel_request != expected_request:
            raise RuntimeError("V3 panel request differs from registered attempt")
        evidence = (record.state_evidence or {}).get("VALIDATION_CONSUMED")
        if evidence != metadata.get("validation_evidence"):
            raise RuntimeError("V3 validation-consumption evidence mismatch")
        return governance, {
            "database": str(governance.path.resolve()),
            "protocol_id": record.protocol_id,
            "protocol_hash": record.protocol_hash,
            "state": record.state,
            "lineage_manifest_sha256": self.config.lineage_manifest_sha256,
            "validation_evidence": evidence,
        }

    def _evaluate(
        self,
        candidate: FactorCandidate,
        values: np.ndarray,
        targets: np.ndarray,
        indices: np.ndarray,
        *,
        permutations: int,
        seed_offset: int,
    ) -> FactorMetrics:
        config = self.config
        return evaluate_factor(
            values,
            targets,
            indices,
            minimum_assets=config.minimum_assets,
            quantile_fraction=config.quantile_fraction,
            tail_count=config.evaluation_top_k or None,
            round_trip_cost_bps=config.round_trip_cost_bps,
            fold_count=config.fold_count,
            permutations=permutations,
            permutation_block_length=config.horizon,
            # Shared resampling seed preserves a common null calendar across
            # the dependent generated candidate family.
            seed=config.seed,
            accelerator=config.accelerator,
        )

    def run(self, panel: PanelData, output_directory: str | Path) -> PipelineResult:
        output_path = Path(output_directory)
        with ResearchRunLock(output_path):
            return self._run_locked(panel, output_path)

    @classmethod
    def _verify_frozen_bundle(
        cls,
        research_directory: str | Path,
        output_directory: str | Path,
        confirm_freeze_hash: str,
    ) -> tuple[Path, Path, dict[str, Any], dict[str, Any], str, FactorFactoryConfig]:
        research_path = Path(research_directory)
        output_path = Path(output_directory)
        if research_path.resolve() == output_path.resolve():
            raise ValueError("holdout output must be separate from the immutable research run")
        research_manifest = ResearchRegistry(research_path).verify_completed()
        if research_manifest.get("effective_promotion_ceiling") == "ENGINEERING_ONLY":
            raise RuntimeError("ENGINEERING_ONLY research can never open production holdout")
        ensemble_manifest = research_manifest.get("ensemble")
        if not isinstance(ensemble_manifest, dict) or not ensemble_manifest.get(
            "awaiting_explicit_holdout_open"
        ):
            raise RuntimeError("research run is not frozen awaiting explicit holdout")
        freeze_path = research_path / "freeze.json"
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
        freeze_hash = str(freeze.pop("freeze_hash", ""))
        canonical = json.dumps(freeze, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if not freeze_hash or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != freeze_hash:
            raise RuntimeError("research freeze hash verification failed")
        if confirm_freeze_hash != freeze_hash:
            raise RuntimeError("explicit freeze hash confirmation does not match")
        engine_source_path = research_path / "engine-source.json"
        engine_source = json.loads(engine_source_path.read_text(encoding="utf-8"))
        source_canonical = json.dumps(
            engine_source, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if hashlib.sha256(source_canonical.encode("utf-8")).hexdigest() != freeze.get(
            "engine_source_sha256"
        ):
            raise RuntimeError("frozen engine source bundle verification failed")
        for name, evidence in engine_source.get("modules", {}).items():
            try:
                source_bytes = base64.b64decode(
                    str(evidence["content_base64"]), validate=True
                )
            except (KeyError, ValueError, binascii.Error) as exc:
                raise RuntimeError(f"frozen engine source encoding failed: {name}") from exc
            if hashlib.sha256(source_bytes).hexdigest() != evidence["sha256"]:
                raise RuntimeError(f"frozen engine source module verification failed: {name}")

        config_values = dict(freeze["config"])
        config_values["selection_gates"] = SelectionGates(**config_values["selection_gates"])
        config_values["cost_model"] = CostModel(**config_values["cost_model"])
        config = FactorFactoryConfig(**config_values)
        return research_path, output_path, research_manifest, freeze, freeze_hash, config

    @classmethod
    def open_frozen_holdout(
        cls,
        panel: PanelData,
        research_directory: str | Path,
        output_directory: str | Path,
        *,
        confirm_freeze_hash: str,
        governance_path: str | Path | None = None,
        attempt_governance_path: str | Path | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> PipelineResult:
        """Open one governed holdout for legacy/in-memory panel callers."""

        (
            research_path,
            output_path,
            research_manifest,
            freeze,
            freeze_hash,
            config,
        ) = cls._verify_frozen_bundle(
            research_directory, output_directory, confirm_freeze_hash
        )
        if config.protocol_version >= 3 and freeze.get("panel_content_scope") == "THROUGH_VALIDATION_ONLY":
            raise RuntimeError(
                "V3 Qlib holdout must use open_frozen_qlib_holdout so governance opens before data loading"
            )
        factory = cls(
            config,
            progress=progress,
            governance_path=governance_path,
            attempt_governance_path=attempt_governance_path,
        )
        with ResearchRunLock(output_path):
            return factory._open_frozen_holdout_locked(
                panel, research_path, output_path, freeze, freeze_hash, research_manifest,
                preopened=None,
            )

    @classmethod
    def open_frozen_qlib_holdout(
        cls,
        qlib_root: str | Path,
        research_directory: str | Path,
        output_directory: str | Path,
        *,
        confirm_freeze_hash: str,
        governance_path: str | Path | None = None,
        attempt_governance_path: str | Path | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> PipelineResult:
        """Open V3 governance before materializing any holdout feature values."""

        from .qlib_store import QlibBinaryStore

        (
            research_path,
            output_path,
            research_manifest,
            freeze,
            freeze_hash,
            config,
        ) = cls._verify_frozen_bundle(
            research_directory, output_directory, confirm_freeze_hash
        )
        if config.protocol_version < 3:
            raise RuntimeError("real Qlib holdout requires a V3 governed freeze")
        if freeze.get("runtime_signature") != cls._runtime_signature(config.accelerator):
            raise RuntimeError("runtime changed after freeze; holdout remains sealed")
        if freeze.get("engine_fingerprint") != cls._engine_fingerprint():
            raise RuntimeError("engine changed after freeze; holdout remains sealed")
        panel_manifest = research_manifest.get("panel")
        if not isinstance(panel_manifest, dict):
            raise RuntimeError("frozen research manifest has no panel definition")
        panel_metadata = panel_manifest.get("metadata")
        fields = panel_manifest.get("fields")
        if (
            not isinstance(panel_metadata, dict)
            or panel_metadata.get("source") != "qlib_binary_store"
            or not isinstance(fields, list)
            or "open" not in fields
            or "close" not in fields
        ):
            raise RuntimeError("frozen V3 run is not a valid Qlib research panel")
        frozen_attempt = freeze.get("research_attempt")
        manifest_attempt = research_manifest.get("research_attempt")
        if not isinstance(frozen_attempt, dict) or frozen_attempt != manifest_attempt:
            raise RuntimeError("freeze and research manifest attempt identities differ")
        protocol_id = str(frozen_attempt.get("protocol_id", ""))
        protocol_hash = str(frozen_attempt.get("protocol_hash", ""))
        frozen_database = str(frozen_attempt.get("database", ""))
        if not protocol_id or len(protocol_hash) != 64 or not frozen_database:
            raise RuntimeError("frozen V3 attempt identity is incomplete")
        attempt_path = Path(attempt_governance_path or frozen_database)
        if attempt_path.resolve() != Path(frozen_database).resolve():
            raise RuntimeError("V3 attempt database override differs from freeze")
        attempt_governance = ResearchAttemptGovernance(attempt_path)
        attempt_record = attempt_governance.require_state(
            protocol_id, protocol_hash, {"FROZEN_AWAITING_HOLDOUT", "HOLDOUT_OPENED"}
        )
        attempt_payload = attempt_record.payload or {}
        immutable_checks = {
            "protocol_version": config.protocol_version,
            "protocol_id": config.protocol_id,
            "run_directory": str(research_path.resolve()),
            "config": asdict(config),
            "engine_fingerprint": cls._engine_fingerprint(),
            "runtime_signature": cls._runtime_signature(config.accelerator),
            "lineage_manifest_sha256": config.lineage_manifest_sha256,
            "splits": freeze.get("splits"),
        }
        for name, expected in immutable_checks.items():
            if attempt_payload.get(name) != expected:
                raise RuntimeError(f"V3 frozen attempt field mismatch: {name}")
        selected = json.loads((research_path / "selected.json").read_text(encoding="utf-8"))
        terminal_evidence = {
            "manifest_sha256": file_sha256(research_path / "manifest.json"),
            "commit_manifest_sha256": json.loads(
                (research_path / "COMMITTED.json").read_text(encoding="utf-8")
            )["manifest_sha256"],
            "result_status": "FROZEN_AWAITING_HOLDOUT",
            "freeze_hash": freeze_hash,
            "selected_count": len(selected),
            "qualified": bool(research_manifest.get("ensemble", {}).get("qualified", False)),
        }
        if (attempt_record.state_evidence or {}).get(
            "FROZEN_AWAITING_HOLDOUT"
        ) != terminal_evidence:
            raise RuntimeError("V3 frozen attempt evidence differs from committed research")

        store = QlibBinaryStore(qlib_root)
        exchanges = tuple(str(value) for value in panel_metadata["exchanges"])
        snapshot_identity = store.research_snapshot_identity(
            start_date=str(panel_manifest["start"]),
            end_date=str(panel_manifest["end"]),
            universe=str(panel_metadata["universe"]),
            fields=tuple(str(value) for value in fields),
            exchanges=exchanges,
            stock_only=bool(panel_metadata["stock_only"]),
        )
        if attempt_payload.get("snapshot_identity") != snapshot_identity:
            raise RuntimeError("current raw Qlib tree differs from registered V3 attempt")
        expected_panel_request = {
            "start_date": str(panel_manifest["start"]),
            "end_date": str(panel_manifest["end"]),
            "universe": str(panel_metadata["universe"]),
            "exchanges": list(exchanges),
            "minimum_history_dates": int(panel_metadata["minimum_history_dates"]),
        }
        if attempt_payload.get("panel_request") != expected_panel_request:
            raise RuntimeError("frozen panel request differs from registered V3 attempt")
        if panel_metadata.get("content_loaded_through") != attempt_payload.get(
            "content_seal", {}
        ).get("loaded_through"):
            raise RuntimeError("research content seal differs from registered V3 attempt")

        holdout = freeze["splits"]["holdout"]
        holdout_key = hashlib.sha256(
            (
                "A_SHARE|" + ",".join(exchanges)
                + "|" + str(holdout["start"])
                + "|" + str(holdout["end"])
                + "|horizon=" + str(config.horizon)
                + "|protocol=" + protocol_hash
            ).encode("utf-8")
        ).hexdigest()
        run_id = hashlib.sha256(
            (holdout_key + "|" + freeze_hash + "|" + str(output_path.resolve())).encode("utf-8")
        ).hexdigest()[:24]
        factory = cls(
            config,
            progress=progress,
            governance_path=governance_path,
            attempt_governance_path=attempt_path,
        )
        holdout_governance = HoldoutGovernance(factory.governance_path)
        reservation_record = {
            "run_id": run_id,
            "holdout_key": holdout_key,
            "protocol_id": protocol_id,
            "protocol_hash": protocol_hash,
            "market": "A_SHARE",
            "horizon": config.horizon,
            "training_fingerprint": str(freeze["training_fingerprint"]),
            "holdout_start": str(holdout["start"]),
            "holdout_end": str(holdout["end"]),
            "family_hash": str(freeze["family_hash"]),
            "freeze_hash": freeze_hash,
            "snapshot_fingerprint": str(snapshot_identity["snapshot_sha256"]),
            "engine_fingerprint": str(freeze["engine_fingerprint"]),
            "run_directory": str(output_path.resolve()),
        }
        opening_evidence = {
            "freeze_hash": freeze_hash,
            "source_research_manifest_sha256": file_sha256(research_path / "manifest.json"),
            "holdout_governance_database": str(holdout_governance.path.resolve()),
            "holdout_run_id": run_id,
            "holdout_key": holdout_key,
            "run_directory": str(output_path.resolve()),
            "snapshot_sha256": snapshot_identity["snapshot_sha256"],
            "raw_tree_fingerprint": snapshot_identity["raw_tree_fingerprint"],
        }
        with ResearchRunLock(output_path):
            reservation = holdout_governance.reserve(reservation_record)
            attempt_governance.transition(
                protocol_id, protocol_hash, "HOLDOUT_OPENED", opening_evidence
            )
            opened = holdout_governance.mark_opened(run_id, freeze_hash)
            if opened.status not in {"OPENED", "RESULT_COMMITTED"}:
                raise RuntimeError(f"unexpected governed holdout state: {opened.status}")
            panel = store.load_panel(
                start_date=str(panel_manifest["start"]),
                end_date=str(panel_manifest["end"]),
                universe=str(panel_metadata["universe"]),
                fields=tuple(str(value) for value in fields),
                exchanges=exchanges,
                minimum_history_dates=int(panel_metadata["minimum_history_dates"]),
                stock_only=bool(panel_metadata["stock_only"]),
                quarantine_adjustment_anomalies=bool(
                    panel_metadata["adjustment_anomaly_quarantine"]
                ),
                anomaly_adjusted_return_threshold=float(
                    panel_metadata["adjustment_anomaly_adjusted_return_threshold"]
                ),
                anomaly_source_change_threshold=float(
                    panel_metadata["adjustment_anomaly_source_change_threshold"]
                ),
                anomaly_disagreement_threshold=float(
                    panel_metadata["adjustment_anomaly_disagreement_threshold"]
                ),
            )
            panel.metadata["holdout_preopen"] = {
                "attempt_database": str(attempt_path.resolve()),
                "protocol_id": protocol_id,
                "protocol_hash": protocol_hash,
                "opening_evidence": opening_evidence,
                "governance_database": str(holdout_governance.path.resolve()),
                "run_id": run_id,
                "holdout_key": holdout_key,
                "reservation_status": reservation.status,
                "opened_status": opened.status,
            }
            return factory._open_frozen_holdout_locked(
                panel,
                research_path,
                output_path,
                freeze,
                freeze_hash,
                research_manifest,
                preopened=panel.metadata["holdout_preopen"],
            )

    def _open_frozen_holdout_locked(
        self,
        panel: PanelData,
        research_path: Path,
        output_path: Path,
        freeze: dict[str, Any],
        freeze_hash: str,
        research_manifest: dict[str, Any],
        preopened: dict[str, Any] | None,
    ) -> PipelineResult:
        config = self.config
        if config.protocol_version >= 3:
            if not isinstance(preopened, dict):
                raise RuntimeError("V3 holdout data cannot be evaluated before governance opens")
            ResearchAttemptGovernance(
                Path(str(preopened["attempt_database"]))
            ).require_state(
                str(preopened["protocol_id"]),
                str(preopened["protocol_hash"]),
                {"HOLDOUT_OPENED"},
            )
            HoldoutGovernance(
                Path(str(preopened["governance_database"]))
            ).require_state(
                str(preopened["run_id"]), freeze_hash, {"OPENED", "RESULT_COMMITTED"}
            )
        frozen_policy = freeze.get("promotion_policy")
        if not isinstance(frozen_policy, dict):
            raise RuntimeError("freeze has no promotion policy")
        source_ceiling = research_manifest.get("effective_promotion_ceiling")
        if source_ceiling != frozen_policy.get("promotion_ceiling"):
            raise RuntimeError("research manifest promotion ceiling differs from freeze")
        if self._promotion_policy(panel) != frozen_policy:
            raise RuntimeError("panel promotion policy changed after freeze")
        effective_ceiling = str(frozen_policy["promotion_ceiling"])
        if effective_ceiling == "ENGINEERING_ONLY":
            raise RuntimeError("ENGINEERING_ONLY panel can never open production holdout")
        if freeze.get("runtime_signature") != self._runtime_signature(config.accelerator):
            raise RuntimeError("Python/NumPy runtime changed after freeze; holdout remains sealed")
        if freeze.get("engine_fingerprint") != self._engine_fingerprint():
            raise RuntimeError("research engine changed after freeze; holdout remains sealed")
        sealed_panel_fingerprint = self._panel_cache_fingerprint(panel)
        content_scope = freeze.get("panel_content_scope")
        if content_scope == "FULL_PANEL_TEST_FIXTURE":
            if freeze.get("sealed_panel_fingerprint") != sealed_panel_fingerprint:
                raise RuntimeError("panel content changed after freeze; holdout remains sealed")
        elif content_scope == "THROUGH_VALIDATION_ONLY":
            if panel.metadata.get("holdout_content_loaded") is not True:
                raise RuntimeError("holdout command requires the full frozen snapshot")
            if freeze.get("snapshot_fingerprint") != panel.metadata.get("snapshot_sha256"):
                raise RuntimeError("snapshot identity changed after freeze; holdout remains sealed")
            if freeze.get("panel_structure_fingerprint") != self._panel_structure_fingerprint(panel):
                raise RuntimeError("panel structure changed after freeze; holdout remains sealed")
        else:
            raise RuntimeError("unsupported frozen panel content scope")
        splits = three_way_time_split(
            len(panel.dates),
            purge=config.horizon + 1,
            discovery_fraction=config.discovery_fraction,
            validation_fraction=config.validation_fraction,
        )
        if freeze.get("splits") != self._split_manifest(panel, splits):
            raise RuntimeError("panel time splits differ from the frozen research run")
        validation_last_decision = int(splits.validation[-1])
        validation_evidence_end = validation_last_decision + config.horizon + 1
        training_fingerprint = self._training_fingerprint(
            panel,
            decision_end=validation_last_decision,
            evidence_end=validation_evidence_end,
        )
        if freeze.get("training_fingerprint") != training_fingerprint:
            raise RuntimeError("training fingerprint differs from the frozen research run")

        components = freeze.get("components")
        if not isinstance(components, list) or not (
            config.minimum_ensemble_components <= len(components) <= config.maximum_ensemble_components
        ):
            raise RuntimeError("invalid frozen ensemble components")
        component_ids: list[str] = []
        component_values: list[np.ndarray] = []
        component_rows: list[dict[str, Any]] = []
        for component in components:
            expression = expression_from_dict(component["expression"])
            formula = expression.canonical()
            candidate_id = hashlib.sha256(formula.encode("utf-8")).hexdigest()[:16]
            if candidate_id != component["candidate_id"] or formula != component["formula"]:
                raise RuntimeError("frozen component identity mismatch")
            values = evaluate_expression(
                expression,
                panel.fields,
                {},
                panel.eligible,
                accelerator=config.accelerator,
                gpu_column_chunk=config.gpu_column_chunk,
            )
            component_ids.append(candidate_id)
            component_values.append(np.asarray(values, dtype=np.float32))
            component_rows.append({
                "candidate_id": candidate_id,
                "formula": formula,
                "status": "FROZEN_ENSEMBLE_COMPONENT",
                "weight": float(component["weight"]),
            })
        frozen_definition = freeze["ensemble"]
        definition = EnsembleDefinition(
            str(frozen_definition["ensemble_id"]),
            tuple(str(value) for value in frozen_definition["component_ids"]),
            tuple(float(value) for value in frozen_definition["weights"]),
            str(frozen_definition["formula"]),
        )
        if tuple(component_ids) != definition.component_ids:
            raise RuntimeError("frozen component order mismatch")
        ensemble_values = build_ensemble(component_values, definition.weights, panel.eligible)

        frozen_attempt = freeze.get("research_attempt")
        if isinstance(frozen_attempt, dict):
            holdout_protocol_id = str(frozen_attempt.get("protocol_id", ""))
            holdout_protocol_hash = str(frozen_attempt.get("protocol_hash", ""))
            if not holdout_protocol_id or len(holdout_protocol_hash) != 64:
                raise RuntimeError("frozen research attempt identity is invalid")
        elif config.protocol_version >= 3:
            raise RuntimeError("V3 freeze has no governed research attempt")
        else:
            # Legacy in-memory test fixtures predate the V3 attempt ledger.
            # They remain isolated by a deterministic synthetic protocol and
            # can never be produced by the real-Qlib CLI.
            holdout_protocol_id = f"LEGACY_FIXTURE:{freeze_hash[:16]}"
            holdout_protocol_hash = hashlib.sha256(
                json.dumps(asdict(config), sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        holdout_key = hashlib.sha256(
            (
                "A_SHARE|" + ",".join(panel.metadata.get("exchanges", ["SH", "SZ"]))
                + "|" + panel.dates[int(splits.holdout[0])]
                + "|" + panel.dates[int(splits.holdout[-1])]
                + "|horizon=" + str(config.horizon)
                + "|protocol=" + holdout_protocol_hash
            ).encode("utf-8")
        ).hexdigest()
        run_id = hashlib.sha256(
            (holdout_key + "|" + freeze_hash + "|" + str(output_path.resolve())).encode("utf-8")
        ).hexdigest()[:24]
        governance = HoldoutGovernance(self.governance_path)
        reservation = governance.reserve({
            "run_id": run_id,
            "holdout_key": holdout_key,
            "protocol_id": holdout_protocol_id,
            "protocol_hash": holdout_protocol_hash,
            "market": "A_SHARE",
            "horizon": config.horizon,
            "training_fingerprint": training_fingerprint,
            "holdout_start": panel.dates[int(splits.holdout[0])],
            "holdout_end": panel.dates[int(splits.holdout[-1])],
            "family_hash": str(freeze["family_hash"]),
            "freeze_hash": freeze_hash,
            "snapshot_fingerprint": str(
                panel.metadata.get("snapshot_sha256") or sealed_panel_fingerprint
            ),
            "engine_fingerprint": str(freeze["engine_fingerprint"]),
            "run_directory": str(output_path.resolve()),
        })
        if preopened is not None:
            if governance.path.resolve() != Path(
                str(preopened["governance_database"])
            ).resolve():
                raise RuntimeError("preopened holdout governance database mismatch")
            if (
                preopened.get("run_id") != run_id
                or preopened.get("holdout_key") != holdout_key
                or reservation.status not in {"OPENED", "RESULT_COMMITTED"}
            ):
                raise RuntimeError("preopened holdout identity/state mismatch")
            attempt_record = ResearchAttemptGovernance(
                Path(str(preopened["attempt_database"]))
            ).require_state(
                holdout_protocol_id, holdout_protocol_hash, {"HOLDOUT_OPENED"}
            )
            if (attempt_record.state_evidence or {}).get(
                "HOLDOUT_OPENED"
            ) != preopened.get("opening_evidence"):
                raise RuntimeError("preopened attempt evidence mismatch")
        holdout_consumption: dict[str, Any] = {
            "governance_database": str(governance.path.resolve()),
            "run_id": run_id,
            "state": reservation.status,
            "recovered": reservation.recovered,
        }
        if reservation.status == "RESULT_COMMITTED":
            payload = reservation.result or {}
            holdout_consumption["restored_from_sealed_result"] = True
        else:
            opened = governance.mark_opened(run_id, freeze_hash)
            if opened.status != "OPENED":
                raise RuntimeError(f"unexpected governed holdout state: {opened.status}")
            holdout_targets = forward_returns(
                panel,
                config.horizon,
                decision_start=int(splits.holdout[0]),
                decision_end=int(splits.holdout[-1]),
            )
            proxy_candidate = FactorCandidate(
                component_ids[0],
                expression_from_dict(components[0]["expression"]),
                generation=0,
                origin="frozen",
            )
            holdout_metrics = benjamini_hochberg([
                self._evaluate(
                    proxy_candidate,
                    ensemble_values,
                    holdout_targets,
                    splits.holdout,
                    permutations=config.permutations,
                    seed_offset=20_000,
                )
            ])[0]
            holdout_failures = gate_failures(holdout_metrics, config.selection_gates)
            holdout_backtest = self._phase_backtest_summary(
                panel, ensemble_values, splits.holdout, config
            )
            if not holdout_backtest["passed"]:
                holdout_failures.append("event_phase_gate")
            holdout_mask = panel.eligible[splits.holdout]
            complete_case_coverage = float(
                np.sum(np.isfinite(ensemble_values[splits.holdout]) & holdout_mask)
                / max(1, int(holdout_mask.sum()))
            )
            if complete_case_coverage < config.minimum_ensemble_coverage:
                holdout_failures.append("ensemble_coverage")
            payload = {
                "schema_version": 1,
                "run_id": run_id,
                "freeze_hash": freeze_hash,
                "metrics": holdout_metrics.to_dict(),
                "failures": holdout_failures,
                "complete_case_coverage": complete_case_coverage,
                "backtest": holdout_backtest,
                "qualified": not holdout_failures,
            }
            committed = governance.commit_result(run_id, freeze_hash, payload)
            holdout_consumption.update({
                "state": committed.status,
                "recovered": reservation.recovered or opened.recovered,
            })

        holdout_result_path = output_path / "holdout-result.json"
        atomic_write_json(holdout_result_path, payload)
        atomic_write_json(output_path / "freeze.json", {**freeze, "freeze_hash": freeze_hash})
        atomic_write_json(
            output_path / "engine-source.json",
            json.loads((research_path / "engine-source.json").read_text(encoding="utf-8")),
        )
        holdout_consumption.update({
            "result_artifact": str(holdout_result_path.resolve()),
            "result_artifact_sha256": file_sha256(holdout_result_path),
        })
        qualified = bool(payload["qualified"])
        holdout_metrics = FactorMetrics(**payload["metrics"])
        holdout_backtest = payload["backtest"]
        selected_rows = []
        if qualified:
            selected_rows.append({
                "candidate_id": definition.ensemble_id,
                "formula": definition.formula,
                "status": effective_ceiling,
                "component_ids": definition.component_ids,
                "weights": definition.weights,
                "holdout": holdout_metrics.to_dict(),
                "holdout_backtest": holdout_backtest,
            })
        registry = ResearchRegistry(output_path)
        registry.write_run(
            manifest={
                "engine": "factor_factory_v2_holdout",
                "safety_ceiling": "SHADOW_ONLY",
                "effective_promotion_ceiling": effective_ceiling,
                "research_attempt": frozen_attempt if isinstance(frozen_attempt, dict) else None,
                "attempt_terminal_state": (
                    "RESULT_COMMITTED" if isinstance(frozen_attempt, dict) else None
                ),
                "source_research_run": str(research_path.resolve()),
                "source_research_manifest_sha256": file_sha256(research_path / "manifest.json"),
                "config": asdict(config),
                "panel": {
                    "start": panel.dates[0], "end": panel.dates[-1],
                    "dates": len(panel.dates), "symbols": len(panel.symbols),
                    "fingerprint": sealed_panel_fingerprint,
                },
                "splits": self._split_manifest(panel, splits),
                "ensemble": {
                    "definition": asdict(definition),
                    "freeze_hash": freeze_hash,
                    "holdout_metrics": holdout_metrics.to_dict(),
                    "holdout_failures": payload["failures"],
                    "holdout_complete_case_coverage": payload["complete_case_coverage"],
                    "holdout_consumption": holdout_consumption,
                    "holdout_was_opened": True,
                    "qualified": qualified,
                },
            },
            candidates=component_rows,
            selected=selected_rows,
            backtests={"holdout_ensemble": holdout_backtest},
        )
        if isinstance(frozen_attempt, dict):
            registry.verify_completed()
            terminal_evidence = {
                "manifest_sha256": file_sha256(output_path / "manifest.json"),
                "source_research_manifest_sha256": file_sha256(
                    research_path / "manifest.json"
                ),
                "freeze_hash": freeze_hash,
                "result_artifact_sha256": file_sha256(holdout_result_path),
                "qualified": qualified,
                "holdout_run_id": run_id,
            }
            ResearchAttemptGovernance(
                self.attempt_governance_path
                or Path(str(frozen_attempt["database"]))
            ).transition(
                holdout_protocol_id,
                holdout_protocol_hash,
                "RESULT_COMMITTED",
                terminal_evidence,
            )
        self._emit(f"governed holdout complete; qualified={int(qualified)}")
        return PipelineResult(
            status=effective_ceiling if qualified else "NO_FACTOR_PROMOTED",
            run_directory=str(output_path.resolve()),
            generated_count=len(component_ids),
            validation_pass_count=len(component_ids),
            shadow_count=1 if qualified and effective_ceiling == "SHADOW_ONLY" else 0,
            selected_ids=(definition.ensemble_id,) if qualified else (),
            best_formula=definition.formula,
            best_holdout_metrics=holdout_metrics.to_dict(),
            best_backtest=holdout_backtest,
        )

    def _run_locked(self, panel: PanelData, output_path: Path) -> PipelineResult:
        config = self.config
        effective_ceiling = str(panel.metadata.get("promotion_ceiling", "SHADOW_ONLY"))
        if effective_ceiling not in {"ENGINEERING_ONLY", "RESEARCH_ONLY", "SHADOW_ONLY"}:
            raise ValueError(f"unsupported panel promotion_ceiling: {effective_ceiling}")
        if not (2 <= config.minimum_ensemble_components <= config.maximum_ensemble_components):
            raise ValueError("ensemble component limits must satisfy 2 <= minimum <= maximum")
        if not (0.0 < config.minimum_phase_pass_ratio <= 1.0):
            raise ValueError("minimum_phase_pass_ratio must be in (0, 1]")
        if not (0.0 < config.minimum_ensemble_coverage <= 1.0):
            raise ValueError("minimum_ensemble_coverage must be in (0, 1]")
        label_lookahead = config.horizon + 1
        splits = three_way_time_split(
            len(panel.dates),
            purge=label_lookahead,
            discovery_fraction=config.discovery_fraction,
            validation_fraction=config.validation_fraction,
        )
        governed_attempt = self._verified_v3_research_attempt(panel, output_path, splits)
        attempt_governance = governed_attempt[0] if governed_attempt is not None else None
        attempt_manifest = governed_attempt[1] if governed_attempt is not None else None
        validation_last_decision = int(splits.validation[-1])
        validation_evidence_end = validation_last_decision + label_lookahead
        training_fingerprint = self._training_fingerprint(
            panel,
            decision_end=validation_last_decision,
            evidence_end=validation_evidence_end,
        )
        evidence_known = self._evidence_known_columns(
            panel,
            decision_end=validation_last_decision,
            evidence_end=validation_evidence_end,
        )
        known_count = int(evidence_known.sum())
        if known_count < config.minimum_assets:
            raise ValueError(
                f"factor mining needs at least {config.minimum_assets} evidence-known instruments; "
                f"found {known_count}"
            )
        self._emit(f"panel ready: {len(panel.dates)} dates x {len(panel.symbols)} instruments")
        training_targets = forward_returns(
            panel,
            config.horizon,
            decision_end=validation_last_decision,
        )
        generator = FactorGenerator(tuple(sorted(panel.fields)), config.seed, config.max_depth)
        candidates = generator.initial_population(config.population_size)
        candidate_index = {candidate.candidate_id: candidate for candidate in candidates}
        discovery_metrics: dict[str, FactorMetrics] = {}
        sealed_panel_fingerprint = self._panel_cache_fingerprint(panel)
        panel_fingerprint = sealed_panel_fingerprint[:16]
        engine_fingerprint = self._engine_fingerprint()
        cache_directory = output_path / "factor_cache" / panel_fingerprint
        cache_directory.mkdir(parents=True, exist_ok=True)

        def cache_path(identifier: str) -> Path:
            return cache_directory / f"{identifier}.npy"

        def cache_sidecar(identifier: str) -> Path:
            return cache_directory / f"{identifier}.json"

        verified_cache: set[str] = set()

        def verify_cache(identifier: str) -> bool:
            path = cache_path(identifier)
            sidecar = cache_sidecar(identifier)
            candidate = candidate_index.get(identifier)
            if candidate is None or not path.is_file() or not sidecar.is_file():
                return False
            try:
                evidence = json.loads(sidecar.read_text(encoding="utf-8"))
                if (
                    evidence.get("candidate_id") != identifier
                    or evidence.get("formula") != candidate.formula
                    or evidence.get("panel_fingerprint") != sealed_panel_fingerprint
                    or evidence.get("engine_fingerprint") != engine_fingerprint
                    or evidence.get("npy_sha256") != file_sha256(path)
                ):
                    return False
                values = np.load(path, mmap_mode="r", allow_pickle=False)
                return values.shape == panel.shape and values.dtype == np.float32
            except (OSError, ValueError, json.JSONDecodeError):
                return False

        def load(identifier: str) -> np.ndarray:
            if identifier not in verified_cache:
                if not verify_cache(identifier):
                    raise ValueError(f"unverified cached factor matrix for {identifier}")
                verified_cache.add(identifier)
            values = np.load(cache_path(identifier), mmap_mode="r", allow_pickle=False)
            if values.shape != panel.shape or values.dtype != np.float32:
                raise ValueError(f"invalid cached factor matrix for {identifier}: {values.shape} {values.dtype}")
            return values

        def materialize(items: list[FactorCandidate]) -> None:
            for position, candidate in enumerate(items, start=1):
                path = cache_path(candidate.candidate_id)
                if verify_cache(candidate.candidate_id):
                    verified_cache.add(candidate.candidate_id)
                    continue
                # The research stage must not even materialize factor values in
                # the sealed holdout.  Causal expressions only need data through
                # the final validation decision date; the independent holdout
                # command reconstructs the frozen expressions on the full panel.
                research_end = validation_last_decision + 1
                research_fields = {
                    name: values[:research_end] for name, values in panel.fields.items()
                }
                research_values = evaluate_expression(
                    candidate.expression,
                    research_fields,
                    {},
                    panel.eligible[:research_end],
                    accelerator=config.accelerator,
                    gpu_column_chunk=config.gpu_column_chunk,
                )
                values = np.full(panel.shape, np.nan, dtype=np.float32)
                values[:research_end] = np.asarray(research_values, dtype=np.float32)
                temporary = path.with_suffix(".tmp.npy")
                np.save(temporary, np.asarray(values, dtype=np.float32), allow_pickle=False)
                os.replace(temporary, path)
                atomic_write_json(cache_sidecar(candidate.candidate_id), {
                    "schema_version": 1,
                    "candidate_id": candidate.candidate_id,
                    "formula": candidate.formula,
                    "panel_fingerprint": sealed_panel_fingerprint,
                    "engine_fingerprint": engine_fingerprint,
                    "npy_sha256": file_sha256(path),
                    "bytes": path.stat().st_size,
                })
                verified_cache.add(candidate.candidate_id)
                if position == 1 or position % 8 == 0 or position == len(items):
                    self._emit(f"materialized {position}/{len(items)} candidates in current batch")

        materialize(candidates)
        for generation in range(1, config.generations + 1):
            self._emit(f"discovery scoring generation {generation - 1}")
            for index, candidate in enumerate(candidates):
                if candidate.candidate_id not in discovery_metrics:
                    discovery_metrics[candidate.candidate_id] = self._evaluate(
                        candidate, load(candidate.candidate_id), training_targets, splits.discovery, permutations=0, seed_offset=index
                    )
                if (index + 1) % 8 == 0 or index + 1 == len(candidates):
                    self._emit(f"discovery {index + 1}/{len(candidates)}")
            elites = sorted(
                candidates,
                key=lambda candidate: (
                    discovery_score(discovery_metrics[candidate.candidate_id])
                    - config.complexity_penalty * candidate.expression.complexity
                ),
                reverse=True,
            )[: config.elite_size]
            existing = {candidate.candidate_id for candidate in candidates}
            children = generator.offspring(
                elites, config.population_size, generation, seen_ids=existing
            )
            candidate_index.update({candidate.candidate_id: candidate for candidate in children})
            materialize(children)
            candidates.extend(children)
            self._emit(f"generation {generation} added {len(children)} candidates; family={len(candidates)}")

        for index, candidate in enumerate(candidates):
            if candidate.candidate_id not in discovery_metrics:
                discovery_metrics[candidate.candidate_id] = self._evaluate(
                    candidate, load(candidate.candidate_id), training_targets, splits.discovery, permutations=0, seed_offset=index
                )

        discovery_component_backtests: dict[str, dict[str, Any]] = {}
        discovery_screen_failures: dict[str, list[str]] = {
            candidate.candidate_id: [] for candidate in candidates
        }
        prevalidation_components: list[FactorCandidate] = []
        validation_candidates = list(candidates)
        v7_selection: EnsembleFirstSelection | None = None
        discovery_ensemble_definition: EnsembleDefinition | None = None
        discovery_ensemble_values: np.ndarray | None = None
        discovery_ensemble_metrics: FactorMetrics | None = None
        discovery_ensemble_backtest: dict[str, Any] | None = None
        discovery_ensemble_failures: list[str] = []
        discovery_freeze_hash: str | None = None
        if config.discovery_first_selection:
            discovery_gates = replace(config.selection_gates, maximum_q_value=1.0)
            for candidate in candidates:
                discovery_screen_failures[candidate.candidate_id] = gate_failures(
                    discovery_metrics[candidate.candidate_id], discovery_gates
                )
            discovery_statistical = [
                candidate for candidate in candidates
                if not discovery_screen_failures[candidate.candidate_id]
            ]
            discovery_statistical.sort(
                key=lambda candidate: (
                    discovery_score(discovery_metrics[candidate.candidate_id])
                    - config.complexity_penalty * candidate.expression.complexity
                ),
                reverse=True,
            )
            if config.ensemble_first_validation:
                discovery_scores = {
                    candidate.candidate_id: (
                        discovery_score(discovery_metrics[candidate.candidate_id])
                        - config.complexity_penalty * candidate.expression.complexity
                    )
                    for candidate in discovery_statistical
                }
                v7_policy = EnsembleFirstPolicy(
                    component_count=config.frozen_ensemble_component_count,
                    candidate_cap=config.maximum_discovery_event_candidates,
                    correlation_cap=config.correlation_cap,
                    minimum_joint_coverage=config.minimum_ensemble_coverage,
                    selection_rule=config.ensemble_selection_rule,
                    maximum_ensemble_trials=config.maximum_ensemble_trials,
                )
                v7_selection = select_single_equal_weight_ensemble(
                    discovery_statistical,
                    discovery_scores,
                    load,
                    splits.discovery,
                    panel.eligible,
                    v7_policy,
                )
                selected_lookup = {
                    candidate.candidate_id: candidate for candidate in discovery_statistical
                }
                proposed_components = [
                    selected_lookup[identifier]
                    for identifier in v7_selection.selected_component_ids
                ]
                for decision in v7_selection.selection_trace:
                    if decision["decision"] == "SELECTED":
                        continue
                    identifier = str(decision["candidate_id"])
                    reason = {
                        "REJECTED_PREREGISTERED_CANDIDATE_CAP": "discovery_selection_capacity",
                        "REJECTED_CORRELATION_CAP": "redundant",
                        "REJECTED_INSUFFICIENT_CORRELATION_OVERLAP": "insufficient_overlap",
                        "REJECTED_JOINT_COVERAGE": "insufficient_joint_coverage",
                        "REJECTED_TARGET_COUNT_REACHED": "not_in_preregistered_frozen_ensemble",
                    }.get(str(decision["decision"]), "v7_selection_rejected")
                    discovery_screen_failures[identifier].append(reason)
                if v7_selection.ready:
                    assert v7_selection.definition is not None
                    discovery_ensemble_definition = v7_selection.definition
                    discovery_ensemble_values = build_selected_ensemble(
                        v7_selection, load, panel.eligible
                    )
                    discovery_ensemble_metrics = self._evaluate(
                        proposed_components[0],
                        discovery_ensemble_values,
                        training_targets,
                        splits.discovery,
                        permutations=0,
                        seed_offset=14_000,
                    )
                    discovery_ensemble_failures = [
                        reason
                        for reason in gate_failures(
                            discovery_ensemble_metrics, discovery_gates
                        )
                        if reason != "fdr"
                    ]
                    if v7_selection.joint_coverage < config.minimum_ensemble_coverage:
                        discovery_ensemble_failures.append("ensemble_coverage")
                    discovery_ensemble_backtest = self._phase_backtest_summary(
                        panel,
                        discovery_ensemble_values,
                        splits.discovery,
                        config,
                    )
                    if not discovery_ensemble_backtest["passed"]:
                        discovery_ensemble_failures.append("event_phase_gate")
                    if not discovery_ensemble_failures:
                        prevalidation_components = proposed_components
                        discovery_freeze_core = {
                            "schema_version": 1,
                            "stage": "DISCOVERY_FROZEN_ENSEMBLE",
                            "protocol_id": config.protocol_id,
                            "protocol_version": config.protocol_version,
                            "discovery_split": self._split_manifest(
                                panel, splits
                            )["discovery"],
                            "selection": v7_selection.to_audit_dict(),
                            "components": [
                                {
                                    "candidate_id": candidate.candidate_id,
                                    "formula": candidate.formula,
                                    "expression": expression_to_dict(
                                        candidate.expression
                                    ),
                                    "weight": discovery_ensemble_definition.weights[
                                        index
                                    ],
                                }
                                for index, candidate in enumerate(
                                    proposed_components
                                )
                            ],
                            "discovery_metrics": discovery_ensemble_metrics.to_dict(),
                            "discovery_backtest": discovery_ensemble_backtest,
                            "validation_hypothesis_count": 1,
                            "validation_component_tests_allowed": False,
                            "validation_reselection_allowed": False,
                            "holdout_was_opened": False,
                        }
                        discovery_freeze_canonical = json.dumps(
                            discovery_freeze_core,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        discovery_freeze_hash = hashlib.sha256(
                            discovery_freeze_canonical.encode("utf-8")
                        ).hexdigest()
                        discovery_freeze_path = output_path / "discovery-freeze.json"
                        atomic_write_json(
                            discovery_freeze_path,
                            {
                                **discovery_freeze_core,
                                "discovery_freeze_hash": discovery_freeze_hash,
                            },
                        )
                        discovery_freeze_readback = json.loads(
                            discovery_freeze_path.read_text(encoding="utf-8")
                        )
                        readback_hash = str(
                            discovery_freeze_readback.pop(
                                "discovery_freeze_hash", ""
                            )
                        )
                        if readback_hash != discovery_freeze_hash or hashlib.sha256(
                            json.dumps(
                                discovery_freeze_readback,
                                ensure_ascii=False,
                                allow_nan=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest() != discovery_freeze_hash:
                            raise RuntimeError(
                                "V7 discovery ensemble freeze verification failed"
                            )
                    else:
                        for candidate in proposed_components:
                            discovery_screen_failures[candidate.candidate_id].append(
                                "discovery_ensemble_not_qualified"
                            )
                else:
                    discovery_ensemble_failures = [
                        "insufficient_fixed_count_components"
                    ]
                validation_candidates = list(prevalidation_components)
                self._emit(
                    "V7 discovery ensemble freeze: "
                    f"statistical={len(discovery_statistical)}; "
                    f"selected={len(v7_selection.selected_component_ids)}; "
                    f"frozen={int(bool(prevalidation_components))}; "
                    "validation_hypotheses="
                    f"{int(bool(prevalidation_components))}"
                )
            else:
                discovery_event_input = discovery_statistical[
                    : config.maximum_discovery_event_candidates
                ]
                for candidate in discovery_statistical[config.maximum_discovery_event_candidates :]:
                    discovery_screen_failures[candidate.candidate_id].append(
                        "discovery_event_capacity"
                    )
                discovery_event_candidates: list[FactorCandidate] = []
                discovery_event_scores: dict[str, float] = {}
                for index, candidate in enumerate(discovery_event_input, start=1):
                    summary = self._phase_backtest_summary(
                        panel, load(candidate.candidate_id), splits.discovery, config
                    )
                    discovery_component_backtests[candidate.candidate_id] = summary
                    if summary["passed"]:
                        discovery_event_candidates.append(candidate)
                        discovery_event_scores[candidate.candidate_id] = (
                            float(summary["selection_score"])
                            - config.complexity_penalty * candidate.expression.complexity
                        )
                    else:
                        discovery_screen_failures[candidate.candidate_id].append(
                            "discovery_event_phase_gate"
                        )
                    if index % 4 == 0 or index == len(discovery_event_input):
                        self._emit(
                            f"discovery event backtests {index}/{len(discovery_event_input)}"
                        )
                maximum_components = min(
                    config.maximum_selected, config.maximum_ensemble_components
                )
                prevalidation_components, discovery_component_failures = select_diversified(
                    discovery_event_candidates,
                    discovery_metrics,
                    load,
                    splits.discovery,
                    gates=discovery_gates,
                    maximum_selected=maximum_components,
                    correlation_cap=config.correlation_cap,
                    rank_scores=discovery_event_scores,
                    eligible_mask=panel.eligible,
                    minimum_selected=config.minimum_ensemble_components,
                    minimum_joint_coverage=config.minimum_ensemble_coverage,
                )
                for identifier, reasons in discovery_component_failures.items():
                    for reason in reasons:
                        if reason not in discovery_screen_failures[identifier]:
                            discovery_screen_failures[identifier].append(reason)
                validation_candidates = list(prevalidation_components)
                self._emit(
                    "discovery-first freeze: "
                    f"statistical={len(discovery_statistical)}; "
                    f"event_passed={len(discovery_event_candidates)}; "
                    f"validation_candidates={len(validation_candidates)}"
                )

        validation_failures = (
            {identifier: list(reasons) for identifier, reasons in discovery_screen_failures.items()}
            if config.discovery_first_selection
            else {candidate.candidate_id: [] for candidate in candidates}
        )
        validation_metrics: dict[str, FactorMetrics] = {}
        statistical_candidates: list[FactorCandidate] = []
        validation_component_backtests: dict[str, dict[str, Any]] = {}
        event_candidates: list[FactorCandidate] = []
        validation_selected: list[FactorCandidate] = []
        ensemble_definition: EnsembleDefinition | None = None
        ensemble_values: np.ndarray | None = None
        validation_ensemble_metrics: FactorMetrics | None = None
        validation_ensemble_failures: list[str] = []
        validation_ensemble_backtest: dict[str, Any] | None = None
        validation_ensemble_coverage: float | None = None
        validation_component_test_count = 0
        validation_hypothesis_count = 0
        validation_statistical_pass_count = 0
        if config.ensemble_first_validation:
            self._emit(
                "V7 validation confirms one frozen ensemble; component tests=0"
            )
            if (
                prevalidation_components
                and discovery_ensemble_definition is not None
                and discovery_ensemble_values is not None
                and not discovery_ensemble_failures
            ):
                validation_selected = list(prevalidation_components)
                ensemble_definition = discovery_ensemble_definition
                ensemble_values = discovery_ensemble_values
                validation_hypothesis_count = 1
                raw_ensemble_validation = self._evaluate(
                    validation_selected[0],
                    ensemble_values,
                    training_targets,
                    splits.validation,
                    permutations=config.permutations,
                    seed_offset=15_000,
                )
                # There is exactly one preregistered Validation hypothesis, so
                # BY reduces to q=p and no component-level family is tested.
                validation_ensemble_metrics = benjamini_yekutieli(
                    [raw_ensemble_validation]
                )[0]
                validation_ensemble_failures = gate_failures(
                    validation_ensemble_metrics, config.selection_gates
                )
                validation_statistical_pass_count = int(
                    not validation_ensemble_failures
                )
                validation_mask = panel.eligible[splits.validation]
                validation_ensemble_coverage = float(
                    np.sum(
                        np.isfinite(ensemble_values[splits.validation])
                        & validation_mask
                    )
                    / max(1, int(validation_mask.sum()))
                )
                if validation_ensemble_coverage < config.minimum_ensemble_coverage:
                    validation_ensemble_failures.append("ensemble_coverage")
                validation_ensemble_backtest = self._phase_backtest_summary(
                    panel, ensemble_values, splits.validation, config
                )
                if not validation_ensemble_backtest["passed"]:
                    validation_ensemble_failures.append("event_phase_gate")
                ensemble_path = cache_path(ensemble_definition.ensemble_id)
                temporary = ensemble_path.with_suffix(".tmp.npy")
                np.save(
                    temporary,
                    ensemble_values.astype(np.float32, copy=False),
                    allow_pickle=False,
                )
                os.replace(temporary, ensemble_path)
            else:
                validation_ensemble_failures = ["no_discovery_frozen_ensemble"]
        else:
            self._emit(
                f"validation scoring {len(validation_candidates)} frozen candidate tests"
            )
            raw_validation: list[FactorMetrics] = []
            for index, candidate in enumerate(validation_candidates):
                raw_validation.append(self._evaluate(
                    candidate,
                    load(candidate.candidate_id),
                    training_targets,
                    splits.validation,
                    permutations=config.permutations,
                    seed_offset=10_000 + index,
                ))
                if (index + 1) % 8 == 0 or index + 1 == len(validation_candidates):
                    self._emit(f"validation {index + 1}/{len(validation_candidates)}")
            validation_component_test_count = len(raw_validation)
            adjusted_validation = benjamini_yekutieli(raw_validation)
            validation_metrics = {
                candidate.candidate_id: metrics
                for candidate, metrics in zip(validation_candidates, adjusted_validation)
            }
            for candidate in validation_candidates:
                validation_failures[candidate.candidate_id] = gate_failures(
                    validation_metrics[candidate.candidate_id], config.selection_gates
                )
            statistical_candidates = [
                candidate for candidate in validation_candidates
                if not validation_failures[candidate.candidate_id]
            ]
            validation_statistical_pass_count = len(statistical_candidates)
            self._emit(
                f"validation statistical gates passed={len(statistical_candidates)}"
            )
            event_rank_scores: dict[str, float] = {}
            for index, candidate in enumerate(statistical_candidates, start=1):
                summary = self._phase_backtest_summary(
                    panel, load(candidate.candidate_id), splits.validation, config
                )
                validation_component_backtests[candidate.candidate_id] = summary
                if summary["passed"]:
                    event_candidates.append(candidate)
                    event_rank_scores[candidate.candidate_id] = (
                        float(summary["selection_score"])
                        - config.complexity_penalty * candidate.expression.complexity
                    )
                else:
                    validation_failures[candidate.candidate_id].append(
                        "event_phase_gate"
                    )
                if index % 4 == 0 or index == len(statistical_candidates):
                    self._emit(
                        f"validation event backtests {index}/{len(statistical_candidates)}"
                    )
            maximum_components = min(
                config.maximum_selected, config.maximum_ensemble_components
            )
            if config.discovery_first_selection:
                confirmed_ids = {candidate.candidate_id for candidate in event_candidates}
                frozen_ids = {
                    candidate.candidate_id for candidate in prevalidation_components
                }
                validation_selected = (
                    list(prevalidation_components)
                    if frozen_ids and confirmed_ids == frozen_ids
                    else []
                )
                component_failures: dict[str, list[str]] = {}
                if frozen_ids and confirmed_ids != frozen_ids:
                    for candidate in prevalidation_components:
                        if candidate.candidate_id not in confirmed_ids:
                            validation_failures[candidate.candidate_id].append(
                                "frozen_component_not_confirmed"
                            )
            else:
                validation_selected, component_failures = select_diversified(
                    event_candidates,
                    validation_metrics,
                    load,
                    splits.validation,
                    gates=config.selection_gates,
                    maximum_selected=maximum_components,
                    correlation_cap=config.correlation_cap,
                    rank_scores=event_rank_scores,
                    eligible_mask=panel.eligible,
                    minimum_selected=config.minimum_ensemble_components,
                    minimum_joint_coverage=config.minimum_ensemble_coverage,
                )
            for identifier, reasons in component_failures.items():
                if reasons:
                    validation_failures[identifier].extend(
                        reason
                        for reason in reasons
                        if reason not in validation_failures[identifier]
                    )
            self._emit(
                f"validation event gates passed={len(event_candidates)}; "
                f"diversified components={len(validation_selected)}"
            )
            if len(validation_selected) >= config.minimum_ensemble_components:
                ensemble_definition = equal_weight_definition(
                    tuple(candidate.candidate_id for candidate in validation_selected)
                )
                ensemble_values = build_ensemble(
                    tuple(load(candidate.candidate_id) for candidate in validation_selected),
                    ensemble_definition.weights,
                    panel.eligible,
                )
                ensemble_path = cache_path(ensemble_definition.ensemble_id)
                temporary = ensemble_path.with_suffix(".tmp.npy")
                np.save(
                    temporary,
                    ensemble_values.astype(np.float32, copy=False),
                    allow_pickle=False,
                )
                os.replace(temporary, ensemble_path)
                raw_ensemble_validation = self._evaluate(
                    validation_selected[0],
                    ensemble_values,
                    training_targets,
                    splits.validation,
                    permutations=0,
                    seed_offset=15_000,
                )
                validation_ensemble_metrics = raw_ensemble_validation
                validation_mask = panel.eligible[splits.validation]
                validation_ensemble_coverage = float(
                    np.sum(
                        np.isfinite(ensemble_values[splits.validation])
                        & validation_mask
                    )
                    / max(1, int(validation_mask.sum()))
                )
                validation_ensemble_failures = [
                    reason
                    for reason in gate_failures(
                        raw_ensemble_validation, config.selection_gates
                    )
                    if reason != "fdr"
                ]
                if validation_ensemble_coverage < config.minimum_ensemble_coverage:
                    validation_ensemble_failures.append("ensemble_coverage")
                validation_ensemble_backtest = self._phase_backtest_summary(
                    panel, ensemble_values, splits.validation, config
                )
                if not validation_ensemble_backtest["passed"]:
                    validation_ensemble_failures.append("event_phase_gate")
            else:
                validation_ensemble_failures = ["insufficient_ensemble_components"]

        frozen_ensemble = (
            ensemble_definition is not None
            and ensemble_values is not None
            and not validation_ensemble_failures
        )
        holdout_metrics: FactorMetrics | None = None
        holdout_failures: list[str] = []
        holdout_ensemble_backtest: dict[str, Any] | None = None
        freeze_hash: str | None = None
        holdout_consumption: dict[str, Any] | None = None
        holdout_was_opened = False
        qualified = False
        if frozen_ensemble:
            family_hash = hashlib.sha256(
                "|".join(sorted(candidate.candidate_id for candidate in candidates)).encode("utf-8")
            ).hexdigest()
            qlib_research_panel = panel.metadata.get("source") == "qlib_binary_store"
            if qlib_research_panel and effective_ceiling == "RESEARCH_ONLY":
                if panel.metadata.get("holdout_content_loaded") is not False:
                    raise RuntimeError(
                        "Qlib research must be loaded with a validation-boundary content seal"
                    )
                snapshot_sha256 = str(panel.metadata.get("snapshot_sha256") or "")
                raw_tree_fingerprint = str(panel.metadata.get("raw_tree_fingerprint") or "")
                try:
                    valid_snapshot_hash = len(snapshot_sha256) == 64 and int(snapshot_sha256, 16) >= 0
                    valid_tree_hash = len(raw_tree_fingerprint) == 64 and int(raw_tree_fingerprint, 16) >= 0
                except ValueError:
                    valid_snapshot_hash = valid_tree_hash = False
                if not valid_snapshot_hash:
                    raise RuntimeError("sealed Qlib research requires a valid archive SHA-256")
                if not valid_tree_hash:
                    raise RuntimeError("sealed Qlib research requires a valid raw-tree fingerprint")
            panel_content_scope = (
                "THROUGH_VALIDATION_ONLY" if qlib_research_panel else "FULL_PANEL_TEST_FIXTURE"
            )
            engine_source = self._engine_source_payload()
            engine_source_canonical = json.dumps(
                engine_source, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            freeze_core = {
                "schema_version": 1,
                "engine": (
                    "factor_factory_v7_ensemble_first_gpu"
                    if config.ensemble_first_validation and config.accelerator == "gpu"
                    else "factor_factory_v7_ensemble_first_cpu"
                    if config.ensemble_first_validation
                    else "factor_factory_v6_discovery_first_gpu"
                    if config.accelerator == "gpu" and config.protocol_version >= 6
                    else "factor_factory_v5_gpu_batched_multifactor"
                    if config.accelerator == "gpu" and config.protocol_version >= 5
                    else "factor_factory_v4_gpu_multifactor"
                    if config.accelerator == "gpu"
                    else "factor_factory_v2_multifactor"
                ),
                "training_fingerprint": training_fingerprint,
                "family_hash": family_hash,
                "sealed_panel_fingerprint": sealed_panel_fingerprint,
                "panel_structure_fingerprint": self._panel_structure_fingerprint(panel),
                "panel_content_scope": panel_content_scope,
                "snapshot_fingerprint": panel.metadata.get("snapshot_sha256"),
                "promotion_policy": self._promotion_policy(panel),
                "engine_fingerprint": engine_fingerprint,
                "engine_source_sha256": hashlib.sha256(
                    engine_source_canonical.encode("utf-8")
                ).hexdigest(),
                "runtime_signature": self._runtime_signature(config.accelerator),
                "components": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "formula": candidate.formula,
                        "weight": ensemble_definition.weights[index],
                        "expression": expression_to_dict(candidate.expression),
                    }
                    for index, candidate in enumerate(validation_selected)
                ],
                "ensemble": asdict(ensemble_definition),
                "ensemble_first_selection": (
                    v7_selection.to_audit_dict()
                    if v7_selection is not None else None
                ),
                "discovery_freeze_hash": discovery_freeze_hash,
                "config": asdict(config),
                "splits": self._split_manifest(panel, splits),
                "research_attempt": attempt_manifest,
            }
            canonical = json.dumps(freeze_core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            freeze_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            freeze_path = output_path / "freeze.json"
            atomic_write_json(output_path / "engine-source.json", engine_source)
            atomic_write_json(freeze_path, {**freeze_core, "freeze_hash": freeze_hash})
            frozen_readback = json.loads(freeze_path.read_text(encoding="utf-8"))
            readback_hash = str(frozen_readback.pop("freeze_hash", ""))
            readback_canonical = json.dumps(
                frozen_readback, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if readback_hash != freeze_hash or hashlib.sha256(readback_canonical.encode("utf-8")).hexdigest() != freeze_hash:
                raise RuntimeError("immutable freeze artifact verification failed")

            if effective_ceiling == "ENGINEERING_ONLY":
                holdout_failures = ["engineering_only_holdout_forbidden"]
            else:
                holdout_failures = ["awaiting_explicit_holdout_open"]
        self._emit(
            f"research complete; frozen={int(frozen_ensemble)}; holdout_opened=0; "
            f"effective_ceiling={effective_ceiling}"
        )

        component_ids = {candidate.candidate_id for candidate in validation_selected}
        event_ids = {candidate.candidate_id for candidate in event_candidates}
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.candidate_id in component_ids:
                status = "ENSEMBLE_COMPONENT"
            elif candidate.candidate_id in event_ids:
                status = "RESEARCH_CANDIDATE"
            else:
                status = "REJECTED"
            rows.append({
                "candidate_id": candidate.candidate_id,
                "formula": candidate.formula,
                "generation": candidate.generation,
                "parents": candidate.parents,
                "origin": candidate.origin,
                "status": status,
                "complexity": candidate.expression.complexity,
                "discovery": discovery_metrics[candidate.candidate_id].to_dict(),
                "validation": (
                    validation_metrics[candidate.candidate_id].to_dict()
                    if candidate.candidate_id in validation_metrics else None
                ),
                "holdout": None,
                "discovery_backtest": discovery_component_backtests.get(
                    candidate.candidate_id
                ),
                "validation_failures": validation_failures.get(candidate.candidate_id, []),
                "holdout_failures": [],
                "validation_backtest": validation_component_backtests.get(candidate.candidate_id),
                "holdout_backtest": None,
            })

        registry = ResearchRegistry(output_path)
        selected_rows: list[dict[str, Any]] = []
        if frozen_ensemble and ensemble_definition is not None and validation_ensemble_metrics is not None:
            selected_rows.append({
                "candidate_id": ensemble_definition.ensemble_id,
                "formula": ensemble_definition.formula,
                "status": (
                    "FROZEN_ENGINEERING_ONLY"
                    if effective_ceiling == "ENGINEERING_ONLY"
                    else "FROZEN_AWAITING_HOLDOUT"
                ),
                "component_ids": ensemble_definition.component_ids,
                "weights": ensemble_definition.weights,
                "validation": validation_ensemble_metrics.to_dict(),
                "holdout": None,
                "validation_backtest": validation_ensemble_backtest,
                "holdout_backtest": None,
            })
        registry.write_run(
            manifest={
                "engine": (
                    "factor_factory_v7_ensemble_first_gpu"
                    if config.ensemble_first_validation and config.accelerator == "gpu"
                    else "factor_factory_v7_ensemble_first_cpu"
                    if config.ensemble_first_validation
                    else "factor_factory_v6_discovery_first_gpu"
                    if config.accelerator == "gpu" and config.protocol_version >= 6
                    else "factor_factory_v5_gpu_batched_multifactor"
                    if config.accelerator == "gpu" and config.protocol_version >= 5
                    else "factor_factory_v4_gpu_multifactor"
                    if config.accelerator == "gpu"
                    else "factor_factory_v2_multifactor"
                ),
                "safety_ceiling": "SHADOW_ONLY",
                "effective_promotion_ceiling": effective_ceiling,
                "research_attempt": attempt_manifest,
                "attempt_terminal_state": (
                    "FROZEN_AWAITING_HOLDOUT"
                    if frozen_ensemble and effective_ceiling != "ENGINEERING_ONLY"
                    else "NO_FACTOR"
                ) if attempt_manifest is not None else None,
                "config": asdict(config),
                "panel": {
                    "start": panel.dates[0],
                    "end": panel.dates[-1],
                    "dates": len(panel.dates),
                    "symbols": len(panel.symbols),
                    "fields": sorted(panel.fields),
                    "metadata": panel.metadata,
                    "fingerprint": panel_fingerprint,
                    "training_fingerprint_through_validation": training_fingerprint,
                },
                "splits": self._split_manifest(panel, splits),
                "selection_protocol": {
                    "discovery_first_selection": config.discovery_first_selection,
                    "ensemble_first_validation": config.ensemble_first_validation,
                    "ensemble_selection_rule": config.ensemble_selection_rule,
                    "maximum_ensemble_trials": config.maximum_ensemble_trials,
                    "frozen_ensemble_component_count": (
                        config.frozen_ensemble_component_count
                    ),
                    "maximum_discovery_event_candidates": (
                        config.maximum_discovery_event_candidates
                    ),
                    "discovery_event_backtest_count": len(discovery_component_backtests),
                    "prevalidation_component_ids": [
                        candidate.candidate_id for candidate in prevalidation_components
                    ],
                    "validation_candidate_count": validation_component_test_count,
                    "validation_component_test_count": validation_component_test_count,
                    "validation_hypothesis_count": validation_hypothesis_count,
                    "validation_target": (
                        "ONE_DISCOVERY_FROZEN_ENSEMBLE"
                        if config.ensemble_first_validation
                        else "COMPONENT_FAMILY_THEN_ENSEMBLE"
                    ),
                    "validation_component_event_gates_required": (
                        not config.ensemble_first_validation
                    ),
                    "validation_reselection_allowed": not config.discovery_first_selection,
                    "discovery_freeze_hash": discovery_freeze_hash,
                    "ensemble_first_selection": (
                        v7_selection.to_audit_dict()
                        if v7_selection is not None else None
                    ),
                },
                "ensemble": {
                    "definition": asdict(ensemble_definition) if ensemble_definition is not None else None,
                    "discovery_metrics": (
                        discovery_ensemble_metrics.to_dict()
                        if discovery_ensemble_metrics is not None else None
                    ),
                    "discovery_backtest": discovery_ensemble_backtest,
                    "discovery_failures": discovery_ensemble_failures,
                    "validation_metrics": validation_ensemble_metrics.to_dict()
                    if validation_ensemble_metrics is not None else None,
                    "validation_complete_case_coverage": validation_ensemble_coverage,
                    "validation_failures": validation_ensemble_failures,
                    "holdout_metrics": holdout_metrics.to_dict() if holdout_metrics is not None else None,
                    "holdout_failures": holdout_failures,
                    "freeze_hash": freeze_hash,
                    "holdout_consumption": holdout_consumption,
                    "holdout_was_opened": holdout_was_opened,
                    "qualified": qualified,
                    "awaiting_explicit_holdout_open": bool(
                        frozen_ensemble and effective_ceiling != "ENGINEERING_ONLY"
                    ),
                    "validation_p_value_policy": (
                        "single preregistered frozen ensemble; BY with m=1 gives q=p; no component validation tests"
                        if config.ensemble_first_validation
                        else "BY FDR confirms discovery-frozen components; no validation reselection"
                        if config.discovery_first_selection
                        else "not used because components and ensemble were selected on validation"
                    ),
                },
            },
            candidates=rows,
            selected=selected_rows,
            backtests={
                "discovery_components": discovery_component_backtests,
                "discovery_ensemble": discovery_ensemble_backtest,
                "validation_components": validation_component_backtests,
                "validation_ensemble": validation_ensemble_backtest,
                "holdout_ensemble": holdout_ensemble_backtest,
            },
        )

        if attempt_governance is not None and attempt_manifest is not None:
            terminal_state = (
                "FROZEN_AWAITING_HOLDOUT"
                if frozen_ensemble and effective_ceiling != "ENGINEERING_ONLY"
                else "NO_FACTOR"
            )
            committed_manifest = registry.verify_completed()
            terminal_evidence = {
                "manifest_sha256": file_sha256(output_path / "manifest.json"),
                "commit_manifest_sha256": json.loads(
                    (output_path / "COMMITTED.json").read_text(encoding="utf-8")
                )["manifest_sha256"],
                "result_status": terminal_state,
                "freeze_hash": freeze_hash,
                "selected_count": len(selected_rows),
                "qualified": bool(
                    committed_manifest.get("ensemble", {}).get("qualified", False)
                ),
            }
            transitioned = attempt_governance.transition(
                str(attempt_manifest["protocol_id"]),
                str(attempt_manifest["protocol_hash"]),
                terminal_state,
                terminal_evidence,
            )
            attempt_manifest["state"] = transitioned.state

        return PipelineResult(
            status=(
                "FROZEN_ENGINEERING_ONLY"
                if frozen_ensemble and effective_ceiling == "ENGINEERING_ONLY"
                else "FROZEN_AWAITING_HOLDOUT"
                if frozen_ensemble
                else "NO_FACTOR_PROMOTED"
            ),
            run_directory=str(output_path.resolve()),
            generated_count=len(candidates),
            validation_pass_count=validation_statistical_pass_count,
            shadow_count=1 if qualified and effective_ceiling == "SHADOW_ONLY" else 0,
            selected_ids=(ensemble_definition.ensemble_id,) if frozen_ensemble and ensemble_definition is not None else (),
            best_formula=ensemble_definition.formula if frozen_ensemble and ensemble_definition is not None else None,
            best_holdout_metrics=None,
            best_backtest=None,
        )

    @staticmethod
    def _backtest_passes(result: BacktestResult, config: FactorFactoryConfig) -> bool:
        if config.require_positive_backtest_return and result.total_return <= 0.0:
            return False
        if result.sharpe < config.minimum_backtest_sharpe:
            return False
        if result.max_drawdown < config.maximum_backtest_drawdown:
            return False
        return True

    @classmethod
    def _market_risk_on(
        cls, panel: PanelData, config: FactorFactoryConfig
    ) -> tuple[np.ndarray, dict[str, Any]]:
        """Causal broad-market trend/breadth gate observed at each close."""

        if not config.market_timing_enabled:
            return np.ones(len(panel.dates), dtype=bool), {
                "enabled": False,
                "signal_time": "close(t)",
                "execution_time": "open(t+1)",
            }
        closes = np.asarray(panel.fields["close"], dtype=np.float64)
        opens = np.asarray(panel.fields["open"], dtype=np.float64)
        finite_close = np.isfinite(closes) & (closes > 0.0)
        finite_open = np.isfinite(opens) & (opens > 0.0)
        market_return = np.zeros(len(panel.dates), dtype=np.float64)
        overnight_return = np.full(len(panel.dates), np.nan, dtype=np.float64)
        intraday_return = np.full(len(panel.dates), np.nan, dtype=np.float64)
        mean_market_return = np.full(len(panel.dates), np.nan, dtype=np.float64)
        daily_breadth = np.full(len(panel.dates), np.nan, dtype=np.float64)
        cross_section_valid = np.zeros(len(panel.dates), dtype=bool)
        eligible_previous_count = np.zeros(len(panel.dates), dtype=np.int64)
        valid_return_count = np.zeros(len(panel.dates), dtype=np.int64)
        coverage = np.zeros(len(panel.dates), dtype=np.float64)
        for date_index in range(1, len(panel.dates)):
            # Membership is frozen at t-1.  An anomaly or missing close at t
            # cannot retroactively remove a large t return from the proxy.
            base = panel.eligible[date_index - 1] & finite_close[date_index - 1]
            common = base & finite_close[date_index]
            eligible_previous_count[date_index] = int(base.sum())
            valid_return_count[date_index] = int(common.sum())
            if eligible_previous_count[date_index] > 0:
                coverage[date_index] = (
                    valid_return_count[date_index] / eligible_previous_count[date_index]
                )
            if (
                valid_return_count[date_index] < config.minimum_assets
                or coverage[date_index] < config.minimum_market_coverage
            ):
                continue
            with np.errstate(divide="ignore", invalid="ignore"):
                returns = closes[date_index, common] / closes[date_index - 1, common] - 1.0
            finite = returns[np.isfinite(returns)]
            if len(finite) >= config.minimum_assets:
                market_return[date_index] = float(np.clip(np.median(finite), -0.20, 0.20))
                mean_market_return[date_index] = float(np.mean(finite))
                daily_breadth[date_index] = float(np.mean(finite > 0.0))
                cross_section_valid[date_index] = True
            execution_common = common & finite_open[date_index]
            if int(execution_common.sum()) >= config.minimum_assets:
                with np.errstate(divide="ignore", invalid="ignore"):
                    overnight = (
                        opens[date_index, execution_common]
                        / closes[date_index - 1, execution_common]
                        - 1.0
                    )
                    intraday = (
                        closes[date_index, execution_common]
                        / opens[date_index, execution_common]
                        - 1.0
                    )
                overnight = overnight[np.isfinite(overnight)]
                intraday = intraday[np.isfinite(intraday)]
                if len(overnight) >= config.minimum_assets and len(intraday) >= config.minimum_assets:
                    overnight_return[date_index] = float(
                        np.clip(np.median(overnight), -0.20, 0.20)
                    )
                    intraday_return[date_index] = float(
                        np.clip(np.median(intraday), -0.20, 0.20)
                    )
        market_index = np.cumprod(1.0 + market_return)

        trend_window = config.market_trend_window
        momentum_window = config.market_momentum_window
        breadth_window = momentum_window
        rolling_breadth = np.full(len(panel.dates), np.nan, dtype=np.float64)
        trend_gap = np.full(len(panel.dates), np.nan, dtype=np.float64)
        momentum = np.full(len(panel.dates), np.nan, dtype=np.float64)
        risk_on = np.zeros(len(panel.dates), dtype=bool)
        for date_index in range(trend_window - 1, len(panel.dates)):
            trend_start = date_index - trend_window + 1
            breadth_start = date_index - breadth_window + 1
            if (
                breadth_start < 1
                or not np.all(cross_section_valid[trend_start:date_index + 1])
            ):
                continue
            trend_average = float(np.mean(market_index[trend_start:date_index + 1]))
            trend_gap[date_index] = market_index[date_index] / max(trend_average, 1e-12) - 1.0
            momentum[date_index] = (
                market_index[date_index]
                / max(market_index[date_index - momentum_window], 1e-12)
                - 1.0
            )
            rolling_breadth[date_index] = float(
                np.mean(daily_breadth[breadth_start:date_index + 1])
            )
            risk_on[date_index] = (
                trend_gap[date_index] > 0.0
                and (
                    momentum[date_index] > 0.0
                    or rolling_breadth[date_index] > config.minimum_market_breadth
                )
            )
        daily = [
            {
                "date": panel.dates[index],
                "eligible_previous": int(eligible_previous_count[index]),
                "valid_returns": int(valid_return_count[index]),
                "coverage": float(coverage[index]),
                "median_return": float(market_return[index]),
                "median_overnight_return": float(overnight_return[index])
                if np.isfinite(overnight_return[index]) else None,
                "median_intraday_return": float(intraday_return[index])
                if np.isfinite(intraday_return[index]) else None,
                "mean_return": float(mean_market_return[index])
                if np.isfinite(mean_market_return[index]) else None,
                "market_index": float(market_index[index]),
                "trend_gap": float(trend_gap[index]) if np.isfinite(trend_gap[index]) else None,
                "momentum": float(momentum[index]) if np.isfinite(momentum[index]) else None,
                "breadth": float(rolling_breadth[index])
                if np.isfinite(rolling_breadth[index]) else None,
                "cross_section_valid": bool(cross_section_valid[index]),
                "risk_on": bool(risk_on[index]),
            }
            for index in range(len(panel.dates))
        ]
        return risk_on, {
            "enabled": True,
            "signal_time": "close(t)",
            "execution_time": "open(t+1)",
            "market_proxy": "cumulative cross-sectional median close return",
            "trend_window": trend_window,
            "momentum_window": momentum_window,
            "breadth_window": breadth_window,
            "minimum_breadth": config.minimum_market_breadth,
            "minimum_coverage": config.minimum_market_coverage,
            "minimum_assets": config.minimum_assets,
            "rule": "trend60 AND (momentum20 OR breadth20); equality is risk-off",
            "_daily": daily,
            "_market_return": market_return,
            "_overnight_return": overnight_return,
            "_intraday_return": intraday_return,
        }

    @classmethod
    def _phase_backtest_summary(
        cls,
        panel: PanelData,
        values: np.ndarray,
        indices: np.ndarray,
        config: FactorFactoryConfig,
    ) -> dict[str, Any]:
        index_array = np.asarray(indices, dtype=np.int64)
        if (
            index_array.ndim != 1
            or len(index_array) < 2
            or np.any(np.diff(index_array) != 1)
        ):
            raise ValueError("phase backtest indices must be one contiguous increasing interval")
        phase_count = min(config.rebalance_phase_count, config.horizon, len(index_array) - 1)
        if phase_count < 1:
            raise ValueError("rebalance phase protocol needs at least one phase")
        risk_on, market_timing = cls._market_risk_on(panel, config)
        timed_values = np.asarray(values, dtype=np.float32).copy()
        timed_values[~risk_on] = np.nan
        tested_risk_on = risk_on[index_array]
        daily_diagnostics = market_timing.pop("_daily", [])
        market_timing.pop("_market_return", None)
        overnight_returns = np.asarray(
            market_timing.pop("_overnight_return", np.full(len(panel.dates), np.nan)),
            dtype=np.float64,
        )
        intraday_returns = np.asarray(
            market_timing.pop("_intraday_return", np.full(len(panel.dates), np.nan)),
            dtype=np.float64,
        )
        scheduled_rebalances_per_phase = [
            int(sum(
                1
                for date in range(int(index_array[0]) + 1, int(index_array[-1]) + 1)
                if (date - 1) % config.horizon == phase
            ))
            for phase in range(phase_count)
        ]
        risk_on_rebalances_per_phase = [
            int(sum(
                bool(risk_on[date - 1])
                for date in range(int(index_array[0]) + 1, int(index_array[-1]) + 1)
                if (date - 1) % config.horizon == phase
            ))
            for phase in range(phase_count)
        ]
        risk_on_rebalance_ratios_per_phase = [
            count / max(1, scheduled)
            for count, scheduled in zip(
                risk_on_rebalances_per_phase, scheduled_rebalances_per_phase
            )
        ]
        risk_on_ratio = float(np.mean(tested_risk_on))
        signal_activity_passed = (
            not config.market_timing_enabled
            or (
                risk_on_ratio >= config.minimum_risk_on_ratio
                and min(risk_on_rebalances_per_phase, default=0)
                >= config.minimum_risk_on_rebalances_per_phase
                and min(risk_on_rebalance_ratios_per_phase, default=0.0)
                >= config.minimum_risk_on_rebalance_ratio_per_phase
            )
        )
        proxy_growth = 1.0
        for date in index_array:
            current = int(date)
            if current <= 0:
                continue
            state_at_previous_close = bool(risk_on[current - 1])
            state_before_previous_close = bool(risk_on[current - 2]) if current > 1 else False
            overnight = overnight_returns[current]
            intraday = intraday_returns[current]
            if state_before_previous_close and np.isfinite(overnight):
                proxy_growth *= 1.0 + float(overnight)
            if state_at_previous_close and np.isfinite(intraday):
                proxy_growth *= 1.0 + float(intraday)
        market_timing = {
            **market_timing,
            "risk_on_dates": int(tested_risk_on.sum()),
            "tested_dates": int(len(tested_risk_on)),
            "risk_on_ratio": risk_on_ratio,
            "minimum_risk_on_ratio": config.minimum_risk_on_ratio,
            "risk_on_rebalances_per_phase": risk_on_rebalances_per_phase,
            "scheduled_rebalances_per_phase": scheduled_rebalances_per_phase,
            "risk_on_rebalance_ratios_per_phase": risk_on_rebalance_ratios_per_phase,
            "minimum_risk_on_rebalances_per_phase": config.minimum_risk_on_rebalances_per_phase,
            "minimum_risk_on_rebalance_ratio_per_phase": (
                config.minimum_risk_on_rebalance_ratio_per_phase
            ),
            "signal_activity_passed": signal_activity_passed,
            "cash_benchmark_return": 0.0,
            "timed_market_proxy_return": float(proxy_growth - 1.0),
            "timed_market_proxy_method": (
                "state transitions execute at next open: an existing risk-on position earns "
                "the overnight median; a next-open risk-on position earns the intraday median"
            ),
            "daily": [daily_diagnostics[int(index)] for index in index_array]
            if daily_diagnostics else [],
        }
        results: list[BacktestResult] = []
        stressed_results: list[BacktestResult] = []
        always_on_results: list[BacktestResult] = []
        cohort_initial_cash = config.initial_cash / phase_count
        stressed_cost = replace(
            config.cost_model,
            commission_bps=config.cost_model.commission_bps * 2.0,
            minimum_commission=config.cost_model.minimum_commission * 2.0,
            sell_stamp_tax_bps=config.cost_model.sell_stamp_tax_bps * 2.0,
            slippage_bps=config.cost_model.slippage_bps * 2.0,
        )
        for phase in range(phase_count):
            results.append(run_a_share_backtest(
                panel,
                timed_values,
                start_index=int(indices[0]),
                end_index=int(indices[-1]),
                rebalance_every=config.horizon,
                execution_phase=phase,
                top_k=min(config.top_k, len(panel.symbols)),
                retention_k=(
                    min(config.retention_k, len(panel.symbols))
                    if config.retention_k else None
                ),
                weighting=config.portfolio_weighting,
                volatility_lookback=config.volatility_lookback,
                initial_cash=cohort_initial_cash,
                cost=config.cost_model,
                risk_on_mask=risk_on,
            ))
            if config.market_timing_enabled:
                always_on_results.append(run_a_share_backtest(
                    panel,
                    values,
                    start_index=int(indices[0]),
                    end_index=int(indices[-1]),
                    rebalance_every=config.horizon,
                    execution_phase=phase,
                    top_k=min(config.top_k, len(panel.symbols)),
                    retention_k=(
                        min(config.retention_k, len(panel.symbols))
                        if config.retention_k else None
                    ),
                    weighting=config.portfolio_weighting,
                    volatility_lookback=config.volatility_lookback,
                    initial_cash=cohort_initial_cash,
                    cost=config.cost_model,
                ))
            if config.require_positive_stressed_backtest_return:
                stressed_results.append(run_a_share_backtest(
                    panel,
                    timed_values,
                    start_index=int(indices[0]),
                    end_index=int(indices[-1]),
                    rebalance_every=config.horizon,
                    execution_phase=phase,
                    top_k=min(config.top_k, len(panel.symbols)),
                    retention_k=(
                        min(config.retention_k, len(panel.symbols))
                        if config.retention_k else None
                    ),
                    weighting=config.portfolio_weighting,
                    volatility_lookback=config.volatility_lookback,
                    initial_cash=cohort_initial_cash,
                    cost=stressed_cost,
                    risk_on_mask=risk_on,
                ))
        actual_invested_rebalances_per_phase = [
            int(sum(
                bool(record.get("scheduled_execution"))
                and bool(record.get("risk_on_signal"))
                and float(record.get("gross_exposure", 0.0)) > 0.0
                for record in result.daily_records
            ))
            for result in results
        ]
        actual_invested_rebalance_ratios_per_phase = [
            count / max(1, scheduled)
            for count, scheduled in zip(
                actual_invested_rebalances_per_phase, scheduled_rebalances_per_phase
            )
        ]
        invested_flags = [
            float(record.get("gross_exposure", 0.0)) > 0.0
            for result in results
            for record in result.daily_records
        ]
        invested_day_ratio = float(np.mean(invested_flags)) if invested_flags else 0.0
        actual_activity_passed = (
            not config.market_timing_enabled
            or (
                invested_day_ratio >= config.minimum_risk_on_ratio
                and min(actual_invested_rebalances_per_phase, default=0)
                >= config.minimum_risk_on_rebalances_per_phase
                and min(actual_invested_rebalance_ratios_per_phase, default=0.0)
                >= config.minimum_risk_on_rebalance_ratio_per_phase
            )
        )
        activity_passed = signal_activity_passed and actual_activity_passed
        market_timing.update({
            "actual_invested_rebalances_per_phase": actual_invested_rebalances_per_phase,
            "actual_invested_rebalance_ratios_per_phase": (
                actual_invested_rebalance_ratios_per_phase
            ),
            "invested_day_ratio": invested_day_ratio,
            "actual_activity_passed": actual_activity_passed,
            "activity_passed": activity_passed,
        })
        pass_flags = [cls._backtest_passes(result, config) for result in results]
        median_return = float(np.median([result.total_return for result in results]))
        median_annualized = float(np.median([result.annualized_return for result in results]))
        median_sharpe = float(np.median([result.sharpe for result in results]))
        median_turnover = float(np.median([result.average_turnover for result in results]))
        worst_drawdown = float(min(result.max_drawdown for result in results))
        phase_sharpes = np.asarray([result.sharpe for result in results], dtype=np.float64)
        phase_sharpe_mad = float(np.median(np.abs(phase_sharpes - np.median(phase_sharpes))))
        cohort_equity = np.sum(
            np.asarray([result.equity_curve for result in results], dtype=np.float64),
            axis=0,
        )
        cohort_daily_returns = np.zeros(len(cohort_equity), dtype=np.float64)
        if len(cohort_equity) > 1:
            cohort_daily_returns[1:] = cohort_equity[1:] / np.maximum(cohort_equity[:-1], 1e-12) - 1.0
        periods = max(1, len(cohort_equity) - 1)
        cohort_total_return = float(cohort_equity[-1] / config.initial_cash - 1.0)
        cohort_annualized_return = float(
            (cohort_equity[-1] / config.initial_cash) ** (252.0 / periods) - 1.0
        )
        cohort_volatility = (
            float(np.std(cohort_daily_returns[1:], ddof=1) * np.sqrt(252.0))
            if len(cohort_equity) > 2 else 0.0
        )
        cohort_sharpe = (
            float(np.mean(cohort_daily_returns[1:]) / np.std(cohort_daily_returns[1:], ddof=1) * np.sqrt(252.0))
            if cohort_volatility > 1e-12 else 0.0
        )
        peaks = np.maximum.accumulate(cohort_equity)
        cohort_max_drawdown = float(np.min(cohort_equity / np.maximum(peaks, 1e-12) - 1.0))
        always_on_comparison: dict[str, Any] | None = None
        if always_on_results:
            always_equity = np.sum(
                np.asarray([result.equity_curve for result in always_on_results], dtype=np.float64),
                axis=0,
            )
            always_returns = np.zeros(len(always_equity), dtype=np.float64)
            if len(always_equity) > 1:
                always_returns[1:] = always_equity[1:] / np.maximum(always_equity[:-1], 1e-12) - 1.0
            always_peaks = np.maximum.accumulate(always_equity)
            always_total_return = float(always_equity[-1] / config.initial_cash - 1.0)
            always_max_drawdown = float(
                np.min(always_equity / np.maximum(always_peaks, 1e-12) - 1.0)
            )
            always_sharpe = (
                float(np.mean(always_returns[1:]) / np.std(always_returns[1:], ddof=1) * np.sqrt(252.0))
                if len(always_returns) > 2 and np.std(always_returns[1:], ddof=1) > 1e-12
                else 0.0
            )
            always_on_comparison = {
                "total_return": always_total_return,
                "sharpe": always_sharpe,
                "max_drawdown": always_max_drawdown,
                "ending_equity": float(always_equity[-1]),
                "overlay_incremental_total_return": cohort_total_return - always_total_return,
                "overlay_drawdown_improvement": cohort_max_drawdown - always_max_drawdown,
            }
        combined_traded_value = np.sum(
            np.asarray([
                [float(record.get("traded_value", 0.0)) for record in result.daily_records]
                for result in results
            ], dtype=np.float64),
            axis=0,
        )
        turnover_denominator = np.r_[config.initial_cash, cohort_equity[:-1]]
        aggregate_turnover = float(np.mean(
            combined_traded_value / np.maximum(turnover_denominator, 1e-12)
        ))
        aggregate_commission = float(sum(
            float(record.get("commission", 0.0))
            for result in results for record in result.daily_records
        ))
        aggregate_stamp_tax = float(sum(
            float(record.get("stamp_tax", 0.0))
            for result in results for record in result.daily_records
        ))
        aggregate_slippage_cost = float(sum(
            float(record.get("slippage_cost", 0.0))
            for result in results for record in result.daily_records
        ))
        aggregate_mean_gross_exposure = float(np.mean([
            sum(float(result.daily_records[date].get("gross_exposure", 0.0)) for result in results)
            / phase_count
            for date in range(len(cohort_equity))
        ]))
        cohort_passed = (
            (not config.require_positive_backtest_return or cohort_total_return > 0.0)
            and cohort_sharpe >= config.minimum_backtest_sharpe
            and cohort_max_drawdown >= config.maximum_backtest_drawdown
        )
        pass_ratio = float(np.mean(pass_flags))
        all_phase_drawdowns_pass = worst_drawdown >= config.maximum_backtest_drawdown
        stressed_total_return: float | None = None
        stressed_max_drawdown: float | None = None
        stressed_worst_phase_drawdown: float | None = None
        stressed_phase_pass_ratio: float | None = None
        stressed_passed = True
        if stressed_results:
            stressed_equity = np.sum(
                np.asarray([result.equity_curve for result in stressed_results], dtype=np.float64),
                axis=0,
            )
            stressed_total_return = float(stressed_equity[-1] / config.initial_cash - 1.0)
            stressed_peaks = np.maximum.accumulate(stressed_equity)
            stressed_max_drawdown = float(
                np.min(stressed_equity / np.maximum(stressed_peaks, 1e-12) - 1.0)
            )
            stressed_worst_phase_drawdown = float(
                min(result.max_drawdown for result in stressed_results)
            )
            stressed_phase_flags = [
                (
                    result.total_return > 0.0
                    and result.sharpe >= config.minimum_backtest_sharpe
                    and result.max_drawdown >= config.maximum_backtest_drawdown
                )
                for result in stressed_results
            ]
            stressed_phase_pass_ratio = float(np.mean(stressed_phase_flags))
            stressed_passed = (
                stressed_total_return > 0.0
                and stressed_max_drawdown >= config.maximum_backtest_drawdown
                and stressed_worst_phase_drawdown >= config.maximum_backtest_drawdown
                and stressed_phase_pass_ratio >= config.minimum_phase_pass_ratio
            )
        delisting_writeoff_count = int(sum(
            result.delisting_writeoff_count for result in results
        ))
        delisting_writeoff_value = float(sum(
            result.delisting_writeoff_value for result in results
        ))
        data_invalidation_writeoff_count = int(sum(
            result.data_invalidation_writeoff_count for result in results
        ))
        data_invalidation_writeoff_value = float(sum(
            result.data_invalidation_writeoff_value for result in results
        ))
        unverified_terminal_event_count = (
            delisting_writeoff_count + data_invalidation_writeoff_count
        )
        data_quality_passed = (
            not config.require_no_unverified_terminal_events
            or unverified_terminal_event_count == 0
        )
        data_quality_audit = {
            "required_clean": config.require_no_unverified_terminal_events,
            "passed": data_quality_passed,
            "delisting_writeoff_count": delisting_writeoff_count,
            "delisting_writeoff_value": delisting_writeoff_value,
            "data_invalidation_writeoff_count": data_invalidation_writeoff_count,
            "data_invalidation_writeoff_value": data_invalidation_writeoff_value,
            "unverified_terminal_event_count": unverified_terminal_event_count,
            "writeoff_value_fraction_of_initial_cash": (
                (delisting_writeoff_value + data_invalidation_writeoff_value)
                / config.initial_cash
            ),
            "classification": (
                "unverified vendor availability/anomaly event; not an official delisting label"
                if panel.metadata.get("source") == "qlib_binary_store"
                else "panel-provided terminal or invalidation event"
            ),
        }
        passed = (
            pass_ratio >= config.minimum_phase_pass_ratio
            and cohort_passed
            and all_phase_drawdowns_pass
            and stressed_passed
            and activity_passed
            and data_quality_passed
        )
        selection_score = (
            cohort_sharpe
            + 0.25 * cohort_annualized_return
            + 0.25 * worst_drawdown
            - 0.25 * phase_sharpe_mad
            - 0.05 * aggregate_turnover
        )
        return {
            "phase_count": phase_count,
            "market_timing": market_timing,
            "always_on_comparison": always_on_comparison,
            "overlay_forced_exit_days": int(sum(
                bool(record.get("forced_risk_exit"))
                for result in results for record in result.daily_records
            )),
            "overlay_blocked_sells": int(sum(
                int(record.get("blocked_sells_today", 0))
                for result in results for record in result.daily_records
                if record.get("forced_risk_exit")
            )),
            "pass_count": int(sum(pass_flags)),
            "pass_ratio": pass_ratio,
            "cohort_passed": cohort_passed,
            "all_phase_drawdowns_pass": all_phase_drawdowns_pass,
            "stressed_passed": stressed_passed,
            "data_quality_audit": data_quality_audit,
            "passed": passed,
            "median_total_return": median_return,
            "median_annualized_return": median_annualized,
            "median_sharpe": median_sharpe,
            "median_turnover": median_turnover,
            "aggregate_turnover": aggregate_turnover,
            "aggregate_commission": aggregate_commission,
            "aggregate_stamp_tax": aggregate_stamp_tax,
            "aggregate_slippage_cost": aggregate_slippage_cost,
            "aggregate_mean_gross_exposure": aggregate_mean_gross_exposure,
            "phase_sharpe_mad": phase_sharpe_mad,
            "double_cost_stress": {
                "enabled": bool(stressed_results),
                "total_return": stressed_total_return,
                "max_drawdown": stressed_max_drawdown,
                "worst_phase_drawdown": stressed_worst_phase_drawdown,
                "phase_pass_ratio": stressed_phase_pass_ratio,
                "commission_bps": stressed_cost.commission_bps,
                "minimum_commission": stressed_cost.minimum_commission,
                "sell_stamp_tax_bps": stressed_cost.sell_stamp_tax_bps,
                "slippage_bps": stressed_cost.slippage_bps,
            },
            "worst_max_drawdown": worst_drawdown,
            "selection_score": float(selection_score),
            "staggered_cohort": {
                "initial_cash": config.initial_cash,
                "cash_per_phase": cohort_initial_cash,
                "total_return": cohort_total_return,
                "annualized_return": cohort_annualized_return,
                "annualized_volatility": cohort_volatility,
                "sharpe": cohort_sharpe,
                "max_drawdown": cohort_max_drawdown,
                "ending_equity": float(cohort_equity[-1]),
                "dates": list(panel.dates[int(indices[0]): int(indices[-1]) + 1]),
                "equity_curve": [float(value) for value in cohort_equity],
            },
            "phases": [
                {"phase": phase, "passed": pass_flags[phase], **result.to_dict(include_curve=False)}
                for phase, result in enumerate(results)
            ],
        }

    @staticmethod
    def _evidence_known_columns(
        panel: PanelData,
        *,
        decision_end: int,
        evidence_end: int,
    ) -> np.ndarray:
        known = np.zeros(len(panel.symbols), dtype=bool)
        for values in panel.fields.values():
            known |= np.any(np.isfinite(values[: evidence_end + 1]), axis=0)
        for mask in (panel.eligible, panel.delisted, panel.invalidated):
            known |= np.any(mask[: evidence_end + 1], axis=0)
        for mask in (panel.buyable, panel.sellable):
            known |= np.any(mask[: decision_end + 1], axis=0)
        return known

    @staticmethod
    def _hash_token(hasher: Any, value: str | bytes) -> None:
        payload = value.encode("utf-8") if isinstance(value, str) else value
        hasher.update(len(payload).to_bytes(8, "big"))
        hasher.update(payload)

    @classmethod
    def _hash_array(cls, hasher: Any, values: np.ndarray) -> None:
        array = np.ascontiguousarray(values)
        cls._hash_token(hasher, array.dtype.str)
        cls._hash_token(hasher, json.dumps(array.shape, separators=(",", ":")))
        cls._hash_token(hasher, memoryview(array).cast("B"))

    @classmethod
    def _training_fingerprint(
        cls,
        panel: PanelData,
        *,
        decision_end: int,
        evidence_end: int,
    ) -> str:
        """Hash only information available through validation, never holdout."""

        known = cls._evidence_known_columns(
            panel, decision_end=decision_end, evidence_end=evidence_end
        )
        columns = sorted(np.flatnonzero(known), key=lambda index: panel.symbols[int(index)])
        hasher = hashlib.sha256(b"factor-factory-training-v2")
        cls._hash_token(hasher, str(decision_end))
        cls._hash_token(hasher, str(evidence_end))
        for value in panel.dates[: evidence_end + 1]:
            cls._hash_token(hasher, value)
        for column in columns:
            cls._hash_token(hasher, panel.symbols[int(column)])
        for name in sorted(panel.fields):
            cls._hash_token(hasher, name)
            cls._hash_array(hasher, panel.fields[name][: evidence_end + 1, columns])
        for name, mask, end in (
            ("eligible", panel.eligible, evidence_end),
            ("delisted", panel.delisted, evidence_end),
            ("invalidated", panel.invalidated, evidence_end),
            ("buyable", panel.buyable, decision_end),
            ("sellable", panel.sellable, decision_end),
        ):
            cls._hash_token(hasher, name)
            cls._hash_array(hasher, mask[: end + 1, columns])
        return hasher.hexdigest()

    @classmethod
    def _panel_cache_fingerprint(cls, panel: PanelData) -> str:
        """Bind expression caches to all panel content, not only provenance text."""

        hasher = hashlib.sha256(CACHE_SCHEMA_VERSION.encode("utf-8"))
        for value in panel.dates:
            cls._hash_token(hasher, value)
        for value in panel.symbols:
            cls._hash_token(hasher, value)
        for name in sorted(panel.fields):
            cls._hash_token(hasher, name)
            cls._hash_array(hasher, panel.fields[name])
        for name, mask in (
            ("eligible", panel.eligible),
            ("buyable", panel.buyable),
            ("sellable", panel.sellable),
            ("delisted", panel.delisted),
            ("invalidated", panel.invalidated),
        ):
            cls._hash_token(hasher, name)
            cls._hash_array(hasher, mask)
        return hasher.hexdigest()

    @classmethod
    def _panel_structure_fingerprint(cls, panel: PanelData) -> str:
        """Bind the requested calendar, symbol identities, and availability endpoints."""

        hasher = hashlib.sha256(b"factor-factory-panel-structure-v1")
        for value in panel.dates:
            cls._hash_token(hasher, value)
        for value in panel.symbols:
            cls._hash_token(hasher, value)
        cls._hash_array(hasher, panel.delisted)
        return hasher.hexdigest()

    @staticmethod
    def _promotion_policy(panel: PanelData) -> dict[str, Any]:
        keys = (
            "source", "promotion_ceiling", "promotion_blockers", "snapshot_sha256",
            "raw_tree_fingerprint", "snapshot_date", "universe", "point_in_time_membership",
            "research_membership_source", "listing_membership_source",
            "interval_semantics", "delisting_policy", "exchanges",
            "minimum_history_dates", "stock_only", "price_scale",
            "execution_warning", "locked_limit_proxy", "limit_warning",
            "adjustment_anomaly_quarantine",
            "adjustment_anomaly_adjusted_return_threshold",
            "adjustment_anomaly_source_change_threshold",
            "adjustment_anomaly_disagreement_threshold",
        )
        policy = {key: panel.metadata.get(key) for key in keys}
        policy["promotion_ceiling"] = str(
            panel.metadata.get("promotion_ceiling", "SHADOW_ONLY")
        )
        return policy

    @staticmethod
    def _runtime_signature(accelerator: str = "cpu") -> dict[str, Any]:
        return {
            "python_implementation": platform.python_implementation(),
            "python_version": list(sys.version_info[:3]),
            "numpy_version": np.__version__,
            "numerical_backend": accelerator_signature(accelerator),
        }

    @staticmethod
    def _engine_source_payload() -> dict[str, Any]:
        root = Path(__file__).resolve().parent
        modules: dict[str, dict[str, str]] = {}
        for name in ENGINE_MODULES:
            source = (root / name).read_bytes()
            modules[name] = {
                "sha256": hashlib.sha256(source).hexdigest(),
                "content_base64": base64.b64encode(source).decode("ascii"),
            }
        return {"schema_version": 1, "modules": modules}

    @staticmethod
    def _engine_fingerprint() -> str:
        """Hash the research engine modules that can change a frozen result."""

        root = Path(__file__).resolve().parent
        hasher = hashlib.sha256(b"factor-factory-engine-v1")
        for name in ENGINE_MODULES:
            path = root / name
            FactorFactory._hash_token(hasher, name)
            FactorFactory._hash_token(hasher, path.read_bytes())
        return hasher.hexdigest()

    @staticmethod
    def _split_manifest(panel: PanelData, splits: TimeSplits) -> dict[str, Any]:
        def describe(indices: np.ndarray) -> dict[str, Any]:
            return {
                "start": panel.dates[int(indices[0])],
                "end": panel.dates[int(indices[-1])],
                "count": len(indices),
            }

        return {
            "discovery": describe(splits.discovery),
            "validation": describe(splits.validation),
            "holdout": describe(splits.holdout),
            "purge_dates": splits.purge,
        }
