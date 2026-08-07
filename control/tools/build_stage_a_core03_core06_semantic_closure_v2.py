#!/usr/bin/env python3
"""Build a fail-closed CORE-03/CORE-06 semantic evidence package.

The package is intentionally admission-neutral.  It binds already-preserved
CSMAR manuals/ZIPs, a full local TRD_Dalyr mechanical audit, and preserved
official exchange-rule evidence.  It does not fetch network resources and does
not mutate catalog, registry, Roadmap, projects, snapshots, or published data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


RUN_ID = "stage-a-core03-core06-semantic-closure-20260730-v2"
PURPOSE_CEILING = "INSPECTION_ONLY"
QUALITY_STATUS = "NOT_ADMITTED"
REPAIR_ID = "strict-evidence-chain-repair-20260730-v1"
DEFAULT_CANDIDATE = (
    "tmp/d2a04b-source-closure/"
    "stage-a-core03-core06-semantic-closure-20260730-v2"
)
DEFAULT_REPORT = (
    "reports/data-governance/"
    "stage-a-core03-core06-semantic-closure-20260730-v2"
)
BUILD_TOOL_PATH = (
    "control/tools/build_stage_a_core03_core06_semantic_closure_v2.py"
)
TEST_SOURCE_PATH = (
    "control/tests/test_build_stage_a_core03_core06_semantic_closure_v2.py"
)
CANDIDATE_PAYLOAD_NAMES = {
    "ANOMALY_LEDGER.json",
    "FIELD_SEMANTICS_MATRIX.json",
    "GATE_IMPACT.json",
    "RULE_VERSION_MATRIX.json",
    "SOURCE_BINDINGS.json",
}
REPORT_PAYLOAD_NAMES = {
    "README.md",
    "TEST_RESULTS.json",
    "VERIFICATION.json",
}

GUARDED_FILES = (
    "data/catalog/datasets.json",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    "control/ROADMAP.md",
    "control/projects.yaml",
    "control/WORKSPACE_INDEX.md",
)
GUARDED_TREES = ("data/market/snapshots", "data/published")

INPUT_FILES = {
    "CSMAR_STANDARD_TRD_MANUAL": (
        "data/market/quarantine/csmar-manual-export/"
        "intake-20260730-08pkg-03manual-v1/raw/documentation/"
        "CSMAR_TRD_中国股票市场交易数据库_数据库说明书_20250616.pdf"
    ),
    "TABLE_SEMANTICS_EVIDENCE_MATRIX": (
        "reports/data-governance/"
        "csmar-trd-dalyr-table-semantics-gap-20260730-v1/"
        "EVIDENCE_MATRIX.json"
    ),
    "TABLE_SEMANTICS_MEASUREMENTS": (
        "reports/data-governance/"
        "csmar-trd-dalyr-table-semantics-gap-20260730-v1/"
        "MEASUREMENTS.json"
    ),
    "TABLE_SEMANTICS_VERIFICATION": (
        "reports/data-governance/"
        "csmar-trd-dalyr-table-semantics-gap-20260730-v1/"
        "VERIFICATION.json"
    ),
    "NULL_ZERO_SEMANTICS_MATRIX": (
        "reports/data-governance/"
        "trd-dalyr-null-zero-semantics-audit-20260730-v2/"
        "SEMANTICS_MATRIX.json"
    ),
    "NULL_ZERO_KEY_CONFLICTS": (
        "reports/data-governance/"
        "trd-dalyr-null-zero-semantics-audit-20260730-v2/"
        "KEY_CONFLICTS.json"
    ),
    "NULL_ZERO_VERIFICATION": (
        "reports/data-governance/"
        "trd-dalyr-null-zero-semantics-audit-20260730-v2/"
        "VERIFICATION.json"
    ),
    "OFFICIAL_RULE_COVERAGE": (
        "reports/data-governance/"
        "stage-a-core03-core06-official-rules-closure-20260730-v1/"
        "OFFICIAL_RULE_COVERAGE.json"
    ),
    "OFFICIAL_EFFECTIVE_DATE_MATRIX": (
        "reports/data-governance/"
        "stage-a-core03-core06-official-rules-closure-20260730-v1/"
        "EFFECTIVE_DATE_MATRIX.json"
    ),
    "OFFICIAL_HISTORICAL_GAPS": (
        "reports/data-governance/"
        "stage-a-core03-core06-official-rules-closure-20260730-v1/"
        "HISTORICAL_GAPS.json"
    ),
    "OFFICIAL_SOURCE_BINDINGS": (
        "reports/data-governance/"
        "stage-a-core03-core06-official-rules-closure-20260730-v1/"
        "SOURCE_BINDINGS.json"
    ),
    "OFFICIAL_VERIFICATION": (
        "reports/data-governance/"
        "stage-a-core03-core06-official-rules-closure-20260730-v1/"
        "VERIFICATION.json"
    ),
    "STAGE_A_QUEUE_DELTA": (
        "reports/data-governance/"
        "stage-a-evidence-consolidation-20260730-v2/"
        "QUEUE_DELTA.json"
    ),
}

OFFICIAL_TEXT_MARKERS = (
    {
        "source_id": "SSE-TRADE-RULE-2006-REPEALED",
        "venue": "SSE",
        "path": (
            "tmp/d2a04b-source-closure/"
            "stage-a-core03-core06-official-rules-closure-20260730-v1/"
            "extracted/SSE-TRADE-RULE-2006-REPEALED.txt"
        ),
        "markers": (
            "证券停牌时，本所发布的行情中包括该证券的信息",
            "证券摘牌后，行情中无该证券的信息",
        ),
    },
    {
        "source_id": "SSE-TRADE-RULE-2023-DOC",
        "venue": "SSE",
        "path": (
            "tmp/d2a04b-source-closure/"
            "stage-a-core03-core06-official-rules-closure-20260730-v1/"
            "extracted/SSE-TRADE-RULE-2023-DOC.txt"
        ),
        "markers": (
            "证券停牌时，本所发布的行情中包括该证券的信息",
            "技术性停牌、临时停市",
            "可以决定恢复交易",
        ),
    },
    {
        "source_id": "SSE-TRADE-RULE-2026-DOC",
        "venue": "SSE",
        "path": (
            "tmp/d2a04b-source-closure/"
            "stage-a-core03-core06-official-rules-closure-20260730-v1/"
            "extracted/SSE-TRADE-RULE-2026-DOC.txt"
        ),
        "markers": (
            "证券停牌时，本所发布的行情中包括该证券的信息",
            "技术性停牌、临时停市",
            "可以决定恢复交易",
        ),
    },
    {
        "source_id": "SZSE-TRADE-RULE-2026-DOC",
        "venue": "SZSE",
        "path": (
            "tmp/d2a04b-source-closure/"
            "stage-a-core03-core06-official-rules-closure-20260730-v1/"
            "extracted/SZSE-TRADE-RULE-2026-DOC.txt"
        ),
        "markers": (
            "证券停牌时，本所发布的行情中包括该证券的",
            "技术性停牌、临时停市",
            "可以决定恢复",
        ),
    },
    {
        "source_id": "BSE-TRADE-RULE-2021",
        "venue": "BSE",
        "path": (
            "tmp/d2a04b-source-closure/"
            "stage-a-core03-core06-official-rules-closure-20260730-v1/"
            "extracted/BSE-TRADE-RULE-2021.txt"
        ),
        "markers": (
            "证券停牌时，本所发布的行情中包括该证券的信息",
            "证券摘牌后，行情中无该证券的信息",
        ),
    },
    {
        "source_id": "BSE-TRADE-RULE-2026",
        "venue": "BSE",
        "path": (
            "tmp/d2a04b-source-closure/"
            "stage-a-core03-core06-official-rules-closure-20260730-v1/"
            "extracted/BSE-TRADE-RULE-2026.txt"
        ),
        "markers": (
            "证券停牌时，本所发布的行情中包括该证券的信息",
            "技术性停牌、临时停市",
            "可以决定恢复交易",
        ),
    },
)

EXPECTED_ONE_SIDED_ROWS = (
    {
        "Stkcd": "900912",
        "Trddt": "1993-07-26",
        "Trdsta": 1,
        "Dnshrtrd": 2164000,
        "Dnvaltrd": 0.0,
        "Ahshrtrd_D": None,
        "Ahvaltrd_D": None,
    },
    {
        "Stkcd": "000009",
        "Trddt": "1994-04-29",
        "Trdsta": 1,
        "Dnshrtrd": 0,
        "Dnvaltrd": 59631000.0,
        "Ahshrtrd_D": None,
        "Ahvaltrd_D": None,
    },
    {
        "Stkcd": "200011",
        "Trddt": "1994-10-26",
        "Trdsta": 1,
        "Dnshrtrd": 2000,
        "Dnvaltrd": 0.0,
        "Ahshrtrd_D": None,
        "Ahvaltrd_D": None,
    },
    {
        "Stkcd": "900924",
        "Trddt": "1995-01-24",
        "Trdsta": 1,
        "Dnshrtrd": 252000,
        "Dnvaltrd": 0.0,
        "Ahshrtrd_D": None,
        "Ahvaltrd_D": None,
    },
    {
        "Stkcd": "900901",
        "Trddt": "1995-02-14",
        "Trdsta": 1,
        "Dnshrtrd": 50000,
        "Dnvaltrd": 0.0,
        "Ahshrtrd_D": None,
        "Ahvaltrd_D": None,
    },
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(data, encoding="utf-8")
    os.replace(temporary, path)


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def file_receipt(root: Path, relative_path: str) -> dict[str, Any]:
    path = root / relative_path
    if not path.is_file():
        raise FileNotFoundError(relative_path)
    return {
        "path": relative_path.replace("\\", "/"),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def tree_receipt(root: Path, relative_path: str) -> dict[str, Any]:
    base = root / relative_path
    if not base.exists():
        return {
            "path": relative_path,
            "exists": False,
            "files": [],
            "file_count": 0,
            "total_bytes": 0,
            "tree_sha256": canonical_sha256([]),
        }
    entries: list[dict[str, Any]] = []
    for current, directory_names, file_names in os.walk(base, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in sorted(directory_names):
            directory = current_path / name
            attrs = getattr(directory.stat(follow_symlinks=False), "st_file_attributes", 0)
            if directory.is_symlink() or attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise RuntimeError(f"Guarded tree contains link/reparse point: {directory}")
            safe_directories.append(name)
        directory_names[:] = safe_directories
        for name in sorted(file_names):
            path = current_path / name
            attrs = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
            if path.is_symlink() or attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise RuntimeError(f"Guarded tree contains link/reparse point: {path}")
            item = file_receipt(root, rel(root, path))
            entries.append(item)
    entries.sort(key=lambda item: item["path"])
    return {
        "path": relative_path,
        "exists": True,
        "files": entries,
        "file_count": len(entries),
        "total_bytes": sum(item["bytes"] for item in entries),
        "tree_sha256": canonical_sha256(entries),
    }


def formal_guard(root: Path) -> dict[str, Any]:
    return {
        "files": {
            path: file_receipt(root, path)
            for path in GUARDED_FILES
        },
        "trees": {
            path: tree_receipt(root, path)
            for path in GUARDED_TREES
        },
    }


def load_inputs(root: Path) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for source_id, path in INPUT_FILES.items():
        receipt = file_receipt(root, path)
        inputs[source_id] = {
            "receipt": receipt,
            "json": load_json(root / path) if path.endswith(".json") else None,
        }
    return inputs


def verify_raw_zips(root: Path, measurements: dict[str, Any]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for candidate_id, candidate in sorted(
        measurements["candidate_integrity"].items()
    ):
        raw_schemas = candidate.get("raw_schema", [])
        if len(raw_schemas) != 1:
            raise RuntimeError(
                f"{candidate_id}: expected exactly one raw_schema row"
            )
        expected = raw_schemas[0]["raw_zip"]
        receipt = file_receipt(root, expected["path"])
        if receipt["bytes"] != expected["bytes"]:
            raise RuntimeError(f"{candidate_id}: raw ZIP byte mismatch")
        if receipt["sha256"] != expected["sha256"]:
            raise RuntimeError(f"{candidate_id}: raw ZIP SHA-256 mismatch")
        receipts.append(
            {
                "candidate_id": candidate_id,
                **receipt,
                "upstream_measurement_verified": True,
            }
        )
    if len(receipts) != 8:
        raise RuntimeError(f"Expected 8 sealed TRD ZIPs, got {len(receipts)}")
    return receipts


def field_semantics_matrix() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "table": {
            "physical_name": "TRD_Dalyr",
            "chinese_name": "日个股回报率文件",
            "manual_version": "2025-06-16",
            "manual_physical_pages": [24, 25, 26],
            "scope": "STANDARD_TRD_DALYR_ONLY",
        },
        "proved_for_hash_bound_manual_and_exports": [
            {
                "field": "Trdsta",
                "classification": "DOCUMENTED_ENUMERATION",
                "values": {
                    "1": "正常交易",
                    "2": "ST",
                    "3": "*ST",
                    "4": "S",
                    "5": "SST",
                    "6": "S*ST",
                    "7": "G",
                    "8": "GST",
                    "9": "G*ST",
                    "10": "U",
                    "11": "UST",
                    "12": "U*ST",
                    "13": "N",
                    "14": "NST",
                    "15": "N*ST",
                    "16": "PT",
                },
                "boundary": (
                    "This is a trading/risk-label enumeration; it is not a "
                    "suspension, holiday, missing-row, or unknown-value code."
                ),
            },
            {
                "field": "Dnshrtrd",
                "classification": "VENDOR_LITERAL",
                "literal": "0=没有交易量",
                "inference_ceiling": (
                    "The literal does not prove that every historical zero is "
                    "free of error, rounding, or unknown-value contamination."
                ),
            },
            {
                "field": "Dnvaltrd",
                "classification": (
                    "VENDOR_LITERAL_PRESERVED__POSSIBLE_LABEL_COPY_ERROR"
                ),
                "literal": (
                    "A股以人民币元计，上海B以美元计，深圳B以港币计，"
                    "0=没有交易量"
                ),
                "automatic_correction": False,
                "inference_ceiling": (
                    "The manual literally says no trading volume for the "
                    "amount field. This package does not silently rewrite it "
                    "as no trading amount."
                ),
            },
            {
                "fields": ["LimitDown", "LimitUp"],
                "classification": "DOCUMENTED_UNIT",
                "literal_unit": "元/股",
            },
            {
                "field": "LimitStatus",
                "classification": "DOCUMENTED_CLOSE_STATE_AND_FORMULA",
                "values": {"-1": "跌停", "0": "未涨跌停", "1": "涨停"},
                "boundary": (
                    "The vendor description is a close-state calculation. It "
                    "does not establish a complete historical rule/version "
                    "chain or prove every exceptional no-limit case."
                ),
            },
        ],
        "not_proved": [
            "TRD_Dalyr row emission policy on full-day suspension",
            "TRD_Dalyr encoding of intraday suspension or no trade",
            "TRD_Dalyr NULL/sentinel contract for every table version",
            "that absent rows, zero volume, and unknown values are distinguishable",
            "that other-table Filling or StateCode semantics apply to TRD_Dalyr",
        ],
        "non_transfer_rule": {
            "classification": "FAIL_CLOSED",
            "statement": (
                "TRD_BwardQuotation/TRD_FwardQuotation StateCode and "
                "TRDNEW_Dalyr Filling are table-specific and are not "
                "transferred to standard TRD_Dalyr."
            ),
        },
    }


def anomaly_ledger(semantics: dict[str, Any]) -> dict[str, Any]:
    checks = semantics["global_consistency_checks"]
    observed = checks["volume_amount_inconsistency_examples"]
    expected = list(EXPECTED_ONE_SIDED_ROWS)
    if observed != expected:
        raise RuntimeError("One-sided zero anomaly rows differ from expected evidence")
    if checks["volume_zero_amount_nonzero_rows"] != 1:
        raise RuntimeError("Expected one volume-zero/amount-nonzero row")
    if checks["volume_nonzero_amount_zero_rows"] != 4:
        raise RuntimeError("Expected four volume-nonzero/amount-zero rows")
    row_hash = canonical_sha256(observed)
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "anomalies": [
            {
                "anomaly_id": "DNVALTRD-MANUAL-LITERAL-CONFLICT",
                "severity": "BLOCKING_SEMANTIC_AMBIGUITY",
                "classification": (
                    "TEXT_LITERAL_PRESERVED__POSSIBLE_LABEL_COPY_ERROR_NOT_RESOLVED"
                ),
                "manual_literal": (
                    "Dnvaltrd [日个股交易金额] - A股以人民币元计，"
                    "上海B以美元计，深圳B以港币计，0=没有交易量"
                ),
                "prior_audit_paraphrase": (
                    semantics["manual_claims"]["Dnvaltrd_zero"]
                ),
                "automatic_correction": False,
                "resolution": "UNKNOWN__VENDOR_TABLE_VERSION_CLARIFICATION_REQUIRED",
            },
            {
                "anomaly_id": "TRD-DALYR-ONE-SIDED-VOLUME-AMOUNT-ZEROS",
                "severity": "BLOCKING_COUNTEREXAMPLE",
                "classification": "ISOLATED_EXACT_ROWS__NO_IMPUTATION",
                "row_count": len(observed),
                "canonicalization": (
                    "UTF-8 JSON; ensure_ascii=false; sort_keys=true; "
                    "separators=(',',':'); source order preserved"
                ),
                "rows_sha256": row_hash,
                "rows": observed,
                "resolution": (
                    "UNKNOWN__DO_NOT_RECODE_OR_IMPUTE_WITHOUT "
                    "VERSIONED_VENDOR_OR_PRIMARY_EVIDENCE"
                ),
            },
        ],
        "mechanical_scope": semantics["scope"],
        "mechanical_findings": semantics["mechanical_findings"],
        "gate_effect": {
            "CORE03-Q2": "UNKNOWN",
            "CORE03-Q3": "UNKNOWN",
            "formal_admission": False,
            "quality_promotion": False,
        },
    }


def marker_evidence(root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for source in OFFICIAL_TEXT_MARKERS:
        path = root / source["path"]
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        matches: list[dict[str, Any]] = []
        for marker in source["markers"]:
            line_numbers = [
                index + 1
                for index, line in enumerate(lines)
                if marker in line
            ]
            if not line_numbers:
                raise RuntimeError(
                    f"{source['source_id']}: marker not found: {marker}"
                )
            matches.append(
                {"marker": marker, "line_numbers": line_numbers}
            )
        results.append(
            {
                "source_id": source["source_id"],
                "venue": source["venue"],
                **file_receipt(root, source["path"]),
                "marker_matches": matches,
                "claim_boundary": (
                    "Exchange-rule evidence proves a market mechanism only; "
                    "it does not prove CSMAR TRD_Dalyr row/value encoding."
                ),
            }
        )
    return results


def rule_version_matrix(
    coverage: dict[str, Any],
    effective_dates: dict[str, Any],
    gaps: dict[str, Any],
    marker_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "source_run_id": coverage["run_id"],
        "preserved_official_facts": coverage["facts"],
        "effective_date_rows": effective_dates["rows"],
        "suspension_market_close_context": {
            "classification": (
                "PARTIAL_OFFICIAL_MARKET_MECHANISM__NOT_VENDOR_ENCODING"
            ),
            "evidence": marker_rows,
            "conclusion": (
                "Preserved SSE/SZSE/BSE rules confirm suspension, temporary "
                "market closure, restoration, and—in selected versions—"
                "continued quote publication while suspended. They do not "
                "close standard TRD_Dalyr row-emission or NULL/zero semantics."
            ),
        },
        "historical_gaps": gaps["gaps"],
        "coverage_conclusion": {
            "complete_sse_version_chain": "FAIL",
            "complete_szse_version_chain": "FAIL",
            "complete_bse_version_chain": "UNKNOWN",
            "security_level_risk_state_intervals": "FAIL",
            "special_no_limit_exhaustiveness": "FAIL",
            "exact_historical_rounding_chain": "FAIL",
        },
    }


def gate_impact(one_sided_hash: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "quality_status": QUALITY_STATUS,
        "gates": {
            "CORE03-Q2": {
                "status": "UNKNOWN",
                "reason": (
                    "Table-specific suspension/no-trade/NULL/row-emission "
                    "contract is not closed."
                ),
            },
            "CORE03-Q3": {
                "status": "UNKNOWN",
                "reason": (
                    "Vendor literals plus local values cannot prove that "
                    "historical unknowns were never encoded as zero; five "
                    "one-sided volume/amount zero counterexamples remain."
                ),
                "one_sided_rows_sha256": one_sided_hash,
            },
            "CORE06-Q3": {
                "status": "FAIL",
                "reason": (
                    "Complete effective-dated venue/board/risk-state rule "
                    "version chains are not materialized."
                ),
            },
            "CORE06-Q4": {
                "status": "FAIL",
                "reason": (
                    "Historical price-limit, rounding, special no-limit, "
                    "delisting/relisting, and security-state chains remain "
                    "incomplete."
                ),
            },
        },
        "stage_a_ready": "0/4",
        "stage_a_status_changed": False,
        "formal_admission": False,
        "quality_promotion": False,
        "research_usable": False,
        "catalog_mutated": False,
        "source_registry_mutated": False,
        "roadmap_mutated": False,
        "projects_mutated": False,
        "snapshot_published": False,
    }


def expected_payload_paths(
    actual_root: Path,
    names: set[str],
    *,
    optional_names: set[str] | None = None,
) -> list[Path]:
    """Return an exact allow-listed payload, including preserved repair history."""

    optional_names = optional_names or set()
    expected: set[str] = set(names)
    expected.update(
        name for name in optional_names if (actual_root / name).is_file()
    )
    history = actual_root / "repair_history"
    if history.exists():
        expected.update(
            path.relative_to(actual_root).as_posix()
            for path in history.rglob("*")
            if path.is_file()
        )
    actual = {
        path.relative_to(actual_root).as_posix()
        for path in actual_root.rglob("*")
        if path.is_file()
        and path.relative_to(actual_root).as_posix()
        not in {"CANDIDATE_MANIFEST.json", "REPORT_MANIFEST.json"}
    }
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected or missing:
        raise RuntimeError(
            "Strict payload allow-list mismatch: "
            f"unexpected={unexpected}; missing={missing}"
        )
    return [actual_root / relative for relative in sorted(expected)]


def output_manifest(
    root: Path,
    run_id: str,
    logical_root: str,
    actual_root: Path,
    paths: Iterable[Path],
    manifest_type: str,
    *,
    manifest_name: str,
    external_bindings: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    receipts: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda item: item.as_posix()):
        relative = path.relative_to(actual_root).as_posix()
        receipt = file_receipt(root, rel(root, path))
        expected_logical = f"{logical_root.rstrip('/')}/{relative}"
        if receipt["path"] != expected_logical:
            raise RuntimeError(
                f"Logical path mismatch: {receipt['path']} != {expected_logical}"
            )
        receipts.append(receipt)
    return {
        "schema_version": 2,
        "run_id": run_id,
        "manifest_type": manifest_type,
        "logical_root": logical_root,
        "self_excluded_from_files": manifest_name,
        "files": receipts,
        "file_count": len(receipts),
        "total_bytes": sum(item["bytes"] for item in receipts),
        "payload_tree_sha256": canonical_sha256(receipts),
        "external_bindings": external_bindings,
    }


def verify_manifest(
    root: Path,
    manifest: dict[str, Any],
    *,
    logical_root: str,
    actual_root: Path,
) -> list[str]:
    """Verify hashes and reject undeclared files in a package directory."""

    errors: list[str] = []
    prefix = logical_root.rstrip("/") + "/"
    excluded = manifest.get("self_excluded_from_files")
    declared: set[str] = set()
    for row in manifest.get("files", []):
        logical = row.get("path")
        if not isinstance(logical, str) or not logical.startswith(prefix):
            errors.append(f"logical path outside root: {logical}")
            continue
        relative = logical[len(prefix) :]
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            errors.append(f"unsafe logical path: {logical}")
            continue
        normalized = relative_path.as_posix()
        if normalized in declared:
            errors.append(f"duplicate logical path: {logical}")
            continue
        declared.add(normalized)
        path = actual_root / relative_path
        if not path.is_file():
            errors.append(f"declared file missing: {normalized}")
            continue
        if path.stat().st_size != row.get("bytes"):
            errors.append(f"byte mismatch: {normalized}")
        elif sha256_file(path) != row.get("sha256"):
            errors.append(f"sha256 mismatch: {normalized}")
    actual = {
        path.relative_to(actual_root).as_posix()
        for path in actual_root.rglob("*")
        if path.is_file()
        and path.relative_to(actual_root).as_posix() != excluded
    }
    for relative in sorted(actual - declared):
        errors.append(f"unexpected file not declared: {relative}")
    for relative in sorted(declared - actual):
        errors.append(f"declared file missing: {relative}")
    return errors


def verify_external_bindings(
    root: Path,
    bindings: dict[str, dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    for binding_id, receipt in bindings.items():
        path_value = receipt.get("path")
        if not isinstance(path_value, str):
            errors.append(f"{binding_id}: missing path")
            continue
        path = Path(path_value)
        if path.is_absolute() or ".." in path.parts:
            errors.append(f"{binding_id}: unsafe path")
            continue
        live = file_receipt(root, path_value)
        if live != receipt:
            errors.append(f"{binding_id}: live receipt mismatch")
    return errors


def run_unit_tests(root: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "unittest",
        "-v",
        "control.tests.test_build_stage_a_core03_core06_semantic_closure_v2",
    ]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (completed.stdout + completed.stderr).strip()
    match = re.search(r"Ran (\d+) tests?", output)
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "python_executable": str(Path(sys.executable).resolve()),
        "executed_generator_source": file_receipt(root, BUILD_TOOL_PATH),
        "executed_test_source": file_receipt(root, TEST_SOURCE_PATH),
        "exit_code": completed.returncode,
        "tests_run": int(match.group(1)) if match else None,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "output": output,
    }


def preserve_repair_snapshot(
    root: Path,
    package_root: Path,
    repair_id: str,
) -> list[dict[str, Any]]:
    history = package_root / "repair_history" / repair_id
    if history.exists():
        raise FileExistsError(history)
    sources = [
        path
        for path in sorted(package_root.rglob("*"))
        if path.is_file() and "repair_history" not in path.relative_to(package_root).parts
    ]
    history.mkdir(parents=True)
    preserved: list[dict[str, Any]] = []
    for source in sources:
        relative = source.relative_to(package_root)
        target = history / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        original = file_receipt(root, rel(root, source))
        shutil.copy2(source, target)
        copy = file_receipt(root, rel(root, target))
        if original["bytes"] != copy["bytes"] or original["sha256"] != copy["sha256"]:
            raise RuntimeError(f"Repair-history copy mismatch: {relative}")
        preserved.append(
            {
                "original": original,
                "preserved_copy": copy,
            }
        )
    return preserved


def existing_repair_snapshot(
    root: Path,
    package_root: Path,
    repair_id: str,
) -> list[dict[str, Any]]:
    history = package_root / "repair_history" / repair_id
    if not history.is_dir():
        raise FileNotFoundError(history)
    preserved: list[dict[str, Any]] = []
    for copy_path in sorted(history.rglob("*")):
        if not copy_path.is_file():
            continue
        relative = copy_path.relative_to(history)
        copy = file_receipt(root, rel(root, copy_path))
        preserved.append(
            {
                "original": {
                    "path": rel(root, package_root / relative),
                    "bytes": copy["bytes"],
                    "sha256": copy["sha256"],
                },
                "preserved_copy": copy,
            }
        )
    return preserved


def verify_preserved_snapshot(
    root: Path,
    package_root: Path,
    repair_id: str,
    manifest_name: str,
) -> list[str]:
    history = package_root / "repair_history" / repair_id
    manifest = load_json(history / manifest_name)
    manifest = dict(manifest)
    manifest["self_excluded_from_files"] = manifest_name
    return verify_manifest(
        root,
        manifest,
        logical_root=rel(root, package_root),
        actual_root=history,
    )


def build(root: Path, candidate: Path, report: Path) -> dict[str, Any]:
    root = root.resolve()
    candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate
    report = (root / report).resolve() if not report.is_absolute() else report
    for output in (candidate, report):
        output.relative_to(root)
        output.mkdir(parents=True, exist_ok=True)

    guard_before = formal_guard(root)
    inputs = load_inputs(root)
    evidence_matrix = inputs["TABLE_SEMANTICS_EVIDENCE_MATRIX"]["json"]
    measurements = inputs["TABLE_SEMANTICS_MEASUREMENTS"]["json"]
    semantics = inputs["NULL_ZERO_SEMANTICS_MATRIX"]["json"]
    key_conflicts = inputs["NULL_ZERO_KEY_CONFLICTS"]["json"]
    coverage = inputs["OFFICIAL_RULE_COVERAGE"]["json"]
    effective_dates = inputs["OFFICIAL_EFFECTIVE_DATE_MATRIX"]["json"]
    gaps = inputs["OFFICIAL_HISTORICAL_GAPS"]["json"]
    queue_delta = inputs["STAGE_A_QUEUE_DELTA"]["json"]

    if evidence_matrix["decision"]["core03_q2_existing_status"] != "UNKNOWN":
        raise RuntimeError("Upstream CORE03-Q2 is not fail-closed UNKNOWN")
    if evidence_matrix["decision"]["core03_q3_existing_status"] != "UNKNOWN":
        raise RuntimeError("Upstream CORE03-Q3 is not fail-closed UNKNOWN")
    if key_conflicts["conflict_key_count"] != 0:
        raise RuntimeError("Unexpected conflicting TRD_Dalyr keys")
    if queue_delta["after_audit_only"]["formal_state"]["stage_a_ready"] != "0/4":
        raise RuntimeError("Upstream Stage A status is not 0/4")

    raw_zip_receipts = verify_raw_zips(root, measurements)
    markers = marker_evidence(root)
    field_matrix = field_semantics_matrix()
    anomalies = anomaly_ledger(semantics)
    rules = rule_version_matrix(coverage, effective_dates, gaps, markers)
    one_sided_hash = anomalies["anomalies"][1]["rows_sha256"]
    gates = gate_impact(one_sided_hash)

    source_bindings = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "path_contract": "CANONICAL_WORKSPACE_RELATIVE",
        "direct_input_files": {
            key: value["receipt"] for key, value in inputs.items()
        },
        "sealed_trd_zip_receipts": raw_zip_receipts,
        "official_extracted_text_receipts": markers,
        "transitive_binding_note": (
            "Raw CSV member CRC/SHA and curated partition hashes remain in "
            "TABLE_SEMANTICS_MEASUREMENTS, which is directly hash-bound here."
        ),
    }

    candidate_payloads = {
        "FIELD_SEMANTICS_MATRIX.json": field_matrix,
        "ANOMALY_LEDGER.json": anomalies,
        "RULE_VERSION_MATRIX.json": rules,
        "GATE_IMPACT.json": gates,
        "SOURCE_BINDINGS.json": source_bindings,
    }
    for name, payload in candidate_payloads.items():
        path = candidate / name
        write_json(path, payload)

    readme = f"""# {RUN_ID}

