#!/usr/bin/env python3
"""Build bound multi-member CSMAR IDX_Chgsmp inspection candidates.

This adapter is deliberately limited to the two reviewed, non-overlapping
exports ``013710529`` (2021--2023) and ``014214039`` (2024--2026-07-30).
The exact duplicate ``013710529 (1)`` is rejected by filename even though its
package hash is identical.  The earlier ``094857265`` export remains governed
by ``parse_csmar_idx_chgsmp_zero_loss_candidate.py`` and is not merged here.

Each source ZIP and member is hash-bound.  Every CSV byte is assigned to the
header or one physical business record, source empty market codes remain empty
strings, quote-only source defects are repaired reversibly, and every Parquet
column remains VARCHAR.  Execution writes only below
``tmp/csmar-idx-chgsmp-candidate`` and never edits catalog, roadmap, registry,
or the source ZIPs.  All outputs are INSPECTION_ONLY/NOT_ADMITTED.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import sys
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, TextIO


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
LEGACY_PATH = Path(__file__).with_name(
    "parse_csmar_idx_chgsmp_zero_loss_candidate.py"
)
LEGACY_SPEC = importlib.util.spec_from_file_location(
    "_qdata_idx_chgsmp_legacy_for_multicsv", LEGACY_PATH
)
if LEGACY_SPEC is None or LEGACY_SPEC.loader is None:
    raise RuntimeError(f"cannot load IDX_Chgsmp adapter: {LEGACY_PATH}")
legacy = importlib.util.module_from_spec(LEGACY_SPEC)
sys.modules[LEGACY_SPEC.name] = legacy
LEGACY_SPEC.loader.exec_module(legacy)
engine = legacy.engine


TOOL_ID = (
    "parse_csmar_idx_chgsmp_multicsv_zero_loss_candidate.py/"
    "csmar-idx-chgsmp-multicsv-zero-loss-candidate-v1"
)
PURPOSE_CEILING = "INSPECTION_ONLY"
ADMISSION_DECISION = "NOT_ADMITTED"
DATASET_ID = "csmar-idx-chgsmp-zero-loss"
TABLE_NAME = "idx_chgsmp_zero_loss_candidate"
OUTPUT_DIR_NAME = "csmar-idx-chgsmp-candidate"
DESCRIPTOR_MEMBER = "IDX_Chgsmp[DES][csv].txt"
COPYRIGHT_MEMBER = "版权声明.pdf"
AS_OF_DATE = "2026-07-30"
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
ROW_ANCHOR = re.compile(
    rb'^"[A-Z0-9]+","\d{4}-\d{2}-\d{2}","'
)


class IdxChgsmpMultiCsvError(RuntimeError):
    """A controlled, fail-closed multi-member candidate rejection."""


@dataclass(frozen=True)
class MemberBinding:
    name: str
    bytes: int
    compressed_bytes: int
    crc32: str
    sha256: str
    row_count: int | None = None
    repaired_record_count: int | None = None
    quote_repair_count: int | None = None


@dataclass(frozen=True)
class PackageBinding:
    package_id: str
    zip_name: str
    zip_bytes: int
    zip_sha256: str
    members: tuple[MemberBinding, ...]
    expected_rows: int
    expected_effective_min: str
    expected_effective_max: str
    expected_announcement_min: str
    expected_announcement_max: str
    expected_index_count: int
    expected_constituent_count: int
    expected_empty_name_count: int
    expected_empty_market_count: int

    @property
    def csv_members(self) -> tuple[MemberBinding, ...]:
        return tuple(
            member
            for member in self.members
            if member.name.casefold().endswith(".csv")
        )

    @property
    def member_map(self) -> dict[str, MemberBinding]:
        return {member.name: member for member in self.members}


COMMON_MEMBERS = (
    MemberBinding(
        name=COPYRIGHT_MEMBER,
        bytes=227_624,
        compressed_bytes=215_143,
        crc32="244a292a",
        sha256=(
            "efed0f0e29f5c89cd8847f49d6dd58d26c3cd4fd7ff22cf676a23072d78ccc66"
        ),
    ),
    MemberBinding(
        name=DESCRIPTOR_MEMBER,
        bytes=1_136,
        compressed_bytes=560,
        crc32="b2f259de",
        sha256=(
            "7e761223a49aa9f34887b5472f4a5e3a6a787686c637406da2bc25edb2b2ec82"
        ),
    ),
)


PACKAGE_BINDINGS = {
    "013710529": PackageBinding(
        package_id="013710529",
        zip_name="指数取样变更文件013710529(仅供清华大学使用).zip",
        zip_bytes=34_824_140,
        zip_sha256=(
            "1ae12f58a0c685816f146ad65a3ea7b95ac515a85b51ea4679417cbf61a4dd99"
        ),
        members=COMMON_MEMBERS
        + (
            MemberBinding(
                "IDX_Chgsmp.csv",
                79_575_073,
                10_224_834,
                "0a0fb272",
                (
                    "cfddc0921ce1da28ae3005705eb978ae350f55979d39bdd51e6877e165a22d4f"
                ),
                1_000_000,
                0,
                0,
            ),
            MemberBinding(
                "IDX_Chgsmp1.csv",
                81_145_381,
                9_538_552,
                "732c3cd2",
                (
                    "9ae15b31f63985ddef8fb74b6f25e278ec116f6964a04d12be40f6e45f5991c6"
                ),
                1_000_000,
                0,
                0,
            ),
            MemberBinding(
                "IDX_Chgsmp2.csv",
                78_402_163,
                9_352_170,
                "9fd6fe50",
                (
                    "03b796ab34e767ec257c9d4184bd2d72ed5dfc8a07326de8e4497bf9a2507d7e"
                ),
                1_000_000,
                282,
                564,
            ),
            MemberBinding(
                "IDX_Chgsmp3.csv",
                44_533_335,
                5_492_205,
                "b3b98c90",
                (
                    "c20a6830c103f4c52863e1b73f2f93e3bc7490c6fa7e8199ed2b5f6f5003127f"
                ),
                596_921,
                0,
                0,
            ),
        ),
        expected_rows=3_596_921,
        expected_effective_min="2021-01-04",
        expected_effective_max="2023-12-29",
        expected_announcement_min="2020-10-14",
        expected_announcement_max="2024-05-31",
        expected_index_count=4_164,
        expected_constituent_count=191_288,
        expected_empty_name_count=20,
        expected_empty_market_count=32_184,
    ),
    "014214039": PackageBinding(
        package_id="014214039",
        zip_name="指数取样变更文件014214039(仅供清华大学使用).zip",
        zip_bytes=74_890_824,
        zip_sha256=(
            "1b17a12f5b770daa20773459246d039a2a7a963dc3cb86135fa35f232efede4e"
        ),
        members=COMMON_MEMBERS
        + (
            MemberBinding(
                "IDX_Chgsmp.csv",
                81_832_006,
                10_541_278,
                "b1584628",
                (
                    "5de853357a6e1743eecd305ef27ce6cf0dd15069f429c70f13d3c6d6f665b088"
                ),
                1_000_000,
                0,
                0,
            ),
            MemberBinding(
                "IDX_Chgsmp1.csv",
                81_721_395,
                11_304_620,
                "da5d5616",
                (
                    "49aac1e96cf86cef138c04cd42994d8fe0a05b452ddd299568962c515557a296"
                ),
                1_000_000,
                0,
                0,
            ),
            MemberBinding(
                "IDX_Chgsmp2.csv",
                84_311_218,
                10_239_893,
                "1a7ec66d",
                (
                    "0b2e7f27c8271baefb662d0f15aa68e5ade525c24ffb267d1b2e07ec5157a2be"
                ),
                1_000_000,
                0,
                0,
            ),
            MemberBinding(
                "IDX_Chgsmp3.csv",
                80_907_448,
                10_952_261,
                "b45659ec",
                (
                    "89a0141672123f9405ae99c06479071d176ee6089cbf08af087b3039d23f629a"
                ),
                1_000_000,
                1_614,
                3_236,
            ),
            MemberBinding(
                "IDX_Chgsmp4.csv",
                77_525_706,
                9_723_066,
                "a9b9b7df",
                (
                    "45282983334def396a0e507ad6a021d76ac370d0c11732d7551af425690e4e3d"
                ),
                1_000_000,
                0,
                0,
            ),
            MemberBinding(
                "IDX_Chgsmp5.csv",
                73_339_775,
                9_020_000,
                "5bbb1632",
                (
                    "dd52316b9e20d070331b750ee8d0ba314e0502dbb36534d17af0c76f5bc904f2"
                ),
                1_000_000,
                0,
                0,
            ),
            MemberBinding(
                "IDX_Chgsmp6.csv",
                79_947_492,
                11_074_984,
                "aa6ecc46",
                (
                    "4c44b96b2cfc8e00a6ee6cc5a2f0d1863d57d85b9b4242db88edf91e20c62f4c"
                ),
                1_000_000,
                0,
                0,
            ),
            MemberBinding(
                "IDX_Chgsmp7.csv",
                12_636_537,
                1_817_919,
                "01f443a9",
                (
                    "5c67558611bdcca8e3d3fad578adcead2639f655bf9b5cf5b3a8eb9c40916a5f"
                ),
                170_930,
                0,
                0,
            ),
        ),
        expected_rows=7_170_930,
        expected_effective_min="2024-01-02",
        expected_effective_max="2026-07-30",
        expected_announcement_min="2023-11-24",
        expected_announcement_max="2026-07-30",
        expected_index_count=4_809,
        expected_constituent_count=246_989,
        expected_empty_name_count=80,
        expected_empty_market_count=109_639,
    ),
}


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
        raise IdxChgsmpMultiCsvError(f"unsafe {label}: {value!r}")
    return value


def catalog_hash(workspace_root: Path) -> str | None:
    path = workspace_root / "data" / "catalog" / "datasets.json"
    return sha256_file(path) if path.is_file() else None


def require_input_location(
    zip_path: Path,
    input_root: Path,
    binding: PackageBinding,
) -> Path:
    root = input_root.resolve(strict=True)
    resolved = zip_path.resolve(strict=True)
    if not root.is_dir():
        raise IdxChgsmpMultiCsvError(f"input root is not a directory: {root}")
    if not resolved.is_file() or resolved.suffix.casefold() != ".zip":
        raise IdxChgsmpMultiCsvError(f"source is not a ZIP file: {resolved}")
    if (
        zip_path.is_symlink()
        or is_reparse_point(zip_path)
        or not is_below(resolved, root)
    ):
        raise IdxChgsmpMultiCsvError(
            "source ZIP must be a non-reparse file below the explicit input root"
        )
    if resolved.name != binding.zip_name:
        raise IdxChgsmpMultiCsvError(
            "source filename is not the reviewed primary export; exact duplicate "
            f"copies are intentionally excluded: {resolved.name!r}"
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
        raise IdxChgsmpMultiCsvError(
            f"output root must stay below {allowed_base.as_posix()}"
        )
    target = base / DATASET_ID / run_id
    if not is_below(target.resolve(), allowed_base):
        raise IdxChgsmpMultiCsvError(
            "candidate target escaped its allowed tmp tree"
        )
    return target


def stream_sha256(stream: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        size += len(block)
        digest.update(block)
    return size, digest.hexdigest()


def bind_source(
    zip_path: Path,
    binding: PackageBinding,
) -> dict[str, Any]:
    package_hash = sha256_file(zip_path)
    if package_hash != binding.zip_sha256:
        raise IdxChgsmpMultiCsvError(
            "source ZIP SHA-256 differs from reviewed binding: "
            f"{package_hash}"
        )
    if zip_path.stat().st_size != binding.zip_bytes:
        raise IdxChgsmpMultiCsvError(
            "source ZIP byte count differs from reviewed binding"
        )
    receipts: dict[str, Any] = {}
    with zipfile.ZipFile(zip_path) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise IdxChgsmpMultiCsvError(f"ZIP CRC failure: {corrupt!r}")
        members = engine.safe_zip_members(archive)
        if set(members) != set(binding.member_map):
            raise IdxChgsmpMultiCsvError(
                "ZIP member set differs from reviewed binding: "
                f"{sorted(members)!r}"
            )
        for name, expected in binding.member_map.items():
            info = members[name]
            with archive.open(info) as stream:
                member_bytes, member_hash = stream_sha256(stream)
            receipt = {
                "bytes": member_bytes,
                "compressed_bytes": info.compress_size,
                "crc32": f"{info.CRC:08x}",
                "encrypted": bool(info.flag_bits & 0x1),
                "sha256": member_hash,
                "expected_row_count": expected.row_count,
            }
            for key in ("bytes", "compressed_bytes", "crc32", "sha256"):
                if receipt[key] != getattr(expected, key):
                    raise IdxChgsmpMultiCsvError(
                        f"{name} {key} differs from reviewed binding: "
                        f"{receipt[key]!r}"
                    )
            if receipt["encrypted"]:
                raise IdxChgsmpMultiCsvError(
                    f"encrypted member rejected: {name}"
                )
            receipts[name] = receipt
    return {
        "package_id": binding.package_id,
        "zip_path": zip_path.as_posix(),
        "zip_filename": zip_path.name,
        "zip_bytes": zip_path.stat().st_size,
        "zip_sha256": package_hash,
        "zip_crc_read_status": "PASS",
        "zip_member_count": len(receipts),
        "members": receipts,
        "csv_member_order": [
            member.name for member in binding.csv_members
        ],
        "encoding": "utf-8-sig",
        "exact_duplicate_policy": (
            "PRIMARY_FILENAME_ONLY__013710529_PAREN_1_EXCLUDED"
        ),
    }


def parse_header(header: bytes) -> None:
    if not header.startswith(codecs.BOM_UTF8):
        raise IdxChgsmpMultiCsvError("source CSV must be UTF-8-SIG")
    try:
        rows = list(
            csv.reader(
                io.StringIO(
                    header.decode("utf-8-sig", errors="strict"),
                    newline="",
                ),
                strict=True,
            )
        )
    except (UnicodeError, csv.Error) as exc:
        raise IdxChgsmpMultiCsvError(
            f"cannot parse source header: {exc}"
        ) from exc
    if rows != [list(legacy.CHGSMP_COLUMNS)]:
        raise IdxChgsmpMultiCsvError(
            f"source header mismatch: {rows!r}"
        )


@dataclass
class PackageAccumulator:
    rows: int = 0
    strict_rows: int = 0
    repaired_rows: int = 0
    quote_repairs: int = 0
    source_bytes: int = 0
    accounted_bytes: int = 0
    repair_sidecar_rows: int = 0
    indexes: set[str] | None = None
    constituents: set[str] | None = None
    markets: Counter[str] | None = None
    security_types: Counter[str] | None = None
    change_types: Counter[str] | None = None
    missing: Counter[str] | None = None
    reason_counts: Counter[str] | None = None
    effective_min: str | None = None
    effective_max: str | None = None
    announcement_min: str | None = None
    announcement_max: str | None = None
    maximum_announcement_lead_days: int = 0
    maximum_announcement_lag_days: int = 0

    def __post_init__(self) -> None:
        self.indexes = set()
        self.constituents = set()
        self.markets = Counter()
        self.security_types = Counter()
        self.change_types = Counter()
        self.missing = Counter()
        self.reason_counts = Counter()


def quality_reasons(values: dict[str, str]) -> tuple[list[str], int]:
    reasons: list[str] = []
    if values["Chgsmp07"] == "":
        reasons.append("SOURCE_MARKET_CODE_EMPTY")
    if values["Chgsmp01"] > AS_OF_DATE:
        reasons.append("EFFECTIVE_DATE_AFTER_AS_OF_DATE")
    if values["Chgsmp06"] > AS_OF_DATE:
        reasons.append("ANNOUNCEMENT_DATE_AFTER_AS_OF_DATE")
    effective = date.fromisoformat(values["Chgsmp01"])
    announced = date.fromisoformat(values["Chgsmp06"])
    lead_days = (effective - announced).days
    if lead_days < 0:
        reasons.append("ANNOUNCEMENT_AFTER_EFFECTIVE_DATE")
    elif lead_days > 0:
        reasons.append("ANNOUNCED_BEFORE_EFFECTIVE_DATE")
    return reasons, lead_days


def update_accumulator(
    accumulator: PackageAccumulator,
    values: list[str],
    *,
    strict: bool,
    repaired: bool,
    quote_repairs: int,
) -> None:
    row = dict(zip(legacy.CHGSMP_COLUMNS, values, strict=True))
    accumulator.rows += 1
    accumulator.strict_rows += int(strict)
    accumulator.repaired_rows += int(repaired)
    accumulator.quote_repairs += quote_repairs
    assert accumulator.indexes is not None
    assert accumulator.constituents is not None
    assert accumulator.markets is not None
    assert accumulator.security_types is not None
    assert accumulator.change_types is not None
    assert accumulator.missing is not None
    assert accumulator.reason_counts is not None
    accumulator.indexes.add(row["Indexcd"])
    accumulator.constituents.add(row["Chgsmp02"])
    accumulator.markets[row["Chgsmp07"]] += 1
    accumulator.security_types[row["Chgsmp05"]] += 1
    accumulator.change_types[row["Chgsmp04"]] += 1
    for column, value in row.items():
        accumulator.missing[column] += int(value == "")
    effective = row["Chgsmp01"]
    announced = row["Chgsmp06"]
    accumulator.effective_min = (
        effective
        if accumulator.effective_min is None
        else min(accumulator.effective_min, effective)
    )
    accumulator.effective_max = (
        effective
        if accumulator.effective_max is None
        else max(accumulator.effective_max, effective)
    )
    accumulator.announcement_min = (
        announced
        if accumulator.announcement_min is None
        else min(accumulator.announcement_min, announced)
    )
    accumulator.announcement_max = (
        announced
        if accumulator.announcement_max is None
        else max(accumulator.announcement_max, announced)
    )
    reasons, lead_days = quality_reasons(row)
    accumulator.reason_counts.update(reasons)
    accumulator.maximum_announcement_lead_days = max(
        accumulator.maximum_announcement_lead_days,
        lead_days,
    )
    accumulator.maximum_announcement_lag_days = max(
        accumulator.maximum_announcement_lag_days,
        -lead_days,
    )


def parse_member_to_canonical(
    *,
    archive: zipfile.ZipFile,
    member: MemberBinding,
    canonical_path: Path,
    repair_sidecar: TextIO,
    accumulator: PackageAccumulator,
    package_ordinal_start: int,
) -> dict[str, Any]:
    spec = replace(
        legacy.CHGSMP_SPEC,
        member_name=member.name,
        expected_record_count=int(member.row_count or 0),
        # The reviewed broad exports contain 20/80 genuinely blank security
        # names in 013/014.  Identity, clocks and coded classifications remain
        # required; source names and markets are preserved without filling.
        required_columns=(
            "Indexcd",
            "Chgsmp01",
            "Chgsmp02",
            "Chgsmp04",
            "Chgsmp05",
            "Chgsmp06",
        ),
    )
    info = archive.getinfo(member.name)
    canonical_hash = hashlib.sha256()
    source_hash = hashlib.sha256()
    header_bytes = 0
    accounted_bytes = 0
    rows = 0
    strict_rows = 0
    repaired_rows = 0
    quote_repairs = 0
    empty_market = 0
    effective_min: str | None = None
    effective_max: str | None = None
    announcement_min: str | None = None
    announcement_max: str | None = None
    with archive.open(info) as source, canonical_path.open(
        "x", encoding="utf-8-sig", errors="strict", newline=""
    ) as output:
        header = source.readline()
        source_hash.update(header)
        parse_header(header)
        header_bytes = len(header)
        accounted_bytes += len(header)
        canonical_header = engine.canonical_csv_body(
            list(legacy.CHGSMP_COLUMNS)
        ) + "\n"
        output.write(canonical_header)
        canonical_hash.update(codecs.BOM_UTF8)
        canonical_hash.update(canonical_header.encode("utf-8"))
        source_offset = len(header)
        for raw_line in source:
            rows += 1
            package_ordinal = package_ordinal_start + rows
            source_hash.update(raw_line)
            accounted_bytes += len(raw_line)
            body, separator = engine.split_final_record_separator(raw_line)
            if not ROW_ANCHOR.match(body):
                raise IdxChgsmpMultiCsvError(
                    f"{member.name} record {rows} lacks reviewed anchor"
                )
            framed = engine.FramedRecord(
                ordinal=rows,
                start_byte=source_offset,
                end_byte=source_offset + len(raw_line),
                region=raw_line,
                body=body,
                separator=separator,
            )
            source_offset += len(raw_line)
            body_text = body.decode("utf-8", errors="strict")
            if body.count(b'"') == 16 and body.count(b'","') == 7:
                raw_parts = body_text[1:-1].split('","')
                if len(raw_parts) != len(legacy.CHGSMP_COLUMNS):
                    raise IdxChgsmpMultiCsvError(
                        f"{member.name} record {rows} width changed"
                    )
                values = raw_parts
                engine.validate_values(values, framed=framed, spec=spec)
                canonical_body = engine.canonical_csv_body(values)
                if canonical_body != body_text:
                    raise IdxChgsmpMultiCsvError(
                        f"{member.name} record {rows} clean roundtrip changed"
                    )
                sidecar = None
                strict = True
                repaired = False
                row_quote_repairs = 0
            else:
                values, receipt, strict = engine.parse_one_record(framed, spec)
                canonical_body = engine.canonical_csv_body(values)
                repaired = receipt["parse_status"] != "CLEAN_STRICT"
                row_quote_repairs = int(receipt["quote_repair_count"])
                if repaired:
                    receipt["source_member"] = member.name
                    receipt["member_record_ordinal"] = rows
                    receipt["package_record_ordinal"] = package_ordinal
                    repair_sidecar.write(
                        json.dumps(
                            receipt,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                            allow_nan=False,
                        )
                        + "\n"
                    )
                    accumulator.repair_sidecar_rows += 1
                sidecar = receipt
            rendered = canonical_body + "\n"
            output.write(rendered)
            canonical_hash.update(rendered.encode("utf-8"))
            strict_rows += int(strict)
            repaired_rows += int(repaired)
            quote_repairs += row_quote_repairs
            empty_market += int(values[7] == "")
            effective_min = (
                values[1]
                if effective_min is None
                else min(effective_min, values[1])
            )
            effective_max = (
                values[1]
                if effective_max is None
                else max(effective_max, values[1])
            )
            announcement_min = (
                values[6]
                if announcement_min is None
                else min(announcement_min, values[6])
            )
            announcement_max = (
                values[6]
                if announcement_max is None
                else max(announcement_max, values[6])
            )
            update_accumulator(
                accumulator,
                values,
                strict=strict,
                repaired=repaired,
                quote_repairs=row_quote_repairs,
            )
    if rows != member.row_count:
        raise IdxChgsmpMultiCsvError(
            f"{member.name} row count {rows} != {member.row_count}"
        )
    if accounted_bytes != member.bytes or source_offset != member.bytes:
        raise IdxChgsmpMultiCsvError(
            f"{member.name} source byte accounting is incomplete"
        )
    if source_hash.hexdigest() != member.sha256:
        raise IdxChgsmpMultiCsvError(
            f"{member.name} stream hash differs from binding"
        )
    if repaired_rows != member.repaired_record_count:
        raise IdxChgsmpMultiCsvError(
            f"{member.name} repaired rows {repaired_rows} != "
            f"{member.repaired_record_count}"
        )
    if quote_repairs != member.quote_repair_count:
        raise IdxChgsmpMultiCsvError(
            f"{member.name} quote repairs {quote_repairs} != "
            f"{member.quote_repair_count}"
        )
    accumulator.source_bytes += member.bytes
    accumulator.accounted_bytes += accounted_bytes
    return {
        "member": member.name,
        "source_bytes": member.bytes,
        "source_sha256": member.sha256,
        "header_bytes": header_bytes,
        "accounted_source_bytes": accounted_bytes,
        "unaccounted_source_bytes": member.bytes - accounted_bytes,
        "logical_block_accounting_ratio": 1.0,
        "row_count": rows,
        "strict_source_record_count": strict_rows,
        "repaired_source_record_count": repaired_rows,
        "quote_repair_count": quote_repairs,
        "empty_market_count": empty_market,
        "effective_date_min": effective_min,
        "effective_date_max": effective_max,
        "announcement_date_min": announcement_min,
        "announcement_date_max": announcement_max,
        "canonical_bytes": canonical_path.stat().st_size,
        "canonical_sha256": canonical_hash.hexdigest(),
        "canonical_file_sha256": sha256_file(canonical_path),
        "clean_record_contract": (
            "SOURCE_BODY_EQUALS_CANONICAL_BODY__ONLY_LINE_ENDING_NORMALIZED"
        ),
        "repaired_record_contract": (
            "EVERY_REPAIR_RECORDED_AND_BYTE_REVERSIBLE"
        ),
    }


def sql_path_list(paths: list[Path]) -> str:
    return "[" + ",".join(
        engine.sql_string(path.resolve().as_posix()) for path in paths
    ) + "]"


def build_and_verify_package_parquet(
    *,
    canonical_paths: list[Path],
    parquet_path: Path,
    temp_dir: Path,
) -> dict[str, Any]:
    duckdb = engine.import_duckdb()
    connection = duckdb.connect(database=":memory:")
    columns = ", ".join(
        engine.sql_identifier(column) for column in legacy.CHGSMP_COLUMNS
    )
    key_sql = ", ".join(
        engine.sql_identifier(column) for column in legacy.MECHANICAL_KEY
    )
    paths_sql = sql_path_list(canonical_paths)
    parquet_target = parquet_path.resolve().as_posix()
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        connection.execute(
            f"SET temp_directory={engine.sql_string(temp_dir.resolve().as_posix())}"
        )
        connection.execute(
            "CREATE TABLE _csv AS SELECT "
            f"{columns} FROM read_csv("
            f"{paths_sql}, header=true, all_varchar=true, "
            "delim=',', quote='\"', escape='\"', strict_mode=true, "
            f"nullstr={engine.sql_string(engine.PARQUET_NULL_SENTINEL)})"
        )
        csv_rows = int(
            connection.execute("SELECT COUNT(*) FROM _csv").fetchone()[0]
        )
        duplicate = connection.execute(
            "SELECT COALESCE(SUM(c - 1), 0), COUNT(*) FROM "
            f"(SELECT {key_sql}, COUNT(*) AS c FROM _csv "
            f"GROUP BY {key_sql} HAVING COUNT(*) > 1)"
        ).fetchone()
        duplicate_rows = int(duplicate[0])
        duplicate_groups = int(duplicate[1])
        if duplicate_rows:
            raise IdxChgsmpMultiCsvError(
                "canonical package contains duplicate candidate keys: "
                f"{duplicate_groups} groups, {duplicate_rows} rows"
            )
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        connection.execute(
            f"COPY (SELECT {columns} FROM _csv ORDER BY {key_sql}) "
            f"TO {engine.sql_string(parquet_target)} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        connection.execute(
            "CREATE VIEW _parquet AS SELECT "
            f"{columns} FROM read_parquet({engine.sql_string(parquet_target)})"
        )
        parquet_rows = int(
            connection.execute("SELECT COUNT(*) FROM _parquet").fetchone()[0]
        )
        csv_minus_parquet = int(
            connection.execute(
                "SELECT COUNT(*) FROM ("
                f"(SELECT {columns} FROM _csv) EXCEPT ALL "
                f"(SELECT {columns} FROM _parquet))"
            ).fetchone()[0]
        )
        parquet_minus_csv = int(
            connection.execute(
                "SELECT COUNT(*) FROM ("
                f"(SELECT {columns} FROM _parquet) EXCEPT ALL "
                f"(SELECT {columns} FROM _csv))"
            ).fetchone()[0]
        )
        if (
            csv_rows != parquet_rows
            or csv_minus_parquet
            or parquet_minus_csv
        ):
            raise IdxChgsmpMultiCsvError(
                "CSV/Parquet roundtrip mismatch"
            )
        types = connection.execute(
            "DESCRIBE SELECT * FROM _parquet"
        ).fetchall()
        non_varchar = [
            row for row in types if str(row[1]).upper() != "VARCHAR"
        ]
        if non_varchar:
            raise IdxChgsmpMultiCsvError(
                f"Parquet inferred non-VARCHAR fields: {non_varchar}"
            )
        return {
            "status": "PASS_ZERO_BIDIRECTIONAL_DIFFERENCE",
            "csv_member_count": len(canonical_paths),
            "csv_row_count": csv_rows,
            "parquet_row_count": parquet_rows,
            "csv_minus_parquet_rows": csv_minus_parquet,
            "parquet_minus_csv_rows": parquet_minus_csv,
            "candidate_key_duplicate_groups": duplicate_groups,
            "candidate_key_duplicate_rows": duplicate_rows,
            "column_count": len(legacy.CHGSMP_COLUMNS),
            "all_columns_varchar": True,
            "empty_string_preservation": (
                "Reserved impossible null sentinel keeps source empty market "
                "codes as empty VARCHAR strings."
            ),
        }
    finally:
        connection.close()


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


def validate_package_semantics(
    accumulator: PackageAccumulator,
    binding: PackageBinding,
) -> dict[str, Any]:
    assert accumulator.indexes is not None
    assert accumulator.constituents is not None
    assert accumulator.markets is not None
    assert accumulator.security_types is not None
    assert accumulator.change_types is not None
    assert accumulator.missing is not None
    assert accumulator.reason_counts is not None
    checks = {
        "rows": (accumulator.rows, binding.expected_rows),
        "effective_min": (
            accumulator.effective_min,
            binding.expected_effective_min,
        ),
        "effective_max": (
            accumulator.effective_max,
            binding.expected_effective_max,
        ),
        "announcement_min": (
            accumulator.announcement_min,
            binding.expected_announcement_min,
        ),
        "announcement_max": (
            accumulator.announcement_max,
            binding.expected_announcement_max,
        ),
        "index_count": (
            len(accumulator.indexes),
            binding.expected_index_count,
        ),
        "constituent_count": (
            len(accumulator.constituents),
            binding.expected_constituent_count,
        ),
        "empty_market_count": (
            accumulator.missing["Chgsmp07"],
            binding.expected_empty_market_count,
        ),
        "empty_security_name_count": (
            accumulator.missing["Chgsmp03"],
            binding.expected_empty_name_count,
        ),
    }
    for label, (actual, expected) in checks.items():
        if actual != expected:
            raise IdxChgsmpMultiCsvError(
                f"reviewed semantic invariant {label} changed: "
                f"{actual!r} != {expected!r}"
            )
    if accumulator.source_bytes != accumulator.accounted_bytes:
        raise IdxChgsmpMultiCsvError(
            "package source byte accounting is incomplete"
        )
    return {
        "logical_row_count": accumulator.rows,
        "column_count": len(legacy.CHGSMP_COLUMNS),
        "actual_field_selection": "8_OF_8",
        "strict_source_record_count": accumulator.strict_rows,
        "repaired_source_record_count": accumulator.repaired_rows,
        "quote_repair_count": accumulator.quote_repairs,
        "repair_sidecar_row_count": accumulator.repair_sidecar_rows,
        "source_csv_bytes": accumulator.source_bytes,
        "accounted_source_bytes": accumulator.accounted_bytes,
        "unaccounted_source_bytes": 0,
        "logical_block_accounting_ratio": 1.0,
        "candidate_key": list(legacy.MECHANICAL_KEY),
        "index_code_unique_count": len(accumulator.indexes),
        "constituent_code_unique_count": len(accumulator.constituents),
        "effective_date_min": accumulator.effective_min,
        "effective_date_max": accumulator.effective_max,
        "announcement_date_min": accumulator.announcement_min,
        "announcement_date_max": accumulator.announcement_max,
        "missing_field_counts": {
            column: accumulator.missing[column]
            for column in legacy.CHGSMP_COLUMNS
        },
        "market_missing_count": accumulator.missing["Chgsmp07"],
        "security_type_counts": dict(
            sorted(accumulator.security_types.items())
        ),
        "market_code_counts": dict(sorted(accumulator.markets.items())),
        "change_type_counts": dict(
            sorted(accumulator.change_types.items())
        ),
        "quality_reason_counts": dict(
            sorted(accumulator.reason_counts.items())
        ),
        "maximum_announcement_lead_days": (
            accumulator.maximum_announcement_lead_days
        ),
        "maximum_announcement_lag_days": (
            accumulator.maximum_announcement_lag_days
        ),
        "empty_market_preservation": (
            "SOURCE_EMPTY_STRING_RETAINED__NO_FILL__NO_DROP__NO_NULL_CAST"
        ),
        "as_of_date": AS_OF_DATE,
    }


def extract_bound_members(
    archive: zipfile.ZipFile,
    binding: PackageBinding,
    extracted_dir: Path,
) -> dict[str, Path]:
    extracted: dict[str, Path] = {}
    for member in binding.members:
        target = extracted_dir / member.name
        with archive.open(member.name) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        if target.stat().st_size != member.bytes:
            raise IdxChgsmpMultiCsvError(
                f"extracted member byte count mismatch: {member.name}"
            )
        if sha256_file(target) != member.sha256:
            raise IdxChgsmpMultiCsvError(
                f"extracted member hash mismatch: {member.name}"
            )
        extracted[member.name] = target
    return extracted


def run(
    *,
    workspace_root: Path,
    package_id: str,
    zip_path: Path,
    input_root: Path,
    run_id: str,
    output_root: Path | None,
    execute: bool,
) -> dict[str, Any]:
    if package_id not in PACKAGE_BINDINGS:
        raise IdxChgsmpMultiCsvError(
            f"unsupported reviewed package_id: {package_id!r}"
        )
    binding = PACKAGE_BINDINGS[package_id]
    workspace = workspace_root.resolve()
    require_safe_component(run_id, "run_id")
    source_zip = require_input_location(zip_path, input_root, binding)
    target = require_output_location(workspace, output_root, run_id)
    catalog_before = catalog_hash(workspace)
    source = bind_source(source_zip, binding)
    if not execute:
        result = {
            "schema_version": 1,
            "manifest_kind": "CSMAR_IDX_CHGSMP_MULTICSV_ZERO_LOSS_PLAN",
            "tool_id": TOOL_ID,
            "status": "BOUND_PLAN_COMPLETE__NO_PARSE__NO_WRITES",
            "write_performed": False,
            "candidate_path": target.as_posix(),
            "source": source,
            "expected_rows": binding.expected_rows,
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
            "catalog_sha256_before": catalog_before,
            "catalog_sha256_after": catalog_hash(workspace),
        }
        if result["catalog_sha256_after"] != catalog_before:
            raise IdxChgsmpMultiCsvError(
                "catalog changed during read-only binding plan"
            )
        return result

    if target.exists():
        raise IdxChgsmpMultiCsvError(
            f"candidate target already exists: {target}"
        )
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
        temp_dir = stage / "duckdb-tmp"
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
        if sha256_file(raw_copy) != binding.zip_sha256:
            raise IdxChgsmpMultiCsvError("raw ZIP copy hash mismatch")

        canonical_paths: list[Path] = []
        member_metrics: list[dict[str, Any]] = []
        accumulator = PackageAccumulator()
        repair_path = receipt_dir / "repaired-records.jsonl"
        accounting_path = receipt_dir / "member-accounting.jsonl"
        with zipfile.ZipFile(source_zip) as archive:
            extracted = extract_bound_members(
                archive, binding, extracted_dir
            )
            package_ordinal = 0
            with repair_path.open(
                "x", encoding="utf-8", errors="strict", newline="\n"
            ) as repair_file, accounting_path.open(
                "x", encoding="utf-8", errors="strict", newline="\n"
            ) as accounting_file:
                for member in binding.csv_members:
                    canonical_path = canonical_dir / member.name
                    metrics = parse_member_to_canonical(
                        archive=archive,
                        member=member,
                        canonical_path=canonical_path,
                        repair_sidecar=repair_file,
                        accumulator=accumulator,
                        package_ordinal_start=package_ordinal,
                    )
                    canonical_paths.append(canonical_path)
                    member_metrics.append(metrics)
                    package_ordinal += int(member.row_count or 0)
                    accounting_file.write(
                        json.dumps(
                            metrics,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                            allow_nan=False,
                        )
                        + "\n"
                    )

        semantic = validate_package_semantics(accumulator, binding)
        parquet_path = curated_dir / "part-000.parquet"
        parquet_roundtrip = build_and_verify_package_parquet(
            canonical_paths=canonical_paths,
            parquet_path=parquet_path,
            temp_dir=temp_dir,
        )
        if parquet_roundtrip["csv_row_count"] != binding.expected_rows:
            raise IdxChgsmpMultiCsvError(
                "Parquet package row count differs from binding"
            )
        semantic["candidate_key_duplicate_groups"] = parquet_roundtrip[
            "candidate_key_duplicate_groups"
        ]
        semantic["candidate_key_duplicate_rows"] = parquet_roundtrip[
            "candidate_key_duplicate_rows"
        ]
        semantic["candidate_key_status"] = (
            "MECHANICALLY_UNIQUE_ONLY__NOT_A_FORMAL_STABLE_KEY"
        )
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

        quality = {
            "schema_version": 1,
            "mechanical_status": "PASS_ZERO_LOSS_MULTICSV_PACKAGE",
            "package_id": binding.package_id,
            "table": "IDX_Chgsmp",
            "dataset_id": DATASET_ID,
            "member_metrics": member_metrics,
            "semantic_metrics": semantic,
            "parquet_roundtrip": parquet_roundtrip,
            "quality_status": "QUARANTINE_REVIEW_REQUIRED",
            "pit_blockers": [
                "Chgsmp06 first-publication status and provider revision chain "
                "are not established.",
                "Blank market codes are preserved and require source or official "
                "identity closure before formal use.",
                "Membership must not activate before Chgsmp01.",
                "Constituent weights and independently versioned methodology are "
                "not present in this table.",
            ],
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
            "catalog_registered": False,
            "source_admission": False,
            "quality_promotion": False,
            "research_ready": False,
        }
        quality_path = quality_dir / "summary.json"
        write_json(quality_path, quality)
        source_binding_path = raw_dir / "source-binding.json"
        write_json(
            source_binding_path,
            {
                "schema_version": 1,
                "source": source,
                "reviewed_binding": {
                    "package_id": binding.package_id,
                    "zip_name": binding.zip_name,
                    "zip_bytes": binding.zip_bytes,
                    "zip_sha256": binding.zip_sha256,
                    "members": [
                        member.__dict__ for member in binding.members
                    ],
                },
                "source_zip_modified": False,
                "catalog_write_authorized": False,
            },
        )
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

        outputs = {
            "raw_zip": artifact(raw_copy, stage),
            "raw_members": {
                name: artifact(path, stage)
                for name, path in extracted.items()
            },
            "canonical_members": {
                path.name: artifact(
                    path,
                    stage,
                    row_count=int(
                        binding.member_map[path.name].row_count or 0
                    ),
                )
                for path in canonical_paths
            },
            "repair_sidecar": artifact(
                repair_path,
                stage,
                row_count=accumulator.repair_sidecar_rows,
            ),
            "member_accounting": artifact(
                accounting_path,
                stage,
                row_count=len(binding.csv_members),
            ),
            "parquet": artifact(
                parquet_path,
                stage,
                row_count=binding.expected_rows,
            ),
            "quality_summary": artifact(quality_path, stage),
            "source_binding": artifact(source_binding_path, stage),
            "query_sql": artifact(query_sql, stage),
        }
        catalog_after = catalog_hash(workspace)
        if catalog_after != catalog_before:
            raise IdxChgsmpMultiCsvError(
                "catalog changed during candidate execution"
            )
        parser_path = Path(__file__).resolve()
        parser_hash = sha256_file(parser_path)
        legacy_hash = sha256_file(LEGACY_PATH)
        engine_hash = sha256_file(legacy.ENGINE_PATH)
        content_binding = {
            "tool_id": TOOL_ID,
            "dataset_id": DATASET_ID,
            "package_id": binding.package_id,
            "run_id": run_id,
            "source": source,
            "parser_script_sha256": parser_hash,
            "legacy_adapter_sha256": legacy_hash,
            "shared_engine_sha256": engine_hash,
            "outputs": outputs,
            "member_metrics": member_metrics,
            "semantic_metrics": semantic,
            "parquet_roundtrip": parquet_roundtrip,
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
        }
        result = {
            "schema_version": 1,
            "manifest_kind": (
                "CSMAR_IDX_CHGSMP_MULTICSV_ZERO_LOSS_CANDIDATE"
            ),
            "tool_id": TOOL_ID,
            "status": "ZERO_LOSS_MULTICSV_CANDIDATE_MATERIALIZED",
            "write_performed": True,
            "created_at": utc_now(),
            "dataset_id": DATASET_ID,
            "table": "IDX_Chgsmp",
            "package_id": binding.package_id,
            "run_id": run_id,
            "candidate_path": target.as_posix(),
            "source": source,
            "parser": {
                "path": parser_path.as_posix(),
                "sha256": parser_hash,
                "legacy_adapter_path": LEGACY_PATH.resolve().as_posix(),
                "legacy_adapter_sha256": legacy_hash,
                "shared_engine_path": legacy.ENGINE_PATH.resolve().as_posix(),
                "shared_engine_sha256": engine_hash,
            },
            "outputs": outputs,
            "member_metrics": member_metrics,
            "semantic_metrics": semantic,
            "parquet_roundtrip": parquet_roundtrip,
            "content_hash_algorithm": (
                "sha256-canonical-json-csmar-idx-chgsmp-multicsv-zero-loss-v1"
            ),
            "content_hash": sha256_bytes(
                canonical_json_bytes(content_binding)
            ),
            "catalog_guard": {
                "path": (
                    workspace / "data" / "catalog" / "datasets.json"
                ).as_posix(),
                "sha256_before": catalog_before,
                "sha256_after": catalog_after,
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
                "source_zip_deleted": False,
            },
        }
        write_json(stage / "manifest.json", result)
        if catalog_hash(workspace) != catalog_before:
            raise IdxChgsmpMultiCsvError(
                "catalog changed before candidate seal"
            )
        os.replace(stage, target)
        return result
    except Exception:
        allowed_base = (
            workspace / "tmp" / OUTPUT_DIR_NAME
        ).resolve()
        if stage.exists() and is_below(stage.resolve(), allowed_base):
            shutil.rmtree(stage)
        raise


def validate_cross_package_zero_overlap(
    *,
    workspace_root: Path,
    candidate_a: Path,
    candidate_b: Path,
    validation_id: str,
) -> dict[str, Any]:
    workspace = workspace_root.resolve()
    require_safe_component(validation_id, "validation_id")
    allowed_base = (
        workspace / "tmp" / OUTPUT_DIR_NAME / "cross-package-validations"
    ).resolve()
    target = allowed_base / f"{validation_id}.json"
    if target.exists():
        raise IdxChgsmpMultiCsvError(
            f"cross-package validation already exists: {target}"
        )
    allowed_candidate_base = (
        workspace / "tmp" / OUTPUT_DIR_NAME / DATASET_ID
    ).resolve()
    roots = [
        candidate_a.resolve(strict=True),
        candidate_b.resolve(strict=True),
    ]
    if any(not is_below(root, allowed_candidate_base) for root in roots):
        raise IdxChgsmpMultiCsvError(
            "cross-package candidate escaped the dedicated tmp tree"
        )
    manifests = []
    parquets = []
    for root in roots:
        manifest = json.loads((root / "manifest.json").read_text("utf-8"))
        if (
            manifest.get("purpose_ceiling") != PURPOSE_CEILING
            and manifest.get("safety", {}).get("purpose_ceiling")
            != PURPOSE_CEILING
        ):
            raise IdxChgsmpMultiCsvError(
                f"candidate is not inspection-only: {root}"
            )
        parquet = root / manifest["outputs"]["parquet"]["relative_path"]
        if sha256_file(parquet) != manifest["outputs"]["parquet"]["sha256"]:
            raise IdxChgsmpMultiCsvError(
                f"candidate Parquet hash changed: {root}"
            )
        manifests.append(manifest)
        parquets.append(parquet)
    duckdb = engine.import_duckdb()
    connection = duckdb.connect(database=":memory:")
    columns = ", ".join(
        engine.sql_identifier(column) for column in legacy.CHGSMP_COLUMNS
    )
    join_clause = " AND ".join(
        f"a.{engine.sql_identifier(column)} IS NOT DISTINCT FROM "
        f"b.{engine.sql_identifier(column)}"
        for column in legacy.MECHANICAL_KEY
    )
    try:
        connection.execute(
            "CREATE VIEW a AS SELECT * FROM read_parquet("
            f"{engine.sql_string(parquets[0].as_posix())})"
        )
        connection.execute(
            "CREATE VIEW b AS SELECT * FROM read_parquet("
            f"{engine.sql_string(parquets[1].as_posix())})"
        )
        rows_a = int(connection.execute("SELECT COUNT(*) FROM a").fetchone()[0])
        rows_b = int(connection.execute("SELECT COUNT(*) FROM b").fetchone()[0])
        full_overlap = int(
            connection.execute(
                "SELECT COUNT(*) FROM ("
                f"(SELECT {columns} FROM a) INTERSECT ALL "
                f"(SELECT {columns} FROM b))"
            ).fetchone()[0]
        )
        key_overlap = int(
            connection.execute(
                f"SELECT COUNT(*) FROM a JOIN b ON {join_clause}"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    if full_overlap or key_overlap:
        raise IdxChgsmpMultiCsvError(
            "reviewed package windows overlap: "
            f"full_rows={full_overlap}, candidate_keys={key_overlap}"
        )
    result = {
        "schema_version": 1,
        "manifest_kind": "CSMAR_IDX_CHGSMP_CROSS_PACKAGE_ZERO_OVERLAP",
        "status": "PASS_ZERO_CROSS_PACKAGE_OVERLAP",
        "created_at": utc_now(),
        "validation_id": validation_id,
        "candidates": [
            {
                "candidate_path": root.as_posix(),
                "package_id": manifest["package_id"],
                "run_id": manifest["run_id"],
                "content_hash": manifest["content_hash"],
                "parquet_sha256": manifest["outputs"]["parquet"]["sha256"],
                "row_count": rows,
            }
            for root, manifest, rows in zip(
                roots, manifests, (rows_a, rows_b), strict=True
            )
        ],
        "full_row_overlap_count": full_overlap,
        "candidate_key_overlap_count": key_overlap,
        "candidate_key": list(legacy.MECHANICAL_KEY),
        "purpose_ceiling": PURPOSE_CEILING,
        "admission_decision": ADMISSION_DECISION,
        "catalog_registered": False,
        "research_ready": False,
    }
    result["content_hash"] = sha256_bytes(canonical_json_bytes(result))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(json_bytes(result))
    result["validation_path"] = target.as_posix()
    result["validation_sha256"] = sha256_file(target)
    return result


def default_downloads() -> Path:
    return Path.home() / "Downloads"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or materialize a bound multi-member CSMAR IDX_Chgsmp "
            "inspection candidate."
        )
    )
    parser.add_argument(
        "--package-id",
        required=True,
        choices=sorted(PACKAGE_BINDINGS),
    )
    parser.add_argument("--zip", required=True, type=Path, dest="zip_path")
    parser.add_argument(
        "--input-root",
        type=Path,
        default=default_downloads(),
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(
            workspace_root=WORKSPACE_ROOT,
            package_id=args.package_id,
            zip_path=args.zip_path,
            input_root=args.input_root,
            run_id=args.run_id,
            output_root=args.output_root,
            execute=bool(args.execute),
        )
    except (
        IdxChgsmpMultiCsvError,
        legacy.IdxChgsmpError,
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
