#!/usr/bin/env python3
"""Audit the sealed 2026-07-30 CSMAR IDX_Idxtrd intake.

This tool is deliberately inspection-only.  It reads the exact sealed intake,
extracts only ``IDX_Idxtrd*.csv`` members into a private staging directory,
loads every source field as VARCHAR, performs exact key/field accounting, and
publishes an isolated candidate plus a human-readable audit receipt.

It never edits catalog, registry, roadmap, snapshot, or published-data state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import duckdb


RUN_ID = "idx-idxtrd-intake-audit-20260730-v1"
INTAKE_RUN_ID = "intake-20260730-18src-12blob-idx-idxtrd-v1"
EXPECTED_INTAKE_MANIFEST_SHA256 = (
    "10439a5e5857fd9cf5c00c36aa2cd3d5fcfaac52bd11a67efff4e31f451eff23"
)
EXPECTED_COLUMNS = (
    "Indexcd",
    "Idxtrd01",
    "Idxtrd02",
    "Idxtrd03",
    "Idxtrd04",
    "Idxtrd05",
    "Idxtrd06",
    "Idxtrd07",
    "Idxtrd08",
    "Idxtrd09",
)
KEY_COLUMNS = EXPECTED_COLUMNS[:2]
VALUE_COLUMNS = EXPECTED_COLUMNS[2:]
MEMBER_PATTERN = re.compile(r"^IDX_Idxtrd[0-9]*\.csv$", re.IGNORECASE)
NULL_SENTINEL = "__CSMAR_NULL_SENTINEL_7F3F7A08_NOT_PRESENT__"

AUTHORITY_GUARDS = (
    "control/WORKSPACE_INDEX.md",
    "control/ROADMAP.md",
    "control/projects.yaml",
    "data/catalog/DATABASE_MANUAL.md",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    "data/catalog/datasets.json",
    "data/catalog/planned_datasets.json",
)
PROTECTED_TREES = (
    "data/market/snapshots",
    "data/published",
)


class AuditError(RuntimeError):
    """Fail-closed audit rejection."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("\\", "/").replace("'", "''") + "'"


