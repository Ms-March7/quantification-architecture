#!/usr/bin/env python3
"""Audit a bounded CORE admission attempt without mutating formal data.

The tool is deliberately write-limited: it may replace one explicitly selected
report directory under ``reports/data-governance``.  It never writes the
formal catalog, source registry, quality state, market payload roots, candidate
payloads, or snapshots.

Contract quality gates are definitions, not failures.  Every blocking
``gate_id`` is evaluated from an optional, hash-bound evidence bundle as
``PASS``, ``FAIL`` or ``UNKNOWN``.  A blocking gate that is ``FAIL`` or
``UNKNOWN`` blocks admission; merely declaring a ``severity=BLOCK`` gate does
not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TARGETS = ("CORE-01", "CORE-02", "CORE-03", "CORE-06")
DEFAULT_REPORT = (
    "reports/data-governance/"
    "d2b-selected-core-formal-admission-gate-20260730-v1"
)
RUN_ID = "d2b-selected-core-formal-admission-gate-20260730-v1"
ALLOWED_GATE_STATUSES = {"PASS", "FAIL", "UNKNOWN"}
REPORT_MANIFEST_NAME = "REPORT_MANIFEST.json"
REPORT_PAYLOAD_FILES = {
    "GATE_DECISION.json",
    "QDATA_RECEIPTS.json",
    "README.md",
    "VERIFICATION.json",
}
ALLOWED_REPORT_FILES = REPORT_PAYLOAD_FILES | {REPORT_MANIFEST_NAME}
ABSENT_SNAPSHOT_SENTINEL = "__FORMAL_ADMISSION_AUDIT_ABSENT__"
STALE_LIMIT_FIELD_GAP = (
    "LimitDown/LimitUp/LimitStatus semantics: fields are absent from the "
    "verified 21-field manual table"
)
CORRECTED_LIMIT_FIELD_GAP = (
    "LimitDown/LimitUp/LimitStatus are present in the verified 24-field local "
    "exports, but the verified 2024-02-05 21-field manual does not define "
    "these three later fields; their source dictionary, units/semantics and "
    "historical rule binding remain unclosed"
)


def canonical_json_bytes(value: Any) -> bytes:
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


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def correct_stale_phase_a_missing(
    core_id: str, missing: Iterable[Any]
) -> tuple[list[Any], list[dict[str, str]]]:
    """Correct one superseded field-presence statement without hiding its gap."""

    corrected: list[Any] = []
    corrections: list[dict[str, str]] = []
    for item in missing:
        if core_id == "CORE-03" and item == STALE_LIMIT_FIELD_GAP:
            corrected.append(CORRECTED_LIMIT_FIELD_GAP)
            corrections.append(
                {
                    "old": STALE_LIMIT_FIELD_GAP,
                    "new": CORRECTED_LIMIT_FIELD_GAP,
                    "reason": (
                        "The 33-package schema inventory and the current "
                        "24-field Parquet candidates contain LimitDown, "
                        "LimitUp and LimitStatus. Presence is closed; official "
                        "semantics and historical rule binding are not."
                    ),
                }
            )
        else:
            corrected.append(item)
    return corrected, corrections


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def workspace_relative(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace).as_posix()


def path_under_workspace(path: Path, workspace: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"{label} escapes workspace: {resolved}") from exc
    return resolved


def require_report_path(path: Path, workspace: Path) -> None:
    report_parent = (workspace / "reports" / "data-governance").resolve()
    resolved = path.resolve()
    if Path(os.path.commonpath([resolved, report_parent])) != report_parent:
        raise ValueError(f"report output escapes reports/data-governance: {resolved}")
    if resolved == report_parent:
        raise ValueError("report output must be a child of reports/data-governance")


def reject_unexpected_report_residuals(output: Path) -> None:
    """Refuse replacement if an old report contains an unregistered residual."""

    if not output.exists():
        return
    if not output.is_dir():
        raise ValueError(f"report output is not a directory: {output}")
    unexpected = sorted(
        item.name
        for item in output.iterdir()
        if item.name not in ALLOWED_REPORT_FILES or not item.is_file()
    )
    if unexpected:
        raise ValueError(
            "refusing report replacement because unregistered residuals exist: "
            f"{unexpected}"
        )


def core_contract_entries(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for item in contract.get("datasets", []):
        plan_id = item.get("plan_id")
        if plan_id in TARGETS:
            entries[plan_id] = item
    missing = sorted(set(TARGETS) - set(entries))
    if missing:
        raise ValueError(f"CORE contract is missing targets: {missing}")
    return entries


def closure_entries(audit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = {
        item["core_id"]: item
        for item in audit.get("core_mapping", [])
        if item.get("core_id") in TARGETS
    }
    missing = sorted(set(TARGETS) - set(entries))
    if missing:
        raise ValueError(f"closure audit is missing targets: {missing}")
    return entries


def catalog_entries(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in catalog.get("datasets", [])
        if isinstance(item, dict) and item.get("dataset_id") and item.get("snapshot_id")
    ]


def catalog_quality_projection(catalog: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for item in catalog_entries(catalog):
        rows.append(
            {
                "dataset_id": str(item["dataset_id"]),
                "snapshot_id": str(item["snapshot_id"]),
                "quality_status": item.get("quality_status"),
                "purpose_ceiling": item.get("purpose_ceiling"),
                "license_status": item.get("license_status"),
                "lifecycle": item.get("lifecycle"),
                "mutability": item.get("mutability"),
            }
        )
    rows.sort(key=lambda row: (row["dataset_id"], row["snapshot_id"]))
    return {
        "row_count": len(rows),
        "sha256": sha256_bytes(canonical_json_bytes(rows)),
    }


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def tree_state(root: Path, workspace: Path) -> dict[str, Any]:
    """Return a deterministic, non-following tree hash for a formal root."""

    relative_root = workspace_relative(root, workspace)
    if not root.exists():
        value = {
            "root": relative_root,
            "exists": False,
            "file_count": 0,
            "directory_count": 0,
            "reparse_count": 0,
            "total_bytes": 0,
            "entries": [],
        }
        value["tree_sha256"] = sha256_bytes(canonical_json_bytes(value["entries"]))
        return value

    entries: list[dict[str, Any]] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name.casefold())
        for child in children:
            child_path = Path(child.path)
            relative = child_path.relative_to(root).as_posix()
            metadata = child.stat(follow_symlinks=False)
            if child.is_symlink() or _is_reparse(metadata):
                entries.append(
                    {
                        "path": relative,
                        "type": "reparse",
                        "bytes": int(metadata.st_size),
                    }
                )
            elif child.is_dir(follow_symlinks=False):
                entries.append({"path": relative, "type": "directory"})
                visit(child_path)
            elif child.is_file(follow_symlinks=False):
                entries.append(
                    {
                        "path": relative,
                        "type": "file",
                        "bytes": int(metadata.st_size),
                        "sha256": sha256_file(child_path),
                    }
                )
            else:
                entries.append(
                    {
                        "path": relative,
                        "type": "other",
                        "bytes": int(metadata.st_size),
                    }
                )

    visit(root)
    entries.sort(key=lambda item: (item["path"].casefold(), item["path"]))
    value = {
        "root": relative_root,
        "exists": True,
        "file_count": sum(item["type"] == "file" for item in entries),
        "directory_count": sum(item["type"] == "directory" for item in entries),
        "reparse_count": sum(item["type"] == "reparse" for item in entries),
        "total_bytes": sum(
            int(item.get("bytes", 0)) for item in entries if item["type"] == "file"
        ),
        "entries": entries,
    }
    value["tree_sha256"] = sha256_bytes(canonical_json_bytes(entries))
    return value


def summarize_tree(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in (
            "root",
            "exists",
            "file_count",
            "directory_count",
            "reparse_count",
            "total_bytes",
            "tree_sha256",
        )
    }


def command_receipt(
    command: list[str],
    *,
    cwd: Path,
    label: str,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "label": label,
        "argv": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "stdout_sha256": sha256_bytes(completed.stdout.encode("utf-8")),
        "stderr_sha256": sha256_bytes(completed.stderr.encode("utf-8")),
    }


def qdata_command(qdata_path: Path, arguments: Iterable[str]) -> list[str]:
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        invocation = subprocess.list2cmdline([str(qdata_path), *arguments])
        return [comspec, "/d", "/s", "/c", invocation]
    return [str(qdata_path), *arguments]


def run_qdata_receipts(
    *,
    workspace: Path,
    qdata_path: Path,
    dataset_ids: dict[str, str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    pre_list = command_receipt(
        qdata_command(qdata_path, ["list", "--json"]),
        cwd=workspace,
        label="qdata_list_pre",
    )
    pre_doctor = command_receipt(
        qdata_command(qdata_path, ["doctor"]),
        cwd=workspace,
        label="qdata_doctor_pre",
    )
    if pre_list["returncode"] != 0:
        raise RuntimeError(f"qdata list failed: {pre_list}")
    try:
        listed = json.loads(pre_list["stdout"])
    except json.JSONDecodeError as exc:
        raise RuntimeError("qdata list did not return valid JSON") from exc
    if not isinstance(listed, list):
        raise RuntimeError("qdata list JSON root is not an array")

    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for item in listed:
        if isinstance(item, dict) and item.get("dataset_id") and item.get("snapshot_id"):
            by_dataset.setdefault(str(item["dataset_id"]), []).append(item)

    target_receipts: dict[str, dict[str, Any]] = {}
    for core_id, dataset_id in dataset_ids.items():
        snapshots = sorted(
            {
                str(item["snapshot_id"])
                for item in by_dataset.get(dataset_id, [])
                if item.get("snapshot_id")
            }
        )
        attempted_snapshots = snapshots or [ABSENT_SNAPSHOT_SENTINEL]
        resolve_receipts = []
        for snapshot_id in attempted_snapshots:
            resolve_receipts.append(
                command_receipt(
                    qdata_command(
                        qdata_path,
                        [
                            "resolve",
                            dataset_id,
                            snapshot_id,
                            "--intent",
                            "research",
                            "--json",
                        ],
                    ),
                    cwd=workspace,
                    label=f"qdata_resolve_research_{core_id}_{snapshot_id}",
                )
            )
        if snapshots and any(item["returncode"] == 0 for item in resolve_receipts):
            status = "PASS"
            reason = "at least one exact registered snapshot resolves for research"
        elif all(item["returncode"] == 2 for item in resolve_receipts):
            status = "FAIL"
            reason = (
                "dataset is not registered"
                if not snapshots
                else "all registered snapshots were rejected for research"
            )
        else:
            status = "UNKNOWN"
            reason = "qdata returned an unexpected execution status"
        target_receipts[core_id] = {
            "dataset_id": dataset_id,
            "registered_snapshots": snapshots,
            "status": status,
            "reason": reason,
            "resolve_receipts": resolve_receipts,
        }

    post_list = command_receipt(
        qdata_command(qdata_path, ["list", "--json"]),
        cwd=workspace,
        label="qdata_list_post",
    )
    post_doctor = command_receipt(
        qdata_command(qdata_path, ["doctor"]),
        cwd=workspace,
        label="qdata_doctor_post",
    )
    if post_list["returncode"] != 0:
        raise RuntimeError(f"post-audit qdata list failed: {post_list}")
    receipts = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "qdata_path": workspace_relative(qdata_path, workspace),
        "pre_list": pre_list,
        "pre_doctor": pre_doctor,
        "targets": target_receipts,
        "post_list": post_list,
        "post_doctor": post_doctor,
        "checks": {
            "list_stable": pre_list["stdout_sha256"] == post_list["stdout_sha256"],
            "doctor_stable": (
                pre_doctor["returncode"] == post_doctor["returncode"]
                and pre_doctor["stdout_sha256"] == post_doctor["stdout_sha256"]
                and pre_doctor["stderr_sha256"] == post_doctor["stderr_sha256"]
            ),
        },
    }
    return receipts, target_receipts


def load_gate_evidence(
    evidence_path: Path | None,
    *,
    workspace: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if evidence_path is None:
        return {}, None
    resolved = path_under_workspace(evidence_path, workspace, "gate evidence")
    value = load_json(resolved)
    if value.get("schema_version") != 1:
        raise ValueError("gate evidence schema_version must be 1")
    targets = value.get("targets")
    if not isinstance(targets, dict):
        raise ValueError("gate evidence targets must be an object")
    return targets, {
        "path": workspace_relative(resolved, workspace),
        "bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def verify_gate_receipt(
    receipt: dict[str, Any],
    *,
    workspace: Path,
    core_id: str,
    gate_id: str,
) -> dict[str, Any]:
    status_value = str(receipt.get("status", "")).upper()
    if status_value not in ALLOWED_GATE_STATUSES:
        raise ValueError(
            f"{core_id}/{gate_id} status must be PASS, FAIL or UNKNOWN"
        )
    evidence = receipt.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(f"{core_id}/{gate_id} must contain non-empty evidence")
    verified = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise ValueError(f"{core_id}/{gate_id} evidence[{index}] is not an object")
        assertion = item.get("assertion")
        if not isinstance(assertion, str) or not assertion.strip():
            raise ValueError(
                f"{core_id}/{gate_id} evidence[{index}] needs an assertion"
            )
        if status_value in {"PASS", "FAIL"}:
            source_path = item.get("path")
            expected_hash = item.get("sha256")
            if not isinstance(source_path, str) or not isinstance(expected_hash, str):
                raise ValueError(
                    f"{core_id}/{gate_id} PASS/FAIL evidence must bind path and sha256"
                )
            resolved = path_under_workspace(
                workspace / Path(source_path),
                workspace,
                f"{core_id}/{gate_id} evidence",
            )
            actual_hash = sha256_file(resolved)
            if actual_hash != expected_hash.lower():
                raise ValueError(
                    f"{core_id}/{gate_id} evidence hash mismatch: {source_path}"
                )
            item = {
                **item,
                "path": workspace_relative(resolved, workspace),
                "sha256": actual_hash,
                "hash_verified": True,
            }
        verified.append(item)
    return {
        "status": status_value,
        "evidence": verified,
        "evidence_source": "explicit_hash_bound_gate_evidence",
    }


def evaluate_quality_gates(
    *,
    core_id: str,
    contract_item: dict[str, Any],
    evidence_targets: dict[str, Any],
    workspace: Path,
    contract_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    core_evidence = evidence_targets.get(core_id, {})
    if not isinstance(core_evidence, dict):
        raise ValueError(f"gate evidence target {core_id} must be an object")
    evaluations = []
    for gate in contract_item.get("quality_gates", []):
        gate_id = str(gate.get("gate_id", ""))
        if not gate_id:
            raise ValueError(f"{core_id} quality gate has no gate_id")
        severity = str(gate.get("severity", "")).upper()
        explicit = core_evidence.get(gate_id)
        if explicit is None:
            evaluated = {
                "status": "UNKNOWN",
                "evidence_source": "no_gate_specific_receipt_supplied",
                "evidence": [
                    {
                        **contract_evidence,
                        "assertion": (
                            "No gate-specific PASS/FAIL receipt was supplied; "
                            "the contract definition alone is not failure evidence."
                        ),
                    }
                ],
            }
        elif not isinstance(explicit, dict):
            raise ValueError(f"{core_id}/{gate_id} receipt must be an object")
        else:
            evaluated = verify_gate_receipt(
                explicit,
                workspace=workspace,
                core_id=core_id,
                gate_id=gate_id,
            )
        evaluations.append(
            {
                "gate_id": gate_id,
                "severity": severity,
                "rule": gate.get("rule"),
                **evaluated,
                "blocks_admission": (
                    severity in {"BLOCK", "BLOCKING"}
                    and evaluated["status"] != "PASS"
                ),
            }
        )
    return evaluations


def check(
    status_value: str,
    *,
    assertion: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    if status_value not in ALLOWED_GATE_STATUSES:
        raise ValueError(f"invalid check status: {status_value}")
    return {
        "status": status_value,
        "assertion": assertion,
        "evidence": evidence,
        "blocks_admission": status_value != "PASS",
    }


def status_from_open_gap(
    value: Any,
    *,
    closure_is_formal: bool,
    label: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if value not in (None, "", False, [], {}):
        status_value = "FAIL"
        assertion = f"{label} remains open: {value}"
    elif closure_is_formal:
        status_value = "PASS"
        assertion = f"{label} is explicitly closed by the formal closure receipt"
    else:
        status_value = "UNKNOWN"
        assertion = f"{label} has no open text but lacks a formal closure receipt"
    return check(status_value, assertion=assertion, evidence=[evidence])


def publisher_negative_probe(
    *,
    workspace: Path,
    publisher_path: Path,
    catalog_path: Path,
    generator_path: Path,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(publisher_path),
        "publish",
        "--workspace-root",
        str(workspace),
        "--catalog",
        str(catalog_path),
        "--request",
        str(generator_path),
        "--raw-dir",
        str(workspace / "data" / "market"),
        "--curated-dir",
        str(workspace / "data" / "market"),
    ]
    receipt = command_receipt(
        command,
        cwd=workspace,
        label="formal_workspace_atomic_publisher_negative_probe",
    )
    expected_fragments = [
        "synthetic workspace sentinel must be a regular file",
        (
            "synthetic publisher requires an isolated workspace containing "
            ".qdata-d12a-synthetic-workspace"
        ),
    ]
    receipt["expected_rejection_observed"] = (
        receipt["returncode"] == 2
        and any(fragment in receipt["stderr"] for fragment in expected_fragments)
    )
    receipt["expected_rejection_fragments"] = expected_fragments
    return receipt


def input_record(path: Path, workspace: Path) -> dict[str, Any]:
    return {
        "path": workspace_relative(path, workspace),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def build_readme(decision: dict[str, Any]) -> str:
    rows = []
    for item in decision["targets"]:
        rows.append(
            "| {core_id} | {candidate_support} | {gate_status} | "
            "{missing_count} | {quality_unknown_or_failed} |".format(**item)
        )
    return "\n".join(
        [
            "# Selected CORE formal-admission gate",
            "",
            f"- Run: `{decision['run_id']}`",
            f"- Scope: `{', '.join(TARGETS)}`",
            f"- Result: `{decision['status']}`",
            f"- Formally admitted: `{decision['admitted_count']}/4`",
            "- Catalog writes by this audit: `0`",
            "- Snapshot publications by this audit: `0`",
            "- Quality promotions or waivers by this audit: `0`",
            "",
            "| CORE | Candidate support | Gate result | Phase-A missing items | "
            "Blocking quality receipts FAIL/UNKNOWN |",
            "|---|---|---|---:|---:|",
            *rows,
            "",
            "A `severity=BLOCK` contract entry is only a gate definition. It blocks "
            "only when its hash-bound receipt is `FAIL` or `UNKNOWN`; merely existing "
            "in the contract is not failure evidence. No gate-specific PASS receipts "
            "were supplied in this run, so required quality gates remain `UNKNOWN`.",
            "",
            "The audit independently pinned the formal catalog, source registry and "
            "catalog quality projection before and after; hashed `data/market/snapshots` "
            "and `data/published`; captured `qdata list/doctor/resolve --intent research` "
            "receipts; and executed a negative production-workspace publisher probe.",
            "",
            "The current atomic publisher rejected the formal workspace because it is "
            "synthetic-fixture-only. No candidate was copied into a formal snapshot, "
            "and the formal catalog, source registry, quality projection and formal "
            "payload roots remained unchanged.",
            "",
            "Next order: close CORE-01 permanent identity, then CORE-02 official "
            "calendar/session history, then CORE-03 source semantics and calibration, "
            "then CORE-06 historical tradability states and rule versions. Supply "
            "gate-specific hash-bound PASS receipts and separately implement and "
            "verify a real-data production publisher before another bounded admission.",
            "",
        ]
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    value.add_argument("--output-dir", type=Path, default=Path(DEFAULT_REPORT))
    value.add_argument("--replace", action="store_true")
    value.add_argument("--gate-evidence", type=Path)
    value.add_argument(
        "--closure-audit",
        type=Path,
        default=Path(
            "reports/data-governance/csmar-33pkg-local-closure-20260729/"
            "CLOSURE_AUDIT.json"
        ),
    )
    value.add_argument(
        "--phase-a-gap-matrix",
        type=Path,
        default=Path(
            "tmp/d2a04-core01-to-core06-candidate/"
            "d2a04-phase-a-20260728-v1/gap-matrix.json"
        ),
    )
    value.add_argument(
        "--source-registry",
        type=Path,
        default=Path(
            "data/catalog/contracts/source_admission_v1/"
            "current_source_registry.json"
        ),
    )
    value.add_argument(
        "--formal-catalog",
        type=Path,
        default=Path("data/catalog/datasets.json"),
    )
    value.add_argument(
        "--core-contract",
        type=Path,
        default=Path(
            "data/catalog/contracts/g_data_core_v1/"
            "core_01_08_market_reference.json"
        ),
    )
    value.add_argument(
        "--publisher-contract",
        type=Path,
        default=Path("data/catalog/contracts/atomic_publish_v1/README.md"),
    )
    value.add_argument(
        "--publisher-implementation",
        type=Path,
        default=Path("control/tools/atomic_snapshot_publish.py"),
    )
    value.add_argument(
        "--qdata-cmd",
        type=Path,
        default=Path("data/catalog/qdata.cmd"),
    )
    return value


def resolve_input(workspace: Path, value: Path, label: str) -> Path:
    candidate = value if value.is_absolute() else workspace / value
    return path_under_workspace(candidate, workspace, label)


def main() -> int:
    args = parser().parse_args()
    workspace = args.workspace_root.resolve(strict=True)
    output = (
        args.output_dir.resolve()
        if args.output_dir.is_absolute()
        else (workspace / args.output_dir).resolve()
    )
    require_report_path(output, workspace)
    reject_unexpected_report_residuals(output)
    if output.exists() and not args.replace:
        raise FileExistsError(f"refusing to overwrite report directory: {output}")

    generator_path = Path(__file__).resolve(strict=True)
    paths = {
        "closure_audit": resolve_input(
            workspace, args.closure_audit, "closure audit"
        ),
        "phase_a_gap_matrix": resolve_input(
            workspace, args.phase_a_gap_matrix, "phase-A gap matrix"
        ),
        "source_registry": resolve_input(
            workspace, args.source_registry, "source registry"
        ),
        "formal_catalog": resolve_input(
            workspace, args.formal_catalog, "formal catalog"
        ),
        "core_contract": resolve_input(
            workspace, args.core_contract, "CORE contract"
        ),
        "publisher_contract": resolve_input(
            workspace, args.publisher_contract, "publisher contract"
        ),
        "publisher_implementation": resolve_input(
            workspace, args.publisher_implementation, "publisher implementation"
        ),
        "qdata_cmd": resolve_input(workspace, args.qdata_cmd, "qdata command"),
        "generator": path_under_workspace(generator_path, workspace, "generator"),
    }

    pre_catalog_hash = sha256_file(paths["formal_catalog"])
    pre_registry_hash = sha256_file(paths["source_registry"])
    pre_catalog = load_json(paths["formal_catalog"])
    pre_quality = catalog_quality_projection(pre_catalog)
    formal_roots = {
        "market_snapshots": workspace / "data" / "market" / "snapshots",
        "published": workspace / "data" / "published",
    }
    pre_trees = {
        label: tree_state(path, workspace) for label, path in formal_roots.items()
    }

    closure = load_json(paths["closure_audit"])
    gaps = load_json(paths["phase_a_gap_matrix"])
    registry = load_json(paths["source_registry"])
    contract = load_json(paths["core_contract"])
    closure_by_core = closure_entries(closure)
    contract_by_core = core_contract_entries(contract)
    gap_by_core = gaps.get("datasets", {})
    if not isinstance(gap_by_core, dict):
        raise ValueError("phase-A gap matrix datasets must be an object")

    evidence_targets, gate_evidence_record = load_gate_evidence(
        (
            resolve_input(workspace, args.gate_evidence, "gate evidence")
            if args.gate_evidence
            else None
        ),
        workspace=workspace,
    )
    contract_record = input_record(paths["core_contract"], workspace)

    dataset_ids = {
        core_id: str(contract_by_core[core_id]["dataset_id"])
        for core_id in TARGETS
    }
    qdata_receipts, qdata_by_core = run_qdata_receipts(
        workspace=workspace,
        qdata_path=paths["qdata_cmd"],
        dataset_ids=dataset_ids,
    )

    publisher_probe = publisher_negative_probe(
        workspace=workspace,
        publisher_path=paths["publisher_implementation"],
        catalog_path=paths["formal_catalog"],
        generator_path=paths["generator"],
    )

    catalog_rows = catalog_entries(pre_catalog)
    catalog_by_dataset: dict[str, list[dict[str, Any]]] = {}
    for item in catalog_rows:
        catalog_by_dataset.setdefault(str(item["dataset_id"]), []).append(item)

    targets = []
    for core_id in TARGETS:
        closure_item = closure_by_core[core_id]
        contract_item = contract_by_core[core_id]
        gap_item = gap_by_core.get(core_id)
        if not isinstance(gap_item, dict):
            raise ValueError(f"phase-A gap matrix is missing {core_id}")
        dataset_id = dataset_ids[core_id]
        generic_gaps = closure_item.get("gaps", {})
        if not isinstance(generic_gaps, dict):
            raise ValueError(f"{core_id} closure gaps must be an object")
        missing, missing_corrections = correct_stale_phase_a_missing(
            core_id, list(gap_item.get("missing", []))
        )
        formal_closure = closure_item.get("formal_closure") is True
        catalog_matches = catalog_by_dataset.get(dataset_id, [])
        qdata_target = qdata_by_core[core_id]

        quality_evaluations = evaluate_quality_gates(
            core_id=core_id,
            contract_item=contract_item,
            evidence_targets=evidence_targets,
            workspace=workspace,
            contract_evidence=contract_record,
        )
        open_blockers = [
            blocker
            for blocker in contract_item.get("known_blockers", [])
            if str(blocker.get("status", "")).upper() not in {"CLOSED", "RESOLVED"}
        ]
        known_blocker_status = "FAIL" if open_blockers else "PASS"
        known_blocker_check = check(
            known_blocker_status,
            assertion=(
                f"{len(open_blockers)} contract blockers remain open"
                if open_blockers
                else "no contract blockers are open"
            ),
            evidence=[contract_record],
        )
        closure_record = input_record(paths["closure_audit"], workspace)
        gap_record = input_record(paths["phase_a_gap_matrix"], workspace)
        catalog_record = input_record(paths["formal_catalog"], workspace)
        qdata_evidence = {
            "path": "QDATA_RECEIPTS.json",
            "assertion": qdata_target["reason"],
            "receipt_status": qdata_target["status"],
        }
        admission_checks = {
            "formal_closure": check(
                "PASS" if formal_closure else "FAIL",
                assertion=(
                    "formal closure receipt is true"
                    if formal_closure
                    else "formal closure receipt is not true"
                ),
                evidence=[closure_record],
            ),
            "formal_catalog_identity": check(
                "PASS" if catalog_matches else "FAIL",
                assertion=(
                    f"{len(catalog_matches)} exact catalog identities exist"
                    if catalog_matches
                    else "dataset identity is absent from the formal catalog"
                ),
                evidence=[catalog_record],
            ),
            "qdata_research_resolution": check(
                qdata_target["status"],
                assertion=qdata_target["reason"],
                evidence=[qdata_evidence],
            ),
            "phase_a_missing_items": check(
                "FAIL" if missing else ("PASS" if formal_closure else "UNKNOWN"),
                assertion=(
                    f"{len(missing)} Phase-A items remain missing"
                    if missing
                    else (
                        "Phase-A missing list is empty and formal closure is true"
                        if formal_closure
                        else "Phase-A missing list is empty but formal closure is absent"
                    )
                ),
                evidence=[gap_record],
            ),
            "rights": status_from_open_gap(
                generic_gaps.get("rights"),
                closure_is_formal=formal_closure,
                label="source and retention rights",
                evidence=closure_record,
            ),
            "schema_semantics": status_from_open_gap(
                generic_gaps.get("schema"),
                closure_is_formal=formal_closure,
                label="schema, units, enums and sentinel semantics",
                evidence=closure_record,
            ),
            "pit": status_from_open_gap(
                generic_gaps.get("pit"),
                closure_is_formal=formal_closure,
                label="PIT available_at and anti-lookahead",
                evidence=closure_record,
            ),
            "revision_chain": status_from_open_gap(
                generic_gaps.get("revision"),
                closure_is_formal=formal_closure,
                label="first-publication and revision chain",
                evidence=closure_record,
            ),
            "official_cross_source_quality": status_from_open_gap(
                generic_gaps.get("quality"),
                closure_is_formal=formal_closure,
                label="official coverage and cross-source quality",
                evidence=closure_record,
            ),
            "known_contract_blockers": known_blocker_check,
        }
        blocking_reasons = [
            f"check:{name}:{item['status']}"
            for name, item in admission_checks.items()
            if item["blocks_admission"]
        ]
        blocking_reasons.extend(
            f"quality_gate:{item['gate_id']}:{item['status']}"
            for item in quality_evaluations
            if item["blocks_admission"]
        )
        formally_admitted = not blocking_reasons
        gate_status = (
            "ADMITTED__AUDIT_PASS__RESEARCH_RESOLVABLE"
            if formally_admitted
            else "BLOCKED__FAIL_CLOSED__NOT_ADMITTED"
        )
        quality_unknown_or_failed = sum(
            item["blocks_admission"] for item in quality_evaluations
        )
        targets.append(
            {
                "core_id": core_id,
                "dataset_id": dataset_id,
                "candidate_support": closure_item["candidate_support"],
                "candidate_summary": closure_item["summary"],
                "phase_a_status": gap_item["status"],
                "preserved_evidence": gap_item.get("preserved", []),
                "phase_a_missing_corrections": missing_corrections,
                "missing": missing,
                "missing_count": len(missing),
                "generic_gaps": generic_gaps,
                "quality_gate_evaluations": quality_evaluations,
                "quality_unknown_or_failed": quality_unknown_or_failed,
                "open_contract_blockers": open_blockers,
                "admission_checks": admission_checks,
                "blocking_reasons": blocking_reasons,
                "gate_status": gate_status,
                "formally_admitted": formally_admitted,
                "formal_catalog_write_by_this_audit": False,
                "snapshot_published_by_this_audit": False,
                "quality_promoted_or_waived_by_this_audit": False,
            }
        )

    post_catalog_hash = sha256_file(paths["formal_catalog"])
    post_registry_hash = sha256_file(paths["source_registry"])
    post_catalog = load_json(paths["formal_catalog"])
    post_quality = catalog_quality_projection(post_catalog)
    post_trees = {
        label: tree_state(path, workspace) for label, path in formal_roots.items()
    }
    tree_checks = {
        label: pre_trees[label]["tree_sha256"] == post_trees[label]["tree_sha256"]
        for label in formal_roots
    }
    state_checks = {
        "catalog_unchanged": pre_catalog_hash == post_catalog_hash,
        "source_registry_unchanged": pre_registry_hash == post_registry_hash,
        "quality_projection_unchanged": pre_quality["sha256"]
        == post_quality["sha256"],
        "formal_roots_unchanged": all(tree_checks.values()),
        "qdata_list_stable": qdata_receipts["checks"]["list_stable"],
        "qdata_doctor_stable": qdata_receipts["checks"]["doctor_stable"],
        "publisher_formal_workspace_rejected": publisher_probe[
            "expected_rejection_observed"
        ],
    }
    if not all(state_checks.values()):
        raise RuntimeError(f"formal-state verification failed: {state_checks}")

    admitted_count = sum(item["formally_admitted"] for item in targets)
    status_value = (
        "BLOCKED__0_OF_4_ADMITTED__FAIL_CLOSED"
        if admitted_count == 0
        else f"AUDITED__{admitted_count}_OF_4_FORMALLY_ADMITTED"
    )
    now = datetime.now(timezone.utc).isoformat()
    decision = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "work_package": (
            "L1-D2B_SELECTED_CORE_FORMAL_ADMISSION_GATE__"
            "CORE01_CORE02_CORE03_CORE06"
        ),
        "executed_at_utc": now,
        "authorization": {
            "user_start_evidence": (
                "User explicitly assigned formal admission to Codex on 2026-07-30 "
                "while manually downloading additional CSMAR candidates."
            ),
            "scope": list(TARGETS),
            "gate_waiver_authorized": False,
            "quality_waiver_authorized": False,
        },
        "status": status_value,
        "admitted_count": admitted_count,
        "target_count": len(TARGETS),
        "targets": targets,
        "gate_evidence_bundle": gate_evidence_record,
        "global_state": {
            "d2a04_completion_gate_passed": gaps.get("completion_gate_passed"),
            "d2b_blocked_on_entry": gaps.get("D2B_blocked"),
            "registry_formal_counts": registry.get("formal_counts", {}),
            "production_real_data_publisher_available": False,
            "publisher_negative_probe_passed": publisher_probe[
                "expected_rejection_observed"
            ],
        },
        "mutations_by_this_audit": {
            "catalog_write": False,
            "snapshot_publication": False,
            "quality_promotion_or_waiver": False,
            "candidate_modification": False,
        },
        "next_action": (
            "SUPPLY_HASH_BOUND_PASS_RECEIPTS_FOR_REQUIRED_GATES__"
            "CLOSE_SOURCE_RIGHTS_AND_CORE01_TO_CORE06_GAPS__"
            "IMPLEMENT_AND_VERIFY_REAL_DATA_PRODUCTION_PUBLISHER__"
            "THEN_REQUEST_NEW_BOUNDED_ADMISSION_RUN"
        ),
    }

    output.mkdir(parents=True, exist_ok=True)
    decision_path = output / "GATE_DECISION.json"
    qdata_path = output / "QDATA_RECEIPTS.json"
    readme_path = output / "README.md"
    verification_path = output / "VERIFICATION.json"
    write_json(decision_path, decision)
    write_json(qdata_path, qdata_receipts)
    readme_path.write_text(
        build_readme(decision),
        encoding="utf-8",
        newline="\n",
    )

    verification = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "status": (
            "PASS__ADMISSION_REJECTED_FAIL_CLOSED__FORMAL_STATE_UNCHANGED"
        ),
        "formal_state": {
            "catalog": {
                "sha256_before": pre_catalog_hash,
                "sha256_after": post_catalog_hash,
            },
            "source_registry": {
                "sha256_before": pre_registry_hash,
                "sha256_after": post_registry_hash,
            },
            "catalog_quality_projection": {
                "before": pre_quality,
                "after": post_quality,
            },
            "roots": {
                label: {
                    "before": summarize_tree(pre_trees[label]),
                    "after": summarize_tree(post_trees[label]),
                    "unchanged": tree_checks[label],
                }
                for label in formal_roots
            },
        },
        "publisher_negative_probe": publisher_probe,
        "inputs": {
            label: input_record(path, workspace) for label, path in paths.items()
        },
        "gate_evidence_input": gate_evidence_record,
        "checks": {
            "target_scope_exact": [item["core_id"] for item in targets]
            == list(TARGETS),
            "gate_statuses_evidence_bound": all(
                item["quality_gate_evaluations"]
                and all(
                    gate["status"] in ALLOWED_GATE_STATUSES and gate["evidence"]
                    for gate in item["quality_gate_evaluations"]
                )
                for item in targets
            ),
            "quality_gate_definitions_not_auto_failed": all(
                all(
                    gate["evidence_source"]
                    in {
                        "no_gate_specific_receipt_supplied",
                        "explicit_hash_bound_gate_evidence",
                    }
                    for gate in item["quality_gate_evaluations"]
                )
                for item in targets
            ),
            "target_statuses_match_evidence": all(
                (
                    bool(item["blocking_reasons"])
                    and item["gate_status"]
                    == "BLOCKED__FAIL_CLOSED__NOT_ADMITTED"
                    and item["formally_admitted"] is False
                )
                or (
                    not item["blocking_reasons"]
                    and item["gate_status"]
                    == "ADMITTED__AUDIT_PASS__RESEARCH_RESOLVABLE"
                    and item["formally_admitted"] is True
                )
                for item in targets
            ),
            "admitted_count_matches_targets": admitted_count
            == sum(item["formally_admitted"] for item in targets),
            **state_checks,
        },
    }
    if not all(verification["checks"].values()):
        raise RuntimeError(f"verification failed: {verification['checks']}")
    write_json(verification_path, verification)

    payload_records = [
        {
            "path": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(
            (decision_path, qdata_path, readme_path, verification_path),
            key=lambda value: value.name,
        )
    ]
    report_manifest = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "generator": input_record(paths["generator"], workspace),
        "manifest_policy": {
            "self_excluded_from_files": REPORT_MANIFEST_NAME,
            "allowed_report_files": sorted(ALLOWED_REPORT_FILES),
            "unregistered_residuals_rejected": True,
        },
        "files": payload_records,
        "canonical_payload_bundle_sha256": sha256_bytes(
            canonical_json_bytes(payload_records)
        ),
    }
    manifest_path = output / REPORT_MANIFEST_NAME
    write_json(manifest_path, report_manifest)

    final_names = {item.name for item in output.iterdir() if item.is_file()}
    unexpected_final = sorted(final_names ^ ALLOWED_REPORT_FILES)
    if unexpected_final:
        raise RuntimeError(f"final report file set mismatch: {unexpected_final}")
    if report_manifest["generator"]["sha256"] != sha256_file(paths["generator"]):
        raise RuntimeError("generator changed during report generation")
    for record in payload_records:
        payload = output / record["path"]
        if sha256_file(payload) != record["sha256"]:
            raise RuntimeError(f"report payload hash drifted: {payload}")

    print(
        json.dumps(
            {
                "status": decision["status"],
                "admitted_count": admitted_count,
                "target_count": len(TARGETS),
                "catalog_sha256": post_catalog_hash,
                "source_registry_sha256": post_registry_hash,
                "quality_projection_sha256": post_quality["sha256"],
                "report": workspace_relative(output, workspace),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
