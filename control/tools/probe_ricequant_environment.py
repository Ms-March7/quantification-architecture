from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = (
    WORKSPACE_ROOT / "tmp" / "ricequant-capability-probe"
).resolve()

MODULES = (
    "rqsdk",
    "rqdatac",
    "rqfactor",
    "rqoptimizer",
    "rqalpha",
    "rqalpha_plus",
)

DISTRIBUTIONS = (
    "rqsdk",
    "rqdatac",
    "rqfactor",
    "rqoptimizer",
    "rqalpha-plus",
)


def _distribution_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in DISTRIBUTIONS:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _command_path(name: str) -> str | None:
    direct = Path(sys.executable).resolve().parent / f"{name}.exe"
    if direct.is_file():
        return str(direct)
    return shutil.which(name)


def build_payload() -> dict[str, Any]:
    module_presence = {
        name: importlib.util.find_spec(name) is not None for name in MODULES
    }
    rqdata_available = module_presence["rqdatac"]
    return {
        "schema_version": 1,
        "probe_id": "ricequant-environment-offline-v1",
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "OFFLINE_RUNTIME_PRESENT__LIVE_CAPABILITY_NOT_TESTED"
            if rqdata_available
            else "OFFLINE_RUNTIME_INCOMPLETE__LIVE_CAPABILITY_NOT_TESTED"
        ),
        "scope": "OFFLINE_PACKAGE_AND_COMMAND_DISCOVERY_ONLY",
        "formal_effect": "NONE",
        "network_requests": 0,
        "api_calls": 0,
        "credentials_read": False,
        "credentials_written": False,
        "license_value_captured": False,
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable": str(Path(sys.executable).resolve()),
            "platform": platform.platform(),
        },
        "modules": module_presence,
        "distributions": _distribution_versions(),
        "commands": {
            "rqsdk": _command_path("rqsdk"),
            "rqalpha-plus": _command_path("rqalpha-plus"),
        },
        "limitations": [
            "No license or entitlement was inspected.",
            "No market, fundamental, fund, factor, risk, or backtest data was requested.",
            "Package presence does not imply module entitlement or formal source admission.",
        ],
    }


def validate_output_path(value: str | Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = DEFAULT_OUTPUT_ROOT / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(DEFAULT_OUTPUT_ROOT)
    except ValueError as exc:
        raise ValueError(
            f"output must stay inside {DEFAULT_OUTPUT_ROOT}"
        ) from exc
    if resolved.suffix.lower() != ".json":
        raise ValueError("output must be a .json file")
    return resolved


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect the local Ricequant runtime without API or license access."
    )
    parser.add_argument(
        "--output",
        help=(
            "Optional JSON output path. Relative paths are rooted under "
            "tmp/ricequant-capability-probe; absolute paths must remain there."
        ),
    )
    parser.add_argument(
        "--require-rqdata",
        action="store_true",
        help="Return exit code 2 when rqdatac is not installed.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_payload()
    if args.output:
        output_path = validate_output_path(args.output)
        write_json_atomic(output_path, payload)
        payload["output_path"] = str(output_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if args.require_rqdata and not payload["modules"]["rqdatac"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
