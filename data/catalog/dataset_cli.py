#!/usr/bin/env python3
"""Read-only workspace dataset discovery and policy-aware path resolution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from snapshot_contract import (
    HASH_ALGORITHM,
    SnapshotContractError,
    load_json_object as load_strict_json_object,
    verify_snapshot_contract,
)


PURPOSE_RANK = {
    "DO_NOT_USE": 0,
    "ENGINEERING_ONLY": 1,
    "SMOKE_ONLY": 2,
    "RESEARCH_ONLY": 3,
    "SHADOW_ONLY": 4,
    "LIVE_READY": 5,
}
INTENT_RANK = {
    "inspection": 0,
    "engineering": 1,
    "smoke": 2,
    "research": 3,
    "shadow": 4,
    "live": 5,
}
REQUIRES_IMMUTABLE = {"research", "shadow", "live"}


class CatalogError(RuntimeError):
    pass


def default_catalog_path() -> Path:
    override = os.environ.get("QUANT_DATA_CATALOG")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().with_name("datasets.json")


def normalize_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError("路径必须是非空字符串")
    return Path(raw.replace("/", os.sep)).resolve()


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        return load_strict_json_object(path)
    except SnapshotContractError as exc:
        raise CatalogError(str(exc)) from exc

    # Unreachable legacy parser retained temporarily for catalog compatibility review.
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"无法读取 JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogError(f"JSON 根必须是 object: {path}")
    return value


def validate_catalog_shape(catalog: dict[str, Any]) -> None:
    if catalog.get("schema_version") != 1:
        raise CatalogError("不是受支持的实际数据目录：schema_version 必须为 1")
    if catalog.get("catalog_mode") != "federated_logical_catalog":
        raise CatalogError("不是实际数据目录：catalog_mode 必须为 federated_logical_catalog")
    for field in ("workspace_root", "owners", "stores", "datasets", "retired_datasets", "publication_contracts"):
        if field not in catalog:
            raise CatalogError(f"实际数据目录缺少字段: {field}")
    normalize_path(catalog["workspace_root"])
    for field in ("owners", "stores", "datasets", "retired_datasets", "publication_contracts"):
        if not isinstance(catalog[field], list):
            raise CatalogError(f"实际数据目录字段必须是数组: {field}")
    for index, item in enumerate(catalog["datasets"]):
        if not isinstance(item, dict):
            raise CatalogError(f"datasets[{index}] 必须是 object")
        for field in ("dataset_id", "snapshot_id", "physical_path"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise CatalogError(f"datasets[{index}].{field} 必须是非空字符串")
        normalize_path(item["physical_path"])


def load_catalog(path: Path) -> dict[str, Any]:
    catalog = load_json_object(path)
    validate_catalog_shape(catalog)
    return catalog


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_contract_tree_snapshot(
    catalog: dict[str, Any],
    item: dict[str, Any],
    snapshot_path: Path,
) -> list[str]:
    """Deep-check a D12A directory snapshot and every referenced payload file."""
    errors: list[str] = []
    label = f"{item.get('dataset_id')}@{item.get('snapshot_id')}"
    required = {
        "manifest.json": snapshot_path / "manifest.json",
        "provenance.json": snapshot_path / "provenance.json",
        "partition-manifest.json": snapshot_path / "partition-manifest.json",
        "quality/summary.json": snapshot_path / "quality" / "summary.json",
    }
    documents: dict[str, dict[str, Any]] = {}
    for name, path in required.items():
        if not path.is_file():
            errors.append(f"{label}: 缺少 {name}")
            continue
        try:
            documents[name] = load_json_object(path)
        except CatalogError as exc:
            errors.append(f"{label}: {name} 损坏: {exc}")
    if len(documents) != len(required):
        return errors

    manifest = documents["manifest.json"]
    provenance = documents["provenance.json"]
    partitions = documents["partition-manifest.json"]
    for name, document in (
        ("manifest", manifest),
        ("provenance", provenance),
        ("partition-manifest", partitions),
    ):
        if document.get("dataset_id") != item.get("dataset_id"):
            errors.append(f"{label}: {name}.dataset_id 不一致")
        if document.get("snapshot_id") != item.get("snapshot_id"):
            errors.append(f"{label}: {name}.snapshot_id 不一致")
    try:
        if normalize_path(manifest.get("root_path")) != snapshot_path.resolve():
            errors.append(f"{label}: manifest.root_path 不一致")
    except CatalogError as exc:
        errors.append(f"{label}: manifest.root_path 无效: {exc}")
    expected_content_hash = str(item.get("content_hash", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", expected_content_hash):
        errors.append(f"{label}: catalog content_hash 不是小写 SHA-256")
    if manifest.get("content_hash") != expected_content_hash:
        errors.append(f"{label}: manifest content_hash 与 catalog 不一致")

    files = partitions.get("files")
    if not isinstance(files, list) or not files:
        errors.append(f"{label}: partition-manifest.files 必须是非空数组")
        return errors
    workspace_root = normalize_path(catalog["workspace_root"])
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
        if not isinstance(expected_hash, str) or sha256_path(candidate) != expected_hash:
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
        "dataset_id": item.get("dataset_id"),
        "snapshot_id": item.get("snapshot_id"),
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
    return errors


def dataset_key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("dataset_id", "")), str(item.get("snapshot_id", ""))


def find_dataset(catalog: dict[str, Any], dataset_id: str, snapshot_id: str) -> dict[str, Any]:
    matches = [
        item
        for item in catalog.get("datasets", [])
        if dataset_key(item) == (dataset_id, snapshot_id)
    ]
    if not matches:
        available = sorted(
            item.get("snapshot_id", "")
            for item in catalog.get("datasets", [])
            if item.get("dataset_id") == dataset_id
        )
        suffix = f"；已登记快照: {', '.join(available)}" if available else ""
        raise CatalogError(f"未登记数据身份 {dataset_id}@{snapshot_id}{suffix}")
    if len(matches) != 1:
        raise CatalogError(f"目录存在重复身份 {dataset_id}@{snapshot_id}")
    return matches[0]


def check_access(
    catalog: dict[str, Any],
    item: dict[str, Any],
    intent: str,
    consumer: str | None,
    *,
    now_utc: datetime | None = None,
) -> None:
    ceiling = item.get("purpose_ceiling")
    if ceiling not in PURPOSE_RANK:
        raise CatalogError(f"未知用途上限: {ceiling!r}")
    if PURPOSE_RANK[ceiling] < INTENT_RANK[intent]:
        raise CatalogError(f"用途被拒绝: {intent} 需要更高权限，登记上限为 {ceiling}")
    if intent != "inspection" and ceiling == "DO_NOT_USE":
        raise CatalogError("DO_NOT_USE 只允许 inspection 审计读取")
    if intent in REQUIRES_IMMUTABLE and item.get("mutability") != "immutable":
        raise CatalogError(f"{intent} 只接受 immutable 快照，当前为 {item.get('mutability')}")
    if intent in REQUIRES_IMMUTABLE and "latest" in str(item.get("snapshot_id", "")).lower():
        raise CatalogError(f"{intent} 禁止 latest")
    if intent in REQUIRES_IMMUTABLE:
        raw_available_at = item.get("available_at")
        if not isinstance(raw_available_at, str) or not raw_available_at.strip():
            raise CatalogError(
                f"{intent} requires a timezone-aware available_at timestamp"
            )
        try:
            available_at = datetime.fromisoformat(
                raw_available_at.replace("Z", "+00:00").replace("z", "+00:00")
            )
        except ValueError as exc:
            raise CatalogError(
                f"{intent} available_at must be RFC 3339"
            ) from exc
        if available_at.tzinfo is None or available_at.utcoffset() is None:
            raise CatalogError(
                f"{intent} available_at must include an explicit UTC offset"
            )
        reference = now_utc or datetime.now(timezone.utc)
        if reference.tzinfo is None or reference.utcoffset() is None:
            raise CatalogError("research access clock must be timezone-aware")
        if available_at.astimezone(timezone.utc) > reference.astimezone(timezone.utc):
            raise CatalogError(
                f"{intent} snapshot is not available until {available_at.isoformat()}"
            )
    if consumer:
        owners = set(catalog.get("owners", []))
        if consumer not in owners:
            raise CatalogError(f"未登记消费者: {consumer}")
        allowed = set(item.get("consumers", []))
        if consumer != item.get("owner_project") and consumer not in allowed:
            raise CatalogError(f"消费者 {consumer} 未获准读取该数据用于 {intent}")


def enriched(item: dict[str, Any]) -> dict[str, Any]:
    value = dict(item)
    path = normalize_path(str(item.get("physical_path", "")))
    value["resolved_path"] = str(path)
    value["path_exists"] = path.exists()
    value["authority_status"] = [
        {"path": str(normalize_path(raw)), "exists": normalize_path(raw).exists()}
        for raw in item.get("authority_paths", [])
    ]
    return value


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def command_list(args: argparse.Namespace, catalog: dict[str, Any]) -> int:
    rows = []
    for item in catalog.get("datasets", []):
        if args.consumer:
            allowed = set(item.get("consumers", []))
            if args.consumer != item.get("owner_project") and args.consumer not in allowed:
                continue
        rows.append(
            {
                "dataset_id": item.get("dataset_id"),
                "snapshot_id": item.get("snapshot_id"),
                "owner_project": item.get("owner_project"),
                "lifecycle": item.get("lifecycle"),
                "mutability": item.get("mutability"),
                "purpose_ceiling": item.get("purpose_ceiling"),
                "quality_status": item.get("quality_status"),
                "path_exists": normalize_path(str(item.get("physical_path", ""))).exists(),
            }
        )
    if args.json:
        print_json(rows)
    else:
        for row in rows:
            identity = f"{row['dataset_id']}@{row['snapshot_id']}"
            print(
                f"{identity}\t{row['purpose_ceiling']}\t{row['quality_status']}\t"
                f"{row['mutability']}\towner={row['owner_project']}\texists={row['path_exists']}"
            )
    return 0


def command_describe(args: argparse.Namespace, catalog: dict[str, Any]) -> int:
    item = find_dataset(catalog, args.dataset_id, args.snapshot_id)
    print_json(enriched(item))
    return 0


def command_resolve(args: argparse.Namespace, catalog: dict[str, Any]) -> int:
    item = find_dataset(catalog, args.dataset_id, args.snapshot_id)
    check_access(catalog, item, args.intent, args.consumer)
    path = normalize_path(str(item.get("physical_path", "")))
    if not path.exists():
        raise CatalogError(f"登记路径不存在: {path}")
    if item.get("hash_algorithm") == "sha256-contract-tree-v1":
        deep_errors = verify_contract_tree_snapshot(catalog, item, path)
        if deep_errors:
            raise CatalogError("目录快照完整性失败: " + " | ".join(deep_errors))
    elif item.get("hash_algorithm") == HASH_ALGORITHM:
        metadata_errors = verify_snapshot_contract(
            catalog=catalog,
            item=item,
            snapshot_path=path,
            deep_payload=False,
        )
        if metadata_errors:
            raise CatalogError("目录快照完整性失败: " + " | ".join(metadata_errors))
    if item.get("quality_status") != "OK":
        print(
            f"DATA ACCESS WARNING: quality_status={item.get('quality_status')}，"
            "解析成功不等于质量或阶段门通过",
            file=sys.stderr,
        )
    if args.json:
        value = enriched(item)
        value["requested_intent"] = args.intent
        value["requested_consumer"] = args.consumer
        print_json(value)
    else:
        print(path)
    return 0


def command_stores(args: argparse.Namespace, catalog: dict[str, Any]) -> int:
    rows = []
    for store in catalog.get("stores", []):
        value = dict(store)
        path = normalize_path(str(store.get("physical_root", "")))
        value["resolved_root"] = str(path)
        value["path_exists"] = path.exists()
        rows.append(value)
    if args.json:
        print_json(rows)
    else:
        for row in rows:
            print(
                f"{row.get('store_id')}\t{row.get('kind')}\t{row.get('purpose_ceiling')}\t"
                f"exists={row.get('path_exists')}\t{row.get('resolved_root')}"
            )
    return 0


def command_doctor(_args: argparse.Namespace, catalog: dict[str, Any]) -> int:
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    store_ids = {store.get("store_id") for store in catalog.get("stores", [])}
    for item in catalog.get("datasets", []):
        key = dataset_key(item)
        label = f"{key[0]}@{key[1]}"
        if key in seen:
            errors.append(f"重复身份: {label}")
        seen.add(key)
        if item.get("store_id") not in store_ids:
            errors.append(f"未登记 store: {label} -> {item.get('store_id')}")
        path = normalize_path(str(item.get("physical_path", "")))
        if not path.exists() and item.get("lifecycle") != "planned":
            errors.append(f"物理路径不存在: {label} -> {path}")
        for raw in item.get("authority_paths", []):
            authority = normalize_path(raw)
            if not authority.exists():
                errors.append(f"authority 不存在: {label} -> {authority}")
        if item.get("hash_algorithm") == "sha256-file-v1" and path.is_file():
            expected = str(item.get("content_hash", "")).lower()
            if sha256_path(path).lower() != expected:
                errors.append(f"文件哈希不一致: {label}")
        if item.get("hash_algorithm") == "sha256-contract-tree-v1" and path.is_dir():
            errors.extend(verify_contract_tree_snapshot(catalog, item, path))
        elif item.get("hash_algorithm") == HASH_ALGORITHM and path.is_dir():
            errors.extend(
                verify_snapshot_contract(
                    catalog=catalog,
                    item=item,
                    snapshot_path=path,
                    deep_payload=True,
                )
            )
        if item.get("quality_status") != "OK":
            warnings.append(f"质量未通过: {label} = {item.get('quality_status')}")
    print(f"DATASET DOCTOR: datasets={len(seen)}, errors={len(errors)}, warnings={len(warnings)}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quantification 统一数据只读解析器")
    parser.add_argument("--catalog", type=Path, default=default_catalog_path())
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="列出登记数据")
    list_parser.add_argument("--consumer")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=command_list)

    describe_parser = subparsers.add_parser("describe", help="查看完整登记和路径状态")
    describe_parser.add_argument("dataset_id")
    describe_parser.add_argument("snapshot_id")
    describe_parser.set_defaults(handler=command_describe)

    resolve_parser = subparsers.add_parser("resolve", help="按意图解析精确物理路径")
    resolve_parser.add_argument("dataset_id")
    resolve_parser.add_argument("snapshot_id")
    resolve_parser.add_argument("--intent", choices=sorted(INTENT_RANK), default="inspection")
    resolve_parser.add_argument("--consumer")
    resolve_parser.add_argument("--json", action="store_true")
    resolve_parser.set_defaults(handler=command_resolve)

    stores_parser = subparsers.add_parser("stores", help="列出存储区")
    stores_parser.add_argument("--json", action="store_true")
    stores_parser.set_defaults(handler=command_stores)

    doctor_parser = subparsers.add_parser("doctor", help="检查身份、路径和 authority")
    doctor_parser.set_defaults(handler=command_doctor)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        catalog = load_catalog(args.catalog.resolve())
        return int(args.handler(args, catalog))
    except CatalogError as exc:
        print(f"DATA ACCESS DENIED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
