#!/usr/bin/env python3
"""Strict, report-only Stage-A evidence consolidation v3.

The tool has four deliberately separate layers:

1. package audit (manifest/file/external-binding integrity);
2. proposition assessment copied from audited evidence;
3. package-declared gate status;
4. the unchanged formal Stage-A state machine.

It never promotes a gate, mutates catalog/registry/Roadmap/projects, publishes a
snapshot, or authorizes research.  Every input digest is supplied by an
explicit input specification; no repaired-package hash is hard-coded here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable


RUN_ID = "stage-a-evidence-consolidation-20260730-v3"
DEFAULT_OUTPUT = f"reports/data-governance/{RUN_ID}"
OUTPUT_MANIFEST = "REPORT_MANIFEST.json"
EXPECTED_OUTPUT_FILES = frozenset(
    {
        "FORMAL_STATE_GUARDS.json",
        "INPUT_SPEC.json",
        "PACKAGE_LEDGER.json",
        "QUEUE_DELTA.json",
        "README.md",
        "RELEASE_DECISION.json",
        "SEMANTIC_LAYERS.json",
        "TEST_RESULTS.json",
        "UPSTREAM_QA.json",
        "VERIFICATION.json",
        OUTPUT_MANIFEST,
    }
)
EXPECTED_PACKAGE_IDS = frozenset(
    {
        "CLOSURE_QUEUE_REPORT",
        "CORE01_CANDIDATE",
        "CORE01_REPORT",
        "CORE02_CANDIDATE",
        "CORE02_REPORT",
        "CORE03_CORE06_CANDIDATE",
        "CORE03_CORE06_REPORT",
        "CSMAR_RIGHTS_REPORT",
        "INDEPENDENT_QA_REPORT",
    }
)
EXPECTED_COMPONENT_IDS = frozenset(
    {"CORE01", "CORE02", "CORE03_CORE06"}
)
GUARDED_FILES = (
    "data/catalog/datasets.json",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    "data/catalog/DATABASE_MANUAL.md",
    "data/catalog/DATA_QUALITY_POLICY.md",
    "control/ROADMAP.md",
    "control/projects.yaml",
    "control/WORKSPACE_INDEX.md",
    (
        "reports/data-governance/"
        "selected-core-two-stage-admission-architecture-audit-20260730-v1/"
        "STATE_MACHINE.json"
    ),
)
GUARDED_TREES = ("data/market/snapshots", "data/published")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_GATE_STATUSES = {
    "CORE01-Q1": "FAIL",
    "CORE01-Q2": "FAIL",
    "CORE01-Q3": "FAIL",
    "CORE01-Q4": "FAIL",
    "CORE01-Q5": "UNKNOWN",
    "CORE02-Q1": "FAIL",
    "CORE02-Q2": "FAIL",
    "CORE02-Q3": "UNKNOWN",
    "CORE02-Q4": "PASS",
    "CORE03-Q2": "UNKNOWN",
    "CORE03-Q3": "UNKNOWN",
    "CORE06-Q3": "FAIL",
    "CORE06-Q4": "FAIL",
    "STAGE-A-RIGHTS": "FAIL",
}
COMPONENT_PACKAGE_IDS = {
    "CORE01": ("CORE01_CANDIDATE", "CORE01_REPORT"),
    "CORE02": ("CORE02_CANDIDATE", "CORE02_REPORT"),
    "CORE03_CORE06": (
        "CORE03_CORE06_CANDIDATE",
        "CORE03_CORE06_REPORT",
    ),
}
COMPONENT_ROOT_FLAGS = {
    "CORE01": "--workspace",
    "CORE02": "--workspace",
    "CORE03_CORE06": "--root",
}


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import audit_stage_a_closure_evidence as v2  # noqa: E402


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def require_child(path: Path, parent: Path, label: str) -> None:
    if not is_within(path, parent) or path.resolve() == parent.resolve():
        raise ValueError(f"{label} must be below {parent.resolve()}: {path}")


def workspace_relative(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def resolve_workspace_path(
    workspace: Path,
    raw_path: str,
    *,
    label: str,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"{label} path must be a non-empty string")
    value = Path(raw_path.replace("\\", os.sep))
    resolved = (
        value.resolve()
        if value.is_absolute()
        else (workspace / value).resolve()
    )
    if not is_within(resolved, workspace):
        raise ValueError(f"{label} escapes workspace: {raw_path}")
    return resolved


def file_binding(path: Path, workspace: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": workspace_relative(path, workspace),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def local_binding(path: Path, root: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": path.resolve().relative_to(root.resolve()).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value.lower()):
        raise ValueError(f"{label} must be a lowercase 64-character SHA-256")
    return value.lower()


def tree_guard(root: Path, workspace: Path) -> dict[str, Any]:
    files = (
        [
            file_binding(path, workspace)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
        if root.is_dir()
        else []
    )
    return {
        "path": workspace_relative(root, workspace),
        "exists": root.is_dir(),
        "file_count": len(files),
        "total_bytes": sum(row["bytes"] for row in files),
        "tree_sha256": sha256_bytes(canonical_json_bytes(files)),
        "files": files,
    }


def formal_guards(workspace: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for relative in GUARDED_FILES:
        path = workspace / relative
        if not path.is_file():
            raise FileNotFoundError(f"guarded file missing: {relative}")
        files[relative] = file_binding(path, workspace)
    return {
        "files": files,
        "trees": {
            relative: tree_guard(workspace / relative, workspace)
            for relative in GUARDED_TREES
        },
    }


def validate_input_spec(spec: Any, workspace: Path) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise TypeError("input spec must be a JSON object")
    if spec.get("schema_version") != 1:
        raise ValueError("input spec schema_version must be 1")
    if spec.get("run_id") != RUN_ID:
        raise ValueError(f"input spec run_id must be {RUN_ID}")

    packages = spec.get("packages")
    if not isinstance(packages, dict):
        raise TypeError("input spec packages must be an object")
    if set(packages) != EXPECTED_PACKAGE_IDS:
        raise ValueError(
            "input spec package ids mismatch: "
            f"expected={sorted(EXPECTED_PACKAGE_IDS)}, "
            f"observed={sorted(packages)}"
        )
    normalized_packages: dict[str, Any] = {}
    for package_id, row in packages.items():
        if not isinstance(row, dict):
            raise TypeError(f"{package_id} package spec must be an object")
        root = resolve_workspace_path(
            workspace, row.get("root"), label=f"{package_id}.root"
        )
        manifest_name = row.get("manifest")
        if manifest_name not in {
            "REPORT_MANIFEST.json",
            "CANDIDATE_MANIFEST.json",
        }:
            raise ValueError(f"{package_id} has unsupported manifest name")
        if "/" in manifest_name or "\\" in manifest_name:
            raise ValueError(f"{package_id} manifest must be a basename")
        normalized_packages[package_id] = {
            "root": root,
            "manifest": manifest_name,
            "sha256": require_sha(
                row.get("sha256"), f"{package_id}.sha256"
            ),
        }

    stage = spec.get("stage_machine")
    if not isinstance(stage, dict):
        raise TypeError("stage_machine must be an object")
    normalized_stage = {
        "path": resolve_workspace_path(
            workspace, stage.get("path"), label="stage_machine"
        ),
        "sha256": require_sha(
            stage.get("sha256"), "stage_machine.sha256"
        ),
    }

    executables = spec.get("executables")
    if not isinstance(executables, dict):
        raise TypeError("executables must be an object")
    if set(executables) != EXPECTED_COMPONENT_IDS:
        raise ValueError("input spec executable component ids mismatch")
    normalized_executables: dict[str, Any] = {}
    for component_id, row in executables.items():
        if not isinstance(row, dict):
            raise TypeError(f"{component_id} executable spec must be object")
        normalized: dict[str, Any] = {}
        for key in ("generator_source", "test_source", "test_results"):
            binding = row.get(key)
            if not isinstance(binding, dict):
                raise TypeError(f"{component_id}.{key} must be an object")
            path = resolve_workspace_path(
                workspace,
                binding.get("path"),
                label=f"{component_id}.{key}",
            )
            normalized[key] = {
                "path": path,
                "sha256": require_sha(
                    binding.get("sha256"),
                    f"{component_id}.{key}.sha256",
                ),
            }
        require_child(
            normalized["generator_source"]["path"],
            workspace / "control" / "tools",
            f"{component_id} generator source",
        )
        require_child(
            normalized["test_source"]["path"],
            workspace / "control" / "tests",
            f"{component_id} test source",
        )
        normalized_executables[component_id] = normalized

    qa = spec.get("independent_qa")
    if not isinstance(qa, dict) or set(qa) != EXPECTED_COMPONENT_IDS:
        raise ValueError("independent_qa component ids mismatch")
    normalized_qa: dict[str, Any] = {}
    for component_id, row in qa.items():
        if not isinstance(row, dict):
            raise TypeError(f"{component_id} QA result must be object")
        status = row.get("status")
        if status not in {"PASS", "FAIL", "PENDING"}:
            raise ValueError(
                f"{component_id} independent QA status is invalid"
            )
        counts: dict[str, int] = {}
        for key in ("p0", "p1", "p2"):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{component_id}.{key} must be nonnegative int")
            counts[key] = value
        basis = row.get("basis")
        if not isinstance(basis, str) or not basis.strip():
            raise ValueError(f"{component_id} independent QA basis missing")
        receipt = row.get("receipt")
        if not isinstance(receipt, dict):
            raise TypeError(
                f"{component_id} independent QA receipt must be object"
            )
        receipt_path = resolve_workspace_path(
            workspace,
            receipt.get("path"),
            label=f"{component_id}.independent_qa.receipt",
        )
        require_child(
            receipt_path,
            normalized_packages["INDEPENDENT_QA_REPORT"]["root"],
            f"{component_id} independent QA receipt",
        )
        normalized_qa[component_id] = {
            "status": status,
            **counts,
            "basis": basis,
            "receipt": {
                "path": receipt_path,
                "sha256": require_sha(
                    receipt.get("sha256"),
                    f"{component_id}.independent_qa.receipt.sha256",
                ),
            },
        }

    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "packages": normalized_packages,
        "stage_machine": normalized_stage,
        "executables": normalized_executables,
        "independent_qa": normalized_qa,
    }


def _manifest_payload(manifest: Any) -> tuple[str, Any]:
    return v2.manifest_payload(manifest)


def _resolve_manifest_file(
    *,
    raw_path: str,
    package_root: Path,
    workspace: Path,
) -> Path:
    value = Path(raw_path.replace("\\", os.sep))
    if value.is_absolute():
        path = value.resolve()
    else:
        workspace_candidate = (workspace / value).resolve()
        package_candidate = (package_root / value).resolve()
        # Workspace-relative manifests use a path that already lands inside
        # the package.  Package-relative manifests use the package candidate.
        # This order prevents a harmless workspace-root README.md from making
        # a package-local README.md ambiguous.
        if (
            workspace_candidate.is_file()
            and is_within(workspace_candidate, package_root)
        ):
            path = workspace_candidate
        elif package_candidate.is_file():
            path = package_candidate
        elif workspace_candidate.is_file():
            path = workspace_candidate
        else:
            raise ValueError(f"manifest file is missing: {raw_path!r}")
    if not path.is_file():
        raise ValueError(f"manifest path is not a file: {raw_path!r}")
    if not is_within(path, package_root):
        raise ValueError(f"manifest payload file escapes package: {raw_path}")
    return path


def _verification_result(
    *,
    path: Path,
    expected_bytes: int,
    expected_sha256: str,
    workspace: Path,
    discovered_from: str,
) -> dict[str, Any]:
    actual_bytes = path.stat().st_size
    actual_sha = sha256_file(path)
    errors = []
    if actual_bytes != expected_bytes:
        errors.append("bytes mismatch")
    if actual_sha != expected_sha256.lower():
        errors.append("sha256 mismatch")
    return {
        "discovered_from": discovered_from,
        "location": None,
        "path": workspace_relative(path, workspace),
        "expected_bytes": expected_bytes,
        "expected_sha256": expected_sha256.lower(),
        "resolved_path": workspace_relative(path, workspace),
        "resolution_basis": "STRICT_PACKAGE_FILE",
        "actual_bytes": actual_bytes,
        "actual_sha256": actual_sha,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }


def _binding_failures(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "discovered_from": row.get("discovered_from"),
            "location": row.get("location"),
            "path": row.get("path"),
            "status": row.get("status"),
            "errors": row.get("errors"),
        }
        for row in rows
        if row.get("status") != "PASS"
    ]


def merge_binding_index(
    target: dict[tuple[int, str], set[Path]],
    source: dict[tuple[int, str], set[Path]],
) -> None:
    for key, paths in source.items():
        target.setdefault(key, set()).update(paths)


def declared_index_from_packages(
    packages: dict[str, dict[str, Any]],
    workspace: Path,
) -> dict[tuple[int, str], set[Path]]:
    """Index only files explicitly declared by the pinned top manifests."""

    index: dict[tuple[int, str], set[Path]] = {}
    for package_id, row in packages.items():
        manifest_path = row["root"] / row["manifest"]
        if not manifest_path.is_file():
            continue
        if sha256_file(manifest_path) != row["sha256"]:
            continue
        try:
            manifest = load_json(manifest_path)
            _, payload = _manifest_payload(manifest)
            records = v2.collect_manifest_bindings(payload)
            for record in records:
                if v2._binding_shape_errors(record):
                    continue
                path = _resolve_manifest_file(
                    raw_path=record["path"],
                    package_root=row["root"],
                    workspace=workspace,
                )
                if (
                    path.stat().st_size == record["bytes"]
                    and sha256_file(path) == record["sha256"].lower()
                ):
                    index.setdefault(
                        (record["bytes"], record["sha256"].lower()), set()
                    ).add(path)
            index.setdefault(
                (manifest_path.stat().st_size, sha256_file(manifest_path)),
                set(),
            ).add(manifest_path)
        except Exception:
            # The strict package audit will surface the concrete error.
            continue
    return index


def _collect_document_bindings(
    value: Any,
    *,
    strict_manifest: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def historical_container(key: str) -> bool:
        normalized = key.lower()
        return (
            v2.is_historical_guard_container(key)
            or "guard" in normalized
            or "pre_repair" in normalized
            or "archived_" in normalized
            or normalized.startswith("historical_")
            or "repair_history" in normalized
        )

    strict_sections = {
        "files",
        "artifacts",
        "build_inputs",
        "external_bindings",
        "candidate_manifest",
    }

    def walk(node: Any, location: str, strict_zone: bool) -> None:
        if isinstance(node, dict):
            present = v2.BINDING_KEYS.intersection(node)
            if v2.BINDING_KEYS.issubset(node):
                records.append(
                    {
                        "location": location,
                        "path": node["path"],
                        "bytes": node["bytes"],
                        "sha256": node["sha256"],
                    }
                )
            elif strict_zone and "path" in present and present:
                missing = sorted(v2.BINDING_KEYS.difference(present))
                raise ValueError(
                    f"incomplete manifest binding at {location}; "
                    f"missing {missing}"
                )
            for key, child in node.items():
                if (
                    historical_container(str(key))
                    and str(key) not in strict_sections
                ):
                    continue
                child_strict = strict_zone or (
                    strict_manifest
                    and location == "$"
                    and str(key) in strict_sections
                )
                walk(child, f"{location}.{key}", child_strict)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{location}[{index}]", strict_zone)

    walk(value, "$", False)
    return records


def _verify_recursive_binding(
    *,
    binding: dict[str, Any],
    document: Path,
    workspace: Path,
    relocation_index: dict[tuple[int, str], set[Path]],
) -> dict[str, Any]:
    result = {
        "discovered_from": workspace_relative(document, workspace),
        "location": binding.get("location"),
        "path": binding.get("path"),
        "expected_bytes": binding.get("bytes"),
        "expected_sha256": binding.get("sha256"),
        "resolved_path": None,
        "resolution_basis": None,
        "actual_bytes": None,
        "actual_sha256": None,
        "status": "FAIL",
        "errors": [],
    }
    shape_errors = v2._binding_shape_errors(binding)
    if shape_errors:
        result["errors"] = shape_errors
        return result
    expected = (binding["bytes"], binding["sha256"].lower())
    raw = Path(binding["path"].replace("\\", os.sep))
    candidates: list[tuple[str, Path]] = []
    if raw.is_absolute():
        candidates.append(("absolute", raw.resolve()))
    else:
        candidates.extend(
            [
                ("workspace_relative", (workspace / raw).resolve()),
                ("document_relative", (document.parent / raw).resolve()),
                ("document_basename", (document.parent / raw.name).resolve()),
            ]
        )
    seen: set[Path] = set()
    for basis, candidate in candidates:
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        if not is_within(candidate, workspace):
            continue
        actual = (candidate.stat().st_size, sha256_file(candidate))
        if actual == expected:
            result.update(
                {
                    "resolved_path": workspace_relative(
                        candidate, workspace
                    ),
                    "resolution_basis": basis,
                    "actual_bytes": actual[0],
                    "actual_sha256": actual[1],
                    "status": "PASS",
                }
            )
            return result

    # Repair-history manifests retain original logical paths.  Relocation is
    # accepted only when the top pinned package graph declares exactly one
    # file with the expected bytes/hash.
    if "repair_history" in document.parts:
        matches = sorted(relocation_index.get(expected, set()))
        if len(matches) == 1:
            candidate = matches[0]
            result.update(
                {
                    "resolved_path": workspace_relative(
                        candidate, workspace
                    ),
                    "resolution_basis": (
                        "UNIQUE_HASH_RELOCATED_ARCHIVE_BINDING"
                    ),
                    "actual_bytes": expected[0],
                    "actual_sha256": expected[1],
                    "status": "PASS",
                }
            )
            return result
        if len(matches) > 1:
            result["errors"] = [
                "historical binding relocation is not uniquely reachable"
            ]
            return result
    result["errors"] = ["no exact bytes/SHA-256 binding target found"]
    return result


def audit_binding_graph_v3(
    *,
    initial_results: list[dict[str, Any]],
    workspace: Path,
    relocation_index: dict[tuple[int, str], set[Path]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queue: list[Path] = []
    for result in initial_results:
        if result.get("status") == "PASS" and result.get("resolved_path"):
            queue.append(workspace / result["resolved_path"])
    seen_documents: set[Path] = set()
    results: list[dict[str, Any]] = []
    parse_failures: list[dict[str, Any]] = []
    seen_expectations: set[tuple[str, int, str, str]] = set()
    cursor = 0
    while cursor < len(queue):
        document = queue[cursor].resolve()
        cursor += 1
        if document in seen_documents:
            continue
        seen_documents.add(document)
        if document.suffix.lower() not in {".json", ".jsonl"}:
            continue
        try:
            value = v2.parse_evidence_document(document)
            if value is None:
                continue
            strict_manifest = document.name.upper().endswith(
                "MANIFEST.JSON"
            )
            bindings = _collect_document_bindings(
                value, strict_manifest=strict_manifest
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            parse_failures.append(
                {
                    "path": workspace_relative(document, workspace),
                    "status": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        for binding in bindings:
            expectation = (
                str(binding.get("path")),
                binding.get("bytes"),
                str(binding.get("sha256")).lower(),
                workspace_relative(document, workspace),
            )
            if expectation in seen_expectations:
                continue
            seen_expectations.add(expectation)
            result = _verify_recursive_binding(
                binding=binding,
                document=document,
                workspace=workspace,
                relocation_index=relocation_index,
            )
            results.append(result)
            if result["status"] == "PASS" and result["resolved_path"]:
                path = workspace / result["resolved_path"]
                relocation_index.setdefault(
                    (result["actual_bytes"], result["actual_sha256"]), set()
                ).add(path)
                if path.suffix.lower() in {".json", ".jsonl"}:
                    queue.append(path)
    return results, parse_failures


def strict_manifest_package(
    *,
    package_id: str,
    root: Path,
    manifest_name: str,
    expected_manifest_sha256: str | None,
    workspace: Path,
    relocation_index: dict[tuple[int, str], set[Path]] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    manifest_path = root / manifest_name
    manifest_binding: dict[str, Any] | None = None
    if not root.is_dir():
        return {
            "package_id": package_id,
            "status": "FAIL",
            "errors": ["package root missing"],
            "root": workspace_relative(root, workspace),
        }
    if not manifest_path.is_file():
        return {
            "package_id": package_id,
            "status": "FAIL",
            "errors": [f"{manifest_name} missing"],
            "root": workspace_relative(root, workspace),
        }
    manifest_binding = file_binding(manifest_path, workspace)
    if (
        expected_manifest_sha256 is not None
        and manifest_binding["sha256"] != expected_manifest_sha256
    ):
        errors.append("manifest SHA-256 does not match explicit input pin")

    manifest: Any = None
    records: list[dict[str, Any]] = []
    local_results: list[dict[str, Any]] = []
    external_results: list[dict[str, Any]] = []
    recursive_results: list[dict[str, Any]] = []
    parse_failures: list[dict[str, Any]] = []
    actual_relative: set[str] = set()
    expected_relative: set[str] = {manifest_name}
    disk_set_excludes: set[str] = set()
    extras: list[str] = []
    missing: list[str] = []
    try:
        manifest = load_json(manifest_path)
        root_format, payload = _manifest_payload(manifest)
        if isinstance(manifest, dict) and "strict_disk_set_excludes" in manifest:
            raw_excludes = manifest["strict_disk_set_excludes"]
            permitted_excludes = {manifest_name, "VERIFICATION.json"}
            if not isinstance(raw_excludes, list) or not all(
                isinstance(name, str) for name in raw_excludes
            ):
                errors.append("strict_disk_set_excludes must be a list of strings")
            elif len(raw_excludes) != len(set(raw_excludes)):
                errors.append("strict_disk_set_excludes contains duplicate entries")
            elif set(raw_excludes) != permitted_excludes:
                errors.append(
                    "strict_disk_set_excludes must exactly equal "
                    + repr(sorted(permitted_excludes))
                )
            else:
                disk_set_excludes = permitted_excludes

                # A report's self-excluded verification record is not an
                # unbound side channel: it must attest to both manifests.
                # Existing candidate packages without this record retain their
                # historical contract; once the record is present it is always
                # checked with the same strict rules.
                verification_path = root / "VERIFICATION.json"
                require_verification = (
                    manifest_name == "REPORT_MANIFEST.json"
                    or verification_path.is_file()
                )
                if require_verification:
                    if not verification_path.is_file():
                        errors.append("VERIFICATION.json missing")
                    else:
                        verification = load_json(verification_path)
                        if not isinstance(verification, dict):
                            errors.append("VERIFICATION.json must be a JSON object")
                        else:
                            if verification.get("run_id") != manifest.get("run_id"):
                                errors.append("VERIFICATION.json run_id does not match manifest")
                            if verification.get("status") != "PASS":
                                errors.append("VERIFICATION.json status is not PASS")
                            for binding_key, expected_name in (
                                ("report_manifest", "REPORT_MANIFEST.json"),
                                ("candidate_manifest", "CANDIDATE_MANIFEST.json"),
                            ):
                                binding = verification.get(binding_key)
                                if not isinstance(binding, dict):
                                    errors.append(
                                        f"VERIFICATION.json {binding_key} binding missing"
                                    )
                                    continue
                                shape_errors = v2._binding_shape_errors(binding)
                                if shape_errors:
                                    errors.extend(
                                        f"VERIFICATION.json invalid {binding_key} binding: {message}"
                                        for message in shape_errors
                                    )
                                    continue
                                raw_bound_path = Path(
                                    binding["path"].replace("\\", os.sep)
                                )
                                bound_path = (
                                    raw_bound_path.resolve()
                                    if raw_bound_path.is_absolute()
                                    else (workspace / raw_bound_path).resolve()
                                )
                                if not bound_path.is_file() or not is_within(
                                    bound_path, workspace
                                ):
                                    errors.append(
                                        f"VERIFICATION.json {binding_key} path is missing or outside workspace"
                                    )
                                    continue
                                if bound_path.name != expected_name:
                                    errors.append(
                                        f"VERIFICATION.json {binding_key} must bind {expected_name}"
                                    )
                                    continue
                                verification_result = _verification_result(
                                    path=bound_path,
                                    expected_bytes=binding["bytes"],
                                    expected_sha256=binding["sha256"],
                                    workspace=workspace,
                                    discovered_from=workspace_relative(
                                        verification_path, workspace
                                    ),
                                )
                                if verification_result["status"] != "PASS":
                                    errors.append(
                                        f"VERIFICATION.json {binding_key} binding failure: "
                                        f"{verification_result.get('errors')}"
                                    )
        records = v2.collect_manifest_bindings(payload)
        if not records:
            raise ValueError("manifest contains no file bindings")
        seen_paths: set[Path] = set()
        for record in records:
            shape_errors = v2._binding_shape_errors(record)
            if shape_errors:
                errors.extend(
                    f"invalid file binding: {message}"
                    for message in shape_errors
                )
                continue
            path = _resolve_manifest_file(
                raw_path=record["path"],
                package_root=root,
                workspace=workspace,
            )
            if path in seen_paths:
                errors.append(
                    "duplicate package file binding: "
                    + workspace_relative(path, workspace)
                )
                continue
            seen_paths.add(path)
            expected_relative.add(
                path.relative_to(root).as_posix()
            )
            local_results.append(
                _verification_result(
                    path=path,
                    expected_bytes=record["bytes"],
                    expected_sha256=record["sha256"],
                    workspace=workspace,
                    discovered_from=workspace_relative(
                        manifest_path, workspace
                    ),
                )
            )
        actual_relative = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }
        audited_actual = actual_relative - disk_set_excludes
        audited_expected = expected_relative - disk_set_excludes
        extras = sorted(audited_actual - audited_expected)
        missing = sorted(audited_expected - audited_actual)
        if extras:
            errors.append(f"undeclared package files: {extras}")
        if missing:
            errors.append(f"manifest-declared files missing: {missing}")
        if isinstance(manifest, dict):
            declared_count = manifest.get("file_count")
            if (
                declared_count is not None
                and declared_count != len(records)
            ):
                errors.append(
                    f"file_count mismatch: {declared_count} != {len(records)}"
                )
            external_node = deepcopy(manifest)
            external_node.pop("files", None)
            external_node.pop("artifacts", None)
            external_records = v2.collect_manifest_bindings(external_node)
            external_results = [
                v2.verify_binding(
                    record,
                    workspace=workspace,
                    local_base=root,
                    discovered_from=workspace_relative(
                        manifest_path, workspace
                    ),
                )
                for record in external_records
            ]
        active_index: dict[tuple[int, str], set[Path]] = {}
        if relocation_index:
            merge_binding_index(active_index, relocation_index)
        for row in local_results + external_results:
            if row.get("status") == "PASS":
                active_index.setdefault(
                    (row["actual_bytes"], row["actual_sha256"]), set()
                ).add(workspace / row["resolved_path"])
        recursive_results, parse_failures = audit_binding_graph_v3(
            initial_results=local_results + external_results,
            workspace=workspace,
            relocation_index=active_index,
        )
        errors.extend(
            f"binding failure: {row['path']}: {row.get('errors')}"
            for row in local_results + external_results + recursive_results
            if row.get("status") != "PASS"
        )
        errors.extend(
            f"parse failure: {row.get('path')}: {row.get('error')}"
            for row in parse_failures
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        root_format = None

    all_results = local_results + external_results + recursive_results
    return {
        "package_id": package_id,
        "root": workspace_relative(root, workspace),
        "manifest": manifest_binding,
        "manifest_root_format": root_format,
        "explicit_manifest_pin": expected_manifest_sha256,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "file_set": {
            "declared_payload_file_count": len(records),
            "actual_recursive_file_count": len(actual_relative),
            "extra_file_count": len(extras),
            "missing_file_count": len(missing),
        },
        "bindings": {
            "local_manifest_binding_count": len(local_results),
            "manifest_external_binding_count": len(external_results),
            "recursive_binding_count": len(recursive_results),
            "failed_binding_count": len(_binding_failures(all_results)),
            "failures": _binding_failures(all_results),
            "parse_failure_count": len(parse_failures),
            "parse_failures": parse_failures,
        },
    }


def verify_explicit_file(
    *,
    path: Path,
    expected_sha256: str,
    workspace: Path,
) -> dict[str, Any]:
    result = {
        "path": workspace_relative(path, workspace),
        "expected_sha256": expected_sha256,
        "actual_sha256": None,
        "bytes": None,
        "status": "FAIL",
        "errors": [],
    }
    if not path.is_file():
        result["errors"] = ["file missing"]
        return result
    result["bytes"] = path.stat().st_size
    result["actual_sha256"] = sha256_file(path)
    if result["actual_sha256"] != expected_sha256:
        result["errors"] = ["sha256 mismatch"]
        return result
    result["status"] = "PASS"
    return result


def run_upstream_verify(
    *,
    component_id: str,
    executable: dict[str, Any],
    packages: dict[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    candidate_id, report_id = COMPONENT_PACKAGE_IDS[component_id]
    generator = executable["generator_source"]["path"]
    command = [
        sys.executable,
        "-X",
        "utf8",
        str(generator),
        COMPONENT_ROOT_FLAGS[component_id],
        str(workspace),
        "--candidate",
        str(packages[candidate_id]["root"]),
        "--report",
        str(packages[report_id]["root"]),
        "--verify-only",
    ]
    completed = subprocess.run(
        command,
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (completed.stdout + completed.stderr).strip()
    return {
        "component_id": component_id,
        "command": [
            "<python>",
            "-X",
            "utf8",
            workspace_relative(generator, workspace),
            COMPONENT_ROOT_FLAGS[component_id],
            ".",
            "--candidate",
            workspace_relative(packages[candidate_id]["root"], workspace),
            "--report",
            workspace_relative(packages[report_id]["root"], workspace),
            "--verify-only",
        ],
        "exit_code": completed.returncode,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "output": output,
    }


def validate_test_results(
    *,
    component_id: str,
    path: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        value = load_json(path)
    except Exception as exc:
        return {
            "component_id": component_id,
            "status": "FAIL",
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    if not isinstance(value, dict):
        errors.append("TEST_RESULTS must be an object")
        value = {}
    if value.get("status") != "PASS":
        errors.append("TEST_RESULTS status is not PASS")
    if value.get("exit_code") != 0:
        errors.append("TEST_RESULTS exit_code is not zero")
    tests_run = value.get("tests_run")
    if (
        isinstance(tests_run, bool)
        or not isinstance(tests_run, int)
        or tests_run < 1
    ):
        errors.append("TEST_RESULTS tests_run is not a positive integer")
    return {
        "component_id": component_id,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "declared_test_status": value.get("status"),
        "declared_exit_code": value.get("exit_code"),
        "declared_tests_run": tests_run,
    }


def _core01_projection(path: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    value = load_json(path)
    gates: dict[str, str] = {}
    propositions: list[dict[str, Any]] = []
    for gate in value.get("gates", []):
        gate_id = gate.get("gate_id")
        status = gate.get("strict_status_v2")
        if gate_id:
            gates[gate_id] = status
        for row in gate.get("subchecks", []):
            propositions.append(
                {
                    "gate_id": gate_id,
                    "subcheck_id": row.get("subcheck_id"),
                    "assessment": row.get("closure_assessment"),
                    "strict_status": row.get("strict_status"),
                    "source": "CORE01_REPORT/GATE_RECOMPUTATION.json",
                }
            )
    return gates, propositions


def _core02_projection(path: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    value = load_json(path)
    gates: dict[str, str] = {}
    propositions: list[dict[str, Any]] = []
    for gate_id, gate in value.get("gates", {}).items():
        gates[gate_id] = gate.get("status")
        for row in gate.get("subchecks", []):
            propositions.append(
                {
                    "gate_id": gate_id,
                    "subcheck_id": row.get("subcheck_id"),
                    "assessment": row.get("status"),
                    "strict_status": row.get("status"),
                    "source": "CORE02_REPORT/SUBCHECK_EVIDENCE_MATRIX.json",
                }
            )
    return gates, propositions


def _core03_projection(path: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    value = load_json(path)
    gates = {
        gate_id: row.get("status")
        for gate_id, row in value.get("gates", {}).items()
    }
    propositions = [
        {
            "gate_id": gate_id,
            "subcheck_id": None,
            "assessment": row.get("status"),
            "strict_status": row.get("status"),
            "source": "CORE03_CORE06_CANDIDATE/GATE_IMPACT.json",
        }
        for gate_id, row in value.get("gates", {}).items()
    ]
    return gates, propositions


def _rights_projection(path: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    propositions, gates = v2.extract_rights_projection(load_json(path))
    return (
        {
            row["gate_id"]: row["declared_gate_status"]
            for row in gates
        },
        propositions,
    )


def find_proxy_unknown(node: Any) -> bool:
    if isinstance(node, dict):
        for key, value in node.items():
            normalized = str(key).lower()
            if "proxy" in normalized and (
                value == "UNKNOWN"
                or (
                    isinstance(value, dict)
                    and any(item == "UNKNOWN" for item in value.values())
                )
            ):
                return True
            if find_proxy_unknown(value):
                return True
    elif isinstance(node, list):
        return any(find_proxy_unknown(value) for value in node)
    return False


def semantic_layers(
    *,
    package_results: dict[str, dict[str, Any]],
    packages: dict[str, Any],
    stage_baseline: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    core01_root = packages["CORE01_REPORT"]["root"]
    core02_root = packages["CORE02_REPORT"]["root"]
    core03_root = packages["CORE03_CORE06_CANDIDATE"]["root"]
    rights_root = packages["CSMAR_RIGHTS_REPORT"]["root"]
    projections = (
        _core01_projection(core01_root / "GATE_RECOMPUTATION.json"),
        _core02_projection(core02_root / "SUBCHECK_EVIDENCE_MATRIX.json"),
        _core03_projection(core03_root / "GATE_IMPACT.json"),
        _rights_projection(rights_root / "RIGHTS_RECEIPT.json"),
    )
    gate_statuses: dict[str, str] = {}
    propositions: list[dict[str, Any]] = []
    for gates, rows in projections:
        overlap = set(gate_statuses).intersection(gates)
        if overlap:
            errors.append(f"duplicate projected gate ids: {sorted(overlap)}")
        gate_statuses.update(gates)
        propositions.extend(rows)
    if gate_statuses != EXPECTED_GATE_STATUSES:
        errors.append(
            "package-declared gate status mismatch: "
            f"expected={EXPECTED_GATE_STATUSES!r}, "
            f"observed={gate_statuses!r}"
        )
    proxy_documents = [
        load_json(core02_root / name)
        for name in ("QA_REPAIR_RECEIPT.json", "VERIFICATION.json")
        if (core02_root / name).is_file()
    ]
    proxy_unknown = any(find_proxy_unknown(value) for value in proxy_documents)
    if not proxy_unknown:
        errors.append(
            "CORE02 repaired evidence lacks explicit UNKNOWN proxy boundary"
        )
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "package_audit_layer": {
            package_id: {
                "status": row["status"],
                "failed_binding_count": row.get("bindings", {}).get(
                    "failed_binding_count"
                ),
                "extra_file_count": row.get("file_set", {}).get(
                    "extra_file_count"
                ),
            }
            for package_id, row in package_results.items()
        },
        "proposition_assessment_layer": {
            "rows": propositions,
            "aggregation_into_gate_performed": False,
        },
        "package_declared_gate_layer": {
            "statuses": dict(sorted(gate_statuses.items())),
            "expected_statuses": dict(sorted(EXPECTED_GATE_STATUSES.items())),
            "matches_fail_closed_expectation": not any(
                "package-declared gate status mismatch" in error
                for error in errors
            ),
            "core02_proxy_boundary": (
                "UNKNOWN" if proxy_unknown else "NOT_EXPLICITLY_UNKNOWN"
            ),
        },
        "formal_stage_machine_layer": {
            "ready": stage_baseline["ready"],
            "ready_count": stage_baseline["ready_count"],
            "target_count": stage_baseline["target_count"],
            "status": stage_baseline["status"],
            "changed": False,
            "formal_effect": "NONE",
        },
        "separation_contract": {
            "package_audit_is_proposition_acceptance": False,
            "proposition_assessment_is_gate_transition": False,
            "package_declared_gate_is_formal_state": False,
            "formal_state_mutation_performed": False,
        },
    }, errors


def stage_baseline(path: Path) -> dict[str, Any]:
    value = load_json(path)
    baseline = v2.stage_a_baseline(value)
    if (
        baseline["ready"] != "0/4"
        or baseline["ready_count"] != 0
        or baseline["target_count"] != 4
        or baseline["all_ready"] is not False
    ):
        raise ValueError(f"formal Stage-A baseline is not 0/4: {baseline}")
    return baseline


def queue_delta(
    *,
    queue_path: Path,
    stage: dict[str, Any],
) -> dict[str, Any]:
    queue = load_json(queue_path)
    rows = queue.get("queue")
    if not isinstance(rows, list) or len(rows) != 26:
        raise ValueError("closure queue must contain exactly 26 items")
    queue_ids = [row.get("queue_id") for row in rows]
    if any(not isinstance(value, str) or not value for value in queue_ids):
        raise ValueError("closure queue has a missing queue_id")
    if len(set(queue_ids)) != len(queue_ids):
        raise ValueError("closure queue has duplicate queue_id values")
    before_map = {
        row["queue_id"]: {
            "core_id": row.get("core_id"),
            "gate_id": row.get("gate_id"),
            "check_id": row.get("check_id"),
            "current_status": row.get("current_status"),
        }
        for row in rows
    }
    after_map = deepcopy(before_map)
    added_ids = sorted(set(after_map) - set(before_map))
    removed_ids = sorted(set(before_map) - set(after_map))
    changed_ids = sorted(
        queue_id
        for queue_id in set(before_map).intersection(after_map)
        if before_map[queue_id] != after_map[queue_id]
    )
    items = []
    for row in rows:
        items.append(
            {
                "queue_id": row.get("queue_id"),
                "core_id": row.get("core_id"),
                "gate_id": row.get("gate_id"),
                "check_id": row.get("check_id"),
                "status_before": row.get("current_status"),
                "status_after_audit_only": row.get("current_status"),
                "queue_status_change": "NONE",
            }
        )
    queue_ready = queue.get("formal_state", {}).get("stage_a_ready")
    if queue_ready != "0/4" or queue_ready != stage["ready"]:
        raise ValueError("queue and State Machine do not agree on Stage A 0/4")
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "items": items,
        "identity_proof": {
            "before_projection_sha256": sha256_bytes(
                canonical_json_bytes(before_map)
            ),
            "after_projection_sha256": sha256_bytes(
                canonical_json_bytes(after_map)
            ),
            "added_queue_ids": added_ids,
            "removed_queue_ids": removed_ids,
            "changed_queue_ids": changed_ids,
        },
        "summary": {
            "queue_item_count": 26,
            "queue_status_changes": len(changed_ids),
            "queue_items_added": len(added_ids),
            "queue_items_removed": len(removed_ids),
            "formal_stage_a_before": "0/4",
            "formal_stage_a_after": "0/4",
            "formal_effect": "NONE",
        },
    }


def independent_qa_release(
    independent_qa: dict[str, dict[str, Any]],
) -> tuple[bool, list[str]]:
    errors = []
    for component_id in sorted(EXPECTED_COMPONENT_IDS):
        row = independent_qa[component_id]
        if (
            row["status"] != "PASS"
            or row["p0"] != 0
            or row["p1"] != 0
            or row["p2"] != 0
        ):
            errors.append(
                f"{component_id} independent QA is not PASS 0/0/0: {row}"
            )
    return not errors, errors


def verify_independent_qa_receipts(
    *,
    independent_qa: dict[str, dict[str, Any]],
    packages: dict[str, dict[str, Any]],
    workspace: Path,
) -> tuple[dict[str, Any], bool, list[str]]:
    target_packages = {
        "CORE01": ("CORE01_CANDIDATE", "CORE01_REPORT"),
        "CORE02": ("CORE02_CANDIDATE", "CORE02_REPORT"),
        "CORE03_CORE06": (
            "CORE03_CORE06_CANDIDATE",
            "CORE03_CORE06_REPORT",
        ),
    }
    rows: dict[str, Any] = {}
    errors: list[str] = []
    for component_id, qa in independent_qa.items():
        receipt_spec = qa["receipt"]
        binding = verify_explicit_file(
            path=receipt_spec["path"],
            expected_sha256=receipt_spec["sha256"],
            workspace=workspace,
        )
        receipt_errors: list[str] = []
        receipt: Any = None
        if binding["status"] != "PASS":
            receipt_errors.extend(binding["errors"])
        else:
            try:
                receipt = load_json(receipt_spec["path"])
            except Exception as exc:
                receipt_errors.append(f"{type(exc).__name__}: {exc}")
        candidate_id, report_id = target_packages[component_id]
        expected_candidate = packages[candidate_id]["sha256"]
        expected_report = packages[report_id]["sha256"]
        if isinstance(receipt, dict):
            if receipt.get("status") != qa["status"]:
                receipt_errors.append("receipt status disagrees with input spec")
            for key in ("p0", "p1", "p2"):
                if receipt.get(key) != qa[key]:
                    receipt_errors.append(
                        f"receipt {key} disagrees with input spec"
                    )
            if (
                receipt.get("candidate_manifest_sha256")
                != expected_candidate
            ):
                receipt_errors.append(
                    "receipt does not bind final candidate manifest"
                )
            if receipt.get("report_manifest_sha256") != expected_report:
                receipt_errors.append(
                    "receipt does not bind final report manifest"
                )
        elif receipt is not None:
            receipt_errors.append("receipt must be a JSON object")
        rows[component_id] = {
            "binding": binding,
            "declared_status": qa["status"],
            "declared_findings": {
                "p0": qa["p0"],
                "p1": qa["p1"],
                "p2": qa["p2"],
            },
            "target_candidate_manifest_sha256": expected_candidate,
            "target_report_manifest_sha256": expected_report,
            "status": "PASS" if not receipt_errors else "FAIL",
            "errors": receipt_errors,
        }
        errors.extend(
            f"{component_id}: {message}" for message in receipt_errors
        )
    return rows, not errors, errors


def release_decision(
    *,
    packages_ok: bool,
    executables_ok: bool,
    upstream_ok: bool,
    semantics_ok: bool,
    independent_qa_ok: bool,
    guards_ok: bool,
) -> dict[str, Any]:
    report_publish_eligible = all(
        (
            packages_ok,
            executables_ok,
            upstream_ok,
            semantics_ok,
            independent_qa_ok,
            guards_ok,
        )
    )
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "report_publish_eligible": report_publish_eligible,
        "status": (
            "PASS__AUDIT_REPORT_PUBLISHABLE__FORMAL_STAGE_A_0_OF_4_UNCHANGED"
            if report_publish_eligible
            else "UNPUBLISHABLE__REBUILD_REQUIRED"
        ),
        "checks": {
            "strict_packages": packages_ok,
            "executable_and_test_bindings": executables_ok,
            "upstream_verify_only": upstream_ok,
            "fail_closed_semantics": semantics_ok,
            "independent_qa_pass_0_0_0": independent_qa_ok,
            "formal_guards_unchanged": guards_ok,
        },
        "formal_effect": "NONE",
        "formal_stage_a_before": "0/4",
        "formal_stage_a_after": "0/4",
        "queue_status_changes": 0,
        "formal_admission": False,
        "research_authorization": False,
        "catalog_update": False,
        "source_registry_update": False,
        "roadmap_update": False,
        "projects_update": False,
        "workspace_index_update": False,
        "snapshot_publish": False,
        "quality_promotion": False,
    }


def run_self_tests(workspace: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "unittest",
        "-v",
        "control.tests.test_audit_stage_a_closure_evidence_v3",
    ]
    completed = subprocess.run(
        command,
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = completed.stdout + completed.stderr
    names = sorted(
        set(
            re.findall(
                r"^(test_[A-Za-z0-9_]+) .*? \.\.\. (?:ok|FAIL|ERROR)$",
                output,
                flags=re.MULTILINE,
            )
        )
    )
    ran = re.search(r"Ran (\d+) tests?", output)
    tests_run = int(ran.group(1)) if ran else len(names)
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "command": [
            "<python>",
            "-X",
            "utf8",
            "-m",
            "unittest",
            "-v",
            "control.tests.test_audit_stage_a_closure_evidence_v3",
        ],
        "exit_code": completed.returncode,
        "tests_run": tests_run,
        "test_names": names,
        "status": (
            "PASS"
            if completed.returncode == 0 and tests_run >= 1
            else "FAIL"
        ),
    }


def package_manifest(
    *,
    root: Path,
    external_bindings: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    files = [
        local_binding(path, root)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != OUTPUT_MANIFEST
    ]
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": status,
        "manifest_type": "AUDIT_REPORT",
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(row["bytes"] for row in files),
        "external_bindings": external_bindings,
        "self_excluded_from_files": OUTPUT_MANIFEST,
    }


def reject_extra_or_missing(root: Path) -> tuple[list[str], list[str]]:
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    return (
        sorted(actual - EXPECTED_OUTPUT_FILES),
        sorted(EXPECTED_OUTPUT_FILES - actual),
    )


def make_readme() -> str:
    return """# Stage-A evidence consolidation v3

