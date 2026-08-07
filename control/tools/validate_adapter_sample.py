#!/usr/bin/env python3
"""Validate a bounded adapter sample without publishing or changing catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


CATALOG_LIB = Path(__file__).resolve().parents[2] / "data" / "catalog"
if str(CATALOG_LIB) not in sys.path:
    sys.path.insert(0, str(CATALOG_LIB))

from adapter_gate import AdapterGateError, validate_adapter_sample  # noqa: E402
from snapshot_contract import SnapshotContractError, load_json_object  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--sample", type=Path, required=True)
    args = parser.parse_args()
    try:
        contract = load_json_object(args.contract)
        sample = load_json_object(args.sample)
        result = validate_adapter_sample(contract, sample)
    except (AdapterGateError, SnapshotContractError, OSError) as exc:
        result = {
            "schema_version": 1,
            "status": "FAIL",
            "purpose_ceiling": "SMOKE_ONLY",
            "source_admission": False,
            "errors": [str(exc)],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
