#!/usr/bin/env python3
"""Separate selected-CORE admission readiness from post-publish validation.

This audit is intentionally read-only outside one selected report directory.
It does not publish a snapshot, write the formal catalog, promote quality, or
create an authorization receipt.

The legacy selected-CORE audit is preserved as a post-admission audit.  Its
``formal_catalog_identity`` and ``qdata_research_resolution`` checks are valid
after publication, but are circular if they are required before a new identity
may be published.  This tool therefore implements two explicit stages:

Stage A
    Pre-admission evidence readiness.  Rights, schema semantics, PIT,
    revision history, official cross-source evidence, closure receipts and
    blocking quality gates must all pass.  Formal catalog presence and qdata
    research resolution are deliberately excluded.

Stage B
    Only after every Stage-A target passes and a separately supplied,
    hash-bound user authorization receipt is valid may a separate production
    publisher run.  After that publication, this audit verifies the exact
    immutable catalog identity and exact ``qdata --intent research`` result.

The current atomic publisher is synthetic-fixture-only.  This tool never calls
it for real data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TARGETS = ("CORE-01", "CORE-02", "CORE-03", "CORE-06")
RUN_ID = "selected-core-two-stage-admission-architecture-audit-20260730-v1"
DEFAULT_LEGACY_DECISION = (
    "reports/data-governance/"
    "d2b-selected-core-formal-admission-gate-20260730-v1/"
    "GATE_DECISION.json"
)
DEFAULT_OUTPUT = (
    "reports/data-governance/"
    "selected-core-two-stage-admission-architecture-audit-20260730-v1"
)
AUTHORIZATION_ACTION = "AUTHORIZE_SELECTED_CORE_ATOMIC_PUBLISH"

STAGE_A_CHECKS = (
    "phase_a_missing_items",
    "rights",
    "schema_semantics",
    "pit",
    "revision_chain",
    "official_cross_source_quality",
    "known_contract_blockers",
)
STAGE_B_POST_PUBLISH_CHECKS = (
    "formal_catalog_identity",
    "qdata_research_resolution",
)
LEGACY_POST_ADMISSION_CHECKS = (
    "formal_closure",
    *STAGE_B_POST_PUBLISH_CHECKS,
)
FORBIDDEN_PRE_ADMISSION_CHECKS = frozenset(LEGACY_POST_ADMISSION_CHECKS)

REPORT_MANIFEST_NAME = "REPORT_MANIFEST.json"
REPORT_PAYLOAD_FILES = {
    "README.md",
    "STATE_MACHINE.json",
    "VERIFICATION.json",
}
ALLOWED_REPORT_FILES = REPORT_PAYLOAD_FILES | {REPORT_MANIFEST_NAME}
IDENTITY_COMPONENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$"
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


def resolve_input(workspace: Path, value: Path, label: str) -> Path:
    candidate = value if value.is_absolute() else workspace / value
    return path_under_workspace(candidate, workspace, label)


def has_reparse_flag(path: Path) -> bool:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse)


def is_safe_identity_component(value: Any) -> bool:
    return (
        isinstance(value, str)
        and IDENTITY_COMPONENT_RE.fullmatch(value) is not None
    )


def require_identity_component(value: Any, label: str) -> str:
    if not is_safe_identity_component(value):
        raise ValueError(f"{label} is not a safe identity component")
    return value


def input_record(path: Path, workspace: Path) -> dict[str, Any]:
    return {
        "path": workspace_relative(path, workspace),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def require_report_path(path: Path, workspace: Path) -> None:
    report_parent = (workspace / "reports" / "data-governance").resolve()
    absolute = Path(os.path.abspath(path))
    if Path(os.path.commonpath([absolute, report_parent])) != report_parent:
        raise ValueError(
            f"report output is not lexically under reports/data-governance: "
            f"{absolute}"
        )
    relative = absolute.relative_to(report_parent)
    cursor = report_parent
    for part in relative.parts:
        cursor /= part
        if cursor.exists() and has_reparse_flag(cursor):
            raise ValueError(
                f"report output path contains a reparse/symlink: {cursor}"
            )
    resolved = absolute.resolve()
    if Path(os.path.commonpath([resolved, report_parent])) != report_parent:
        raise ValueError(f"report output escapes reports/data-governance: {resolved}")
    if resolved == report_parent:
        raise ValueError("report output must be a child of reports/data-governance")


def reject_unexpected_report_residuals(output: Path) -> None:
    if not output.exists():
        return
    if has_reparse_flag(output) or not output.is_dir():
        raise ValueError(f"report output is not a directory: {output}")
    rejected = []
    for item in output.iterdir():
        reason = None
        if item.name not in ALLOWED_REPORT_FILES:
            reason = "unregistered"
        elif has_reparse_flag(item):
            reason = "reparse_or_symlink"
        elif not item.is_file():
            reason = "not_regular_file"
        elif item.stat().st_nlink != 1:
            reason = f"hardlink_count_{item.stat().st_nlink}"
        if reason is not None:
            rejected.append(f"{item.name}:{reason}")
    if rejected:
        raise ValueError(
            "refusing report replacement because unregistered residuals exist: "
            f"{sorted(rejected)}"
        )


def detect_pre_admission_cycle(check_names: Iterable[str]) -> dict[str, Any]:
    names = tuple(check_names)
    offenders = sorted(set(names) & FORBIDDEN_PRE_ADMISSION_CHECKS)
    return {
        "cycle_risk_detected": bool(offenders),
        "offending_checks": offenders,
        "reason": (
            "Pre-admission requires state that can exist only after admission "
            "and publication."
            if offenders
            else "No post-publish catalog or qdata result is required by Stage A."
        ),
    }


def _require_check(
    admission_checks: dict[str, Any],
    name: str,
    *,
    core_id: str,
) -> dict[str, Any]:
    value = admission_checks.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{core_id} legacy decision is missing check {name}")
    status_value = str(value.get("status", "")).upper()
    if status_value not in {"PASS", "FAIL", "UNKNOWN"}:
        raise ValueError(f"{core_id}/{name} has invalid status: {status_value!r}")
    return deepcopy(value)


def _reclassify_stage_a_check(
    *,
    target: dict[str, Any],
    admission_checks: dict[str, Any],
    name: str,
    core_id: str,
) -> dict[str, Any]:
    """Remove the legacy formal-closure dependency from evidence checks.

    The legacy generator marks a check UNKNOWN when no gap text remains but
    the CORE identity is not yet in the formal plan.  For Stage A that is the
    wrong dependency direction.  Here, the already hash-bound legacy report is
    interpreted directly: an explicit empty missing/gap/blocker collection is
    PASS, a non-empty collection is FAIL, and an absent collection is UNKNOWN.
    """

    legacy = _require_check(admission_checks, name, core_id=core_id)
    status_value = "UNKNOWN"
    detail: Any = None
    basis = ""
    if name == "phase_a_missing_items":
        if "missing" in target and isinstance(target["missing"], list):
            detail = deepcopy(target["missing"])
            status_value = "PASS" if not detail else "FAIL"
            basis = "explicit Phase-A missing collection"
        else:
            basis = "Phase-A missing collection is absent or invalid"
    elif name == "known_contract_blockers":
        if "open_contract_blockers" in target and isinstance(
            target["open_contract_blockers"], list
        ):
            detail = deepcopy(target["open_contract_blockers"])
            status_value = "PASS" if not detail else "FAIL"
            basis = "explicit open-contract-blocker collection"
        else:
            basis = "open-contract-blocker collection is absent or invalid"
    else:
        gap_key = {
            "rights": "rights",
            "schema_semantics": "schema",
            "pit": "pit",
            "revision_chain": "revision",
            "official_cross_source_quality": "quality",
        }[name]
        generic_gaps = target.get("generic_gaps")
        if isinstance(generic_gaps, dict) and gap_key in generic_gaps:
            detail = deepcopy(generic_gaps[gap_key])
            status_value = (
                "PASS" if detail in (None, "", False, [], {}) else "FAIL"
            )
            basis = f"explicit generic_gaps.{gap_key} value"
        else:
            basis = f"generic_gaps.{gap_key} is absent"

    return {
        **legacy,
        "legacy_status": legacy["status"],
        "status": status_value,
        "assertion": (
            f"Stage-A evidence classification from {basis}; "
            f"open_detail={detail!r}"
        ),
        "blocks_admission": status_value != "PASS",
        "stage_a_reclassified_without_formal_catalog_dependency": True,
    }


def classify_legacy_target(target: dict[str, Any]) -> dict[str, Any]:
    """Project one legacy target into the non-circular two-stage model."""

    core_id = str(target.get("core_id", ""))
    if core_id not in TARGETS:
        raise ValueError(
            f"invalid selected-CORE target: "
            f"{core_id!r}/{target.get('dataset_id')!r}"
        )
    dataset_id = require_identity_component(
        target.get("dataset_id"), f"{core_id} dataset_id"
    )
    if not dataset_id:
        raise ValueError(f"invalid selected-CORE target: {core_id!r}/{dataset_id!r}")
    checks = target.get("admission_checks")
    if not isinstance(checks, dict):
        raise ValueError(f"{core_id} admission_checks must be an object")

    stage_a_checks = {
        name: _reclassify_stage_a_check(
            target=target,
            admission_checks=checks,
            name=name,
            core_id=core_id,
        )
        for name in STAGE_A_CHECKS
    }
    stage_b_checks = {
        name: _require_check(checks, name, core_id=core_id)
        for name in LEGACY_POST_ADMISSION_CHECKS
    }

    quality_gates = target.get("quality_gate_evaluations")
    if not isinstance(quality_gates, list):
        raise ValueError(f"{core_id} quality_gate_evaluations must be an array")
    blocking_quality = []
    for item in quality_gates:
        if not isinstance(item, dict):
            raise ValueError(f"{core_id} quality gate entry must be an object")
        status_value = str(item.get("status", "")).upper()
        if status_value not in {"PASS", "FAIL", "UNKNOWN"}:
            raise ValueError(
                f"{core_id}/{item.get('gate_id')} has invalid quality status"
            )
        # Preserve every gate whose contract severity is blocking, including
        # PASS gates.  Otherwise the authorization digest would omit the very
        # PASS receipts that make Stage A ready.
        severity = str(item.get("severity", "")).upper()
        if severity in {"BLOCK", "BLOCKING"}:
            blocking_quality.append(deepcopy(item))

    stage_a_blockers = [
        f"check:{name}:{item['status']}"
        for name, item in stage_a_checks.items()
        if str(item["status"]).upper() != "PASS"
    ]
    stage_a_blockers.extend(
        f"quality_gate:{item.get('gate_id')}:{item.get('status')}"
        for item in blocking_quality
        if str(item.get("status", "")).upper() != "PASS"
    )
    ready = not stage_a_blockers

    return {
        "core_id": core_id,
        "dataset_id": dataset_id,
        "candidate_support": target.get("candidate_support"),
        "stage_a": {
            "status": (
                "PASS__PRE_ADMISSION_EVIDENCE_READY"
                if ready
                else "BLOCKED__PRE_ADMISSION_EVIDENCE_NOT_READY"
            ),
            "ready": ready,
            "checks": stage_a_checks,
            "blocking_quality_gates": blocking_quality,
            "blocking_reasons": stage_a_blockers,
        },
        "stage_b_legacy_observations": {
            "classification": "POST_PUBLISH_ONLY__NOT_STAGE_A_INPUT",
            "checks": stage_b_checks,
        },
        "legacy_result_preserved": {
            "gate_status": target.get("gate_status"),
            "formally_admitted": target.get("formally_admitted"),
            "blocking_reasons": deepcopy(target.get("blocking_reasons", [])),
        },
    }


def evaluate_phase_a(
    legacy_decision: dict[str, Any],
    *,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    raw_targets = legacy_decision.get("targets")
    if not isinstance(raw_targets, list):
        raise ValueError("legacy gate decision targets must be an array")
    by_core = {
        str(item.get("core_id")): item
        for item in raw_targets
        if isinstance(item, dict) and item.get("core_id") in TARGETS
    }
    missing = sorted(set(TARGETS) - set(by_core))
    if missing:
        raise ValueError(f"legacy gate decision is missing targets: {missing}")

    targets = [classify_legacy_target(by_core[core_id]) for core_id in TARGETS]
    ready_count = sum(item["stage_a"]["ready"] for item in targets)
    digest_projection = [
        {
            "core_id": item["core_id"],
            "dataset_id": item["dataset_id"],
            "status": item["stage_a"]["status"],
            "checks": {
                name: value["status"]
                for name, value in item["stage_a"]["checks"].items()
            },
            "blocking_quality_gates": [
                {
                    "gate_id": gate.get("gate_id"),
                    "status": gate.get("status"),
                }
                for gate in item["stage_a"]["blocking_quality_gates"]
            ],
            "blocking_reasons": item["stage_a"]["blocking_reasons"],
        }
        for item in targets
    ]
    digest_bundle = {
        "source_sha256": source_sha256,
        "targets": digest_projection,
    }
    bundle_sha256 = sha256_bytes(canonical_json_bytes(digest_bundle))
    return {
        "status": (
            "PASS__ALL_TARGETS_PRE_ADMISSION_READY"
            if ready_count == len(TARGETS)
            else f"BLOCKED__{ready_count}_OF_{len(TARGETS)}_PRE_ADMISSION_READY"
        ),
        "ready_count": ready_count,
        "target_count": len(TARGETS),
        "all_ready": ready_count == len(TARGETS),
        "bundle_sha256": bundle_sha256,
        "bundle_source_sha256": source_sha256,
        "digest_projection": digest_projection,
        "targets": targets,
    }


def validate_authorization_value(
    receipt: dict[str, Any],
    *,
    phase_a_bundle_sha256: str,
) -> dict[str, Any]:
    errors = []
    if receipt.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if receipt.get("action") != AUTHORIZATION_ACTION:
        errors.append(f"action must be {AUTHORIZATION_ACTION}")
    if sorted(receipt.get("scope", [])) != sorted(TARGETS):
        errors.append("scope must exactly match selected CORE targets")
    if receipt.get("phase_a_bundle_sha256") != phase_a_bundle_sha256:
        errors.append("phase_a_bundle_sha256 does not match current Stage A")
    if receipt.get("authorized_by") != "USER":
        errors.append("authorized_by must be USER")
    if not isinstance(receipt.get("authorized_at_utc"), str) or not receipt.get(
        "authorized_at_utc"
    ):
        errors.append("authorized_at_utc must be non-empty")
    statement = receipt.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        errors.append("statement must be non-empty")
    return {
        "status": "PASS" if not errors else "FAIL",
        "valid": not errors,
        "errors": errors,
        "action": receipt.get("action"),
        "scope": receipt.get("scope"),
        "phase_a_bundle_sha256": receipt.get("phase_a_bundle_sha256"),
    }


def validate_publish_receipt_value(
    receipt: dict[str, Any],
    *,
    phase_a_bundle_sha256: str,
    authorization_receipt_sha256: str,
    phase_a_targets: list[dict[str, Any]],
) -> dict[str, Any]:
    errors = []
    if receipt.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if receipt.get("status") != "PUBLISHED":
        errors.append("status must be PUBLISHED")
    if receipt.get("phase_a_bundle_sha256") != phase_a_bundle_sha256:
        errors.append("phase_a_bundle_sha256 does not match current Stage A")
    if (
        receipt.get("authorization_receipt_sha256")
        != authorization_receipt_sha256
    ):
        errors.append("authorization_receipt_sha256 mismatch")

    raw_targets = receipt.get("targets")
    if not isinstance(raw_targets, list):
        errors.append("targets must be an array")
        raw_targets = []
    by_core: dict[str, dict[str, Any]] = {}
    for item in raw_targets:
        if not isinstance(item, dict):
            errors.append("publish target must be an object")
            continue
        core_id = str(item.get("core_id", ""))
        if core_id in by_core:
            errors.append(f"duplicate publish target: {core_id}")
        by_core[core_id] = item
    if sorted(by_core) != sorted(TARGETS):
        errors.append("publish targets must exactly match selected CORE targets")

    expected_datasets = {
        item["core_id"]: item["dataset_id"] for item in phase_a_targets
    }
    verified_targets = []
    for core_id in TARGETS:
        item = by_core.get(core_id)
        if item is None:
            continue
        dataset_id = str(item.get("dataset_id", ""))
        snapshot_id = str(item.get("snapshot_id", ""))
        content_hash = str(item.get("content_hash", ""))
        if dataset_id != expected_datasets[core_id]:
            errors.append(f"{core_id} dataset_id mismatch")
        if not is_safe_identity_component(dataset_id):
            errors.append(f"{core_id} dataset_id is not a safe identity component")
        if (
            not is_safe_identity_component(snapshot_id)
            or snapshot_id.lower() == "latest"
        ):
            errors.append(f"{core_id} needs an exact non-latest snapshot_id")
        if len(content_hash) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in content_hash
        ):
            errors.append(f"{core_id} needs a sha256 content_hash")
        verified_targets.append(
            {
                "core_id": core_id,
                "dataset_id": dataset_id,
                "snapshot_id": snapshot_id,
                "content_hash": content_hash.lower(),
            }
        )
    return {
        "status": "PASS" if not errors else "FAIL",
        "valid": not errors,
        "errors": errors,
        "targets": verified_targets,
    }


def evaluate_catalog_post_publish(
    *,
    publish_targets: list[dict[str, Any]],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    rows = catalog.get("datasets")
    if not isinstance(rows, list):
        raise ValueError("formal catalog datasets must be an array")
    results = []
    for target in publish_targets:
        matches = [
            item
            for item in rows
            if isinstance(item, dict)
            and item.get("dataset_id") == target["dataset_id"]
            and item.get("snapshot_id") == target["snapshot_id"]
        ]
        errors = []
        if len(matches) != 1:
            errors.append(f"expected one exact catalog row, observed {len(matches)}")
        else:
            row = matches[0]
            if row.get("mutability") != "immutable":
                errors.append("catalog row is not immutable")
            if row.get("content_hash") != target["content_hash"]:
                errors.append("catalog content_hash does not match publish receipt")
        results.append(
            {
                **target,
                "status": "PASS" if not errors else "FAIL",
                "errors": errors,
                "match_count": len(matches),
            }
        )
    return {
        "status": (
            "PASS"
            if results and all(item["status"] == "PASS" for item in results)
            else "FAIL"
        ),
        "targets": results,
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
    argument_list = [str(value) for value in arguments]
    if qdata_path.suffix.lower() in {".cmd", ".bat"}:
        dataset_cli = qdata_path.with_name("dataset_cli.py")
        if (
            not dataset_cli.is_file()
            or has_reparse_flag(dataset_cli)
        ):
            raise ValueError(
                f"qdata batch wrapper has no ordinary sibling dataset_cli.py: "
                f"{qdata_path}"
            )
        return [
            sys.executable,
            "-X",
            "utf8",
            str(dataset_cli),
            *argument_list,
        ]
    return [str(qdata_path), *argument_list]


def run_exact_qdata_post_publish(
    *,
    workspace: Path,
    qdata_path: Path,
    publish_targets: list[dict[str, Any]],
) -> dict[str, Any]:
    results = []
    for target in publish_targets:
        dataset_id = require_identity_component(
            target.get("dataset_id"),
            f"{target.get('core_id')} dataset_id",
        )
        snapshot_id = require_identity_component(
            target.get("snapshot_id"),
            f"{target.get('core_id')} snapshot_id",
        )
        if snapshot_id.lower() == "latest":
            raise ValueError(
                f"{target.get('core_id')} snapshot_id cannot be latest"
            )
        receipt = command_receipt(
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
            label=f"post_publish_qdata_{target['core_id']}",
        )
        results.append(
            {
                **target,
                "status": "PASS" if receipt["returncode"] == 0 else "FAIL",
                "receipt": receipt,
            }
        )
    return {
        "status": (
            "PASS"
            if results and all(item["status"] == "PASS" for item in results)
            else "FAIL"
        ),
        "targets": results,
    }


def evaluate_post_publish_validation(
    *,
    publish_targets: list[dict[str, Any]],
    catalog_validation: dict[str, Any],
    qdata_validation: dict[str, Any],
) -> dict[str, Any]:
    expected = {
        item["core_id"]: (item["dataset_id"], item["snapshot_id"])
        for item in publish_targets
    }
    raw_qdata_targets = qdata_validation.get("targets")
    qdata_errors = []
    if not isinstance(raw_qdata_targets, list):
        qdata_errors.append("qdata targets must be an array")
        raw_qdata_targets = []
    observed: dict[str, tuple[str, str, str]] = {}
    for item in raw_qdata_targets:
        if not isinstance(item, dict):
            qdata_errors.append("qdata target must be an object")
            continue
        core_id = str(item.get("core_id", ""))
        if core_id in observed:
            qdata_errors.append(f"duplicate qdata target: {core_id}")
        observed[core_id] = (
            str(item.get("dataset_id", "")),
            str(item.get("snapshot_id", "")),
            str(item.get("status", "")),
        )
        receipt = item.get("receipt")
        if isinstance(receipt, dict) and receipt.get("returncode") != 0:
            qdata_errors.append(f"{core_id} qdata returncode is not zero")
    if sorted(observed) != sorted(expected):
        qdata_errors.append("qdata targets do not exactly match publish targets")
    for core_id, (dataset_id, snapshot_id) in expected.items():
        value = observed.get(core_id)
        if value is None:
            continue
        if value[:2] != (dataset_id, snapshot_id):
            qdata_errors.append(f"{core_id} qdata identity mismatch")
        if value[2] != "PASS":
            qdata_errors.append(f"{core_id} qdata status is not PASS")
    exact_qdata = {
        **qdata_validation,
        "exact_identity_errors": qdata_errors,
        "exact_identity_match": not qdata_errors,
    }
    passed = (
        catalog_validation.get("status") == "PASS"
        and qdata_validation.get("status") == "PASS"
        and not qdata_errors
    )
    return {
        "status": (
            "PASS__EXACT_CATALOG_AND_QDATA_RESEARCH_RESOLUTION"
            if passed
            else "FAIL__POST_PUBLISH_VALIDATION"
        ),
        "valid": passed,
        "catalog": catalog_validation,
        "qdata": exact_qdata,
    }


def evaluate_phase_b(
    *,
    phase_a: dict[str, Any],
    authorization: dict[str, Any] | None,
    authorization_record: dict[str, Any] | None,
    publish_receipt: dict[str, Any] | None,
    publish_receipt_record: dict[str, Any] | None,
    catalog: dict[str, Any] | None = None,
    qdata_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not phase_a["all_ready"]:
        return {
            "status": "BLOCKED__PHASE_A_NOT_READY",
            "authorization": {"status": "NOT_EVALUATED"},
            "publication": {"status": "NOT_AUTHORIZED__NOT_EXECUTED"},
            "post_publish_validation": {"status": "NOT_RUN"},
        }

    if authorization is None:
        return {
            "status": "WAITING__EXPLICIT_PHASE_B_AUTHORIZATION",
            "authorization": {"status": "NOT_PROVIDED"},
            "publication": {"status": "NOT_AUTHORIZED__NOT_EXECUTED"},
            "post_publish_validation": {"status": "NOT_RUN"},
        }
    authorization_result = validate_authorization_value(
        authorization,
        phase_a_bundle_sha256=phase_a["bundle_sha256"],
    )
    authorization_result["record"] = authorization_record
    if not authorization_result["valid"]:
        return {
            "status": "BLOCKED__INVALID_PHASE_B_AUTHORIZATION",
            "authorization": authorization_result,
            "publication": {"status": "NOT_AUTHORIZED__NOT_EXECUTED"},
            "post_publish_validation": {"status": "NOT_RUN"},
        }

    if publish_receipt is None:
        return {
            "status": "READY_FOR_SEPARATE_ATOMIC_PUBLISH__NOT_EXECUTED",
            "authorization": authorization_result,
            "publication": {
                "status": "AUTHORIZED__NO_PUBLISH_RECEIPT",
                "publisher_must_be_separate": True,
            },
            "post_publish_validation": {"status": "NOT_RUN"},
        }
    if authorization_record is None:
        raise ValueError("authorization record is required with a publish receipt")
    publication_result = validate_publish_receipt_value(
        publish_receipt,
        phase_a_bundle_sha256=phase_a["bundle_sha256"],
        authorization_receipt_sha256=authorization_record["sha256"],
        phase_a_targets=phase_a["targets"],
    )
    publication_result["record"] = publish_receipt_record
    if not publication_result["valid"]:
        return {
            "status": "BLOCKED__INVALID_PUBLISH_RECEIPT",
            "authorization": authorization_result,
            "publication": publication_result,
            "post_publish_validation": {"status": "NOT_RUN"},
        }
    if catalog is None or qdata_validation is None:
        return {
            "status": "PUBLISHED__POST_PUBLISH_VALIDATION_REQUIRED",
            "authorization": authorization_result,
            "publication": publication_result,
            "post_publish_validation": {"status": "NOT_RUN"},
        }

    catalog_validation = evaluate_catalog_post_publish(
        publish_targets=publication_result["targets"],
        catalog=catalog,
    )
    post = evaluate_post_publish_validation(
        publish_targets=publication_result["targets"],
        catalog_validation=catalog_validation,
        qdata_validation=qdata_validation,
    )
    return {
        "status": (
            "PASS__PUBLISHED_AND_POST_PUBLISH_VALIDATED"
            if post["valid"]
            else "FAIL__PUBLISHED_BUT_POST_PUBLISH_VALIDATION_FAILED"
        ),
        "authorization": authorization_result,
        "publication": publication_result,
        "post_publish_validation": post,
    }


def assess_current_publisher(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    required_markers = {
        "isolated_workspace_guard": (
            "assert_isolated_synthetic_workspace(workspace_root, catalog_path)"
        ),
        "synthetic_manifest": '"synthetic_fixture": True',
        "synthetic_cli_description": "L1-D12A isolated synthetic atomic publisher",
    }
    observed = {
        name: marker in text for name, marker in required_markers.items()
    }
    synthetic_only = all(observed.values())
    return {
        "status": (
            "REAL_DATA_UNSUPPORTED__SYNTHETIC_FIXTURE_ONLY"
            if synthetic_only
            else "UNKNOWN__MANUAL_REVIEW_REQUIRED"
        ),
        "synthetic_only": synthetic_only,
        "marker_checks": observed,
        "sha256": sha256_file(path),
    }


def build_readme(state: dict[str, Any]) -> str:
    rows = []
    for target in state["phase_a"]["targets"]:
        rows.append(
            "| {core_id} | {dataset_id} | {status} | {blockers} |".format(
                core_id=target["core_id"],
                dataset_id=target["dataset_id"],
                status=target["stage_a"]["status"],
                blockers=len(target["stage_a"]["blocking_reasons"]),
            )
        )
    return "\n".join(
        [
            "# Selected CORE two-stage admission architecture",
            "",
            f"- Run: `{state['run_id']}`",
            f"- Overall: `{state['status']}`",
            (
                f"- Stage A ready: `{state['phase_a']['ready_count']}/"
                f"{state['phase_a']['target_count']}`"
            ),
            f"- Stage B: `{state['phase_b']['status']}`",
            "- Formal catalog writes: `0`",
            "- Snapshot publications: `0`",
            "- Quality promotions or waivers: `0`",
            "",
            "## Architecture finding",
            "",
            "The legacy audit is preserved and reclassified as a "
            "`POST_ADMISSION_AUDIT`. Requiring its `formal_closure`, "
            "`formal_catalog_identity` and `qdata_research_resolution` checks "
            "before publication creates a dependency cycle: the legacy "
            "`formal_closure` is derived from formal plan counts, while a new "
            "identity also cannot already be registered or research-resolvable "
            "before it is admitted and atomically published.",
            "",
            "Stage A now excludes those three post-publish checks. It still fails "
            "closed on rights, schema semantics, PIT, revision history, official "
            "cross-source evidence, closure evidence, open contract blockers and "
            "every blocking quality gate.",
            "",
            "| CORE | Dataset | Stage A | Blocking reasons |",
            "|---|---|---|---:|",
            *rows,
            "",
            "## Stage B boundary",
            "",
            "Stage B can start only when all four Stage-A targets pass and a "
            "separate user authorization receipt is hash-bound to the exact Stage-A "
            "bundle. A separate real-data publisher must then produce an exact "
            "publish receipt. Only afterward are formal catalog identity and exact "
            "`qdata resolve <dataset_id> <snapshot_id> --intent research` checked.",
            "",
            "The current `atomic_snapshot_publish.py` is deliberately "
            "synthetic-fixture-only. This audit did not call it and did not create "
            "an authorization or publish receipt.",
            "",
            "For the post-publish probe, dataset and snapshot identities must be "
            "safe single path components. On Windows the batch wrapper is not "
            "passed through a composed `cmd /c` string; the sibling "
            "`dataset_cli.py` receives a subprocess argument vector directly. "
            "Report replacement also refuses symlink/reparse or multi-link files.",
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
    value.add_argument(
        "--legacy-gate-decision",
        type=Path,
        default=Path(DEFAULT_LEGACY_DECISION),
    )
    value.add_argument(
        "--formal-catalog",
        type=Path,
        default=Path("data/catalog/datasets.json"),
    )
    value.add_argument(
        "--qdata-cmd",
        type=Path,
        default=Path("data/catalog/qdata.cmd"),
    )
    value.add_argument(
        "--publisher-implementation",
        type=Path,
        default=Path("control/tools/atomic_snapshot_publish.py"),
    )
    value.add_argument("--authorization-receipt", type=Path)
    value.add_argument("--publish-receipt", type=Path)
    value.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT))
    value.add_argument("--replace", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    workspace = args.workspace_root.resolve(strict=True)
    output_candidate = (
        args.output_dir
        if args.output_dir.is_absolute()
        else workspace / args.output_dir
    )
    require_report_path(output_candidate, workspace)
    output = Path(os.path.abspath(output_candidate))
    reject_unexpected_report_residuals(output)
    if output.exists() and not args.replace:
        raise FileExistsError(f"refusing to overwrite report directory: {output}")

    generator = path_under_workspace(Path(__file__), workspace, "generator")
    legacy_path = resolve_input(
        workspace, args.legacy_gate_decision, "legacy gate decision"
    )
    catalog_path = resolve_input(workspace, args.formal_catalog, "formal catalog")
    qdata_path = resolve_input(workspace, args.qdata_cmd, "qdata command")
    publisher_path = resolve_input(
        workspace, args.publisher_implementation, "publisher implementation"
    )
    authorization_path = (
        resolve_input(
            workspace, args.authorization_receipt, "authorization receipt"
        )
        if args.authorization_receipt
        else None
    )
    publish_path = (
        resolve_input(workspace, args.publish_receipt, "publish receipt")
        if args.publish_receipt
        else None
    )

    legacy_hash_before = sha256_file(legacy_path)
    catalog_hash_before = sha256_file(catalog_path)
    legacy = load_json(legacy_path)
    catalog = load_json(catalog_path)
    phase_a = evaluate_phase_a(legacy, source_sha256=legacy_hash_before)

    authorization = load_json(authorization_path) if authorization_path else None
    authorization_record = (
        input_record(authorization_path, workspace) if authorization_path else None
    )
    publish_receipt = load_json(publish_path) if publish_path else None
    publish_record = (
        input_record(publish_path, workspace) if publish_path else None
    )

    # Compute an initial Stage-B state without running qdata.  Exact qdata is
    # invoked only after valid A, authorization and publish receipts.
    phase_b = evaluate_phase_b(
        phase_a=phase_a,
        authorization=authorization,
        authorization_record=authorization_record,
        publish_receipt=publish_receipt,
        publish_receipt_record=publish_record,
    )
    if phase_b["status"] == "PUBLISHED__POST_PUBLISH_VALIDATION_REQUIRED":
        publish_targets = phase_b["publication"]["targets"]
        qdata_validation = run_exact_qdata_post_publish(
            workspace=workspace,
            qdata_path=qdata_path,
            publish_targets=publish_targets,
        )
        phase_b = evaluate_phase_b(
            phase_a=phase_a,
            authorization=authorization,
            authorization_record=authorization_record,
            publish_receipt=publish_receipt,
            publish_receipt_record=publish_record,
            catalog=catalog,
            qdata_validation=qdata_validation,
        )

    publisher = assess_current_publisher(publisher_path)
    legacy_check_names = sorted(
        {
            name
            for target in legacy.get("targets", [])
            if isinstance(target, dict)
            for name in (
                target.get("admission_checks", {}).keys()
                if isinstance(target.get("admission_checks"), dict)
                else []
            )
        }
    )
    new_cycle_check = detect_pre_admission_cycle(STAGE_A_CHECKS)
    legacy_cycle_check = detect_pre_admission_cycle(legacy_check_names)
    status_value = (
        f"{phase_a['status']}__PHASE_B_{phase_b['status']}"
        if not phase_a["all_ready"]
        else phase_b["status"]
    )
    state = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status_value,
        "architecture": {
            "legacy_tool_classification": "POST_ADMISSION_AUDIT",
            "legacy_cycle_check_if_used_as_stage_a": legacy_cycle_check,
            "new_stage_a_cycle_check": new_cycle_check,
            "stage_a_checks": list(STAGE_A_CHECKS),
            "legacy_post_admission_checks": list(
                LEGACY_POST_ADMISSION_CHECKS
            ),
            "stage_b_post_publish_checks": list(STAGE_B_POST_PUBLISH_CHECKS),
            "state_transitions": [
                "A_EVIDENCE_BLOCKED -> stop and record gaps",
                "A_ALL_PASS -> wait for explicit hash-bound user authorization",
                "AUTHORIZED -> separate production atomic publisher",
                "PUBLISHED -> exact catalog identity plus exact qdata research resolve",
                "POST_PUBLISH_PASS -> formally usable exact snapshot identity",
            ],
        },
        "legacy_input": {
            **input_record(legacy_path, workspace),
            "status": legacy.get("status"),
            "admitted_count": legacy.get("admitted_count"),
            "preserved_unchanged": None,
        },
        "phase_a": phase_a,
        "phase_b": phase_b,
        "publisher_capability": {
            **publisher,
            "path": workspace_relative(publisher_path, workspace),
            "invoked_by_this_audit": False,
        },
        "mutations_by_this_audit": {
            "formal_catalog_write": False,
            "snapshot_publication": False,
            "quality_promotion_or_waiver": False,
            "authorization_creation": False,
            "legacy_report_modification": False,
        },
    }

    legacy_hash_after = sha256_file(legacy_path)
    catalog_hash_after = sha256_file(catalog_path)
    state["legacy_input"]["preserved_unchanged"] = (
        legacy_hash_before == legacy_hash_after
    )
    checks = {
        "legacy_report_unchanged": legacy_hash_before == legacy_hash_after,
        "formal_catalog_unchanged": catalog_hash_before == catalog_hash_after,
        "legacy_cycle_detected": legacy_cycle_check["cycle_risk_detected"],
        "legacy_cycle_has_exact_formal_state_checks": (
            legacy_cycle_check["offending_checks"]
            == sorted(LEGACY_POST_ADMISSION_CHECKS)
        ),
        "new_stage_a_cycle_free": not new_cycle_check["cycle_risk_detected"],
        "stage_a_excludes_catalog_and_qdata": not (
            set(STAGE_A_CHECKS) & FORBIDDEN_PRE_ADMISSION_CHECKS
        ),
        "target_scope_exact": [
            item["core_id"] for item in phase_a["targets"]
        ]
        == list(TARGETS),
        "phase_a_count_consistent": phase_a["ready_count"]
        == sum(item["stage_a"]["ready"] for item in phase_a["targets"]),
        "publisher_real_data_not_claimed": publisher["synthetic_only"],
        "no_formal_mutations_claimed": not any(
            state["mutations_by_this_audit"].values()
        ),
    }
    if not all(checks.values()):
        raise RuntimeError(f"two-stage verification failed: {checks}")

    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "STATE_MACHINE.json"
    readme_path = output / "README.md"
    verification_path = output / "VERIFICATION.json"
    write_json(state_path, state)
    readme_path.write_text(
        build_readme(state),
        encoding="utf-8",
        newline="\n",
    )
    verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": "PASS__NON_CIRCULAR_TWO_STAGE_MODEL__NO_FORMAL_MUTATION",
        "checks": checks,
        "formal_state": {
            "catalog": {
                "path": workspace_relative(catalog_path, workspace),
                "sha256_before": catalog_hash_before,
                "sha256_after": catalog_hash_after,
            },
            "legacy_gate_decision": {
                "path": workspace_relative(legacy_path, workspace),
                "sha256_before": legacy_hash_before,
                "sha256_after": legacy_hash_after,
            },
        },
        "inputs": {
            "generator": input_record(generator, workspace),
            "publisher": input_record(publisher_path, workspace),
            "qdata": input_record(qdata_path, workspace),
            "authorization_receipt": authorization_record,
            "publish_receipt": publish_record,
        },
    }
    write_json(verification_path, verification)

    payload_paths = sorted(
        (readme_path, state_path, verification_path),
        key=lambda path: path.name,
    )
    payload_records = [
        {
            "path": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in payload_paths
    ]
    manifest = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "generator": input_record(generator, workspace),
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
    write_json(manifest_path, manifest)

    reject_unexpected_report_residuals(output)
    final_names = {item.name for item in output.iterdir() if item.is_file()}
    if final_names != ALLOWED_REPORT_FILES:
        raise RuntimeError(
            f"final report file set mismatch: "
            f"{sorted(final_names ^ ALLOWED_REPORT_FILES)}"
        )
    for record in payload_records:
        if sha256_file(output / record["path"]) != record["sha256"]:
            raise RuntimeError(f"report payload hash drifted: {record['path']}")

    print(
        json.dumps(
            {
                "status": state["status"],
                "phase_a_ready": phase_a["ready_count"],
                "phase_a_target_count": phase_a["target_count"],
                "phase_b": phase_b["status"],
                "legacy_cycle_detected": legacy_cycle_check[
                    "cycle_risk_detected"
                ],
                "formal_catalog_sha256": catalog_hash_after,
                "report": workspace_relative(output, workspace),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
