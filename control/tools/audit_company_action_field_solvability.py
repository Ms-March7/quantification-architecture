#!/usr/bin/env python3
"""Fail-closed field-solvability audit for the company-action candidate ledger.

This tool never writes to the candidate payload or the formal catalog.  It
classifies existing evidence into direct, locally derivable, official-search
required, and not-recoverable-without-new-evidence buckets.  Derived values are
reported only as aggregate candidates; no business row is backfilled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

import duckdb


WORKSPACE = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATE_ROOT = (
    WORKSPACE
    / "data/market/quarantine/cn-a-share-corporate-actions-multisource"
    / "candidate-20260805-v2"
)
DEFAULT_OUTPUT_DIR = (
    WORKSPACE
    / "reports/data-governance/corporate-action-field-solvability-audit-20260806-v1"
)
AUDIT_VERSION = "company-action-field-solvability-audit-v1"

LEDGER_NAME = "unified_source_observation_ledger.parquet"
OVERLAP_NAME = "cross_source_event_overlap.parquet"
PIT_NAME = "joinquant_daily_pit_candidate.parquet"
JQ_XR_XD_NAME = "joinquant_raw/STK_XR_XD.parquet"

REQUIRED_LEDGER_COLUMNS = {
    "observation_id",
    "source_family",
    "source_table",
    "source_row_key",
    "source_row_sha256",
    "local_security_code",
    "permanent_security_id",
    "event_family",
    "lifecycle_stage_candidate",
    "announcement_date_candidate",
    "published_at_candidate",
    "source_publication_date",
    "available_at",
    "event_date_candidate",
    "ex_date_candidate",
    "record_date_candidate",
    "payment_or_listing_date_candidate",
    "vendor_revision_date",
    "revision_id",
    "supersedes_revision_id",
    "withdrawal_state",
    "cash_gross_cny_per_source_share",
    "cash_net_cny_per_source_share",
    "bonus_shares_per_source_share",
    "rights_shares_per_source_share",
    "rights_price_cny_per_share",
    "capitalization_shares_per_source_share",
    "split_increment_shares_per_source_share",
    "adjustment_factor",
    "total_shares_observed",
    "source_terms_json",
    "source_security_scope",
    "research_eligible",
}

AUDITED_FIELDS = [
    "observation_id",
    "local_security_code",
    "permanent_security_id",
    "event_family",
    "lifecycle_stage_candidate",
    "announcement_date_candidate",
    "published_at_candidate",
    "source_publication_date",
    "available_at",
    "event_date_candidate",
    "ex_date_candidate",
    "record_date_candidate",
    "payment_or_listing_date_candidate",
    "vendor_revision_date",
    "revision_id",
    "supersedes_revision_id",
    "withdrawal_state",
    "cash_gross_cny_per_source_share",
    "cash_net_cny_per_source_share",
    "bonus_shares_per_source_share",
    "rights_shares_per_source_share",
    "rights_price_cny_per_share",
    "capitalization_shares_per_source_share",
    "split_increment_shares_per_source_share",
    "adjustment_factor",
    "total_shares_observed",
    "source_terms_json",
]

NUMERIC_FIELDS = [
    "cash_gross_cny_per_source_share",
    "cash_net_cny_per_source_share",
    "bonus_shares_per_source_share",
    "rights_shares_per_source_share",
    "rights_price_cny_per_share",
    "capitalization_shares_per_source_share",
    "split_increment_shares_per_source_share",
    "adjustment_factor",
    "total_shares_observed",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json_bytes(value))


def nonempty_sql(field: str) -> str:
    escaped = field.replace('"', '""')
    return f'NULLIF(TRIM(CAST("{escaped}" AS VARCHAR)), \'\') IS NOT NULL'


def sql_path(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def query_one(con: duckdb.DuckDBPyConnection, sql: str) -> dict[str, Any]:
    cursor = con.execute(sql)
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("expected exactly one aggregate row")
    return {item[0]: value for item, value in zip(cursor.description, row)}


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    if not result.is_finite():
        return None
    return result


NOTE_BASE_PATTERN = re.compile(r"(?:\u6bcf)?10(?:\u80a1)?")
NOTE_PATTERNS = {
    "cash_per_10": re.compile(
        r"(?:\u6d3e(?:\u53d1)?|\u73b0\u91d1\u7ea2\u5229)\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*\u5143"
    ),
    "bonus_per_10": re.compile(
        r"\u9001\s*([0-9]+(?:\.[0-9]+)?)(?:\u80a1)?"
        r"(?=\u8f6c|\u6d3e|\u9001|\s|$|[\uff08(])"
    ),
    "capitalization_per_10": re.compile(
        r"\u8f6c(?:\u589e)?\s*([0-9]+(?:\.[0-9]+)?)\s*\u80a1"
    ),
}


def parse_jq_implementation_note(note: Any) -> dict[str, Decimal]:
    """Parse only explicit standard per-10-share terms from a JQ note.

    The parser intentionally ignores vague prose, rights issues, tax variants,
    and any statement without an explicit numeric term.  Its output is a text
    evidence candidate, not a formal unit contract.
    """

    if note is None:
        return {}
    normalized = str(note).replace(",", "").replace("\uff0c", "").strip()
    if not normalized:
        return {}
    base_match = NOTE_BASE_PATTERN.search(normalized)
    if not base_match:
        return {}
    # One explicit per-10-share base governs every term in the same standard
    # implementation clause (for example, 10送2转增3股派0.5元).
    normalized = normalized[base_match.end() :]
    parsed: dict[str, Decimal] = {}
    for name, pattern in NOTE_PATTERNS.items():
        match = pattern.search(normalized)
        if not match:
            continue
        value = decimal_or_none(match.group(1))
        if value is not None and value >= 0:
            parsed[name] = value
    return parsed


def decimal_close(left: Decimal, right: Decimal, tolerance: Decimal = Decimal("0.000001")) -> bool:
    return abs(left - right) <= tolerance


def extract_observation_values(text: Any) -> list[float]:
    if text is None:
        return []
    values: list[float] = []
    for token in re.findall(r"=\s*([-+]?[0-9]+(?:\.[0-9]+)?)", str(text)):
        try:
            value = float(token)
        except ValueError:
            continue
        if math.isfinite(value):
            values.append(value)
    return values


def contains_power_of_ten_pair(values: Iterable[float], tolerance: float = 1e-6) -> bool:
    positives = sorted({abs(value) for value in values if value and math.isfinite(value)})
    for index, left in enumerate(positives):
        for right in positives[index + 1 :]:
            ratio = right / left
            for target in (10.0, 100.0, 1000.0):
                if abs(ratio - target) <= tolerance * target:
                    return True
    return False


def deterministic_rank_key(value: str, salt: str = AUDIT_VERSION) -> str:
    return hashlib.sha256(f"{value}|{salt}".encode("utf-8")).hexdigest()


def ensure_within(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise RuntimeError(f"{label} escapes allowed root: {resolved}") from exc
    current = resolved
    while current != root_resolved.parent:
        if current.exists() and current.is_symlink():
            raise RuntimeError(f"{label} contains a symlink: {current}")
        if current == root_resolved:
            break
        current = current.parent
    return resolved


@dataclass(frozen=True)
class AuditPaths:
    candidate_root: Path
    output_dir: Path
    ledger: Path
    overlap: Path
    pit: Path
    jq_xr_xd: Path
    candidate_manifest: Path
    datasets_catalog: Path


def resolve_paths(candidate_root: Path, output_dir: Path) -> AuditPaths:
    candidate_root = ensure_within(
        candidate_root,
        WORKSPACE / "data/market/quarantine",
        "candidate root",
    )
    output_dir = ensure_within(
        output_dir,
        WORKSPACE / "reports/data-governance",
        "output directory",
    )
    return AuditPaths(
        candidate_root=candidate_root,
        output_dir=output_dir,
        ledger=candidate_root / LEDGER_NAME,
        overlap=candidate_root / OVERLAP_NAME,
        pit=candidate_root / PIT_NAME,
        jq_xr_xd=candidate_root / JQ_XR_XD_NAME,
        candidate_manifest=candidate_root / "CANDIDATE_MANIFEST.json",
        datasets_catalog=WORKSPACE / "data/catalog/datasets.json",
    )


def verify_inputs(paths: AuditPaths) -> tuple[dict[str, Any], dict[str, Any]]:
    required = [
        paths.ledger,
        paths.overlap,
        paths.pit,
        paths.jq_xr_xd,
        paths.candidate_manifest,
        paths.datasets_catalog,
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"required inputs missing: {missing}")
    manifest = json.loads(paths.candidate_manifest.read_text(encoding="utf-8"))
    if manifest.get("purpose_ceiling") != "INSPECTION_ONLY":
        raise RuntimeError("candidate purpose ceiling is not INSPECTION_ONLY")
    if manifest.get("quality_status") != "NOT_ADMITTED":
        raise RuntimeError("candidate quality status is not NOT_ADMITTED")
    if manifest.get("formal_effect") != "NONE":
        raise RuntimeError("candidate unexpectedly has formal effect")
    verified: dict[str, Any] = {}
    for name, artifact in manifest.get("artifacts", {}).items():
        rel = artifact.get("path")
        expected = artifact.get("sha256")
        if not rel or not expected:
            raise RuntimeError(f"manifest artifact {name} lacks path/hash")
        path = ensure_within(WORKSPACE / rel, WORKSPACE, f"artifact {name}")
        if not path.is_file():
            raise RuntimeError(f"manifest artifact missing: {path}")
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(f"manifest artifact hash mismatch: {name}")
        verified[name] = {
            "path": path.relative_to(WORKSPACE).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": actual,
        }
    return manifest, verified


def build_field_statistics(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    expressions = ["COUNT(*) AS rows_total"]
    for field in AUDITED_FIELDS:
        expressions.append(
            f"COUNT(*) FILTER (WHERE {nonempty_sql(field)}) AS \"{field}__nonempty\""
        )
    row = query_one(con, "SELECT " + ",\n".join(expressions) + " FROM ledger")
    total = int(row.pop("rows_total"))
    return {
        "rows_total": total,
        "fields": {
            key.removesuffix("__nonempty"): {
                "rows_nonempty": int(value),
                "coverage_ratio": float(value) / total if total else 0.0,
            }
            for key, value in row.items()
        },
    }


def build_numeric_statistics(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in NUMERIC_FIELDS:
        clean = f"REPLACE(TRIM(CAST(\"{field}\" AS VARCHAR)), ',', '')"
        row = query_one(
            con,
            f"""
            SELECT
              COUNT(*) FILTER (WHERE {nonempty_sql(field)}) AS nonempty,
              COUNT(*) FILTER (
                WHERE {nonempty_sql(field)} AND TRY_CAST({clean} AS DOUBLE) IS NOT NULL
              ) AS parseable,
              COUNT(*) FILTER (
                WHERE TRY_CAST({clean} AS DOUBLE) > 0
              ) AS positive,
              COUNT(*) FILTER (
                WHERE TRY_CAST({clean} AS DOUBLE) < 0
              ) AS negative
            FROM ledger
            """,
        )
        result[field] = {key: int(value) for key, value in row.items()}
    return result


def build_publication_statistics(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    return query_one(
        con,
        """
        WITH dates AS (
          SELECT
            TRY_CAST(NULLIF(TRIM(source_publication_date), '') AS DATE) AS source_date,
            TRY_CAST(NULLIF(TRIM(published_at_candidate), '') AS DATE) AS published_date,
            TRY_CAST(NULLIF(TRIM(announcement_date_candidate), '') AS DATE) AS announcement_date,
            NULLIF(TRIM(available_at), '') AS formal_available_at
          FROM ledger
        )
        SELECT
          COUNT(*) AS rows_total,
          COUNT(*) FILTER (WHERE formal_available_at IS NOT NULL) AS formal_available_at_rows,
          COUNT(*) FILTER (WHERE source_date IS NOT NULL) AS source_publication_date_rows,
          COUNT(*) FILTER (WHERE published_date IS NOT NULL) AS published_candidate_date_rows,
          COUNT(*) FILTER (WHERE announcement_date IS NOT NULL) AS announcement_candidate_date_rows,
          COUNT(*) FILTER (
            WHERE COALESCE(source_date, published_date, announcement_date) IS NOT NULL
          ) AS broader_daily_date_evidence_rows,
          COUNT(*) FILTER (
            WHERE COALESCE(source_date, published_date, announcement_date) IS NULL
          ) AS no_daily_publication_evidence_rows
        FROM dates
        """,
    )


def build_identity_statistics(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    return query_one(
        con,
        f"""
        SELECT
          COUNT(*) AS rows_total,
          COUNT(*) FILTER (WHERE {nonempty_sql('permanent_security_id')}) AS permanent_id_rows,
          COUNT(*) FILTER (WHERE REGEXP_FULL_MATCH(COALESCE(local_security_code, ''), '[0-9]{{6}}')) AS six_digit_local_code_rows,
          COUNT(*) FILTER (WHERE source_security_scope = 'SHSZ_SIX_DIGIT_NON_B_CANDIDATE') AS jq_shsz_six_digit_candidate_rows,
          COUNT(*) FILTER (WHERE source_security_scope = 'B_SHARE_VENDOR_CODE') AS explicit_b_share_rows,
          COUNT(*) FILTER (WHERE source_security_scope = 'VENDOR_NONSTANDARD_OR_TEMPORARY_CODE') AS explicit_nonstandard_rows,
          COUNT(*) FILTER (WHERE NOT {nonempty_sql('local_security_code')}) AS missing_local_code_rows
        FROM ledger
        """,
    )


def build_revision_statistics(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    base = query_one(
        con,
        f"""
        SELECT
          COUNT(*) AS rows_total,
          COUNT(DISTINCT observation_id) AS distinct_observation_ids,
          COUNT(*) FILTER (WHERE {nonempty_sql('revision_id')}) AS revision_id_rows,
          COUNT(*) FILTER (WHERE {nonempty_sql('supersedes_revision_id')}) AS supersedes_rows,
          COUNT(*) FILTER (WHERE {nonempty_sql('vendor_revision_date')}) AS vendor_revision_date_rows,
          COUNT(*) FILTER (
            WHERE {nonempty_sql('vendor_revision_date')} AND NOT {nonempty_sql('revision_id')}
          ) AS vendor_revision_without_revision_id_rows,
          COUNT(*) FILTER (WHERE withdrawal_state = 'CANCELLED') AS cancelled_rows,
          COUNT(*) FILTER (WHERE withdrawal_state = 'UNKNOWN') AS unknown_withdrawal_rows
        FROM ledger
        """,
    )
    jq_groups = query_one(
        con,
        """
        WITH grouped AS (
          SELECT source_table, source_row_key,
                 COUNT(*) AS rows_in_group,
                 COUNT(DISTINCT lifecycle_stage_candidate) AS stage_count
          FROM ledger
          WHERE source_family = 'JOINQUANT'
          GROUP BY source_table, source_row_key
        )
        SELECT
          COUNT(*) AS source_row_groups,
          COUNT(*) FILTER (WHERE stage_count >= 2) AS multi_stage_source_row_groups,
          MAX(stage_count) AS maximum_stages_per_source_row
        FROM grouped
        """,
    )
    return {**base, "joinquant_source_row_lifecycle": jq_groups}


def build_term_prerequisite_statistics(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    return query_one(
        con,
        """
        WITH parsed AS (
          SELECT
            TRY_CAST(NULLIF(TRIM(bonus_shares_per_source_share), '') AS DOUBLE) AS bonus,
            TRY_CAST(NULLIF(TRIM(capitalization_shares_per_source_share), '') AS DOUBLE) AS capitalization,
            TRY_CAST(NULLIF(TRIM(split_increment_shares_per_source_share), '') AS DOUBLE) AS split_increment,
            TRY_CAST(NULLIF(TRIM(rights_shares_per_source_share), '') AS DOUBLE) AS rights,
            TRY_CAST(NULLIF(TRIM(adjustment_factor), '') AS DOUBLE) AS adjustment,
            TRY_CAST(NULLIF(TRIM(total_shares_observed), '') AS DOUBLE) AS total_shares
          FROM ledger
        )
        SELECT
          COUNT(*) FILTER (WHERE adjustment > 0) AS direct_positive_adjustment_factor_rows,
          COUNT(*) FILTER (WHERE total_shares > 0) AS direct_positive_total_shares_rows,
          COUNT(*) FILTER (
            WHERE COALESCE(bonus, 0) > 0 OR COALESCE(capitalization, 0) > 0 OR COALESCE(split_increment, 0) > 0
          ) AS rows_with_positive_bonus_capitalization_or_split,
          COUNT(*) FILTER (
            WHERE (COALESCE(bonus, 0) > 0 OR COALESCE(capitalization, 0) > 0 OR COALESCE(split_increment, 0) > 0)
              AND COALESCE(rights, 0) = 0
          ) AS simple_share_multiplier_candidate_rows_excluding_rights,
          COUNT(*) FILTER (WHERE COALESCE(rights, 0) > 0) AS positive_rights_term_rows
        FROM parsed
        """,
    )


def audit_jq_note_terms(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = con.execute(
        """
        SELECT id, _source_row_sha256, code, implementation_bonusnote,
               bonus_ratio_rmb, dividend_ratio, transfer_ratio
        FROM jq_xr_xd
        WHERE NULLIF(TRIM(implementation_bonusnote), '') IS NOT NULL
        """
    ).fetchall()
    field_map = {
        "cash_per_10": 4,
        "bonus_per_10": 5,
        "capitalization_per_10": 6,
    }
    counts: Counter[str] = Counter()
    per_term: dict[str, Counter[str]] = {name: Counter() for name in field_map}
    mismatch_samples: list[dict[str, Any]] = []
    for row in rows:
        counts["rows_with_implementation_note"] += 1
        parsed = parse_jq_implementation_note(row[3])
        if parsed:
            counts["rows_with_parseable_standard_note_terms"] += 1
        row_has_mismatch = False
        row_has_comparison = False
        for term, index in field_map.items():
            note_value = parsed.get(term)
            source_value = decimal_or_none(row[index])
            if note_value is not None:
                per_term[term]["note_term_rows"] += 1
            if source_value is not None:
                per_term[term]["source_numeric_rows"] += 1
            if note_value is None or source_value is None:
                continue
            row_has_comparison = True
            per_term[term]["comparable_rows"] += 1
            if decimal_close(note_value, source_value):
                per_term[term]["matching_rows"] += 1
            else:
                per_term[term]["mismatching_rows"] += 1
                row_has_mismatch = True
                mismatch_samples.append(
                    {
                        "source_row_sha256": row[1],
                        "vendor_code": row[2],
                        "term": term,
                        "note_value_per_10_candidate": str(note_value),
                        "source_value": str(source_value),
                        "note": row[3],
                    }
                )
        if row_has_comparison:
            counts["rows_with_any_comparable_term"] += 1
        if row_has_mismatch:
            counts["rows_with_any_mismatch"] += 1
        elif parsed:
            counts["rows_parseable_without_detected_mismatch"] += 1
    mismatch_samples.sort(
        key=lambda item: deterministic_rank_key(str(item.get("source_row_sha256")))
    )
    return {
        "status": "TEXT_CROSSCHECK_ONLY__NOT_A_FORMAL_UNIT_CONTRACT",
        "counts": dict(counts),
        "per_term": {name: dict(counter) for name, counter in per_term.items()},
        "mismatch_sample": mismatch_samples[:20],
        "candidate_conversion": "explicit standard note ratio divided by 10 to per-share; no row was persisted",
    }


def audit_conflicts(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = con.execute(
        """
        SELECT local_security_code, overlap_date, normalized_family,
               cash_observations, bonus_observations, rights_observations,
               cash_value_conflict, bonus_value_conflict, rights_value_conflict
        FROM overlap
        WHERE cash_value_conflict OR bonus_value_conflict OR rights_value_conflict
        """
    ).fetchall()
    counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for row in rows:
        counts["any_numeric_conflict_keys"] += 1
        labels = (
            ("cash", row[3], bool(row[6])),
            ("bonus", row[4], bool(row[7])),
            ("rights", row[5], bool(row[8])),
        )
        has_power_ten = False
        for label, text, flag in labels:
            if not flag:
                continue
            counts[f"{label}_conflict_keys"] += 1
            if contains_power_of_ten_pair(extract_observation_values(text)):
                counts[f"{label}_conflicts_with_power_of_ten_pair"] += 1
                has_power_ten = True
        if has_power_ten:
            counts["any_conflict_with_power_of_ten_pair"] += 1
        samples.append(
            {
                "local_security_code": row[0],
                "overlap_date": row[1],
                "normalized_family": row[2],
                "cash_observations": row[3],
                "bonus_observations": row[4],
                "rights_observations": row[5],
                "contains_power_of_ten_pair": has_power_ten,
            }
        )
    samples.sort(
        key=lambda item: deterministic_rank_key(
            "|".join(
                str(item.get(name) or "")
                for name in ("local_security_code", "overlap_date", "normalized_family")
            )
        )
    )
    return {"counts": dict(counts), "deterministic_sample": samples[:20]}


def fetch_sample(
    con: duckdb.DuckDBPyConnection,
    where_sql: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    cursor = con.execute(
        f"""
        SELECT observation_id, source_family, source_table, source_row_key,
               local_security_code, event_family, lifecycle_stage_candidate,
               announcement_date_candidate, event_date_candidate,
               vendor_revision_date, withdrawal_state, source_security_scope
        FROM ledger
        WHERE {where_sql}
        ORDER BY SHA256(COALESCE(observation_id, '') || '|{AUDIT_VERSION}')
        LIMIT {int(limit)}
        """
    )
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def build_search_queue_sample(
    con: duckdb.DuckDBPyConnection,
    conflict_audit: Mapping[str, Any],
    sample_size: int,
) -> dict[str, Any]:
    missing_date = """
      TRY_CAST(NULLIF(TRIM(source_publication_date), '') AS DATE) IS NULL
      AND TRY_CAST(NULLIF(TRIM(published_at_candidate), '') AS DATE) IS NULL
      AND TRY_CAST(NULLIF(TRIM(announcement_date_candidate), '') AS DATE) IS NULL
    """
    return {
        "schema_version": 1,
        "audit_version": AUDIT_VERSION,
        "sampling": "deterministic_sha256__no_result_dependent_resampling",
        "sample_size_per_category": sample_size,
        "categories": {
            "missing_daily_publication_evidence": fetch_sample(
                con, missing_date, limit=sample_size
            ),
            "vendor_revision_without_revision_id": fetch_sample(
                con,
                f"{nonempty_sql('vendor_revision_date')} AND NOT {nonempty_sql('revision_id')}",
                limit=sample_size,
            ),
            "explicit_cancellation": fetch_sample(
                con, "withdrawal_state = 'CANCELLED'", limit=max(26, sample_size)
            ),
            "nonstandard_or_temporary_code": fetch_sample(
                con,
                "source_security_scope = 'VENDOR_NONSTANDARD_OR_TEMPORARY_CODE'",
                limit=sample_size,
            ),
            "numeric_conflicts": conflict_audit.get("deterministic_sample", []),
        },
        "warning": "These are search candidates, not resolved facts or formal action rows.",
    }


def build_readme(result: Mapping[str, Any]) -> str:
    pub = result["publication_time"]
    identity = result["identity"]
    revision = result["revision_chain"]
    conflict = result["conflicts"]["counts"]
    jq = result["joinquant_note_unit_crosscheck"]["counts"]
    terms = result["multiplier_prerequisites"]
    return f"""# 公司行动字段可解性审计（2026-08-06 v1）

