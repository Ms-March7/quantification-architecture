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
    return {
        "status": "REVIEW",
        "message": "MCP live probes are deferred until the quant_data server exists and live mode is explicitly selected.",
        "commands": [],
        "artifacts": [],
    }
