#!/usr/bin/env python3
"""Shared verifier for immutable workspace snapshot contracts.

This module is deliberately standard-library only.  It is used by the D12A
publisher, the catalog validator, and qdata so that publication and discovery
enforce the same identity and integrity rules.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


HASH_ALGORITHM = "sha256-snapshot-contract-v1"
AUTHORITY_RELATIVE_PATHS = (
    "request.json",
    "manifest.json",
    "provenance.json",
    "partition-manifest.json",
    "quality/summary.json",
)


class SnapshotContractError(RuntimeError):
    """Raised when a contract JSON document cannot be read safely."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SnapshotContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    raise SnapshotContractError(f"non-finite JSON number is forbidden: {value}")


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
    except (OSError, json.JSONDecodeError, SnapshotContractError) as exc:
        raise SnapshotContractError(f"cannot read strict JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SnapshotContractError(f"JSON root must be an object: {path}")
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_contract_hash(request_contract: dict[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(request_contract))


def snapshot_contract_value(
    request_contract: dict[str, Any],
    manifest: dict[str, Any],
    provenance: dict[str, Any],
    partition_manifest: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    manifest_contract = dict(manifest)
    manifest_contract.pop("content_hash", None)
    return {
        "hash_algorithm": HASH_ALGORITHM,
        "request": request_contract,
        "manifest": manifest_contract,
        "provenance": provenance,
        "partition_manifest": partition_manifest,
        "quality": quality,
    }


def snapshot_contract_hash(
    request_contract: dict[str, Any],
    manifest: dict[str, Any],
    provenance: dict[str, Any],
    partition_manifest: dict[str, Any],
    quality: dict[str, Any],
) -> str:
    return sha256_bytes(
        canonical_bytes(
            snapshot_contract_value(
                request_contract,
                manifest,
                provenance,
                partition_manifest,
                quality,
            )
        )
    )


def is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def _safe_relative_path(raw: Any, label: str, errors: list[str]) -> Path | None:
    if not isinstance(raw, str) or not raw:
        errors.append(f"{label}: path must be a non-empty string")
        return None
    relative = Path(raw.replace("/", os.sep))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        errors.append(f"{label}: path must be a normalized workspace-relative path: {raw!r}")
        return None
    return relative


def _contained_path(
    workspace_root: Path,
    relative: Path,
    label: str,
    errors: list[str],
) -> Path | None:
    lexical_root = Path(os.path.abspath(workspace_root))
    lexical = Path(os.path.abspath(lexical_root / relative))
    try:
        if Path(os.path.commonpath([lexical_root, lexical])) != lexical_root:
            errors.append(f"{label}: path escapes workspace")
            return None
    except ValueError:
        errors.append(f"{label}: path is on a different volume")
        return None
    current = lexical_root
    for part in relative.parts:
        current /= part
        if current.exists() and is_reparse_point(current):
            errors.append(f"{label}: symlink/junction/reparse point is forbidden: {current}")
            return None
    resolved_root = lexical_root.resolve()
    resolved = lexical.resolve(strict=False)
    try:
        if Path(os.path.commonpath([resolved_root, resolved])) != resolved_root:
            errors.append(f"{label}: resolved path escapes workspace")
            return None
    except ValueError:
        errors.append(f"{label}: resolved path is on a different volume")
        return None
    return lexical


def _payload_identity(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": entry.get("layer"),
        "relative_path": entry.get("relative_path"),
        "bytes": entry.get("bytes"),
        "sha256": entry.get("sha256"),
    }


def verify_snapshot_contract(
    *,
    catalog: dict[str, Any],
    item: dict[str, Any],
    snapshot_path: Path,
    deep_payload: bool,
) -> list[str]:
    """Verify one directory snapshot; return all detected errors."""
    errors: list[str] = []
    label = f"{item.get('dataset_id')}@{item.get('snapshot_id')}"
    try:
        workspace_root = Path(str(catalog["workspace_root"]).replace("/", os.sep)).resolve()
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return [f"{label}: invalid catalog workspace_root: {exc}"]
    snapshot_path = snapshot_path.resolve(strict=False)
    try:
        if Path(os.path.commonpath([workspace_root, snapshot_path])) != workspace_root:
            return [f"{label}: snapshot path escapes workspace"]
    except ValueError:
        return [f"{label}: snapshot path is on a different volume"]
    if not snapshot_path.is_dir():
        return [f"{label}: snapshot directory does not exist: {snapshot_path}"]

    documents: dict[str, dict[str, Any]] = {}
    required_paths = {
        relative: snapshot_path / Path(relative)
        for relative in AUTHORITY_RELATIVE_PATHS
    }
    expected_authorities = {path.resolve(strict=False) for path in required_paths.values()}
    actual_authorities: set[Path] = set()
    for raw in item.get("authority_paths", []):
        if isinstance(raw, str) and raw:
            actual_authorities.add(Path(raw.replace("/", os.sep)).resolve(strict=False))
    if actual_authorities != expected_authorities:
        errors.append(f"{label}: authority_paths must be exactly the five snapshot contract documents")
    for relative, path in required_paths.items():
        if not path.is_file():
            errors.append(f"{label}: missing {relative}")
            continue
        try:
            documents[relative] = load_json_object(path)
        except SnapshotContractError as exc:
            errors.append(f"{label}: invalid {relative}: {exc}")
    if len(documents) != len(required_paths):
        return errors

    request = documents["request.json"]
    manifest = documents["manifest.json"]
    provenance = documents["provenance.json"]
    partitions = documents["partition-manifest.json"]
    quality = documents["quality/summary.json"]
    expected_dataset = item.get("dataset_id")
    expected_snapshot = item.get("snapshot_id")
    for name, document in documents.items():
        if document.get("dataset_id") != expected_dataset:
            errors.append(f"{label}: {name}.dataset_id mismatch")
        if document.get("snapshot_id") != expected_snapshot:
            errors.append(f"{label}: {name}.snapshot_id mismatch")

    expected_root = str(snapshot_path).replace("\\", "/")
    if str(manifest.get("root_path", "")).replace("\\", "/") != expected_root:
        errors.append(f"{label}: manifest.root_path mismatch")
    if manifest.get("hash_algorithm") != HASH_ALGORITHM:
        errors.append(f"{label}: unsupported manifest hash_algorithm")
    if item.get("hash_algorithm") != HASH_ALGORITHM:
        errors.append(f"{label}: catalog hash_algorithm mismatch")

    catalog_bindings = {
        "store_id": item.get("store_id"),
        "owner_project": item.get("owner_project"),
        "purpose_ceiling": item.get("purpose_ceiling"),
        "license_status": item.get("license_status"),
        "quality_status": item.get("quality_status"),
        "consumers": item.get("consumers"),
    }
    for field, catalog_value in catalog_bindings.items():
        if request.get(field) != catalog_value:
            errors.append(f"{label}: catalog {field} is not bound to request.json")
    if item.get("mutability") != "immutable":
        errors.append(f"{label}: contract snapshot must be immutable")

    request_hash = request_contract_hash(request)
    if provenance.get("request_contract_hash") != request_hash:
        errors.append(f"{label}: provenance request_contract_hash mismatch")

    files = partitions.get("files")
    if not isinstance(files, list) or not files:
        errors.append(f"{label}: partition-manifest.files must be a non-empty array")
        return errors
    payload_entries: list[dict[str, Any]] = []
    seen_workspace_paths: set[str] = set()
    seen_layer_paths: set[tuple[str, str]] = set()
    layer_actual: dict[str, set[str]] = {"raw": set(), "curated": set()}
    layer_roots: dict[str, Path] = {}
    for layer, field in (("raw", "raw_root"), ("curated", "curated_root")):
        relative = _safe_relative_path(manifest.get(field), f"{label}: manifest.{field}", errors)
        if relative is not None:
            contained = _contained_path(workspace_root, relative, f"{label}: manifest.{field}", errors)
            if contained is not None:
                layer_roots[layer] = contained

    for index, entry in enumerate(files):
        entry_label = f"{label}: partition files[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{entry_label}: must be an object")
            continue
        layer = entry.get("layer")
        if layer not in {"raw", "curated"}:
            errors.append(f"{entry_label}: invalid layer")
            continue
        relative = _safe_relative_path(entry.get("relative_path"), f"{entry_label}.relative_path", errors)
        workspace_relative = _safe_relative_path(
            entry.get("workspace_relative_path"),
            f"{entry_label}.workspace_relative_path",
            errors,
        )
        if relative is None or workspace_relative is None or layer not in layer_roots:
            continue
        relative_text = relative.as_posix()
        workspace_text = workspace_relative.as_posix()
        folded_workspace = workspace_text.casefold()
        folded_identity = (str(layer).casefold(), relative_text.casefold())
        if folded_workspace in seen_workspace_paths:
            errors.append(f"{entry_label}: duplicate/case-folded workspace path")
        if folded_identity in seen_layer_paths:
            errors.append(f"{entry_label}: duplicate/case-folded layer path")
        seen_workspace_paths.add(folded_workspace)
        seen_layer_paths.add(folded_identity)
        expected_path = layer_roots[layer] / relative
        actual_path = _contained_path(workspace_root, workspace_relative, entry_label, errors)
        if actual_path is None:
            continue
        if actual_path.resolve(strict=False) != expected_path.resolve(strict=False):
            errors.append(f"{entry_label}: workspace path does not match layer root")
            continue
        expected_bytes = entry.get("bytes")
        expected_sha = entry.get("sha256")
        if not isinstance(expected_bytes, int) or expected_bytes < 0:
            errors.append(f"{entry_label}: bytes must be a non-negative integer")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            errors.append(f"{entry_label}: sha256 must be a lowercase digest")
        payload_entries.append(_payload_identity(entry))
        layer_actual[layer].add(relative_text.casefold())
        if deep_payload:
            if not actual_path.is_file():
                errors.append(f"{entry_label}: payload file is missing")
            else:
                if actual_path.stat().st_size != expected_bytes:
                    errors.append(f"{entry_label}: byte size mismatch")
                if isinstance(expected_sha, str) and sha256_file(actual_path) != expected_sha:
                    errors.append(f"{entry_label}: file hash mismatch")

    payload_entries.sort(key=lambda value: (str(value["layer"]), str(value["relative_path"])))
    payload_hash = sha256_bytes(canonical_bytes(payload_entries))
    if partitions.get("payload_hash") != payload_hash:
        errors.append(f"{label}: partition payload_hash mismatch")
    if manifest.get("payload_hash") != payload_hash:
        errors.append(f"{label}: manifest payload_hash mismatch")
    if provenance.get("payload_hash") != payload_hash:
        errors.append(f"{label}: provenance payload_hash mismatch")

    if deep_payload:
        for layer, root in layer_roots.items():
            if not root.is_dir():
                errors.append(f"{label}: {layer} root is missing")
                continue
            discovered: set[str] = set()
            for path in root.rglob("*"):
                if is_reparse_point(path):
                    errors.append(f"{label}: reparse point under {layer} root: {path}")
                    continue
                if path.is_file():
                    discovered.add(path.relative_to(root).as_posix().casefold())
            if discovered != layer_actual[layer]:
                errors.append(f"{label}: {layer} file set differs from partition manifest")

    expected_content_hash = snapshot_contract_hash(request, manifest, provenance, partitions, quality)
    if manifest.get("content_hash") != expected_content_hash:
        errors.append(f"{label}: manifest content_hash cannot be reconstructed")
    if item.get("content_hash") != expected_content_hash:
        errors.append(f"{label}: catalog content_hash cannot be reconstructed")
    return errors
