#!/usr/bin/env python3
"""Resolve the preserved STKSPCTRMT tail anomalies without semantic guessing.

The v1 zero-loss candidate deliberately left 73 records fail-closed because a
normal CSV reader returned more than the documented 15 columns.  The online
RESSSET dictionary later confirmed the final schema contract:

* field 14: ``TrmtDes`` (declared width 400);
* field 15: ``TrmtRea`` (declared width 255);
* ``TrmtType`` is the integer enumeration 1--15;
* ``InfoPubDt`` spans 1998-04-28 through 2026-07-01 in this export.

This tool does not infer meaning from the Chinese text.  It enumerates every
raw comma that can separate exactly two scalar fields while retaining the raw
field bytes (represented as decoded Unicode text), then applies only the field
order and declared character limits.  A quoted scalar may contain bare inner
quotes because that is the precise defect being repaired; its *outermost*
quotes remain structural.  An unquoted scalar may not contain quotes, commas,
or line breaks.

If and only if every v1-ambiguous record has exactly one legal split, an
independent v2 inspection candidate is materialized.  Otherwise the tool
writes no v2 candidate.  It never overwrites v1, writes no catalog or source
registry, and has no admission or quality-promotion effect.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_V1_ROOT = (
    WORKSPACE_ROOT
    / "tmp"
    / "local-dataset-build"
    / "resset-stkspctrmt"
    / "manual-export-20260802-zero-loss-v1"
)
DEFAULT_V2_ROOT = (
    WORKSPACE_ROOT
    / "tmp"
    / "local-dataset-build"
    / "resset-stkspctrmt"
    / "manual-export-20260802-zero-loss-v2"
)
CATALOG_PATH = WORKSPACE_ROOT / "data" / "catalog" / "datasets.json"
SOURCE_REGISTRY_PATH = (
    WORKSPACE_ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "source_admission_v1"
    / "current_source_registry.json"
)

TOOL_ID = (
    "audit_resset_stkspctrmt_tail_v2.py/"
    "resset-stkspctrmt-raw-tail-unique-resolution-v2"
)
EXPECTED_V1_CANDIDATE_SHA256 = (
    "8e7aa3c8ead322bc46b69cd4f689c42df00e65e5abeafbaa9a477199fc8e5640"
)
EXPECTED_SOURCE_ZIP_SHA256 = (
    "963e36bcbc8c3dc305932cf169829865d4c3d5375430c70f57e6f217533bcbdb"
)
EXPECTED_SOURCE_MEMBER_SHA256 = (
    "76a089fbde2b67f22987497219b80b0f3b6697216426066638777a07cd391537"
)
EXPECTED_ROW_COUNT = 4_105
EXPECTED_AMBIGUOUS_COUNT = 73
V1_AMBIGUOUS_STATUS = "AMBIGUOUS_TAIL_PRESERVED_ONLY"
V1_STANDARD_STATUS = "UNAMBIGUOUS_15_COLUMN_PARSE"
V2_RESOLVED_STATUS = "UNIQUE_RAW_QUOTE_AND_LENGTH_RECOVERY_V2"
TRMT_DES_LIMIT = 400
TRMT_REA_LIMIT = 255
TRMT_TYPE_MIN = 1
TRMT_TYPE_MAX = 15
INFO_PUB_DATE_MIN = "1998-04-28"
INFO_PUB_DATE_MAX = "2026-07-01"

V2_EXTRA_COLUMNS = (
    "v1_tail_parse_status",
    "v1_repair_class",
    "v2_tail_resolution_method",
    "v2_candidate_count",
    "v2_dictionary_length_check",
    "v2_source_reconstruction_status",
)


class TailAuditError(RuntimeError):
    """Fail-closed validation error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def hash_tree(root: Path) -> dict[str, dict[str, Any]]:
    if not root.is_dir():
        raise TailAuditError(f"expected directory is missing: {root}")
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        result[path.relative_to(root).as_posix()] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    return result


