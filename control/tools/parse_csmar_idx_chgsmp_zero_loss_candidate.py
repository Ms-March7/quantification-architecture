#!/usr/bin/env python3
"""Build the bound CSMAR IDX_Chgsmp zero-loss inspection candidate.

The reviewed export is a strict UTF-8-SIG CSV, but its mechanically unique
candidate key includes ``Chgsmp07`` while 804 source rows deliberately carry an
empty market code.  The generic local-download builder correctly refuses that
combination.  This adapter preserves the empty strings without filling,
dropping, or casting them, binds every source byte to a record receipt, writes
all Parquet columns as VARCHAR, and emits a row-level semantic-review queue.

The default operation is a complete write-free plan. ``--execute`` writes only
below ``tmp/csmar-idx-chgsmp-candidate``.  It never edits the source ZIP,
catalog, source registry, or roadmap.  All outputs remain
``INSPECTION_ONLY/NOT_ADMITTED``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import uuid
import zipfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TextIO


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATH = Path(__file__).with_name(
    "parse_csmar_bdt_zero_loss_candidate.py"
)
ENGINE_SPEC = importlib.util.spec_from_file_location(
    "_qdata_bdt_zero_loss_engine_for_idx_chgsmp", ENGINE_PATH
)
if ENGINE_SPEC is None or ENGINE_SPEC.loader is None:
    raise RuntimeError(f"cannot load zero-loss engine: {ENGINE_PATH}")
engine = importlib.util.module_from_spec(ENGINE_SPEC)
sys.modules[ENGINE_SPEC.name] = engine
ENGINE_SPEC.loader.exec_module(engine)


TOOL_ID = (
    "parse_csmar_idx_chgsmp_zero_loss_candidate.py/"
    "csmar-idx-chgsmp-zero-loss-candidate-v1"
)
PURPOSE_CEILING = "INSPECTION_ONLY"
ADMISSION_DECISION = "NOT_ADMITTED"
DATASET_ID = "csmar-idx-chgsmp-zero-loss"
TABLE_NAME = "idx_chgsmp_zero_loss_candidate"
OUTPUT_DIR_NAME = "csmar-idx-chgsmp-candidate"
EXPECTED_ZIP_NAME = (
    "指数取样变更文件094857265(仅供清华大学使用).zip"
)
EXPECTED_ZIP_SHA256 = (
    "a92786e597918636e6fb5390462fcd1fb3be7612c7bda8fe667756d2db682847"
)
EXPECTED_ZIP_BYTES = 3_181_332
CSV_MEMBER = "IDX_Chgsmp.csv"
DESCRIPTOR_MEMBER = "IDX_Chgsmp[DES][csv].txt"
COPYRIGHT_MEMBER = "版权声明.pdf"
EXPECTED_MEMBERS = {
    CSV_MEMBER: {
        "bytes": 22_494_442,
        "compressed_bytes": 2_965_271,
        "crc32": "7cc79b21",
        "sha256": (
            "6aae3a31fde4921af9eb38e1999100779f94ba22ac5acd2fdd9ed2c4458d9745"
        ),
    },
    DESCRIPTOR_MEMBER: {
        "bytes": 1_136,
        "compressed_bytes": 560,
        "crc32": "b2f259de",
        "sha256": (
            "7e761223a49aa9f34887b5472f4a5e3a6a787686c637406da2bc25edb2b2ec82"
        ),
    },
    COPYRIGHT_MEMBER: {
        "bytes": 227_624,
        "compressed_bytes": 215_143,
        "crc32": "244a292a",
        "sha256": (
            "efed0f0e29f5c89cd8847f49d6dd58d26c3cd4fd7ff22cf676a23072d78ccc66"
        ),
    },
}
CHGSMP_COLUMNS = (
    "Indexcd",
    "Chgsmp01",
    "Chgsmp02",
    "Chgsmp03",
    "Chgsmp04",
    "Chgsmp05",
    "Chgsmp06",
    "Chgsmp07",
)
MECHANICAL_KEY = (
    "Indexcd",
    "Chgsmp01",
    "Chgsmp02",
    "Chgsmp04",
    "Chgsmp05",
    "Chgsmp06",
    "Chgsmp07",
)
CHGSMP_SPEC = engine.TableSpec(
    name="IDX_Chgsmp",
    member_name=CSV_MEMBER,
    descriptor_member_name=DESCRIPTOR_MEMBER,
    dataset_id=DATASET_ID,
    table_name=TABLE_NAME,
    columns=CHGSMP_COLUMNS,
    key_columns=MECHANICAL_KEY,
    # Chgsmp07 is intentionally excluded: 804 reviewed source rows are blank.
    required_columns=CHGSMP_COLUMNS[:-1],
    date_columns=("Chgsmp01", "Chgsmp06"),
    binary_flag_columns=(),
    anchor=re.compile(
        rb'(?m)^"[A-Z0-9]+","\d{4}-\d{2}-\d{2}","'
    ),
    expected_record_count=279_744,
)
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
EXPECTED_SEMANTIC = {
    "logical_row_count": 279_744,
    "column_count": 8,
    "index_code_unique_count": 1_548,
    "constituent_code_unique_count": 35_786,
    "market_missing_count": 804,
    "future_effective_boundary_count": 122,
    "advance_announcement_count": 684,
    "maximum_announcement_lead_days": 1_113,
    "bond_record_count": 268_608,
    "bond_record_percentage": 96.0192,
    "quality_queue_unique_record_count": 1_488,
}
BOUNDARY_EFFECTIVE_DATE = "2029-06-15"


class IdxChgsmpError(RuntimeError):
    """A controlled, fail-closed IDX_Chgsmp candidate rejection."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(json_bytes(value))


