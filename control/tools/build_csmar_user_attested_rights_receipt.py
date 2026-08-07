#!/usr/bin/env python3
"""Build a deterministic, fail-closed CSMAR user-attested rights receipt.

The receipt is deliberately narrow:

* it covers only exact source aliases and sealed canonical blobs named by the
  three reviewed CSMAR manual-export intake manifests;
* it verifies every canonical blob against manifest bytes and SHA-256;
* it records a user attestation, not an independent provider/institution
  contract review or legal opinion;
* it does not modify the data catalog, source registry, Roadmap, project
  registry, formal snapshots, published data, quality state, or admission
  state.

No wall-clock value is written.  Given byte-identical sealed inputs and
unchanged constants, the four report artifacts are byte-identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


RUN_ID = "csmar-user-attested-rights-receipt-20260730-v1"
ATTESTATION_DATE = "2026-07-30"
DECLARATION = (
    "USER_ATTESTED__LOCAL_RETENTION__INTERNAL_ACADEMIC_DERIVATION__"
    "NO_RAW_EXTERNAL_REDISTRIBUTION__NO_COMMERCIAL_USE"
)
DEFAULT_REPORT_RELATIVE = (
    "reports/data-governance/"
    "csmar-user-attested-rights-receipt-20260730-v1"
)
OUTPUT_FILES = {
    "README.md",
    "REPORT_MANIFEST.json",
    "RIGHTS_RECEIPT.json",
    "VERIFICATION.json",
}

DEFAULT_BATCH_SPECS: tuple[dict[str, str], ...] = (
    {
        "run_id": "intake-20260729-33pkg-v1",
        "manifest_path": (
            "data/market/quarantine/csmar-manual-export/"
            "intake-20260729-33pkg-v1/INTAKE_MANIFEST.json"
        ),
        "manifest_sha256": (
            "c156608a8d1087b2cd58baf39ccbe04c"
            "5278fa9e33b7800234f90eea4f17e8c2"
        ),
    },
    {
        "run_id": "intake-20260730-08pkg-03manual-v1",
        "manifest_path": (
            "data/market/quarantine/csmar-manual-export/"
            "intake-20260730-08pkg-03manual-v1/INTAKE_MANIFEST.json"
        ),
        "manifest_sha256": (
            "9d4785ffd243cecb4c122f63984168db"
            "143ab8385f1e7c6169bd91a7faddb149"
        ),
    },
    {
        "run_id": "intake-20260730-18src-12blob-idx-idxtrd-v1",
        "manifest_path": (
            "data/market/quarantine/csmar-manual-export/"
            "intake-20260730-18src-12blob-idx-idxtrd-v1/"
            "INTAKE_MANIFEST.json"
        ),
        "manifest_sha256": (
            "10439a5e5857fd9cf5c00c36aa2cd3d"
            "5fcfaac52bd11a67efff4e31f451eff23"
        ),
    },
)

ALLOWED_SCOPE: tuple[dict[str, str], ...] = (
    {
        "code": "LOCAL_LONG_TERM_RETENTION_ON_THIS_MACHINE",
        "statement_zh": "允许在本机长期保存本回执精确绑定的封存资产。",
    },
    {
        "code": "CAMPUS_INTERNAL_NONCOMMERCIAL_ACADEMIC_RESEARCH",
        "statement_zh": "允许在校内内部开展非商业学术研究。",
    },
    {
        "code": "INTERNAL_DERIVED_FACTORS_AND_STATISTICS",
        "statement_zh": "允许在内部形成派生因子和统计结果。",
    },
)
PROHIBITED_SCOPE: tuple[dict[str, str], ...] = (
    {
        "code": "RAW_EXTERNAL_REDISTRIBUTION",
        "statement_zh": "禁止将原始数据或原始封存包向外部传播、转交或再分发。",
    },
    {
        "code": "COMMERCIAL_USE",
        "statement_zh": "禁止商业用途。",
    },
)
NOT_COVERED_SCOPE: tuple[dict[str, str], ...] = (
    {
        "code": "API_OR_AUTOMATED_BULK_COLLECTION",
        "statement_zh": "不覆盖 API、自动登录、自动化批量采集或整库抓取权利。",
    },
    {
        "code": "POST_AFFILIATION_DISPOSITION",
        "statement_zh": "不覆盖离校、离职或机构关系终止后的保留、迁移或删除安排。",
    },
    {
        "code": "OFFICIAL_SOURCE_RIGHTS",
        "statement_zh": "不覆盖交易所、法定披露平台或其他官方来源的获取、留存或再利用权利。",
    },
    {
        "code": "INDEPENDENT_CONTRACT_VERIFICATION",
        "statement_zh": "不构成对供应商、学校或机构合同条款的独立核验，也不是法律意见。",
    },
)

GUARDED_FILES = (
    "data/catalog/datasets.json",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    "control/ROADMAP.md",
    "control/projects.yaml",
)
GUARDED_TREES = (
    "data/market/snapshots",
    "data/published",
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the stable JSON representation used for content identities."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_reparse(path: Path) -> bool:
    attrs = getattr(path.lstat(), "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attrs & flag)


def require_plain_existing_path(path: Path, stop: Path) -> None:
    """Reject links/reparse points from ``stop`` through ``path``."""

    # Use lexical absolute paths here.  Resolving first would erase the very
    # link/reparse component this function is required to detect.
    resolved_stop = Path(os.path.abspath(os.fspath(stop)))
    resolved_path = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = resolved_path.relative_to(resolved_stop)
    except ValueError as exc:
        raise ValueError(f"path escapes reviewed root: {path}") from exc
    current = resolved_stop
    if is_reparse(current):
        raise RuntimeError(f"reviewed root is a link/reparse point: {current}")
    for part in relative.parts:
        current = current / part
        if not current.exists():
            raise FileNotFoundError(current)
        if is_reparse(current):
            raise RuntimeError(f"link/reparse point is not allowed: {current}")


def workspace_relative(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {path}") from exc


def resolve_workspace_relative(workspace: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"invalid canonical workspace-relative path: {relative!r}")
    path = workspace.joinpath(*pure.parts)
    workspace_relative(path, workspace)
    return path


def resolve_batch_relative(batch_root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"invalid canonical batch-relative path: {relative!r}")
    path = batch_root.joinpath(*pure.parts)
    try:
        path.resolve().relative_to(batch_root.resolve())
    except ValueError as exc:
        raise ValueError(f"canonical blob escapes batch root: {relative!r}") from exc
    return path


def require_leaf_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or "\\" in value:
        raise ValueError(f"{label} must be one exact non-empty leaf name")
    return value


def require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} is not a lowercase SHA-256")
    return value


def require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} is not a nonnegative integer")
    return value


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_file_binding(
    path: Path, workspace: Path
) -> tuple[dict[str, Any], tuple[int, int, int]]:
    if not path.is_file() or is_reparse(path):
        raise RuntimeError(f"expected a plain file: {path}")
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    before_token = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_token = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if before_token != after_token:
        raise RuntimeError(f"file changed while hashing: {path}")
    return (
        {
            "path": workspace_relative(path, workspace),
            "bytes": after.st_size,
            "sha256": digest,
        },
        after_token,
    )


def assert_file_token(path: Path, token: tuple[int, int, int]) -> None:
    current = path.stat()
    observed = (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
    if observed != token:
        raise RuntimeError(f"sealed input changed during receipt generation: {path}")


def tree_guard(root: Path, workspace: Path) -> dict[str, Any]:
    if not root.exists():
        return {
            "path": workspace_relative(root, workspace),
            "exists": False,
            "file_count": 0,
            "total_bytes": 0,
            "tree_sha256": sha256_bytes(b"[]"),
        }
    require_plain_existing_path(root, workspace)
    if not root.is_dir():
        raise RuntimeError(f"guarded tree is not a directory: {root}")
    files: list[dict[str, Any]] = []
    for item in sorted(root.rglob("*")):
        if is_reparse(item):
            raise RuntimeError(f"guarded tree contains a link/reparse point: {item}")
        if item.is_file():
            binding, _ = stable_file_binding(item, workspace)
            files.append(binding)
    return {
        "path": workspace_relative(root, workspace),
        "exists": True,
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "tree_sha256": sha256_bytes(canonical_json_bytes(files)),
    }


def formal_guard_state(workspace: Path) -> dict[str, Any]:
    return {
        "files": {
            relative: stable_file_binding(workspace / relative, workspace)[0]
            for relative in GUARDED_FILES
        },
        "trees": {
            relative: tree_guard(workspace / relative, workspace)
            for relative in GUARDED_TREES
        },
    }


def validate_common_manifest(
    manifest: dict[str, Any], expected_run_id: str
) -> None:
    if manifest.get("schema_version") != 1:
        raise ValueError(f"{expected_run_id}: unsupported schema_version")
    if manifest.get("run_id") != expected_run_id:
        raise ValueError(f"{expected_run_id}: manifest run_id mismatch")
    expected = {
        "lifecycle": "quarantine_intake",
        "mutability": "immutable_after_seal",
        "purpose_ceiling": "INSPECTION_ONLY",
        "admission_decision": "NOT_ADMITTED",
        "catalog_registered": False,
        "research_ready": False,
        "hash_algorithm": "sha256",
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(
                f"{expected_run_id}: {key} must remain {value!r}, "
                f"observed {manifest.get(key)!r}"
            )


def canonical_blob_binding(
    *,
    workspace: Path,
    batch_root: Path,
    run_id: str,
    blob_index: int,
    row: dict[str, Any],
    source_aliases: list[str],
    stable_tokens: list[tuple[Path, tuple[int, int, int]]],
) -> dict[str, Any]:
    sha256 = require_sha256(row.get("sha256"), f"{run_id} blob SHA-256")
    byte_count = require_nonnegative_int(
        row.get("bytes"), f"{run_id} blob bytes"
    )
    canonical_filename = require_leaf_name(
        row.get("canonical_filename"), f"{run_id} canonical_filename"
    )
    relative = row.get("destination_relative_path")
    if not isinstance(relative, str):
        raise ValueError(f"{run_id}: destination_relative_path must be text")
    pure = PurePosixPath(relative)
    if pure.name != canonical_filename:
        raise ValueError(
            f"{run_id}: canonical filename/path mismatch: {canonical_filename!r}"
        )
    path = resolve_batch_relative(batch_root, relative)
    require_plain_existing_path(path, workspace)
    binding, token = stable_file_binding(path, workspace)
    stable_tokens.append((path, token))
    if binding["bytes"] != byte_count or binding["sha256"] != sha256:
        raise RuntimeError(
            f"{run_id}: canonical blob bytes/SHA-256 mismatch: {binding['path']}"
        )
    if "copy_hash_verification" in row and row["copy_hash_verification"] != "PASS":
        raise ValueError(f"{run_id}: copy_hash_verification is not PASS")
    return {
        "canonical_blob_id": f"{run_id}::blob::{blob_index:04d}",
        "batch_run_id": run_id,
        "blob_ordinal": blob_index,
        "kind": row.get("kind", "package"),
        "canonical_filename": canonical_filename,
        "canonical_path": binding["path"],
        "bytes": byte_count,
        "sha256": sha256,
        "source_aliases": source_aliases,
        "source_alias_count": len(source_aliases),
        "manifest_to_payload_bytes_sha256_verified": True,
    }


def parse_legacy_packages(
    *,
    workspace: Path,
    batch_root: Path,
    run_id: str,
    manifest: dict[str, Any],
    stable_tokens: list[tuple[Path, tuple[int, int, int]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    packages = manifest.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ValueError(f"{run_id}: packages must be a non-empty list")
    if manifest.get("package_count") != len(packages):
        raise ValueError(f"{run_id}: package_count mismatch")

    aliases: list[dict[str, Any]] = []
    blobs: list[dict[str, Any]] = []
    seen_aliases: set[str] = set()
    seen_hashes: set[str] = set()
    seen_paths: set[str] = set()
    for index, package in enumerate(packages, start=1):
        if not isinstance(package, dict):
            raise ValueError(f"{run_id}: package row {index} is not an object")
        filename = require_leaf_name(
            package.get("filename"), f"{run_id} package filename"
        )
        if filename in seen_aliases:
            raise ValueError(f"{run_id}: duplicate exact source alias: {filename!r}")
        seen_aliases.add(filename)
        row = dict(package)
        row["canonical_filename"] = filename
        row["kind"] = "package"
        blob = canonical_blob_binding(
            workspace=workspace,
            batch_root=batch_root,
            run_id=run_id,
            blob_index=index,
            row=row,
            source_aliases=[filename],
            stable_tokens=stable_tokens,
        )
        if blob["sha256"] in seen_hashes:
            raise ValueError(f"{run_id}: duplicate canonical blob SHA-256")
        if blob["canonical_path"] in seen_paths:
            raise ValueError(f"{run_id}: duplicate canonical blob path")
        seen_hashes.add(blob["sha256"])
        seen_paths.add(blob["canonical_path"])
        blobs.append(blob)
        source_path = package.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            raise ValueError(f"{run_id}: source_path must be non-empty text")
        aliases.append(
            {
                "source_alias_id": f"{run_id}::alias::{index:04d}",
                "batch_run_id": run_id,
                "source_ordinal": index,
                "source_alias": filename,
                "source_path_recorded": source_path,
                "kind": "package",
                "bytes": blob["bytes"],
                "sha256": blob["sha256"],
                "canonical_blob_id": blob["canonical_blob_id"],
                "canonical_path": blob["canonical_path"],
                "canonical_bytes": blob["bytes"],
                "canonical_sha256": blob["sha256"],
                "alias_to_canonical_bytes_sha256_match": True,
            }
        )
    observed_total = sum(row["bytes"] for row in aliases)
    if manifest.get("total_bytes") != observed_total:
        raise ValueError(f"{run_id}: total_bytes mismatch")
    return aliases, blobs


def parse_deduplicated_manifest(
    *,
    workspace: Path,
    batch_root: Path,
    run_id: str,
    manifest: dict[str, Any],
    stable_tokens: list[tuple[Path, tuple[int, int, int]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_files = manifest.get("source_files")
    unique_blobs = manifest.get("unique_blobs")
    if not isinstance(source_files, list) or not source_files:
        raise ValueError(f"{run_id}: source_files must be a non-empty list")
    if not isinstance(unique_blobs, list) or not unique_blobs:
        raise ValueError(f"{run_id}: unique_blobs must be a non-empty list")
    if manifest.get("source_file_count") != len(source_files):
        raise ValueError(f"{run_id}: source_file_count mismatch")
    if manifest.get("unique_blob_count") != len(unique_blobs):
        raise ValueError(f"{run_id}: unique_blob_count mismatch")

    source_by_alias: dict[str, dict[str, Any]] = {}
    source_order: dict[str, int] = {}
    for index, source in enumerate(source_files, start=1):
        if not isinstance(source, dict):
            raise ValueError(f"{run_id}: source row {index} is not an object")
        alias = require_leaf_name(
            source.get("filename"), f"{run_id} source filename"
        )
        if alias in source_by_alias:
            raise ValueError(f"{run_id}: duplicate exact source alias: {alias!r}")
        byte_count = require_nonnegative_int(
            source.get("bytes"), f"{run_id} source bytes"
        )
        sha256 = require_sha256(
            source.get("sha256"), f"{run_id} source SHA-256"
        )
        source_path = source.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            raise ValueError(f"{run_id}: source_path must be non-empty text")
        source_by_alias[alias] = {
            "source_alias_id": f"{run_id}::alias::{index:04d}",
            "batch_run_id": run_id,
            "source_ordinal": index,
            "source_alias": alias,
            "source_path_recorded": source_path,
            "kind": source.get("kind"),
            "bytes": byte_count,
            "sha256": sha256,
        }
        source_order[alias] = index

    declared_aliases: list[str] = []
    blobs: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    seen_paths: set[str] = set()
    alias_to_blob: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(unique_blobs, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"{run_id}: blob row {index} is not an object")
        raw_aliases = row.get("source_aliases")
        if not isinstance(raw_aliases, list) or not raw_aliases:
            raise ValueError(f"{run_id}: blob source_aliases must be non-empty")
        exact_aliases = [
            require_leaf_name(value, f"{run_id} blob source alias")
            for value in raw_aliases
        ]
        if len(set(exact_aliases)) != len(exact_aliases):
            raise ValueError(f"{run_id}: duplicate alias within one canonical blob")
        if row.get("source_alias_count") != len(exact_aliases):
            raise ValueError(f"{run_id}: source_alias_count mismatch")
        blob = canonical_blob_binding(
            workspace=workspace,
            batch_root=batch_root,
            run_id=run_id,
            blob_index=index,
            row=row,
            source_aliases=exact_aliases,
            stable_tokens=stable_tokens,
        )
        if blob["sha256"] in seen_hashes:
            raise ValueError(f"{run_id}: duplicate canonical blob SHA-256")
        if blob["canonical_path"] in seen_paths:
            raise ValueError(f"{run_id}: duplicate canonical blob path")
        seen_hashes.add(blob["sha256"])
        seen_paths.add(blob["canonical_path"])
        blobs.append(blob)
        for alias in exact_aliases:
            if alias in alias_to_blob:
                raise ValueError(
                    f"{run_id}: alias maps to more than one canonical blob: {alias!r}"
                )
            alias_to_blob[alias] = blob
            declared_aliases.append(alias)

    if Counter(declared_aliases) != Counter(source_by_alias.keys()):
        missing = sorted(set(source_by_alias) - set(declared_aliases))
        extra = sorted(set(declared_aliases) - set(source_by_alias))
        raise ValueError(
            f"{run_id}: exact alias coverage mismatch; "
            f"missing={missing!r}, extra={extra!r}"
        )

    aliases: list[dict[str, Any]] = []
    for alias in sorted(source_by_alias, key=source_order.__getitem__):
        source = source_by_alias[alias]
        blob = alias_to_blob[alias]
        if (
            source["bytes"] != blob["bytes"]
            or source["sha256"] != blob["sha256"]
            or source["kind"] != blob["kind"]
        ):
            raise ValueError(
                f"{run_id}: alias bytes/SHA-256/kind do not match "
                f"canonical blob: {alias!r}"
            )
        aliases.append(
            {
                **source,
                "canonical_blob_id": blob["canonical_blob_id"],
                "canonical_path": blob["canonical_path"],
                "canonical_bytes": blob["bytes"],
                "canonical_sha256": blob["sha256"],
                "alias_to_canonical_bytes_sha256_match": True,
            }
        )

    source_total = sum(row["bytes"] for row in aliases)
    unique_total = sum(row["bytes"] for row in blobs)
    if manifest.get("source_total_bytes") != source_total:
        raise ValueError(f"{run_id}: source_total_bytes mismatch")
    if manifest.get("unique_storage_bytes") != unique_total:
        raise ValueError(f"{run_id}: unique_storage_bytes mismatch")
    return aliases, blobs


def load_batch(
    workspace: Path, spec: dict[str, str]
) -> tuple[dict[str, Any], list[tuple[Path, tuple[int, int, int]]]]:
    run_id = spec["run_id"]
    expected_manifest_hash = require_sha256(
        spec["manifest_sha256"], f"{run_id} expected manifest SHA-256"
    )
    manifest_path = resolve_workspace_relative(workspace, spec["manifest_path"])
    require_plain_existing_path(manifest_path, workspace)
    manifest_binding, manifest_token = stable_file_binding(
        manifest_path, workspace
    )
    if manifest_binding["sha256"] != expected_manifest_hash:
        raise RuntimeError(
            f"{run_id}: sealed manifest SHA-256 changed; "
            f"expected={expected_manifest_hash}, "
            f"observed={manifest_binding['sha256']}"
        )
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"{run_id}: manifest root is not an object")
    validate_common_manifest(manifest, run_id)
    stable_tokens: list[tuple[Path, tuple[int, int, int]]] = [
        (manifest_path, manifest_token)
    ]
    batch_root = manifest_path.parent
    if "packages" in manifest:
        aliases, blobs = parse_legacy_packages(
            workspace=workspace,
            batch_root=batch_root,
            run_id=run_id,
            manifest=manifest,
            stable_tokens=stable_tokens,
        )
        manifest_shape = "LEGACY_PACKAGES_ONE_ALIAS_PER_CANONICAL_BLOB"
    elif "source_files" in manifest and "unique_blobs" in manifest:
        aliases, blobs = parse_deduplicated_manifest(
            workspace=workspace,
            batch_root=batch_root,
            run_id=run_id,
            manifest=manifest,
            stable_tokens=stable_tokens,
        )
        manifest_shape = "SOURCE_ALIASES_TO_DEDUPLICATED_CANONICAL_BLOBS"
    else:
        raise ValueError(f"{run_id}: unsupported intake manifest shape")

    result = {
        "run_id": run_id,
        "manifest_shape": manifest_shape,
        "manifest": manifest_binding,
        "purpose_ceiling": manifest["purpose_ceiling"],
        "admission_decision": manifest["admission_decision"],
        "source_alias_count": len(aliases),
        "source_alias_total_bytes": sum(row["bytes"] for row in aliases),
        "canonical_blob_count": len(blobs),
        "canonical_blob_total_bytes": sum(row["bytes"] for row in blobs),
        "source_alias_bindings": aliases,
        "canonical_blob_bindings": blobs,
    }
    return result, stable_tokens


def public_batch_summary(batch: dict[str, Any]) -> dict[str, Any]:
    return {
        key: batch[key]
        for key in (
            "run_id",
            "manifest_shape",
            "manifest",
            "purpose_ceiling",
            "admission_decision",
            "source_alias_count",
            "source_alias_total_bytes",
            "canonical_blob_count",
            "canonical_blob_total_bytes",
        )
    }


def build_rights_receipt(batches: list[dict[str, Any]]) -> dict[str, Any]:
    alias_bindings = [
        alias
        for batch in batches
        for alias in batch["source_alias_bindings"]
    ]
    blob_bindings = [
        blob
        for batch in batches
        for blob in batch["canonical_blob_bindings"]
    ]
    global_hashes = {blob["sha256"] for blob in blob_bindings}
    return {
        "schema_version": 1,
        "receipt_id": RUN_ID,
        "attestation_date": ATTESTATION_DATE,
        "declaration": DECLARATION,
        "attestation": {
            "basis": "DIRECT_USER_ATTESTATION",
            "evidence_character": (
                "USER_ATTESTATION_ONLY__NOT_INDEPENDENT_CONTRACT_VERIFICATION"
            ),
            "provider_contract_independently_verified": False,
            "institution_contract_independently_verified": False,
            "legal_opinion": False,
        },
        "covered_subject": (
            "EXACT_SOURCE_ALIASES_AND_SEALED_CANONICAL_BLOBS_IN_THE_"
            "THREE_BOUND_MANUAL_EXPORT_MANIFESTS_ONLY"
        ),
        "rights_scope": {
            "allowed": list(ALLOWED_SCOPE),
            "prohibited": list(PROHIBITED_SCOPE),
            "not_covered": list(NOT_COVERED_SCOPE),
        },
        "input_manifests": [public_batch_summary(batch) for batch in batches],
        "totals": {
            "manifest_count": len(batches),
            "source_alias_count": len(alias_bindings),
            "source_alias_total_bytes": sum(
                row["bytes"] for row in alias_bindings
            ),
            "canonical_blob_count": len(blob_bindings),
            "global_unique_canonical_sha256_count": len(global_hashes),
            "canonical_blob_total_bytes": sum(
                row["bytes"] for row in blob_bindings
            ),
        },
        "integrity_contract": {
            "hash_algorithm": "sha256",
            "manifest_sha256_pinned": True,
            "every_source_alias_bound_to_one_canonical_blob": True,
            "every_alias_binding_matches_canonical_bytes_sha256": True,
            "every_canonical_blob_rehashed_from_sealed_storage": True,
            "canonical_path_contract": "WORKSPACE_RELATIVE_POSIX",
        },
        "source_alias_bindings": alias_bindings,
        "canonical_blob_bindings": blob_bindings,
        "admission_effect": {
            "rights_boundary_recorded_for_exact_assets": True,
            "independent_contract_verification_completed": False,
            "source_admission_changed": False,
            "catalog_registration_changed": False,
            "quality_status_changed": False,
            "research_authorized": False,
            "formal_snapshot_published": False,
            "purpose_ceiling_preserved": "INSPECTION_ONLY",
            "admission_decision_preserved": "NOT_ADMITTED",
        },
    }


def binding_for_bytes(
    relative_path: str, value: bytes
) -> dict[str, Any]:
    return {
        "path": relative_path,
        "bytes": len(value),
        "sha256": sha256_bytes(value),
    }


def build_readme(receipt: dict[str, Any]) -> bytes:
    totals = receipt["totals"]
    manifests = receipt["input_manifests"]
    lines = [
        "# CSMAR 人工导出包用户权利声明回执",
        "",
        "结论：已把用户声明精确绑定到三批封存清单中的每个 source alias "
        "和每个 canonical blob，并现场复核 canonical blob 的字节数与 SHA-256。",
        "",
        "声明：",
        "",
        f"`{DECLARATION}`",
        "",
        "允许范围：",
        "",
        "- 在本机长期保存本回执精确绑定的封存资产；",
        "- 在校内内部开展非商业学术研究；",
        "- 在内部形成派生因子和统计结果。",
        "",
        "禁止范围：",
        "",
        "- 禁止原始数据或原始封存包向外部传播、转交或再分发；",
        "- 禁止商业用途。",
        "",
        "明确不覆盖：",
        "",
        "- API、自动登录、自动化批量采集或整库抓取；",
        "- 离校、离职或机构关系终止后的保留、迁移或删除安排；",
        "- 交易所、法定披露平台或其他官方来源的权利；",
        "- 供应商、学校或机构合同条款的独立核验。",
        "",
        "证据性质：本回执只记录用户声明及其精确资产绑定，不是独立合同核验、"
        "法律意见或正式来源准入决定。它不修改 catalog、source registry、"
        "Roadmap、质量状态、正式快照或研究授权；三批资产继续保持 "
        "`INSPECTION_ONLY/NOT_ADMITTED`。",
        "",
        "覆盖统计：",
        "",
        f"- 封存 manifest：{totals['manifest_count']}；",
        f"- source alias：{totals['source_alias_count']}，"
        f"{totals['source_alias_total_bytes']} B；",
        f"- canonical blob：{totals['canonical_blob_count']}，"
        f"{totals['canonical_blob_total_bytes']} B；",
        f"- 全局唯一 canonical SHA-256："
        f"{totals['global_unique_canonical_sha256_count']}。",
        "",
        "输入 manifest：",
        "",
    ]
    for item in manifests:
        binding = item["manifest"]
        lines.append(
            f"- `{item['run_id']}`：`{binding['path']}`，"
            f"{binding['bytes']} B，SHA-256 `{binding['sha256']}`。"
        )
    lines.extend(
        [
            "",
            "机器可核验明细见 `RIGHTS_RECEIPT.json`；执行与不变性检查见 "
            "`VERIFICATION.json`；报告文件级哈希见 `REPORT_MANIFEST.json`。",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def build_verification(
    *,
    receipt: dict[str, Any],
    receipt_binding: dict[str, Any],
    readme_binding: dict[str, Any],
    formal_guard_unchanged: bool,
) -> dict[str, Any]:
    totals = receipt["totals"]
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": (
            "PASS__EXACT_MANIFEST_AND_PAYLOAD_BINDINGS__"
            "USER_ATTESTATION_SCOPE_RECORDED__NO_FORMAL_ADMISSION"
        ),
        "declaration_exact_match": receipt["declaration"] == DECLARATION,
        "attestation_basis": "DIRECT_USER_ATTESTATION",
        "independent_contract_verification": False,
        "manifest_checks": {
            "expected_manifest_count": 3,
            "observed_manifest_count": totals["manifest_count"],
            "all_manifest_sha256_pinned_and_verified": True,
            "all_manifest_purpose_ceilings": "INSPECTION_ONLY",
            "all_manifest_admission_decisions": "NOT_ADMITTED",
        },
        "binding_checks": {
            "source_alias_count": totals["source_alias_count"],
            "canonical_blob_count": totals["canonical_blob_count"],
            "global_unique_canonical_sha256_count": totals[
                "global_unique_canonical_sha256_count"
            ],
            "all_aliases_mapped_exactly_once": True,
            "all_alias_bytes_sha256_match_canonical": True,
            "all_canonical_payload_bytes_sha256_verified": True,
            "all_input_files_stable_during_generation": True,
        },
        "scope_checks": {
            "allowed_codes": [item["code"] for item in ALLOWED_SCOPE],
            "prohibited_codes": [item["code"] for item in PROHIBITED_SCOPE],
            "not_covered_codes": [item["code"] for item in NOT_COVERED_SCOPE],
            "raw_external_redistribution_prohibited": True,
            "commercial_use_prohibited": True,
            "api_or_automated_bulk_collection_not_covered": True,
            "post_affiliation_disposition_not_covered": True,
            "official_source_rights_not_covered": True,
        },
        "artifact_bindings": [receipt_binding, readme_binding],
        "reproducibility_contract": {
            "wall_clock_written": False,
            "runtime_hostname_written": False,
            "absolute_canonical_payload_paths_written": False,
            "json_key_order": "LEXICOGRAPHIC",
            "line_endings": "LF",
            "idempotent_existing_output_requires_byte_identity": True,
        },
        "formal_guard": {
            "guarded_files": list(GUARDED_FILES),
            "guarded_trees": list(GUARDED_TREES),
            "unchanged": formal_guard_unchanged,
        },
        "mutations": {
            "datasets_json": False,
            "current_source_registry_json": False,
            "roadmap": False,
            "projects_yaml": False,
            "formal_snapshots": False,
            "published_data": False,
            "quality_or_admission_state": False,
        },
        "formal_admission": False,
        "research_usable": False,
    }


def build_report_artifacts(
    *,
    report_relative: str,
    batches: list[dict[str, Any]],
    formal_guard_unchanged: bool,
) -> dict[str, bytes]:
    receipt = build_rights_receipt(batches)
    receipt_bytes = canonical_json_bytes(receipt)
    readme_bytes = build_readme(receipt)
    receipt_path = f"{report_relative}/RIGHTS_RECEIPT.json"
    readme_path = f"{report_relative}/README.md"
    verification = build_verification(
        receipt=receipt,
        receipt_binding=binding_for_bytes(receipt_path, receipt_bytes),
        readme_binding=binding_for_bytes(readme_path, readme_bytes),
        formal_guard_unchanged=formal_guard_unchanged,
    )
    verification_bytes = canonical_json_bytes(verification)
    without_manifest = {
        "README.md": readme_bytes,
        "RIGHTS_RECEIPT.json": receipt_bytes,
        "VERIFICATION.json": verification_bytes,
    }
    report_files = [
        binding_for_bytes(f"{report_relative}/{name}", value)
        for name, value in sorted(without_manifest.items())
    ]
    report_manifest = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "artifact_type": "USER_ATTESTED_RIGHTS_RECEIPT",
        "hash_algorithm": "sha256",
        "file_count": len(report_files),
        "total_bytes": sum(item["bytes"] for item in report_files),
        "files": report_files,
        "self_excluded": True,
        "deterministic": True,
    }
    return {
        **without_manifest,
        "REPORT_MANIFEST.json": canonical_json_bytes(report_manifest),
    }


def verify_written_report(
    report: Path, workspace: Path, expected: dict[str, bytes]
) -> dict[str, Any]:
    require_plain_existing_path(report, workspace)
    if not report.is_dir():
        raise RuntimeError(f"report path is not a directory: {report}")
    actual_names = {item.name for item in report.iterdir() if item.is_file()}
    nonfiles = [item.name for item in report.iterdir() if not item.is_file()]
    if actual_names != OUTPUT_FILES or nonfiles:
        raise RuntimeError(
            f"report file-set mismatch: files={sorted(actual_names)!r}, "
            f"nonfiles={sorted(nonfiles)!r}"
        )
    for name, value in expected.items():
        path = report / name
        if path.read_bytes() != value:
            raise RuntimeError(f"report byte mismatch: {path}")
    manifest = read_json(report / "REPORT_MANIFEST.json")
    verified = 0
    for binding in manifest["files"]:
        path = resolve_workspace_relative(workspace, binding["path"])
        observed, _ = stable_file_binding(path, workspace)
        if observed != binding:
            raise RuntimeError(f"report manifest binding mismatch: {binding['path']}")
        verified += 1
    if verified != manifest["file_count"]:
        raise RuntimeError("report manifest file_count mismatch")
    manifest_path = report / "REPORT_MANIFEST.json"
    return {
        "report_manifest_files_verified": verified,
        "report_manifest": {
            "path": workspace_relative(manifest_path, workspace),
            "bytes": manifest_path.stat().st_size,
            "sha256": sha256_file(manifest_path),
        },
    }


def write_new_report(report: Path, artifacts: dict[str, bytes]) -> None:
    parent = report.parent
    staging = parent / f".staging-{RUN_ID}"
    if staging.exists() or is_reparse(parent):
        raise RuntimeError(f"unsafe or occupied report staging path: {staging}")
    staging.mkdir(exist_ok=False)
    try:
        for name, value in sorted(artifacts.items()):
            path = staging / name
            with path.open("xb") as handle:
                handle.write(value)
        staging.rename(report)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def generate_report(
    workspace: Path,
    *,
    batch_specs: Iterable[dict[str, str]] = DEFAULT_BATCH_SPECS,
    report_relative: str = DEFAULT_REPORT_RELATIVE,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    if not workspace.is_dir() or is_reparse(workspace):
        raise RuntimeError(f"workspace must be a plain directory: {workspace}")
    report = resolve_workspace_relative(workspace, report_relative)
    report_parent = workspace / "reports" / "data-governance"
    try:
        report.resolve().relative_to(report_parent.resolve())
    except ValueError as exc:
        raise ValueError("report must remain below reports/data-governance") from exc
    if report == report_parent:
        raise ValueError("report must be a child of reports/data-governance")
    require_plain_existing_path(report_parent, workspace)

    specs = list(batch_specs)
    if len(specs) != 3:
        raise ValueError("the receipt contract requires exactly three manifests")
    if len({spec["run_id"] for spec in specs}) != 3:
        raise ValueError("batch run_id values must be unique")

    guard_pre = formal_guard_state(workspace)
    batches: list[dict[str, Any]] = []
    stable_tokens: list[tuple[Path, tuple[int, int, int]]] = []
    for spec in specs:
        batch, tokens = load_batch(workspace, spec)
        batches.append(batch)
        stable_tokens.extend(tokens)
    for path, token in stable_tokens:
        assert_file_token(path, token)
    guard_after_read = formal_guard_state(workspace)
    if guard_pre != guard_after_read:
        raise RuntimeError("formal guard changed while reading sealed inputs")

    artifacts = build_report_artifacts(
        report_relative=report_relative,
        batches=batches,
        formal_guard_unchanged=True,
    )
    if set(artifacts) != OUTPUT_FILES:
        raise RuntimeError("internal output file-set contract mismatch")

    idempotent_existing_output = report.exists()
    if idempotent_existing_output:
        verify_written_report(report, workspace, artifacts)
    else:
        write_new_report(report, artifacts)
    written = verify_written_report(report, workspace, artifacts)

    for path, token in stable_tokens:
        assert_file_token(path, token)
    guard_final = formal_guard_state(workspace)
    if guard_pre != guard_final:
        raise RuntimeError("formal guard changed during receipt generation")

    receipt = json.loads(artifacts["RIGHTS_RECEIPT.json"])
    return {
        "run_id": RUN_ID,
        "report": workspace_relative(report, workspace),
        "declaration": DECLARATION,
        "manifest_count": receipt["totals"]["manifest_count"],
        "source_alias_count": receipt["totals"]["source_alias_count"],
        "source_alias_total_bytes": receipt["totals"][
            "source_alias_total_bytes"
        ],
        "canonical_blob_count": receipt["totals"]["canonical_blob_count"],
        "canonical_blob_total_bytes": receipt["totals"][
            "canonical_blob_total_bytes"
        ],
        "global_unique_canonical_sha256_count": receipt["totals"][
            "global_unique_canonical_sha256_count"
        ],
        "idempotent_existing_output": idempotent_existing_output,
        "formal_guard_unchanged": True,
        "formal_admission": False,
        "research_usable": False,
        "artifact_hashes": {
            name: {
                "bytes": len(value),
                "sha256": sha256_bytes(value),
            }
            for name, value in sorted(artifacts.items())
        },
        **written,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=str(Path(__file__).resolve().parents[2]),
        help="Quantification workspace root",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = generate_report(Path(args.workspace))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
