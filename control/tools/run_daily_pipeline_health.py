#!/usr/bin/env python3
"""Read-only health check for the inspection-only local daily pipeline.

The checker binds one explicit orchestration request, its SQLite state, an
explicit list of sealed local-update candidate manifests and an explicit list
of quarantine receipts.  It has no acquisition, alerting, scheduling,
catalog-writing, snapshot-publishing or promotion surface.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import uuid
from collections import Counter
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
MODE = "daily_pipeline_health"
MONITOR_ID = "daily_pipeline_health/v1"
PURPOSE_CEILING = "INSPECTION_ONLY"
PASS_STATUS = "PASS_HEALTHY_INSPECTION_ONLY"
FAIL_STATUS = "FAIL_CLOSED_DAILY_PIPELINE_HEALTH"
OUTPUT_ROOT_REL = PurePosixPath("tmp/daily-pipeline-health")
UPDATE_ROOT_REL = PurePosixPath("tmp/local-daily-update")
COMPLETE_STATUSES = {"SUCCEEDED", "NO_CHANGE"}
KNOWN_STATUSES = {
    "PENDING",
    "RETRY_PENDING",
    "RUNNING",
    "SUCCEEDED",
    "NO_CHANGE",
    "FAILED_TERMINAL",
}
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class HealthError(RuntimeError):
    """Controlled fail-closed input or consistency error."""

    def __init__(self, message: str, *, code: str = "HEALTH_INPUT_REJECTED"):
        super().__init__(message)
        self.code = code


def _load_module(name: str, path: Path) -> Any:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise HealthError(f"cannot load required local module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ORCHESTRATION = _load_module(
    "_daily_pipeline_health_orchestration",
    ROOT / "control" / "tools" / "run_local_daily_orchestration.py",
)
DAILY_UPDATE = _load_module(
    "_daily_pipeline_health_update",
    ROOT / "control" / "tools" / "run_daily_update_local_smoke.py",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_bytes(value: Any) -> bytes:
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


def _reject_constant(value: str) -> None:
    raise HealthError(f"non-finite JSON constant is forbidden: {value}")


def _strict_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HealthError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except HealthError:
        raise
    except Exception as exc:
        raise HealthError(f"invalid UTF-8 JSON file: {path}: {exc}") from exc


def parse_date(value: Any, label: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HealthError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HealthError(f"{label} is not a valid date") from exc


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise HealthError(f"{label} must be strict RFC 3339 with timezone")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HealthError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HealthError(f"{label} must include an explicit timezone")
    return parsed


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HealthError(f"cannot lstat path: {path}: {exc}") from exc
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_ATTRIBUTE
    )


def _safe_relative(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise HealthError(f"{label} must be a non-empty POSIX workspace path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise HealthError(f"{label} is not a canonical safe relative path")
    return relative


def _safe_component(value: Any, label: str) -> str:
    if not isinstance(value, str) or COMPONENT_RE.fullmatch(value) is None:
        raise HealthError(f"{label} must be a safe identifier")
    return value


def _bound_spec(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256", "bytes"}:
        raise HealthError(f"{label} must contain relative_path, sha256 and bytes")
    _safe_relative(value["relative_path"], f"{label}.relative_path")
    if not isinstance(value["sha256"], str) or HASH_RE.fullmatch(value["sha256"]) is None:
        raise HealthError(f"{label}.sha256 must be lowercase SHA-256")
    if (
        isinstance(value["bytes"], bool)
        or not isinstance(value["bytes"], int)
        or value["bytes"] < 0
    ):
        raise HealthError(f"{label}.bytes must be a non-negative integer")
    return value


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HealthError("health request must be a JSON object")
    expected = {
        "schema_version",
        "mode",
        "purpose_ceiling",
        "run_id",
        "as_of",
        "through_date",
        "max_watermark_lag_due_dates",
        "orchestration_request",
        "orchestration_database",
        "candidate_manifests",
        "quarantine_receipts",
    }
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing or extras:
        raise HealthError(
            f"health request field mismatch; missing={missing}, unsupported={extras}"
        )
    if value["schema_version"] != 1 or value["mode"] != MODE:
        raise HealthError("unsupported health request version or mode")
    if value["purpose_ceiling"] != PURPOSE_CEILING:
        raise HealthError("purpose_ceiling must remain INSPECTION_ONLY")
    _safe_component(value["run_id"], "run_id")
    parse_timestamp(value["as_of"], "as_of")
    through = parse_date(value["through_date"], "through_date")
    maximum = value["max_watermark_lag_due_dates"]
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 0 <= maximum <= 4000:
        raise HealthError("max_watermark_lag_due_dates must be from 0 to 4000")
    _bound_spec(value["orchestration_request"], "orchestration_request")
    database = value["orchestration_database"]
    if not isinstance(database, dict) or set(database) != {"relative_path"}:
        raise HealthError("orchestration_database must contain only relative_path")
    _safe_relative(database["relative_path"], "orchestration_database.relative_path")

    candidates = value["candidate_manifests"]
    if not isinstance(candidates, list) or len(candidates) > 4000:
        raise HealthError("candidate_manifests must be an array with at most 4000 items")
    seen_dates: set[str] = set()
    seen_paths: set[str] = set()
    for index, item in enumerate(candidates):
        label = f"candidate_manifests[{index}]"
        if not isinstance(item, dict) or set(item) != {
            "run_date",
            "relative_path",
            "sha256",
            "bytes",
            "expected_row_count",
            "expected_date_min",
            "expected_date_max",
        }:
            raise HealthError(f"{label} has an invalid field set")
        run_date = parse_date(item["run_date"], f"{label}.run_date")
        if run_date > through:
            raise HealthError(f"{label}.run_date exceeds through_date")
        relative = _safe_relative(item["relative_path"], f"{label}.relative_path")
        if tuple(relative.parts[:2]) != tuple(UPDATE_ROOT_REL.parts):
            raise HealthError(f"{label}.relative_path must be below tmp/local-daily-update")
        if len(relative.parts) != 5 or relative.parts[-1] != "manifest.json":
            raise HealthError(f"{label}.relative_path must identify a direct candidate manifest")
        if relative.parts[2] == "quarantine":
            raise HealthError(f"{label}.relative_path cannot identify quarantine")
        if not isinstance(item["sha256"], str) or HASH_RE.fullmatch(item["sha256"]) is None:
            raise HealthError(f"{label}.sha256 must be lowercase SHA-256")
        if isinstance(item["bytes"], bool) or not isinstance(item["bytes"], int) or item["bytes"] < 0:
            raise HealthError(f"{label}.bytes must be non-negative")
        if (
            isinstance(item["expected_row_count"], bool)
            or not isinstance(item["expected_row_count"], int)
            or item["expected_row_count"] < 0
        ):
            raise HealthError(f"{label}.expected_row_count must be non-negative")
        date_min = parse_date(item["expected_date_min"], f"{label}.expected_date_min")
        date_max = parse_date(item["expected_date_max"], f"{label}.expected_date_max")
        if date_min > date_max:
            raise HealthError(f"{label} has an inverted expected date range")
        if item["run_date"] in seen_dates or item["relative_path"].casefold() in seen_paths:
            raise HealthError("candidate manifest dates and paths must be unique")
        seen_dates.add(item["run_date"])
        seen_paths.add(item["relative_path"].casefold())

    quarantines = value["quarantine_receipts"]
    if not isinstance(quarantines, list) or len(quarantines) > 4000:
        raise HealthError("quarantine_receipts must be an array with at most 4000 items")
    seen_quarantine: set[str] = set()
    for index, item in enumerate(quarantines):
        label = f"quarantine_receipts[{index}]"
        _bound_spec(item, label)
        relative = _safe_relative(item["relative_path"], f"{label}.relative_path")
        if (
            len(relative.parts) != 6
            or tuple(relative.parts[:3]) != ("tmp", "local-daily-update", "quarantine")
            or relative.parts[-1] != "failure.json"
        ):
            raise HealthError(f"{label} must identify a direct quarantine failure.json")
        key = item["relative_path"].casefold()
        if key in seen_quarantine:
            raise HealthError("quarantine receipt paths must be unique")
        seen_quarantine.add(key)
    return value


def _workspace_root(value: Path) -> Path:
    lexical = Path(os.path.abspath(value))
    if not lexical.is_dir() or _is_reparse(lexical):
        raise HealthError("workspace root must be a regular non-reparse directory")
    return lexical.resolve(strict=True)


def _regular_file(root: Path, relative: PurePosixPath, label: str) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists():
            raise HealthError(f"{label} is missing: {relative.as_posix()}")
        if _is_reparse(current):
            raise HealthError(f"{label} contains a reparse or symlink component")
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HealthError(f"{label} escapes the workspace") from exc
    if not resolved.is_file():
        raise HealthError(f"{label} must be a regular file")
    return resolved


def _bound_file(root: Path, spec: dict[str, Any], label: str) -> Path:
    relative = _safe_relative(spec["relative_path"], f"{label}.relative_path")
    path = _regular_file(root, relative, label)
    if path.stat().st_size != spec["bytes"] or sha256_file(path) != spec["sha256"]:
        raise HealthError(f"{label} no longer matches its byte/hash binding")
    return path


def _expected_job_status(rows: list[dict[str, Any]]) -> str:
    if rows and all(row["status"] in COMPLETE_STATUSES for row in rows):
        return "COMPLETE"
    if any(row["status"] == "FAILED_TERMINAL" for row in rows):
        return "BLOCKED"
    if any(row["status"] == "RUNNING" for row in rows):
        return "RUNNING"
    return "READY"


def _candidate_issue(
    issues: list[dict[str, Any]], run_date: str, code: str, detail: str
) -> None:
    issues.append({"run_date": run_date, "code": code, "detail": detail})


def _check_candidates(
    *,
    root: Path,
    request: dict[str, Any],
    dataset_id: str,
    rows_by_date: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    specified_dates = {item["run_date"] for item in request["candidate_manifests"]}
    for run_date, row in rows_by_date.items():
        if (
            run_date <= request["through_date"]
            and row["status"] in COMPLETE_STATUSES
            and run_date not in specified_dates
        ):
            _candidate_issue(
                issues,
                run_date,
                "COMPLETED_DATE_CANDIDATE_EVIDENCE_MISSING",
                "completed due date has no explicitly bound candidate manifest",
            )

    for item in sorted(request["candidate_manifests"], key=lambda value: value["run_date"]):
        run_date = item["run_date"]
        relative = _safe_relative(item["relative_path"], "candidate relative_path")
        row = rows_by_date.get(run_date)
        if row is None:
            _candidate_issue(issues, run_date, "CANDIDATE_DATE_NOT_SCHEDULED", "run date is absent from orchestration state")
            continue
        if row["status"] not in COMPLETE_STATUSES:
            _candidate_issue(issues, run_date, "CANDIDATE_BOUND_TO_UNFINISHED_DATE", f"schedule status is {row['status']}")
        if relative.parts[2] != dataset_id:
            _candidate_issue(issues, run_date, "CANDIDATE_PATH_DATASET_MISMATCH", "path dataset component differs from orchestration dataset")
            continue
        try:
            path = _regular_file(root, relative, "candidate manifest")
        except HealthError as exc:
            _candidate_issue(issues, run_date, "CANDIDATE_MANIFEST_MISSING_OR_UNSAFE", str(exc))
            continue
        actual_bytes = path.stat().st_size
        actual_hash = sha256_file(path)
        if actual_bytes != item["bytes"] or actual_hash != item["sha256"]:
            _candidate_issue(issues, run_date, "CANDIDATE_MANIFEST_BINDING_DRIFT", "manifest bytes or SHA-256 differs from explicit request binding")
            continue
        if row.get("result_sha256") != actual_hash:
            _candidate_issue(issues, run_date, "SQLITE_RESULT_HASH_DRIFT", "SQLite result_sha256 differs from candidate manifest SHA-256")
        candidate_root = path.parent
        try:
            manifest = DAILY_UPDATE.verify_candidate(candidate_root)
            strict_manifest = read_json(path)
            if strict_manifest != manifest:
                raise HealthError("strict manifest parse differs from candidate verifier")
            quality = read_json(candidate_root / "quality" / "summary.json")
        except Exception as exc:
            _candidate_issue(issues, run_date, "CANDIDATE_SEAL_VERIFICATION_FAILED", str(exc))
            continue
        if manifest.get("dataset_id") != dataset_id:
            _candidate_issue(issues, run_date, "CANDIDATE_DATASET_ID_DRIFT", "manifest dataset_id differs from orchestration dataset")
        for key in ("catalog_registered", "source_admission", "quality_promotion", "research_ready"):
            if manifest.get(key) is not False:
                _candidate_issue(issues, run_date, "CANDIDATE_BOUNDARY_DRIFT", f"manifest {key} must remain false")
        if manifest.get("purpose_ceiling") != PURPOSE_CEILING:
            _candidate_issue(issues, run_date, "CANDIDATE_BOUNDARY_DRIFT", "manifest purpose ceiling changed")
        if not isinstance(quality, dict) or quality.get("metrics") is None:
            _candidate_issue(issues, run_date, "QUALITY_SUMMARY_INVALID", "quality summary or metrics is missing")
            continue
        if (
            quality.get("mechanical_status") != "PASS"
            or quality.get("admission_decision") != "NOT_ADMITTED"
            or quality.get("purpose_ceiling") != PURPOSE_CEILING
            or any(quality.get(key) is not False for key in ("source_admission", "catalog_registered", "quality_promotion", "research_ready"))
        ):
            _candidate_issue(issues, run_date, "QUALITY_BOUNDARY_DRIFT", "quality summary no longer has the inspection-only PASS/NOT_ADMITTED boundary")
        metrics = quality["metrics"]
        if not isinstance(metrics, dict):
            _candidate_issue(issues, run_date, "QUALITY_METRICS_INVALID", "metrics must be an object")
            continue
        row_count = manifest.get("row_count")
        date_min_text = metrics.get("date_min")
        date_max_text = metrics.get("date_max")
        try:
            date_min = parse_date(date_min_text, "quality.metrics.date_min")
            date_max = parse_date(date_max_text, "quality.metrics.date_max")
        except HealthError as exc:
            _candidate_issue(issues, run_date, "CANDIDATE_COVERAGE_INVALID", str(exc))
            continue
        if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
            _candidate_issue(issues, run_date, "CANDIDATE_ROW_COUNT_INVALID", "manifest row_count is invalid")
            continue
        if metrics.get("merged_rows") != row_count:
            _candidate_issue(issues, run_date, "CANDIDATE_ROW_COUNT_INTERNAL_DRIFT", "quality merged_rows differs from manifest row_count")
        if row_count != item["expected_row_count"]:
            _candidate_issue(issues, run_date, "CANDIDATE_ROW_COUNT_EXPECTATION_DRIFT", "actual row_count differs from explicitly expected row_count")
        if date_min_text != item["expected_date_min"] or date_max_text != item["expected_date_max"]:
            _candidate_issue(issues, run_date, "CANDIDATE_COVERAGE_EXPECTATION_DRIFT", "actual date range differs from explicitly expected range")
        run_day = date.fromisoformat(run_date)
        if not date_min <= run_day <= date_max:
            _candidate_issue(issues, run_date, "CANDIDATE_RUN_DATE_NOT_COVERED", "candidate date range does not include its scheduled run date")
        verified.append(
            {
                "run_date": run_date,
                "relative_path": item["relative_path"],
                "manifest_sha256": actual_hash,
                "row_count": row_count,
                "date_min": date_min_text,
                "date_max": date_max_text,
                "content_hash": manifest.get("content_hash"),
            }
        )

    for previous, current in zip(verified, verified[1:]):
        if current["row_count"] < previous["row_count"]:
            _candidate_issue(issues, current["run_date"], "CANDIDATE_ROW_COUNT_REGRESSION", f"row_count fell below {previous['run_date']}")
        if current["date_min"] > previous["date_min"]:
            _candidate_issue(issues, current["run_date"], "CANDIDATE_DATE_MIN_REGRESSION", f"date_min moved later than {previous['run_date']}")
        if current["date_max"] < previous["date_max"]:
            _candidate_issue(issues, current["run_date"], "CANDIDATE_DATE_MAX_REGRESSION", f"date_max moved earlier than {previous['run_date']}")
    return {
        "specified_count": len(request["candidate_manifests"]),
        "verified_count": len(verified),
        "verified": verified,
        "drift_count": len(issues),
        "drift": issues,
    }


def _check_quarantines(
    *, root: Path, request: dict[str, Any], dataset_id: str
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    verified_count = 0
    expected_fields = {
        "schema_version",
        "engine_id",
        "status",
        "dataset_id",
        "run_id",
        "reason",
        "message",
        "request_fingerprint",
        "failed_at",
        "rollback",
        "formal_catalog_guard",
        "purpose_ceiling",
        "source_admission",
        "catalog_registered",
        "quality_promotion",
        "research_ready",
    }
    for item in request["quarantine_receipts"]:
        relative_text = item["relative_path"]
        relative = _safe_relative(relative_text, "quarantine relative_path")
        if relative.parts[3] != dataset_id:
            issues.append(
                {
                    "relative_path": relative_text,
                    "code": "QUARANTINE_PATH_DATASET_MISMATCH",
                    "detail": "path dataset component differs from orchestration dataset",
                }
            )
            continue
        try:
            path = _regular_file(root, relative, "quarantine receipt")
        except HealthError as exc:
            issues.append({"relative_path": relative_text, "code": "QUARANTINE_RECEIPT_MISSING_OR_UNSAFE", "detail": str(exc)})
            continue
        actual_hash = sha256_file(path)
        if path.stat().st_size != item["bytes"] or actual_hash != item["sha256"]:
            issues.append({"relative_path": relative_text, "code": "QUARANTINE_RECEIPT_BINDING_DRIFT", "detail": "receipt bytes or SHA-256 differs from explicit request binding"})
            continue
        try:
            receipt = read_json(path)
            if not isinstance(receipt, dict) or set(receipt) != expected_fields:
                raise HealthError("quarantine receipt field set is invalid")
            guard = receipt["formal_catalog_guard"]
            rollback = receipt["rollback"]
            if not isinstance(guard, dict) or set(guard) != {"sha256_before", "sha256_after", "unchanged"}:
                raise HealthError("formal_catalog_guard is invalid")
            if not isinstance(rollback, dict) or set(rollback) != {"candidate_published", "formal_catalog_written", "staging_removed"}:
                raise HealthError("rollback boundary is invalid")
            before = guard["sha256_before"]
            after = guard["sha256_after"]
            if not isinstance(before, str) or HASH_RE.fullmatch(before) is None or not isinstance(after, str) or HASH_RE.fullmatch(after) is None or before != after or guard["unchanged"] is not True:
                raise HealthError("formal catalog guard does not prove unchanged state")
            if rollback != {"candidate_published": False, "formal_catalog_written": False, "staging_removed": True}:
                raise HealthError("quarantine rollback boundary drifted")
            if (
                receipt["schema_version"] != 1
                or receipt["engine_id"] != DAILY_UPDATE.ENGINE_ID
                or receipt["status"] != "QUARANTINED"
                or receipt["dataset_id"] != dataset_id
                or receipt["purpose_ceiling"] != PURPOSE_CEILING
                or any(receipt[key] is not False for key in ("source_admission", "catalog_registered", "quality_promotion", "research_ready"))
                or not isinstance(receipt["reason"], str)
                or not receipt["reason"]
            ):
                raise HealthError("quarantine receipt identity or boundary is invalid")
        except HealthError as exc:
            issues.append({"relative_path": relative_text, "code": "QUARANTINE_RECEIPT_INVALID", "detail": str(exc)})
            continue
        verified_count += 1
        reasons[receipt["reason"]] += 1
    return {
        "specified_count": len(request["quarantine_receipts"]),
        "verified_count": verified_count,
        "reason_counts": dict(sorted(reasons.items())),
        "drift_count": len(issues),
        "drift": issues,
    }


def run(*, workspace_root: Path, health_request_path: Path) -> dict[str, Any]:
    root = _workspace_root(workspace_root)
    request_lexical = Path(os.path.abspath(health_request_path))
    try:
        request_relative = PurePosixPath(request_lexical.relative_to(root).as_posix())
    except ValueError as exc:
        raise HealthError("health request must stay inside workspace") from exc
    request_path = _regular_file(root, request_relative, "health request")
    request_bytes_before = request_path.stat().st_size
    request_file_hash_before = sha256_file(request_path)
    request = validate_request(read_json(request_path))
    as_of = parse_timestamp(request["as_of"], "as_of")
    through = parse_date(request["through_date"], "through_date")

    orchestration_request_path = _bound_file(
        root, request["orchestration_request"], "orchestration request"
    )
    context = ORCHESTRATION.validate_workspace(root, orchestration_request_path)
    db_relative = _safe_relative(
        request["orchestration_database"]["relative_path"],
        "orchestration_database.relative_path",
    )
    database_path = _regular_file(root, db_relative, "orchestration database")
    if database_path != context.database_path.resolve(strict=True):
        raise HealthError("explicit orchestration database does not match request-derived database")
    database_hash_before = sha256_file(database_path)
    connection = ORCHESTRATION.open_readonly_database(database_path)
    try:
        job = dict(ORCHESTRATION.verify_database(connection, context))
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT run_date, status, attempt_count, claimed_at, "
                "lease_expires_at, result_sha256, last_error_code "
                "FROM schedule_dates WHERE job_id = ? ORDER BY run_date",
                (context.request["job_id"],),
            )
        ]
    finally:
        connection.close()
    if sha256_file(database_path) != database_hash_before:
        raise HealthError("orchestration database changed during read-only check", code="DATABASE_CHANGED_DURING_CHECK")

    for index, row in enumerate(rows):
        parse_date(row.get("run_date"), f"schedule_dates[{index}].run_date")
        if not isinstance(row.get("status"), str):
            raise HealthError("schedule status must be text", code="SQLITE_STATE_INVALID")
        attempt_count = row.get("attempt_count")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise HealthError(
                "schedule attempt_count is invalid", code="SQLITE_STATE_INVALID"
            )
        result_hash = row.get("result_sha256")
        if result_hash is not None and (
            not isinstance(result_hash, str) or HASH_RE.fullmatch(result_hash) is None
        ):
            raise HealthError(
                "schedule result_sha256 is invalid", code="SQLITE_STATE_INVALID"
            )

    state_drift: list[dict[str, Any]] = []
    calendar = context.request["calendar_dates"]
    if [row["run_date"] for row in rows] != calendar:
        state_drift.append({"code": "SCHEDULE_DATE_SET_DRIFT", "detail": "SQLite schedule dates differ from the bound orchestration request"})
    recomputed_watermark: str | None = None
    for row in rows:
        if row["status"] in COMPLETE_STATUSES:
            recomputed_watermark = row["run_date"]
        else:
            break
    if job["watermark_date"] != recomputed_watermark:
        state_drift.append({"code": "STORED_WATERMARK_DRIFT", "detail": "stored watermark differs from the recomputed contiguous watermark"})
    expected_job_status = _expected_job_status(rows)
    if job["status"] != expected_job_status:
        state_drift.append({"code": "JOB_STATUS_DRIFT", "detail": f"stored={job['status']} recomputed={expected_job_status}"})

    due_rows = [row for row in rows if row["run_date"] <= request["through_date"]]
    terminal_failures: list[dict[str, Any]] = []
    expired_leases: list[dict[str, Any]] = []
    for row in rows:
        if row["status"] not in KNOWN_STATUSES:
            state_drift.append({"code": "UNKNOWN_SCHEDULE_STATUS", "run_date": row["run_date"], "detail": str(row["status"])})
        if row["status"] == "RUNNING":
            if not row["claimed_at"] or not row["lease_expires_at"]:
                state_drift.append({"code": "RUNNING_LEASE_FIELDS_MISSING", "run_date": row["run_date"], "detail": "RUNNING row lacks claimed_at or lease_expires_at"})
            else:
                try:
                    expiry = parse_timestamp(row["lease_expires_at"], "lease_expires_at")
                except HealthError as exc:
                    state_drift.append({"code": "RUNNING_LEASE_TIMESTAMP_INVALID", "run_date": row["run_date"], "detail": str(exc)})
                else:
                    if row in due_rows and expiry <= as_of:
                        expired_leases.append({"run_date": row["run_date"], "attempt_count": row["attempt_count"], "claimed_at": row["claimed_at"], "lease_expires_at": row["lease_expires_at"]})
        elif row["claimed_at"] is not None or row["lease_expires_at"] is not None:
            state_drift.append({"code": "INACTIVE_LEASE_FIELDS_PRESENT", "run_date": row["run_date"], "detail": "non-RUNNING row retains lease fields"})
        if row in due_rows and row["status"] == "FAILED_TERMINAL":
            terminal_failures.append({"run_date": row["run_date"], "attempt_count": row["attempt_count"], "last_error_code": row["last_error_code"]})

    unfinished_due = [row for row in due_rows if row["status"] not in COMPLETE_STATUSES]
    if recomputed_watermark is None:
        lag_due_dates = len(due_rows)
        lag_calendar_days = (
            (through - date.fromisoformat(due_rows[0]["run_date"])).days + 1
            if due_rows
            else 0
        )
    else:
        lag_due_dates = sum(row["run_date"] > recomputed_watermark for row in due_rows)
        lag_calendar_days = max(0, (through - date.fromisoformat(recomputed_watermark)).days)

    rows_by_date = {row["run_date"]: row for row in rows}
    candidates = _check_candidates(
        root=root,
        request=request,
        dataset_id=job["dataset_id"],
        rows_by_date=rows_by_date,
    )
    quarantines = _check_quarantines(
        root=root, request=request, dataset_id=job["dataset_id"]
    )

    failures: list[str] = []
    if state_drift:
        failures.append("ORCHESTRATION_STATE_DRIFT")
    if lag_due_dates > request["max_watermark_lag_due_dates"]:
        failures.append("WATERMARK_LAG_EXCEEDS_THRESHOLD")
    if terminal_failures:
        failures.append("TERMINAL_FAILURES_PRESENT")
    if expired_leases:
        failures.append("EXPIRED_LEASES_PRESENT")
    if candidates["drift_count"]:
        failures.append("CANDIDATE_DRIFT_PRESENT")
    if quarantines["drift_count"]:
        failures.append("QUARANTINE_RECEIPT_DRIFT_PRESENT")

    if request_path.stat().st_size != request_bytes_before or sha256_file(request_path) != request_file_hash_before:
        raise HealthError("health request changed during verification", code="HEALTH_REQUEST_CHANGED_DURING_CHECK")
    if sha256_file(orchestration_request_path) != request["orchestration_request"]["sha256"]:
        raise HealthError("orchestration request changed during verification", code="ORCHESTRATION_REQUEST_CHANGED_DURING_CHECK")
    if sha256_file(database_path) != database_hash_before:
        raise HealthError("orchestration database changed after state read", code="DATABASE_CHANGED_DURING_CHECK")
    for item in request["candidate_manifests"]:
        relative = _safe_relative(item["relative_path"], "candidate relative_path")
        try:
            current = _regular_file(root, relative, "candidate manifest")
        except HealthError:
            continue
        if current.stat().st_size != item["bytes"] or sha256_file(current) != item["sha256"]:
            raise HealthError("candidate manifest changed during verification", code="CANDIDATE_CHANGED_DURING_CHECK")
    for item in request["quarantine_receipts"]:
        relative = _safe_relative(item["relative_path"], "quarantine relative_path")
        try:
            current = _regular_file(root, relative, "quarantine receipt")
        except HealthError:
            continue
        if current.stat().st_size != item["bytes"] or sha256_file(current) != item["sha256"]:
            raise HealthError("quarantine receipt changed during verification", code="QUARANTINE_CHANGED_DURING_CHECK")
    if ORCHESTRATION.sha256_file(context.catalog_path) != context.catalog_sha256:
        raise HealthError("formal catalog changed during health verification", code="FORMAL_CATALOG_CHANGED_DURING_CHECK")

    return {
        "schema_version": 1,
        "monitor_id": MONITOR_ID,
        "status": FAIL_STATUS if failures else PASS_STATUS,
        "health_failures": failures,
        "run_id": request["run_id"],
        "as_of": request["as_of"],
        "through_date": request["through_date"],
        "job": {
            "job_id": job["job_id"],
            "dataset_id": job["dataset_id"],
            "source_id": job["source_id"],
            "stored_status": job["status"],
            "recomputed_status": expected_job_status,
            "stored_watermark": job["watermark_date"],
            "recomputed_watermark": recomputed_watermark,
            "orchestration_request_sha256": context.request_sha256,
            "orchestration_database_sha256": database_hash_before,
        },
        "watermark": {
            "due_date_count": len(due_rows),
            "unfinished_due_date_count": len(unfinished_due),
            "earliest_unfinished": unfinished_due[0]["run_date"] if unfinished_due else None,
            "lag_due_dates": lag_due_dates,
            "lag_calendar_days": lag_calendar_days,
            "max_lag_due_dates": request["max_watermark_lag_due_dates"],
        },
        "terminal_failures": terminal_failures,
        "expired_leases": expired_leases,
        "orchestration_state_drift": state_drift,
        "candidates": candidates,
        "quarantine": quarantines,
        "boundary": {
            "purpose_ceiling": PURPOSE_CEILING,
            "read_only_inputs": True,
            "network_used": False,
            "alerts_sent": False,
            "scheduler_created": False,
            "formal_data_written": False,
            "formal_catalog_written": False,
            "formal_snapshot_written": False,
            "quality_promotion": False,
            "research_ready": False,
        },
    }


def write_once(root: Path, run_id: str, output: Path, report: dict[str, Any]) -> Path:
    lexical = Path(os.path.abspath(output))
    expected = root / Path(*OUTPUT_ROOT_REL.parts) / f"{run_id}.json"
    if lexical != expected:
        raise HealthError(f"output must be exactly {expected}")
    parent = expected.parent
    current = root
    for part in OUTPUT_ROOT_REL.parts:
        current = current / part
        if current.exists():
            if _is_reparse(current) or not current.is_dir():
                raise HealthError("health output path contains a non-directory or reparse component")
        else:
            current.mkdir()
            if _is_reparse(current):
                raise HealthError("new health output directory unexpectedly became a reparse point")
    if expected.exists():
        raise HealthError("health output already exists; overwrite is forbidden")
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary = parent / f".{expected.name}.{uuid.uuid4().hex}.partial"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, expected)
        if sha256_file(expected) != hashlib.sha256(payload.encode("utf-8")).hexdigest():
            raise HealthError("published health output failed hash verification")
    except FileExistsError as exc:
        raise HealthError("health output already exists; overwrite is forbidden") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    return expected


def _argument_path(value: str, root: Path) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else root / candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", default=str(ROOT))
    parser.add_argument("--health-request", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace_root)
    request_path = _argument_path(args.health_request, workspace)
    try:
        report = run(workspace_root=workspace, health_request_path=request_path)
        if args.output:
            output = _argument_path(args.output, workspace)
            write_once(_workspace_root(workspace), report["run_id"], output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if report["status"] == PASS_STATUS else 3
    except HealthError as exc:
        failure = {
            "schema_version": 1,
            "monitor_id": MONITOR_ID,
            "status": "FAIL_CLOSED_INVALID_HEALTH_REQUEST",
            "error_code": exc.code,
            "error": str(exc),
            "boundary": {
                "purpose_ceiling": PURPOSE_CEILING,
                "formal_catalog_written": False,
                "formal_snapshot_written": False,
                "scheduler_created": False,
                "research_ready": False,
            },
        }
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