def is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def require_safe_component(value: str, label: str) -> str:
    if not SAFE_COMPONENT.fullmatch(value):
        raise IdxChgsmpError(f"unsafe {label}: {value!r}")
    return value


def require_input_location(zip_path: Path, input_root: Path) -> Path:
    root = input_root.resolve(strict=True)
    resolved = zip_path.resolve(strict=True)
    if not root.is_dir():
        raise IdxChgsmpError(f"input root is not a directory: {root}")
    if not resolved.is_file() or resolved.suffix.casefold() != ".zip":
        raise IdxChgsmpError(f"source is not a ZIP file: {resolved}")
    if (
        zip_path.is_symlink()
        or is_reparse_point(zip_path)
        or not is_below(resolved, root)
    ):
        raise IdxChgsmpError(
            "source ZIP must be a non-reparse file below the explicit input root"
        )
    if resolved.name != EXPECTED_ZIP_NAME:
        raise IdxChgsmpError(
            f"source filename is not the reviewed export: {resolved.name!r}"
        )
    return resolved


def require_output_location(
    workspace_root: Path,
    output_root: Path | None,
    run_id: str,
) -> Path:
    workspace = workspace_root.resolve()
    allowed_base = (workspace / "tmp" / OUTPUT_DIR_NAME).resolve()
    base = allowed_base if output_root is None else output_root.resolve()
    if not is_below(base, allowed_base):
        raise IdxChgsmpError(
            f"output root must stay below {allowed_base.as_posix()}"
        )
    target = base / DATASET_ID / run_id
    if not is_below(target.resolve(), allowed_base):
        raise IdxChgsmpError("candidate target escaped its allowed tmp tree")
    return target


def catalog_hash(workspace_root: Path) -> str | None:
    path = workspace_root / "data" / "catalog" / "datasets.json"
    return sha256_file(path) if path.is_file() else None


