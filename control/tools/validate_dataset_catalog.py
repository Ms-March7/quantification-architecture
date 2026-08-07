#!/usr/bin/env python3
"""Validate the Quantification workspace dataset catalog using stdlib only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


CATALOG_LIB = Path(__file__).resolve().parents[2] / "data" / "catalog"
if str(CATALOG_LIB) not in sys.path:
    sys.path.insert(0, str(CATALOG_LIB))

from snapshot_contract import (  # noqa: E402
    HASH_ALGORITHM,
    SnapshotContractError,
    load_json_object as load_strict_json_object,
    verify_snapshot_contract,
)


STORE_KINDS = {
    "shared_run_store",
    "immutable_snapshot_store",
    "published_feature_store",
    "immutable_research_evidence_store",
    "application_state_store",
    "experimental_cache_store",
    "historical_output_store",
    "temporary_run_store",
}
LIFECYCLES = {"active", "frozen", "review", "historical", "failed", "smoke", "unmanaged", "planned"}
MUTABILITIES = {"immutable", "append_only", "mutable", "mixed"}
PURPOSES = {"DO_NOT_USE", "ENGINEERING_ONLY", "SMOKE_ONLY", "RESEARCH_ONLY", "SHADOW_ONLY", "LIVE_READY"}
LICENSES = {"VERIFIED", "DECLARED", "UNVERIFIED", "MIXED", "NOT_APPLICABLE"}
QUALITIES = {"OK", "REVIEW", "PARTIAL", "MISSING", "CONFLICT", "FAILED", "UNMANAGED"}
PURPOSE_RANK = {
    "DO_NOT_USE": 0,
    "ENGINEERING_ONLY": 1,
    "SMOKE_ONLY": 2,
    "RESEARCH_ONLY": 3,
    "SHADOW_ONLY": 4,
    "LIVE_READY": 5,
}
CATALOG_REQUIRED = {
    "schema_version",
    "updated_at",
    "workspace_root",
    "catalog_mode",
    "physical_migration",
    "authority_precedence",
    "owners",
    "stores",
    "datasets",
    "retired_datasets",
    "publication_contracts",
}

STORE_REQUIRED = {
    "store_id",
    "owner_project",
    "physical_root",
    "kind",
    "lifecycle",
    "mutability",
    "purpose_ceiling",
    "authority_paths",
}
DATASET_REQUIRED = {
    "dataset_id",
    "snapshot_id",
    "store_id",
    "owner_project",
    "physical_path",
    "lifecycle",
    "mutability",
    "purpose_ceiling",
    "license_status",
    "quality_status",
    "authority_paths",
    "consumers",
}
RETIRED_DATASET_REQUIRED = {
    "dataset_id",
    "snapshot_id",
    "owner_project",
    "former_physical_path",
    "retired_at",
    "status",
    "reason",
    "deleted_files",
    "deleted_bytes",
    "retained_authority_paths",
    "cleanup_receipt",
}


def parse_args() -> argparse.Namespace:
    workspace = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="校验 Quantification 统一数据目录")
    parser.add_argument(
        "catalog",
        nargs="?",
        type=Path,
        default=workspace / "data" / "catalog" / "datasets.json",
        help="datasets.json 路径",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    try:
        return load_strict_json_object(path)
    except SnapshotContractError as exc:
        raise ValueError(str(exc)) from exc


def normalized_path(raw: str) -> Path:
    return Path(raw.replace("/", os.sep)).resolve()


def validate_path(
    raw: Any,
    label: str,
    workspace_root: Path,
    errors: list[str],
    *,
    must_exist: bool = True,
) -> None:
    if not isinstance(raw, str) or not raw:
        errors.append(f"{label}: 路径必须是非空字符串")
        return
    path = normalized_path(raw)
    if not path.is_absolute():
        errors.append(f"{label}: 必须是绝对路径: {raw}")
        return
    try:
        common = Path(os.path.commonpath([workspace_root, path]))
    except ValueError:
        errors.append(f"{label}: 路径不在工作区磁盘: {raw}")
        return
    if common != workspace_root:
        errors.append(f"{label}: 路径越出工作区: {raw}")
    if must_exist and not path.exists():
        errors.append(f"{label}: 路径不存在: {raw}")


def require_fields(item: dict[str, Any], required: set[str], label: str, errors: list[str]) -> None:
    missing = sorted(required - item.keys())
    if missing:
        errors.append(f"{label}: 缺少字段 {', '.join(missing)}")


def require_enum(value: Any, allowed: set[str], label: str, errors: list[str]) -> None:
    if value not in allowed:
        errors.append(f"{label}: 非法值 {value!r}，允许值为 {sorted(allowed)}")


def validate_linked_manifests(
    dataset: dict[str, Any],
    label: str,
    errors: list[str],
) -> None:
    """Cross-check fields when an authority JSON exposes snapshot identity."""
    for raw_authority in dataset.get("authority_paths", []):
        if not isinstance(raw_authority, str) or not raw_authority.lower().endswith(".json"):
            continue
        authority = normalized_path(raw_authority)
        if not authority.is_file():
            continue
        try:
            with authority.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{label}: authority JSON 无法解析 {authority}: {exc}")
            continue
        if not isinstance(manifest, dict):
            continue
        if "dataset_id" in manifest and manifest["dataset_id"] != dataset.get("dataset_id"):
            errors.append(
                f"{label}: authority dataset_id 不一致: "
                f"{manifest['dataset_id']!r} != {dataset.get('dataset_id')!r}"
            )
        if "snapshot_id" in manifest and manifest["snapshot_id"] != dataset.get("snapshot_id"):
            errors.append(
                f"{label}: authority snapshot_id 不一致: "
                f"{manifest['snapshot_id']!r} != {dataset.get('snapshot_id')!r}"
            )
        if "content_hash" in manifest and manifest["content_hash"] != dataset.get("content_hash"):
            errors.append(
                f"{label}: authority content_hash 不一致: "
                f"{manifest['content_hash']!r} != {dataset.get('content_hash')!r}"
            )
        if "root_path" in manifest:
            manifest_root = normalized_path(manifest["root_path"])
            catalog_root = normalized_path(dataset.get("physical_path", ""))
            if manifest_root != catalog_root:
                errors.append(
                    f"{label}: authority root_path 不一致: "
                    f"{manifest['root_path']!r} != {dataset.get('physical_path')!r}"
                )


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_contract_tree_snapshot(
    catalog: dict[str, Any],
    dataset: dict[str, Any],
    label: str,
    workspace_root: Path,
    errors: list[str],
) -> None:
    if dataset.get("hash_algorithm") != "sha256-contract-tree-v1":
        return
    snapshot_path = normalized_path(dataset.get("physical_path", ""))
    if not snapshot_path.is_dir():
        errors.append(f"{label}: contract-tree 快照必须是目录")
        return
    required = {
        "manifest.json": snapshot_path / "manifest.json",
        "provenance.json": snapshot_path / "provenance.json",
        "partition-manifest.json": snapshot_path / "partition-manifest.json",
        "quality/summary.json": snapshot_path / "quality" / "summary.json",
    }
    authority_paths = {
        normalized_path(raw)
        for raw in dataset.get("authority_paths", [])
        if isinstance(raw, str) and raw
    }
    documents: dict[str, dict[str, Any]] = {}
    for name, path in required.items():
        if path.resolve() not in authority_paths:
            errors.append(f"{label}: authority_paths 缺少 {name}")
        if not path.is_file():
            errors.append(f"{label}: 缺少 {name}")
            continue
        try:
            documents[name] = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{label}: {name} 无法解析: {exc}")
    if len(documents) != len(required):
        return
    manifest = documents["manifest.json"]
    provenance = documents["provenance.json"]
    partitions = documents["partition-manifest.json"]
    for name, document in (
        ("manifest", manifest),
        ("provenance", provenance),
        ("partition-manifest", partitions),
    ):
        if document.get("dataset_id") != dataset.get("dataset_id"):
            errors.append(f"{label}: {name}.dataset_id 不一致")
        if document.get("snapshot_id") != dataset.get("snapshot_id"):
            errors.append(f"{label}: {name}.snapshot_id 不一致")
    if normalized_path(manifest.get("root_path", "")) != snapshot_path:
        errors.append(f"{label}: manifest.root_path 不一致")
    expected_content_hash = dataset.get("content_hash")
    if not isinstance(expected_content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_content_hash):
        errors.append(f"{label}: content_hash 必须是小写 SHA-256")
        expected_content_hash = ""
    if manifest.get("content_hash") != expected_content_hash:
        errors.append(f"{label}: manifest content_hash 与 catalog 不一致")
    files = partitions.get("files")
    if not isinstance(files, list) or not files:
        errors.append(f"{label}: partition-manifest.files 必须是非空数组")
        return
    payload_entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            errors.append(f"{label}: partition files[{index}] 必须是 object")
            continue
        relative = entry.get("workspace_relative_path")
        if not isinstance(relative, str) or not relative:
            errors.append(f"{label}: partition files[{index}] 路径缺失")
            continue
        if relative in seen_paths:
            errors.append(f"{label}: partition 路径重复: {relative}")
        seen_paths.add(relative)
        candidate = (workspace_root / Path(relative)).resolve()
        try:
            if Path(os.path.commonpath([workspace_root, candidate])) != workspace_root:
                errors.append(f"{label}: partition 路径越出工作区: {relative}")
                continue
        except ValueError:
            errors.append(f"{label}: partition 路径不在工作区磁盘: {relative}")
            continue
        if not candidate.is_file():
            errors.append(f"{label}: partition 文件不存在: {candidate}")
            continue
        expected_bytes = entry.get("bytes")
        expected_hash = entry.get("sha256")
        if candidate.stat().st_size != expected_bytes:
            errors.append(f"{label}: partition 字节数不一致: {relative}")
        if not isinstance(expected_hash, str) or sha256_file(candidate) != expected_hash:
            errors.append(f"{label}: partition 哈希不一致: {relative}")
        payload_entries.append(
            {
                "layer": entry.get("layer"),
                "relative_path": entry.get("relative_path"),
                "bytes": expected_bytes,
                "sha256": expected_hash,
            }
        )
    payload_entries.sort(key=lambda value: (str(value["layer"]), str(value["relative_path"])))
    payload_hash = hashlib.sha256(canonical_bytes(payload_entries)).hexdigest()
    if partitions.get("payload_hash") != payload_hash or manifest.get("payload_hash") != payload_hash:
        errors.append(f"{label}: payload_hash 与分区清单不一致")
    contract_value = {
        "dataset_id": dataset.get("dataset_id"),
        "snapshot_id": dataset.get("snapshot_id"),
        "source_id": provenance.get("source_id"),
        "contract_id": manifest.get("contract_id"),
        "dataset_schema_version": manifest.get("dataset_schema_version"),
        "available_at": manifest.get("available_at"),
        "partition_segments": manifest.get("partition_segments"),
        "payload_hash": payload_hash,
    }
    contract_hash = hashlib.sha256(canonical_bytes(contract_value)).hexdigest()
    if contract_hash != expected_content_hash:
        errors.append(f"{label}: 根合同哈希无法由 manifest/provenance/partition 重建")


def validate_catalog(catalog: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    require_fields(catalog, CATALOG_REQUIRED, "catalog", errors)
    if catalog.get("schema_version") != 1:
        errors.append("schema_version: 仅支持 1")
    if catalog.get("catalog_mode") != "federated_logical_catalog":
        errors.append("catalog_mode: 必须是 federated_logical_catalog")
    if not isinstance(catalog.get("physical_migration"), bool):
        errors.append("physical_migration: 必须是布尔值")
    updated_at = catalog.get("updated_at")
    if not isinstance(updated_at, str):
        errors.append("updated_at: 必须是 ISO 8601 字符串")
    else:
        try:
            datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"updated_at: 不是有效 ISO 8601 时间: {updated_at!r}")
    precedence = catalog.get("authority_precedence")
    if not isinstance(precedence, list) or not precedence or not all(isinstance(x, str) for x in precedence):
        errors.append("authority_precedence: 必须是非空字符串数组")

    raw_root = catalog.get("workspace_root")
    if not isinstance(raw_root, str):
        return errors + ["workspace_root: 必须是字符串"]
    workspace_root = normalized_path(raw_root)
    validate_path(raw_root, "workspace_root", workspace_root, errors)

    owners = catalog.get("owners")
    if not isinstance(owners, list) or not owners or not all(isinstance(x, str) for x in owners):
        errors.append("owners: 必须是非空字符串数组")
        owner_set: set[str] = set()
    else:
        owner_set = set(owners)
        if len(owner_set) != len(owners):
            errors.append("owners: 存在重复值")

    stores = catalog.get("stores")
    datasets = catalog.get("datasets")
    retired_datasets = catalog.get("retired_datasets", [])
    contracts = catalog.get("publication_contracts")
    if not isinstance(stores, list):
        errors.append("stores: 必须是数组")
        stores = []
    if not isinstance(datasets, list):
        errors.append("datasets: 必须是数组")
        datasets = []
    if not isinstance(retired_datasets, list):
        errors.append("retired_datasets: 必须是数组")
        retired_datasets = []
    if not isinstance(contracts, list):
        errors.append("publication_contracts: 必须是数组")

    store_ids: set[str] = set()
    stores_by_id: dict[str, dict[str, Any]] = {}
    for index, store in enumerate(stores):
        label = f"stores[{index}]"
        if not isinstance(store, dict):
            errors.append(f"{label}: 必须是对象")
            continue
        require_fields(store, STORE_REQUIRED, label, errors)
        store_id = store.get("store_id")
        if store_id in store_ids:
            errors.append(f"{label}.store_id: 重复值 {store_id}")
        if isinstance(store_id, str):
            store_ids.add(store_id)
            stores_by_id[store_id] = store
        if store.get("owner_project") not in owner_set:
            errors.append(f"{label}.owner_project: 未登记所有者 {store.get('owner_project')!r}")
        require_enum(store.get("kind"), STORE_KINDS, f"{label}.kind", errors)
        require_enum(store.get("lifecycle"), LIFECYCLES, f"{label}.lifecycle", errors)
        require_enum(store.get("mutability"), MUTABILITIES, f"{label}.mutability", errors)
        require_enum(store.get("purpose_ceiling"), PURPOSES, f"{label}.purpose_ceiling", errors)
        planned = store.get("lifecycle") == "planned"
        validate_path(store.get("physical_root"), f"{label}.physical_root", workspace_root, errors, must_exist=not planned)
        authorities = store.get("authority_paths")
        if not isinstance(authorities, list) or not authorities:
            errors.append(f"{label}.authority_paths: 必须是非空数组")
        else:
            for authority_index, authority in enumerate(authorities):
                validate_path(authority, f"{label}.authority_paths[{authority_index}]", workspace_root, errors)

    dataset_keys: set[tuple[str, str]] = set()
    for index, dataset in enumerate(datasets):
        label = f"datasets[{index}]"
        if not isinstance(dataset, dict):
            errors.append(f"{label}: 必须是对象")
            continue
        require_fields(dataset, DATASET_REQUIRED, label, errors)
        dataset_id = dataset.get("dataset_id")
        snapshot_id = dataset.get("snapshot_id")
        key = (dataset_id, snapshot_id)
        if key in dataset_keys:
            errors.append(f"{label}: 重复数据身份 {key}")
        if isinstance(dataset_id, str) and isinstance(snapshot_id, str):
            dataset_keys.add(key)
        if isinstance(snapshot_id, str) and "latest" in snapshot_id.lower():
            errors.append(f"{label}.snapshot_id: 禁止使用 latest")
        if dataset.get("store_id") not in store_ids:
            errors.append(f"{label}.store_id: 未登记 store {dataset.get('store_id')!r}")
        else:
            store = stores_by_id[dataset["store_id"]]
            if dataset.get("owner_project") != store.get("owner_project"):
                errors.append(
                    f"{label}.owner_project: 与 store 所有者不一致: "
                    f"{dataset.get('owner_project')!r} != {store.get('owner_project')!r}"
                )
            try:
                dataset_path = normalized_path(dataset.get("physical_path", ""))
                store_root = normalized_path(store.get("physical_root", ""))
                if Path(os.path.commonpath([dataset_path, store_root])) != store_root:
                    errors.append(f"{label}.physical_path: 不在所属 store 根目录下")
            except (OSError, ValueError):
                errors.append(f"{label}.physical_path: 无法与所属 store 比较")
            dataset_purpose = dataset.get("purpose_ceiling")
            store_purpose = store.get("purpose_ceiling")
            if dataset_purpose in PURPOSE_RANK and store_purpose in PURPOSE_RANK:
                if PURPOSE_RANK[dataset_purpose] > PURPOSE_RANK[store_purpose]:
                    errors.append(
                        f"{label}.purpose_ceiling: 高于所属 store 上限 "
                        f"{dataset_purpose} > {store_purpose}"
                    )
        if dataset.get("owner_project") not in owner_set:
            errors.append(f"{label}.owner_project: 未登记所有者 {dataset.get('owner_project')!r}")
        require_enum(dataset.get("lifecycle"), LIFECYCLES, f"{label}.lifecycle", errors)
        require_enum(dataset.get("mutability"), MUTABILITIES, f"{label}.mutability", errors)
        require_enum(dataset.get("purpose_ceiling"), PURPOSES, f"{label}.purpose_ceiling", errors)
        require_enum(dataset.get("license_status"), LICENSES, f"{label}.license_status", errors)
        require_enum(dataset.get("quality_status"), QUALITIES, f"{label}.quality_status", errors)
        planned = dataset.get("lifecycle") == "planned"
        validate_path(dataset.get("physical_path"), f"{label}.physical_path", workspace_root, errors, must_exist=not planned)
        member_paths = dataset.get("member_paths", [])
        if not isinstance(member_paths, list):
            errors.append(f"{label}.member_paths: 必须是数组")
        else:
            for member_index, member in enumerate(member_paths):
                validate_path(member, f"{label}.member_paths[{member_index}]", workspace_root, errors, must_exist=not planned)
        authorities = dataset.get("authority_paths")
        if not isinstance(authorities, list) or not authorities:
            errors.append(f"{label}.authority_paths: 必须是非空数组")
        else:
            for authority_index, authority in enumerate(authorities):
                validate_path(authority, f"{label}.authority_paths[{authority_index}]", workspace_root, errors)
        validate_linked_manifests(dataset, label, errors)
        validate_contract_tree_snapshot(catalog, dataset, label, workspace_root, errors)
        if dataset.get("hash_algorithm") == HASH_ALGORITHM:
            errors.extend(
                verify_snapshot_contract(
                    catalog=catalog,
                    item=dataset,
                    snapshot_path=normalized_path(dataset.get("physical_path", "")),
                    deep_payload=True,
                )
            )
        if dataset.get("mutability") == "immutable" and not authorities:
            errors.append(f"{label}: immutable 数据必须有 manifest、provenance 或项目目录作为权威引用")
        consumers = dataset.get("consumers")
        if not isinstance(consumers, list) or not all(isinstance(x, str) for x in consumers):
            errors.append(f"{label}.consumers: 必须是字符串数组")
        else:
            unknown_consumers = sorted(set(consumers) - owner_set)
            if unknown_consumers:
                errors.append(f"{label}.consumers: 未登记消费者 {unknown_consumers}")

    retired_keys: set[tuple[str, str]] = set()
    for index, retired in enumerate(retired_datasets):
        label = f"retired_datasets[{index}]"
        if not isinstance(retired, dict):
            errors.append(f"{label}: 必须是对象")
            continue
        require_fields(retired, RETIRED_DATASET_REQUIRED, label, errors)
        key = (retired.get("dataset_id"), retired.get("snapshot_id"))
        if key in retired_keys:
            errors.append(f"{label}: 重复退役身份 {key}")
        retired_keys.add(key)
        if key in dataset_keys:
            errors.append(f"{label}: 同一身份不能同时处于活动目录和退役目录 {key}")
        if retired.get("owner_project") not in owner_set:
            errors.append(f"{label}.owner_project: 未登记所有者 {retired.get('owner_project')!r}")
        if retired.get("status") != "PAYLOAD_DELETED_METADATA_RETAINED":
            errors.append(f"{label}.status: 非法退役状态 {retired.get('status')!r}")
        former_path = retired.get("former_physical_path")
        validate_path(former_path, f"{label}.former_physical_path", workspace_root, errors, must_exist=False)
        if isinstance(former_path, str) and normalized_path(former_path).exists():
            errors.append(f"{label}.former_physical_path: 已删除数据本体不应仍存在: {former_path}")
        for field in ("deleted_files", "deleted_bytes"):
            value = retired.get(field)
            if not isinstance(value, int) or value < 0:
                errors.append(f"{label}.{field}: 必须是非负整数")
        retained = retired.get("retained_authority_paths")
        if not isinstance(retained, list) or not retained:
            errors.append(f"{label}.retained_authority_paths: 必须是非空数组")
        else:
            for authority_index, authority in enumerate(retained):
                validate_path(authority, f"{label}.retained_authority_paths[{authority_index}]", workspace_root, errors)
        validate_path(retired.get("cleanup_receipt"), f"{label}.cleanup_receipt", workspace_root, errors)

    if isinstance(contracts, list):
        for index, contract in enumerate(contracts):
            label = f"publication_contracts[{index}]"
            if not isinstance(contract, dict):
                errors.append(f"{label}: 必须是对象")
                continue
            require_fields(contract, {"producer", "consumer", "artifact", "status", "requirements"}, label, errors)
            if contract.get("producer") not in owner_set:
                errors.append(f"{label}.producer: 未登记所有者 {contract.get('producer')!r}")
            if contract.get("consumer") not in owner_set:
                errors.append(f"{label}.consumer: 未登记所有者 {contract.get('consumer')!r}")
            if contract.get("status") not in {"blocked", "planned", "active"}:
                errors.append(f"{label}.status: 必须是 blocked、planned 或 active")
            requirements = contract.get("requirements")
            if not isinstance(requirements, list) or not requirements or not all(
                isinstance(item, str) for item in requirements
            ):
                errors.append(f"{label}.requirements: 必须是非空字符串数组")

    return errors


def main() -> int:
    args = parse_args()
    try:
        catalog = load_json(args.catalog.resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"CATALOG INVALID: 无法读取 {args.catalog}: {exc}", file=sys.stderr)
        return 2

    errors = validate_catalog(catalog)
    if errors:
        print(f"CATALOG INVALID: {len(errors)} 个问题", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    immutable = sum(1 for item in catalog["datasets"] if item.get("mutability") == "immutable")
    blocked = sum(1 for item in catalog["publication_contracts"] if item.get("status") == "blocked")
    print(
        "CATALOG PASS: "
        f"stores={len(catalog['stores'])}, datasets={len(catalog['datasets'])}, "
        f"retired={len(catalog.get('retired_datasets', []))}, "
        f"immutable={immutable}, publication_blocked={blocked}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