def file_guard(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise TailAuditError(f"guarded file is missing: {path}")
    return {
        "path": path.as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def strip_record_ending(raw_tail: str) -> tuple[str, str]:
    if raw_tail.endswith("\r\n"):
        return raw_tail[:-2], "CRLF"
    if raw_tail.endswith("\n"):
        return raw_tail[:-1], "LF"
    raise TailAuditError("raw tail has no recognized record terminator")


def decode_scalar(raw_field: str) -> tuple[str, str] | None:
    """Decode one field under the explicit zero-loss repair grammar.

    Outermost quotes are structural and are removed.  All inner characters,
    including bare quotes and commas, remain literal and unchanged.  This is
    reversible because the original raw field and its hash remain in evidence.
    """

    if raw_field == "":
        return "", "EMPTY_UNQUOTED"
    if raw_field.startswith('"') and raw_field.endswith('"'):
        return raw_field[1:-1], "OUTERMOST_QUOTED_VERBATIM_INTERIOR"
    if any(character in raw_field for character in ('"', ",", "\r", "\n")):
        return None
    return raw_field, "STRICT_UNQUOTED"


def enumerate_tail_splits(raw_tail: str) -> tuple[list[dict[str, Any]], str]:
    payload, ending = strip_record_ending(raw_tail)
    candidates: list[dict[str, Any]] = []
    for index, character in enumerate(payload):
        if character != ",":
            continue
        left_raw = payload[:index]
        right_raw = payload[index + 1 :]
        left = decode_scalar(left_raw)
        right = decode_scalar(right_raw)
        if left is None or right is None:
            continue
        left_value, left_mode = left
        right_value, right_mode = right
        if len(left_value) > TRMT_DES_LIMIT or len(right_value) > TRMT_REA_LIMIT:
            continue
        reconstructed = f"{left_raw},{right_raw}"
        if reconstructed != payload:
            raise TailAuditError("internal raw-tail reconstruction failed")
        candidates.append(
            {
                "delimiter_char_index": index,
                "TrmtDes": left_value,
                "TrmtRea": right_value,
                "left_raw": left_raw,
                "right_raw": right_raw,
                "left_decode_mode": left_mode,
                "right_decode_mode": right_mode,
                "TrmtDes_char_count": len(left_value),
                "TrmtRea_char_count": len(right_value),
                "TrmtDes_sha256_utf8": sha256_text(left_value),
                "TrmtRea_sha256_utf8": sha256_text(right_value),
                "raw_reconstruction_status": "PASS",
            }
        )
    return candidates, ending


def load_v1_candidate(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if sha256_file(path) != EXPECTED_V1_CANDIDATE_SHA256:
        raise TailAuditError("v1 candidate SHA-256 changed")
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise TailAuditError("v1 candidate has no header")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    if len(rows) != EXPECTED_ROW_COUNT:
        raise TailAuditError("v1 candidate row count changed")
    nobs = [int(row["nobs"]) for row in rows]
    if nobs != list(range(1, EXPECTED_ROW_COUNT + 1)):
        raise TailAuditError("v1 nobs is not the exact sequence 1..4105")
    return fieldnames, rows


def validate_raw_hashes(rows: list[dict[str, str]]) -> None:
    for row in rows:
        if sha256_text(row["raw_record_text"]) != row["raw_record_sha256"]:
            raise TailAuditError(f"raw record hash mismatch at nobs={row['nobs']}")
        if sha256_text(row["tail_raw_text"]) != row["tail_sha256"]:
            raise TailAuditError(f"raw tail hash mismatch at nobs={row['nobs']}")


def dictionary_checks(rows: list[dict[str, str]]) -> dict[str, Any]:
    trmt_types = [int(row["TrmtType"]) for row in rows]
    info_dates = [row["InfoPubDt"] for row in rows]
    result = {
        "TrmtType_min": min(trmt_types),
        "TrmtType_max": max(trmt_types),
        "TrmtType_all_in_1_to_15": all(
            TRMT_TYPE_MIN <= value <= TRMT_TYPE_MAX for value in trmt_types
        ),
        "InfoPubDt_min": min(info_dates),
        "InfoPubDt_max": max(info_dates),
        "InfoPubDt_matches_confirmed_export_range": (
            min(info_dates) == INFO_PUB_DATE_MIN
            and max(info_dates) == INFO_PUB_DATE_MAX
        ),
    }
    if not result["TrmtType_all_in_1_to_15"]:
        raise TailAuditError("TrmtType violates the confirmed 1..15 enumeration")
    if not result["InfoPubDt_matches_confirmed_export_range"]:
        raise TailAuditError("InfoPubDt range differs from the confirmed export")
    return result


def resolve_rows(
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    ambiguous = [row for row in rows if row["tail_parse_status"] == V1_AMBIGUOUS_STATUS]
    unexpected = [
        row["nobs"]
        for row in rows
        if row["tail_parse_status"] not in {V1_AMBIGUOUS_STATUS, V1_STANDARD_STATUS}
    ]
    if unexpected:
        raise TailAuditError(f"unexpected v1 tail statuses at nobs={unexpected[:10]}")
    if len(ambiguous) != EXPECTED_AMBIGUOUS_COUNT:
        raise TailAuditError("v1 ambiguous-tail count changed")

    resolved_rows: list[dict[str, str]] = []
    ledger: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    standard_reason_nonempty = sum(
        bool(row["TrmtRea_candidate"])
        for row in rows
        if row["tail_parse_status"] == V1_STANDARD_STATUS
    )

    for original in rows:
        row = dict(original)
        row["v1_tail_parse_status"] = original["tail_parse_status"]
        row["v1_repair_class"] = original["repair_class"]
        if original["tail_parse_status"] == V1_STANDARD_STATUS:
            row["v2_tail_resolution_method"] = "V1_UNAMBIGUOUS_PASSTHROUGH"
            row["v2_candidate_count"] = "1"
            row["v2_dictionary_length_check"] = "PASS"
            row["v2_source_reconstruction_status"] = "PASS_V1_RAW_PRESERVED"
            resolved_rows.append(row)
            continue

        candidates, ending = enumerate_tail_splits(original["tail_raw_text"])
        decision = "UNIQUE" if len(candidates) == 1 else (
            "NO_LEGAL_SPLIT" if not candidates else "MULTIPLE_LEGAL_SPLITS"
        )
        evidence: dict[str, Any] = {
            "nobs": int(original["nobs"]),
            "raw_record_sha256": original["raw_record_sha256"],
            "tail_sha256": original["tail_sha256"],
            "v1_repair_class": original["repair_class"],
            "physical_line_count": int(original["physical_line_count"]),
            "record_ending": ending,
            "raw_tail_char_count": len(original["tail_raw_text"]),
            "raw_comma_count": original["tail_raw_text"].count(","),
            "raw_quote_count": original["tail_raw_text"].count('"'),
            "raw_internal_quoted_transition_count": original[
                "tail_raw_text"
            ][1:-4].count('","'),
            "legal_candidate_count": len(candidates),
            "decision": decision,
            "decision_basis": (
                "raw scalar envelope + exact two-field order + "
                "TrmtDes<=400 + TrmtRea<=255; no text semantics"
            ),
            "candidates": candidates,
            "tail_raw_text": original["tail_raw_text"],
        }
        ledger.append(evidence)
        if len(candidates) != 1:
            unresolved.append(evidence)
            continue

        candidate = candidates[0]
        row["TrmtDes_candidate"] = candidate["TrmtDes"]
        row["TrmtRea_candidate"] = candidate["TrmtRea"]
        row["tail_parse_status"] = V2_RESOLVED_STATUS
        row["v2_tail_resolution_method"] = (
            "EXHAUSTIVE_RAW_TWO_SCALAR_ENUMERATION__UNIQUE"
        )
        row["v2_candidate_count"] = "1"
        row["v2_dictionary_length_check"] = "PASS"
        row["v2_source_reconstruction_status"] = "PASS"
        resolved_rows.append(row)

    metrics = {
        "row_count": len(rows),
        "v1_ambiguous_count": len(ambiguous),
        "v2_unique_resolution_count": sum(
            item["decision"] == "UNIQUE" for item in ledger
        ),
        "v2_unresolved_count": len(unresolved),
        "standard_TrmtRea_nonempty_count": standard_reason_nonempty,
        "ambiguous_raw_starts_with_quote_count": sum(
            item["tail_raw_text"].startswith('"') for item in ledger
        ),
        "ambiguous_raw_ends_with_quoted_desc_and_empty_reason_count": sum(
            item["tail_raw_text"].endswith('",\r\n') for item in ledger
        ),
        "ambiguous_internal_quoted_transition_count": sum(
            item["raw_internal_quoted_transition_count"] for item in ledger
        ),
        "resolved_TrmtDes_min_chars": min(
            item["candidates"][0]["TrmtDes_char_count"]
            for item in ledger
            if item["decision"] == "UNIQUE"
        ),
        "resolved_TrmtDes_max_chars": max(
            item["candidates"][0]["TrmtDes_char_count"]
            for item in ledger
            if item["decision"] == "UNIQUE"
        ),
        "resolved_TrmtRea_max_chars": max(
            item["candidates"][0]["TrmtRea_char_count"]
            for item in ledger
            if item["decision"] == "UNIQUE"
        ),
    }
    if unresolved:
        raise TailAuditError(
            f"fail-closed: {len(unresolved)} tails are not uniquely resolvable"
        )
    if len(resolved_rows) != EXPECTED_ROW_COUNT:
        raise TailAuditError("resolved row count is incomplete")
    return resolved_rows, ledger, metrics


def write_candidate_csv(
    path: Path, fieldnames: list[str], rows: list[dict[str, str]]
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=fieldnames,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def verify_written_candidate(
    path: Path, source_rows: list[dict[str, str]]
) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != EXPECTED_ROW_COUNT:
        raise TailAuditError("written v2 candidate row count changed")
    for source, written in zip(source_rows, rows, strict=True):
        if source != written:
            raise TailAuditError(f"v2 CSV roundtrip mismatch at nobs={source['nobs']}")
        if sha256_text(written["raw_record_text"]) != written["raw_record_sha256"]:
            raise TailAuditError("written raw-record hash mismatch")
        if sha256_text(written["tail_raw_text"]) != written["tail_sha256"]:
            raise TailAuditError("written raw-tail hash mismatch")
    return {
        "status": "PASS",
        "row_count": len(rows),
        "raw_record_hash_match_count": len(rows),
        "raw_tail_hash_match_count": len(rows),
        "nobs_unique_and_contiguous": [int(row["nobs"]) for row in rows]
        == list(range(1, EXPECTED_ROW_COUNT + 1)),
    }


def artifact(path: Path, root: Path, *, row_count: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "relative_path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if row_count is not None:
        value["row_count"] = row_count
    return value


def report_text(metrics: dict[str, Any], dictionary: dict[str, Any]) -> str:
    return f"""# RESSET STKSPCTRMT tail resolution v2

## Outcome

- v1 rows: {metrics['row_count']:,}
- v1 preserved ambiguous tails: {metrics['v1_ambiguous_count']}
- uniquely resolved by exhaustive raw-boundary enumeration: {metrics['v2_unique_resolution_count']}
- unresolved: {metrics['v2_unresolved_count']}
- recovered `TrmtDes` character range: {metrics['resolved_TrmtDes_min_chars']}--{metrics['resolved_TrmtDes_max_chars']}
- recovered `TrmtRea` maximum characters: {metrics['resolved_TrmtRea_max_chars']}

All 73 records begin with the outer quote and end with `\"`, followed by the
terminal delimiter and CRLF.  There is no internal `\",\"` transition, so no
alternative quoted second-field boundary exists.  Exhaustive enumeration gives
exactly one legal two-field split per record.  Inner quotes and commas are kept
verbatim; no linguistic or financial interpretation was used.

## Dictionary checks

- `TrmtType` observed range: {dictionary['TrmtType_min']}--{dictionary['TrmtType_max']} (confirmed enum 1--15)
- `InfoPubDt` observed range: {dictionary['InfoPubDt_min']}--{dictionary['InfoPubDt_max']}
- limits applied: `TrmtDes <= 400`, `TrmtRea <= 255` Unicode characters

## Boundary

This remains `INSPECTION_ONLY / NOT_ADMITTED`.  v1, the source ZIP, catalog,
source registry, quality status and formal admission are unchanged.  This v2
only closes the mechanical tail-arity ambiguity for the local candidate.
"""


def plan(v1_root: Path, v2_root: Path) -> dict[str, Any]:
    candidate = v1_root / "curated" / "candidate.csv"
    fieldnames, rows = load_v1_candidate(candidate)
    validate_raw_hashes(rows)
    dictionary = dictionary_checks(rows)
    resolved_rows, ledger, metrics = resolve_rows(rows)
    return {
        "tool_id": TOOL_ID,
        "mode": "PLAN",
        "write_performed": False,
        "v1_root": v1_root.as_posix(),
        "v2_root": v2_root.as_posix(),
        "v2_root_exists": v2_root.exists(),
        "metrics": metrics,
        "dictionary_checks": dictionary,
        "output_field_count": len(fieldnames) + len(V2_EXTRA_COLUMNS),
        "resolved_row_count": len(resolved_rows),
        "ledger_row_count": len(ledger),
        "safety": {
            "purpose_ceiling": "INSPECTION_ONLY",
            "admission_decision": "NOT_ADMITTED",
            "formal_effect": "NONE",
            "catalog_write": False,
            "quality_promotion": False,
            "v1_overwrite": False,
        },
    }


def execute(v1_root: Path, v2_root: Path) -> dict[str, Any]:
    if v2_root.exists():
        raise TailAuditError(f"refusing to overwrite existing v2 root: {v2_root}")
    if v2_root.parent.resolve() != DEFAULT_V2_ROOT.parent.resolve():
        raise TailAuditError("v2 root must be a direct child of the STKSPCTRMT build root")

    v1_tree_before = hash_tree(v1_root)
    catalog_before = file_guard(CATALOG_PATH)
    registry_before = file_guard(SOURCE_REGISTRY_PATH)
    v1_candidate = v1_root / "curated" / "candidate.csv"
    fieldnames, rows = load_v1_candidate(v1_candidate)
    validate_raw_hashes(rows)
    dictionary = dictionary_checks(rows)
    resolved_rows, ledger, metrics = resolve_rows(rows)

    source_zip = next((v1_root / "raw").glob("*.zip"))
    source_member = v1_root / "raw" / "extracted" / "RESSET_STKSPCTRMT_1.csv"
    if sha256_file(source_zip) != EXPECTED_SOURCE_ZIP_SHA256:
        raise TailAuditError("bound source ZIP hash changed")
    if sha256_file(source_member) != EXPECTED_SOURCE_MEMBER_SHA256:
        raise TailAuditError("bound source CSV-member hash changed")

    stage = v2_root.parent / f".staging-{v2_root.name}-{uuid.uuid4().hex}"
    (stage / "curated").mkdir(parents=True, exist_ok=False)
    (stage / "quality").mkdir(parents=True, exist_ok=False)

    output_fieldnames = [*fieldnames, *V2_EXTRA_COLUMNS]
    candidate_path = stage / "curated" / "candidate.csv"
    write_candidate_csv(candidate_path, output_fieldnames, resolved_rows)
    roundtrip = verify_written_candidate(candidate_path, resolved_rows)

    ledger_path = stage / "quality" / "tail-resolution-ledger.json"
    write_json(ledger_path, ledger)
    summary = {
        "schema_version": 2,
        "dataset_id": "resset-stkspctrmt",
        "table": "STKSPCTRMT",
        "status": "ALL_73_TAILS_UNIQUELY_RESOLVED",
        "created_at": utc_now(),
        "dictionary_contract": {
            "evidence_kind": "ONLINE_DICTIONARY_OPERATOR_CONFIRMED",
            "TrmtDes_declared_width": TRMT_DES_LIMIT,
            "TrmtRea_declared_width": TRMT_REA_LIMIT,
            "TrmtType_enum": [TRMT_TYPE_MIN, TRMT_TYPE_MAX],
            "InfoPubDt_export_range": [INFO_PUB_DATE_MIN, INFO_PUB_DATE_MAX],
        },
        "dictionary_checks": dictionary,
        "metrics": metrics,
        "candidate_roundtrip": roundtrip,
        "input_bindings": {
            "v1_candidate": file_guard(v1_candidate),
            "source_zip": file_guard(source_zip),
            "source_csv_member": file_guard(source_member),
        },
        "safety": {
            "purpose_ceiling": "INSPECTION_ONLY",
            "admission_decision": "NOT_ADMITTED",
            "formal_effect": "NONE",
            "catalog_registered": False,
            "quality_promotion": False,
            "research_ready": False,
            "source_admission": False,
        },
    }
    summary_path = stage / "quality" / "summary.json"
    write_json(summary_path, summary)

    report_path = stage / "REPORT.md"
    report_path.write_text(
        report_text(metrics, dictionary), encoding="utf-8", newline="\n"
    )
    provenance = {
        "schema_version": 2,
        "tool_id": TOOL_ID,
        "created_at": utc_now(),
        "workspace_root": WORKSPACE_ROOT.as_posix(),
        "input_v1_root": v1_root.as_posix(),
        "input_v1_candidate_sha256": sha256_file(v1_candidate),
        "bound_source_zip_sha256": sha256_file(source_zip),
        "bound_source_member_sha256": sha256_file(source_member),
        "transformation": (
            "exhaustive raw two-scalar boundary enumeration; outermost quote "
            "envelope retained as evidence; inner text retained verbatim"
        ),
        "semantic_inference_used": False,
        "manual_row_decisions": 0,
    }
    provenance_path = stage / "provenance.json"
    write_json(provenance_path, provenance)

    v1_tree_after = hash_tree(v1_root)
    catalog_after = file_guard(CATALOG_PATH)
    registry_after = file_guard(SOURCE_REGISTRY_PATH)
    verification = {
        "status": "PASS",
        "v1_tree_unchanged": v1_tree_before == v1_tree_after,
        "catalog_unchanged": catalog_before == catalog_after,
        "source_registry_unchanged": registry_before == registry_after,
        "catalog_before": catalog_before,
        "catalog_after": catalog_after,
        "source_registry_before": registry_before,
        "source_registry_after": registry_after,
        "candidate_roundtrip": roundtrip,
        "all_73_have_exactly_one_candidate": metrics["v2_unique_resolution_count"]
        == EXPECTED_AMBIGUOUS_COUNT
        and metrics["v2_unresolved_count"] == 0,
    }
    if not all(
        verification[key]
        for key in (
            "v1_tree_unchanged",
            "catalog_unchanged",
            "source_registry_unchanged",
            "all_73_have_exactly_one_candidate",
        )
    ):
        raise TailAuditError("post-write immutability verification failed")
    verification_path = stage / "quality" / "verification.json"
    write_json(verification_path, verification)

    artifacts = {
        "candidate_csv": artifact(
            candidate_path, stage, row_count=EXPECTED_ROW_COUNT
        ),
        "tail_resolution_ledger": artifact(
            ledger_path, stage, row_count=EXPECTED_AMBIGUOUS_COUNT
        ),
        "quality_summary": artifact(summary_path, stage),
        "verification": artifact(verification_path, stage),
        "provenance": artifact(provenance_path, stage),
        "report": artifact(report_path, stage),
    }
    content_hash = sha256_bytes(
        json.dumps(
            {
                "artifacts": artifacts,
                "metrics": metrics,
                "dictionary_checks": dictionary,
                "safety": summary["safety"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    manifest = {
        "schema_version": 2,
        "manifest_kind": "RESSET_STKSPCTRMT_ZERO_LOSS_CANDIDATE_V2",
        "dataset_id": "resset-stkspctrmt",
        "table": "STKSPCTRMT",
        "status": "ZERO_LOSS_V2_MATERIALIZED__INSPECTION_ONLY__NOT_ADMITTED",
        "created_at": utc_now(),
        "tool_id": TOOL_ID,
        "candidate_path": v2_root.as_posix(),
        "input_v1_root": v1_root.as_posix(),
        "metrics": metrics,
        "dictionary_checks": dictionary,
        "candidate_roundtrip": roundtrip,
        "artifacts": artifacts,
        "content_hash_algorithm": "sha256-canonical-json-resset-stkspctrmt-v2",
        "content_hash": content_hash,
        "safety": summary["safety"],
    }
    write_json(stage / "manifest.json", manifest)
    os.replace(stage, v2_root)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v1-root", type=Path, default=DEFAULT_V1_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_V2_ROOT)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="materialize the independent v2 inspection candidate",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = (
            execute(args.v1_root.resolve(), args.output_root.resolve())
            if args.execute
            else plan(args.v1_root.resolve(), args.output_root.resolve())
        )
    except (OSError, ValueError, KeyError, TailAuditError) as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL_CLOSED",
                    "write_performed": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
