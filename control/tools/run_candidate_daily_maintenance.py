#!/usr/bin/env python3
"""Run bounded candidate-only daily maintenance and refresh its central index.

This orchestrator deliberately has no data-source client and never reads an
authentication value.  It runs bounded candidate workers as child processes,
including a local-only JoinQuant verifier, then rebuilds the local candidate
asset index.  Formal catalog publication, quality promotion, factor execution
and snapshot compaction are outside this program.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import msvcrt
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "data/catalog/contracts/candidate_daily_maintenance_v1/contract.json"
FORMAL_CATALOG_PATH = ROOT / "data/catalog/datasets.json"
CANDIDATE_INDEX_PATH = ROOT / "data/catalog/candidate_assets.json"
TRANSACTION_PARENT = ROOT / "data/market/transactions/candidate-daily-maintenance"
LOCK_PATH = TRANSACTION_PARENT / ".maintenance.lock"
SHANGHAI = ZoneInfo("Asia/Shanghai")
PRODUCER_ID = "run_candidate_daily_maintenance.py/v1"
MAX_CAPTURE_CHARS = 2000


class MaintenanceError(RuntimeError):
    """Fail-closed maintenance contract violation."""


@dataclass(frozen=True)
class Step:
    name: str
    script: Path
    mode_args: tuple[str, ...]


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MaintenanceError(f"JSON object required: {path}")
    return value


def load_contract() -> dict[str, Any]:
    contract = load_json(CONTRACT_PATH)
    if contract.get("contract_id") != "candidate_daily_maintenance_v1":
        raise MaintenanceError("unexpected candidate maintenance contract")
    safety = contract.get("safety", {})
    forbidden = (
        "formal_catalog_write",
        "formal_snapshot_write",
        "quality_promotion",
        "research_run",
        "validation_or_holdout_consumption",
        "credential_value_read",
        "credential_value_capture",
    )
    if any(safety.get(key) is not False for key in forbidden):
        raise MaintenanceError("maintenance safety contract is not fail-closed")
    if safety.get("purpose_ceiling") != "INSPECTION_ONLY":
        raise MaintenanceError("maintenance purpose ceiling must be INSPECTION_ONLY")
    if safety.get("candidate_index_write") is not True:
        raise MaintenanceError("candidate index write must be explicitly enabled")
    expected_order = [
        "rqdata_shsz_price_volume",
        "rqdata_b_path",
        "rqdata_financial_pit",
        "joinquant_share_capital_pit",
        "candidate_asset_index",
    ]
    if contract.get("step_order") != expected_order:
        raise MaintenanceError("candidate maintenance step order is not the approved fail-closed order")
    entrypoints = contract.get("entrypoints")
    if not isinstance(entrypoints, dict) or set(entrypoints) != set(expected_order):
        raise MaintenanceError("candidate maintenance entrypoints do not match the approved steps")
    return contract


def default_through() -> date:
    now = datetime.now(SHANGHAI)
    return now.date() if now.time() >= time(16, 0) else now.date() - timedelta(days=1)


def safe_text(value: str) -> str:
    text = re.sub(r"(?:tcp|rqdata|rqdatac)://\S+", "<redacted>", value)
    text = re.sub(
        r"(?i)\b(license|token|password|secret|api[_-]?key)\b\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    return text[-MAX_CAPTURE_CHARS:]


def parse_json_output(value: str) -> dict[str, Any]:
    text = value.strip()
    if not text:
        raise MaintenanceError("child process returned no JSON")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise MaintenanceError("child process JSON must be an object")
    return parsed


def acquire_lock() -> int:
    TRANSACTION_PARENT.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        os.close(descriptor)
        raise MaintenanceError(f"maintenance lock is held by another process: {LOCK_PATH}") from exc
    try:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, canonical_bytes({
            "pid": os.getpid(),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "producer": PRODUCER_ID,
        }))
        os.fsync(descriptor)
    except Exception:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)
        raise
    return descriptor


def release_lock(descriptor: int) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(descriptor)


def steps(contract: Mapping[str, Any], through: date, mode: str) -> list[Step]:
    entrypoints = contract["entrypoints"]
    data_mode = "--run" if mode == "run" else "--plan"
    index_mode = "--write" if mode == "run" else "--check"
    return [
        Step(
            "rqdata_shsz_price_volume",
            ROOT / str(entrypoints["rqdata_shsz_price_volume"]),
            (data_mode, "--through", through.isoformat()),
        ),
        Step(
            "rqdata_b_path",
            ROOT / str(entrypoints["rqdata_b_path"]),
            (data_mode, "--through", through.isoformat()),
        ),
        Step(
            "rqdata_financial_pit",
            ROOT / str(entrypoints["rqdata_financial_pit"]),
            (data_mode, "--discover-through", through.isoformat()),
        ),
        Step(
            "joinquant_share_capital_pit",
            ROOT / str(entrypoints["joinquant_share_capital_pit"]),
            (data_mode,),
        ),
        Step(
            "candidate_asset_index",
            ROOT / str(entrypoints["candidate_asset_index"]),
            (index_mode,),
        ),
    ]


def execute_step(step: Step, *, python: Path | str = sys.executable) -> dict[str, Any]:
    if not step.script.is_file():
        return {
            "name": step.name,
            "status": "FAIL_CLOSED__ENTRYPOINT_MISSING",
            "returncode": 127,
            "entrypoint": step.script.resolve().relative_to(ROOT.resolve()).as_posix(),
        }
    command = [str(python), "-X", "utf8", str(step.script), *step.mode_args]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    result: dict[str, Any] = {
        "name": step.name,
        "returncode": completed.returncode,
        "entrypoint": step.script.resolve().relative_to(ROOT.resolve()).as_posix(),
    }
    try:
        payload = parse_json_output(completed.stdout)
    except Exception as exc:
        result.update({
            "status": "FAIL_CLOSED__INVALID_CHILD_OUTPUT",
            "error": {"type": type(exc).__name__, "message": safe_text(str(exc))},
        })
        if completed.stderr.strip():
            result["stderr_tail_redacted"] = safe_text(completed.stderr)
        return result
    result["status"] = str(payload.get("status", "UNKNOWN"))
    result["result"] = payload
    if completed.returncode != 0 and completed.stderr.strip():
        result["stderr_tail_redacted"] = safe_text(completed.stderr)
    return result


def plan(through: date) -> dict[str, Any]:
    contract = load_contract()
    planned_steps = steps(contract, through, "plan")
    return {
        "schema_version": 1,
        "producer": PRODUCER_ID,
        "status": "PLAN__NO_NETWORK_NO_WRITE",
        "through": through.isoformat(),
        "step_order": [item.name for item in planned_steps],
        "commands": [
            {
                "name": item.name,
                "entrypoint": item.script.resolve().relative_to(ROOT.resolve()).as_posix(),
                "arguments": list(item.mode_args),
            }
            for item in planned_steps
        ],
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_catalog_write": False,
        "formal_snapshot_write": False,
        "quality_promotion": False,
        "factor_runs": 0,
        "credential_value_read": False,
        "credential_value_captured": False,
    }


def run(through: date, *, python: Path | str = sys.executable) -> dict[str, Any]:
    contract = load_contract()
    descriptor = acquire_lock()
    started = datetime.now(timezone.utc)
    try:
        catalog_before = sha256_file(FORMAL_CATALOG_PATH)
        results = [execute_step(step, python=python) for step in steps(contract, through, "run")]
        catalog_after = sha256_file(FORMAL_CATALOG_PATH)
        formal_catalog_unchanged = catalog_before == catalog_after
        successful = all(item["returncode"] == 0 for item in results)
        index_written = CANDIDATE_INDEX_PATH.is_file()
        overall_pass = successful and formal_catalog_unchanged and index_written
        finished = datetime.now(timezone.utc)
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "producer": PRODUCER_ID,
            "status": "PASS__CANDIDATE_DAILY_MAINTENANCE" if overall_pass else "FAIL_CLOSED__CANDIDATE_DAILY_MAINTENANCE",
            "through": through.isoformat(),
            "started_at_utc": started.isoformat(),
            "finished_at_utc": finished.isoformat(),
            "steps": results,
            "formal_catalog_sha256_before": catalog_before,
            "formal_catalog_sha256_after": catalog_after,
            "formal_catalog_unchanged": formal_catalog_unchanged,
            "candidate_index_present": index_written,
            "candidate_index_sha256": sha256_file(CANDIDATE_INDEX_PATH) if index_written else None,
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_effect": "NONE__CANDIDATE_ONLY",
            "formal_snapshot_write": False,
            "quality_promotion": False,
            "factor_runs": 0,
            "credential_value_read": False,
            "credential_value_captured": False,
        }
        receipt_name = f"candidate-maintenance-{finished.astimezone(SHANGHAI):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}-v1.json"
        receipt_path = TRANSACTION_PARENT / receipt_name
        atomic_write(receipt_path, canonical_bytes(receipt))
        receipt["receipt"] = receipt_path.resolve().relative_to(ROOT.resolve()).as_posix()
        return receipt
    finally:
        release_lock(descriptor)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--through", type=date.fromisoformat)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    through = args.through or default_through()
    try:
        result = plan(through) if args.plan else run(through)
    except Exception as exc:
        print(json.dumps({
            "status": "FAIL_CLOSED",
            "error": {"type": type(exc).__name__, "message": safe_text(str(exc))},
        }, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not str(result.get("status", "")).startswith("FAIL") else 2


if __name__ == "__main__":
    raise SystemExit(main())
