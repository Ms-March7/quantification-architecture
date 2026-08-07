#!/usr/bin/env python3
"""Build the deterministic R0 classic-factor specification catalog only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid


PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from factor_factory.r0_classic_specs import build_catalog, sha256_file  # noqa: E402


DEFAULT_OUTPUT = PROJECT / "configs/r0_shsz_pv_classic_factor_catalog_v1.json"
LEGACY_512 = PROJECT / "configs/r0_shsz_pv_factor_catalog_512_v1.json"
JQ_DEFINITIONS = WORKSPACE / (
    "reports/data-governance/factor-source-inventory-20260803-v1/"
    "joinquant_official_factor_definitions_20260803.csv"
)
RQ_METADATA = WORKSPACE / (
    "reports/data-governance/factor-ab-capability-20260805-v1/"
    "RQ_FACTOR_METADATA.json"
)


def _payload(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def atomic_publish(path: Path, value: object, *, replace: bool = False) -> str:
    """Publish once; an identical rerun is a no-op and drift fails closed."""

    data = _payload(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == data:
            return "UNCHANGED_IDENTICAL"
        if not replace:
            raise FileExistsError(
                f"refusing to overwrite differing catalog without --replace: {path}"
            )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "PUBLISHED" if not replace else "REPLACED_EXPLICITLY"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--legacy-512", type=Path, default=LEGACY_512)
    parser.add_argument("--joinquant-definitions", type=Path, default=JQ_DEFINITIONS)
    parser.add_argument("--ricequant-metadata", type=Path, default=RQ_METADATA)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="explicitly replace only this new catalog path when deterministic content changed",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output == LEGACY_512.resolve():
        raise ValueError("the frozen legacy 512 catalog cannot be used as output")
    catalog = build_catalog(
        legacy_catalog_path=args.legacy_512.resolve(),
        joinquant_definitions_path=args.joinquant_definitions.resolve(),
        ricequant_metadata_path=args.ricequant_metadata.resolve(),
    )
    publication = atomic_publish(output, catalog, replace=args.replace)
    summary = {
        "publication": publication,
        "status": catalog["status"],
        "catalog_id": catalog["catalog_id"],
        "record_count": catalog["record_count"],
        "family_counts": catalog["family_counts"],
        "kind_count": len(catalog["kind_counts"]),
        "supplier_formula_summary": catalog["supplier_formula_summary"],
        "legacy_512_comparison": catalog["legacy_512_comparison"],
        "catalog_content_sha256": catalog["catalog_content_sha256"],
        "file_sha256": sha256_file(output),
        "factor_runs": catalog["factor_runs"],
        "validation_consumed": catalog["validation_consumed"],
        "holdout_opened": catalog["holdout_opened"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
