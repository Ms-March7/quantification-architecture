#!/usr/bin/env python3
"""Build the bound CSMAR IDX_Idxinfo zero-loss inspection candidate.

The default operation is a complete, write-free plan. ``--execute`` writes
only below ``tmp/csmar-idx-zero-loss-candidate``. It never edits the source
ZIP, formal catalog, source registry, or roadmap.

This adapter is intentionally bound to the reviewed 2026-07-30 manual export.
It reuses the reversible record framing and quote repair engine already used
for malformed CSMAR BDT text fields, while adding IDX-specific source hashes,
semantic checks, PIT/license blockers, raw member preservation, and one
newline/quote evidence object per logical source record.

All outputs remain ``INSPECTION_ONLY/NOT_ADMITTED``.
"""

from __future__ import annotations

import argparse
import csv
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
    "_qdata_bdt_zero_loss_engine", ENGINE_PATH
)
if ENGINE_SPEC is None or ENGINE_SPEC.loader is None:
    raise RuntimeError(f"cannot load zero-loss engine: {ENGINE_PATH}")
engine = importlib.util.module_from_spec(ENGINE_SPEC)
sys.modules[ENGINE_SPEC.name] = engine
ENGINE_SPEC.loader.exec_module(engine)


TOOL_ID = (
    "parse_csmar_idx_idxinfo_zero_loss_candidate.py/"
    "csmar-idx-idxinfo-zero-loss-candidate-v1"
)
PURPOSE_CEILING = "INSPECTION_ONLY"
ADMISSION_DECISION = "NOT_ADMITTED"
DATASET_ID = "csmar-idx-idxinfo-zero-loss"
TABLE_NAME = "idx_idxinfo_zero_loss_candidate"
EXPECTED_ZIP_NAME = "指数基本信息文件011942405(仅供清华大学使用).zip"
EXPECTED_ZIP_SHA256 = (
    "5d678e2e4d7213d9c072c88e61f0e32c921616b2f398e120f3bcaa270705a1b9"
)
CSV_MEMBER = "IDX_Idxinfo.csv"
DESCRIPTOR_MEMBER = "IDX_Idxinfo[DES][csv].txt"
COPYRIGHT_MEMBER = "版权声明.pdf"
EXPECTED_MEMBERS = {
    CSV_MEMBER: {
        "sha256": (
            "514e0889799f309a913acc89cbc5dc9f0ae7fad019f2fc43d8f403d2b75d7e9b"
        ),
        "bytes": 5_462_874,
        "crc32": "d1b33cc7",
    },
    DESCRIPTOR_MEMBER: {
        "sha256": (
            "7f60e84e306f9232baf173219b3db1e4ac6b5a4e751cbc5619a286a451cd73f1"
        ),
        "bytes": 1_030,
        "crc32": "5f4ad308",
    },
    COPYRIGHT_MEMBER: {
        "sha256": (
            "efed0f0e29f5c89cd8847f49d6dd58d26c3cd4fd7ff22cf676a23072d78ccc66"
        ),
        "bytes": 227_624,
        "crc32": "244a292a",
    },
}
IDX_COLUMNS = (
    "Indexcd",
    "Idxinfo01",
    "Idxinfo11",
    "Idxinfo02",
    "Idxinfo03",
    "Idxinfo04",
    "Idxinfo05",
    "Idxinfo06",
    "Idxinfo07",
    "Idxinfo08",
    "Idxinfo09",
    "Idxinfo10",
)
IDX_SPEC = engine.TableSpec(
    name="IDX_Idxinfo",
    member_name=CSV_MEMBER,
    descriptor_member_name=DESCRIPTOR_MEMBER,
    dataset_id=DATASET_ID,
    table_name=TABLE_NAME,
    columns=IDX_COLUMNS,
    key_columns=("Indexcd", "Idxinfo11"),
    required_columns=("Indexcd", "Idxinfo01", "Idxinfo11"),
    # Idxinfo02 is nullable, so its validity is checked separately.
    date_columns=("Idxinfo11",),
    binary_flag_columns=(),
    anchor=re.compile(
        rb'(?m)^"[A-Za-z0-9.]+","[^"\r\n]*",'
        rb'"\d{4}-\d{2}-\d{2}","'
    ),
    expected_record_count=5_574,
)
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")