本包是严格、只读的证据汇总审计，不是新的正式准入。

- 九个输入包均由显式 SHA-256 pin 约束，并核对包内完整文件集、外部绑定和递归证据绑定。
- 三组生成器、测试源码、机器测试结果及 `--verify-only` 都被绑定和复核。
- package audit、proposition assessment、package-declared gate 和 formal Stage-A state 四层分开记录。
- CORE02-Q3 与代理/凭据传输边界均 fail-closed 为 `UNKNOWN`。
- closure queue 26 项保持原状态，变化数为 0。
- 正式 Stage A 保持 `0/4`；catalog、registry、Roadmap、projects、Workspace Index、快照和 published 均无变更。
- 本报告 `formal_effect=NONE`，不授予正式准入或研究权限。
"""


def external_manifest_bindings(
    *,
    workspace: Path,
    normalized: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "v2_auditor_source": file_binding(
            workspace / "control/tools/audit_stage_a_closure_evidence.py",
            workspace,
        ),
        "v3_auditor_source": file_binding(Path(__file__), workspace),
        "v3_test_source": file_binding(
            workspace
            / "control/tests/test_audit_stage_a_closure_evidence_v3.py",
            workspace,
        ),
        "stage_machine": file_binding(
            normalized["stage_machine"]["path"], workspace
        ),
    }
    for package_id, row in normalized["packages"].items():
        result[f"{package_id.lower()}_manifest"] = file_binding(
            row["root"] / row["manifest"], workspace
        )
    for component_id, row in normalized["executables"].items():
        for key, binding in row.items():
            result[f"{component_id.lower()}_{key}"] = file_binding(
                binding["path"], workspace
            )
    for component_id, row in normalized["independent_qa"].items():
        result[f"{component_id.lower()}_independent_qa_receipt"] = (
            file_binding(row["receipt"]["path"], workspace)
        )
    return result


def _serializable_input_spec(
    normalized: dict[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "packages": {
            package_id: {
                "root": workspace_relative(row["root"], workspace),
                "manifest": row["manifest"],
                "sha256": row["sha256"],
            }
            for package_id, row in normalized["packages"].items()
        },
        "stage_machine": {
            "path": workspace_relative(
                normalized["stage_machine"]["path"], workspace
            ),
            "sha256": normalized["stage_machine"]["sha256"],
        },
        "executables": {
            component_id: {
                key: {
                    "path": workspace_relative(binding["path"], workspace),
                    "sha256": binding["sha256"],
                }
                for key, binding in row.items()
            }
            for component_id, row in normalized["executables"].items()
        },
        "independent_qa": {
            component_id: {
                "status": row["status"],
                "p0": row["p0"],
                "p1": row["p1"],
                "p2": row["p2"],
                "basis": row["basis"],
                "receipt": {
                    "path": workspace_relative(
                        row["receipt"]["path"], workspace
                    ),
                    "sha256": row["receipt"]["sha256"],
                },
            }
            for component_id, row in normalized["independent_qa"].items()
        },
    }


def build(
    *,
    workspace: Path,
    input_spec_path: Path,
    output: Path,
) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    staging = output.with_name(f".{output.name}.building-{os.getpid()}")
    if staging.exists():
        raise FileExistsError(f"staging already exists: {staging}")
    staging.mkdir(parents=True)
    before = formal_guards(workspace)
    try:
        normalized = validate_input_spec(
            load_json(input_spec_path), workspace
        )
        if (
            sha256_file(normalized["stage_machine"]["path"])
            != normalized["stage_machine"]["sha256"]
        ):
            raise RuntimeError("Stage machine does not match explicit pin")
        stage = stage_baseline(normalized["stage_machine"]["path"])
        relocation_index = declared_index_from_packages(
            normalized["packages"], workspace
        )

        package_results = {
            package_id: strict_manifest_package(
                package_id=package_id,
                root=row["root"],
                manifest_name=row["manifest"],
                expected_manifest_sha256=row["sha256"],
                workspace=workspace,
                relocation_index=relocation_index,
            )
            for package_id, row in normalized["packages"].items()
        }
        packages_ok = all(
            row["status"] == "PASS" for row in package_results.values()
        )

        executable_rows: dict[str, Any] = {}
        upstream_rows: dict[str, Any] = {}
        for component_id, row in normalized["executables"].items():
            bindings = {
                key: verify_explicit_file(
                    path=binding["path"],
                    expected_sha256=binding["sha256"],
                    workspace=workspace,
                )
                for key, binding in row.items()
            }
            tests = validate_test_results(
                component_id=component_id,
                path=row["test_results"]["path"],
            )
            executable_rows[component_id] = {
                "bindings": bindings,
                "test_results_validation": tests,
                "status": (
                    "PASS"
                    if all(
                        item["status"] == "PASS"
                        for item in bindings.values()
                    )
                    and tests["status"] == "PASS"
                    else "FAIL"
                ),
            }
            upstream_rows[component_id] = run_upstream_verify(
                component_id=component_id,
                executable=row,
                packages=normalized["packages"],
                workspace=workspace,
            )

        semantics, semantic_errors = semantic_layers(
            package_results=package_results,
            packages=normalized["packages"],
            stage_baseline=stage,
        )
        queue = queue_delta(
            queue_path=(
                normalized["packages"]["CLOSURE_QUEUE_REPORT"]["root"]
                / "CLOSURE_QUEUE.json"
            ),
            stage=stage,
        )
        independent_declared_ok, independent_declared_errors = (
            independent_qa_release(
                normalized["independent_qa"]
            )
        )
        (
            independent_receipts,
            independent_receipts_ok,
            independent_receipt_errors,
        ) = verify_independent_qa_receipts(
            independent_qa=normalized["independent_qa"],
            packages=normalized["packages"],
            workspace=workspace,
        )
        independent_ok = (
            independent_declared_ok and independent_receipts_ok
        )
        independent_errors = (
            independent_declared_errors + independent_receipt_errors
        )
        self_tests = run_self_tests(workspace)

        after_analysis = formal_guards(workspace)
        guards_ok = before == after_analysis
        executable_ok = (
            all(
                row["status"] == "PASS"
                for row in executable_rows.values()
            )
            and self_tests["status"] == "PASS"
        )
        upstream_ok = all(
            row["status"] == "PASS" for row in upstream_rows.values()
        )
        queue_identity_ok = (
            queue["summary"]["queue_status_changes"] == 0
            and queue["summary"]["queue_items_added"] == 0
            and queue["summary"]["queue_items_removed"] == 0
            and queue["identity_proof"]["before_projection_sha256"]
            == queue["identity_proof"]["after_projection_sha256"]
        )
        semantics_ok = not semantic_errors and queue_identity_ok
        decision = release_decision(
            packages_ok=packages_ok,
            executables_ok=executable_ok,
            upstream_ok=upstream_ok,
            semantics_ok=semantics_ok,
            independent_qa_ok=independent_ok,
            guards_ok=guards_ok,
        )

        write_json(
            staging / "INPUT_SPEC.json",
            _serializable_input_spec(normalized, workspace),
        )
        write_json(
            staging / "PACKAGE_LEDGER.json",
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "packages": package_results,
                "summary": {
                    "package_count": len(package_results),
                    "package_pass_count": sum(
                        row["status"] == "PASS"
                        for row in package_results.values()
                    ),
                    "all_packages_pass": packages_ok,
                    "formal_effect": "NONE",
                },
            },
        )
        write_json(
            staging / "UPSTREAM_QA.json",
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "executable_and_test_bindings": executable_rows,
                "verify_only": upstream_rows,
                "independent_qa": _serializable_input_spec(
                    normalized, workspace
                )["independent_qa"],
                "independent_qa_receipts": independent_receipts,
                "independent_qa_errors": independent_errors,
                "status": (
                    "PASS"
                    if executable_ok and upstream_ok and independent_ok
                    else "FAIL"
                ),
            },
        )
        write_json(staging / "SEMANTIC_LAYERS.json", semantics)
        write_json(staging / "QUEUE_DELTA.json", queue)
        write_json(
            staging / "FORMAL_STATE_GUARDS.json",
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "before": before,
                "after_analysis": after_analysis,
                "unchanged": guards_ok,
                "formal_effect": "NONE",
            },
        )
        write_json(staging / "RELEASE_DECISION.json", decision)
        write_json(staging / "TEST_RESULTS.json", self_tests)
        (staging / "README.md").write_text(
            make_readme(), encoding="utf-8", newline="\n"
        )
        write_json(
            staging / "VERIFICATION.json",
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "strict_package_audit": packages_ok,
                "executable_and_test_bindings": executable_ok,
                "upstream_verify_only": upstream_ok,
                "semantic_errors": semantic_errors,
                "independent_qa_errors": independent_errors,
                "formal_stage_a": "0/4",
                "queue_item_count": 26,
                "queue_status_changes": 0,
                "formal_effect": "NONE",
                "formal_guards_unchanged": guards_ok,
                "status": decision["status"],
            },
        )
        write_json(
            staging / OUTPUT_MANIFEST,
            package_manifest(
                root=staging,
                external_bindings=external_manifest_bindings(
                    workspace=workspace,
                    normalized=normalized,
                ),
                status=decision["status"],
            ),
        )
        extras, missing = reject_extra_or_missing(staging)
        after_final = formal_guards(workspace)
        if extras or missing:
            raise RuntimeError(
                f"output allowlist mismatch extras={extras} missing={missing}"
            )
        if before != after_final or after_analysis != after_final:
            raise RuntimeError("formal guards changed while building report")
        if not decision["report_publish_eligible"]:
            raise RuntimeError(
                "v3 is UNPUBLISHABLE__REBUILD_REQUIRED; staging preserved at "
                + str(staging)
            )
        staging.rename(output)
        return verify_existing(workspace=workspace, output=output)
    except Exception:
        raise


def verify_existing(*, workspace: Path, output: Path) -> dict[str, Any]:
    extras, missing = reject_extra_or_missing(output)
    errors = []
    if extras or missing:
        errors.append(
            f"output allowlist mismatch extras={extras} missing={missing}"
        )
    spec_path = output / "INPUT_SPEC.json"
    normalized = validate_input_spec(load_json(spec_path), workspace)
    relocation_index = declared_index_from_packages(
        normalized["packages"], workspace
    )
    manifest_audit = strict_manifest_package(
        package_id="V3_REPORT",
        root=output,
        manifest_name=OUTPUT_MANIFEST,
        expected_manifest_sha256=None,
        workspace=workspace,
        relocation_index=relocation_index,
    )
    if manifest_audit["status"] != "PASS":
        errors.extend(manifest_audit["errors"])
    stage = stage_baseline(normalized["stage_machine"]["path"])
    if (
        sha256_file(normalized["stage_machine"]["path"])
        != normalized["stage_machine"]["sha256"]
    ):
        errors.append("live Stage machine does not match input pin")
    package_results = {
        package_id: strict_manifest_package(
            package_id=package_id,
            root=row["root"],
            manifest_name=row["manifest"],
            expected_manifest_sha256=row["sha256"],
            workspace=workspace,
            relocation_index=relocation_index,
        )
        for package_id, row in normalized["packages"].items()
    }
    if any(row["status"] != "PASS" for row in package_results.values()):
        errors.append("one or more upstream packages failed live strict audit")
    executable_rows = {}
    upstream_rows = {}
    for component_id, row in normalized["executables"].items():
        executable_rows[component_id] = {
            key: verify_explicit_file(
                path=binding["path"],
                expected_sha256=binding["sha256"],
                workspace=workspace,
            )
            for key, binding in row.items()
        }
        if any(
            item["status"] != "PASS"
            for item in executable_rows[component_id].values()
        ):
            errors.append(f"{component_id} executable/test binding failed")
        test_validation = validate_test_results(
            component_id=component_id,
            path=row["test_results"]["path"],
        )
        if test_validation["status"] != "PASS":
            errors.append(f"{component_id} TEST_RESULTS failed validation")
        upstream_rows[component_id] = run_upstream_verify(
            component_id=component_id,
            executable=row,
            packages=normalized["packages"],
            workspace=workspace,
        )
        if upstream_rows[component_id]["status"] != "PASS":
            errors.append(f"{component_id} live --verify-only failed")
    _, semantic_errors = semantic_layers(
        package_results=package_results,
        packages=normalized["packages"],
        stage_baseline=stage,
    )
    errors.extend(semantic_errors)
    queue = queue_delta(
        queue_path=(
            normalized["packages"]["CLOSURE_QUEUE_REPORT"]["root"]
            / "CLOSURE_QUEUE.json"
        ),
        stage=stage,
    )
    if (
        queue["summary"]["queue_status_changes"] != 0
        or queue["summary"]["queue_items_added"] != 0
        or queue["summary"]["queue_items_removed"] != 0
        or queue["identity_proof"]["before_projection_sha256"]
        != queue["identity_proof"]["after_projection_sha256"]
    ):
        errors.append("closure queue audit projection is not an identity")
    independent_declared_ok, independent_declared_errors = (
        independent_qa_release(normalized["independent_qa"])
    )
    (
        _independent_receipts,
        independent_receipts_ok,
        independent_receipt_errors,
    ) = verify_independent_qa_receipts(
        independent_qa=normalized["independent_qa"],
        packages=normalized["packages"],
        workspace=workspace,
    )
    if not independent_declared_ok or not independent_receipts_ok:
        errors.extend(
            independent_declared_errors + independent_receipt_errors
        )
    stored_guard = load_json(output / "FORMAL_STATE_GUARDS.json")
    live_guard = formal_guards(workspace)
    if (
        stored_guard.get("unchanged") is not True
        or stored_guard.get("before") != live_guard
        or stored_guard.get("after_analysis") != live_guard
    ):
        errors.append("live formal guards do not match sealed v3 guards")
    decision = load_json(output / "RELEASE_DECISION.json")
    if (
        decision.get("report_publish_eligible") is not True
        or decision.get("formal_effect") != "NONE"
        or decision.get("formal_stage_a_before") != "0/4"
        or decision.get("formal_stage_a_after") != "0/4"
        or decision.get("queue_status_changes") != 0
        or decision.get("formal_admission") is not False
        or decision.get("research_authorization") is not False
    ):
        errors.append("sealed release decision violates audit-only contract")
    tests = load_json(output / "TEST_RESULTS.json")
    if (
        tests.get("status") != "PASS"
        or tests.get("exit_code") != 0
        or not isinstance(tests.get("tests_run"), int)
        or tests["tests_run"] < 1
    ):
        errors.append("v3 TEST_RESULTS is not a passing machine receipt")
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "report_manifest": file_binding(
            output / OUTPUT_MANIFEST, workspace
        ),
        "strict_output_package": manifest_audit["status"],
        "strict_upstream_package_count": len(package_results),
        "strict_upstream_package_pass_count": sum(
            row["status"] == "PASS" for row in package_results.values()
        ),
        "live_upstream_verify_only": {
            key: row["status"] for key, row in upstream_rows.items()
        },
        "formal_stage_a": "0/4",
        "queue_status_changes": 0,
        "formal_effect": "NONE",
        "writes_performed": 0,
        "network_requests_performed": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--input-spec")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    output = resolve_workspace_path(
        workspace, args.output, label="output"
    )
    require_child(
        output,
        workspace / "reports" / "data-governance",
        "output",
    )
    if args.verify_only:
        if args.input_spec:
            parser.error("--input-spec is not used with --verify-only")
        result = verify_existing(workspace=workspace, output=output)
    else:
        if not args.input_spec:
            parser.error("--input-spec is required for build mode")
        input_spec = resolve_workspace_path(
            workspace, args.input_spec, label="input spec"
        )
        result = build(
            workspace=workspace,
            input_spec_path=input_spec,
            output=output,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
