#!/usr/bin/env python3
"""Generate strict local-candidate requests for the reviewed CSMAR finance ZIPs.

This is deliberately narrow:
- exactly one ZIP and one CSV member per table;
- only the 12 reviewed financial-industry statements plus FIN_AuditStats;
- observed column count and data-row count must match the reviewed intake;
- FIN_Audit and FIN_AuditDetail are excluded because their CSV quoting is not
  valid for the strict v1 candidate builder.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import pathlib
import zipfile


TABLES = {
    "FS_ICombas.csv": ("csmar-fs-icombas", 92, 928),
    "FS_IComins.csv": ("csmar-fs-icomins", 66, 928),
    "FS_IComscfd.csv": ("csmar-fs-icomscfd", 55, 922),
    "FS_IComscfi.csv": ("csmar-fs-icomscfi", 38, 426),
    "FS_SCombas.csv": ("csmar-fs-scombas", 94, 5_238),
    "FS_SComins.csv": ("csmar-fs-scomins", 62, 5_238),
    "FS_SComscfd.csv": ("csmar-fs-scomscfd", 60, 5_209),
    "FS_SComscfi.csv": ("csmar-fs-scomscfi", 38, 2_807),
    "FS_BCombas.csv": ("csmar-fs-bcombas", 87, 4_795),
    "FS_BComins.csv": ("csmar-fs-bcomins", 57, 4_795),
    "FS_BComscfd.csv": ("csmar-fs-bcomscfd", 63, 4_755),
    "FS_BComscfi.csv": ("csmar-fs-bcomscfi", 38, 2_890),
    "FIN_AuditStats.csv": ("csmar-fin-auditstats", 9, 41_178),
}

FS_STRING_COLUMNS = {
    "Stkcd",
    "ShortName",
    "Typrep",
    "IfCorrect",
    "DeclareDate",
    "InstitutionID",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--intake-raw", type=pathlib.Path, required=True)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    return parser.parse_args()


def inspect_packages(intake_raw: pathlib.Path) -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = {}
    for package in sorted(intake_raw.glob("*.zip")):
        if not package.is_file():
            continue
        with zipfile.ZipFile(package) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise RuntimeError(f"CRC failure in {package.name}: {bad_member}")
            for member in archive.infolist():
                member_name = pathlib.PurePosixPath(member.filename).name
                if member_name not in TABLES:
                    continue
                if member_name in found:
                    raise RuntimeError(
                        f"More than one package contains {member_name}"
                    )
                with archive.open(member) as binary:
                    text = io.TextIOWrapper(
                        binary, encoding="utf-8-sig", errors="strict", newline=""
                    )
                    reader = csv.reader(text)
                    header = next(reader)
                    rows = sum(1 for _ in reader)
                found[member_name] = {
                    "package": package,
                    "member": member_name,
                    "header": header,
                    "rows": rows,
                    "unpacked_bytes": member.file_size,
                    "archive_member_count": len(archive.infolist()),
                }
    missing = sorted(set(TABLES) - set(found))
    extra = sorted(set(found) - set(TABLES))
    if missing or extra:
        raise RuntimeError(f"Target mismatch: missing={missing}, extra={extra}")
    return found


def build_request(
    member_name: str, evidence: dict[str, object]
) -> tuple[str, dict[str, object]]:
    dataset_id, expected_column_count, expected_rows = TABLES[member_name]
    package = evidence["package"]
    header = evidence["header"]
    rows = evidence["rows"]
    assert isinstance(package, pathlib.Path)
    assert isinstance(header, list)
    assert isinstance(rows, int)
    if len(header) != expected_column_count:
        raise RuntimeError(
            f"{member_name}: observed {len(header)} columns, "
            f"expected {expected_column_count}"
        )
    if rows != expected_rows:
        raise RuntimeError(
            f"{member_name}: observed {rows} rows, expected {expected_rows}"
        )

    if member_name == "FIN_AuditStats.csv":
        date_column = "Accper"
        key_columns = ["Stkcd", "Accper"]
        required_columns = ["Stkcd", "Accper", "AuditNo", "Format"]
        column_types = {
            column: (
                "DATE"
                if column == "Accper"
                else "INTEGER"
                if column == "AuditNo"
                else "VARCHAR"
            )
            for column in header
        }
        semantic_note = (
            "Accper is an accounting report period, not available_at. "
            "This summary table does not establish initial disclosure time, "
            "revision identity, supersedes links, or audit-text integrity."
        )
    else:
        date_column = "EndDate"
        key_columns = ["Stkcd", "EndDate", "Typrep"]
        required_columns = ["Stkcd", "EndDate", "Typrep", "IfCorrect"]
        column_types = {
            column: (
                "DATE"
                if column == "EndDate"
                else "VARCHAR"
                if column in FS_STRING_COLUMNS
                else "DECIMAL(20,2)"
            )
            for column in header
        }
        semantic_note = (
            "EndDate is an accounting report period, not available_at. "
            "DeclareDate is a correction-disclosure list and does not provide "
            "the initial publication timestamp or a complete revision chain."
        )

    run_id = f"csmar-33pkg-{dataset_id}-20260729-v1"
    schema_id = (
        f"{dataset_id}-observed-{expected_column_count}col-"
        "20260729-candidate-v1"
    )
    source_access_note = (
        "The user explicitly authorized this manually downloaded CSMAR package "
        "under institution-provided academic research access and authorized "
        "the 33-package transactional intake on 2026-07-29. The canonical ZIP "
        "was independently SHA-256 and CRC/decompression verified before this "
        f"request was generated. {semantic_note} This request authorizes only "
        "an isolated INSPECTION_ONLY/NOT_ADMITTED local candidate. It does not "
        "assert redistribution rights, formal source admission, semantic or "
        "PIT closure, quality promotion, catalog registration, or research "
        "readiness."
    )
    input_limit = max(package.stat().st_size * 2, package.stat().st_size + 1_048_576)
    unpacked_bytes = int(evidence["unpacked_bytes"])
    unpacked_limit = max(unpacked_bytes * 2, unpacked_bytes + 1_048_576)
    archive_member_count = int(evidence["archive_member_count"])
    request = {
        "schema_version": 1,
        "mode": "local_download_candidate",
        "dataset_id": dataset_id,
        "run_id": run_id,
        "source_id": "CSMAR_MANUAL_EXPORT",
        "dataset_schema_version": schema_id,
        "authorized_local_research_download": True,
        "source_access_note": source_access_note,
        "include_patterns": [package.name],
        "zip_member_patterns": [member_name],
        "table": {
            "table_name": dataset_id.replace("-", "_") + "_candidate",
            "expected_columns": header,
            "column_types": column_types,
            "date_column": date_column,
            "key_columns": key_columns,
            "required_columns": required_columns,
            "delimiter": ",",
            "encoding": "utf-8-sig",
            "allow_extra_columns": False,
            "partition_mode": "year_month",
        },
        "limits": {
            "max_packages": 1,
            "max_csv_sources": 1,
            "max_archive_members": archive_member_count,
            "max_total_input_bytes": input_limit,
            "max_total_unpacked_bytes": unpacked_limit,
            "max_compression_ratio": 1_000,
            "max_rows": expected_rows + 100,
        },
    }
    filename = f"{dataset_id}.csmar-33pkg-20260729.request.json"
    return filename, request


def main() -> int:
    args = parse_args()
    intake_raw = args.intake_raw.resolve(strict=True)
    output_dir = args.output_dir.resolve(strict=True)
    evidence = inspect_packages(intake_raw)
    written: list[str] = []
    for member_name in sorted(TABLES):
        filename, request = build_request(member_name, evidence[member_name])
        destination = output_dir / filename
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite {destination}")
        destination.write_text(
            json.dumps(request, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        written.append(str(destination))
    print(
        json.dumps(
            {
                "status": "GENERATED",
                "request_count": len(written),
                "requests": written,
                "excluded_strictly": ["FIN_Audit.csv", "FIN_AuditDetail.csv"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
