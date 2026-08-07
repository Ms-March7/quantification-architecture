#!/usr/bin/env python3
"""Validate the L1-D3A G-DATA-CORE contract using the Python standard library."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


DATASET_REQUIRED = {
    "plan_id",
    "dataset_id",
    "schema_version",
    "grain",
    "pit_contract",
    "source_bindings",
    "tables",
    "quality_gates",
    "known_blockers",
}
PIT_REQUIRED = {"decision_time_rule", "required_time_fields", "revision_rule"}
BINDING_REQUIRED = {
    "binding_id",
    "primary_source_ids",
    "calibration_source_ids",
    "license_gate",
    "ingestion_status",
}
TABLE_REQUIRED = {"table_id", "grain", "inherits", "primary_key", "fields"}
FIELD_REQUIRED = {
    "name",
    "dtype",
    "nullable",
    "unit",
    "description",
    "source_binding",
    "pit_role",
}
SOURCE_REQUIRED = {
    "source_id",
    "provider",
    "tier",
    "authority_type",
    "official_urls",
    "acquisition_role",
    "access_status",
    "license_status",
    "bulk_ingestion_status",
    "evidence_as_of",
    "notes",
}
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def load_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON root must be an object")
    return value


def require_fields(value: dict[str, Any], required: set[str], label: str, errors: list[str]) -> None:
    missing = sorted(required - value.keys())
    if missing:
        errors.append(f"{label}: 缺少字段 {', '.join(missing)}")


def string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def validate() -> tuple[list[str], dict[str, int]]:
    workspace = Path(__file__).resolve().parents[2]
    root = workspace / "data" / "catalog" / "contracts" / "g_data_core_v1"
    contract = load_object(root / "contract.json")
    sources_doc = load_object(root / contract.get("source_registry", "sources.json"))
    planned = load_object(workspace / "data" / "catalog" / "planned_datasets.json")
    errors: list[str] = []

    if contract.get("schema_version") != 1:
        errors.append("contract.schema_version: 仅支持1")
    if not SEMVER.match(str(contract.get("contract_version", ""))):
        errors.append("contract.contract_version: 必须是x.y.z")
    group_files = contract.get("dataset_contract_files")
    if not string_list(group_files) or len(group_files) != 3:
        errors.append("contract.dataset_contract_files: 必须精确列出3个非空文件")
        group_files = []

    sources = sources_doc.get("sources")
    if not isinstance(sources, list):
        errors.append("sources.sources: 必须是数组")
        sources = []
    source_ids: set[str] = set()
    for index, source in enumerate(sources):
        label = f"sources[{index}]"
        if not isinstance(source, dict):
            errors.append(f"{label}: 必须是对象")
            continue
        require_fields(source, SOURCE_REQUIRED, label, errors)
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            errors.append(f"{label}.source_id: 必须是非空字符串")
        elif source_id in source_ids:
            errors.append(f"{label}.source_id: 重复值 {source_id}")
        else:
            source_ids.add(source_id)
        if not string_list(source.get("official_urls")) and source.get("official_urls") != []:
            errors.append(f"{label}.official_urls: 必须是字符串数组")

    global_bindings = contract.get("global_source_bindings")
    if not isinstance(global_bindings, list):
        errors.append("contract.global_source_bindings: 必须是数组")
        global_bindings = []
    global_binding_ids: set[str] = set()
    for index, binding in enumerate(global_bindings):
        label = f"global_source_bindings[{index}]"
        if not isinstance(binding, dict):
            errors.append(f"{label}: 必须是对象")
            continue
        require_fields(binding, BINDING_REQUIRED, label, errors)
        binding_id = binding.get("binding_id")
        if isinstance(binding_id, str):
            if binding_id in global_binding_ids:
                errors.append(f"{label}.binding_id: 重复值 {binding_id}")
            global_binding_ids.add(binding_id)
        for key in ("primary_source_ids", "calibration_source_ids"):
            refs = binding.get(key)
            if not string_list(refs) and refs != []:
                errors.append(f"{label}.{key}: 必须是字符串数组")
                continue
            for ref in refs or []:
                if ref not in source_ids:
                    errors.append(f"{label}.{key}: 未登记source_id {ref}")

    field_sets = contract.get("field_sets")
    if not isinstance(field_sets, dict) or not field_sets:
        errors.append("contract.field_sets: 必须是非空对象")
        field_sets = {}
    inherited_fields: dict[str, dict[str, dict[str, Any]]] = {}
    for field_set_id, fields in field_sets.items():
        label = f"field_sets.{field_set_id}"
        if not isinstance(fields, list) or not fields:
            errors.append(f"{label}: 必须是非空数组")
            continue
        names: dict[str, dict[str, Any]] = {}
        for index, field in enumerate(fields):
            field_label = f"{label}[{index}]"
            if not isinstance(field, dict):
                errors.append(f"{field_label}: 必须是对象")
                continue
            require_fields(field, FIELD_REQUIRED, field_label, errors)
            name = field.get("name")
            if not isinstance(name, str) or not name:
                errors.append(f"{field_label}.name: 必须是非空字符串")
            elif name in names:
                errors.append(f"{field_label}.name: 重复值 {name}")
            else:
                names[name] = field
            if field.get("source_binding") not in global_binding_ids:
                errors.append(f"{field_label}.source_binding: 未登记全局binding")
        inherited_fields[field_set_id] = names

    core_plans = {
        item["plan_id"]: item["dataset_id"]
        for item in planned.get("datasets", [])
        if isinstance(item, dict) and item.get("gate") == "G_DATA_CORE"
    }
    datasets: list[dict[str, Any]] = []
    group_ids: set[str] = set()
    for relative in group_files:
        path = root / relative
        if not path.is_file():
            errors.append(f"dataset_contract_files: 文件不存在 {relative}")
            continue
        group = load_object(path)
        if group.get("schema_version") != 1:
            errors.append(f"{relative}.schema_version: 仅支持1")
        if group.get("contract_version") != contract.get("contract_version"):
            errors.append(f"{relative}.contract_version: 与主合同不一致")
        group_id = group.get("group_id")
        if not isinstance(group_id, str) or group_id in group_ids:
            errors.append(f"{relative}.group_id: 缺失或重复")
        else:
            group_ids.add(group_id)
        values = group.get("datasets")
        if not isinstance(values, list):
            errors.append(f"{relative}.datasets: 必须是数组")
        else:
            datasets.extend(item for item in values if isinstance(item, dict))
            if len(values) != sum(isinstance(item, dict) for item in values):
                errors.append(f"{relative}.datasets: 每项必须是对象")

    seen_plan_ids: set[str] = set()
    seen_dataset_ids: set[str] = set()
    table_count = 0
    field_count = sum(len(value) for value in field_sets.values() if isinstance(value, list))
    blocked_count = 0
    for index, dataset in enumerate(datasets):
        label = f"datasets[{index}]"
        require_fields(dataset, DATASET_REQUIRED, label, errors)
        plan_id = dataset.get("plan_id")
        dataset_id = dataset.get("dataset_id")
        if plan_id in seen_plan_ids:
            errors.append(f"{label}.plan_id: 重复值 {plan_id}")
        if dataset_id in seen_dataset_ids:
            errors.append(f"{label}.dataset_id: 重复值 {dataset_id}")
        if isinstance(plan_id, str):
            seen_plan_ids.add(plan_id)
        if isinstance(dataset_id, str):
            seen_dataset_ids.add(dataset_id)
        if core_plans.get(plan_id) != dataset_id:
            errors.append(f"{label}: 与planned_datasets不一致 {plan_id} -> {dataset_id}")
        if not SEMVER.match(str(dataset.get("schema_version", ""))):
            errors.append(f"{label}.schema_version: 必须是x.y.z")

        pit = dataset.get("pit_contract")
        if not isinstance(pit, dict):
            errors.append(f"{label}.pit_contract: 必须是对象")
            pit = {}
        else:
            require_fields(pit, PIT_REQUIRED, f"{label}.pit_contract", errors)
        required_time_fields = pit.get("required_time_fields", [])
        if not string_list(required_time_fields):
            errors.append(f"{label}.pit_contract.required_time_fields: 必须是非空字符串数组")
            required_time_fields = []
        for mandatory in ("available_at", "revision_id", "snapshot_id"):
            if mandatory not in required_time_fields:
                errors.append(f"{label}.pit_contract.required_time_fields: 缺少 {mandatory}")

        bindings = dataset.get("source_bindings")
        if not isinstance(bindings, list) or not bindings:
            errors.append(f"{label}.source_bindings: 必须是非空数组")
            bindings = []
        binding_ids = set(global_binding_ids)
        for binding_index, binding in enumerate(bindings):
            binding_label = f"{label}.source_bindings[{binding_index}]"
            if not isinstance(binding, dict):
                errors.append(f"{binding_label}: 必须是对象")
                continue
            require_fields(binding, BINDING_REQUIRED, binding_label, errors)
            binding_id = binding.get("binding_id")
            if not isinstance(binding_id, str) or not binding_id:
                errors.append(f"{binding_label}.binding_id: 必须是非空字符串")
            elif binding_id in binding_ids:
                errors.append(f"{binding_label}.binding_id: 重复或覆盖全局binding {binding_id}")
            else:
                binding_ids.add(binding_id)
            if str(binding.get("ingestion_status", "")).startswith("BLOCKED"):
                blocked_count += 1
            for key in ("primary_source_ids", "calibration_source_ids"):
                refs = binding.get(key)
                if not string_list(refs) and refs != []:
                    errors.append(f"{binding_label}.{key}: 必须是字符串数组")
                    continue
                for ref in refs or []:
                    if ref not in source_ids:
                        errors.append(f"{binding_label}.{key}: 未登记source_id {ref}")

        tables = dataset.get("tables")
        if not isinstance(tables, list) or not tables:
            errors.append(f"{label}.tables: 必须是非空数组")
            tables = []
        table_ids: set[str] = set()
        dataset_field_union: set[str] = set()
        for table_index, table in enumerate(tables):
            table_count += 1
            table_label = f"{label}.tables[{table_index}]"
            if not isinstance(table, dict):
                errors.append(f"{table_label}: 必须是对象")
                continue
            require_fields(table, TABLE_REQUIRED, table_label, errors)
            table_id = table.get("table_id")
            if not isinstance(table_id, str) or not table_id:
                errors.append(f"{table_label}.table_id: 必须是非空字符串")
            elif table_id in table_ids:
                errors.append(f"{table_label}.table_id: 重复值 {table_id}")
            else:
                table_ids.add(table_id)
            inherits = table.get("inherits")
            if not string_list(inherits):
                errors.append(f"{table_label}.inherits: 必须是非空字符串数组")
                inherits = []
            if "record_lineage" not in inherits:
                errors.append(f"{table_label}.inherits: 必须包含record_lineage")
            resolved_fields: set[str] = set()
            inherited_definitions: dict[str, dict[str, Any]] = {}
            for field_set_id in inherits:
                if field_set_id not in inherited_fields:
                    errors.append(f"{table_label}.inherits: 未登记field_set {field_set_id}")
                else:
                    inherited_definitions.update(inherited_fields[field_set_id])
                    resolved_fields.update(inherited_fields[field_set_id])
            fields = table.get("fields")
            if not isinstance(fields, list) or not fields:
                errors.append(f"{table_label}.fields: 必须是非空数组")
                fields = []
            local_names: set[str] = set()
            for field_index, field in enumerate(fields):
                field_count += 1
                field_label = f"{table_label}.fields[{field_index}]"
                if not isinstance(field, dict):
                    errors.append(f"{field_label}: 必须是对象")
                    continue
                require_fields(field, FIELD_REQUIRED, field_label, errors)
                name = field.get("name")
                if not isinstance(name, str) or not name:
                    errors.append(f"{field_label}.name: 必须是非空字符串")
                elif name in local_names:
                    errors.append(f"{field_label}.name: 本地重复字段 {name}")
                else:
                    local_names.add(name)
                    resolved_fields.add(name)
                    inherited = inherited_definitions.get(name)
                    if inherited is not None:
                        if field.get("dtype") != inherited.get("dtype"):
                            errors.append(
                                f"{field_label}.dtype: 细化继承字段{name}时不得改变类型 "
                                f"{field.get('dtype')!r} != {inherited.get('dtype')!r}"
                            )
                        if inherited.get("nullable") is False and field.get("nullable") is True:
                            errors.append(
                                f"{field_label}.nullable: 细化继承字段{name}时不得放宽非空约束"
                            )
                if not isinstance(field.get("nullable"), bool):
                    errors.append(f"{field_label}.nullable: 必须是布尔值")
                if field.get("source_binding") not in binding_ids:
                    errors.append(f"{field_label}.source_binding: 未登记binding {field.get('source_binding')}")
            primary_key = table.get("primary_key")
            if not string_list(primary_key):
                errors.append(f"{table_label}.primary_key: 必须是非空字符串数组")
            else:
                for key in primary_key:
                    if key not in resolved_fields:
                        errors.append(f"{table_label}.primary_key: 字段不存在 {key}")
            dataset_field_union.update(resolved_fields)

        for required_time in required_time_fields:
            if required_time not in dataset_field_union:
                errors.append(f"{label}: PIT必需字段在全部逻辑表中均不存在 {required_time}")

        quality_gates = dataset.get("quality_gates")
        if not isinstance(quality_gates, list) or not quality_gates:
            errors.append(f"{label}.quality_gates: 必须是非空数组")
        elif not all(isinstance(item, (str, dict)) for item in quality_gates):
            errors.append(f"{label}.quality_gates: 每项必须是字符串或对象")
        known_blockers = dataset.get("known_blockers")
        if not isinstance(known_blockers, list):
            errors.append(f"{label}.known_blockers: 必须是数组")
        elif not all(isinstance(item, (str, dict)) for item in known_blockers):
            errors.append(f"{label}.known_blockers: 每项必须是字符串或对象")

    if seen_plan_ids != set(core_plans):
        errors.append(
            "CORE覆盖不完整: "
            f"missing={sorted(set(core_plans) - seen_plan_ids)}, "
            f"extra={sorted(seen_plan_ids - set(core_plans))}"
        )
    if len(datasets) != 20:
        errors.append(f"datasets: 必须精确包含20项，实际{len(datasets)}")

    metrics = {
        "sources": len(source_ids),
        "datasets": len(datasets),
        "tables": table_count,
        "fields": field_count,
        "blocked_bindings": blocked_count,
    }
    return errors, metrics


def main() -> int:
    try:
        errors, metrics = validate()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"CORE CONTRACT INVALID: 无法读取合同: {exc}", file=sys.stderr)
        return 2
    if errors:
        print(f"CORE CONTRACT INVALID: {len(errors)} 个问题", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "CORE CONTRACT PASS: "
        f"sources={metrics['sources']}, datasets={metrics['datasets']}, "
        f"tables={metrics['tables']}, fields={metrics['fields']}, "
        f"blocked_bindings={metrics['blocked_bindings']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