def bound_source(
    zip_path: Path,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    package_hash = sha256_file(zip_path)
    if package_hash != EXPECTED_ZIP_SHA256:
        raise IdxChgsmpError(
            "source ZIP SHA-256 differs from the reviewed export: "
            f"{package_hash}"
        )
    if zip_path.stat().st_size != EXPECTED_ZIP_BYTES:
        raise IdxChgsmpError("source ZIP byte count differs from reviewed binding")
    with zipfile.ZipFile(zip_path) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise IdxChgsmpError(f"ZIP CRC failure: {corrupt!r}")
        members = engine.safe_zip_members(archive)
        if set(members) != set(EXPECTED_MEMBERS):
            raise IdxChgsmpError(
                "ZIP member set differs from reviewed export: "
                f"{sorted(members)!r}"
            )
        payloads: dict[str, bytes] = {}
        receipts: dict[str, Any] = {}
        for name, expected in EXPECTED_MEMBERS.items():
            info = members[name]
            payload = archive.read(info)
            receipt = {
                "bytes": len(payload),
                "compressed_bytes": info.compress_size,
                "crc32": f"{info.CRC:08x}",
                "encrypted": bool(info.flag_bits & 0x1),
                "sha256": sha256_bytes(payload),
            }
            for key in ("bytes", "compressed_bytes", "crc32", "sha256"):
                if receipt[key] != expected[key]:
                    raise IdxChgsmpError(
                        f"{name} {key} differs from reviewed binding: "
                        f"{receipt[key]!r}"
                    )
            if receipt["encrypted"]:
                raise IdxChgsmpError(f"encrypted member rejected: {name}")
            payloads[name] = payload
            receipts[name] = receipt
    return (
        {
            "zip_path": zip_path.as_posix(),
            "zip_filename": zip_path.name,
            "zip_bytes": zip_path.stat().st_size,
            "zip_sha256": package_hash,
            "zip_crc_read_status": "PASS",
            "zip_member_count": len(receipts),
            "members": receipts,
            "encoding": "utf-8-sig",
        },
        payloads,
    )


def row_quality_reasons(
    values: dict[str, str],
) -> tuple[list[str], int]:
    reasons: list[str] = []
    if values["Chgsmp07"] == "":
        reasons.append("SOURCE_MARKET_CODE_EMPTY")
    if values["Chgsmp01"] == BOUNDARY_EFFECTIVE_DATE:
        reasons.append("FUTURE_EFFECTIVE_DATE_AT_QUERY_BOUNDARY")
    effective = date.fromisoformat(values["Chgsmp01"])
    announced = date.fromisoformat(values["Chgsmp06"])
    lead_days = (effective - announced).days
    if lead_days < 0:
        reasons.append("ANNOUNCEMENT_AFTER_EFFECTIVE_DATE")
    elif lead_days > 0:
        reasons.append("ANNOUNCED_BEFORE_EFFECTIVE_DATE")
    return reasons, lead_days


def semantic_metrics(raw: bytes) -> dict[str, Any]:
    _, _, framed = engine.frame_records(raw, CHGSMP_SPEC)
    key_counts: Counter[tuple[str, ...]] = Counter()
    index_codes: set[str] = set()
    constituent_codes: set[str] = set()
    missing = Counter()
    security_types = Counter()
    market_codes = Counter()
    change_types = Counter()
    effective_dates: list[str] = []
    announcement_dates: list[str] = []
    reason_counts = Counter()
    queue_ordinals: set[int] = set()
    max_lead_days = 0
    strict_count = 0
    repaired_count = 0
    for record in framed:
        values, receipt, strict_valid = engine.parse_one_record(
            record, CHGSMP_SPEC
        )
        strict_count += int(strict_valid)
        repaired_count += int(receipt["parse_status"] != "CLEAN_STRICT")
        row = dict(zip(CHGSMP_COLUMNS, values, strict=True))
        key_counts[tuple(row[column] for column in MECHANICAL_KEY)] += 1
        index_codes.add(row["Indexcd"])
        constituent_codes.add(row["Chgsmp02"])
        for column, value in row.items():
            missing[column] += int(value == "")
        security_types[row["Chgsmp05"]] += 1
        market_codes[row["Chgsmp07"]] += 1
        change_types[row["Chgsmp04"]] += 1
        effective_dates.append(row["Chgsmp01"])
        announcement_dates.append(row["Chgsmp06"])
        reasons, lead_days = row_quality_reasons(row)
        max_lead_days = max(max_lead_days, lead_days)
        if reasons:
            queue_ordinals.add(record.ordinal)
            reason_counts.update(reasons)

    duplicate_groups = sum(count > 1 for count in key_counts.values())
    duplicate_rows = sum(
        count - 1 for count in key_counts.values() if count > 1
    )
    bond_count = security_types["3"]
    row_count = len(framed)
    result = {
        "logical_row_count": row_count,
        "column_count": len(CHGSMP_COLUMNS),
        "actual_field_selection": "8_OF_8",
        "strict_source_record_count": strict_count,
        "repaired_source_record_count": repaired_count,
        "candidate_key": list(MECHANICAL_KEY),
        "candidate_key_unique_count": len(key_counts),
        "candidate_key_duplicate_groups": duplicate_groups,
        "candidate_key_duplicate_rows": duplicate_rows,
        "candidate_key_status": (
            "MECHANICALLY_UNIQUE_ONLY__NOT_A_FORMAL_STABLE_KEY__"
            "CHGSMP07_EMPTY_STRING_PARTICIPATES_FOR_804_SOURCE_ROWS"
        ),
        "index_code_unique_count": len(index_codes),
        "constituent_code_unique_count": len(constituent_codes),
        "effective_date_min": min(effective_dates),
        "effective_date_max": max(effective_dates),
        "announcement_date_min": min(announcement_dates),
        "announcement_date_max": max(announcement_dates),
        "missing_field_counts": {
            column: missing[column] for column in CHGSMP_COLUMNS
        },
        "market_missing_count": missing["Chgsmp07"],
        "future_effective_boundary_date": BOUNDARY_EFFECTIVE_DATE,
        "future_effective_boundary_count": reason_counts[
            "FUTURE_EFFECTIVE_DATE_AT_QUERY_BOUNDARY"
        ],
        "advance_announcement_count": reason_counts[
            "ANNOUNCED_BEFORE_EFFECTIVE_DATE"
        ],
        "announcement_after_effective_count": reason_counts[
            "ANNOUNCEMENT_AFTER_EFFECTIVE_DATE"
        ],
        "maximum_announcement_lead_days": max_lead_days,
        "bond_record_count": bond_count,
        "bond_record_percentage": round(100.0 * bond_count / row_count, 4),
        "security_type_counts": dict(sorted(security_types.items())),
        "market_code_counts": dict(sorted(market_codes.items())),
        "change_type_counts": dict(sorted(change_types.items())),
        "quality_queue_reason_counts": dict(sorted(reason_counts.items())),
        "quality_queue_unique_record_count": len(queue_ordinals),
        "empty_market_preservation": (
            "SOURCE_EMPTY_STRING_RETAINED__NO_FILL__NO_DROP__NO_NULL_CAST"
        ),
    }
    for key, expected in EXPECTED_SEMANTIC.items():
        if result[key] != expected:
            raise IdxChgsmpError(
                f"reviewed semantic invariant changed for {key}: "
                f"{result[key]!r} != {expected!r}"
            )
    if result["announcement_after_effective_count"] != 0:
        raise IdxChgsmpError("announcement-after-effective rows were observed")
    if duplicate_rows:
        raise IdxChgsmpError("reviewed mechanical key is not unique")
    return result


class SidecarQueueAugmenter:
    """Augment every record receipt and list all actionable queue records."""

    def __init__(self, sidecar: TextIO, queue: TextIO):
        self.sidecar = sidecar
        self.queue = queue
        self.rows = 0
        self.queue_rows = 0
        self.reason_counts: Counter[str] = Counter()
        self.max_lead_days = 0

    def write(self, value: str) -> int:
        if not value.endswith("\n") or value.count("\n") != 1:
            raise IdxChgsmpError("unexpected shared sidecar write boundary")
        receipt = json.loads(value)
        key = {name: str(receipt["key"][name]) for name in MECHANICAL_KEY}
        reasons, lead_days = row_quality_reasons(key)
        receipt["source_market_code_is_empty"] = key["Chgsmp07"] == ""
        receipt["source_empty_market_preserved_as_empty_string"] = (
            key["Chgsmp07"] == ""
        )
        receipt["announcement_lead_days"] = lead_days
        receipt["quality_queue_reasons"] = reasons
        receipt["formal_stable_key_asserted"] = False
        self.rows += 1
        self.max_lead_days = max(self.max_lead_days, lead_days)
        self.reason_counts.update(reasons)
        if reasons:
            queue_item = {
                "record_ordinal": receipt["record_ordinal"],
                "source_start_byte": receipt["source_start_byte"],
                "source_end_byte": receipt["source_end_byte"],
                "source_record_sha256": receipt["source_record_sha256"],
                "key": key,
                "announcement_lead_days": lead_days,
                "quality_queue_reasons": reasons,
                "source_value_modified": False,
            }
            self.queue.write(
                json.dumps(
                    queue_item,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            )
            self.queue_rows += 1
        rendered = (
            json.dumps(
                receipt,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
        return self.sidecar.write(rendered)


def artifact(
    path: Path,
    root: Path,
    *,
    row_count: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "relative_path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if row_count is not None:
        result["row_count"] = row_count
    return result


def validate_parser_result(result: dict[str, Any]) -> None:
    metrics = result["metrics"]
    checks = {
        "output_record_count": 279_744,
        "strict_valid_record_count": 279_744,
        "repaired_record_count": 0,
        "duplicate_key_count": 0,
        "unaccounted_source_bytes": 0,
    }
    for key, expected in checks.items():
        if metrics[key] != expected:
            raise IdxChgsmpError(
                f"parser invariant changed for {key}: "
                f"{metrics[key]!r} != {expected!r}"
            )
    if metrics["accounted_source_bytes"] != metrics["source_csv_bytes"]:
        raise IdxChgsmpError("CSV byte attribution is incomplete")


def quality_summary(
    parsed: dict[str, Any],
    semantic: dict[str, Any],
    parquet_roundtrip: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mechanical_status": "PASS_ZERO_LOSS_NO_REPAIRS_REQUIRED",
        "table": "IDX_Chgsmp",
        "dataset_id": DATASET_ID,
        "parser_metrics": parsed["metrics"],
        "semantic_metrics": semantic,
        "parquet_roundtrip": parquet_roundtrip,
        "semantic_status": "QUARANTINE_REVIEW_REQUIRED",
        "quality_queues": {
            "source_market_code_empty": {
                "row_count": semantic["market_missing_count"],
                "handling": "PRESERVE_EMPTY_STRING__DO_NOT_FILL_OR_DROP",
            },
            "future_effective_date_at_query_boundary": {
                "date": BOUNDARY_EFFECTIVE_DATE,
                "row_count": semantic["future_effective_boundary_count"],
                "handling": "QUARANTINE__DO_NOT_ACTIVATE_MEMBERSHIP",
            },
            "announced_before_effective_date": {
                "row_count": semantic["advance_announcement_count"],
                "maximum_lead_days": semantic[
                    "maximum_announcement_lead_days"
                ],
                "handling": "PRESERVE_DUAL_CLOCKS__NO_EARLY_MEMBERSHIP",
            },
            "cross_asset_bond_dominance": {
                "bond_row_count": semantic["bond_record_count"],
                "bond_percentage": semantic["bond_record_percentage"],
                "handling": "COHORT_STATISTIC__NOT_AN_A_SHARE_REPRESENTATIVE_SAMPLE",
            },
        },
        "pit_blockers": [
            "Chgsmp06 is labelled announcement date but first-publication status "
            "is not established.",
            "Revision, withdrawal, cancellation and supersedes chains are absent.",
            "The 122 records effective at the exact 2029-06-15 query boundary "
            "require provider or official-index verification.",
            "Membership must not become effective before Chgsmp01 even when "
            "Chgsmp06 is earlier.",
            "Constituent weights and independently versioned methodology are "
            "not present in this table.",
        ],
        "mapping": {
            "candidate": "CORE-10/cn-index-constituents-weights-pit",
            "candidate_scope": (
                "STRUCTURE_AND_PARSER_SAMPLE_ONLY__NOT_COMPLETE_HISTORY__"
                "NOT_A_SHARE_REPRESENTATIVE"
            ),
        },
        "license_status": (
            "UNRESOLVED__PACKAGE_COPYRIGHT_NOTICE_BOUND__INSTITUTIONAL_"
            "CONTRACT_SCOPE_NOT_IN_PACKAGE"
        ),
        "purpose_ceiling": PURPOSE_CEILING,
        "admission_decision": ADMISSION_DECISION,
        "catalog_registered": False,
        "source_admission": False,
        "quality_promotion": False,
        "research_ready": False,
    }


def run(
    *,
    workspace_root: Path,
    zip_path: Path,
    input_root: Path,
    run_id: str,
    output_root: Path | None,
    execute: bool,
) -> dict[str, Any]:
    workspace = workspace_root.resolve()
    require_safe_component(run_id, "run_id")
    source_zip = require_input_location(zip_path, input_root)
    target = require_output_location(workspace, output_root, run_id)
    before_catalog = catalog_hash(workspace)
    source, payloads = bound_source(source_zip)
    parsed = engine.parse_archive(
        zip_path=source_zip,
        spec=CHGSMP_SPEC,
        sinks=None,
    )
    validate_parser_result(parsed)
    semantic = semantic_metrics(payloads[CSV_MEMBER])

    if not execute:
        result = {
            "schema_version": 1,
            "manifest_kind": "CSMAR_IDX_CHGSMP_ZERO_LOSS_PLAN",
            "tool_id": TOOL_ID,
            "status": "PLAN_COMPLETE__NO_WRITES",
            "write_performed": False,
            "candidate_path": target.as_posix(),
            "source": source,
            "parser_metrics": parsed["metrics"],
            "semantic_metrics": semantic,
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
            "catalog_sha256_before": before_catalog,
            "catalog_sha256_after": catalog_hash(workspace),
        }
        if result["catalog_sha256_after"] != before_catalog:
            raise IdxChgsmpError("catalog changed during read-only plan")
        return result

    if target.exists():
        raise IdxChgsmpError(f"candidate target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{run_id}.partial-{uuid.uuid4().hex}"
    stage.mkdir()
    try:
        raw_dir = stage / "raw"
        extracted_dir = raw_dir / "extracted"
        canonical_dir = stage / "canonical"
        receipt_dir = stage / "receipts"
        curated_dir = stage / "curated"
        quality_dir = stage / "quality"
        for directory in (
            raw_dir,
            extracted_dir,
            canonical_dir,
            receipt_dir,
            curated_dir,
            quality_dir,
        ):
            directory.mkdir(exist_ok=True)

        raw_copy = raw_dir / source_zip.name
        shutil.copyfile(source_zip, raw_copy)
        if sha256_file(raw_copy) != EXPECTED_ZIP_SHA256:
            raise IdxChgsmpError("raw ZIP copy hash mismatch")
        extracted: dict[str, Path] = {}
        for name, payload in payloads.items():
            path = extracted_dir / name
            path.write_bytes(payload)
            if sha256_file(path) != EXPECTED_MEMBERS[name]["sha256"]:
                raise IdxChgsmpError(f"extracted member hash mismatch: {name}")
            extracted[name] = path

        canonical_csv = canonical_dir / CSV_MEMBER
        sidecar = receipt_dir / "records.jsonl"
        queue_path = quality_dir / "semantic-review-queue.jsonl"
        with canonical_csv.open(
            "x", encoding="utf-8-sig", errors="strict", newline=""
        ) as csv_file, sidecar.open(
            "x", encoding="utf-8", errors="strict", newline="\n"
        ) as sidecar_file, queue_path.open(
            "x", encoding="utf-8", errors="strict", newline="\n"
        ) as queue_file:
            augmenter = SidecarQueueAugmenter(sidecar_file, queue_file)
            parsed = engine.parse_archive(
                zip_path=source_zip,
                spec=CHGSMP_SPEC,
                sinks=engine.OutputSinks(
                    csv_file=csv_file,
                    sidecar_file=augmenter,
                ),
            )
        validate_parser_result(parsed)
        if augmenter.rows != EXPECTED_SEMANTIC["logical_row_count"]:
            raise IdxChgsmpError("sidecar row count differs from source")
        if (
            augmenter.queue_rows
            != EXPECTED_SEMANTIC["quality_queue_unique_record_count"]
        ):
            raise IdxChgsmpError("semantic queue row count differs from review")
        expected_reason_counts = {
            "SOURCE_MARKET_CODE_EMPTY": 804,
            "FUTURE_EFFECTIVE_DATE_AT_QUERY_BOUNDARY": 122,
            "ANNOUNCED_BEFORE_EFFECTIVE_DATE": 684,
        }
        if dict(augmenter.reason_counts) != expected_reason_counts:
            raise IdxChgsmpError("semantic queue reason counts changed")
        if augmenter.max_lead_days != 1_113:
            raise IdxChgsmpError("semantic queue maximum lead changed")

        parquet_path = curated_dir / "part-000.parquet"
        parquet_roundtrip = engine.build_and_verify_parquet(
            repaired_csv=canonical_csv,
            parquet_path=parquet_path,
            spec=CHGSMP_SPEC,
        )
        if parquet_roundtrip["csv_row_count"] != 279_744:
            raise IdxChgsmpError("Parquet row count differs from reviewed export")

        query_sql = stage / "query.sql"
        final_parquet = (
            target / "curated" / "part-000.parquet"
        ).as_posix()
        query_sql.write_text(
            "-- INSPECTION_ONLY/NOT_ADMITTED; not a research input.\n"
            f"CREATE OR REPLACE VIEW {engine.sql_identifier(TABLE_NAME)} AS\n"
            "SELECT * FROM read_parquet("
            f"{engine.sql_string(final_parquet)});\n",
            encoding="utf-8",
            newline="\n",
        )

        quality = quality_summary(parsed, semantic, parquet_roundtrip)
        quality_path = quality_dir / "summary.json"
        write_json(quality_path, quality)
        source_binding_path = raw_dir / "source-binding.json"
        write_json(
            source_binding_path,
            {
                "schema_version": 1,
                "source": source,
                "reviewed_binding": {
                    "zip_name": EXPECTED_ZIP_NAME,
                    "zip_bytes": EXPECTED_ZIP_BYTES,
                    "zip_sha256": EXPECTED_ZIP_SHA256,
                    "members": EXPECTED_MEMBERS,
                },
                "source_zip_modified": False,
                "catalog_write_authorized": False,
            },
        )

        parser_path = Path(__file__).resolve()
        parser_hash = sha256_file(parser_path)
        engine_hash = sha256_file(ENGINE_PATH)
        row_count = parsed["metrics"]["output_record_count"]
        outputs = {
            "raw_zip": artifact(raw_copy, stage),
            "raw_csv_member": artifact(extracted[CSV_MEMBER], stage),
            "raw_descriptor_member": artifact(
                extracted[DESCRIPTOR_MEMBER], stage
            ),
            "raw_copyright_member": artifact(
                extracted[COPYRIGHT_MEMBER], stage
            ),
            "source_binding": artifact(source_binding_path, stage),
            "canonical_csv": artifact(
                canonical_csv, stage, row_count=row_count
            ),
            "record_sidecar": artifact(
                sidecar, stage, row_count=row_count
            ),
            "semantic_review_queue": artifact(
                queue_path,
                stage,
                row_count=augmenter.queue_rows,
            ),
            "parquet": artifact(
                parquet_path, stage, row_count=row_count
            ),
            "quality_summary": artifact(quality_path, stage),
            "query_sql": artifact(query_sql, stage),
        }
        after_catalog = catalog_hash(workspace)
        if after_catalog != before_catalog:
            raise IdxChgsmpError("catalog changed during candidate execution")
        content_binding = {
            "tool_id": TOOL_ID,
            "dataset_id": DATASET_ID,
            "run_id": run_id,
            "source": source,
            "parser_script_sha256": parser_hash,
            "shared_engine_sha256": engine_hash,
            "outputs": outputs,
            "parser_metrics": parsed["metrics"],
            "semantic_metrics": semantic,
            "parquet_roundtrip": parquet_roundtrip,
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
        }
        result = {
            "schema_version": 1,
            "manifest_kind": "CSMAR_IDX_CHGSMP_ZERO_LOSS_CANDIDATE",
            "tool_id": TOOL_ID,
            "status": "ZERO_LOSS_CANDIDATE_MATERIALIZED",
            "write_performed": True,
            "created_at": utc_now(),
            "dataset_id": DATASET_ID,
            "table": "IDX_Chgsmp",
            "run_id": run_id,
            "candidate_path": target.as_posix(),
            "source": source,
            "parser": {
                "path": parser_path.as_posix(),
                "sha256": parser_hash,
                "shared_engine_path": ENGINE_PATH.resolve().as_posix(),
                "shared_engine_sha256": engine_hash,
            },
            "outputs": outputs,
            "parser_metrics": parsed["metrics"],
            "semantic_metrics": semantic,
            "parquet_roundtrip": parquet_roundtrip,
            "content_hash_algorithm": (
                "sha256-canonical-json-csmar-idx-chgsmp-zero-loss-v1"
            ),
            "content_hash": sha256_bytes(
                canonical_json_bytes(content_binding)
            ),
            "catalog_guard": {
                "path": (
                    workspace / "data" / "catalog" / "datasets.json"
                ).as_posix(),
                "sha256_before": before_catalog,
                "sha256_after": after_catalog,
                "write_authorized": False,
            },
            "safety": {
                "purpose_ceiling": PURPOSE_CEILING,
                "admission_decision": ADMISSION_DECISION,
                "catalog_registered": False,
                "source_admission": False,
                "quality_promotion": False,
                "research_ready": False,
                "source_zip_modified": False,
            },
        }
        write_json(stage / "manifest.json", result)
        if catalog_hash(workspace) != before_catalog:
            raise IdxChgsmpError("catalog changed before candidate seal")
        os.replace(stage, target)
        return result
    except Exception:
        allowed_base = (
            workspace / "tmp" / OUTPUT_DIR_NAME
        ).resolve()
        if stage.exists() and is_below(stage.resolve(), allowed_base):
            shutil.rmtree(stage)
        raise


def default_downloads() -> Path:
    return Path.home() / "Downloads"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or materialize the bound zero-loss CSMAR IDX_Chgsmp "
            "inspection candidate."
        )
    )
    parser.add_argument("--zip", required=True, type=Path, dest="zip_path")
    parser.add_argument(
        "--input-root",
        type=Path,
        default=default_downloads(),
        help="explicit local inbox root (default: current user's Downloads)",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        action="store_true",
        help="perform the complete write-free plan (default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="materialize INSPECTION_ONLY/NOT_ADMITTED output below tmp",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(
            workspace_root=WORKSPACE_ROOT,
            zip_path=args.zip_path,
            input_root=args.input_root,
            run_id=args.run_id,
            output_root=args.output_root,
            execute=bool(args.execute),
        )
    except (
        IdxChgsmpError,
        engine.BdtZeroLossError,
        OSError,
        UnicodeError,
        zipfile.BadZipFile,
    ) as exc:
        print(
            json.dumps(
                {"status": "REJECTED", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
