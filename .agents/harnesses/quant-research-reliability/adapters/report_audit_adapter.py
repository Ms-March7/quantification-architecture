from __future__ import annotations

from pathlib import Path
from typing import Any


def run_case(
    *,
    case: dict[str, Any],
    target: dict[str, Any],
    harness_root: Path,
    run_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    fixture = case.get("fixture")
    if not fixture:
        return {
            "status": "FAIL",
            "message": "report audit case has no fixture",
            "commands": [],
            "artifacts": [],
        }
    fixture_path = (harness_root / fixture).resolve()
    if not fixture_path.exists():
        return {
            "status": "FAIL",
            "message": f"fixture does not exist: {fixture_path}",
            "commands": [],
            "artifacts": [],
        }
    if fixture_path.is_dir():
        artifacts = [path.resolve().as_posix() for path in fixture_path.rglob("*") if path.is_file()]
    else:
        artifacts = [fixture_path.as_posix()]
    return {
        "status": "PASS",
        "message": "report fixture collected",
        "commands": [],
        "artifacts": artifacts,
    }
