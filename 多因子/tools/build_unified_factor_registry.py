"""Build the source-qualified factor metadata registry; never fetch values."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_factory.factor_dependencies import (  # noqa: E402
    classify_joinquant_definition,
    classify_local_expression,
    classify_ricequant_type,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _definition_hash(definition: str | None) -> str | None:
    if definition is None or not definition.strip():
        return None
    normalized = " ".join(definition.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _rq_availability(factor_type: str, metadata: dict[str, Any]) -> str:
    probe = metadata["value_probes"].get(factor_type, {})
    if probe.get("status") == "OK" and int(probe.get("non_null_count", 0)) > 0:
        return "CATEGORY_BOUNDED_VALUE_SAMPLE_NON_NULL__NOT_FACTOR_WIDE_PROOF"
    if probe.get("status") in {"OK", "OK_EMPTY"}:
        return "CATEGORY_ENDPOINT_RESPONDED__SAMPLE_EMPTY_OR_NULL"
    if probe.get("status") == "ERROR":
        return f"CATEGORY_SAMPLE_ERROR__{probe.get('error_type', 'UNKNOWN')}"
    return "METADATA_ONLY__VALUE_NOT_PROBED"


def build(rq_metadata_path: Path, output_path: Path, summary_path: Path) -> dict[str, Any]:
    local_path = WORKSPACE_ROOT / "多因子/configs/r0_shsz_pv_factor_catalog_512_v1.json"
    jq_path = WORKSPACE_ROOT / (
        "reports/data-governance/factor-source-inventory-20260803-v1/"
        "joinquant_official_factor_definitions_20260803.csv"
    )
    local = json.loads(local_path.read_text(encoding="utf-8"))
    rq_metadata = json.loads(rq_metadata_path.read_text(encoding="utf-8"))
    with jq_path.open("r", encoding="utf-8-sig", newline="") as handle:
        jq_rows = list(csv.DictReader(handle))

    records: list[dict[str, Any]] = []
    for candidate in local["candidates"]:
        assessment = classify_local_expression(candidate["formula"])
        records.append({
            "factor_id": f"local:r0:{candidate['candidate_id']}",
            "source": "local",
            "source_key": candidate["candidate_id"],
            "namespace": "r0_shsz_pv_factor_catalog_512_v1",
            "display_name": candidate["candidate_id"],
            "definition": candidate["formula"],
            "definition_sha256": _definition_hash(candidate["formula"]),
            **assessment.to_dict(),
            "availability_status": "LOCAL_DEFINITION_PRESENT",
            "value_status": "NOT_COMPUTED",
            "admission_status": "ENGINEERING_READY__NOT_RESEARCH_EVALUATED",
            "origin": candidate["origin"],
            "family": candidate["family"],
            "generation": candidate["generation"],
            "causal_contract": candidate["causal_contract"],
        })

    for row in jq_rows:
        definition = row["formula_or_definition"].strip() or None
        assessment = classify_joinquant_definition(
            row["namespace"], definition or "", row["implemented_status"]
        )
        records.append({
            "factor_id": f"joinquant:{row['factor_key']}",
            "source": "joinquant",
            "source_key": row["factor_key"],
            "namespace": row["namespace"],
            "display_name": row["factor_name"],
            "definition": definition,
            "definition_sha256": _definition_hash(definition),
            **assessment.to_dict(),
            "availability_status": row["implemented_status"],
            "value_status": "NOT_IMPORTED__LOCAL_RECOMPUTE_OR_BOUNDED_VALIDATION_REQUIRED",
            "admission_status": "SOURCE_DEFINITION_INSPECTION_ONLY__NOT_FACTOR_VALUE_DATASET",
            "signature": row["signature"],
            "category": row["category"],
            "source_url": row["source_url"],
            "captured_date": row["captured_date"],
        })

    for factor_type, type_metadata in sorted(rq_metadata["factor_types"].items()):
        assessment = classify_ricequant_type(factor_type)
        for name in type_metadata["names"]:
            source_digest = hashlib.sha256(f"{factor_type}:{name}".encode("utf-8")).hexdigest()[:20]
            records.append({
                "factor_id": f"ricequant:{factor_type}:{source_digest}",
                "source": "ricequant",
                "source_key": name,
                "namespace": factor_type,
                "display_name": name,
                "definition": None,
                "definition_sha256": None,
                **assessment.to_dict(),
                "availability_status": _rq_availability(factor_type, rq_metadata),
                "value_status": "NOT_IMPORTED__CATEGORY_SAMPLE_ONLY",
                "admission_status": "INSPECTION_METADATA_ONLY__PIT_RIGHTS_AND_COVERAGE_UNVERIFIED",
                "catalog_observed_on": "2026-08-05",
            })

    records.sort(key=lambda value: value["factor_id"])
    factor_ids = [record["factor_id"] for record in records]
    if len(factor_ids) != len(set(factor_ids)):
        raise ValueError("duplicate factor_id in unified registry")

    source_counts = Counter(record["source"] for record in records)
    dependency_counts = Counter(record["dependency_level"] for record in records)
    compute_counts = Counter(record["local_compute_status"] for record in records)
    definition_hashes = [record["definition_sha256"] for record in records if record["definition_sha256"]]
    definition_groups = Counter(definition_hashes)
    registry: dict[str, Any] = {
        "schema_version": 1,
        "registry_id": "unified-factor-registry-20260805-v1",
        "status": "METADATA_REGISTERED__NO_FACTOR_VALUES__NO_RESEARCH_RUN",
        "protocol_id": "full-market-layered-factor-research-c-v1.1-20260805",
        "source_artifacts": [
            {"path": str(local_path.relative_to(WORKSPACE_ROOT)).replace("\\", "/"), "sha256": _sha256_file(local_path)},
            {"path": str(jq_path.relative_to(WORKSPACE_ROOT)).replace("\\", "/"), "sha256": _sha256_file(jq_path)},
            {"path": str(rq_metadata_path.relative_to(WORKSPACE_ROOT)).replace("\\", "/"), "sha256": _sha256_file(rq_metadata_path)},
        ],
        "record_count": len(records),
        "summary": {
            "source_counts": dict(sorted(source_counts.items())),
            "dependency_level_counts": dict(sorted(dependency_counts.items())),
            "local_compute_status_counts": dict(sorted(compute_counts.items())),
            "records_with_definition": len(definition_hashes),
            "duplicate_definition_group_count": sum(count > 1 for count in definition_groups.values()),
            "factor_value_rows": 0,
            "ic_runs": 0,
            "backtest_runs": 0,
            "validation_consumed": False,
            "holdout_opened": False,
        },
        "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "registry_id": registry["registry_id"],
        "status": registry["status"],
        "record_count": registry["record_count"],
        **registry["summary"],
        "registry_path": str(output_path.relative_to(WORKSPACE_ROOT)).replace("\\", "/"),
        "registry_sha256": _sha256_file(output_path),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rq-metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=WORKSPACE_ROOT / "data/catalog/factors.json")
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    summary = build(args.rq_metadata.resolve(), args.output.resolve(), args.summary.resolve())
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
