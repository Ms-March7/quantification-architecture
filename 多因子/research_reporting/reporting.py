"""Integrity-first reports for immutable factor-research run artifacts.

The module is deliberately independent from ``factor_factory``.  A committed
run is read only after every SHA-256 declared by COMMITTED/manifest has passed.
An aborted run is treated as technical evidence only: partial candidate files
are never promoted into research conclusions.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 2
REPORTER_NAME = "research_reporting"
STAGES = ("discovery", "validation", "holdout")
SEMANTIC_PHASES = (
    "discovery_numeric_screening",
    "discovery_event_gate",
    "validation",
    "holdout",
)
METRIC_FIELDS = (
    "mean_ic",
    "icir",
    "cost_adjusted_top_excess",
    "stressed_cost_adjusted_top_excess",
    "top_excess_mean",
    "turnover",
    "p_value",
    "q_value",
    "permutations",
    "permutation_block_length",
)


class ReportingError(RuntimeError):
    """Base class for a report that cannot be produced safely."""


class IntegrityError(ReportingError):
    """Raised when committed bytes do not match their immutable registry."""


class RunStateError(ReportingError):
    """Raised when a directory has no single terminal marker."""


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportingError(f"不能读取 JSON {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise IntegrityError(f"不能读取待校验文件 {path}: {exc}") from exc
    return digest.hexdigest()


def _inside_run(run_dir: Path, relative_name: str) -> Path:
    relative = Path(relative_name)
    if relative.is_absolute():
        raise IntegrityError(f"产物路径必须是相对路径: {relative_name}")
    root = run_dir.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise IntegrityError(f"产物路径越界: {relative_name}") from exc
    return candidate


def _normalize_artifacts(value: Any) -> list[tuple[str, Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        pairs = list(value.items())
    elif isinstance(value, list):
        pairs = []
        for record in value:
            if not isinstance(record, Mapping) or not isinstance(record.get("path"), str):
                raise IntegrityError("manifest.artifacts 列表项缺少 path")
            pairs.append((record["path"], record))
    else:
        raise IntegrityError("manifest.artifacts 必须是对象或列表")
    if not pairs:
        raise IntegrityError("manifest.artifacts 为空，无法校验提交内容")
    for name, record in pairs:
        if not isinstance(name, str) or not isinstance(record, Mapping):
            raise IntegrityError("manifest.artifacts 含非法条目")
    return pairs


def _verify_committed(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Path], dict[str, Any]]:
    committed_path = run_dir / "COMMITTED.json"
    manifest_path = run_dir / "manifest.json"
    committed = _read_json(committed_path)
    if not isinstance(committed, dict):
        raise IntegrityError("COMMITTED.json 必须是 JSON 对象")

    expected_manifest = committed.get("manifest_sha256")
    if not isinstance(expected_manifest, str) or not expected_manifest:
        raise IntegrityError("COMMITTED.json 缺少 manifest_sha256")
    actual_manifest = _sha256(manifest_path)
    if actual_manifest.casefold() != expected_manifest.casefold():
        raise IntegrityError(
            f"manifest.json SHA-256 不匹配（期望 {expected_manifest}，实际 {actual_manifest}）"
        )
    # The commit marker is the trust anchor.  Do not parse or act on manifest
    # content until its exact bytes have been authenticated against that marker.
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise IntegrityError("manifest.json 必须是 JSON 对象")

    verified_paths: dict[str, Path] = {}
    evidence: list[dict[str, Any]] = []
    for relative_name, registry in _normalize_artifacts(manifest.get("artifacts")):
        path = _inside_run(run_dir, relative_name)
        if not path.is_file():
            raise IntegrityError(f"登记产物不存在: {relative_name}")
        expected_hash = registry.get("sha256")
        if not isinstance(expected_hash, str) or not expected_hash:
            raise IntegrityError(f"登记产物缺少 sha256: {relative_name}")
        expected_bytes = registry.get("bytes")
        actual_bytes = path.stat().st_size
        if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool):
            raise IntegrityError(f"登记产物缺少合法 bytes: {relative_name}")
        if actual_bytes != expected_bytes:
            raise IntegrityError(
                f"{relative_name} 字节数不匹配（期望 {expected_bytes}，实际 {actual_bytes}）"
            )
        actual_hash = _sha256(path)
        if actual_hash.casefold() != expected_hash.casefold():
            raise IntegrityError(
                f"{relative_name} SHA-256 不匹配（期望 {expected_hash}，实际 {actual_hash}）"
            )
        verified_paths[relative_name] = path
        evidence.append(
            {
                "path": relative_name,
                "bytes": actual_bytes,
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
                "status": "PASSED",
            }
        )

    integrity = {
        "status": "PASSED",
        "commit_manifest_sha256": expected_manifest,
        "actual_manifest_sha256": actual_manifest,
        "artifacts": evidence,
    }
    return committed, manifest, verified_paths, integrity


def _find_artifact(paths: Mapping[str, Path], basename: str) -> Path | None:
    exact = paths.get(basename)
    if exact is not None:
        return exact
    matches = [path for name, path in paths.items() if Path(name).name == basename]
    if len(matches) > 1:
        raise IntegrityError(f"manifest 中有多个 {basename}，含义不唯一")
    return matches[0] if matches else None


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _metric_summary(metrics: Any) -> dict[str, int | float | None]:
    source = metrics if isinstance(metrics, Mapping) else {}
    return {name: _finite_number(source.get(name)) for name in METRIC_FIELDS}


def _is_discovery_first(manifest: Mapping[str, Any]) -> bool:
    config = manifest.get("config")
    protocol = manifest.get("selection_protocol")
    return bool(
        (isinstance(config, Mapping) and config.get("discovery_first_selection"))
        or (isinstance(protocol, Mapping) and protocol.get("discovery_first_selection"))
    )


def _candidate_failure_phase(
    *,
    raw_stage: str,
    gate: str,
    discovery_first: bool,
    has_validation_metrics: bool,
) -> str:
    """Map legacy storage fields to the phase that actually produced a failure.

    V6 discovery-first runs deliberately copy Discovery screening failures into
    ``validation_failures`` for row-level compatibility.  Treating that field
    name as stage evidence therefore mislabels candidates that never reached
    Validation.
    """

    if raw_stage == "discovery":
        return "discovery_numeric_screening"
    if raw_stage == "validation" and discovery_first and not has_validation_metrics:
        if gate.startswith("discovery_event_"):
            return "discovery_event_gate"
        return "discovery_numeric_screening"
    return raw_stage


def _statistical_test_summary(
    metrics: Mapping[str, Any],
    *,
    stage: str,
    discovery_first: bool,
    configured_permutations: int | None,
    maximum_q_value: float | None,
) -> dict[str, Any]:
    recorded = _finite_number(metrics.get("permutations"))
    permutations: int | None = None
    source = "NOT_RECORDED"
    if isinstance(recorded, int) and not isinstance(recorded, bool):
        permutations = recorded
        source = "CANDIDATE_METRICS"
    elif discovery_first and stage == "discovery":
        # factor_factory's discovery-first contract scores Discovery with
        # permutations=0; p=q=1.0 are then placeholders, not test results.
        permutations = 0
        source = "DISCOVERY_FIRST_PROTOCOL"
    elif stage == "validation" and configured_permutations is not None:
        permutations = configured_permutations
        source = "RUN_CONFIG"

    p_value = _finite_number(metrics.get("p_value"))
    q_value = _finite_number(metrics.get("q_value"))
    if permutations == 0:
        status = "NOT_RUN"
        supports_significance = False
        note_cn = "未运行置换检验；p/q=1（如出现）是占位值，不构成统计显著证据。"
    elif permutations is None:
        status = "UNKNOWN"
        supports_significance = False
        note_cn = "产物未记录可核验的置换次数，不作统计显著性判断。"
    elif p_value is None or q_value is None or maximum_q_value is None:
        status = "TESTED_THRESHOLD_UNAVAILABLE"
        supports_significance = False
        note_cn = "已配置置换检验，但缺少完整 p/q 或阈值证据，不作统计显著性判断。"
    else:
        supports_significance = bool(q_value <= maximum_q_value)
        status = "PASSED_Q_THRESHOLD" if supports_significance else "FAILED_Q_THRESHOLD"
        note_cn = (
            "q 值通过预登记阈值。"
            if supports_significance
            else "q 值未通过预登记阈值，不构成统计显著证据。"
        )
    return {
        "status": status,
        "permutations": permutations,
        "permutations_source": source,
        "p_value": p_value,
        "q_value": q_value,
        "maximum_q_value": maximum_q_value,
        "supports_significance_claim": supports_significance,
        "note_cn": note_cn,
    }


def _ranking_key(entry: Mapping[str, Any]) -> tuple[float, float, float]:
    metrics = entry["metrics"]
    values = []
    for key in ("mean_ic", "icir", "cost_adjusted_top_excess"):
        value = metrics.get(key)
        values.append(float(value) if value is not None else -math.inf)
    return tuple(values)  # type: ignore[return-value]


def _push_top(
    heap: list[tuple[tuple[float, float, float], int, dict[str, Any]]],
    entry: dict[str, Any],
    ordinal: int,
    top_n: int,
) -> None:
    item = (_ranking_key(entry), ordinal, entry)
    if len(heap) < top_n:
        heapq.heappush(heap, item)
    elif item[:2] > heap[0][:2]:
        heapq.heapreplace(heap, item)


def _compact_event(candidate_id: str, stage: str, value: Any, kind: str = "candidate") -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    stress = value.get("double_cost_stress")
    stress_map = stress if isinstance(stress, Mapping) else {}
    market = value.get("market_timing")
    market_map = market if isinstance(market, Mapping) else {}
    return {
        "candidate_id": candidate_id,
        "stage": stage,
        "kind": kind,
        "passed": bool(value.get("passed", False)),
        "pass_count": _finite_number(value.get("pass_count")),
        "phase_count": _finite_number(value.get("phase_count")),
        "pass_ratio": _finite_number(value.get("pass_ratio")),
        "median_total_return": _finite_number(value.get("median_total_return")),
        "median_annualized_return": _finite_number(value.get("median_annualized_return")),
        "median_sharpe": _finite_number(value.get("median_sharpe")),
        "worst_max_drawdown": _finite_number(value.get("worst_max_drawdown")),
        "median_turnover": _finite_number(value.get("median_turnover")),
        "cohort_passed": bool(value.get("cohort_passed", False)),
        "stressed_passed": bool(value.get("stressed_passed", False)),
        "all_phase_drawdowns_pass": bool(value.get("all_phase_drawdowns_pass", False)),
        "market_activity_passed": bool(
            market_map.get("actual_activity_passed", market_map.get("activity_passed", False))
        ),
        "double_cost_stress": {
            "total_return": _finite_number(stress_map.get("total_return")),
            "max_drawdown": _finite_number(stress_map.get("max_drawdown")),
            "phase_pass_ratio": _finite_number(stress_map.get("phase_pass_ratio")),
        },
    }


def _candidate_data(
    path: Path | None,
    top_n: int,
    *,
    discovery_first: bool,
    configured_permutations: int | None,
    maximum_q_value: float | None,
) -> dict[str, Any]:
    heaps: dict[str, list[tuple[tuple[float, float, float], int, dict[str, Any]]]] = {
        stage: [] for stage in STAGES
    }
    stage_metric_counts = Counter()
    gate_counts: dict[str, Counter[str]] = {
        phase: Counter() for phase in SEMANTIC_PHASES
    }
    events: dict[tuple[str, str], dict[str, Any]] = {}
    count = 0
    if path is None:
        return {
            "candidate_count": None,
            "top_candidate_stage": None,
            "top_candidates": [],
            "candidate_gate_failures": {phase: {} for phase in SEMANTIC_PHASES},
            "stage_metric_counts": {stage: 0 for stage in STAGES},
            "events": events,
        }

    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ReportingError(f"不能读取候选文件 {path}: {exc}") from exc
    with handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise IntegrityError(f"已校验的 candidates.jsonl 第 {line_number} 行不是合法 JSON") from exc
            if not isinstance(row, Mapping):
                raise IntegrityError(f"已校验的 candidates.jsonl 第 {line_number} 行不是对象")
            count += 1
            candidate_id = str(row.get("candidate_id", f"row-{line_number}"))
            has_validation_metrics = isinstance(row.get("validation"), Mapping)
            semantic_failures: dict[str, list[str]] = {
                phase: [] for phase in SEMANTIC_PHASES
            }
            for raw_stage in STAGES:
                failures = row.get(f"{raw_stage}_failures", [])
                if failures is None:
                    failures = []
                if not isinstance(failures, list):
                    raise IntegrityError(
                        f"候选 {candidate_id} 的 {raw_stage}_failures 不是列表"
                    )
                for gate in failures:
                    gate_name = str(gate)
                    phase = _candidate_failure_phase(
                        raw_stage=raw_stage,
                        gate=gate_name,
                        discovery_first=discovery_first,
                        has_validation_metrics=has_validation_metrics,
                    )
                    gate_counts[phase][gate_name] += 1
                    semantic_failures[phase].append(gate_name)

            for stage in STAGES:
                metrics = row.get(stage)
                if isinstance(metrics, Mapping):
                    stage_metric_counts[stage] += 1
                    summary = _metric_summary(metrics)
                    if stage == "discovery":
                        entry_failures = (
                            semantic_failures["discovery_numeric_screening"]
                            + semantic_failures["discovery_event_gate"]
                        )
                    else:
                        entry_failures = semantic_failures.get(stage, [])
                    entry = {
                        "candidate_id": candidate_id,
                        "formula": str(row.get("formula", "")),
                        "generation": row.get("generation"),
                        "origin": row.get("origin"),
                        "status": row.get("status"),
                        "complexity": row.get("complexity"),
                        "stage": stage,
                        "metrics": summary,
                        "statistical_test": _statistical_test_summary(
                            summary,
                            stage=stage,
                            discovery_first=discovery_first,
                            configured_permutations=configured_permutations,
                            maximum_q_value=maximum_q_value,
                        ),
                        "failures": entry_failures,
                    }
                    _push_top(heaps[stage], entry, count, top_n)

                event_stage = (
                    "discovery_event_gate"
                    if discovery_first and stage == "discovery"
                    else stage
                )
                event = _compact_event(
                    candidate_id,
                    event_stage,
                    row.get(f"{stage}_backtest"),
                )
                if event is not None:
                    events[(event_stage, candidate_id)] = event

    if stage_metric_counts["validation"]:
        top_stage = "validation"
    elif stage_metric_counts["discovery"]:
        top_stage = "discovery"
    elif stage_metric_counts["holdout"]:
        top_stage = "holdout"
    else:
        top_stage = None
    top_candidates = []
    if top_stage is not None:
        ordered = sorted(heaps[top_stage], key=lambda item: (item[0], item[1]), reverse=True)
        top_candidates = [item[2] for item in ordered]
    return {
        "candidate_count": count,
        "top_candidate_stage": top_stage,
        "top_candidates": top_candidates,
        "candidate_gate_failures": {
            phase: dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))
            for phase, counter in gate_counts.items()
        },
        "stage_metric_counts": {
            stage: int(stage_metric_counts[stage]) for stage in STAGES
        },
        "events": events,
    }


def _add_backtest_artifact(
    path: Path | None,
    events: dict[tuple[str, str], dict[str, Any]],
    *,
    discovery_first: bool,
) -> None:
    if path is None:
        return
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        return
    for stage in STAGES:
        event_stage = (
            "discovery_event_gate"
            if discovery_first and stage == "discovery"
            else stage
        )
        components = payload.get(f"{stage}_components")
        if isinstance(components, Mapping):
            for candidate_id, value in components.items():
                key = (event_stage, str(candidate_id))
                event = _compact_event(str(candidate_id), event_stage, value)
                if event is not None and key not in events:
                    events[key] = event
        ensemble = payload.get(f"{stage}_ensemble")
        key = (event_stage, "__ensemble__")
        event = _compact_event(
            "__ensemble__", event_stage, ensemble, kind="ensemble"
        )
        if event is not None and key not in events:
            events[key] = event


def _selected_count(path: Path | None) -> int | None:
    if path is None:
        return None
    payload = _read_json(path)
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, Mapping):
        for key in ("selected", "components", "candidates"):
            if isinstance(payload.get(key), list):
                return len(payload[key])
    raise IntegrityError("已校验的 selected.json 结构无法识别")


def _nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _runtime_evidence(manifest: Mapping[str, Any]) -> dict[str, Any]:
    signature = _nested(manifest, "research_attempt", "validation_evidence", "runtime_signature")
    if not isinstance(signature, Mapping):
        signature = _nested(
            manifest,
            "panel",
            "metadata",
            "research_attempt",
            "validation_evidence",
            "runtime_signature",
        )
    config = manifest.get("config") if isinstance(manifest.get("config"), Mapping) else {}
    return {
        "engine": manifest.get("engine"),
        "created_at_utc": manifest.get("created_at_utc"),
        "runtime": manifest.get("runtime") if isinstance(manifest.get("runtime"), Mapping) else {},
        "runtime_signature": dict(signature) if isinstance(signature, Mapping) else {},
        "configured_accelerator": config.get("accelerator"),
        "gpu_column_chunk": config.get("gpu_column_chunk"),
        "protocol_id": config.get("protocol_id"),
        "protocol_version": config.get("protocol_version"),
    }


def _ensemble_gate_counts(
    manifest: Mapping[str, Any],
    *,
    discovery_first: bool,
    validation_consumed: bool,
) -> dict[str, dict[str, int]]:
    ensemble = manifest.get("ensemble")
    if not isinstance(ensemble, Mapping):
        return {phase: {} for phase in SEMANTIC_PHASES}
    counters: dict[str, Counter[str]] = {
        phase: Counter() for phase in SEMANTIC_PHASES
    }
    for stage in ("validation", "holdout"):
        values = ensemble.get(f"{stage}_failures", [])
        if not isinstance(values, list):
            continue
        for item in values:
            gate = str(item)
            phase = stage
            if (
                stage == "validation"
                and discovery_first
                and not validation_consumed
                and gate == "insufficient_ensemble_components"
            ):
                # No frozen Discovery components existed, so this is a
                # Validation-entry failure rather than evidence that Validation
                # was evaluated.  Keep it with the Discovery event/freeze phase.
                phase = "discovery_event_gate"
            counters[phase][gate] += 1
    return {
        phase: dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))
        for phase, counter in counters.items()
    }


def _stage_semantics(
    manifest: Mapping[str, Any],
    candidates: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    *,
    discovery_first: bool,
) -> dict[str, Any]:
    metric_counts = candidates.get("stage_metric_counts", {})
    if not isinstance(metric_counts, Mapping):
        metric_counts = {}
    gate_counts = candidates.get("candidate_gate_failures", {})
    if not isinstance(gate_counts, Mapping):
        gate_counts = {}
    event_counts = Counter(str(event.get("stage")) for event in events)
    protocol = manifest.get("selection_protocol")
    protocol_map = protocol if isinstance(protocol, Mapping) else {}
    ensemble = manifest.get("ensemble")
    ensemble_map = ensemble if isinstance(ensemble, Mapping) else {}
    attempt = manifest.get("research_attempt")
    attempt_map = attempt if isinstance(attempt, Mapping) else {}

    validation_candidate_count = _finite_number(
        protocol_map.get("validation_candidate_count")
    )
    validation_metric_count = int(metric_counts.get("validation", 0) or 0)
    validation_event_count = int(event_counts.get("validation", 0))
    has_validation_ensemble_metrics = isinstance(
        ensemble_map.get("validation_metrics"), Mapping
    )
    validation_consumed = bool(
        validation_metric_count
        or validation_event_count
        or has_validation_ensemble_metrics
        or (
            isinstance(validation_candidate_count, (int, float))
            and validation_candidate_count > 0
        )
    )
    registry_state = attempt_map.get("state")
    if validation_consumed:
        validation_status = "CONSUMED"
        validation_note = "存在候选或组合的 Validation 评估证据。"
    elif discovery_first and validation_candidate_count == 0:
        validation_status = "NOT_CONSUMED"
        validation_note = (
            "Discovery 未冻结出可送检组件，Validation 候选数为 0；没有候选、事件或组合的 "
            "Validation 指标。登记状态不替代这些阶段证据。"
        )
    else:
        validation_status = "NO_EVALUATION_EVIDENCE"
        validation_note = "未发现候选、事件或组合的 Validation 评估证据。"

    discovery_event_stage = "discovery_event_gate" if discovery_first else "discovery"
    discovery_event_count = int(event_counts.get(discovery_event_stage, 0))
    discovery_metric_count = int(metric_counts.get("discovery", 0) or 0)
    discovery_numeric_gates = gate_counts.get("discovery_numeric_screening", {})
    discovery_event_gates = gate_counts.get("discovery_event_gate", {})
    numeric_failure_count = (
        sum(int(value) for value in discovery_numeric_gates.values())
        if isinstance(discovery_numeric_gates, Mapping)
        else 0
    )
    event_failure_count = (
        sum(int(value) for value in discovery_event_gates.values())
        if isinstance(discovery_event_gates, Mapping)
        else 0
    )

    holdout_opened = bool(ensemble_map.get("holdout_was_opened", False))
    holdout_metric_count = int(metric_counts.get("holdout", 0) or 0)
    holdout_event_count = int(event_counts.get("holdout", 0))
    holdout_has_metrics = isinstance(ensemble_map.get("holdout_metrics"), Mapping)
    holdout_has_evidence = bool(
        holdout_opened
        or holdout_metric_count
        or holdout_event_count
        or holdout_has_metrics
    )

    return {
        "protocol": "DISCOVERY_FIRST" if discovery_first else "STANDARD",
        "discovery_numeric_screening": {
            "status": "CONSUMED" if discovery_metric_count else "NOT_CONSUMED",
            "candidate_metric_count": discovery_metric_count,
            "candidate_gate_failure_count": numeric_failure_count,
            "permutations": 0 if discovery_first else None,
            "statistical_test_status": "NOT_RUN" if discovery_first else "NOT_INFERRED",
            "note_cn": (
                "Discovery 数值筛选用于描述性排序和数值门；permutations=0，p/q=1 是未检验占位，"
                "不得据此宣称统计显著。"
                if discovery_first
                else "Discovery 数值指标按已提交产物汇总。"
            ),
        },
        "discovery_event_gate": {
            "status": "CONSUMED" if discovery_event_count else "NOT_REACHED",
            "candidate_backtest_count": discovery_event_count,
            "candidate_gate_failure_count": event_failure_count,
            "note_cn": "这是 Discovery 上的事件/分相回测门，不是 Validation。",
        },
        "validation": {
            "status": validation_status,
            "candidate_count": validation_candidate_count,
            "candidate_metric_count": validation_metric_count,
            "event_backtest_count": validation_event_count,
            "ensemble_metrics_present": has_validation_ensemble_metrics,
            "registry_state": registry_state,
            "note_cn": validation_note,
        },
        "holdout": {
            "status": "OPENED" if holdout_has_evidence else "NOT_OPENED",
            "holdout_was_opened": holdout_opened,
            "candidate_metric_count": holdout_metric_count,
            "event_backtest_count": holdout_event_count,
            "ensemble_metrics_present": holdout_has_metrics,
            "note_cn": (
                "Holdout 有打开或消费证据。"
                if holdout_has_evidence
                else "Holdout 未打开，也没有 Holdout 指标或回测证据。"
            ),
        },
    }


def _top_candidate_scope(
    top_stage: str | None,
    top_candidates: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    phase = (
        "discovery_numeric_screening"
        if top_stage == "discovery"
        else top_stage
    )
    statuses = Counter(
        str(item.get("statistical_test", {}).get("status", "UNKNOWN"))
        for item in top_candidates
        if isinstance(item, Mapping)
    )
    return {
        "stage": top_stage,
        "phase": phase,
        "metric_scope": (
            "DISCOVERY_SPLIT_ONLY" if top_stage == "discovery" else str(top_stage or "NONE").upper()
        ),
        "ranking_basis": "mean_ic_then_icir_then_cost_adjusted_top_excess",
        "ranking_is_significance_claim": False,
        "statistical_test_status_counts": dict(sorted(statuses.items())),
        "note_cn": (
            "Top 候选是 Discovery 数值筛选的描述性排序，不是 Validation 结果，也不是统计显著性排名。"
            if top_stage == "discovery"
            else "候选排名按阶段指标排序，本身不构成统计显著性结论。"
        ),
    }


def _event_sort_key(event: Mapping[str, Any]) -> tuple[int, float, float]:
    total_return = event.get("median_total_return")
    sharpe = event.get("median_sharpe")
    return (
        int(bool(event.get("passed"))),
        float(total_return) if total_return is not None else -math.inf,
        float(sharpe) if sharpe is not None else -math.inf,
    )


def _build_committed(run_dir: Path, top_n: int) -> dict[str, Any]:
    committed, manifest, paths, integrity = _verify_committed(run_dir)
    discovery_first = _is_discovery_first(manifest)
    config = manifest.get("config")
    config_map = config if isinstance(config, Mapping) else {}
    raw_permutations = _finite_number(config_map.get("permutations"))
    configured_permutations = (
        int(raw_permutations)
        if isinstance(raw_permutations, int) and not isinstance(raw_permutations, bool)
        else None
    )
    selection_gates = config_map.get("selection_gates")
    gates_map = selection_gates if isinstance(selection_gates, Mapping) else {}
    raw_maximum_q = _finite_number(gates_map.get("maximum_q_value"))
    maximum_q_value = (
        float(raw_maximum_q) if raw_maximum_q is not None else None
    )
    candidates = _candidate_data(
        _find_artifact(paths, "candidates.jsonl"),
        top_n,
        discovery_first=discovery_first,
        configured_permutations=configured_permutations,
        maximum_q_value=maximum_q_value,
    )
    selected_count = _selected_count(_find_artifact(paths, "selected.json"))
    _add_backtest_artifact(
        _find_artifact(paths, "backtests.json"),
        candidates["events"],
        discovery_first=discovery_first,
    )
    all_events = sorted(candidates.pop("events").values(), key=_event_sort_key, reverse=True)
    stage_semantics = _stage_semantics(
        manifest,
        candidates,
        all_events,
        discovery_first=discovery_first,
    )
    validation_consumed = stage_semantics["validation"]["status"] == "CONSUMED"
    ensemble = manifest.get("ensemble") if isinstance(manifest.get("ensemble"), Mapping) else {}
    qualified = bool(ensemble.get("qualified", False))
    promoted = bool(selected_count and qualified)
    if promoted:
        verdict_code = "COMMITTED_PROMOTED"
        verdict_cn = "研究产物完整提交，且存在已合格晋级因子。"
    elif qualified:
        verdict_code = "COMMITTED_QUALIFIED_WITHOUT_SELECTION"
        verdict_cn = "研究产物完整提交，研究门标记合格但没有已选因子；这是状态不一致，不算晋级成功。"
    else:
        verdict_code = "COMMITTED_NO_PROMOTION"
        verdict_cn = "研究产物完整提交，但未发现可晋级因子。"

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "reporter": REPORTER_NAME,
        "source_run": str(run_dir),
        "run_name": run_dir.name,
        "run_kind": "COMMITTED",
        "integrity": integrity,
        "outcome": {
            "terminal_state": manifest.get("attempt_terminal_state"),
            "verdict_code": verdict_code,
            "verdict_cn": verdict_cn,
            "artifact_committed": True,
            "research_qualified": qualified,
            "factor_promoted": promoted,
        },
        "summary": {
            "candidate_count": candidates["candidate_count"],
            "selected_count": selected_count,
            "event_backtest_count": len(all_events),
            "holdout_was_opened": bool(ensemble.get("holdout_was_opened", False)),
            "effective_promotion_ceiling": manifest.get("effective_promotion_ceiling"),
            "safety_ceiling": manifest.get("safety_ceiling"),
        },
        "stage_semantics": stage_semantics,
        "candidate_gate_failures": candidates["candidate_gate_failures"],
        "ensemble_gate_failures": _ensemble_gate_counts(
            manifest,
            discovery_first=discovery_first,
            validation_consumed=validation_consumed,
        ),
        "top_candidate_stage": candidates["top_candidate_stage"],
        "top_candidate_scope": _top_candidate_scope(
            candidates["top_candidate_stage"], candidates["top_candidates"]
        ),
        "top_candidates": candidates["top_candidates"],
        "event_backtests": all_events[:top_n],
        "runtime_evidence": _runtime_evidence(manifest),
        "commit_evidence": {
            "committed_at_utc": committed.get("committed_at_utc"),
            "registry_schema": committed.get("registry_schema"),
        },
    }
    return report


def _build_aborted(run_dir: Path) -> dict[str, Any]:
    aborted_path = run_dir / "ABORTED.json"
    aborted = _read_json(aborted_path)
    if not isinstance(aborted, Mapping):
        raise RunStateError("ABORTED.json 必须是 JSON 对象")
    runtime = aborted.get("runtime") if isinstance(aborted.get("runtime"), Mapping) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "reporter": REPORTER_NAME,
        "source_run": str(run_dir),
        "run_name": run_dir.name,
        "run_kind": "ABORTED",
        "integrity": {
            "status": "NOT_APPLICABLE_ABORTED",
            "aborted_marker_sha256": _sha256(aborted_path),
            "artifacts": [],
            "note_cn": "技术中止运行没有 COMMITTED 清单；未读取任何部分候选产物。",
        },
        "outcome": {
            "terminal_state": aborted.get("state", "ABORTED_TECHNICAL"),
            "verdict_code": "ABORTED_TECHNICAL",
            "verdict_cn": "技术中止，未形成可审计的已提交因子结论。",
            "artifact_committed": False,
            "research_qualified": False,
            "factor_promoted": False,
        },
        "summary": {
            "candidate_count": None,
            "selected_count": None,
            "event_backtest_count": None,
            "materialized_cache_matrices": aborted.get("cached_factor_matrices"),
            "materialized_cache_bytes": aborted.get("cached_factor_bytes"),
            "holdout_was_opened": bool(aborted.get("holdout_was_opened", False)),
            "effective_promotion_ceiling": None,
            "safety_ceiling": None,
        },
        "stage_semantics": {
            "protocol": "UNKNOWN_ABORTED",
            "discovery_numeric_screening": {"status": "NOT_APPLICABLE"},
            "discovery_event_gate": {"status": "NOT_APPLICABLE"},
            "validation": {"status": "NOT_APPLICABLE"},
            "holdout": {"status": "NOT_APPLICABLE"},
        },
        "candidate_gate_failures": {phase: {} for phase in SEMANTIC_PHASES},
        "ensemble_gate_failures": {phase: {} for phase in SEMANTIC_PHASES},
        "top_candidate_stage": None,
        "top_candidate_scope": _top_candidate_scope(None, []),
        "top_candidates": [],
        "event_backtests": [],
        "runtime_evidence": {
            "engine": aborted.get("engine"),
            "created_at_utc": aborted.get("aborted_at_utc"),
            "runtime": dict(runtime),
            "runtime_signature": aborted.get("runtime_signature", {}),
            "configured_accelerator": aborted.get("accelerator"),
            "gpu_column_chunk": aborted.get("gpu_column_chunk"),
            "protocol_id": aborted.get("protocol_id"),
            "protocol_version": aborted.get("protocol_version"),
        },
        "abort_evidence": {
            "reason": aborted.get("reason"),
            "result_committed": bool(aborted.get("result_committed", False)),
            "manifest_exists": bool(aborted.get("manifest_exists", False)),
            "selected_exists": bool(aborted.get("selected_exists", False)),
        },
    }


def build_report(run_dir: str | os.PathLike[str], top_n: int = 10) -> dict[str, Any]:
    """Build a report in memory without modifying the source run."""

    if top_n < 1:
        raise ReportingError("top_n 必须至少为 1")
    run = Path(run_dir).expanduser().resolve()
    if not run.is_dir():
        raise RunStateError(f"运行目录不存在: {run}")
    committed = (run / "COMMITTED.json").is_file()
    aborted = (run / "ABORTED.json").is_file()
    if committed == aborted:
        state = "同时存在 COMMITTED.json 和 ABORTED.json" if committed else "没有终态标记"
        raise RunStateError(f"{run} {state}")
    return _build_committed(run, top_n) if committed else _build_aborted(run)


def _fmt(value: Any, percent: bool = False) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.2%}" if percent else f"{value:.6f}"
    return str(value)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _phase_label(value: Any) -> str:
    labels = {
        "discovery": "Discovery",
        "discovery_numeric_screening": "Discovery 数值筛选",
        "discovery_event_gate": "Discovery 事件门",
        "validation": "Validation",
        "holdout": "Holdout",
    }
    key = str(value)
    return labels.get(key, key)


def _test_status_label(value: Any) -> str:
    labels = {
        "NOT_RUN": "未检验",
        "UNKNOWN": "不可核验",
        "TESTED_THRESHOLD_UNAVAILABLE": "已检验/阈值不全",
        "PASSED_Q_THRESHOLD": "通过 q 阈值",
        "FAILED_Q_THRESHOLD": "未通过 q 阈值",
    }
    key = str(value)
    return labels.get(key, key)


def _stage_status_label(value: Any) -> str:
    labels = {
        "CONSUMED": "CONSUMED（已消费）",
        "NOT_CONSUMED": "NOT_CONSUMED（未消费）",
        "NOT_REACHED": "NOT_REACHED（未到达）",
        "NO_EVALUATION_EVIDENCE": "NO_EVALUATION_EVIDENCE（无评估证据）",
        "OPENED": "OPENED（已打开）",
        "NOT_OPENED": "NOT_OPENED（未打开）",
        "NOT_APPLICABLE": "NOT_APPLICABLE（不适用）",
    }
    key = str(value)
    return labels.get(key, key)


def _json_code(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render a concise Chinese audit report."""

    outcome = report["outcome"]
    summary = report["summary"]
    integrity = report["integrity"]
    stage_semantics = report.get("stage_semantics", {})
    stage_map = stage_semantics if isinstance(stage_semantics, Mapping) else {}
    discovery_numeric = stage_map.get("discovery_numeric_screening", {})
    discovery_event = stage_map.get("discovery_event_gate", {})
    validation_semantics = stage_map.get("validation", {})
    holdout_semantics = stage_map.get("holdout", {})
    discovery_numeric = discovery_numeric if isinstance(discovery_numeric, Mapping) else {}
    discovery_event = discovery_event if isinstance(discovery_event, Mapping) else {}
    validation_semantics = validation_semantics if isinstance(validation_semantics, Mapping) else {}
    holdout_semantics = holdout_semantics if isinstance(holdout_semantics, Mapping) else {}
    lines = [
        f"# 因子研究运行报告：{_cell(report['run_name'])}",
        "",
        f"> **结论：{_cell(outcome['verdict_cn'])}**",
        "",
        "这份报告区分“产物完整提交”和“因子研究成功”。零入选或未通过研究门绝不记为成功。",
        "",
        "## 总览",
        "",
        "| 项目 | 值 |",
        "|---|---:|",
        f"| 运行终态 | {_cell(outcome.get('terminal_state'))} |",
        f"| 产物已提交 | {_fmt(outcome.get('artifact_committed'))} |",
        f"| 研究门合格 | {_fmt(outcome.get('research_qualified'))} |",
        f"| 因子已晋级 | {_fmt(outcome.get('factor_promoted'))} |",
        f"| 候选数 | {_fmt(summary.get('candidate_count'))} |",
        f"| 入选数 | {_fmt(summary.get('selected_count'))} |",
        f"| 事件回测数 | {_fmt(summary.get('event_backtest_count'))} |",
        f"| Discovery 数值筛选 | {_cell(_stage_status_label(discovery_numeric.get('status')))} |",
        f"| Discovery 事件门 | {_cell(_stage_status_label(discovery_event.get('status')))} |",
        f"| Validation 候选评估 | {_cell(_stage_status_label(validation_semantics.get('status')))} |",
        f"| Holdout 阶段 | {_cell(_stage_status_label(holdout_semantics.get('status')))} |",
        f"| Holdout 已打开 | {_fmt(summary.get('holdout_was_opened'))} |",
        f"| Promotion ceiling | {_cell(summary.get('effective_promotion_ceiling') or '—')} |",
        f"| Safety ceiling | {_cell(summary.get('safety_ceiling') or '—')} |",
        "",
        "## 完整性",
        "",
        f"完整性状态：`{_cell(integrity.get('status'))}`。",
        "",
    ]
    artifacts = integrity.get("artifacts", [])
    if artifacts:
        lines += ["| 产物 | 字节 | SHA-256 | 状态 |", "|---|---:|---|---|"]
        for item in artifacts:
            lines.append(
                f"| {_cell(item.get('path'))} | {_fmt(item.get('bytes'))} | "
                f"`{_cell(item.get('actual_sha256'))}` | {_cell(item.get('status'))} |"
            )
        lines.append("")
    elif integrity.get("note_cn"):
        lines += [str(integrity["note_cn"]), ""]

    lines += ["## 阶段语义与消费状态", ""]
    lines += [
        "| 阶段 | 状态 | 可审计证据 | 解释 |",
        "|---|---|---:|---|",
        f"| Discovery 数值筛选 | {_cell(_stage_status_label(discovery_numeric.get('status')))} | "
        f"候选指标 {_fmt(discovery_numeric.get('candidate_metric_count'))}；失败记录 "
        f"{_fmt(discovery_numeric.get('candidate_gate_failure_count'))} | "
        f"{_cell(discovery_numeric.get('note_cn', '—'))} |",
        f"| Discovery 事件门 | {_cell(_stage_status_label(discovery_event.get('status')))} | "
        f"事件回测 {_fmt(discovery_event.get('candidate_backtest_count'))}；失败记录 "
        f"{_fmt(discovery_event.get('candidate_gate_failure_count'))} | "
        f"{_cell(discovery_event.get('note_cn', '—'))} |",
        f"| Validation | {_cell(_stage_status_label(validation_semantics.get('status')))} | "
        f"候选 {_fmt(validation_semantics.get('candidate_count'))}；指标 "
        f"{_fmt(validation_semantics.get('candidate_metric_count'))}；事件回测 "
        f"{_fmt(validation_semantics.get('event_backtest_count'))} | "
        f"{_cell(validation_semantics.get('note_cn', '—'))} |",
        f"| Holdout | {_cell(_stage_status_label(holdout_semantics.get('status')))} | "
        f"候选指标 {_fmt(holdout_semantics.get('candidate_metric_count'))}；事件回测 "
        f"{_fmt(holdout_semantics.get('event_backtest_count'))} | "
        f"{_cell(holdout_semantics.get('note_cn', '—'))} |",
        "",
    ]
    registry_state = validation_semantics.get("registry_state")
    if registry_state:
        lines += [
            f"登记状态为 `{_cell(registry_state)}`；它是运行治理状态，不能替代上表中候选/组合是否实际产生 Validation 指标的证据。",
            "",
        ]

    lines += ["## 失败门频数", ""]
    gate_rows: list[tuple[str, str, int, str]] = []
    for stage, gates in report.get("candidate_gate_failures", {}).items():
        for gate, count in gates.items():
            gate_rows.append((stage, gate, count, "candidate"))
    for stage, gates in report.get("ensemble_gate_failures", {}).items():
        for gate, count in gates.items():
            gate_rows.append((stage, gate, count, "ensemble"))
    gate_rows.sort(key=lambda row: (-row[2], row[0], row[1], row[3]))
    if gate_rows:
        lines += ["| 阶段 | 门 | 失败次数 | 来源 |", "|---|---|---:|---|"]
        for stage, gate, count, source in gate_rows:
            lines.append(
                f"| {_cell(_phase_label(stage))} | {_cell(gate)} | {count} | {source} |"
            )
    else:
        lines.append("无可审计的失败门记录。")
    lines.append("")

    stage = report.get("top_candidate_stage")
    scope = report.get("top_candidate_scope", {})
    scope_map = scope if isinstance(scope, Mapping) else {}
    phase = scope_map.get("phase")
    lines += [f"## Top 候选（{_phase_label(phase or stage or '无可用阶段')}；描述性排序）", ""]
    if scope_map.get("note_cn"):
        lines += [str(scope_map["note_cn"]), ""]
    candidates = report.get("top_candidates", [])
    if candidates:
        lines += [
            "| 排名 | 候选 | 公式 | IC | ICIR | 成本后超额 | p | q | permutations | 统计检验 | 状态 |",
            "|---:|---|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
        for rank, item in enumerate(candidates, start=1):
            metrics = item["metrics"]
            statistical_test = item.get("statistical_test", {})
            test_map = statistical_test if isinstance(statistical_test, Mapping) else {}
            lines.append(
                f"| {rank} | {_cell(item.get('candidate_id'))} | {_cell(item.get('formula'))} | "
                f"{_fmt(metrics.get('mean_ic'))} | {_fmt(metrics.get('icir'))} | "
                f"{_fmt(metrics.get('cost_adjusted_top_excess'), percent=True)} | "
                f"{_fmt(test_map.get('p_value'))} | {_fmt(test_map.get('q_value'))} | "
                f"{_fmt(test_map.get('permutations'))} | "
                f"{_cell(_test_status_label(test_map.get('status')))} | "
                f"{_cell(item.get('status'))} |"
            )
    else:
        lines.append("没有可审计的候选排名；这不代表成功。")
    lines.append("")

    lines += ["## 事件回测", ""]
    events = report.get("event_backtests", [])
    if events:
        lines += [
            "| 候选 | 阶段 | 通过 | 收益中位数 | 最大回撤最差值 | Sharpe 中位数 | 双倍成本收益 |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
        for item in events:
            stress = item.get("double_cost_stress", {})
            lines.append(
                f"| {_cell(item.get('candidate_id'))} | {_cell(_phase_label(item.get('stage')))} | "
                f"{_fmt(item.get('passed'))} | {_fmt(item.get('median_total_return'), percent=True)} | "
                f"{_fmt(item.get('worst_max_drawdown'), percent=True)} | "
                f"{_fmt(item.get('median_sharpe'))} | "
                f"{_fmt(stress.get('total_return'), percent=True)} |"
            )
    else:
        lines.append("没有已提交、可审计的事件回测摘要。")
    lines.append("")

    lines += [
        "## 运行环境与 GPU 证据",
        "",
        "以下内容来自已提交清单或中止标记，不是本报告器重新探测的设备状态。",
        "",
        "```json",
        _json_code(report.get("runtime_evidence", {})),
        "```",
        "",
        "## 边界",
        "",
        "本报告只汇总并审计既有运行产物，不会重新计算因子、打开 Holdout、连接券商或构成投资建议。",
        "",
        f"生成时间（UTC）：{_cell(report.get('generated_at_utc'))}",
        "",
    ]
    return "\n".join(lines)


def _safe_basename(value: str) -> str:
    if not value or value in {".", ".."} or Path(value).name != value:
        raise ReportingError("basename 必须是不含目录的文件基名")
    sanitized = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "-", value).strip(".-")
    if not sanitized:
        raise ReportingError("basename 清理后为空")
    return sanitized


def _stage_atomic(path: Path, content: str) -> Path:
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except OSError as exc:
        raise ReportingError(f"不能暂存报告 {path}: {exc}") from exc


def write_report(
    run_dir: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    top_n: int = 10,
    basename: str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Build and atomically replace JSON and Markdown report files."""

    report = build_report(run_dir, top_n=top_n)
    output = Path(output_dir).expanduser().resolve()
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReportingError(f"不能创建报告目录 {output}: {exc}") from exc
    base = _safe_basename(basename or f"{report['run_name']}-research-report")
    json_path = output / f"{base}.json"
    markdown_path = output / f"{base}.md"
    json_content = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    markdown_content = render_markdown(report)
    json_tmp: Path | None = None
    markdown_tmp: Path | None = None
    try:
        json_tmp = _stage_atomic(json_path, json_content)
        markdown_tmp = _stage_atomic(markdown_path, markdown_content)
        os.replace(json_tmp, json_path)
        json_tmp = None
        os.replace(markdown_tmp, markdown_path)
        markdown_tmp = None
    except OSError as exc:
        raise ReportingError(f"不能原子替换报告文件: {exc}") from exc
    finally:
        for temporary in (json_tmp, markdown_tmp):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return json_path, markdown_path, report