class IdxZeroLossError(RuntimeError):
    """A controlled, fail-closed IDX candidate rejection."""


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
        raise IdxZeroLossError(f"unsafe {label}: {value!r}")
    return value


def require_input_location(zip_path: Path, input_root: Path) -> Path:
    root = input_root.resolve(strict=True)
    resolved = zip_path.resolve(strict=True)
    if not root.is_dir():
        raise IdxZeroLossError(f"input root is not a directory: {root}")
    if not resolved.is_file() or resolved.suffix.casefold() != ".zip":
        raise IdxZeroLossError(f"source is not a ZIP file: {resolved}")
    if (
        zip_path.is_symlink()
        or is_reparse_point(zip_path)
        or not is_below(resolved, root)
    ):
        raise IdxZeroLossError(
            "source ZIP must be a non-reparse file below the explicit input root"
        )
    if resolved.name != EXPECTED_ZIP_NAME:
        raise IdxZeroLossError(
            f"source filename is not the reviewed export: {resolved.name!r}"
        )
    return resolved


def require_output_location(
    workspace_root: Path,
    output_root: Path | None,
    run_id: str,
) -> Path:
    workspace = workspace_root.resolve()
    tmp_root = (workspace / "tmp").resolve()
    base = (
        tmp_root / "csmar-idx-zero-loss-candidate"
        if output_root is None
        else output_root.resolve()
    )
    if not is_below(base, tmp_root):
        raise IdxZeroLossError("output root must stay below the workspace tmp tree")
    target = base / DATASET_ID / run_id
    if not is_below(target.resolve(), tmp_root):
        raise IdxZeroLossError("candidate target escaped the workspace tmp tree")
    return target


def catalog_hash(workspace_root: Path) -> str | None:
    path = workspace_root / "data" / "catalog" / "datasets.json"
    return sha256_file(path) if path.is_file() else None


