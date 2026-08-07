#!/usr/bin/env python3
"""Connect landed normalized deltas to the inspection-only daily state machine.

This bounded caller deliberately contains no network client, credential
handling, scheduler, arbitrary command execution, formal publisher, catalog
writer or quality-promotion path.  It composes the existing
``run_local_daily_orchestration.py`` and
``run_daily_update_local_smoke.py`` APIs without weakening either contract.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

import run_daily_update_local_smoke as daily_update
import run_local_daily_orchestration as orchestration


ROOT = Path(__file__).resolve().parents[2]
ENGINE_ID = "local_daily_inspection_worker/composed-v1"
PURPOSE_CEILING = "INSPECTION_ONLY"
SUCCESS_STATUSES = {"CANDIDATE_PUBLISHED", "NO_CHANGE"}


class WorkerError(RuntimeError):
    """Fail-closed worker error with a stable machine code."""

    def __init__(self, message: str, *, code: str = "WORKER_REJECTED"):
        super().__init__(message)
        self.code = stable_error_code(code)


@dataclass(frozen=True)
class Binding:
    context: orchestration.Context
    update_request_path: Path
    update_request: dict[str, Any]
    inbox: Path
    incoming_date: date
    update_plan: daily_update.UpdateResult


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_error_code(value: Any) -> str:
    text = re.sub(r"[^A-Z0-9]+", "_", str(value).upper()).strip("_")
    if not text or not text[0].isalpha():
        text = f"WORKER_{text or 'REJECTED'}"
    if len(text) < 2:
        text = f"WORKER_{text}"
    return text[:64]


def result_boundary(status: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "status": status,
        "purpose_ceiling": PURPOSE_CEILING,
        "network_used": False,
        "credentials_used": False,
        "scheduler_created": False,
        "formal_data_written": False,
        "formal_catalog_written": False,
        "source_admission": False,
        "quality_promotion": False,
        "research_ready": False,
    }


def require_no_reparse_chain(
    workspace_root: Path, path: Path, label: str
) -> Path:
    root = workspace_root.resolve()
    lexical = Path(os.path.abspath(path))
    try:
        lexical.relative_to(root)
    except ValueError as exc:
        raise WorkerError(
            f"{label} must be lexically below the workspace",
            code="WORKER_PATH_REJECTED",
        ) from exc
    current = lexical
    while True:
        if orchestration.is_reparse(current):
            raise WorkerError(
                f"{label} or its parent chain contains a reparse/symlink path",
                code="WORKER_PATH_REJECTED",
            )
        if current == root:
            break
        parent = current.parent
        if parent == current:
            raise WorkerError(
                f"{label} parent chain escaped the workspace",
                code="WORKER_PATH_REJECTED",
            )
        current = parent
    return lexical


def regular_workspace_file(workspace_root: Path, path: Path, label: str) -> Path:
    lexical = require_no_reparse_chain(workspace_root, path, label)
    resolved = lexical.resolve()
    if (
        not resolved.is_file()
        or not orchestration.is_under(resolved, workspace_root)
    ):
        raise WorkerError(
            f"{label} must be a regular file below the workspace",
            code="WORKER_PATH_REJECTED",
        )
    return resolved


def validate_binding(
    *,
    workspace_root: Path,
    orchestration_request_path: Path,
    update_request_path: Path,
    inbox: Path,
    through_date: date,
    output_root: Path | None = None,
) -> Binding:
    workspace_root = workspace_root.resolve()
    context = orchestration.validate_workspace(
        workspace_root,
        regular_workspace_file(
            workspace_root,
            orchestration_request_path,
            "orchestration request",
        ),
    )
    update_request_path = regular_workspace_file(
        workspace_root, update_request_path, "update request"
    )
    update_request = daily_update.strict_request(update_request_path)
    for field in ("dataset_scope", "dataset_id", "source_id"):
        if update_request[field] != context.request[field]:
            raise WorkerError(
                f"{field} differs between orchestration and update requests",
                code="WORKER_IDENTITY_BINDING_MISMATCH",
            )

    lexical_inbox = Path(os.path.abspath(inbox))
    if not lexical_inbox.is_dir():
        raise WorkerError(
            "inbox must be an existing directory below the workspace",
            code="WORKER_PATH_REJECTED",
        )
    require_no_reparse_chain(workspace_root, lexical_inbox, "inbox")
    inbox = daily_update.validate_inbox(workspace_root, lexical_inbox)
    inputs = daily_update.discover_inputs(
        inbox,
        update_request["include_patterns"],
        update_request["limits"],
    )
    input_dates: set[date] = set()
    date_column = update_request["table"]["date_column"]
    incoming_rows = 0
    for item in inputs:
        for row in daily_update.iter_input_rows(item, update_request["table"]):
            input_dates.add(row[date_column])
            incoming_rows += 1
            if len(input_dates) > 1:
                raise WorkerError(
                    "one worker invocation may contain only one input date",
                    code="WORKER_MULTIPLE_INPUT_DATES",
                )
    if incoming_rows == 0 or len(input_dates) != 1:
        raise WorkerError(
            "worker input must contain exactly one non-empty date",
            code="WORKER_INPUT_DATE_MISSING",
        )
    incoming_date = next(iter(input_dates))
    if incoming_date.isoformat() not in context.request["calendar_dates"]:
        raise WorkerError(
            "input date is absent from the frozen orchestration calendar",
            code="WORKER_DATE_OUTSIDE_CALENDAR",
        )
    if incoming_date > through_date:
        raise WorkerError(
            "input date is later than through_date",
            code="WORKER_DATE_AFTER_THROUGH_DATE",
        )

    update_plan = daily_update.run(
        workspace_root=workspace_root,
        request_path=update_request_path,
        inbox=inbox,
        output_root=output_root,
        execute=False,
    )
    if update_plan.status != "PLAN":
        raise WorkerError(
            f"unexpected update plan status: {update_plan.status}",
            code="WORKER_UPDATE_PLAN_REJECTED",
        )
    return Binding(
        context=context,
        update_request_path=update_request_path,
        update_request=update_request,
        inbox=inbox,
        incoming_date=incoming_date,
        update_plan=update_plan,
    )


def read_quarantine_reason(result: daily_update.UpdateResult) -> str:
    if result.quarantine_path is None:
        return "LOCAL_UPDATE_QUARANTINED"
    failure_path = Path(result.quarantine_path) / "failure.json"
    try:
        payload = daily_update.read_json(failure_path)
    except daily_update.DailyUpdateError:
        return "LOCAL_UPDATE_QUARANTINED"
    return stable_error_code(payload.get("reason", "LOCAL_UPDATE_QUARANTINED"))


def require_unchanged_binding(initial: Binding, current: Binding, stage: str) -> None:
    if (
        current.context.request_sha256 != initial.context.request_sha256
        or current.update_request != initial.update_request
        or current.incoming_date != initial.incoming_date
        or current.update_plan.request_fingerprint
        != initial.update_plan.request_fingerprint
    ):
        raise WorkerError(
            f"validated request/input binding drifted during {stage}",
            code="WORKER_BINDING_DRIFT",
        )


def revalidate_binding(
    initial: Binding,
    *,
    workspace_root: Path,
    orchestration_request_path: Path,
    update_request_path: Path,
    inbox: Path,
    through_date: date,
    output_root: Path | None,
    stage: str,
) -> Binding:
    try:
        current = validate_binding(
            workspace_root=workspace_root,
            orchestration_request_path=orchestration_request_path,
            update_request_path=update_request_path,
            inbox=inbox,
            through_date=through_date,
            output_root=output_root,
        )
    except Exception as exc:
        raise WorkerError(
            f"validated request/input binding could not be rechecked during {stage}: {exc}",
            code="WORKER_BINDING_REVALIDATION_FAILED",
        ) from exc
    require_unchanged_binding(initial, current, stage)
    return current


def verify_result_binding(
    *,
    binding: Binding,
    update_result: daily_update.UpdateResult,
    workspace_root: Path,
) -> tuple[Path, str]:
    if update_result.candidate_path is None:
        raise WorkerError(
            "successful update result lacks candidate_path",
            code="WORKER_RESULT_PATH_MISSING",
        )
    candidate = Path(update_result.candidate_path).resolve()
    if not orchestration.is_under(candidate, workspace_root.resolve() / "tmp"):
        raise WorkerError(
            "candidate result escaped workspace tmp",
            code="WORKER_RESULT_PATH_REJECTED",
        )
    try:
        manifest = daily_update.verify_candidate(candidate)
        candidate_request = daily_update.read_json(candidate / "request.json")
        if candidate_request != binding.update_request:
            raise WorkerError(
                "sealed candidate request differs from validated request",
                code="WORKER_RESULT_BINDING_DRIFT",
            )
        expected_fingerprint = binding.update_plan.request_fingerprint
        if (
            expected_fingerprint is None
            or update_result.request_fingerprint != expected_fingerprint
            or manifest.get("request_fingerprint") != expected_fingerprint
            or manifest.get("dataset_id") != binding.update_request["dataset_id"]
            or manifest.get("run_id") != binding.update_request["run_id"]
        ):
            raise WorkerError(
                "sealed candidate fingerprint or identity differs from validated plan",
                code="WORKER_RESULT_BINDING_DRIFT",
            )

        raw_inventory = [
            item
            for item in manifest.get("payload_inventory", [])
            if isinstance(item, dict) and item.get("layer") == "raw"
        ]
        if not raw_inventory:
            raise WorkerError(
                "sealed candidate has no bound raw input inventory",
                code="WORKER_RESULT_RAW_INVENTORY_MISSING",
            )
        candidate_dates: set[date] = set()
        date_column = binding.update_request["table"]["date_column"]
        candidate_rows = 0
        for item in raw_inventory:
            relative_path = item.get("relative_path")
            if not isinstance(relative_path, str):
                raise WorkerError(
                    "sealed candidate raw inventory path is invalid",
                    code="WORKER_RESULT_RAW_INVENTORY_INVALID",
                )
            raw_path = (candidate / relative_path).resolve()
            input_file = daily_update.InputFile(
                path=raw_path,
                relative_path=relative_path,
                bytes=item["bytes"],
                sha256=item["sha256"],
            )
            for row in daily_update.iter_input_rows(
                input_file, binding.update_request["table"]
            ):
                candidate_dates.add(row[date_column])
                candidate_rows += 1
        if candidate_rows == 0 or candidate_dates != {binding.incoming_date}:
            raise WorkerError(
                "sealed candidate raw input date differs from claimed input date",
                code="WORKER_RESULT_DATE_BINDING_DRIFT",
            )
        result_sha256 = daily_update.sha256_file(candidate / "manifest.json")
    except WorkerError:
        raise
    except (daily_update.DailyUpdateError, OSError, KeyError, TypeError) as exc:
        raise WorkerError(
            f"sealed candidate verification failed: {exc}",
            code="WORKER_RESULT_VERIFICATION_FAILED",
        ) from exc
    return candidate, result_sha256


def execution_error_code(exc: Exception) -> str:
    if isinstance(exc, WorkerError):
        return exc.code
    if isinstance(exc, daily_update.DailyUpdateError):
        return stable_error_code(exc.code)
    return "WORKER_EXECUTION_EXCEPTION"


def run(
    *,
    workspace_root: Path,
    orchestration_request_path: Path,
    update_request_path: Path,
    inbox: Path,
    through_date: date,
    worker_id: str,
    claim_event_id: str,
    finish_event_id: str,
    output_root: Path | None = None,
    execute: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    orchestration.safe_component(worker_id, "worker_id")
    orchestration.safe_component(claim_event_id, "claim_event_id")
    orchestration.safe_component(finish_event_id, "finish_event_id")
    if claim_event_id == finish_event_id:
        raise WorkerError(
            "claim and finish event IDs must differ",
            code="WORKER_EVENT_ID_COLLISION",
        )
    binding = validate_binding(
        workspace_root=workspace_root,
        orchestration_request_path=orchestration_request_path,
        update_request_path=update_request_path,
        inbox=inbox,
        through_date=through_date,
        output_root=output_root,
    )
    base = result_boundary("PLAN")
    base.update(
        {
            "job_id": binding.context.request["job_id"],
            "dataset_id": binding.context.request["dataset_id"],
            "source_id": binding.context.request["source_id"],
            "run_id": binding.update_request["run_id"],
            "incoming_date": binding.incoming_date.isoformat(),
            "candidate_path": binding.update_plan.candidate_path,
            "request_fingerprint": binding.update_plan.request_fingerprint,
            "row_count": binding.update_plan.row_count,
            "new_row_count": binding.update_plan.new_row_count,
        }
    )
    if not execute:
        return base

    initialization = orchestration.initialize(binding.context, now=now)
    schedule = orchestration.plan(binding.context, through_date=through_date)
    next_date = schedule["next_unfinished_date"]
    if next_date is None:
        base.update({"status": "NO_DUE_RUN", "initialization": initialization})
        return base
    if next_date != binding.incoming_date.isoformat():
        base.update(
            {
                "status": "INPUT_NOT_EARLIEST_DUE",
                "initialization": initialization,
                "next_unfinished_date": next_date,
            }
        )
        return base

    claim = orchestration.claim(
        binding.context,
        event_id=claim_event_id,
        worker_id=worker_id,
        through_date=through_date,
        now=now,
    )
    if claim["status"] != "CLAIMED":
        base.update(
            {
                "status": claim["status"],
                "initialization": initialization,
                "claim": claim,
            }
        )
        return base
    if claim["run_date"] != binding.incoming_date.isoformat():
        failure = orchestration.fail(
            binding.context,
            event_id=finish_event_id,
            run_date=date.fromisoformat(claim["run_date"]),
            claim_token=claim["claim_token"],
            error_code="WORKER_DATE_BINDING_MISMATCH",
            now=now,
        )
        raise WorkerError(
            f"claimed date {claim['run_date']} differs from input date",
            code=failure["status"],
        )

    execution_status = "WORKER_FAILED"
    quarantine_path: str | None = None
    try:
        revalidate_binding(
            binding,
            workspace_root=workspace_root,
            orchestration_request_path=orchestration_request_path,
            update_request_path=update_request_path,
            inbox=inbox,
            through_date=through_date,
            output_root=output_root,
            stage="pre-execution",
        )
        update_result = daily_update.run(
            workspace_root=workspace_root,
            request_path=binding.update_request_path,
            inbox=binding.inbox,
            output_root=output_root,
            execute=True,
        )
        if update_result.status not in SUCCESS_STATUSES:
            execution_status = "ENGINE_QUARANTINED"
            quarantine_path = update_result.quarantine_path
            raise WorkerError(
                "daily update engine quarantined the candidate",
                code=read_quarantine_reason(update_result),
            )
        candidate, result_sha256 = verify_result_binding(
            binding=binding,
            update_result=update_result,
            workspace_root=workspace_root,
        )
        revalidate_binding(
            binding,
            workspace_root=workspace_root,
            orchestration_request_path=orchestration_request_path,
            update_request_path=update_request_path,
            inbox=inbox,
            through_date=through_date,
            output_root=output_root,
            stage="post-execution",
        )
    except Exception as exc:
        error_code = execution_error_code(exc)
        failure = orchestration.fail(
            binding.context,
            event_id=finish_event_id,
            run_date=binding.incoming_date,
            claim_token=claim["claim_token"],
            error_code=error_code,
            now=now,
        )
        base.update(
            {
                "status": execution_status,
                "error_code": error_code,
                "failure": failure,
                "claim": claim,
            }
        )
        if quarantine_path is not None:
            base["quarantine_path"] = quarantine_path
        return base
    outcome = (
        "NO_CHANGE" if update_result.status == "NO_CHANGE" else "SUCCEEDED"
    )
    completion = orchestration.complete(
        binding.context,
        event_id=finish_event_id,
        run_date=binding.incoming_date,
        claim_token=claim["claim_token"],
        result_sha256=result_sha256,
        outcome=outcome,
        now=now,
    )
    base.update(
        {
            "status": update_result.status,
            "candidate_path": candidate.as_posix(),
            "result_sha256": result_sha256,
            "row_count": update_result.row_count,
            "new_row_count": update_result.new_row_count,
            "duplicate_row_count": update_result.duplicate_row_count,
            "partition_count": update_result.partition_count,
            "claim": claim,
            "completion": completion,
        }
    )
    return base


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one source-independent INSPECTION_ONLY local daily worker step"
        )
    )
    parser.add_argument("--workspace-root", type=Path, default=ROOT)
    parser.add_argument("--orchestration-request", type=Path, required=True)
    parser.add_argument("--update-request", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--through-date", type=date.fromisoformat, required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--claim-event-id", required=True)
    parser.add_argument("--finish-event-id", required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = run(
            workspace_root=args.workspace_root,
            orchestration_request_path=args.orchestration_request,
            update_request_path=args.update_request,
            inbox=args.inbox,
            through_date=args.through_date,
            worker_id=args.worker_id,
            claim_event_id=args.claim_event_id,
            finish_event_id=args.finish_event_id,
            output_root=args.output_root,
            execute=args.execute,
        )
    except (
        WorkerError,
        orchestration.OrchestrationError,
        daily_update.DailyUpdateError,
    ) as exc:
        code = stable_error_code(getattr(exc, "code", "WORKER_REJECTED"))
        print(
            canonical_json(
                {
                    **result_boundary("REJECTED"),
                    "error_code": code,
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(canonical_json(result))
    return 0 if result["status"] in {
        "PLAN",
        "CANDIDATE_PUBLISHED",
        "NO_CHANGE",
        "NO_DUE_RUN",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
