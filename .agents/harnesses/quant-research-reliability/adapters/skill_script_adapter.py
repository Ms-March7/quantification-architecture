from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CommandResult:
    command: list[str]
    cwd: str
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _replacement_value(token: str, values: dict[str, Any]) -> Any:
    if token.startswith("{") and token.endswith("}") and token.count("{") == 1:
        return values.get(token[1:-1], "")
    rendered = token
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", _stringify(value))
    return rendered


def render_command(command: list[Any], values: dict[str, Any]) -> list[str]:
    rendered: list[str] = []
    for item in command:
        value = _replacement_value(str(item), values)
        if isinstance(value, list):
            rendered.extend(_stringify(part) for part in value if _stringify(part))
        elif isinstance(value, tuple):
            rendered.extend(_stringify(part) for part in value if _stringify(part))
        else:
            text = _stringify(value)
            if text:
                rendered.append(text)
    if rendered and rendered[0].lower() in {"py", "python", "python3"}:
        rendered[0] = sys.executable
    return rendered


def _command_sets(entrypoint: dict[str, Any]) -> list[list[Any]]:
    if "command" in entrypoint:
        commands = [entrypoint["command"]]
    elif "commands" in entrypoint:
        raw = entrypoint["commands"]
        if raw and all(isinstance(item, str) for item in raw):
            commands = [raw]
        else:
            commands = list(raw or [])
    else:
        commands = []
    commands.extend(entrypoint.get("post_commands") or [])
    return commands


def run_case(
    *,
    case: dict[str, Any],
    target: dict[str, Any],
    harness_root: Path,
    run_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    entrypoint_name = case.get("adapter_entrypoint")
    if not entrypoint_name:
        return {
            "status": "FAIL",
            "message": "case has no adapter_entrypoint",
            "commands": [],
            "artifacts": [],
        }
    entrypoints = target.get("entrypoints") or {}
    entrypoint = entrypoints.get(entrypoint_name)
    if not entrypoint:
        return {
            "status": "FAIL",
            "message": f"target has no entrypoint {entrypoint_name!r}",
            "commands": [],
            "artifacts": [],
        }

    params = dict(case.get("params") or {})
    fixture = case.get("fixture")
    if fixture:
        fixture_path = (harness_root / fixture).resolve()
    else:
        fixture_path = harness_root
    values = {
        **params,
        "fixture": fixture_path.as_posix(),
        "run_dir": run_dir.as_posix(),
        "harness_root": harness_root.as_posix(),
    }
    cwd = entrypoint.get("cwd") or target.get("path") or harness_root
    cwd_path = Path(str(cwd)).resolve()

    command_results: list[CommandResult] = []
    worst_status = "PASS"
    message = "commands completed"
    for raw_command in _command_sets(entrypoint):
        command = render_command(raw_command, values)
        if not command:
            continue
        start = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
        )
        duration = time.monotonic() - start
        result = CommandResult(
            command=command,
            cwd=str(cwd_path),
            exit_code=completed.returncode,
            duration_seconds=duration,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        command_results.append(result)
        if completed.returncode != 0:
            worst_status = "FAIL"
            message = f"command failed with exit code {completed.returncode}"
            break

    artifacts = []
    for rel in target.get("expected_artifacts") or []:
        artifacts.append((run_dir / rel).resolve())

    return {
        "status": worst_status,
        "message": message,
        "commands": [result.__dict__ for result in command_results],
        "artifacts": [path.as_posix() for path in artifacts],
    }
