#!/usr/bin/env python3
"""Verify the published R0_SHSZ_PV v2 snapshot and research input gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CATALOG_LIB = ROOT / "data/catalog"
if str(CATALOG_LIB) not in sys.path:
    sys.path.insert(0, str(CATALOG_LIB))

import dataset_cli  # noqa: E402
from snapshot_contract import verify_snapshot_contract  # noqa: E402


CONTRACT = ROOT / "data/catalog/contracts/r0_shsz_pv_v2/contract.json"
CATALOG = ROOT / "data/catalog/datasets.json"
DATASET_ID = "cn-a-share-r0-shsz-pv-panel"
SNAPSHOT_ID = "20050104-20260803-v1"
JOURNAL = ROOT / "data/market/transactions/formal-workspace-execution-r0-shsz-pv-publication-20260804-v3.json"
LEDGER = ROOT / "data/market/authorization-ledger/auth-r0-shsz-pv-publication-20260804-v3.json"
SMOKE = ROOT / "reports/data-governance/r0-shsz-pv-paneldata-formal-smoke-20260804-v1/RESULT.json"


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON object required: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def verify() -> dict[str, Any]:
    contract = load(CONTRACT)
    catalog = dataset_cli.load_catalog(CATALOG)
    item = dataset_cli.find_dataset(catalog, DATASET_ID, SNAPSHOT_ID)
    dataset_cli.check_access(catalog, item, "research", "multi-factor")
    snapshot = Path(str(item["physical_path"]).replace("/", os.sep))
    contract_errors = verify_snapshot_contract(
        catalog=catalog, item=item, snapshot_path=snapshot, deep_payload=True
    )
    manifest = load(snapshot / "manifest.json")
    quality = load(snapshot / "quality/summary.json")
    journal = load(JOURNAL)
    ledger = load(LEDGER)
    smoke = load(SMOKE)
    checks = {
        "contract_identity": contract.get("contract_id") == "r0_shsz_pv_v2",
        "narrow_gate_only": contract.get("independence", {}).get("canonical_gate_satisfied_by_this_contract") is False,
        "catalog_identity_exact": item.get("dataset_id") == DATASET_ID and item.get("snapshot_id") == SNAPSHOT_ID,
        "catalog_research_access": item.get("purpose_ceiling") == "RESEARCH_ONLY" and item.get("quality_status") == "OK",
        "catalog_immutable_frozen": item.get("mutability") == "immutable" and item.get("lifecycle") == "frozen",
        "five_authorities_and_payload_hashes": not contract_errors,
        "research_contract_exact": manifest.get("research_input_contract") == contract.get("research_input_contract"),
        "quality_gate_pass": quality.get("overall") == "PASS" and quality.get("catalog_quality_status") == "OK",
        "atomic_publication": journal.get("state") == "PUBLISHED",
        "single_use_authorization_consumed": ledger.get("state") == "CONSUMED",
        "qdata_content_hash_bound": item.get("content_hash") == manifest.get("content_hash"),
        "paneldata_formal_smoke": smoke.get("verdict") == "PASS__BOUNDED_PANELDATA_SMOKE_ONLY",
        "paneldata_identity_bound": smoke.get("identity") == f"{DATASET_ID}@{SNAPSHOT_ID}",
        "paneldata_content_hash_bound": smoke.get("content_hash") == item.get("content_hash"),
        "same_day_leakage_forbidden": smoke.get("signal_lag_sessions_minimum") == 1,
        "no_factor_or_validation_use": all(
            smoke.get(key) in {0, False}
            for key in ("factor_runs", "ic_runs", "backtest_runs", "validation_consumed", "holdout_opened")
        ),
    }
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "receipt_id": "r0-shsz-pv-v2-formal-acceptance-20260804-v1",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS__R0_SHSZ_PV_V2_RESEARCH_INPUT_OPEN" if passed else "FAIL_CLOSED",
        "gate_id": "R0_SHSZ_PV",
        "canonical_r0_pv_effect": "NONE",
        "canonical_r1_effect": "NONE",
        "dataset_id": DATASET_ID,
        "snapshot_id": SNAPSHOT_ID,
        "content_hash": item.get("content_hash"),
        "checks": checks,
        "snapshot_contract_errors": contract_errors,
        "scope": contract.get("scope"),
        "allowed_research": "SHSZ_ORDINARY_A_PRICE_VOLUME_SINGLE_FACTOR_DISCOVERY_AND_PREREGISTERED_VALIDATION_ONLY",
        "forbidden_research": ["FUNDAMENTAL", "INDUSTRY_NEUTRAL", "ETF_LOOKTHROUGH", "GLOBAL_CONTEXT", "AUTOMATIC_TRADING"],
        "factor_runs": 0,
        "validation_consumed": False,
        "holdout_opened": False,
        "evidence_sha256": {
            "contract": sha256_file(CONTRACT), "catalog": sha256_file(CATALOG),
            "journal": sha256_file(JOURNAL), "ledger": sha256_file(LEDGER),
            "paneldata_smoke": sha256_file(SMOKE),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        result = verify()
    except Exception as exc:
        result = {"schema_version": 1, "status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"}
    if args.receipt:
        atomic_write(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if str(result.get("status", "")).startswith("PASS__") else 2


if __name__ == "__main__":
    raise SystemExit(main())