这是 CORE-03 / CORE-06 的离线、只读语义证据包，不是正式准入。

## 已确证

- 标准 `TRD_Dalyr` 的表身份、24 字段范围、`Trdsta=1..16` 标签值域。
- `Dnshrtrd=0` 的供应商原文；`Dnvaltrd=0` 原文逐字保留。
- `LimitDown` / `LimitUp` 单位与 `LimitStatus=-1/0/1` 收盘状态说明。
- 18,691,664 个无冲突键的机械审计结果。
- 5 条成交量/成交额单边为零反例；规范 JSON SHA-256：
  `{one_sided_hash}`。
- 已保存的上交所、深交所、北交所规则可证明停复牌/临时停市等市场机制。

## 明确保留的异常与缺口

- `Dnvaltrd` 是成交金额字段，但说明书原文写作“0=没有交易量”。本包标记为
  `TEXT_LITERAL_PRESERVED__POSSIBLE_LABEL_COPY_ERROR_NOT_RESOLVED`，没有擅自改成
  “没有交易金额”。
- 交易所规则不能推出 CSMAR `TRD_Dalyr` 在全天停牌、无交易、缺失、未知时的
  行生成与 NULL/0 编码。
- 5 条单边零值不填补、不纠正、不丢弃。
- 三所全历史规则版本链、证券级 ST/退市/重上市有效区间、全部无涨跌幅例外和
  精确舍入链仍未闭合。

