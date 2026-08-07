#!/usr/bin/env python3
"""Validate the complete planned data estate using only the Python standard library."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


EXPECTED_GATE_COUNTS = {
    "G_DATA_CORE": 20,
    "G_DATA_GLOBAL_CONTEXT": 8,
    "G_DATA_HK_EQUITY": 4,
    "G_DATA_GLOBAL_EQUITY": 3,
    "G_DATA_FUND": 4,
    "G_DATA_EVENT": 7,
    "G_DATA_XASSET": 2,
    "G_DATA_HF": 4,
}
EXPECTED_PLAN_PREFIXES = {
    "G_DATA_CORE": "CORE-",
    "G_DATA_GLOBAL_CONTEXT": "GCTX-",
    "G_DATA_HK_EQUITY": "HK-",
    "G_DATA_GLOBAL_EQUITY": "GLEQ-",
    "G_DATA_FUND": "FUND-",
    "G_DATA_EVENT": "EVENT-",
    "G_DATA_XASSET": "XASSET-",
    "G_DATA_HF": "HF-",
}
EXPECTED_COMPLETION_GATES = {
    "R1_CLASSIC_CN": {
        "G_DATA_CORE",
    },
    "R2_A_SHARE_GLOBAL": {
        "G_DATA_CORE",
        "G_DATA_GLOBAL_CONTEXT",
    },
    "G_DATA_RESEARCH_BASE": {
        "G_DATA_CORE",
        "G_DATA_GLOBAL_CONTEXT",
    },
    "REQUIRED_DATABASE_COMPLETE_35": {
        "G_DATA_CORE",
        "G_DATA_GLOBAL_CONTEXT",
        "G_DATA_HK_EQUITY",
        "G_DATA_GLOBAL_EQUITY",
    },
    "FULL_DATABASE_COMPLETE_52": set(EXPECTED_GATE_COUNTS),
}
EXPECTED_RESEARCH_TIER_PLAN_IDS = {
    "L0_INSPECTION": set(),
    "R0_PV": {f"CORE-{index:02d}" for index in range(1, 7)},
}
EXPECTED_RESEARCH_TIER_GATES = {
    "R1_CLASSIC_CN": {"G_DATA_CORE"},
    "R2_A_SHARE_GLOBAL": {"G_DATA_CORE", "G_DATA_GLOBAL_CONTEXT"},
}
EXPECTED_CURRENT_STATUS = {
    "L0_INSPECTION",
    "R0_PV",
    "R1_CLASSIC_CN",
    "R2_A_SHARE_GLOBAL",
    "REQUIRED_DATABASE_COMPLETE_35",
    "FULL_DATABASE_COMPLETE_52",
}
DATASET_REQUIRED = {
    "plan_id",
    "dataset_id",
    "name_zh",
    "domain",
    "priority",
    "gate",
    "frequency",
    "history_target",
    "pit_required",
    "source_profile",
    "storage",
    "status",
    "decision",
}


def load_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON root must be an object")
    return value


def require_nonempty_string(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label}: 必须是非空字符串")


def validate() -> tuple[list[str], dict[str, int]]:
    workspace = Path(__file__).resolve().parents[2]
    path = workspace / "data" / "catalog" / "planned_datasets.json"
    document = load_object(path)
    errors: list[str] = []

    if document.get("schema_version") != 1:
        errors.append("schema_version: 仅支持1")
    if document.get("status") != "PLANNING_ONLY_NOT_RESOLVABLE":
        errors.append("status: 必须保持PLANNING_ONLY_NOT_RESOLVABLE")

    gate_definitions = document.get("gate_definitions")
    if not isinstance(gate_definitions, dict):
        errors.append("gate_definitions: 必须是对象")
        gate_definitions = {}
    missing_gates = sorted(set(EXPECTED_GATE_COUNTS) - set(gate_definitions))
    extra_gates = sorted(set(gate_definitions) - set(EXPECTED_GATE_COUNTS))
    if missing_gates or extra_gates:
        errors.append(f"gate_definitions: missing={missing_gates}, extra={extra_gates}")

    source_profiles = document.get("source_profiles")
    if not isinstance(source_profiles, dict):
        errors.append("source_profiles: 必须是对象")
        source_profiles = {}

    completion = document.get("completion_definitions")
    if not isinstance(completion, dict):
        errors.append("completion_definitions: 必须是对象")
        completion = {}
    for name, expected in EXPECTED_COMPLETION_GATES.items():
        value = completion.get(name)
        if not isinstance(value, list) or set(value) != expected or len(value) != len(expected):
            errors.append(f"completion_definitions.{name}: 必须精确引用 {sorted(expected)}")
    for name, expected in EXPECTED_RESEARCH_TIER_PLAN_IDS.items():
        value = completion.get(name)
        if not isinstance(value, list) or set(value) != expected or len(value) != len(expected):
            errors.append(f"completion_definitions.{name}: 必须精确引用 {sorted(expected)}")

    current_status = completion.get("current_status")
    if not isinstance(current_status, dict):
        errors.append("completion_definitions.current_status: 必须是对象")
    else:
        missing_status = sorted(EXPECTED_CURRENT_STATUS - set(current_status))
        extra_status = sorted(set(current_status) - EXPECTED_CURRENT_STATUS)
        if missing_status or extra_status:
            errors.append(
                "completion_definitions.current_status: "
                f"missing={missing_status}, extra={extra_status}"
            )
        for name in EXPECTED_CURRENT_STATUS:
            if current_status.get(name) != "NOT_MET":
                errors.append(f"completion_definitions.current_status.{name}: 当前必须为NOT_MET")

    research_tiers = document.get("research_tier_definitions")
    if not isinstance(research_tiers, dict):
        errors.append("research_tier_definitions: 必须是对象")
        research_tiers = {}
    expected_tier_names = set(EXPECTED_RESEARCH_TIER_PLAN_IDS) | set(
        EXPECTED_RESEARCH_TIER_GATES
    )
    missing_tiers = sorted(expected_tier_names - set(research_tiers))
    extra_tiers = sorted(set(research_tiers) - expected_tier_names)
    if missing_tiers or extra_tiers:
        errors.append(
            f"research_tier_definitions: missing={missing_tiers}, extra={extra_tiers}"
        )
    for name, expected in EXPECTED_RESEARCH_TIER_PLAN_IDS.items():
        tier = research_tiers.get(name)
        if not isinstance(tier, dict):
            continue
        plan_ids = tier.get("required_plan_ids")
        if not isinstance(plan_ids, list) or set(plan_ids) != expected or len(plan_ids) != len(expected):
            errors.append(
                f"research_tier_definitions.{name}.required_plan_ids: "
                f"必须精确引用 {sorted(expected)}"
            )
        if tier.get("completion_status") != "NOT_MET":
            errors.append(f"research_tier_definitions.{name}.completion_status: 当前必须为NOT_MET")
    for name, expected in EXPECTED_RESEARCH_TIER_GATES.items():
        tier = research_tiers.get(name)
        if not isinstance(tier, dict):
            continue
        gates = tier.get("required_gates")
        if not isinstance(gates, list) or set(gates) != expected or len(gates) != len(expected):
            errors.append(
                f"research_tier_definitions.{name}.required_gates: "
                f"必须精确引用 {sorted(expected)}"
            )
        if tier.get("completion_status") != "NOT_MET":
            errors.append(f"research_tier_definitions.{name}.completion_status: 当前必须为NOT_MET")

    datasets = document.get("datasets")
    if not isinstance(datasets, list):
        errors.append("datasets: 必须是数组")
        datasets = []

    seen_plan_ids: set[str] = set()
    seen_dataset_ids: set[str] = set()
    gate_counts: Counter[str] = Counter()
    for index, item in enumerate(datasets):
        label = f"datasets[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label}: 必须是对象")
            continue
        missing = sorted(DATASET_REQUIRED - item.keys())
        if missing:
            errors.append(f"{label}: 缺少字段 {', '.join(missing)}")

        plan_id = item.get("plan_id")
        dataset_id = item.get("dataset_id")
        gate = item.get("gate")
        source_profile = item.get("source_profile")
        status = item.get("status")

        require_nonempty_string(plan_id, f"{label}.plan_id", errors)
        require_nonempty_string(dataset_id, f"{label}.dataset_id", errors)
        require_nonempty_string(status, f"{label}.status", errors)

        if isinstance(plan_id, str):
            if plan_id in seen_plan_ids:
                errors.append(f"{label}.plan_id: 重复值 {plan_id}")
            seen_plan_ids.add(plan_id)
        if isinstance(dataset_id, str):
            if dataset_id in seen_dataset_ids:
                errors.append(f"{label}.dataset_id: 重复值 {dataset_id}")
            seen_dataset_ids.add(dataset_id)

        if gate not in EXPECTED_GATE_COUNTS:
            errors.append(f"{label}.gate: 未定义 {gate}")
        else:
            gate_counts[gate] += 1
            prefix = EXPECTED_PLAN_PREFIXES[gate]
            if isinstance(plan_id, str) and not plan_id.startswith(prefix):
                errors.append(f"{label}.plan_id: {gate} 必须使用前缀 {prefix}")

        if source_profile not in source_profiles:
            errors.append(f"{label}.source_profile: 未定义 {source_profile}")
        storage = item.get("storage")
        if not isinstance(storage, list) or not storage or not all(
            isinstance(value, str) and value for value in storage
        ):
            errors.append(f"{label}.storage: 必须是非空字符串数组")
        if not isinstance(item.get("pit_required"), bool):
            errors.append(f"{label}.pit_required: 必须是布尔值")

        if gate == "G_DATA_GLOBAL_CONTEXT" and (
            not isinstance(status, str)
            or not status.startswith("PLANNED_REQUIRED_FOR_RESEARCH_BASE")
        ):
            errors.append(f"{label}.status: GCTX必须标记为正式研究基础必建")
        if gate in {"G_DATA_HK_EQUITY", "G_DATA_GLOBAL_EQUITY"} and (
            not isinstance(status, str)
            or not status.startswith("PLANNED_REQUIRED_FOR_COMPLETE_DATABASE")
        ):
            errors.append(f"{label}.status: HK/GLEQ必须标记为完整数据库必建")

    declared_count = document.get("planned_dataset_count")
    if declared_count != len(datasets):
        errors.append(
            f"planned_dataset_count: 声明{declared_count}，实际{len(datasets)}"
        )
    if len(datasets) != sum(EXPECTED_GATE_COUNTS.values()):
        errors.append(f"datasets: 必须精确包含52项，实际{len(datasets)}")
    if dict(gate_counts) != EXPECTED_GATE_COUNTS:
        errors.append(
            f"gate counts: 预期{EXPECTED_GATE_COUNTS}，实际{dict(gate_counts)}"
        )

    r0_pv_count = len(EXPECTED_RESEARCH_TIER_PLAN_IDS["R0_PV"])
    r1_classic_cn_count = sum(
        gate_counts[gate] for gate in EXPECTED_RESEARCH_TIER_GATES["R1_CLASSIC_CN"]
    )
    r2_a_share_global_count = sum(
        gate_counts[gate] for gate in EXPECTED_RESEARCH_TIER_GATES["R2_A_SHARE_GLOBAL"]
    )
    research_base_count = sum(
        gate_counts[gate] for gate in EXPECTED_COMPLETION_GATES["G_DATA_RESEARCH_BASE"]
    )
    required_count = sum(
        gate_counts[gate]
        for gate in EXPECTED_COMPLETION_GATES["REQUIRED_DATABASE_COMPLETE_35"]
    )
    full_count = sum(gate_counts.values())
    if (
        r0_pv_count,
        r1_classic_cn_count,
        r2_a_share_global_count,
        research_base_count,
        required_count,
        full_count,
    ) != (6, 20, 28, 28, 35, 52):
        errors.append(
            "completion counts: "
            f"r0_pv={r0_pv_count}, r1_classic_cn={r1_classic_cn_count}, "
            f"r2_a_share_global={r2_a_share_global_count}, "
            f"research_base={research_base_count}, required={required_count}, full={full_count}"
        )

    metrics = {
        "datasets": len(datasets),
        "gates": len(gate_counts),
        "source_profiles": len(source_profiles),
        "r0_pv": r0_pv_count,
        "r1_classic_cn": r1_classic_cn_count,
        "r2_a_share_global": r2_a_share_global_count,
        "research_base": research_base_count,
        "required_database": required_count,
        "full_database": full_count,
    }
    return errors, metrics


def main() -> int:
    try:
        errors, metrics = validate()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"PLANNED CATALOG INVALID: 无法读取计划: {exc}", file=sys.stderr)
        return 2
    if errors:
        print(f"PLANNED CATALOG INVALID: {len(errors)} 个问题", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "PLANNED CATALOG PASS: "
        f"datasets={metrics['datasets']}, gates={metrics['gates']}, "
        f"source_profiles={metrics['source_profiles']}, "
        f"r0_pv={metrics['r0_pv']}, "
        f"r1_classic_cn={metrics['r1_classic_cn']}, "
        f"r2_a_share_global={metrics['r2_a_share_global']}, "
        f"research_base={metrics['research_base']}, "
        f"required_database={metrics['required_database']}, "
        f"full_database={metrics['full_database']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
