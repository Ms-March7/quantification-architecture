#!/usr/bin/env python3
"""Exhaustively audit TRD_Dalyr NULL/zero encodings without changing admission.

The audit consumes only hash-bound local candidate partitions and the already
sealed CSMAR TRD manual evidence.  It deliberately distinguishes:

* what the versioned manual states;
* what every observed candidate row actually contains; and
* what still cannot be proved from local values alone.

It never mutates the catalog, source registry, Roadmap, quality state, formal
snapshots, or research authorization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb


RUN_ID = "trd-dalyr-null-zero-semantics-audit-20260730-v2"
DEFAULT_MEASUREMENTS = (
    "reports/data-governance/"
    "core01-02-03-06-local-evidence-closure-20260730-v1/"
    "MEASUREMENTS.json"
)
DEFAULT_EVIDENCE_MAP = (
    "reports/data-governance/"
    "csmar-intake-supplemental-evidence-20260730-v1/"
    "EVIDENCE_MAP.json"
)
DEFAULT_OUTPUT = (
    "reports/data-governance/"
    "trd-dalyr-null-zero-semantics-audit-20260730-v2"
)
PROTECTED_FILES = {
    "datasets_json": "data/catalog/datasets.json",
    "source_registry": (
        "data/catalog/contracts/source_admission_v1/"
        "current_source_registry.json"
    ),
    "roadmap": "control/ROADMAP.md",
    "projects": "control/projects.yaml",
}
FORMAL_ROOTS = {
    "market_snapshots": "data/market/snapshots",
    "published": "data/published",
}
REPORT_FILES = {
    "FORMAL_STATE_GUARDS.json",
    "KEY_CONFLICTS.json",
    "README.md",
    "REPORT_MANIFEST.json",
    "SEMANTICS_MATRIX.json",
    "VERIFICATION.json",
}
DATA_FIELDS = [
    "Stkcd",
    "Trddt",
    "Opnprc",
    "Hiprc",
    "Loprc",
    "Clsprc",
    "Dnshrtrd",
    "Dnvaltrd",
    "Dsmvosd",
    "Dsmvtll",
    "Dretwd",
    "Dretnd",
    "Adjprcwd",
    "Adjprcnd",
    "Markettype",
    "Capchgdt",
    "Trdsta",
    "Ahshrtrd_D",
    "Ahvaltrd_D",
    "PreClosePrice",
    "ChangeRatio",
    "LimitDown",
    "LimitUp",
    "LimitStatus",
]
SEMANTIC_FIELDS = [
    "Opnprc",
    "Hiprc",
    "Loprc",
    "Clsprc",
    "Dnshrtrd",
    "Dnvaltrd",
    "Ahshrtrd_D",
    "Ahvaltrd_D",
    "PreClosePrice",
    "LimitDown",
    "LimitUp",
]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_ref(workspace: Path, path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    before = resolved.stat()
    digest = sha256(resolved)
    after = resolved.stat()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise RuntimeError(f"file changed while hashing: {resolved}")
    return {
        "path": resolved.relative_to(workspace.resolve()).as_posix(),
        "bytes": after.st_size,
        "sha256": digest,
    }


def is_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def tree_ref(workspace: Path, relative_root: str) -> dict[str, Any]:
    root = (workspace / relative_root).resolve()
    if not root.exists():
        return {
            "path": relative_root,
            "exists": False,
            "file_count": 0,
            "total_bytes": 0,
            "tree_sha256": hashlib.sha256(b"").hexdigest(),
        }
    if is_reparse(root):
        raise RuntimeError(f"formal root is a reparse point: {root}")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if is_reparse(path):
            raise RuntimeError(f"reparse point below formal root: {path}")
        if path.is_file():
            ref = file_ref(workspace, path)
            entries.append(ref)
    canonical = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "path": relative_root,
        "exists": True,
        "file_count": len(entries),
        "total_bytes": sum(item["bytes"] for item in entries),
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def formal_guards(workspace: Path) -> dict[str, Any]:
    return {
        "authority_files": {
            key: file_ref(workspace, workspace / relative)
            for key, relative in PROTECTED_FILES.items()
        },
        "formal_roots": {
            key: tree_ref(workspace, relative)
            for key, relative in FORMAL_ROOTS.items()
        },
    }


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def verify_inputs(
    workspace: Path,
    measurements_path: Path,
    evidence_map_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    measurements_ref = file_ref(workspace, measurements_path)
    evidence_map_ref = file_ref(workspace, evidence_map_path)
    measurements = read_json(measurements_path)
    evidence_map = read_json(evidence_map_path)

    candidates: list[dict[str, Any]] = []
    bindings = measurements.get("candidate_bindings", {})
    for name in sorted(bindings):
        if not name.startswith("TRD_Dalyr_"):
            continue
        binding = bindings[name]
        manifest_ref = binding.get("manifest", {})
        quality_ref = binding.get("quality", {})
        for label, expected in (
            ("manifest", manifest_ref),
            ("quality", quality_ref),
        ):
            actual = file_ref(workspace, workspace / expected["path"])
            if (
                actual["bytes"] != expected["bytes"]
                or actual["sha256"] != expected["sha256"]
            ):
                raise RuntimeError(
                    f"{name} {label} binding mismatch: "
                    f"expected={expected}, actual={actual}"
                )

        entries = binding.get("curated_tree", {}).get("entries", [])
        if not entries:
            raise RuntimeError(f"no curated partitions bound for {name}")
        verified_entries: list[dict[str, Any]] = []
        for expected in entries:
            actual = file_ref(workspace, workspace / expected["path"])
            if (
                actual["bytes"] != expected["bytes"]
                or actual["sha256"] != expected["sha256"]
            ):
                raise RuntimeError(
                    f"{name} partition binding mismatch: "
                    f"expected={expected}, actual={actual}"
                )
            verified_entries.append(actual)
        candidates.append(
            {
                "candidate_id": name,
                "manifest": manifest_ref,
                "quality": quality_ref,
                "partitions": verified_entries,
            }
        )

    if not candidates:
        raise RuntimeError("no TRD_Dalyr candidate bindings found")

    trd_manual = None
    for item in evidence_map.get("unique_blobs", []):
        if item.get("blob_id") == "PDF_CSMAR_TRD_20250616":
            trd_manual = item
            break
    if trd_manual is None:
        raise RuntimeError("versioned TRD manual evidence is missing")
    actual_manual = file_ref(workspace, workspace / trd_manual["path"])
    if (
        actual_manual["bytes"] != trd_manual["bytes"]
        or actual_manual["sha256"] != trd_manual["sha256"]
    ):
        raise RuntimeError(
            "TRD manual binding mismatch: "
            f"expected={trd_manual}, actual={actual_manual}"
        )

    source_refs = {
        "measurements": measurements_ref,
        "evidence_map": evidence_map_ref,
        "trd_manual": actual_manual,
    }
    return candidates, source_refs, trd_manual


def create_raw_table(
    connection: duckdb.DuckDBPyConnection,
    candidates: list[dict[str, Any]],
    workspace: Path,
) -> None:
    selects: list[str] = []
    fields = ", ".join(f'"{field}"' for field in DATA_FIELDS)
    for candidate in candidates:
        paths = [
            (workspace / item["path"]).resolve().as_posix()
            for item in candidate["partitions"]
        ]
        path_list = ", ".join(sql_quote(path) for path in paths)
        candidate_id = sql_quote(candidate["candidate_id"])
        selects.append(
            f"SELECT {candidate_id} AS __candidate_id, {fields} "
            f"FROM read_parquet([{path_list}], "
            "union_by_name=true, hive_partitioning=false)"
        )
    connection.execute("CREATE TEMP TABLE raw AS " + " UNION ALL ".join(selects))

    actual_columns = [
        row[0]
        for row in connection.execute(
            "DESCRIBE SELECT * EXCLUDE (__candidate_id) FROM raw"
        ).fetchall()
    ]
    if actual_columns != DATA_FIELDS:
        raise RuntimeError(
            f"unexpected normalized columns: {actual_columns!r}"
        )


def build_audit(
    connection: duckdb.DuckDBPyConnection,
) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = ", ".join(f'"{field}"' for field in DATA_FIELDS)
    connection.execute(
        f"CREATE TEMP TABLE exact_unique AS SELECT DISTINCT {fields} FROM raw"
    )
    connection.execute(
        """
        CREATE TEMP TABLE key_versions AS
        SELECT Stkcd, Trddt, COUNT(*) AS distinct_versions
        FROM exact_unique
        GROUP BY Stkcd, Trddt
        """
    )
    connection.execute(
        """
        CREATE TEMP VIEW clean AS
        SELECT value.*
        FROM exact_unique AS value
        JOIN key_versions AS key_info USING (Stkcd, Trddt)
        WHERE key_info.distinct_versions = 1
        """
    )

    raw_rows = connection.execute("SELECT COUNT(*) FROM raw").fetchone()[0]
    exact_unique_rows = connection.execute(
        "SELECT COUNT(*) FROM exact_unique"
    ).fetchone()[0]
    clean_rows = connection.execute("SELECT COUNT(*) FROM clean").fetchone()[0]
    conflict_keys = connection.execute(
        "SELECT COUNT(*) FROM key_versions WHERE distinct_versions > 1"
    ).fetchone()[0]
    distinct_keys = connection.execute(
        "SELECT COUNT(*) FROM key_versions"
    ).fetchone()[0]
    date_min, date_max, code_count, date_count = connection.execute(
        """
        SELECT
          MIN(Trddt),
          MAX(Trddt),
          COUNT(DISTINCT Stkcd),
          COUNT(DISTINCT Trddt)
        FROM clean
        """
    ).fetchone()

    conflict_rows = connection.execute(
        """
        SELECT Stkcd, Trddt, distinct_versions
        FROM key_versions
        WHERE distinct_versions > 1
        ORDER BY Trddt, Stkcd
        LIMIT 100
        """
    ).fetchall()
    conflicts = {
        "raw_rows": raw_rows,
        "exact_unique_rows": exact_unique_rows,
        "exact_duplicate_rows_removed": raw_rows - exact_unique_rows,
        "distinct_keys": distinct_keys,
        "conflict_key_count": conflict_keys,
        "clean_nonconflicting_rows": clean_rows,
        "conflict_examples": [
            {
                "Stkcd": row[0],
                "Trddt": row[1].isoformat(),
                "distinct_versions": row[2],
            }
            for row in conflict_rows
        ],
        "conflict_policy": (
            "All keys with more than one distinct 24-field row are excluded "
            "from semantic conclusions; no source is silently preferred."
        ),
    }

    aggregate_expressions = ["COUNT(*) AS rows"]
    for field in SEMANTIC_FIELDS:
        aggregate_expressions.extend(
            [
                f'COUNT(*) FILTER (WHERE "{field}" IS NULL) '
                f'AS "{field}__null"',
                f'COUNT(*) FILTER (WHERE "{field}" = 0) '
                f'AS "{field}__zero"',
                f'COUNT(*) FILTER (WHERE "{field}" < 0) '
                f'AS "{field}__negative"',
                f'COUNT(*) FILTER (WHERE "{field}" > 0) '
                f'AS "{field}__positive"',
            ]
        )
    aggregation_sql = ", ".join(aggregate_expressions)
    status_cursor = connection.execute(
        f"""
        SELECT
          COALESCE(CAST(Trdsta AS VARCHAR), '__NULL__') AS status,
          {aggregation_sql},
          COUNT(*) FILTER (
            WHERE Opnprc IS NULL
              AND Hiprc IS NULL
              AND Loprc IS NULL
              AND Clsprc IS NULL
          ) AS all_ohlc_null,
          COUNT(*) FILTER (
            WHERE Opnprc IS NOT NULL
              AND Hiprc IS NOT NULL
              AND Loprc IS NOT NULL
              AND Clsprc IS NOT NULL
          ) AS all_ohlc_present,
          COUNT(*) FILTER (
            WHERE (Opnprc IS NULL OR Hiprc IS NULL
                   OR Loprc IS NULL OR Clsprc IS NULL)
              AND NOT (Opnprc IS NULL AND Hiprc IS NULL
                       AND Loprc IS NULL AND Clsprc IS NULL)
          ) AS partial_ohlc_null
        FROM clean
        GROUP BY status
        ORDER BY
          CASE WHEN status = '__NULL__' THEN 999 ELSE CAST(status AS INTEGER) END
        """
    )
    columns = [
        description[0] for description in status_cursor.description
    ]
    status_rows = status_cursor.fetchall()
    by_status = [
        {
            key: (
                value.isoformat()
                if hasattr(value, "isoformat")
                else value
            )
            for key, value in zip(columns, row)
        }
        for row in status_rows
    ]

    consistency_columns = [
        "price_zero_rows",
        "volume_zero_amount_nonzero_rows",
        "volume_nonzero_amount_zero_rows",
        "volume_null_amount_zero_rows",
        "volume_zero_amount_null_rows",
        "negative_volume_rows",
        "negative_amount_rows",
        "invalid_trade_status_rows",
        "null_trade_status_rows",
    ]
    consistency_values = connection.execute(
        """
        SELECT
          COUNT(*) FILTER (
            WHERE Opnprc = 0 OR Hiprc = 0 OR Loprc = 0 OR Clsprc = 0
          ),
          COUNT(*) FILTER (
            WHERE Dnshrtrd = 0 AND Dnvaltrd IS NOT NULL AND Dnvaltrd <> 0
          ),
          COUNT(*) FILTER (
            WHERE Dnshrtrd IS NOT NULL AND Dnshrtrd <> 0 AND Dnvaltrd = 0
          ),
          COUNT(*) FILTER (
            WHERE Dnshrtrd IS NULL AND Dnvaltrd = 0
          ),
          COUNT(*) FILTER (
            WHERE Dnshrtrd = 0 AND Dnvaltrd IS NULL
          ),
          COUNT(*) FILTER (WHERE Dnshrtrd < 0),
          COUNT(*) FILTER (WHERE Dnvaltrd < 0),
          COUNT(*) FILTER (
            WHERE Trdsta IS NOT NULL AND (Trdsta < 1 OR Trdsta > 16)
          ),
          COUNT(*) FILTER (WHERE Trdsta IS NULL)
        FROM clean
        """
    ).fetchone()
    consistency = dict(zip(consistency_columns, consistency_values))
    inconsistency_rows = connection.execute(
        """
        SELECT
          Stkcd,
          Trddt,
          Trdsta,
          Dnshrtrd,
          Dnvaltrd,
          Ahshrtrd_D,
          Ahvaltrd_D
        FROM clean
        WHERE (Dnshrtrd = 0 AND Dnvaltrd IS NOT NULL AND Dnvaltrd <> 0)
           OR (Dnshrtrd IS NOT NULL AND Dnshrtrd <> 0 AND Dnvaltrd = 0)
           OR (Dnshrtrd IS NULL AND Dnvaltrd = 0)
           OR (Dnshrtrd = 0 AND Dnvaltrd IS NULL)
        ORDER BY Trddt, Stkcd
        LIMIT 100
        """
    ).fetchall()
    consistency["volume_amount_inconsistency_examples"] = [
        {
            "Stkcd": row[0],
            "Trddt": row[1].isoformat(),
            "Trdsta": row[2],
            "Dnshrtrd": row[3],
            "Dnvaltrd": row[4],
            "Ahshrtrd_D": row[5],
            "Ahvaltrd_D": row[6],
        }
        for row in inconsistency_rows
    ]

    status_values = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT Trdsta FROM clean WHERE Trdsta IS NOT NULL"
        ).fetchall()
    }
    observed_statuses_with_complete_vendor_enum = (
        bool(status_values)
        and status_values.issubset(set(range(1, 17)))
        and consistency["invalid_trade_status_rows"] == 0
    )
    no_price_zero_observed = consistency["price_zero_rows"] == 0
    volume_amount_zero_pair_consistent = (
        consistency["volume_zero_amount_nonzero_rows"] == 0
        and consistency["volume_nonzero_amount_zero_rows"] == 0
        and consistency["volume_null_amount_zero_rows"] == 0
        and consistency["volume_zero_amount_null_rows"] == 0
    )

    semantics = {
        "scope": {
            "date_min": date_min.isoformat() if date_min else None,
            "date_max": date_max.isoformat() if date_max else None,
            "security_codes": code_count,
            "trading_dates": date_count,
            "rows_excluding_conflicting_keys": clean_rows,
            "observed_trade_status_values": sorted(status_values),
        },
        "manual_claims": {
            "manual_version": "2025-06-16",
            "trdsta": (
                "Physical page 24 / printed page 20 documents the "
                "TRD_Dalyr Trdsta enumeration 1-16."
            ),
            "Dnshrtrd_zero": "0 means no trading volume.",
            "Dnvaltrd_zero": "0 means no trading amount.",
            "boundary": (
                "The manual does not state that every historical unknown "
                "value is never represented as zero, and TRDNEW Filling "
                "semantics are not transferred to TRD_Dalyr."
            ),
        },
        "by_trade_status": by_status,
        "global_consistency_checks": consistency,
        "mechanical_findings": {
            "observed_statuses_within_documented_enum": (
                observed_statuses_with_complete_vendor_enum
            ),
            "no_ohlc_price_zero_observed": no_price_zero_observed,
            "volume_amount_zero_pair_consistent": (
                volume_amount_zero_pair_consistent
            ),
        },
        "gate_effect": {
            "CORE03-Q2": (
                "PARTIAL__EXHAUSTIVE_OBSERVED_ROW_AUDIT_AND_VERSIONED_"
                "VENDOR_ZERO_MEANING__PRICE_ENCODING_NOT_DOCUMENTED"
            ),
            "CORE03-Q3": (
                "UNKNOWN__LOCAL_VALUES_CANNOT_PROVE_VENDOR_NEVER_ENCODED_"
                "A_HISTORICAL_UNKNOWN_AS_ZERO"
            ),
            "formal_admission": False,
            "quality_promotion": False,
            "gate_status_change": "NONE__RECOMPUTED_GATE_REQUIRED",
        },
    }
    return semantics, conflicts


def build_report(
    workspace: Path,
    measurements_path: Path,
    evidence_map_path: Path,
    output: Path,
) -> None:
    workspace = workspace.resolve()
    output = output.resolve()
    report_root = (workspace / "reports" / "data-governance").resolve()
    try:
        output.relative_to(report_root)
    except ValueError as exc:
        raise ValueError(f"output escapes report root: {output}") from exc
    if output.exists():
        raise FileExistsError(
            f"refusing to overwrite existing report: {output}"
        )

    before_guards = formal_guards(workspace)
    candidates, source_refs, trd_manual = verify_inputs(
        workspace,
        measurements_path,
        evidence_map_path,
    )

    staging = output.with_name(f".{output.name}.building-{os.getpid()}")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        connection = duckdb.connect(database=":memory:")
        try:
            create_raw_table(connection, candidates, workspace)
            semantics, conflicts = build_audit(connection)
        finally:
            connection.close()

        after_guards = formal_guards(workspace)
        guards_unchanged = before_guards == after_guards
        if not guards_unchanged:
            raise RuntimeError("protected formal state changed during audit")

        write_json(staging / "SEMANTICS_MATRIX.json", semantics)
        write_json(staging / "KEY_CONFLICTS.json", conflicts)
        write_json(
            staging / "FORMAL_STATE_GUARDS.json",
            {
                "before": before_guards,
                "after": after_guards,
                "unchanged": True,
            },
        )

        verification = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": (
                "PASS__ALL_HASH_BOUND_PARTITIONS_AUDITED__"
                "OBSERVED_ZERO_SEMANTICS_PARTIAL__NO_FORMAL_ADMISSION"
            ),
            "source_refs": source_refs,
            "tool": file_ref(workspace, Path(__file__).resolve()),
            "candidate_count": len(candidates),
            "candidate_ids": [
                candidate["candidate_id"] for candidate in candidates
            ],
            "partition_count": sum(
                len(candidate["partitions"]) for candidate in candidates
            ),
            "trd_manual": {
                "blob_id": trd_manual["blob_id"],
                "document_id": trd_manual["document_id"],
                "version": "2025-06-16",
                "verified_locations": trd_manual["verified_locations"],
            },
            "formal_guards_unchanged": True,
            "catalog_update": False,
            "source_registry_update": False,
            "roadmap_update": False,
            "projects_update": False,
            "snapshot_publish": False,
            "quality_promotion": False,
            "formal_admission": False,
            "research_authorization": False,
            "gate_transition": False,
        }
        write_json(staging / "VERIFICATION.json", verification)
        (staging / "README.md").write_text(
            "\n".join(
                [
                    f"# {RUN_ID}",
                    "",
                    "This fail-closed audit hashes every bound TRD_Dalyr "
                    "partition, removes only exact duplicate rows, excludes "
                    "conflicting security-date keys, and exhaustively counts "
                    "NULL/zero/negative/positive encodings by Trdsta.",
                    "",
                    f"- Candidate packages: {len(candidates)}",
                    f"- Bound partitions: {verification['partition_count']}",
                    f"- Non-conflicting rows: "
                    f"{semantics['scope']['rows_excluding_conflicting_keys']:,}",
                    f"- Date range: {semantics['scope']['date_min']} to "
                    f"{semantics['scope']['date_max']}",
                    f"- Conflicting security-date keys: "
                    f"{conflicts['conflict_key_count']:,}",
                    "",
                    "The 2025-06-16 TRD manual defines Dnshrtrd=0 and "
                    "Dnvaltrd=0 as no trading volume/amount.  The exhaustive "
                    "observed-row audit is additional mechanical evidence, "
                    "but it cannot prove the stronger historical statement "
                    "that the vendor never encoded an unknown value as zero. "
                    "Therefore CORE03-Q2 remains partial, CORE03-Q3 remains "
                    "unknown, and this report makes no admission transition.",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        manifest_files = []
        for path in sorted(staging.iterdir(), key=lambda item: item.name):
            if path.name == "REPORT_MANIFEST.json":
                continue
            manifest_files.append(
                {
                    "path": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
        write_json(
            staging / "REPORT_MANIFEST.json",
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "hash_algorithm": "SHA-256",
                "self_excluded": True,
                "file_count": len(manifest_files),
                "files": manifest_files,
            },
        )
        residuals = {
            path.name for path in staging.iterdir()
        } - REPORT_FILES
        if residuals:
            raise RuntimeError(f"unexpected report files: {sorted(residuals)}")
        staging.replace(output)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--measurements", default=DEFAULT_MEASUREMENTS)
    parser.add_argument("--evidence-map", default=DEFAULT_EVIDENCE_MAP)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace).resolve()
    build_report(
        workspace,
        (workspace / args.measurements).resolve(),
        (workspace / args.evidence_map).resolve(),
        (workspace / args.output).resolve(),
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "run_id": RUN_ID,
                "output": args.output,
                "formal_admission": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