## Gate 结果

- `CORE03-Q2 = UNKNOWN`
- `CORE03-Q3 = UNKNOWN`
- `CORE06-Q3 = FAIL`
- `CORE06-Q4 = FAIL`
- Stage A 保持 `0/4`，没有正式 catalog/registry/Roadmap/projects 修改，没有快照，
  不可用于研究。

## 可复核执行证据

- 机器可读测试命令、退出码、实际执行的生成器/测试源码哈希见
  `TEST_RESULTS.json`。
- `CANDIDATE_MANIFEST.json` 与 `REPORT_MANIFEST.json` 均绑定生成器、测试源码和
  `TEST_RESULTS.json`；严格复核会拒绝未声明的额外文件。
- 只读复核命令：
  `python -X utf8 control/tools/build_stage_a_core03_core06_semantic_closure_v2.py --verify-only`
"""
    readme_path = report / "README.md"
    write_text(readme_path, readme)

    test_results = run_unit_tests(root)
    test_results_path = report / "TEST_RESULTS.json"
    write_json(test_results_path, test_results)
    if test_results["status"] != "PASS":
        raise RuntimeError("CORE03/CORE06 semantic-closure unit tests failed")

    guard_after = formal_guard(root)
    guard_unchanged = guard_before == guard_after
    if not guard_unchanged:
        raise RuntimeError("Formal guard changed during evidence build")

    executed_bindings = {
        "executed_generator_source": file_receipt(root, BUILD_TOOL_PATH),
        "executed_test_source": file_receipt(root, TEST_SOURCE_PATH),
        "machine_readable_test_results": file_receipt(
            root, rel(root, test_results_path)
        ),
    }
    verification = {
        "schema_version": 3,
        "run_id": RUN_ID,
        "status": "PASS__FAIL_CLOSED_EVIDENCE_PACKAGE",
        "checks": {
            "network_used": False,
            "portal_accessed": False,
            "input_files_hash_bound": len(inputs),
            "sealed_trd_zips_hash_verified": len(raw_zip_receipts),
            "official_text_files_marker_verified": len(markers),
            "trd_unique_nonconflicting_rows": key_conflicts[
                "clean_nonconflicting_rows"
            ],
            "trd_conflict_key_count": key_conflicts["conflict_key_count"],
            "one_sided_zero_rows": 5,
            "one_sided_zero_rows_sha256": one_sided_hash,
            "dnvaltrd_literal_auto_corrected": False,
            "formal_guard_unchanged": guard_unchanged,
            "unit_tests_status": test_results["status"],
            "unit_tests_run": test_results["tests_run"],
        },
        "executed_artifact_bindings": executed_bindings,
        "expected_gate_statuses": {
            gate_id: row["status"]
            for gate_id, row in gates["gates"].items()
        },
        "formal_guard_before": guard_before,
        "formal_guard_after": guard_after,
        "formal_admission": False,
        "quality_promotion": False,
        "research_usable": False,
    }
    verification_path = report / "VERIFICATION.json"
    write_json(verification_path, verification)

    candidate_payload_paths = expected_payload_paths(
        candidate, CANDIDATE_PAYLOAD_NAMES
    )
    candidate_manifest = output_manifest(
        root,
        RUN_ID,
        rel(root, candidate),
        candidate,
        candidate_payload_paths,
        "CANDIDATE_PAYLOAD",
        manifest_name="CANDIDATE_MANIFEST.json",
        external_bindings=executed_bindings,
    )
    candidate_manifest_path = candidate / "CANDIDATE_MANIFEST.json"
    write_json(candidate_manifest_path, candidate_manifest)

    report_payload_paths = expected_payload_paths(
        report,
        REPORT_PAYLOAD_NAMES,
        optional_names={"QA_REPAIR_RECEIPT.json"},
    )
    report_bindings = {
        **executed_bindings,
        "candidate_manifest": file_receipt(
            root, rel(root, candidate_manifest_path)
        ),
    }
    report_manifest = output_manifest(
        root,
        RUN_ID,
        rel(root, report),
        report,
        report_payload_paths,
        "REPORT_PAYLOAD",
        manifest_name="REPORT_MANIFEST.json",
        external_bindings=report_bindings,
    )
    report_manifest_path = report / "REPORT_MANIFEST.json"
    write_json(report_manifest_path, report_manifest)

    candidate_errors = verify_manifest(
        root,
        candidate_manifest,
        logical_root=rel(root, candidate),
        actual_root=candidate,
    )
    report_errors = verify_manifest(
        root,
        report_manifest,
        logical_root=rel(root, report),
        actual_root=report,
    )
    external_errors = verify_external_bindings(root, report_bindings)
    if candidate_errors or report_errors or external_errors:
        raise RuntimeError(
            "Strict package verification failed: "
            + "; ".join(candidate_errors + report_errors + external_errors)
        )

    return {
        "run_id": RUN_ID,
        "candidate_root": rel(root, candidate),
        "report_root": rel(root, report),
        "candidate_manifest": file_receipt(
            root, rel(root, candidate_manifest_path)
        ),
        "report_manifest": file_receipt(root, rel(root, report_manifest_path)),
        "one_sided_rows_sha256": one_sided_hash,
        "gate_statuses": verification["expected_gate_statuses"],
        "formal_guard_unchanged": guard_unchanged,
        "tests": {
            "status": test_results["status"],
            "tests_run": test_results["tests_run"],
            "receipt": file_receipt(root, rel(root, test_results_path)),
        },
        "strict_manifest_verification": "PASS",
    }


def verify_existing(root: Path, candidate: Path, report: Path) -> dict[str, Any]:
    """Strict, read-only package verification with live formal-state guard."""

    root = root.resolve()
    candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate
    report = (root / report).resolve() if not report.is_absolute() else report
    candidate.relative_to(root)
    report.relative_to(root)
    candidate_manifest = load_json(candidate / "CANDIDATE_MANIFEST.json")
    report_manifest = load_json(report / "REPORT_MANIFEST.json")
    verification = load_json(report / "VERIFICATION.json")
    test_results = load_json(report / "TEST_RESULTS.json")
    gates = load_json(candidate / "GATE_IMPACT.json")

    candidate_errors = verify_manifest(
        root,
        candidate_manifest,
        logical_root=rel(root, candidate),
        actual_root=candidate,
    )
    report_errors = verify_manifest(
        root,
        report_manifest,
        logical_root=rel(root, report),
        actual_root=report,
    )
    candidate_external = candidate_manifest.get("external_bindings", {})
    report_external = report_manifest.get("external_bindings", {})
    candidate_external_errors = verify_external_bindings(
        root, candidate_external
    )
    report_external_errors = verify_external_bindings(root, report_external)
    binding_consistent = all(
        candidate_external.get(key) == report_external.get(key)
        for key in (
            "executed_generator_source",
            "executed_test_source",
            "machine_readable_test_results",
        )
    )
    verification_bindings_match = (
        verification.get("executed_artifact_bindings") == candidate_external
    )
    live_guard = formal_guard(root)
    recorded_pre = verification.get("formal_guard_before")
    recorded_after = verification.get("formal_guard_after")
    live_guard_matches = live_guard == recorded_pre == recorded_after
    expected_gate_statuses = {
        "CORE03-Q2": "UNKNOWN",
        "CORE03-Q3": "UNKNOWN",
        "CORE06-Q3": "FAIL",
        "CORE06-Q4": "FAIL",
    }
    observed_gate_statuses = {
        gate_id: gates["gates"][gate_id]["status"]
        for gate_id in expected_gate_statuses
    }
    test_results_valid = (
        test_results.get("status") == "PASS"
        and test_results.get("exit_code") == 0
        and isinstance(test_results.get("tests_run"), int)
        and test_results["tests_run"] >= 1
        and test_results.get("executed_generator_source")
        == candidate_external.get("executed_generator_source")
        and test_results.get("executed_test_source")
        == candidate_external.get("executed_test_source")
    )
    result = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "mode": "STRICT_VERIFY_ONLY",
        "writes_performed": 0,
        "network_used": False,
        "candidate_manifest_errors": candidate_errors,
        "report_manifest_errors": report_errors,
        "candidate_external_binding_errors": candidate_external_errors,
        "report_external_binding_errors": report_external_errors,
        "candidate_report_bindings_consistent": binding_consistent,
        "verification_bindings_match": verification_bindings_match,
        "machine_readable_test_results_valid": test_results_valid,
        "tests_run": test_results.get("tests_run"),
        "live_formal_guard_matches_recorded": live_guard_matches,
        "expected_gate_statuses": expected_gate_statuses,
        "observed_gate_statuses": observed_gate_statuses,
        "gate_statuses_unchanged": (
            observed_gate_statuses == expected_gate_statuses
        ),
        "formal_admission": gates.get("formal_admission"),
        "research_usable": gates.get("research_usable"),
    }
    result["status"] = (
        "PASS"
        if not candidate_errors
        and not report_errors
        and not candidate_external_errors
        and not report_external_errors
        and binding_consistent
        and verification_bindings_match
        and test_results_valid
        and live_guard_matches
        and result["gate_statuses_unchanged"]
        and result["formal_admission"] is False
        and result["research_usable"] is False
        else "FAIL"
    )
    return result


def repair_existing(root: Path, candidate: Path, report: Path) -> dict[str, Any]:
    root = root.resolve()
    candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate
    report = (root / report).resolve() if not report.is_absolute() else report
    candidate.relative_to(root)
    report.relative_to(root)
    if not candidate.is_dir() or not report.is_dir():
        raise FileNotFoundError("Existing candidate and report are required")

    guard_before = formal_guard(root)
    candidate_history_exists = (
        candidate / "repair_history" / REPAIR_ID
    ).is_dir()
    report_history_exists = (
        report / "repair_history" / REPAIR_ID
    ).is_dir()
    if candidate_history_exists != report_history_exists:
        raise RuntimeError("Only one repair-history side exists; fail closed")
    if candidate_history_exists:
        pre_candidate_errors = verify_preserved_snapshot(
            root,
            candidate,
            REPAIR_ID,
            "CANDIDATE_MANIFEST.json",
        )
        pre_report_errors = verify_preserved_snapshot(
            root,
            report,
            REPAIR_ID,
            "REPORT_MANIFEST.json",
        )
        if pre_candidate_errors or pre_report_errors:
            raise RuntimeError(
                "Preserved pre-repair package is not hash-consistent: "
                + "; ".join(pre_candidate_errors + pre_report_errors)
            )
        preserved_candidate = existing_repair_snapshot(
            root, candidate, REPAIR_ID
        )
        preserved_report = existing_repair_snapshot(
            root, report, REPAIR_ID
        )
    else:
        old_candidate_manifest = load_json(
            candidate / "CANDIDATE_MANIFEST.json"
        )
        old_candidate_manifest = dict(old_candidate_manifest)
        old_candidate_manifest["self_excluded_from_files"] = (
            "CANDIDATE_MANIFEST.json"
        )
        old_report_manifest = load_json(report / "REPORT_MANIFEST.json")
        old_report_manifest = dict(old_report_manifest)
        old_report_manifest["self_excluded_from_files"] = (
            "REPORT_MANIFEST.json"
        )
        pre_candidate_errors = verify_manifest(
            root,
            old_candidate_manifest,
            logical_root=rel(root, candidate),
            actual_root=candidate,
        )
        pre_report_errors = verify_manifest(
            root,
            old_report_manifest,
            logical_root=rel(root, report),
            actual_root=report,
        )
        if pre_candidate_errors or pre_report_errors:
            raise RuntimeError(
                "Pre-repair package is not internally hash-consistent: "
                + "; ".join(pre_candidate_errors + pre_report_errors)
            )
        preserved_candidate = preserve_repair_snapshot(
            root, candidate, REPAIR_ID
        )
        preserved_report = preserve_repair_snapshot(
            root, report, REPAIR_ID
        )
    build_result = build(root, candidate, report)
    guard_after_build = formal_guard(root)
    if guard_before != guard_after_build:
        raise RuntimeError("Formal guard changed during repair build")

    test_results_path = report / "TEST_RESULTS.json"
    candidate_manifest_path = candidate / "CANDIDATE_MANIFEST.json"
    repair_receipt = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "repair_id": REPAIR_ID,
        "repaired_at_utc": datetime.now(timezone.utc).isoformat(),
        "trigger": (
            "Independent review found that manifests did not bind the "
            "executed generator/test sources or machine-readable test result."
        ),
        "network_requests_during_repair": 0,
        "raw_files_modified": 0,
        "formal_authority_files_modified": 0,
        "formal_snapshot_or_published_files_modified": 0,
        "deletions": 0,
        "pre_repair_candidate_files": preserved_candidate,
        "pre_repair_report_files": preserved_report,
        "changes": [
            "Preserved the complete pre-repair candidate/report package.",
            "Bound the live generator and test source hashes in both manifests.",
            "Added machine-readable TEST_RESULTS with command, output and exit code.",
            "Added strict verify-only mode with live formal-guard recomputation.",
            "Added exact recursive manifest verification and extra-file rejection.",
            "Kept CORE03-Q2/Q3 UNKNOWN and CORE06-Q3/Q4 FAIL.",
        ],
        "executed_generator_source": file_receipt(root, BUILD_TOOL_PATH),
        "executed_test_source": file_receipt(root, TEST_SOURCE_PATH),
        "machine_readable_test_results": file_receipt(
            root, rel(root, test_results_path)
        ),
        "candidate_manifest_after_repair": file_receipt(
            root, rel(root, candidate_manifest_path)
        ),
        "formal_guard_before_repair": guard_before,
        "formal_guard_after_repair_build": guard_after_build,
    }
    repair_receipt_path = report / "QA_REPAIR_RECEIPT.json"
    write_json(repair_receipt_path, repair_receipt)

    executed_bindings = {
        "executed_generator_source": file_receipt(root, BUILD_TOOL_PATH),
        "executed_test_source": file_receipt(root, TEST_SOURCE_PATH),
        "machine_readable_test_results": file_receipt(
            root, rel(root, test_results_path)
        ),
        "candidate_manifest": file_receipt(
            root, rel(root, candidate_manifest_path)
        ),
    }
    report_payload_paths = expected_payload_paths(
        report,
        REPORT_PAYLOAD_NAMES,
        optional_names={"QA_REPAIR_RECEIPT.json"},
    )
    report_manifest = output_manifest(
        root,
        RUN_ID,
        rel(root, report),
        report,
        report_payload_paths,
        "REPORT_PAYLOAD",
        manifest_name="REPORT_MANIFEST.json",
        external_bindings=executed_bindings,
    )
    report_manifest_path = report / "REPORT_MANIFEST.json"
    write_json(report_manifest_path, report_manifest)

    guard_after = formal_guard(root)
    if guard_before != guard_after:
        raise RuntimeError("Formal guard changed during repair finalization")
    strict_result = verify_existing(root, candidate, report)
    if strict_result["status"] != "PASS":
        raise RuntimeError(
            "Strict post-repair verification failed: "
            + json.dumps(strict_result, ensure_ascii=False, sort_keys=True)
        )
    return {
        "run_id": RUN_ID,
        "repair_id": REPAIR_ID,
        "candidate_manifest": file_receipt(
            root, rel(root, candidate_manifest_path)
        ),
        "report_manifest": file_receipt(root, rel(root, report_manifest_path)),
        "repair_receipt": file_receipt(
            root, rel(root, repair_receipt_path)
        ),
        "test_results": file_receipt(root, rel(root, test_results_path)),
        "tests_run": strict_result["tests_run"],
        "strict_verify": strict_result["status"],
        "gate_statuses": strict_result["observed_gate_statuses"],
        "formal_guard_unchanged": True,
        "network_used": False,
        "raw_files_modified": 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--candidate", type=Path, default=Path(DEFAULT_CANDIDATE))
    parser.add_argument("--report", type=Path, default=Path(DEFAULT_REPORT))
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--repair-existing", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.verify_only and args.repair_existing:
        raise SystemExit("--verify-only and --repair-existing are mutually exclusive")
    if args.verify_only:
        result = verify_existing(args.root, args.candidate, args.report)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["status"] == "PASS" else 1
    if args.repair_existing:
        result = repair_existing(args.root, args.candidate, args.report)
    else:
        result = build(args.root, args.candidate, args.report)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