状态：`PASS__SOLVABILITY_QUANTIFIED__NO_FIELD_BACKFILL__INSPECTION_ONLY`

## 结论

全量只读检查 {result['input']['ledger_rows']:,} 条多源公司行动观察后，可以确认下一步应先用本地确定性规则补日级 PIT 候选、标准单位、证券身份和简单股份乘数前提，再对缺公告时间、修订/撤回关系、复杂权益和冲突键定向搜索官方档案。本批没有回填任何业务行。

## 机械结果

- 正式 `available_at`：{pub['formal_available_at_rows']:,}/{pub['rows_total']:,}。
- 存在日级公开日期证据：{pub['broader_daily_date_evidence_rows']:,}；仍无日级证据：{pub['no_daily_publication_evidence_rows']:,}。
- 永久证券 ID：{identity['permanent_id_rows']:,}；六位本地代码：{identity['six_digit_local_code_rows']:,}；显式 B 股观察：{identity['explicit_b_share_rows']:,}；非标准/临时代码：{identity['explicit_nonstandard_rows']:,}。
- `revision_id`：{revision['revision_id_rows']:,}；`supersedes_revision_id`：{revision['supersedes_rows']:,}；有供应商修订日但无 revision ID：{revision['vendor_revision_without_revision_id_rows']:,}。
- 明确取消：{revision['cancelled_rows']:,}。
- 跨源数值冲突键：{conflict.get('any_numeric_conflict_keys', 0):,}；其中包含明显 10/100/1000 倍差候选：{conflict.get('any_conflict_with_power_of_ten_pair', 0):,}。这些只是单位冲突候选，不得直接换算覆盖。
- JQ 实施说明可解析标准条款的行：{jq.get('rows_with_parseable_standard_note_terms', 0):,}；可与供应商比例字段对照的行：{jq.get('rows_with_any_comparable_term', 0):,}；检出不一致的行：{jq.get('rows_with_any_mismatch', 0):,}。
- 现有正调整因子观察：{terms['direct_positive_adjustment_factor_rows']:,}；不含配股选择的简单股份乘数前提候选：{terms['simple_share_multiplier_candidate_rows_excluding_rights']:,}。