def sql_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def is_below(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def has_reparse_ancestor(path: Path, stop: Path) -> bool:
    current = path.resolve(strict=False)
    stop = stop.resolve()
    while True:
        if current.exists():
            attrs = getattr(current.stat(), "st_file_attributes", 0)
            if current.is_symlink() or attrs & 0x400:
                return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


def require_output(path: Path, allowed_root: Path, workspace: Path) -> Path:
    resolved = path.resolve(strict=False)
    allowed = allowed_root.resolve()
    if not is_below(resolved, allowed):
        raise AuditError(f"output escaped allowed root: {resolved}")
    if has_reparse_ancestor(resolved.parent, workspace):
        raise AuditError(f"output path has a reparse-point ancestor: {resolved}")
    if resolved.exists():
        raise AuditError(f"refusing to overwrite existing output: {resolved}")
    return resolved


def validate_member_name(name: str) -> str:
    posix = PurePosixPath(name)
    if posix.is_absolute() or ".." in posix.parts or "\\" in name:
        raise AuditError(f"unsafe ZIP member path: {name!r}")
    if len(posix.parts) != 1:
        raise AuditError(f"IDX_Idxtrd CSV must be at ZIP root: {name!r}")
    if not MEMBER_PATTERN.fullmatch(posix.name):
        raise AuditError(f"unexpected IDX_Idxtrd member name: {name!r}")
    return posix.name


def validate_csv_header(header: Iterable[str]) -> str:
    fields = tuple(header)
    if fields == KEY_COLUMNS:
        return "KEY_ONLY_2"
    if fields == EXPECTED_COLUMNS:
        return "FULL_10"
    raise AuditError(
        "unexpected IDX_Idxtrd header: "
        f"expected {KEY_COLUMNS!r} or {EXPECTED_COLUMNS!r}, observed {fields!r}"
    )


def file_record(path: Path, root: Path | None = None) -> dict[str, Any]:
    item = path.stat()
    display = (
        path.resolve().relative_to(root.resolve()).as_posix()
        if root is not None and is_below(path, root)
        else path.resolve().as_posix()
    )
    return {
        "path": display,
        "bytes": item.st_size,
        "sha256": sha256_file(path),
    }


def file_record_for_final_path(path: Path, final_path: Path) -> dict[str, Any]:
    record = file_record(path)
    record["path"] = final_path.resolve(strict=False).as_posix()
    return record


def snapshot_guards(workspace: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for relative in AUTHORITY_GUARDS:
        path = workspace / relative
        if not path.is_file():
            raise AuditError(f"required authority guard is missing: {relative}")
        files[relative] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    trees: dict[str, Any] = {}
    for relative in PROTECTED_TREES:
        root = workspace / relative
        rows: list[dict[str, Any]] = []
        if root.exists():
            for path in sorted(p for p in root.rglob("*") if p.is_file()):
                rows.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
        trees[relative] = {
            "file_count": len(rows),
            "content_hash": sha256_bytes(canonical_json_bytes(rows)),
        }
    return {"files": files, "trees": trees}


def bind_intake(intake_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = intake_root / "INTAKE_MANIFEST.json"
    if not manifest_path.is_file():
        raise AuditError(f"missing intake manifest: {manifest_path}")
    manifest_hash = sha256_file(manifest_path)
    if manifest_hash != EXPECTED_INTAKE_MANIFEST_SHA256:
        raise AuditError(
            "intake manifest hash mismatch: "
            f"expected {EXPECTED_INTAKE_MANIFEST_SHA256}, observed {manifest_hash}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_flags = {
        "run_id": INTAKE_RUN_ID,
        "mutability": "immutable_after_seal",
        "purpose_ceiling": "INSPECTION_ONLY",
        "admission_decision": "NOT_ADMITTED",
        "catalog_registered": False,
        "research_ready": False,
        "unique_blob_count": 12,
    }
    for key, expected in expected_flags.items():
        if manifest.get(key) != expected:
            raise AuditError(
                f"intake boundary mismatch for {key}: "
                f"expected {expected!r}, observed {manifest.get(key)!r}"
            )
    blobs: list[dict[str, Any]] = []
    observed_hashes: set[str] = set()
    for blob in manifest.get("unique_blobs", []):
        if blob.get("kind") != "package":
            raise AuditError("this intake audit accepts package blobs only")
        relative = blob.get("destination_relative_path")
        if not isinstance(relative, str):
            raise AuditError("intake blob has no destination_relative_path")
        path = intake_root / Path(relative)
        if not path.is_file() or not is_below(path, intake_root):
            raise AuditError(f"sealed blob path is missing or escaped: {relative}")
        digest = sha256_file(path)
        if digest != blob.get("sha256") or path.stat().st_size != blob.get("bytes"):
            raise AuditError(f"sealed blob hash/size mismatch: {relative}")
        if digest in observed_hashes:
            raise AuditError(f"duplicate unique-blob hash in intake: {digest}")
        observed_hashes.add(digest)
        blobs.append(
            {
                "path": path,
                "relative_path": path.relative_to(intake_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest,
                "manifest_blob": blob,
            }
        )
    if len(blobs) != 12:
        raise AuditError(f"expected 12 sealed ZIP blobs, observed {len(blobs)}")
    return manifest, sorted(blobs, key=lambda item: item["relative_path"])


def stream_member_to_file(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with archive.open(info, "r") as source, destination.open("wb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            target.write(chunk)
            digest.update(chunk)
            total += len(chunk)
    if total != info.file_size:
        raise AuditError(
            f"ZIP member byte mismatch for {info.filename}: "
            f"expected {info.file_size}, observed {total}"
        )
    return total, digest.hexdigest()


def read_header(path: Path) -> tuple[tuple[str, ...], str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            header = tuple(next(reader))
    except (UnicodeDecodeError, StopIteration, csv.Error) as exc:
        raise AuditError(f"cannot parse UTF-8 CSV header for {path}: {exc}") from exc
    return header, validate_csv_header(header)


def extract_and_bind_members(
    blobs: list[dict[str, Any]], extract_root: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    members: list[dict[str, Any]] = []
    descriptors: list[dict[str, Any]] = []
    extract_root.mkdir(parents=True, exist_ok=False)
    for zip_ordinal, blob in enumerate(blobs):
        zip_path: Path = blob["path"]
        with zipfile.ZipFile(zip_path) as archive:
            failed = archive.testzip()
            if failed is not None:
                raise AuditError(f"ZIP CRC/read failure: {zip_path.name}:{failed}")
            csv_infos = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and MEMBER_PATTERN.fullmatch(PurePosixPath(info.filename).name)
            ]
            if not csv_infos:
                raise AuditError(f"ZIP contains no IDX_Idxtrd CSV: {zip_path.name}")
            seen_names: set[str] = set()
            for member_ordinal, info in enumerate(csv_infos):
                member_name = validate_member_name(info.filename)
                folded = member_name.casefold()
                if folded in seen_names:
                    raise AuditError(
                        f"duplicate IDX_Idxtrd member name in ZIP: {member_name}"
                    )
                seen_names.add(folded)
                destination = (
                    extract_root
                    / f"z{zip_ordinal:02d}_m{member_ordinal:02d}_{member_name}"
                )
                member_bytes, member_hash = stream_member_to_file(
                    archive, info, destination
                )
                header, schema_variant = read_header(destination)
                members.append(
                    {
                        "member_id": f"z{zip_ordinal:02d}m{member_ordinal:02d}",
                        "source_zip_path": blob["relative_path"],
                        "source_zip_filename": zip_path.name,
                        "source_zip_bytes": blob["bytes"],
                        "source_zip_sha256": blob["sha256"],
                        "member_name": member_name,
                        "member_bytes": member_bytes,
                        "member_sha256": member_hash,
                        "member_crc32": f"{info.CRC:08x}",
                        "header": list(header),
                        "schema_variant": schema_variant,
                        "extracted_path": destination,
                    }
                )
            for info in archive.infolist():
                if PurePosixPath(info.filename).name == "IDX_Idxtrd[DES][csv].txt":
                    with archive.open(info, "r") as source:
                        payload = source.read()
                    descriptors.append(
                        {
                            "source_zip_path": blob["relative_path"],
                            "source_zip_sha256": blob["sha256"],
                            "member_name": info.filename,
                            "bytes": len(payload),
                            "sha256": sha256_bytes(payload),
                            "crc32": f"{info.CRC:08x}",
                        }
                    )
    if len(members) != 24:
        raise AuditError(f"expected 24 IDX_Idxtrd CSV members, observed {len(members)}")
    return members, descriptors


def create_source_table(connection: duckdb.DuckDBPyConnection) -> None:
    data_fields = ",\n".join(
        f"{sql_ident(field)} VARCHAR NOT NULL" for field in EXPECTED_COLUMNS
    )
    connection.execute(
        f"""
        CREATE TABLE source_rows (
          {data_fields},
          _source_zip_path VARCHAR NOT NULL,
          _source_zip_sha256 VARCHAR NOT NULL,
          _source_member_id VARCHAR NOT NULL,
          _source_member_name VARCHAR NOT NULL,
          _source_member_sha256 VARCHAR NOT NULL,
          _schema_variant VARCHAR NOT NULL,
          _source_row_ordinal BIGINT NOT NULL
        )
        """
    )


def load_members(
    connection: duckdb.DuckDBPyConnection,
    members: list[dict[str, Any]],
) -> None:
    for member in members:
        path = member["extracted_path"]
        projection = [sql_ident(field) for field in member["header"]]
        if member["schema_variant"] == "KEY_ONLY_2":
            projection.extend(
                f"CAST('' AS VARCHAR) AS {sql_ident(field)}"
                for field in VALUE_COLUMNS
            )
        projection_sql = ",\n".join(projection)
        connection.execute(
            f"""
            INSERT INTO source_rows
            SELECT
              {projection_sql},
              {sql_string(member['source_zip_path'])},
              {sql_string(member['source_zip_sha256'])},
              {sql_string(member['member_id'])},
              {sql_string(member['member_name'])},
              {sql_string(member['member_sha256'])},
              {sql_string(member['schema_variant'])},
              row_number() OVER ()
            FROM read_csv(
              {sql_string(path.resolve())},
              header=true,
              all_varchar=true,
              nullstr={sql_string(NULL_SENTINEL)},
              strict_mode=true,
              ignore_errors=false
            )
            """
        )


def build_analysis_tables(connection: duckdb.DuckDBPyConnection) -> None:
    conflict_parts = []
    for field in VALUE_COLUMNS:
        qfield = sql_ident(field)
        conflict_parts.append(
            f"""
            SELECT
              Indexcd,
              Idxtrd01,
              {sql_string(field)} AS field,
              count(DISTINCT nullif({qfield}, '')) AS distinct_nonempty_values,
              min(nullif({qfield}, '')) AS min_value,
              max(nullif({qfield}, '')) AS max_value
            FROM source_rows
            GROUP BY Indexcd, Idxtrd01
            HAVING count(DISTINCT nullif({qfield}, '')) > 1
            """
        )
    connection.execute(
        "CREATE TABLE conflict_fields AS " + "\nUNION ALL\n".join(conflict_parts)
    )
    connection.execute(
        """
        CREATE TABLE conflict_keys AS
        SELECT DISTINCT Indexcd, Idxtrd01 FROM conflict_fields
        """
    )
    connection.execute(
        """
        CREATE TABLE key_stats AS
        SELECT
          Indexcd,
          Idxtrd01,
          count(*) AS source_row_count,
          count(DISTINCT _source_member_id) AS source_member_count,
          count(DISTINCT _source_zip_sha256) AS source_zip_count,
          count(*) FILTER (WHERE _schema_variant = 'KEY_ONLY_2') AS key_only_rows,
          count(*) FILTER (WHERE _schema_variant = 'FULL_10') AS full_rows
        FROM source_rows
        GROUP BY Indexcd, Idxtrd01
        """
    )
    merged_values = ",\n".join(
        f"coalesce(max(nullif({sql_ident(field)}, '')), '') AS {sql_ident(field)}"
        for field in VALUE_COLUMNS
    )
    connection.execute(
        f"""
        CREATE TABLE merged_rows AS
        SELECT
          s.Indexcd,
          s.Idxtrd01,
          {merged_values},
          count(*) AS source_row_count,
          count(DISTINCT s._source_member_id) AS source_member_count,
          count(DISTINCT s._source_zip_sha256) AS source_zip_count,
          count(*) FILTER (WHERE s._schema_variant = 'KEY_ONLY_2') AS key_only_rows,
          count(*) FILTER (WHERE s._schema_variant = 'FULL_10') AS full_rows
        FROM source_rows AS s
        WHERE NOT EXISTS (
          SELECT 1
          FROM conflict_keys AS c
          WHERE c.Indexcd = s.Indexcd AND c.Idxtrd01 = s.Idxtrd01
        )
        GROUP BY s.Indexcd, s.Idxtrd01
        """
    )
    connection.execute(
        """
        CREATE TABLE conflict_observations AS
        SELECT s.*
        FROM source_rows AS s
        INNER JOIN conflict_keys AS c
          ON c.Indexcd = s.Indexcd AND c.Idxtrd01 = s.Idxtrd01
        """
    )
    connection.execute(
        """
        CREATE TABLE source_key_zip AS
        SELECT DISTINCT Indexcd, Idxtrd01, _source_zip_sha256, _source_zip_path
        FROM source_rows
        """
    )


def scalar(connection: duckdb.DuckDBPyConnection, query: str) -> Any:
    return connection.execute(query).fetchone()[0]


def row_dicts(
    connection: duckdb.DuckDBPyConnection, query: str
) -> list[dict[str, Any]]:
    cursor = connection.execute(query)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def collect_metrics(
    connection: duckdb.DuckDBPyConnection,
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    invalid_key_rows = scalar(
        connection,
        """
        SELECT count(*) FROM source_rows
        WHERE Indexcd = '' OR Idxtrd01 = ''
        """,
    )
    invalid_date_rows = scalar(
        connection,
        """
        SELECT count(*) FROM source_rows
        WHERE NOT regexp_full_match(Idxtrd01, '[0-9]{4}-[0-9]{2}-[0-9]{2}')
           OR try_strptime(Idxtrd01, '%Y-%m-%d') IS NULL
        """,
    )
    exact = row_dicts(
        connection,
        f"""
        WITH grouped AS (
          SELECT {", ".join(sql_ident(field) for field in EXPECTED_COLUMNS)},
                 count(*) AS copies
          FROM source_rows
          GROUP BY {", ".join(sql_ident(field) for field in EXPECTED_COLUMNS)}
          HAVING count(*) > 1
        )
        SELECT
          count(*) AS exact_duplicate_groups,
          coalesce(sum(copies), 0) AS rows_in_exact_duplicate_groups,
          coalesce(sum(copies - 1), 0) AS exact_redundant_rows
        FROM grouped
        """,
    )[0]
    conflict_key_count = scalar(connection, "SELECT count(*) FROM conflict_keys")
    source_row_count = scalar(connection, "SELECT count(*) FROM source_rows")
    distinct_key_count = scalar(connection, "SELECT count(*) FROM key_stats")
    merged_row_count = scalar(connection, "SELECT count(*) FROM merged_rows")
    duplicate_key_groups = scalar(
        connection, "SELECT count(*) FROM key_stats WHERE source_row_count > 1"
    )
    duplicate_key_rows = scalar(
        connection,
        "SELECT coalesce(sum(source_row_count), 0) "
        "FROM key_stats WHERE source_row_count > 1",
    )
    compatible_duplicate_keys = scalar(
        connection,
        """
        SELECT count(*)
        FROM key_stats AS k
        WHERE k.source_row_count > 1
          AND NOT EXISTS (
            SELECT 1 FROM conflict_keys AS c
            WHERE c.Indexcd = k.Indexcd AND c.Idxtrd01 = k.Idxtrd01
          )
        """,
    )
    member_profiles = row_dicts(
        connection,
        """
        SELECT
          _source_member_id AS member_id,
          any_value(_source_zip_path) AS source_zip_path,
          any_value(_source_zip_sha256) AS source_zip_sha256,
          any_value(_source_member_name) AS member_name,
          any_value(_source_member_sha256) AS member_sha256,
          any_value(_schema_variant) AS schema_variant,
          count(*) AS row_count,
          count(DISTINCT Indexcd) AS distinct_index_codes,
          min(Idxtrd01) AS date_min,
          max(Idxtrd01) AS date_max,
          count(*) - count(DISTINCT (Indexcd, Idxtrd01)) AS duplicate_key_rows_beyond_first,
          count(*) FILTER (WHERE Indexcd = '' OR Idxtrd01 = '') AS blank_key_rows,
          count(*) FILTER (
            WHERE NOT regexp_full_match(Idxtrd01, '[0-9]{4}-[0-9]{2}-[0-9]{2}')
               OR try_strptime(Idxtrd01, '%Y-%m-%d') IS NULL
          ) AS invalid_date_rows
        FROM source_rows
        GROUP BY _source_member_id
        ORDER BY _source_member_id
        """,
    )
    profile_by_id = {row["member_id"]: row for row in member_profiles}
    for member in members:
        member.update(profile_by_id[member["member_id"]])
        member.pop("extracted_path", None)
    field_profiles = []
    for field in EXPECTED_COLUMNS:
        qfield = sql_ident(field)
        row = row_dicts(
            connection,
            f"""
            SELECT
              {sql_string(field)} AS field,
              count(*) AS nonconflict_key_rows,
              count(*) FILTER (WHERE {qfield} = '') AS empty_rows,
              count(*) FILTER (WHERE {qfield} <> '') AS nonempty_rows,
              count(DISTINCT nullif({qfield}, '')) AS distinct_nonempty_values
            FROM merged_rows
            """,
        )[0]
        field_profiles.append(row)
    overlaps = row_dicts(
        connection,
        """
        SELECT
          a._source_zip_sha256 AS left_zip_sha256,
          any_value(a._source_zip_path) AS left_zip_path,
          b._source_zip_sha256 AS right_zip_sha256,
          any_value(b._source_zip_path) AS right_zip_path,
          count(*) AS overlapping_keys
        FROM source_key_zip AS a
        INNER JOIN source_key_zip AS b
          ON a.Indexcd = b.Indexcd
         AND a.Idxtrd01 = b.Idxtrd01
         AND a._source_zip_sha256 < b._source_zip_sha256
        GROUP BY a._source_zip_sha256, b._source_zip_sha256
        ORDER BY overlapping_keys DESC, left_zip_sha256, right_zip_sha256
        """,
    )
    schema_profiles = row_dicts(
        connection,
        """
        SELECT
          _schema_variant AS schema_variant,
          count(*) AS source_rows,
          count(DISTINCT _source_member_id) AS member_count,
          count(DISTINCT _source_zip_sha256) AS zip_count
        FROM source_rows
        GROUP BY _schema_variant
        ORDER BY _schema_variant
        """,
    )
    coverage = row_dicts(
        connection,
        """
        SELECT
          min(Idxtrd01) AS date_min,
          max(Idxtrd01) AS date_max,
          count(DISTINCT Idxtrd01) AS distinct_dates,
          count(DISTINCT Indexcd) AS distinct_index_codes
        FROM key_stats
        """,
    )[0]
    key_only_only_keys = scalar(
        connection, "SELECT count(*) FROM key_stats WHERE full_rows = 0"
    )
    conflict_fields = row_dicts(
        connection,
        """
        SELECT field, count(*) AS conflicting_keys
        FROM conflict_fields
        GROUP BY field
        ORDER BY field
        """,
    )
    identity_collisions = row_dicts(
        connection,
        """
        SELECT
          Indexcd,
          count(DISTINCT Idxtrd09) AS distinct_nonempty_names,
          string_agg(DISTINCT Idxtrd09, ' | ' ORDER BY Idxtrd09) AS observed_names,
          min(Idxtrd01) AS date_min,
          max(Idxtrd01) AS date_max,
          count(DISTINCT Idxtrd01) AS observed_dates
        FROM source_rows
        WHERE Idxtrd09 <> ''
        GROUP BY Indexcd
        HAVING count(DISTINCT Idxtrd09) > 1
        ORDER BY Indexcd
        """,
    )
    return {
        "source_row_count": source_row_count,
        "distinct_candidate_key_count": distinct_key_count,
        "candidate_key": list(KEY_COLUMNS),
        "merged_nonconflict_row_count": merged_row_count,
        "conflict_key_count": conflict_key_count,
        "conflict_field_count": scalar(
            connection, "SELECT count(*) FROM conflict_fields"
        ),
        "conflict_fields": conflict_fields,
        "identity_collisions": identity_collisions,
        "candidate_key_unique_after_conflict_quarantine": True,
        "candidate_key_formal_identity_accepted": False,
        "formal_key_requirement": [
            "provider_or_exchange_namespace",
            "canonical_index_id",
            "trading_date",
        ],
        "invalid_key_rows": invalid_key_rows,
        "invalid_date_rows": invalid_date_rows,
        "duplicate_key_groups": duplicate_key_groups,
        "rows_in_duplicate_key_groups": duplicate_key_rows,
        "compatible_duplicate_key_groups": compatible_duplicate_keys,
        "key_only_without_full_observation_keys": key_only_only_keys,
        "exact_duplicates": exact,
        "coverage": coverage,
        "schema_profiles": schema_profiles,
        "field_profiles": field_profiles,
        "package_pair_overlaps": overlaps,
        "member_profiles": members,
        "accounting": {
            "distinct_keys_equal_merged_plus_conflicts": (
                distinct_key_count == merged_row_count + conflict_key_count
            ),
            "source_rows_equal_singletons_plus_duplicate_group_rows": (
                source_row_count
                == (distinct_key_count - duplicate_key_groups)
                + duplicate_key_rows
            ),
        },
    }


def copy_parquet(
    connection: duckdb.DuckDBPyConnection, query: str, path: Path
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection.execute(
        f"""
        COPY ({query})
        TO {sql_string(path.resolve())}
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )


def artifact(path: Path, root: Path, row_count: int | None = None) -> dict[str, Any]:
    result = {
        "relative_path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if row_count is not None:
        result["row_count"] = row_count
    return result


def render_readme(metrics: dict[str, Any], candidate_relative: str) -> str:
    coverage = metrics["coverage"]
    exact = metrics["exact_duplicates"]
    status = metrics["mechanical_status"]
    return f"""# IDX_Idxtrd sealed-intake mechanical audit

- Run: `{RUN_ID}`
- Status: `{status}`
- Intake: `{INTAKE_RUN_ID}`
- Purpose ceiling: `INSPECTION_ONLY`
- Source ZIP blobs: `12`
- CSV members: `24`
- Source rows: `{metrics['source_row_count']:,}`
- Distinct candidate keys: `{metrics['distinct_candidate_key_count']:,}`
- Non-conflicting deduplicated rows: `{metrics['merged_nonconflict_row_count']:,}`
- Conflicting keys quarantined: `{metrics['conflict_key_count']:,}`
- Exact redundant rows: `{exact['exact_redundant_rows']:,}`
- Index codes observed: `{coverage['distinct_index_codes']:,}`
- Date coverage observed: `{coverage['date_min']}` through `{coverage['date_max']}`
- Formal admission: `false`
- Research ready: `false`

## Method

Every sealed ZIP was re-hashed and fully CRC-read. All 24 root-level
`IDX_Idxtrd*.csv` members were parsed as UTF-8 CSV and accepted only with one
of two observed schemas: the explicit two-column key schema
`(Indexcd, Idxtrd01)` or the full ten-column source schema. All values were
loaded as text; empty source strings were not converted to zero.

The explicit inspection candidate key is `(Indexcd, Idxtrd01)`. Exact duplicate
rows were counted byte-semantically at the decoded field level. Repeated keys
were merged only when every non-empty field had one exact value. Keys with two
or more distinct non-empty values in any field were excluded from the merged
candidate and retained in conflict Parquet evidence.

## Important boundary

The two-column files prove only that a key was present in those exports. They
do not backfill price, volume, amount, return, or name fields. Absence of an
index/date observation is not labelled a data gap because this audit has no
official index-validity calendar. Field semantics, units, provider identity,
return variants, licence scope, PIT/revision history, and independent official
calibration remain outside this mechanical audit.

`Indexcd` is not accepted as a permanent identity namespace. Any observed
same-code/same-date field conflict is a hard identity blocker; formal data
requires a namespace-resolved `canonical_index_id + trading_date` key.

The isolated candidate is `{candidate_relative}`. It is not registered in
`datasets.json`, is not a formal snapshot, and must not be used for factor
research or backtesting.

## Evidence

- `AUDIT.json`: full row, field, member, overlap, duplicate, conflict, and
  coverage measurements.
- `VERIFICATION.json`: artifact hashes and authority/protected-tree guards.
- Candidate `manifest.json`: source-to-output bindings.
- Candidate `quality/summary.json`: fail-closed mechanical decision.
- Candidate `curated/idx_idxtrd_deduplicated.parquet`: non-conflicting,
  exact-deduplicated inspection rows.
- Candidate `quality/conflict_fields.parquet` and
  `quality/conflict_observations.parquet`: quarantined conflict evidence.
"""


def run(
    workspace: Path,
    intake_root: Path,
    candidate_output: Path,
    report_output: Path,
) -> dict[str, Any]:
    workspace = workspace.resolve(strict=True)
    expected_intake_base = (
        workspace / "data/market/quarantine/csmar-manual-export"
    ).resolve(strict=True)
    intake_root = intake_root.resolve(strict=True)
    if not is_below(intake_root, expected_intake_base):
        raise AuditError("intake escaped the controlled quarantine hierarchy")
    candidate_base = (workspace / "tmp/idx-idxtrd-intake-audit").resolve(
        strict=False
    )
    report_base = (workspace / "reports/data-governance").resolve(strict=True)
    candidate_target = require_output(
        candidate_output, candidate_base, workspace
    )
    report_target = require_output(report_output, report_base, workspace)
    candidate_target.parent.mkdir(parents=True, exist_ok=True)
    report_target.parent.mkdir(parents=True, exist_ok=True)

    guards_before = snapshot_guards(workspace)
    manifest, blobs = bind_intake(intake_root)
    input_before = {
        "manifest_sha256": sha256_file(intake_root / "INTAKE_MANIFEST.json"),
        "receipt_sha256": sha256_file(intake_root / "SOURCE_RETENTION_RECEIPT.json"),
        "blob_hashes": {item["relative_path"]: item["sha256"] for item in blobs},
    }

    token = uuid.uuid4().hex[:12]
    candidate_stage = candidate_target.parent / (
        f".{candidate_target.name}.staging-{token}"
    )
    report_stage = report_target.parent / f".{report_target.name}.staging-{token}"
    candidate_stage.mkdir()
    report_stage.mkdir()
    work = candidate_stage / ".work"
    extract_root = work / "extracted"
    work.mkdir()

    members, descriptors = extract_and_bind_members(blobs, extract_root)
    database = work / "audit.duckdb"
    connection = duckdb.connect(str(database))
    connection.execute("SET threads = 4")
    connection.execute("SET preserve_insertion_order = false")
    connection.execute(f"SET temp_directory = {sql_string((work / 'ducktmp').resolve())}")
    try:
        create_source_table(connection)
        load_members(connection, members)
        build_analysis_tables(connection)
        metrics = collect_metrics(connection, members)
        if metrics["invalid_key_rows"] or metrics["invalid_date_rows"]:
            mechanical_status = "FAILED__INVALID_KEY_OR_DATE_ROWS"
        elif not all(metrics["accounting"].values()):
            mechanical_status = "FAILED__ROW_ACCOUNTING_MISMATCH"
        elif metrics["conflict_key_count"]:
            mechanical_status = (
                "PASS_ACCOUNTING__FIELD_CONFLICTS_QUARANTINED__INSPECTION_ONLY"
            )
        else:
            mechanical_status = (
                "PASS_EXACT_DEDUP_NO_FIELD_CONFLICT__INSPECTION_ONLY"
            )
        metrics["mechanical_status"] = mechanical_status
        metrics["purpose_ceiling"] = "INSPECTION_ONLY"
        metrics["admission_decision"] = "NOT_ADMITTED"
        metrics["formal_admission"] = False
        metrics["research_ready"] = False
        metrics["descriptors"] = descriptors

        curated = candidate_stage / "curated/idx_idxtrd_deduplicated.parquet"
        conflicts = candidate_stage / "quality/conflict_fields.parquet"
        conflict_observations = (
            candidate_stage / "quality/conflict_observations.parquet"
        )
        copy_parquet(
            connection,
            """
            SELECT * FROM merged_rows ORDER BY Indexcd, Idxtrd01
            """,
            curated,
        )
        copy_parquet(
            connection,
            """
            SELECT * FROM conflict_fields ORDER BY Indexcd, Idxtrd01, field
            """,
            conflicts,
        )
        copy_parquet(
            connection,
            """
            SELECT * FROM conflict_observations
            ORDER BY Indexcd, Idxtrd01, _source_zip_sha256, _source_member_id,
                     _source_row_ordinal
            """,
            conflict_observations,
        )
        roundtrip = {
            "deduplicated_rows": scalar(
                connection,
                f"SELECT count(*) FROM read_parquet({sql_string(curated.resolve())})",
            ),
            "deduplicated_duplicate_keys": scalar(
                connection,
                f"""
                SELECT count(*) FROM (
                  SELECT Indexcd, Idxtrd01
                  FROM read_parquet({sql_string(curated.resolve())})
                  GROUP BY Indexcd, Idxtrd01
                  HAVING count(*) > 1
                )
                """,
            ),
            "conflict_field_rows": scalar(
                connection,
                f"SELECT count(*) FROM read_parquet({sql_string(conflicts.resolve())})",
            ),
            "conflict_observation_rows": scalar(
                connection,
                "SELECT count(*) FROM read_parquet"
                f"({sql_string(conflict_observations.resolve())})",
            ),
        }
    finally:
        connection.close()

    if (
        roundtrip["deduplicated_rows"]
        != metrics["merged_nonconflict_row_count"]
        or roundtrip["deduplicated_duplicate_keys"] != 0
        or roundtrip["conflict_field_rows"] != metrics["conflict_field_count"]
    ):
        raise AuditError(f"Parquet roundtrip mismatch: {roundtrip}")

    inventory_path = candidate_stage / "inventory/members.json"
    quality_path = candidate_stage / "quality/summary.json"
    query_path = candidate_stage / "query.sql"
    write_json(
        inventory_path,
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "intake_run_id": INTAKE_RUN_ID,
            "members": metrics["member_profiles"],
            "descriptors": descriptors,
        },
    )
    quality = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "mechanical_status": metrics["mechanical_status"],
        "purpose_ceiling": "INSPECTION_ONLY",
        "admission_decision": "NOT_ADMITTED",
        "formal_admission": False,
        "catalog_registered": False,
        "quality_promoted": False,
        "research_ready": False,
        "candidate_key": list(KEY_COLUMNS),
        "source_row_count": metrics["source_row_count"],
        "distinct_candidate_key_count": metrics["distinct_candidate_key_count"],
        "merged_nonconflict_row_count": metrics["merged_nonconflict_row_count"],
        "conflict_key_count": metrics["conflict_key_count"],
        "conflict_field_count": metrics["conflict_field_count"],
        "invalid_key_rows": metrics["invalid_key_rows"],
        "invalid_date_rows": metrics["invalid_date_rows"],
        "duplicate_key_groups": metrics["duplicate_key_groups"],
        "exact_duplicates": metrics["exact_duplicates"],
        "coverage": metrics["coverage"],
        "schema_profiles": metrics["schema_profiles"],
        "identity_collisions": metrics["identity_collisions"],
        "candidate_key_formal_identity_accepted": False,
        "formal_key_requirement": metrics["formal_key_requirement"],
        "known_gaps": [
            "two key-only members contain no value fields",
            "Indexcd is not a namespace-resolved permanent index identity",
            "official index validity calendar is not available in this audit",
            "provider identity, return variant, currency and methodology version are not closed",
            "field semantics, units, licence, PIT and revision history are not formally admitted",
            "independent official cross-source calibration is not performed",
        ],
    }
    write_json(quality_path, quality)
    query_path.write_text(
        "-- Inspection only. Do not use this candidate for research.\n"
        "SELECT *\n"
        "FROM read_parquet('curated/idx_idxtrd_deduplicated.parquet')\n"
        "ORDER BY Indexcd, Idxtrd01;\n",
        encoding="utf-8",
    )

    parser_path = Path(__file__).resolve()
    outputs = {
        "deduplicated_parquet": artifact(
            curated, candidate_stage, metrics["merged_nonconflict_row_count"]
        ),
        "conflict_fields_parquet": artifact(
            conflicts, candidate_stage, metrics["conflict_field_count"]
        ),
        "conflict_observations_parquet": artifact(
            conflict_observations,
            candidate_stage,
            roundtrip["conflict_observation_rows"],
        ),
        "member_inventory": artifact(inventory_path, candidate_stage),
        "quality_summary": artifact(quality_path, candidate_stage),
        "query_sql": artifact(query_path, candidate_stage),
    }
    candidate_manifest = {
        "schema_version": 1,
        "manifest_kind": "CSMAR_IDX_IDXTRD_SEALED_INTAKE_AUDIT_CANDIDATE",
        "run_id": RUN_ID,
        "dataset_id": "csmar-idx-idxtrd-intake-audit",
        "lifecycle": "candidate",
        "mutability": "immutable_after_seal",
        "purpose_ceiling": "INSPECTION_ONLY",
        "admission_decision": "NOT_ADMITTED",
        "catalog_registered": False,
        "formal_admission": False,
        "research_ready": False,
        "candidate_path": candidate_target.as_posix(),
        "intake": {
            "path": intake_root.relative_to(workspace).as_posix(),
            "run_id": manifest["run_id"],
            "manifest_sha256": EXPECTED_INTAKE_MANIFEST_SHA256,
            "zip_blob_count": len(blobs),
            "zip_blobs": [
                {
                    "relative_path": item["relative_path"],
                    "bytes": item["bytes"],
                    "sha256": item["sha256"],
                }
                for item in blobs
            ],
        },
        "parser": {
            "path": parser_path.relative_to(workspace).as_posix(),
            "sha256": sha256_file(parser_path),
            "python": os.sys.version,
            "duckdb": duckdb.__version__,
        },
        "mechanical_status": metrics["mechanical_status"],
        "candidate_key": list(KEY_COLUMNS),
        "metrics": {
            key: value
            for key, value in metrics.items()
            if key
            not in {
                "member_profiles",
                "package_pair_overlaps",
                "field_profiles",
                "descriptors",
            }
        },
        "outputs": outputs,
        "catalog_guard": {
            "write_authorized": False,
            "datasets_json_sha256_before": guards_before["files"][
                "data/catalog/datasets.json"
            ]["sha256"],
        },
    }
    candidate_manifest["content_hash"] = sha256_bytes(
        canonical_json_bytes(candidate_manifest)
    )
    manifest_path = candidate_stage / "manifest.json"
    write_json(manifest_path, candidate_manifest)

    # The work database and extracted staging members are reproducible from the
    # sealed intake and are intentionally not part of the candidate.
    shutil.rmtree(work)

    candidate_relative = candidate_target.relative_to(workspace).as_posix()
    audit = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": metrics["mechanical_status"],
        "scope": "SEALED_INTAKE_MECHANICAL_AUDIT_ONLY",
        "purpose_ceiling": "INSPECTION_ONLY",
        "admission_decision": "NOT_ADMITTED",
        "formal_admission": False,
        "quality_promoted": False,
        "research_ready": False,
        "candidate_path": candidate_relative,
        "intake_manifest_sha256": EXPECTED_INTAKE_MANIFEST_SHA256,
        "metrics": metrics,
        "roundtrip": roundtrip,
        "candidate_manifest": {
            "path": f"{candidate_relative}/manifest.json",
            "bytes": manifest_path.stat().st_size,
            "sha256": sha256_file(manifest_path),
        },
    }
    audit_path = report_stage / "AUDIT.json"
    readme_path = report_stage / "README.md"
    write_json(audit_path, audit)
    readme_path.write_text(
        render_readme(metrics, candidate_relative), encoding="utf-8"
    )

    guards_after = snapshot_guards(workspace)
    input_after = {
        "manifest_sha256": sha256_file(intake_root / "INTAKE_MANIFEST.json"),
        "receipt_sha256": sha256_file(intake_root / "SOURCE_RETENTION_RECEIPT.json"),
        "blob_hashes": {
            item["relative_path"]: sha256_file(item["path"]) for item in blobs
        },
    }
    if guards_after != guards_before:
        raise AuditError("authority or protected-tree guard changed during audit")
    if input_after != input_before:
        raise AuditError("sealed intake changed during audit")
    verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": metrics["mechanical_status"],
        "checks": {
            "sealed_intake_manifest_hash_match": True,
            "zip_blob_count_12": len(blobs) == 12,
            "csv_member_count_24": len(members) == 24,
            "all_zip_crc_reads_pass": True,
            "all_csv_headers_allowed": True,
            "invalid_key_rows_zero": metrics["invalid_key_rows"] == 0,
            "invalid_date_rows_zero": metrics["invalid_date_rows"] == 0,
            "row_accounting_pass": all(metrics["accounting"].values()),
            "parquet_roundtrip_pass": True,
            "deduplicated_candidate_key_unique": (
                roundtrip["deduplicated_duplicate_keys"] == 0
            ),
            "authority_guards_unchanged": guards_after == guards_before,
            "protected_trees_unchanged": guards_after["trees"]
            == guards_before["trees"],
            "sealed_intake_unchanged": input_after == input_before,
            "formal_admission_false": True,
            "research_ready_false": True,
        },
        "guards_before_after": {
            "before": guards_before,
            "after": guards_after,
        },
        "sealed_intake_before_after": {
            "before": input_before,
            "after": input_after,
        },
        "artifacts": {
            "report_readme": file_record_for_final_path(
                readme_path, report_target / "README.md"
            ),
            "report_audit": file_record_for_final_path(
                audit_path, report_target / "AUDIT.json"
            ),
            "candidate_manifest": file_record_for_final_path(
                manifest_path, candidate_target / "manifest.json"
            ),
            **outputs,
        },
    }
    verification_path = report_stage / "VERIFICATION.json"
    write_json(verification_path, verification)

    candidate_stage.rename(candidate_target)
    report_stage.rename(report_target)
    return {
        "run_id": RUN_ID,
        "status": metrics["mechanical_status"],
        "candidate": candidate_target.as_posix(),
        "report": report_target.as_posix(),
        "source_rows": metrics["source_row_count"],
        "distinct_keys": metrics["distinct_candidate_key_count"],
        "deduplicated_rows": metrics["merged_nonconflict_row_count"],
        "conflict_keys": metrics["conflict_key_count"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=Path("."))
    parser.add_argument(
        "--intake-root",
        type=Path,
        default=Path(
            "data/market/quarantine/csmar-manual-export/"
            "intake-20260730-18src-12blob-idx-idxtrd-v1"
        ),
    )
    parser.add_argument(
        "--candidate-output",
        type=Path,
        default=Path("tmp/idx-idxtrd-intake-audit") / RUN_ID,
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        default=Path("reports/data-governance") / RUN_ID,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = args.workspace_root.resolve(strict=True)

    def under_workspace(value: Path) -> Path:
        return value if value.is_absolute() else workspace / value

    try:
        result = run(
            workspace=workspace,
            intake_root=under_workspace(args.intake_root),
            candidate_output=under_workspace(args.candidate_output),
            report_output=under_workspace(args.report_output),
        )
    except (AuditError, OSError, ValueError, zipfile.BadZipFile, duckdb.Error) as exc:
        raise SystemExit(f"IDX_Idxtrd audit failed closed: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
