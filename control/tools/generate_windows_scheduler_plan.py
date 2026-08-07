#!/usr/bin/env python3
"""Generate and verify inspection-only Windows Task Scheduler XML plans.

The module is deliberately plan-only.  It never registers a task, launches a
process, opens a network connection, handles credentials, writes formal data,
mutates the catalog, or promotes quality.  The only optional write is an
atomic, non-overwriting bundle below ``tmp/scheduler-plan-generator``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any, Sequence

import run_daily_update_local_smoke as daily_update
import run_local_daily_orchestration as orchestration


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = (
    ROOT / "data" / "catalog" / "contracts" / "scheduler_plan_generator_v1"
)
REQUEST_SCHEMA_REL = Path(
    "data/catalog/contracts/scheduler_plan_generator_v1/request.schema.json"
)
GENERATOR_REL = Path("control/tools/generate_windows_scheduler_plan.py")
WORKER_REL = Path("control/tools/run_local_daily_inspection_worker.py")
ORCHESTRATION_REL = Path("control/tools/run_local_daily_orchestration.py")
DAILY_UPDATE_REL = Path("control/tools/run_daily_update_local_smoke.py")
PYTHON_REL = Path(
    "control/runtime/data-engineering/.venv/Scripts/python.exe"
)
OUTPUT_ROOT_REL = Path("tmp/scheduler-plan-generator")

ENGINE_ID = "windows_task_scheduler_plan_generator/xml-v1"
MODE = "scheduler_plan_generator"
PURPOSE_CEILING = "INSPECTION_ONLY"
TIMEZONE = "Asia/Shanghai"
WINDOWS_TIMEZONE_ID = "China Standard Time"
ACCOUNT_PLACEHOLDER = "__WINDOWS_ACCOUNT_PLACEHOLDER__"
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
MAX_JOBS = 366
SECRET_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|secret|credential|cookie|api[ _-]?key|"
    r"access[ _-]?key|token|账号|密码|密钥|令牌)"
)
COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TIME_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

SAFETY_FIELDS = {
    "task_registration": False,
    "network_allowed": False,
    "credentials_allowed": False,
    "formal_data_write": False,
    "formal_catalog_write": False,
    "source_admission": False,
    "quality_promotion": False,
    "research_ready": False,
}


class SchedulerPlanError(RuntimeError):
    """Fail-closed plan error with a stable machine code."""

    def __init__(self, message: str, *, code: str = "SCHEDULER_PLAN_REJECTED"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BoundJob:
    trade_date: date
    update_request_path: Path
    update_request_relative: str
    update_request: dict[str, Any]
    inbox_path: Path
    inbox_relative: str


@dataclass(frozen=True)
class BoundRequest:
    workspace_root: Path
    request_path: Path
    request_sha256: str
    request: dict[str, Any]
    orchestration_request_path: Path
    orchestration_request_relative: str
    orchestration_request_sha256: str
    orchestration_request: dict[str, Any]
    jobs: tuple[BoundJob, ...]
    tool_paths: dict[str, Path]
    tool_hashes: dict[str, str]


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


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SchedulerPlanError(
            f"invalid JSON file: {path}", code="SCHEDULER_PLAN_JSON_INVALID"
        ) from exc


def require_exact_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        missing = sorted(required - set(value) if isinstance(value, dict) else required)
        extra = sorted(set(value) - required if isinstance(value, dict) else [])
        raise SchedulerPlanError(
            f"{label} keys are not exact; missing={missing}, extra={extra}",
            code="SCHEDULER_PLAN_SCHEMA_REJECTED",
        )
    return value


def parse_iso_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise SchedulerPlanError(
            f"{label} must be an ISO date",
            code="SCHEDULER_PLAN_SCHEMA_REJECTED",
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SchedulerPlanError(
            f"{label} must be an ISO date",
            code="SCHEDULER_PLAN_SCHEMA_REJECTED",
        ) from exc
    if parsed.isoformat() != value:
        raise SchedulerPlanError(
            f"{label} must be a canonical ISO date",
            code="SCHEDULER_PLAN_SCHEMA_REJECTED",
        )
    return parsed


def parse_local_time(value: Any) -> time:
    if not isinstance(value, str) or not TIME_PATTERN.fullmatch(value):
        raise SchedulerPlanError(
            "trigger_local_time must be canonical HH:MM:SS",
            code="SCHEDULER_PLAN_SCHEMA_REJECTED",
        )
    return time.fromisoformat(value)


def path_is_reparse(path: Path) -> bool:
    try:
        return orchestration.is_reparse(path)
    except OSError as exc:
        raise SchedulerPlanError(
            f"could not inspect path metadata: {path}",
            code="SCHEDULER_PLAN_PATH_REJECTED",
        ) from exc


def lexical_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def require_no_existing_reparse_chain(
    workspace_root: Path, path: Path, label: str
) -> None:
    root = Path(os.path.abspath(workspace_root))
    current = Path(os.path.abspath(path))
    if not lexical_under(current, root):
        raise SchedulerPlanError(
            f"{label} escapes the workspace",
            code="SCHEDULER_PLAN_PATH_REJECTED",
        )
    while True:
        if current.exists() and path_is_reparse(current):
            raise SchedulerPlanError(
                f"{label} contains a reparse/symlink path",
                code="SCHEDULER_PLAN_PATH_REJECTED",
            )
        if current == root:
            return
        parent = current.parent
        if parent == current:
            raise SchedulerPlanError(
                f"{label} parent chain escaped the workspace",
                code="SCHEDULER_PLAN_PATH_REJECTED",
            )
        current = parent


def normalize_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SchedulerPlanError(
            f"{label} must be a non-empty relative path",
            code="SCHEDULER_PLAN_PATH_REJECTED",
        )
    normalized = value.replace("\\", "/")
    candidate = Path(normalized)
    if (
        candidate.is_absolute()
        or candidate.drive
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise SchedulerPlanError(
            f"{label} must be a normalized workspace-relative path",
            code="SCHEDULER_PLAN_PATH_REJECTED",
        )
    return candidate.as_posix()


def resolve_relative(
    workspace_root: Path,
    value: Any,
    label: str,
    *,
    must_exist_file: bool = False,
    required_parent: Path | None = None,
) -> tuple[Path, str]:
    relative = normalize_relative_path(value, label)
    lexical = Path(os.path.abspath(workspace_root / Path(relative)))
    require_no_existing_reparse_chain(workspace_root, lexical, label)
    if required_parent is not None and not lexical_under(
        lexical, Path(os.path.abspath(required_parent))
    ):
        raise SchedulerPlanError(
            f"{label} must remain below {required_parent}",
            code="SCHEDULER_PLAN_PATH_REJECTED",
        )
    if must_exist_file and not lexical.is_file():
        raise SchedulerPlanError(
            f"{label} must be an existing regular file",
            code="SCHEDULER_PLAN_PATH_REJECTED",
        )
    return lexical, relative


def strict_request(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    required = {
        "schema_version",
        "mode",
        "purpose_ceiling",
        "plan_id",
        "timezone",
        "windows_timezone_id",
        "account_placeholder",
        "trigger_local_time",
        "orchestration_request",
        "jobs",
        "safety",
        "authorization_note",
    }
    payload = require_exact_keys(payload, required, "request")
    if payload["schema_version"] != 1 or payload["mode"] != MODE:
        raise SchedulerPlanError(
            "request version or mode is unsupported",
            code="SCHEDULER_PLAN_SCHEMA_REJECTED",
        )
    if payload["purpose_ceiling"] != PURPOSE_CEILING:
        raise SchedulerPlanError(
            "purpose_ceiling must remain INSPECTION_ONLY",
            code="SCHEDULER_PLAN_FORMAL_SCOPE_REJECTED",
        )
    if not isinstance(payload["plan_id"], str) or not COMPONENT_PATTERN.fullmatch(
        payload["plan_id"]
    ):
        raise SchedulerPlanError(
            "plan_id is invalid", code="SCHEDULER_PLAN_SCHEMA_REJECTED"
        )
    if payload["timezone"] != TIMEZONE:
        raise SchedulerPlanError(
            "timezone must be Asia/Shanghai",
            code="SCHEDULER_PLAN_TIMEZONE_REJECTED",
        )
    if payload["windows_timezone_id"] != WINDOWS_TIMEZONE_ID:
        raise SchedulerPlanError(
            "windows_timezone_id must be China Standard Time",
            code="SCHEDULER_PLAN_TIMEZONE_REJECTED",
        )
    if payload["account_placeholder"] != ACCOUNT_PLACEHOLDER:
        raise SchedulerPlanError(
            "only the non-secret account placeholder is accepted",
            code="SCHEDULER_PLAN_CREDENTIAL_REJECTED",
        )
    parse_local_time(payload["trigger_local_time"])
    normalize_relative_path(
        payload["orchestration_request"], "orchestration_request"
    )

    safety = require_exact_keys(payload["safety"], set(SAFETY_FIELDS), "safety")
    for field, expected in SAFETY_FIELDS.items():
        if safety[field] is not expected:
            raise SchedulerPlanError(
                f"safety.{field} must remain false",
                code="SCHEDULER_PLAN_FORMAL_SCOPE_REJECTED",
            )

    note = payload["authorization_note"]
    if (
        not isinstance(note, str)
        or not 1 <= len(note) <= 500
        or SECRET_PATTERN.search(note)
    ):
        raise SchedulerPlanError(
            "authorization_note is invalid or contains credential-like text",
            code="SCHEDULER_PLAN_CREDENTIAL_REJECTED",
        )

    jobs = payload["jobs"]
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_JOBS:
        raise SchedulerPlanError(
            f"jobs must contain 1..{MAX_JOBS} entries",
            code="SCHEDULER_PLAN_SCHEMA_REJECTED",
        )
    seen_dates: set[str] = set()
    seen_updates: set[str] = set()
    seen_inboxes: set[str] = set()
    previous: str | None = None
    for index, raw_job in enumerate(jobs):
        job = require_exact_keys(
            raw_job, {"trade_date", "update_request", "inbox"}, f"jobs[{index}]"
        )
        parsed = parse_iso_date(job["trade_date"], f"jobs[{index}].trade_date")
        trade_date = parsed.isoformat()
        update_relative = normalize_relative_path(
            job["update_request"], f"jobs[{index}].update_request"
        )
        inbox_relative = normalize_relative_path(
            job["inbox"], f"jobs[{index}].inbox"
        )
        if previous is not None and trade_date <= previous:
            raise SchedulerPlanError(
                "jobs must be strictly sorted by unique trade_date",
                code="SCHEDULER_PLAN_SCHEMA_REJECTED",
            )
        previous = trade_date
        if (
            trade_date in seen_dates
            or update_relative in seen_updates
            or inbox_relative in seen_inboxes
        ):
            raise SchedulerPlanError(
                "job dates, update requests and inboxes must each be unique",
                code="SCHEDULER_PLAN_SCHEMA_REJECTED",
            )
        seen_dates.add(trade_date)
        seen_updates.add(update_relative)
        seen_inboxes.add(inbox_relative)
    return payload


def bind_request(workspace_root: Path, request_path: Path) -> BoundRequest:
    workspace_root = Path(os.path.abspath(workspace_root))
    if not workspace_root.is_dir() or path_is_reparse(workspace_root):
        raise SchedulerPlanError(
            "workspace root must be a regular directory",
            code="SCHEDULER_PLAN_PATH_REJECTED",
        )
    request_path = Path(os.path.abspath(request_path))
    require_no_existing_reparse_chain(workspace_root, request_path, "request")
    if not request_path.is_file():
        raise SchedulerPlanError(
            "request must be a regular workspace file",
            code="SCHEDULER_PLAN_PATH_REJECTED",
        )
    request = strict_request(request_path)

    tool_paths: dict[str, Path] = {}
    tool_hashes: dict[str, str] = {}
    fixed_paths = {
        "python_executable": PYTHON_REL,
        "worker_tool": WORKER_REL,
        "orchestration_tool": ORCHESTRATION_REL,
        "daily_update_tool": DAILY_UPDATE_REL,
        "generator_tool": GENERATOR_REL,
        "request_schema": REQUEST_SCHEMA_REL,
    }
    for label, relative in fixed_paths.items():
        path, _ = resolve_relative(
            workspace_root,
            relative.as_posix(),
            label,
            must_exist_file=True,
        )
        tool_paths[label] = path
        tool_hashes[label] = sha256_file(path)

    orchestration_path, orchestration_relative = resolve_relative(
        workspace_root,
        request["orchestration_request"],
        "orchestration_request",
        must_exist_file=True,
    )
    orchestration_payload = orchestration.validate_request(
        orchestration.read_json(orchestration_path)
    )
    if (
        orchestration_payload.get("mode") != orchestration.REQUEST_MODE
        or orchestration_payload.get("purpose_ceiling") != PURPOSE_CEILING
    ):
        raise SchedulerPlanError(
            "orchestration request is not inspection-only",
            code="SCHEDULER_PLAN_FORMAL_SCOPE_REJECTED",
        )
    if orchestration_payload.get("timezone") != TIMEZONE:
        raise SchedulerPlanError(
            "orchestration timezone differs from the scheduler plan",
            code="SCHEDULER_PLAN_TIMEZONE_REJECTED",
        )
    orchestration_dates = set(orchestration_payload["calendar_dates"])

    tmp_root = workspace_root / "tmp"
    jobs: list[BoundJob] = []
    run_ids: set[str] = set()
    for index, job in enumerate(request["jobs"]):
        trade_date = parse_iso_date(job["trade_date"], f"jobs[{index}].trade_date")
        if trade_date.isoformat() not in orchestration_dates:
            raise SchedulerPlanError(
                "every planned date must exist in the frozen orchestration calendar",
                code="SCHEDULER_PLAN_NON_TRADING_DATE_REJECTED",
            )
        update_path, update_relative = resolve_relative(
            workspace_root,
            job["update_request"],
            f"jobs[{index}].update_request",
            must_exist_file=True,
        )
        update_payload = daily_update.strict_request(update_path)
        if update_payload.get("mode") != daily_update.MODE:
            raise SchedulerPlanError(
                "update request is not the inspection-only local candidate worker mode",
                code="SCHEDULER_PLAN_FORMAL_SCOPE_REJECTED",
            )
        for field in ("dataset_scope", "dataset_id", "source_id"):
            if update_payload.get(field) != orchestration_payload.get(field):
                raise SchedulerPlanError(
                    f"{field} differs between orchestration and update requests",
                    code="SCHEDULER_PLAN_IDENTITY_BINDING_MISMATCH",
                )
        run_id = update_payload["run_id"]
        if run_id in run_ids:
            raise SchedulerPlanError(
                "each scheduled date requires a unique update run_id",
                code="SCHEDULER_PLAN_IDENTITY_BINDING_MISMATCH",
            )
        run_ids.add(run_id)
        inbox_path, inbox_relative = resolve_relative(
            workspace_root,
            job["inbox"],
            f"jobs[{index}].inbox",
            required_parent=tmp_root,
        )
        jobs.append(
            BoundJob(
                trade_date=trade_date,
                update_request_path=update_path,
                update_request_relative=update_relative,
                update_request=update_payload,
                inbox_path=inbox_path,
                inbox_relative=inbox_relative,
            )
        )

    return BoundRequest(
        workspace_root=workspace_root,
        request_path=request_path,
        request_sha256=sha256_file(request_path),
        request=request,
        orchestration_request_path=orchestration_path,
        orchestration_request_relative=orchestration_relative,
        orchestration_request_sha256=sha256_file(orchestration_path),
        orchestration_request=orchestration_payload,
        jobs=tuple(jobs),
        tool_paths=tool_paths,
        tool_hashes=tool_hashes,
    )


def windows_quote_argument(value: str) -> str:
    """Quote one argument using the Windows C runtime parsing convention."""

    if value and not re.search(r'[\s"]', value):
        return value
    result = ['"']
    backslashes = 0
    for character in value:
        if character == "\\":
            backslashes += 1
        elif character == '"':
            result.append("\\" * (backslashes * 2 + 1))
            result.append('"')
            backslashes = 0
        else:
            result.append("\\" * backslashes)
            result.append(character)
            backslashes = 0
    result.append("\\" * (backslashes * 2))
    result.append('"')
    return "".join(result)


def windows_command_line(arguments: Sequence[str]) -> str:
    return " ".join(windows_quote_argument(str(item)) for item in arguments)


def windows_path(path: Path) -> str:
    return str(path).replace("/", "\\")


def xml_tag(name: str) -> str:
    return f"{{{TASK_NAMESPACE}}}{name}"


def add_text(parent: ET.Element, name: str, text: str) -> ET.Element:
    child = ET.SubElement(parent, xml_tag(name))
    child.text = text
    return child


def build_task_xml(
    *,
    bound: BoundRequest,
    job: BoundJob,
    command: dict[str, Any],
    task_uri: str,
) -> bytes:
    ET.register_namespace("", TASK_NAMESPACE)
    task = ET.Element(xml_tag("Task"), {"version": "1.4"})
    registration = ET.SubElement(task, xml_tag("RegistrationInfo"))
    add_text(registration, "Author", "Quantification inspection-only plan generator")
    add_text(registration, "URI", task_uri)
    add_text(
        registration,
        "Description",
        (
            "PLAN ONLY; INSPECTION_ONLY; account placeholder retained; "
            f"worker_sha256={bound.tool_hashes['worker_tool']}; "
            f"orchestration_request_sha256={bound.orchestration_request_sha256}; "
            f"update_request_sha256={sha256_file(job.update_request_path)}; "
            "no network, formal write, catalog mutation, admission or promotion."
        ),
    )

    triggers = ET.SubElement(task, xml_tag("Triggers"))
    trigger = ET.SubElement(triggers, xml_tag("TimeTrigger"))
    start = f"{job.trade_date.isoformat()}T{bound.request['trigger_local_time']}+08:00"
    add_text(trigger, "StartBoundary", start)
    add_text(trigger, "Enabled", "true")

    principals = ET.SubElement(task, xml_tag("Principals"))
    principal = ET.SubElement(principals, xml_tag("Principal"), {"id": "Author"})
    add_text(principal, "UserId", ACCOUNT_PLACEHOLDER)
    add_text(principal, "LogonType", "InteractiveToken")
    add_text(principal, "RunLevel", "LeastPrivilege")

    settings = ET.SubElement(task, xml_tag("Settings"))
    add_text(settings, "MultipleInstancesPolicy", "IgnoreNew")
    add_text(settings, "DisallowStartIfOnBatteries", "false")
    add_text(settings, "StopIfGoingOnBatteries", "false")
    add_text(settings, "AllowHardTerminate", "true")
    add_text(settings, "StartWhenAvailable", "true")
    add_text(settings, "RunOnlyIfNetworkAvailable", "false")
    idle = ET.SubElement(settings, xml_tag("IdleSettings"))
    add_text(idle, "StopOnIdleEnd", "false")
    add_text(idle, "RestartOnIdle", "false")
    add_text(settings, "AllowStartOnDemand", "false")
    add_text(settings, "Enabled", "true")
    add_text(settings, "Hidden", "false")
    add_text(settings, "RunOnlyIfIdle", "false")
    add_text(settings, "WakeToRun", "false")
    add_text(settings, "ExecutionTimeLimit", "PT1H")
    add_text(settings, "Priority", "7")

    actions = ET.SubElement(task, xml_tag("Actions"), {"Context": "Author"})
    execute = ET.SubElement(actions, xml_tag("Exec"))
    add_text(execute, "Command", command["executable"])
    add_text(execute, "Arguments", command["arguments_text"])
    add_text(execute, "WorkingDirectory", command["working_directory"])

    tree = ET.ElementTree(task)
    ET.indent(tree, space="  ")
    payload = ET.tostring(
        task,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=False,
    ) + b"\n"
    validate_task_xml(payload, command=command, task_uri=task_uri, start=start)
    return payload


def find_required_text(root: ET.Element, path: str, expected: str) -> None:
    element = root.find(path, {"t": TASK_NAMESPACE})
    if element is None or element.text != expected:
        raise SchedulerPlanError(
            f"task XML binding mismatch at {path}",
            code="SCHEDULER_PLAN_XML_REJECTED",
        )


def validate_task_xml(
    payload: bytes,
    *,
    command: dict[str, Any],
    task_uri: str,
    start: str,
) -> None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise SchedulerPlanError(
            "task XML is not well formed", code="SCHEDULER_PLAN_XML_REJECTED"
        ) from exc
    if root.tag != xml_tag("Task") or root.attrib != {"version": "1.4"}:
        raise SchedulerPlanError(
            "task XML root is invalid", code="SCHEDULER_PLAN_XML_REJECTED"
        )
    bindings = {
        "./t:RegistrationInfo/t:URI": task_uri,
        "./t:Triggers/t:TimeTrigger/t:StartBoundary": start,
        "./t:Triggers/t:TimeTrigger/t:Enabled": "true",
        "./t:Principals/t:Principal/t:UserId": ACCOUNT_PLACEHOLDER,
        "./t:Principals/t:Principal/t:LogonType": "InteractiveToken",
        "./t:Principals/t:Principal/t:RunLevel": "LeastPrivilege",
        "./t:Settings/t:MultipleInstancesPolicy": "IgnoreNew",
        "./t:Settings/t:StartWhenAvailable": "true",
        "./t:Settings/t:RunOnlyIfNetworkAvailable": "false",
        "./t:Settings/t:AllowStartOnDemand": "false",
        "./t:Actions/t:Exec/t:Command": command["executable"],
        "./t:Actions/t:Exec/t:Arguments": command["arguments_text"],
        "./t:Actions/t:Exec/t:WorkingDirectory": command["working_directory"],
    }
    for path, expected in bindings.items():
        find_required_text(root, path, expected)
    serialized = payload.decode("utf-8")
    scrubbed = serialized.replace(ACCOUNT_PLACEHOLDER, "").replace(
        "InteractiveToken", ""
    )
    if SECRET_PATTERN.search(scrubbed):
        raise SchedulerPlanError(
            "task XML contains credential-like text",
            code="SCHEDULER_PLAN_CREDENTIAL_REJECTED",
        )


def build_plan(bound: BoundRequest, *, include_xml: bool) -> dict[str, Any]:
    workspace = bound.workspace_root
    worker_id = f"scheduler-{bound.request['plan_id']}"
    tasks: list[dict[str, Any]] = []
    update_hashes: dict[str, str] = {}
    for job in bound.jobs:
        compact_date = job.trade_date.strftime("%Y%m%d")
        update_hash = sha256_file(job.update_request_path)
        update_hashes[job.update_request_relative] = update_hash
        claim_id = f"claim-{bound.request['plan_id']}-{compact_date}"
        finish_id = f"finish-{bound.request['plan_id']}-{compact_date}"
        arguments = [
            windows_path(bound.tool_paths["worker_tool"]),
            "--workspace-root",
            windows_path(workspace),
            "--orchestration-request",
            windows_path(bound.orchestration_request_path),
            "--update-request",
            windows_path(job.update_request_path),
            "--inbox",
            windows_path(job.inbox_path),
            "--through-date",
            job.trade_date.isoformat(),
            "--worker-id",
            worker_id,
            "--claim-event-id",
            claim_id,
            "--finish-event-id",
            finish_id,
            "--execute",
        ]
        command = {
            "executable": windows_path(bound.tool_paths["python_executable"]),
            "arguments": arguments,
            "arguments_text": windows_command_line(arguments),
            "working_directory": windows_path(workspace),
            "expected_hashes": {
                "python_executable_sha256": bound.tool_hashes["python_executable"],
                "worker_tool_sha256": bound.tool_hashes["worker_tool"],
                "orchestration_tool_sha256": bound.tool_hashes[
                    "orchestration_tool"
                ],
                "daily_update_tool_sha256": bound.tool_hashes["daily_update_tool"],
                "orchestration_request_sha256": bound.orchestration_request_sha256,
                "update_request_sha256": update_hash,
            },
        }
        task_uri = (
            f"/Quantification/InspectionOnly/{bound.request['plan_id']}/"
            f"{job.trade_date.isoformat()}"
        )
        xml_payload = build_task_xml(
            bound=bound, job=job, command=command, task_uri=task_uri
        )
        task = {
            "trade_date": job.trade_date.isoformat(),
            "task_uri": task_uri,
            "xml_relative_path": f"tasks/{job.trade_date.isoformat()}.xml",
            "xml_sha256": sha256_bytes(xml_payload),
            "trigger": {
                "kind": "EXPLICIT_FROZEN_TRADING_DATE_TIME_TRIGGER",
                "start_boundary": (
                    f"{job.trade_date.isoformat()}T"
                    f"{bound.request['trigger_local_time']}+08:00"
                ),
                "timezone": TIMEZONE,
                "windows_timezone_id": WINDOWS_TIMEZONE_ID,
            },
            "account": {
                "user_id": ACCOUNT_PLACEHOLDER,
                "logon_type": "InteractiveToken",
                "run_level": "LeastPrivilege",
            },
            "orchestration_request": bound.orchestration_request_relative,
            "update_request": job.update_request_relative,
            "inbox": job.inbox_relative,
            "command": command,
        }
        if include_xml:
            task["xml_utf8"] = xml_payload.decode("utf-8")
        tasks.append(task)

    manifest = {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "status": "PLAN",
        "purpose_ceiling": PURPOSE_CEILING,
        "plan_id": bound.request["plan_id"],
        "request_sha256": bound.request_sha256,
        "account_placeholder": ACCOUNT_PLACEHOLDER,
        "timezone": TIMEZONE,
        "windows_timezone_id": WINDOWS_TIMEZONE_ID,
        "trade_calendar_binding": {
            "kind": "FROZEN_ORCHESTRATION_CALENDAR_SUBSET",
            "orchestration_request": bound.orchestration_request_relative,
            "orchestration_request_sha256": bound.orchestration_request_sha256,
            "planned_dates": [job.trade_date.isoformat() for job in bound.jobs],
        },
        "expected_hashes": {
            **{f"{key}_sha256": value for key, value in bound.tool_hashes.items()},
            "orchestration_request_sha256": bound.orchestration_request_sha256,
            "update_requests": update_hashes,
        },
        "replay_semantics": {
            "task_start_when_available": True,
            "fixed_event_ids_are_idempotent": True,
            "earliest_unfinished_date_enforced_by_orchestration": True,
            "inbox_arrival_or_retry_not_created_by_this_plan": True,
        },
        "mutual_exclusion": {
            "task_scheduler_multiple_instances_policy": "IgnoreNew",
            "orchestration_single_writer": "SQLite BEGIN IMMEDIATE",
            "orchestration_claim_guard": "single active claim with expiring lease",
        },
        "safety": dict(SAFETY_FIELDS),
        "installation_authorized": False,
        "runtime_preconditions": [
            "Re-verify every bound hash immediately before any separately authorized installation.",
            "Replace no account placeholder in this sealed plan; materialize a new separately authorized plan instead.",
            "The date-specific inbox must already contain exactly one normalized logical trade date.",
            "The host timezone must be China Standard Time and its clock must be correct.",
        ],
        "tasks": tasks,
    }
    return manifest


def validate_output_path(workspace_root: Path, output_dir: Path) -> Path:
    workspace_root = Path(os.path.abspath(workspace_root))
    output_root = Path(os.path.abspath(workspace_root / OUTPUT_ROOT_REL))
    output_dir = Path(os.path.abspath(output_dir))
    require_no_existing_reparse_chain(workspace_root, output_dir, "output_dir")
    if output_dir == output_root or not lexical_under(output_dir, output_root):
        raise SchedulerPlanError(
            "output_dir must be a named child below tmp/scheduler-plan-generator",
            code="SCHEDULER_PLAN_OUTPUT_REJECTED",
        )
    if output_dir.exists():
        raise SchedulerPlanError(
            "output_dir already exists; overwrite is forbidden",
            code="SCHEDULER_PLAN_OUTPUT_EXISTS",
        )
    return output_dir


def write_file_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def write_bundle(bound: BoundRequest, output_dir: Path) -> dict[str, Any]:
    output_dir = validate_output_path(bound.workspace_root, output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    require_no_existing_reparse_chain(
        bound.workspace_root, output_dir.parent, "output parent"
    )
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    if staging.exists():
        raise SchedulerPlanError(
            "unexpected staging collision", code="SCHEDULER_PLAN_OUTPUT_REJECTED"
        )
    plan = build_plan(bound, include_xml=True)
    try:
        staging.mkdir()
        write_file_exclusive(staging / "request.json", bound.request_path.read_bytes())
        tasks = plan["tasks"]
        for task in tasks:
            xml_payload = task.pop("xml_utf8").encode("utf-8")
            write_file_exclusive(staging / task["xml_relative_path"], xml_payload)
        manifest_payload = canonical_json_bytes(plan)
        write_file_exclusive(staging / "manifest.json", manifest_payload)
        os.rename(staging, output_dir)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    result = dict(plan)
    result["status"] = "PLAN_BUNDLE_WRITTEN"
    result["bundle_path"] = str(output_dir)
    result["manifest_sha256"] = sha256_file(output_dir / "manifest.json")
    return result


def verify_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise SchedulerPlanError(
            f"{label} is not a canonical SHA-256",
            code="SCHEDULER_PLAN_MANIFEST_REJECTED",
        )
    return value


def verify_existing_bundle(workspace_root: Path, bundle_dir: Path) -> dict[str, Any]:
    workspace_root = Path(os.path.abspath(workspace_root))
    output_root = Path(os.path.abspath(workspace_root / OUTPUT_ROOT_REL))
    bundle_dir = Path(os.path.abspath(bundle_dir))
    require_no_existing_reparse_chain(workspace_root, bundle_dir, "bundle_dir")
    if (
        bundle_dir == output_root
        or not lexical_under(bundle_dir, output_root)
        or not bundle_dir.is_dir()
    ):
        raise SchedulerPlanError(
            "bundle_dir must be an existing named bundle below tmp/scheduler-plan-generator",
            code="SCHEDULER_PLAN_OUTPUT_REJECTED",
        )
    request_path = bundle_dir / "request.json"
    manifest_path = bundle_dir / "manifest.json"
    if not request_path.is_file() or not manifest_path.is_file():
        raise SchedulerPlanError(
            "bundle lacks request.json or manifest.json",
            code="SCHEDULER_PLAN_MANIFEST_REJECTED",
        )
    bound = bind_request(workspace_root, request_path)
    actual = read_json(manifest_path)
    expected = build_plan(bound, include_xml=False)
    if actual != expected:
        raise SchedulerPlanError(
            "manifest differs from the current request/tool/hash binding",
            code="SCHEDULER_PLAN_MANIFEST_REJECTED",
        )
    for task in expected["tasks"]:
        xml_path = bundle_dir / task["xml_relative_path"]
        if not xml_path.is_file():
            raise SchedulerPlanError(
                "bundle task XML is missing",
                code="SCHEDULER_PLAN_XML_REJECTED",
            )
        verify_hash(task["xml_sha256"], "task xml hash")
        payload = xml_path.read_bytes()
        if sha256_bytes(payload) != task["xml_sha256"]:
            raise SchedulerPlanError(
                "bundle task XML hash mismatch",
                code="SCHEDULER_PLAN_XML_REJECTED",
            )
        validate_task_xml(
            payload,
            command=task["command"],
            task_uri=task["task_uri"],
            start=task["trigger"]["start_boundary"],
        )
    allowed = {
        "request.json",
        "manifest.json",
        *[task["xml_relative_path"] for task in expected["tasks"]],
    }
    actual_files = {
        path.relative_to(bundle_dir).as_posix()
        for path in bundle_dir.rglob("*")
        if path.is_file()
    }
    if actual_files != allowed:
        raise SchedulerPlanError(
            "bundle file inventory is not exact",
            code="SCHEDULER_PLAN_MANIFEST_REJECTED",
        )
    return {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "status": "PASS",
        "purpose_ceiling": PURPOSE_CEILING,
        "bundle_path": str(bundle_dir),
        "manifest_sha256": sha256_file(manifest_path),
        "task_count": len(expected["tasks"]),
        "safety": dict(SAFETY_FIELDS),
    }


def result_boundary(status: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "status": status,
        "purpose_ceiling": PURPOSE_CEILING,
        "safety": dict(SAFETY_FIELDS),
        "installation_authorized": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate or verify an INSPECTION_ONLY Windows Task Scheduler XML "
            "plan without registering or running anything."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--workspace-root", type=Path, default=ROOT)
    generate.add_argument("--request", type=Path, required=True)
    generate.add_argument("--write-plan", action="store_true")
    generate.add_argument("--output-dir", type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--workspace-root", type=Path, default=ROOT)
    verify.add_argument("--bundle-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "generate":
            if args.write_plan != (args.output_dir is not None):
                raise SchedulerPlanError(
                    "--write-plan and --output-dir must be supplied together",
                    code="SCHEDULER_PLAN_OUTPUT_REJECTED",
                )
            bound = bind_request(args.workspace_root, args.request)
            if args.write_plan:
                result = write_bundle(bound, args.output_dir)
            else:
                result = build_plan(bound, include_xml=True)
        else:
            result = verify_existing_bundle(args.workspace_root, args.bundle_dir)
    except (SchedulerPlanError, orchestration.OrchestrationError, daily_update.DailyUpdateError) as exc:
        print(
            canonical_json_bytes(
                {
                    **result_boundary("REJECTED"),
                    "error_code": getattr(exc, "code", "SCHEDULER_PLAN_REJECTED"),
                    "message": str(exc),
                }
            ).decode("utf-8"),
            file=sys.stderr,
            end="",
        )
        return 2
    print(canonical_json_bytes(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