def bound_source(zip_path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    package_hash = sha256_file(zip_path)
    if package_hash != EXPECTED_ZIP_SHA256:
        raise IdxZeroLossError(
            "source ZIP SHA-256 differs from the reviewed export: "
            f"{package_hash}"
        )
    with zipfile.ZipFile(zip_path) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise IdxZeroLossError(f"ZIP CRC failure: {corrupt!r}")
        members = engine.safe_zip_members(archive)
        if set(members) != set(EXPECTED_MEMBERS):
            raise IdxZeroLossError(
                "ZIP member set differs from the reviewed export: "
                f"{sorted(members)!r}"
            )
        payloads: dict[str, bytes] = {}
        member_receipts: dict[str, Any] = {}
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
            for key in ("bytes", "crc32", "sha256"):
                if receipt[key] != expected[key]:
                    raise IdxZeroLossError(
                        f"{name} {key} differs from reviewed binding: "
                        f"{receipt[key]!r}"
                    )
            if receipt["encrypted"]:
                raise IdxZeroLossError(f"encrypted member rejected: {name}")
            payloads[name] = payload
            member_receipts[name] = receipt
    return (
        {
            "zip_path": zip_path.as_posix(),
            "zip_filename": zip_path.name,
            "zip_bytes": zip_path.stat().st_size,
            "zip_sha256": package_hash,
            "zip_crc_read_status": "PASS",
            "zip_member_count": len(member_receipts),
            "members": member_receipts,
            "encoding": "utf-8-sig",
        },
        payloads,
    )


def newline_metrics(value: bytes) -> dict[str, int]:
    crlf = value.count(b"\r\n")
    cr = value.count(b"\r")
    lf = value.count(b"\n")
    return {
        "crlf": crlf,
        "lone_cr": cr - crlf,
        "lone_lf": lf - crlf,
        "terminators": cr + lf - crlf,
    }


class SidecarAugmenter:
    """Add per-record newline and quote evidence to the shared sidecar."""

    def __init__(self, target: TextIO, raw: bytes):
        self.target = target
        self.raw = raw
        self.rows = 0
        self.newline_rows = 0
        self.quote_rows = 0

    def write(self, value: str) -> int:
        if not value.endswith("\n") or value.count("\n") != 1:
            raise IdxZeroLossError("unexpected shared sidecar write boundary")
        receipt = json.loads(value)
        start = int(receipt["source_start_byte"])
        end = int(receipt["source_end_byte"])
        region = self.raw[start:end]
        separator = bytes.fromhex(receipt["source_separator_hex"])
        if separator:
            if not region.endswith(separator):
                raise IdxZeroLossError("sidecar separator does not bind source")
            body = region[: -len(separator)]
        else:
            body = region
        lines = newline_metrics(body)
        quote_actions = [
            action
            for action in receipt["actions"]
            if action["action"] == "INSERT_QUOTE_TO_ESCAPE_UNESCAPED_QUOTE"
        ]
        receipt["source_body_newlines"] = lines
        receipt["source_contains_embedded_newline"] = bool(lines["terminators"])
        receipt["source_contains_bare_quote"] = bool(quote_actions)
        receipt["bare_quote_actions"] = quote_actions
        receipt["source_literal_replacement_character_count"] = (
            body.decode("utf-8", errors="strict").count("\ufffd")
        )
        self.rows += 1
        self.newline_rows += int(receipt["source_contains_embedded_newline"])
        self.quote_rows += int(receipt["source_contains_bare_quote"])
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
        return self.target.write(rendered)


def semantic_metrics(raw: bytes) -> dict[str, Any]:
    _, _, framed = engine.frame_records(raw, IDX_SPEC)
    rows: list[list[str]] = []
    newline_rows = 0
    bare_quote_rows = 0
    literal_replacement_characters = 0
    for record in framed:
        values, receipt, _ = engine.parse_one_record(record, IDX_SPEC)
        rows.append(values)
        body_lines = newline_metrics(record.body)
        newline_rows += int(body_lines["terminators"] > 0)
        bare_quote_rows += int(receipt["quote_repair_count"] > 0)
        literal_replacement_characters += sum(
            value.count("\ufffd") for value in values
        )

    key_counts = Counter((row[0], row[2]) for row in rows)
    code_counts = Counter(row[0] for row in rows)
    duplicate_codes = {
        code: count for code, count in code_counts.items() if count > 1
    }
    base_dates = [row[3] for row in rows if row[3]]
    invalid_base_dates: list[str] = []
    for value in base_dates:
        try:
            date.fromisoformat(value)
        except ValueError:
            invalid_base_dates.append(value)
    if invalid_base_dates:
        raise IdxZeroLossError(
            f"invalid nonblank base dates: {invalid_base_dates[:10]!r}"
        )
    allowed_codes = {
        "Idxinfo05": {"", "1", "2", "3", "4"},
        "Idxinfo06": {"", "1", "2", "3", "4"},
        "Idxinfo07": {"", "1", "2", "3", "4", "5", "6", "7"},
        "Idxinfo08": {"", "1", "2", "3", "4", "5"},
        "Idxinfo09": {"", "1", "2", "3", "4", "5", "6", "7", "8", "9"},
    }
    undefined: dict[str, dict[str, int]] = {}
    for field, allowed in allowed_codes.items():
        index = IDX_COLUMNS.index(field)
        counts = Counter(
            row[index] for row in rows if row[index] not in allowed
        )
        undefined[field] = dict(sorted(counts.items()))
    missing = {
        field: sum(row[index] == "" for row in rows)
        for index, field in enumerate(IDX_COLUMNS)
    }
    return {
        "logical_row_count": len(rows),
        "column_count": len(IDX_COLUMNS),
        "candidate_key": ["Indexcd", "Idxinfo11"],
        "candidate_key_unique_count": len(key_counts),
        "candidate_key_duplicate_groups": sum(
            count > 1 for count in key_counts.values()
        ),
        "candidate_key_duplicate_rows": sum(
            count - 1 for count in key_counts.values() if count > 1
        ),
        "index_code_unique_count": len(code_counts),
        "index_code_duplicate_groups": duplicate_codes,
        "launch_date_min": min(row[2] for row in rows),
        "launch_date_max": max(row[2] for row in rows),
        "base_date_nonblank_count": len(base_dates),
        "base_date_blank_count": len(rows) - len(base_dates),
        "base_date_min": min(base_dates),
        "base_date_max": max(base_dates),
        "embedded_newline_record_count": newline_rows,
        "bare_quote_record_count": bare_quote_rows,
        "literal_replacement_character_count": literal_replacement_characters,
        "missing_field_counts": missing,
        "undefined_descriptor_code_counts": undefined,
    }


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


def quality_summary(
    result: dict[str, Any],
    parquet_roundtrip: dict[str, Any],
    semantic: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mechanical_status": (
            "PASS_ZERO_LOSS_WITH_REVERSIBLE_QUOTE_REPAIRS"
        ),
        "table": "IDX_Idxinfo",
        "dataset_id": DATASET_ID,
        "metrics": result["metrics"],
        "semantic_metrics": semantic,
        "parquet_roundtrip": parquet_roundtrip,
        "semantic_status": "REVIEW_REQUIRED",
        "semantic_findings": [
            "Indexcd is not globally unique; 980001 and 980002 are each reused.",
            "Descriptor codes are stale: Idxinfo05 contains 5, 6 and 5;3; "
            "Idxinfo06 and Idxinfo09 contain undocumented code 10.",
            "One source narrative contains 14 literal U+FFFD replacement "
            "characters.",
            "Start/base dates are not knowledge or availability times.",
        ],
        "pit_blockers": [
            "provider_id is not fully resolved",
            "return_variant and currency are absent",
            "methodology_version and revision chain are absent",
            "methodology publication/effective timestamps are absent",
            "available_at and valid_from/valid_to are absent",
            "termination date and historical identity changes are absent",
        ],
        "license_status": (
            "UNRESOLVED__PACKAGE_COPYRIGHT_NOTICE_REQUIRES_WRITTEN_"
            "AUTHORIZATION__INSTITUTIONAL_CONTRACT_SCOPE_NOT_IN_PACKAGE"
        ),
        "mapping": {
            "primary_candidate": "CORE-09/cn-index-master",
            "partial_secondary_candidate": "GCTX-02/global-benchmark-master",
            "does_not_satisfy": [
                "CORE-10/cn-index-constituents-weights-pit",
                "CORE-12/cn-index-daily",
                "GCTX-03/global-benchmark-daily",
            ],
        },
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
        spec=IDX_SPEC,
        sinks=None,
    )
    semantic = semantic_metrics(payloads[CSV_MEMBER])
    if semantic["candidate_key_duplicate_rows"] != 0:
        raise IdxZeroLossError("reviewed candidate key is not unique")
    if parsed["metrics"]["unaccounted_source_bytes"] != 0:
        raise IdxZeroLossError("source byte attribution is incomplete")

    if not execute:
        result = {
            "schema_version": 1,
            "manifest_kind": "CSMAR_IDX_IDXINFO_ZERO_LOSS_PLAN",
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
            raise IdxZeroLossError("catalog changed during read-only plan")
        return result

    if target.exists():
        raise IdxZeroLossError(f"candidate target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{run_id}.partial-{uuid.uuid4().hex}"
    stage.mkdir()
    try:
        raw_dir = stage / "raw"
        extracted_dir = raw_dir / "extracted"
        repaired_dir = stage / "repaired"
        repair_dir = stage / "repair"
        curated_dir = stage / "curated"
        quality_dir = stage / "quality"
        for directory in (
            raw_dir,
            extracted_dir,
            repaired_dir,
            repair_dir,
            curated_dir,
            quality_dir,
        ):
            directory.mkdir(exist_ok=True)

        raw_copy = raw_dir / source_zip.name
        shutil.copyfile(source_zip, raw_copy)
        if sha256_file(raw_copy) != EXPECTED_ZIP_SHA256:
            raise IdxZeroLossError("raw ZIP copy hash mismatch")
        extracted: dict[str, Path] = {}
        for name, payload in payloads.items():
            path = extracted_dir / name
            path.write_bytes(payload)
            if sha256_file(path) != EXPECTED_MEMBERS[name]["sha256"]:
                raise IdxZeroLossError(f"extracted member hash mismatch: {name}")
            extracted[name] = path

        repaired_csv = repaired_dir / CSV_MEMBER
        sidecar = repair_dir / "records.jsonl"
        with repaired_csv.open(
            "x", encoding="utf-8-sig", errors="strict", newline=""
        ) as csv_file, sidecar.open(
            "x", encoding="utf-8", errors="strict", newline="\n"
        ) as sidecar_file:
            augmented = SidecarAugmenter(
                sidecar_file,
                payloads[CSV_MEMBER],
            )
            parsed = engine.parse_archive(
                zip_path=source_zip,
                spec=IDX_SPEC,
                sinks=engine.OutputSinks(
                    csv_file=csv_file,
                    sidecar_file=augmented,
                ),
            )
        if augmented.rows != 5_574:
            raise IdxZeroLossError("sidecar row count differs from source")
        if augmented.newline_rows != semantic["embedded_newline_record_count"]:
            raise IdxZeroLossError("sidecar newline evidence count mismatch")
        if augmented.quote_rows != semantic["bare_quote_record_count"]:
            raise IdxZeroLossError("sidecar quote evidence count mismatch")

        parquet_path = curated_dir / "part-000.parquet"
        parquet_roundtrip = engine.build_and_verify_parquet(
            repaired_csv=repaired_csv,
            parquet_path=parquet_path,
            spec=IDX_SPEC,
        )
        if parquet_roundtrip["csv_row_count"] != 5_574:
            raise IdxZeroLossError("Parquet row count differs from reviewed export")

        query_sql = stage / "query.sql"
        final_parquet_path = (
            target / "curated" / "part-000.parquet"
        ).as_posix()
        query_sql.write_text(
            "-- INSPECTION_ONLY/NOT_ADMITTED; not a research input.\n"
            f"CREATE OR REPLACE VIEW {engine.sql_identifier(TABLE_NAME)} AS\n"
            "SELECT * FROM read_parquet("
            f"{engine.sql_string(final_parquet_path)});\n",
            encoding="utf-8",
            newline="\n",
        )

        quality = quality_summary(parsed, parquet_roundtrip, semantic)
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
            "repaired_csv": artifact(
                repaired_csv, stage, row_count=row_count
            ),
            "record_sidecar": artifact(
                sidecar, stage, row_count=row_count
            ),
            "parquet": artifact(
                parquet_path, stage, row_count=row_count
            ),
            "quality_summary": artifact(quality_path, stage),
            "query_sql": artifact(query_sql, stage),
        }
        after_catalog = catalog_hash(workspace)
        if after_catalog != before_catalog:
            raise IdxZeroLossError("catalog changed during candidate execution")
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
            "manifest_kind": "CSMAR_IDX_IDXINFO_ZERO_LOSS_CANDIDATE",
            "tool_id": TOOL_ID,
            "status": "ZERO_LOSS_CANDIDATE_MATERIALIZED",
            "write_performed": True,
            "created_at": utc_now(),
            "dataset_id": DATASET_ID,
            "table": "IDX_Idxinfo",
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
                "sha256-canonical-json-csmar-idx-idxinfo-zero-loss-v1"
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
            raise IdxZeroLossError("catalog changed before candidate seal")
        os.replace(stage, target)
        return result
    except Exception:
        tmp_root = (workspace / "tmp").resolve()
        if stage.exists() and is_below(stage.resolve(), tmp_root):
            shutil.rmtree(stage)
        raise


def default_downloads() -> Path:
    return Path.home() / "Downloads"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or materialize the bound zero-loss CSMAR IDX_Idxinfo "
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
        IdxZeroLossError,
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