## 可解性分类

1. **直接可用的来源观察**：观察 ID/哈希、代码、事件/阶段候选、主要日期及部分现金/送转/配股/调整因子。
2. **本地可推导候选**：日级公开日期后的下一交易时点、标准六位 A 股身份、有明确单位证据的每股换算、简单送转股份乘数。
3. **需官方档案**：精确首次公开时间、稳定 action ID、修订/撤回/替代链、复杂或差异化权益、非标准代码和未裁决冲突。
4. **不可无证据自动恢复**：供应商未保留的旧版本/首见时刻、无公告证据的真值裁决、保存/派生权法律结论。

## 边界

`INSPECTION_ONLY / NOT_ADMITTED / formal_effect=NONE`。未修改 `datasets.json`，未写候选业务表，未运行因子、IC、回测，未消费 Validation/Holdout，外部网络请求为 0。
"""


def artifact_record(path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(WORKSPACE).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def run_audit(paths: AuditPaths, sample_size: int) -> dict[str, Any]:
    datasets_hash_before = sha256_file(paths.datasets_catalog)
    candidate_manifest, verified_inputs = verify_inputs(paths)

    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=8")
    con.execute(f"CREATE VIEW ledger AS SELECT * FROM read_parquet('{sql_path(paths.ledger)}')")
    con.execute(f"CREATE VIEW overlap AS SELECT * FROM read_parquet('{sql_path(paths.overlap)}')")
    con.execute(f"CREATE VIEW pit AS SELECT * FROM read_parquet('{sql_path(paths.pit)}')")
    con.execute(f"CREATE VIEW jq_xr_xd AS SELECT * FROM read_parquet('{sql_path(paths.jq_xr_xd)}')")

    columns = {row[0] for row in con.execute("DESCRIBE ledger").fetchall()}
    missing_columns = sorted(REQUIRED_LEDGER_COLUMNS - columns)
    if missing_columns:
        raise RuntimeError(f"ledger required columns missing: {missing_columns}")

    integrity = query_one(
        con,
        """
        SELECT COUNT(*) AS rows_total,
               COUNT(DISTINCT observation_id) AS distinct_observation_ids,
               COUNT(*) FILTER (WHERE research_eligible) AS research_eligible_rows
        FROM ledger
        """,
    )
    manifest_rows = int(candidate_manifest["metrics"]["unified_ledger_rows"])
    if int(integrity["rows_total"]) != manifest_rows:
        raise RuntimeError("ledger row count differs from candidate manifest")
    if int(integrity["distinct_observation_ids"]) != manifest_rows:
        raise RuntimeError("observation_id uniqueness failed")
    if int(integrity["research_eligible_rows"]) != 0:
        raise RuntimeError("candidate unexpectedly contains research-eligible rows")

    source_counts = {
        row[0]: int(row[1])
        for row in con.execute(
            "SELECT source_family, COUNT(*) FROM ledger GROUP BY source_family ORDER BY source_family"
        ).fetchall()
    }
    field_stats = build_field_statistics(con)
    numeric_stats = build_numeric_statistics(con)
    publication_stats = {
        key: int(value) for key, value in build_publication_statistics(con).items()
    }
    identity_stats = {
        key: int(value) for key, value in build_identity_statistics(con).items()
    }
    revision_raw = build_revision_statistics(con)
    revision_stats = {
        key: (
            {inner: int(number) for inner, number in value.items()}
            if isinstance(value, dict)
            else int(value)
        )
        for key, value in revision_raw.items()
    }
    multiplier_stats = {
        key: int(value)
        for key, value in build_term_prerequisite_statistics(con).items()
    }
    jq_note_audit = audit_jq_note_terms(con)
    conflict_audit = audit_conflicts(con)
    search_sample = build_search_queue_sample(con, conflict_audit, sample_size)
    con.close()

    datasets_hash_after = sha256_file(paths.datasets_catalog)
    if datasets_hash_before != datasets_hash_after:
        raise RuntimeError("datasets.json changed during read-only audit")

    result: dict[str, Any] = {
        "schema_version": 1,
        "run_id": "corporate-action-field-solvability-audit-20260806-v1",
        "audit_version": AUDIT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS__SOLVABILITY_QUANTIFIED__NO_FIELD_BACKFILL__INSPECTION_ONLY",
        "classification": "INSPECTION_ONLY__NOT_ADMITTED",
        "formal_effect": "NONE",
        "input": {
            "candidate_id": candidate_manifest["candidate_id"],
            "candidate_manifest": artifact_record(paths.candidate_manifest),
            "verified_candidate_artifacts": verified_inputs,
            "ledger_rows": int(integrity["rows_total"]),
            "source_counts": source_counts,
            "datasets_catalog_sha256_before": datasets_hash_before,
            "datasets_catalog_sha256_after": datasets_hash_after,
        },
        "integrity": {key: int(value) for key, value in integrity.items()},
        "field_coverage": field_stats,
        "numeric_parseability": numeric_stats,
        "publication_time": publication_stats,
        "identity": identity_stats,
        "revision_chain": revision_stats,
        "multiplier_prerequisites": multiplier_stats,
        "joinquant_note_unit_crosscheck": jq_note_audit,
        "conflicts": conflict_audit,
        "solvability_matrix": [
            {
                "field_family": "daily_available_at_candidate",
                "classification": "LOCALLY_DERIVABLE_CANDIDATE",
                "rows_with_broader_daily_date_evidence": publication_stats[
                    "broader_daily_date_evidence_rows"
                ],
                "remaining_requirement": "immutable formal trading calendar and source-time semantics",
            },
            {
                "field_family": "exact_first_visible_timestamp",
                "classification": "OFFICIAL_ARCHIVE_OR_SOURCE_REQUIRED",
                "formal_rows": publication_stats["formal_available_at_rows"],
                "warning": "date-only evidence must not be promoted to an exact timestamp",
            },
            {
                "field_family": "permanent_security_identity",
                "classification": "PARTIAL_LOCAL_DERIVATION_PLUS_OFFICIAL_MAPPING",
                "direct_rows": identity_stats["permanent_id_rows"],
                "six_digit_code_rows": identity_stats["six_digit_local_code_rows"],
                "nonstandard_rows": identity_stats["explicit_nonstandard_rows"],
            },
            {
                "field_family": "revision_and_supersedes_chain",
                "classification": "MOSTLY_OFFICIAL_ARCHIVE_REQUIRED",
                "direct_revision_rows": revision_stats["revision_id_rows"],
                "direct_supersedes_rows": revision_stats["supersedes_rows"],
                "vendor_revision_without_revision_id_rows": revision_stats[
                    "vendor_revision_without_revision_id_rows"
                ],
            },
            {
                "field_family": "unit_normalization",
                "classification": "PARTIAL_LOCAL_TEXT_AND_DICTIONARY_DERIVATION",
                "jq_parseable_standard_note_rows": jq_note_audit["counts"].get(
                    "rows_with_parseable_standard_note_terms", 0
                ),
                "warning": "text crosscheck is not a supplier unit contract",
            },
            {
                "field_family": "price_multiplier",
                "classification": "DIRECT_FACTOR_PARTIAL__FORMULA_NEEDS_PRICE_AND_UNIT_CONTRACT",
                "direct_positive_adjustment_factor_rows": multiplier_stats[
                    "direct_positive_adjustment_factor_rows"
                ],
                "derived_rows_written": 0,
            },
            {
                "field_family": "share_multiplier",
                "classification": "SIMPLE_TERMS_LOCALLY_DERIVABLE_CANDIDATE",
                "simple_candidate_rows_excluding_rights": multiplier_stats[
                    "simple_share_multiplier_candidate_rows_excluding_rights"
                ],
                "warning": "rights participation and complex capital changes remain UNKNOWN",
                "derived_rows_written": 0,
            },
            {
                "field_family": "multi_source_conflict_truth",
                "classification": "OFFICIAL_ADJUDICATION_REQUIRED",
                "conflict_keys": conflict_audit["counts"].get(
                    "any_numeric_conflict_keys", 0
                ),
                "power_of_ten_candidate_keys": conflict_audit["counts"].get(
                    "any_conflict_with_power_of_ten_pair", 0
                ),
            },
            {
                "field_family": "save_and_derivation_rights",
                "classification": "NOT_TECHNICALLY_INFERABLE",
                "warning": "requires contract or rights evidence",
            },
        ],
        "next_search_queue_counts": {
            "missing_daily_publication_evidence_rows": publication_stats[
                "no_daily_publication_evidence_rows"
            ],
            "vendor_revision_without_revision_id_rows": revision_stats[
                "vendor_revision_without_revision_id_rows"
            ],
            "explicit_cancellation_rows": revision_stats["cancelled_rows"],
            "nonstandard_or_temporary_code_rows": identity_stats[
                "explicit_nonstandard_rows"
            ],
            "numeric_conflict_keys": conflict_audit["counts"].get(
                "any_numeric_conflict_keys", 0
            ),
        },
        "safety": {
            "external_network_requests": 0,
            "candidate_business_rows_written": 0,
            "formal_catalog_write": False,
            "quality_promotion": False,
            "factor_runs": 0,
            "ic_runs": 0,
            "backtest_runs": 0,
            "factor_value_rows": 0,
            "validation_consumed": False,
            "holdout_opened": False,
            "credentials_read_or_written": False,
        },
    }
    result["search_queue_sample"] = search_sample
    return result


def publish_report(paths: AuditPaths, result: dict[str, Any]) -> None:
    output_parent = paths.output_dir.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    if paths.output_dir.exists():
        raise RuntimeError(f"refusing to overwrite existing output: {paths.output_dir}")
    staging = Path(tempfile.mkdtemp(prefix=paths.output_dir.name + ".staging-", dir=output_parent))
    result_path = staging / "RESULT.json"
    sample_path = staging / "SEARCH_QUEUE_SAMPLE.json"
    readme_path = staging / "README.md"
    write_json(result_path, {key: value for key, value in result.items() if key != "search_queue_sample"})
    write_json(sample_path, result["search_queue_sample"])
    readme_path.write_text(build_readme(result), encoding="utf-8", newline="\n")

    code_path = Path(__file__).resolve()
    test_path = WORKSPACE / "control/tests/test_company_action_field_solvability.py"
    manifest = {
        "schema_version": 1,
        "run_id": result["run_id"],
        "created_at": result["created_at"],
        "status": result["status"],
        "classification": result["classification"],
        "formal_effect": "NONE",
        "input": result["input"],
        "code_artifacts": {
            "audit_tool": artifact_record(code_path),
            "unit_tests": artifact_record(test_path),
        },
        "report_artifacts": {
            "result": {
                "path": (paths.output_dir / "RESULT.json").relative_to(WORKSPACE).as_posix(),
                "bytes": result_path.stat().st_size,
                "sha256": sha256_file(result_path),
            },
            "search_queue_sample": {
                "path": (paths.output_dir / "SEARCH_QUEUE_SAMPLE.json").relative_to(WORKSPACE).as_posix(),
                "bytes": sample_path.stat().st_size,
                "sha256": sha256_file(sample_path),
            },
            "readme": {
                "path": (paths.output_dir / "README.md").relative_to(WORKSPACE).as_posix(),
                "bytes": readme_path.stat().st_size,
                "sha256": sha256_file(readme_path),
            },
        },
        "safety": result["safety"],
    }
    write_json(staging / "REPORT_MANIFEST.json", manifest)
    os.replace(staging, paths.output_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, default=DEFAULT_CANDIDATE_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="write a new immutable inspection report; without this flag only print the plan",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.sample_size < 1 or args.sample_size > 100:
        raise SystemExit("--sample-size must be in [1, 100]")
    paths = resolve_paths(args.candidate_root, args.output_dir)
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "PLAN_ONLY__NO_READ_OR_WRITE",
                    "candidate_root": str(paths.candidate_root),
                    "output_dir": str(paths.output_dir),
                    "sample_size": args.sample_size,
                    "external_network_requests": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    result = run_audit(paths, args.sample_size)
    publish_report(paths, result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "output_dir": str(paths.output_dir),
                "ledger_rows": result["input"]["ledger_rows"],
                "formal_effect": "NONE",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
