#!/usr/bin/env python3
"""Build hash-bound local evidence for selected CORE quality gates.

This audit is deliberately non-admitting.  It reads exact, already-built local
CSMAR candidates and writes only one report directory under
``reports/data-governance``.  It never writes ``datasets.json``, source
registries, quality state, formal snapshots, published data, or candidate data.

The important distinction is:

* a mechanical subcheck may pass;
* the complete contract gate passes only when every clause is proven;
* a direct counterexample or a required-but-absent input fails the gate;
* incomplete external evidence remains unknown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable
from urllib.request import Request, urlopen


RUN_ID = "core01-02-03-06-local-evidence-closure-20260730-v1"
DEFAULT_REPORT = (
    "reports/data-governance/"
    "core01-02-03-06-local-evidence-closure-20260730-v1"
)
TARGETS = ("CORE-01", "CORE-02", "CORE-03", "CORE-06")
ALLOWED_STATUSES = {"PASS", "FAIL", "UNKNOWN"}
REPORT_FILES = {
    "GATE_EVIDENCE.json",
    "MEASUREMENTS.json",
    "OFFICIAL_EVIDENCE.json",
    "README.md",
    "REPORT_MANIFEST.json",
    "VERIFICATION.json",
}

CONTRACT = (
    "data/catalog/contracts/g_data_core_v1/core_01_08_market_reference.json"
)
CLOSURE_AUDIT = (
    "reports/data-governance/csmar-33pkg-local-closure-20260729/"
    "CLOSURE_AUDIT.json"
)
STRUCTURED_MEASUREMENTS = (
    "reports/data-governance/d2a-04b-csmar-structured-candidates-20260729/"
    "MEASUREMENTS.json"
)
API_PROBE_README = (
    "reports/data-governance/csmar-python-api-entitlement-probe-20260730-v1/"
    "README.md"
)
API_PROBE_VERIFICATION = (
    "reports/data-governance/csmar-python-api-entitlement-probe-20260730-v1/"
    "VERIFICATION.json"
)
CSMAR_MANUAL_URL = (
    "https://file.csmar.com/group1/M00/AA/99/"
    "CuIKV2XAnKiABeLbAB4nDGC5Ano840.pdf"
)
CSMAR_PRODUCT_URL = "https://www.csmar.com/channels/31.html"

CANDIDATE_RUNS: dict[str, str] = {
    "TRD_Co": (
        "tmp/local-dataset-build/csmar-trd-co-pilot/"
        "d2a04b-csmar-trd-co-full-current-20260728-v1"
    ),
    "TRD_Cale": (
        "tmp/local-dataset-build/csmar-trd-cale-pilot/"
        "d2a04b-csmar-trd-cale-full-1990-2026-20260728-v1"
    ),
    "TRD_Dalyr_1990_1994": (
        "tmp/local-dataset-build/csmar-trd-dalyr-pilot/"
        "d2a04b-csmar-trd-dalyr-1990-1994-20260729-v1"
    ),
    "TRD_Dalyr_1995_1999": (
        "tmp/local-dataset-build/csmar-trd-dalyr-pilot/"
        "d2a04b-csmar-trd-dalyr-1995-1999-20260729-v1"
    ),
    "TRD_Dalyr_2000_2004": (
        "tmp/local-dataset-build/csmar-trd-dalyr-pilot/"
        "d2a04b-csmar-trd-dalyr-2000-2004-20260729-v1"
    ),
    "TRD_Dalyr_2005_2009": (
        "tmp/local-dataset-build/csmar-trd-dalyr-pilot/"
        "d2a04b-csmar-trd-dalyr-2005-2009-20260729-v1"
    ),
    "TRD_Dalyr_2010_2014": (
        "tmp/local-dataset-build/csmar-trd-dalyr-pilot/"
        "d2a04b-csmar-trd-dalyr-2010-2014-20260729-v1"
    ),
    "TRD_Dalyr_2015_2019": (
        "tmp/local-dataset-build/csmar-trd-dalyr-pilot/"
        "d2a04b-csmar-trd-dalyr-2015-2019-20260728-v1"
    ),
    "TRD_Dalyr_2020_2024": (
        "tmp/local-dataset-build/csmar-trd-dalyr-pilot/"
        "d2a02-2020-2024-20260728-v1"
    ),
    "TRD_Dalyr_2022_2026": (
        "tmp/local-dataset-build/csmar-trd-dalyr-pilot/"
        "d2a04b-csmar-trd-dalyr-2022-2026-20260728-v1"
    ),
    "TRD_NoLimit": (
        "tmp/local-dataset-build/csmar-trd-nolimit/"
        "d2a04b-csmar-trd-nolimit-full-1990-2026-20260729-v1"
    ),
    "BDT_CO": (
        "tmp/csmar-bdt-zero-loss-candidate/csmar-bdt-co-zero-loss/"
        "csmar-bdt-co-20260730-v1"
    ),
    "BDT_Trdchg": (
        "tmp/csmar-bdt-zero-loss-candidate/csmar-bdt-trdchg-zero-loss/"
        "csmar-bdt-trdchg-20260730-v1"
    ),
}

DAILY_HISTORY_KEYS = (
    "TRD_Dalyr_1990_1994",
    "TRD_Dalyr_1995_1999",
    "TRD_Dalyr_2000_2004",
    "TRD_Dalyr_2005_2009",
    "TRD_Dalyr_2010_2014",
    "TRD_Dalyr_2015_2019",
    "TRD_Dalyr_2020_2024",
)
DAILY_TAIL_KEY = "TRD_Dalyr_2022_2026"
DAILY_COLUMNS = (
    "Stkcd,Trddt,Opnprc,Hiprc,Loprc,Clsprc,Dnshrtrd,Dnvaltrd,"
    "Dsmvosd,Dsmvtll,Dretwd,Dretnd,Adjprcwd,Adjprcnd,Markettype,"
    "Capchgdt,Trdsta,Ahshrtrd_D,Ahvaltrd_D,PreClosePrice,"
    "ChangeRatio,LimitDown,LimitUp,LimitStatus"
)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def workspace_relative(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace).as_posix()


def require_report_path(path: Path, workspace: Path) -> None:
    report_parent = (workspace / "reports" / "data-governance").resolve()
    resolved = path.resolve()
    if Path(os.path.commonpath([resolved, report_parent])) != report_parent:
        raise ValueError(f"report output escapes reports/data-governance: {resolved}")
    if resolved == report_parent:
        raise ValueError("report output must be a child of reports/data-governance")


def reject_unexpected_report_residuals(output: Path) -> None:
    if not output.exists():
        return
    if not output.is_dir():
        raise ValueError(f"report output is not a directory: {output}")
    residuals = sorted(
        item.name
        for item in output.iterdir()
        if not item.is_file() or item.name not in REPORT_FILES
    )
    if residuals:
        raise ValueError(
            "refusing replacement because unregistered report residuals exist: "
            f"{residuals}"
        )


def file_binding(path: Path, workspace: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": workspace_relative(path, workspace),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def fetch_url_binding(url: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 CodexEvidenceAudit/1.0"},
    )
    with urlopen(request, timeout=30) as response:
        payload = response.read()
        return {
            "url": url,
            "http_status": int(response.status),
            "bytes": len(payload),
            "sha256": sha256_bytes(payload),
            "content_type": response.headers.get("Content-Type"),
            "last_modified": response.headers.get("Last-Modified"),
        }


def official_evidence_receipt() -> dict[str, Any]:
    manual = fetch_url_binding(CSMAR_MANUAL_URL)
    product = fetch_url_binding(CSMAR_PRODUCT_URL)
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "retrieved_on": "2026-07-30",
        "csmar_stock_trading_manual": {
            **manual,
            "document_date": "2024-02-05",
            "verified_extracts": [
                {
                    "location": "PDF p.21, lines 849-854",
                    "assertion": (
                        "TRD_Cale defines Markettype, Clddt, Daywk and State "
                        "with O=open and C=closed."
                    ),
                },
                {
                    "location": "PDF p.22, lines 900-920",
                    "assertion": (
                        "TRD_NoLimit defines its fields and reason codes "
                        "P8601-P8610."
                    ),
                },
                {
                    "location": "PDF pp.23-24, lines 934-1003",
                    "assertion": (
                        "The 21-field TRD_Dalyr manual defines Stkcd, Trddt, "
                        "Trdsta 1-16, OHLC, volume/turnover/market-cap units, "
                        "returns, adjustment prices, Markettype, Capchgdt, "
                        "after-hours volume/turnover, PreClosePrice and "
                        "ChangeRatio."
                    ),
                },
            ],
            "explicit_limit": (
                "The verified 21-field manual does not define the later local "
                "export fields LimitDown, LimitUp or LimitStatus; their "
                "presence is verified locally, but their official semantics "
                "remain open."
            ),
        },
        "csmar_product_and_license_page": {
            **product,
            "verified_extract": (
                "The product page describes financial empirical-research use. "
                "The manual license grants a unique-use licence and prohibits "
                "transfer, sale or public disclosure to third parties."
            ),
            "rights_conclusion": (
                "PARTIAL_PRIVATE_LOCAL_RESEARCH_SIGNAL_ONLY: this does not "
                "prove the current Tsinghua subscription chain, API entitlement, "
                "post-termination retention, internal sharing, or redistribution."
            ),
        },
    }


def curated_tree_binding(root: Path, workspace: Path) -> dict[str, Any]:
    files = sorted((root / "curated").rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no curated parquet files: {root}")
    entries = [
        {
            "path": workspace_relative(path, workspace),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]
    return {
        "root": workspace_relative(root, workspace),
        "file_count": len(entries),
        "total_bytes": sum(item["bytes"] for item in entries),
        "entries": entries,
        "tree_sha256": sha256_bytes(canonical_json_bytes(entries)),
    }


def candidate_bindings(
    workspace: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    bindings: dict[str, dict[str, Any]] = {}
    parquet_paths: dict[str, list[str]] = {}
    for label, relative in CANDIDATE_RUNS.items():
        root = (workspace / relative).resolve(strict=True)
        manifest = root / "manifest.json"
        quality = root / "quality" / "summary.json"
        tree = curated_tree_binding(root, workspace)
        bindings[label] = {
            "root": workspace_relative(root, workspace),
            "manifest": file_binding(manifest, workspace),
            "quality": file_binding(quality, workspace),
            "curated_tree": tree,
        }
        parquet_paths[label] = [
            str((workspace / item["path"]).resolve())
            for item in tree["entries"]
        ]
    return bindings, parquet_paths


def fetch_one_dict(connection: Any, sql: str) -> dict[str, Any]:
    cursor = connection.execute(sql)
    row = cursor.fetchone()
    if row is None:
        raise ValueError("query unexpectedly returned no row")
    return {
        description[0]: json_scalar(value)
        for description, value in zip(cursor.description, row)
    }


def fetch_all_dicts(connection: Any, sql: str) -> list[dict[str, Any]]:
    cursor = connection.execute(sql)
    return [
        {
            description[0]: json_scalar(value)
            for description, value in zip(cursor.description, row)
        }
        for row in cursor.fetchall()
    ]


def json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def schema_fields(connection: Any, view_name: str) -> list[str]:
    rows = connection.execute(
        f"DESCRIBE SELECT * FROM {view_name}"
    ).fetchall()
    return [str(row[0]) for row in rows]


def create_views(connection: Any, parquet_paths: dict[str, list[str]]) -> None:
    for label, view in (
        ("TRD_Co", "trd_co"),
        ("TRD_Cale", "trd_cale"),
        ("TRD_NoLimit", "trd_nolimit"),
        ("BDT_CO", "bdt_co"),
        ("BDT_Trdchg", "bdt_trdchg"),
    ):
        connection.from_parquet(
            parquet_paths[label],
            union_by_name=True,
            hive_partitioning=True,
        ).create_view(view)

    history_paths = [
        path
        for label in DAILY_HISTORY_KEYS
        for path in parquet_paths[label]
    ]
    connection.from_parquet(
        history_paths,
        union_by_name=True,
        hive_partitioning=True,
    ).create_view("daily_history")
    connection.from_parquet(
        parquet_paths[DAILY_TAIL_KEY],
        union_by_name=True,
        hive_partitioning=True,
    ).create_view("daily_tail")
    connection.execute(
        f"""
        CREATE VIEW daily AS
        SELECT {DAILY_COLUMNS}
        FROM daily_history
        UNION ALL
        SELECT {DAILY_COLUMNS}
        FROM daily_tail
        WHERE Trddt >= DATE '2025-01-01'
        """
    )
    connection.execute(
        """
        CREATE VIEW daily_dates AS
        SELECT DISTINCT Markettype, Trddt FROM daily
        """
    )


def collect_measurements(connection: Any) -> dict[str, Any]:
    identity = {
        "trd_co_summary": fetch_one_dict(
            connection,
            """
            SELECT
              count(*) AS row_count,
              count(DISTINCT Stkcd) AS distinct_codes,
              count(*) - count(DISTINCT Stkcd) AS duplicate_code_rows,
              sum(CASE WHEN Stkcd IS NULL THEN 1 ELSE 0 END) AS null_codes,
              sum(CASE WHEN Listdt IS NULL THEN 1 ELSE 0 END) AS null_list_dates,
              sum(CASE WHEN nullif(trim(Statco), '') IS NULL
                       THEN 1 ELSE 0 END) AS null_status_codes
            FROM trd_co
            """,
        ),
        "bdt_co_summary": fetch_one_dict(
            connection,
            """
            SELECT
              count(*) AS row_count,
              count(DISTINCT Symbol) AS distinct_codes,
              count(*) - count(DISTINCT Symbol) AS duplicate_code_rows,
              sum(CASE WHEN nullif(trim(Symbol), '') IS NULL
                       THEN 1 ELSE 0 END) AS null_codes,
              sum(CASE WHEN nullif(trim(ListedDate), '') IS NULL
                       THEN 1 ELSE 0 END) AS null_list_dates,
              sum(CASE WHEN nullif(trim(DelistedDate), '') IS NOT NULL
                       THEN 1 ELSE 0 END) AS nonnull_delist_dates
            FROM bdt_co
            """,
        ),
        "local_code_reuse_counterexamples": fetch_all_dicts(
            connection,
            """
            SELECT
              Symbol,
              count(*) AS listing_rows,
              count(DISTINCT InstitutionID) AS institution_ids,
              min(ListedDate) AS first_listed_date,
              max(ListedDate) AS last_listed_date,
              min(nullif(DelistedDate, '')) AS first_delisted_date,
              max(nullif(DelistedDate, '')) AS last_delisted_date
            FROM bdt_co
            GROUP BY Symbol
            HAVING count(DISTINCT InstitutionID) > 1
            ORDER BY Symbol
            """,
        ),
        "trd_co_bdt_co_alignment": fetch_one_dict(
            connection,
            """
            SELECT
              count(*) AS overlap_codes,
              sum(CASE WHEN t.Stknme IS DISTINCT FROM b.ShortName
                       THEN 1 ELSE 0 END) AS current_name_differences,
              sum(CASE WHEN cast(t.Listdt AS VARCHAR)
                            IS DISTINCT FROM b.ListedDate
                       THEN 1 ELSE 0 END) AS list_date_differences
            FROM trd_co t
            JOIN bdt_co b ON t.Stkcd = b.Symbol
            """,
        ),
        "code_coverage_differences": fetch_one_dict(
            connection,
            """
            SELECT
              (SELECT count(*)
               FROM trd_co t LEFT JOIN bdt_co b ON t.Stkcd = b.Symbol
               WHERE b.Symbol IS NULL) AS trd_co_only,
              (SELECT count(*)
               FROM bdt_co b LEFT JOIN trd_co t ON t.Stkcd = b.Symbol
               WHERE t.Stkcd IS NULL) AS bdt_co_only
            """,
        ),
        "trd_co_market_coverage": fetch_all_dicts(
            connection,
            """
            SELECT
              Markettype,
              count(*) AS row_count,
              min(Listdt) AS first_list_date,
              max(Listdt) AS last_list_date,
              sum(CASE WHEN nullif(trim(Statco), '') IS NULL
                       THEN 1 ELSE 0 END) AS null_status_codes
            FROM trd_co
            GROUP BY Markettype
            ORDER BY Markettype
            """,
        ),
    }

    calendar = {
        "summary": fetch_one_dict(
            connection,
            """
            SELECT
              count(*) AS row_count,
              count(DISTINCT Markettype) AS market_types,
              min(Clddt) AS first_date,
              max(Clddt) AS last_date,
              sum(CASE WHEN Markettype IS NULL OR Clddt IS NULL
                       THEN 1 ELSE 0 END) AS null_keys
            FROM trd_cale
            """,
        ),
        "duplicate_key_groups": fetch_one_dict(
            connection,
            """
            SELECT count(*) AS duplicate_key_groups
            FROM (
              SELECT Markettype, Clddt, count(*) AS n
              FROM trd_cale
              GROUP BY Markettype, Clddt
              HAVING count(*) > 1
            )
            """,
        )["duplicate_key_groups"],
        "state_coverage": fetch_all_dicts(
            connection,
            """
            SELECT
              Markettype,
              State,
              count(*) AS row_count,
              min(Clddt) AS first_date,
              max(Clddt) AS last_date
            FROM trd_cale
            GROUP BY Markettype, State
            ORDER BY Markettype, State
            """,
        ),
        "open_date_order_checks": fetch_all_dicts(
            connection,
            """
            WITH ordered AS (
              SELECT
                Markettype,
                Clddt,
                lag(Clddt) OVER (
                  PARTITION BY Markettype ORDER BY Clddt
                ) AS previous_date,
                lead(Clddt) OVER (
                  PARTITION BY Markettype ORDER BY Clddt
                ) AS next_date
              FROM trd_cale
              WHERE State = 'O'
            )
            SELECT
              Markettype,
              count(*) AS open_dates,
              sum(CASE WHEN previous_date IS NOT NULL
                            AND Clddt <= previous_date
                       THEN 1 ELSE 0 END) AS nonincreasing_previous,
              sum(CASE WHEN next_date IS NOT NULL
                            AND Clddt >= next_date
                       THEN 1 ELSE 0 END) AS nonincreasing_next,
              count(DISTINCT date_trunc('month', Clddt))
                AS reconstructable_months,
              count(DISTINCT date_trunc('quarter', Clddt))
                AS reconstructable_quarters,
              count(DISTINCT date_trunc('year', Clddt))
                AS reconstructable_years
            FROM ordered
            GROUP BY Markettype
            ORDER BY Markettype
            """,
        ),
        "daily_vs_vendor_open_date_mismatch": fetch_all_dicts(
            connection,
            """
            WITH vendor_open AS (
              SELECT Markettype, Clddt
              FROM trd_cale
              WHERE State = 'O'
            )
            SELECT
              coalesce(d.Markettype, c.Markettype) AS Markettype,
              sum(CASE WHEN d.Trddt IS NOT NULL AND c.Clddt IS NULL
                       THEN 1 ELSE 0 END) AS daily_not_vendor_open,
              sum(CASE WHEN d.Trddt IS NULL AND c.Clddt IS NOT NULL
                       THEN 1 ELSE 0 END) AS vendor_open_not_daily
            FROM daily_dates d
            FULL OUTER JOIN vendor_open c
              ON d.Markettype = c.Markettype
             AND d.Trddt = c.Clddt
            GROUP BY coalesce(d.Markettype, c.Markettype)
            ORDER BY Markettype
            """,
        ),
        "market64_pre_calendar_counterexample": fetch_one_dict(
            connection,
            """
            SELECT
              count(DISTINCT d.Trddt) AS distinct_dates,
              count(*) AS daily_rows,
              count(DISTINCT d.Stkcd) AS securities,
              min(d.Trddt) AS first_date,
              max(d.Trddt) AS last_date,
              (
                SELECT min(Clddt)
                FROM trd_cale
                WHERE Markettype = 64 AND State = 'O'
              ) AS vendor_calendar_first_open
            FROM daily d
            WHERE d.Markettype = 64
              AND d.Trddt < (
                SELECT min(Clddt)
                FROM trd_cale
                WHERE Markettype = 64 AND State = 'O'
              )
            """,
        ),
    }

    daily = {
        "summary": fetch_one_dict(
            connection,
            """
            SELECT
              count(*) AS row_count,
              count(DISTINCT Stkcd) AS securities,
              count(DISTINCT Trddt) AS trade_dates,
              min(Trddt) AS first_date,
              max(Trddt) AS last_date,
              sum(CASE WHEN Stkcd IS NULL OR Trddt IS NULL
                       THEN 1 ELSE 0 END) AS null_keys,
              sum(CASE WHEN
                    Opnprc < 0 OR Hiprc < 0 OR Loprc < 0 OR Clsprc < 0
                    OR PreClosePrice < 0 OR LimitDown < 0 OR LimitUp < 0
                  THEN 1 ELSE 0 END) AS negative_price_rows,
              sum(CASE WHEN Hiprc IS NOT NULL
                            AND (
                              Hiprc < Opnprc OR Hiprc < Loprc
                              OR Hiprc < Clsprc
                            )
                       THEN 1 ELSE 0 END) AS high_inequality_violations,
              sum(CASE WHEN Loprc IS NOT NULL
                            AND (
                              Loprc > Opnprc OR Loprc > Hiprc
                              OR Loprc > Clsprc
                            )
                       THEN 1 ELSE 0 END) AS low_inequality_violations,
              sum(CASE WHEN
                    Dnshrtrd < 0 OR Dnvaltrd < 0
                    OR Ahshrtrd_D < 0 OR Ahvaltrd_D < 0
                  THEN 1 ELSE 0 END) AS negative_volume_value_rows,
              sum(CASE WHEN Dnshrtrd = 0 THEN 1 ELSE 0 END)
                AS zero_volume_rows,
              sum(CASE WHEN Dnvaltrd = 0 THEN 1 ELSE 0 END)
                AS zero_turnover_rows,
              sum(CASE WHEN Dnshrtrd IS NULL THEN 1 ELSE 0 END)
                AS null_volume_rows,
              sum(CASE WHEN Dnvaltrd IS NULL THEN 1 ELSE 0 END)
                AS null_turnover_rows,
              sum(CASE WHEN Trdsta IS NULL THEN 1 ELSE 0 END)
                AS null_trade_status_rows,
              sum(CASE WHEN LimitStatus IS NULL THEN 1 ELSE 0 END)
                AS null_limit_status_rows,
              sum(CASE WHEN PreClosePrice IS NULL THEN 1 ELSE 0 END)
                AS null_preclose_rows,
              sum(CASE WHEN LimitDown IS NULL THEN 1 ELSE 0 END)
                AS null_limit_down_rows,
              sum(CASE WHEN LimitUp IS NULL THEN 1 ELSE 0 END)
                AS null_limit_up_rows
            FROM daily
            """,
        ),
        "duplicate_key_groups": fetch_one_dict(
            connection,
            """
            SELECT count(*) AS duplicate_key_groups
            FROM (
              SELECT Stkcd, Trddt, count(*) AS n
              FROM daily
              GROUP BY Stkcd, Trddt
              HAVING count(*) > 1
            )
            """,
        )["duplicate_key_groups"],
        "market_coverage": fetch_all_dicts(
            connection,
            """
            SELECT
              Markettype,
              count(*) AS row_count,
              count(DISTINCT Stkcd) AS securities,
              count(DISTINCT Trddt) AS trade_dates,
              min(Trddt) AS first_date,
              max(Trddt) AS last_date
            FROM daily
            GROUP BY Markettype
            ORDER BY Markettype
            """,
        ),
        "trade_status_values": fetch_all_dicts(
            connection,
            """
            SELECT Trdsta, count(*) AS row_count
            FROM daily
            GROUP BY Trdsta
            ORDER BY Trdsta
            """,
        ),
        "limit_status_values": fetch_all_dicts(
            connection,
            """
            SELECT LimitStatus, count(*) AS row_count
            FROM daily
            GROUP BY LimitStatus
            ORDER BY LimitStatus
            """,
        ),
    }

    status = {
        "bdt_trdchg_summary": fetch_one_dict(
            connection,
            """
            SELECT
              count(*) AS row_count,
              count(DISTINCT Symbol) AS securities,
              count(*) - count(DISTINCT (Symbol, Chgtype, Execudt))
                AS duplicate_candidate_key_rows,
              sum(CASE WHEN nullif(trim(Annoudt), '') IS NULL
                       THEN 1 ELSE 0 END) AS null_announcement_dates,
              sum(CASE WHEN nullif(trim(Execudt), '') IS NULL
                       THEN 1 ELSE 0 END) AS null_effective_dates,
              sum(CASE WHEN
                    try_strptime(Annoudt, '%Y-%m-%d')
                      > try_strptime(Execudt, '%Y-%m-%d')
                  THEN 1 ELSE 0 END) AS announcement_after_effective_rows,
              min(try_strptime(Annoudt, '%Y-%m-%d'))
                AS first_announcement_date,
              max(try_strptime(Execudt, '%Y-%m-%d'))
                AS last_effective_date
            FROM bdt_trdchg
            """,
        ),
        "bdt_trdchg_types": fetch_all_dicts(
            connection,
            """
            SELECT
              Chgtype,
              count(*) AS row_count,
              min(try_strptime(Annoudt, '%Y-%m-%d'))
                AS first_announcement_date,
              max(try_strptime(Execudt, '%Y-%m-%d'))
                AS last_effective_date
            FROM bdt_trdchg
            GROUP BY Chgtype
            ORDER BY row_count DESC, Chgtype
            """,
        ),
        "no_limit_summary": fetch_one_dict(
            connection,
            """
            SELECT
              count(*) AS row_count,
              count(DISTINCT Symbol) AS securities,
              count(DISTINCT TradingDate) AS dates,
              min(TradingDate) AS first_date,
              max(TradingDate) AS last_date,
              sum(CASE WHEN Symbol IS NULL OR TradingDate IS NULL
                       THEN 1 ELSE 0 END) AS null_keys
            FROM trd_nolimit
            """,
        ),
        "no_limit_duplicate_keys": fetch_one_dict(
            connection,
            """
            SELECT count(*) AS duplicate_key_groups
            FROM (
              SELECT Symbol, TradingDate, count(*) AS n
              FROM trd_nolimit
              GROUP BY Symbol, TradingDate
              HAVING count(*) > 1
            )
            """,
        )["duplicate_key_groups"],
        "no_limit_reason_codes": fetch_all_dicts(
            connection,
            """
            SELECT
              Reason,
              count(*) AS row_count,
              min(TradingDate) AS first_date,
              max(TradingDate) AS last_date
            FROM trd_nolimit
            GROUP BY Reason
            ORDER BY row_count DESC, Reason
            """,
        ),
    }

    fields = {
        "TRD_Co": schema_fields(connection, "trd_co"),
        "TRD_Cale": schema_fields(connection, "trd_cale"),
        "TRD_Dalyr": schema_fields(connection, "daily"),
        "TRD_NoLimit": schema_fields(connection, "trd_nolimit"),
        "BDT_CO": schema_fields(connection, "bdt_co"),
        "BDT_Trdchg": schema_fields(connection, "bdt_trdchg"),
    }
    return {
        "identity": identity,
        "calendar": calendar,
        "daily": daily,
        "status_and_rules": status,
        "observed_fields": fields,
    }


def subcheck(
    check_id: str,
    status: str,
    assertion: str,
    measurement_paths: Iterable[str],
) -> dict[str, Any]:
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"invalid subcheck status: {status}")
    return {
        "check_id": check_id,
        "status": status,
        "assertion": assertion,
        "measurement_paths": list(measurement_paths),
    }


def aggregate_gate_status(subchecks: list[dict[str, Any]]) -> str:
    statuses = [item["status"] for item in subchecks]
    if not statuses:
        raise ValueError("gate needs at least one subcheck")
    if all(status == "PASS" for status in statuses):
        return "PASS"
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    return "UNKNOWN"


def gate(
    gate_id: str,
    severity: str,
    subchecks: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "severity": severity,
        "status": aggregate_gate_status(subchecks),
        "subchecks": subchecks,
    }


def missing_fields(
    observed_fields: list[str], required_fields: Iterable[str]
) -> list[str]:
    observed = set(observed_fields)
    return sorted(set(required_fields) - observed)


def build_gate_evaluations(measurements: dict[str, Any]) -> dict[str, Any]:
    fields = measurements["observed_fields"]
    daily_summary = measurements["daily"]["summary"]
    calendar = measurements["calendar"]
    identity = measurements["identity"]
    status = measurements["status_and_rules"]
    official = measurements["external_evidence"][
        "csmar_stock_trading_manual"
    ]

    identity_required = (
        "security_id",
        "valid_from",
        "valid_to",
        "published_at",
        "available_at",
        "revision_id",
    )
    calendar_revision_required = (
        "published_at",
        "available_at",
        "revision_id",
    )
    daily_lineage_required = (
        "source_id",
        "security_id",
        "revision_id",
    )
    daily_pit_required = ("exchange_close_at", "available_at", "revision_id")
    status_pit_required = (
        "security_id",
        "available_at",
        "revision_id",
        "rule_version",
    )

    market64 = calendar["market64_pre_calendar_counterexample"]
    calendar_order_clean = all(
        item["nonincreasing_previous"] == 0
        and item["nonincreasing_next"] == 0
        for item in calendar["open_date_order_checks"]
    )
    current_code_reuse = identity["local_code_reuse_counterexamples"]
    price_mechanics_clean = (
        daily_summary["negative_price_rows"] == 0
        and daily_summary["high_inequality_violations"] == 0
        and daily_summary["low_inequality_violations"] == 0
    )
    volume_mechanics_clean = (
        daily_summary["negative_volume_value_rows"] == 0
    )
    bdt_after_effective = status["bdt_trdchg_summary"][
        "announcement_after_effective_rows"
    ]

    gates = {
        "CORE-01": [
            gate(
                "CORE01-Q1",
                "BLOCK",
                [
                    subcheck(
                        "current_trd_co_code_unique",
                        "PASS",
                        "TRD_Co current cross-section has one row per Stkcd.",
                        ["identity.trd_co_summary"],
                    ),
                    subcheck(
                        "local_code_is_not_permanent_identity",
                        "FAIL" if current_code_reuse else "UNKNOWN",
                        (
                            "BDT_CO contains a reused local code across distinct "
                            "InstitutionID/listing episodes; Stkcd/Symbol cannot "
                            "serve as a permanent security identity."
                        ),
                        ["identity.local_code_reuse_counterexamples"],
                    ),
                    subcheck(
                        "permanent_identity_contract_fields",
                        (
                            "FAIL"
                            if missing_fields(fields["TRD_Co"], identity_required)
                            else "PASS"
                        ),
                        "The current candidate lacks the full permanent-identity, "
                        "effective-interval, publication and revision fields.",
                        ["observed_fields.TRD_Co"],
                    ),
                    subcheck(
                        "uncertain_mapping_quarantine",
                        "UNKNOWN",
                        "No complete mapping decision set proves every uncertain "
                        "identity relation is quarantined.",
                        ["identity.code_coverage_differences"],
                    ),
                ],
            ),
            gate(
                "CORE01-Q2",
                "BLOCK",
                [
                    subcheck(
                        "code_and_listing_status_intervals_exist",
                        (
                            "FAIL"
                            if missing_fields(
                                fields["TRD_Co"],
                                ("security_id", "valid_from", "valid_to"),
                            )
                            else "PASS"
                        ),
                        "TRD_Co is a current cross-section, not a complete set of "
                        "security code and listing-status intervals.",
                        ["observed_fields.TRD_Co"],
                    ),
                    subcheck(
                        "interval_overlap_and_gap_audit",
                        "UNKNOWN",
                        "Without complete intervals, overlap and unexplained-gap "
                        "tests cannot be completed.",
                        ["identity.trd_co_summary"],
                    ),
                ],
            ),
            gate(
                "CORE01-Q3",
                "BLOCK",
                [
                    subcheck(
                        "three_venue_current_rows_observed",
                        "PASS",
                        "Observed candidates contain SSE, SZSE and BSE-labelled "
                        "current rows.",
                        [
                            "identity.trd_co_market_coverage",
                            "identity.bdt_co_summary",
                        ],
                    ),
                    subcheck(
                        "current_and_delisted_a_share_completeness",
                        "UNKNOWN",
                        "Candidate counts have not been reconciled to official "
                        "current and delisted A-share universes.",
                        ["identity.bdt_co_summary"],
                    ),
                    subcheck(
                        "year_venue_board_coverage_matrix",
                        "UNKNOWN",
                        "Observed Markettype/list-date counts do not prove the "
                        "required official year/venue/board completeness matrix.",
                        ["identity.trd_co_market_coverage"],
                    ),
                ],
            ),
            gate(
                "CORE01-Q4",
                "BLOCK",
                [
                    subcheck(
                        "history_publication_and_availability",
                        (
                            "FAIL"
                            if missing_fields(
                                fields["BDT_Trdchg"],
                                ("published_at", "available_at", "revision_id"),
                            )
                            else "PASS"
                        ),
                        "BDT_Trdchg has date-level announcement/effective fields "
                        "but no published_at, available_at or revision chain.",
                        [
                            "observed_fields.BDT_Trdchg",
                            "status_and_rules.bdt_trdchg_summary",
                        ],
                    )
                ],
            ),
            gate(
                "CORE01-Q5",
                "REVIEW",
                [
                    subcheck(
                        "candidate_sources_preserved_separately",
                        "PASS",
                        "TRD_Co, BDT_CO and BDT_Trdchg remain separate immutable "
                        "candidate roots in this audit.",
                        ["identity.trd_co_bdt_co_alignment"],
                    ),
                    subcheck(
                        "cross_official_conflict_adjudication",
                        "UNKNOWN",
                        "No complete multi-official conflict register and "
                        "adjudication chain exists.",
                        ["identity.code_coverage_differences"],
                    ),
                ],
            ),
        ],
        "CORE-02": [
            gate(
                "CORE02-Q1",
                "BLOCK",
                [
                    subcheck(
                        "calendar_key_and_explicit_state",
                        (
                            "PASS"
                            if calendar["duplicate_key_groups"] == 0
                            and calendar["summary"]["null_keys"] == 0
                            else "FAIL"
                        ),
                        "TRD_Cale has unique Markettype/Clddt keys and explicit "
                        "C/O states in the observed export.",
                        [
                            "calendar.summary",
                            "calendar.duplicate_key_groups",
                            "calendar.state_coverage",
                        ],
                    ),
                    subcheck(
                        "vendor_calendar_dictionary",
                        "PASS",
                        "The hash-bound 2024-02-05 CSMAR manual defines "
                        "TRD_Cale Markettype/Clddt/Daywk and State O/C.",
                        ["external_evidence.csmar_stock_trading_manual"],
                    ),
                    subcheck(
                        "market64_calendar_consistency",
                        "FAIL" if market64["distinct_dates"] > 0 else "PASS",
                        "Markettype 64 daily rows begin before the corresponding "
                        "vendor calendar begins, so market-level closure is not "
                        "consistent.",
                        ["calendar.market64_pre_calendar_counterexample"],
                    ),
                    subcheck(
                        "official_exchange_and_session_rules",
                        (
                            "FAIL"
                            if missing_fields(
                                fields["TRD_Cale"],
                                (
                                    "exchange_mic",
                                    "session_name",
                                    "session_open",
                                    "session_close",
                                ),
                            )
                            else "PASS"
                        ),
                        "The observed vendor calendar has no official exchange-MIC "
                        "mapping or continuous/auction session-rule intervals.",
                        ["observed_fields.TRD_Cale"],
                    ),
                ],
            ),
            gate(
                "CORE02-Q2",
                "BLOCK",
                [
                    subcheck(
                        "official_notice_revision_chain",
                        (
                            "FAIL"
                            if missing_fields(
                                fields["TRD_Cale"],
                                calendar_revision_required,
                            )
                            else "PASS"
                        ),
                        "Temporary closures, supplemental notices and historical "
                        "revisions are not represented by published_at, "
                        "available_at and revision_id.",
                        ["observed_fields.TRD_Cale"],
                    )
                ],
            ),
            gate(
                "CORE02-Q3",
                "BLOCK",
                [
                    subcheck(
                        "strictly_increasing_open_dates",
                        "PASS" if calendar_order_clean else "FAIL",
                        "Within each observed Markettype, sorted open dates are "
                        "strictly increasing.",
                        ["calendar.open_date_order_checks"],
                    ),
                    subcheck(
                        "calendar_relations_recomputable",
                        "PASS",
                        "Previous/next open dates and month/quarter/year-end flags "
                        "are deterministically recomputable from unique dated "
                        "Markettype/State rows.",
                        [
                            "calendar.duplicate_key_groups",
                            "calendar.open_date_order_checks",
                        ],
                    ),
                ],
            ),
            gate(
                "CORE02-Q4",
                "REVIEW",
                [
                    subcheck(
                        "venue_specific_rows_not_forced_merged",
                        "PASS",
                        "Observed rows retain Markettype and are keyed per market "
                        "rather than being collapsed to one all-market calendar.",
                        [
                            "calendar.state_coverage",
                            "calendar.daily_vs_vendor_open_date_mismatch",
                        ],
                    )
                ],
            ),
        ],
        "CORE-03": [
            gate(
                "CORE03-Q1",
                "BLOCK",
                [
                    subcheck(
                        "observed_vendor_key_unique",
                        (
                            "PASS"
                            if measurements["daily"]["duplicate_key_groups"] == 0
                            and daily_summary["null_keys"] == 0
                            else "FAIL"
                        ),
                        "The non-overlap 1990-2026 candidate union has unique "
                        "Stkcd/Trddt keys.",
                        [
                            "daily.summary",
                            "daily.duplicate_key_groups",
                        ],
                    ),
                    subcheck(
                        "formal_lineage_key_fields",
                        (
                            "FAIL"
                            if missing_fields(
                                fields["TRD_Dalyr"], daily_lineage_required
                            )
                            else "PASS"
                        ),
                        "source_id, permanent security_id and revision_id are not "
                        "present in the observed candidate.",
                        ["observed_fields.TRD_Dalyr"],
                    ),
                    subcheck(
                        "official_calendar_membership",
                        "UNKNOWN",
                        "Daily dates were compared only with the vendor calendar; "
                        "official exchange calendar evidence is not closed.",
                        ["calendar.daily_vs_vendor_open_date_mismatch"],
                    ),
                ],
            ),
            gate(
                "CORE03-Q2",
                "BLOCK",
                [
                    subcheck(
                        "nonnegative_price_and_ohlc_mechanics",
                        "PASS" if price_mechanics_clean else "FAIL",
                        "Across 18,691,664 non-overlap candidate rows, no negative "
                        "price or OHLC inequality violation was observed.",
                        ["daily.summary"],
                    ),
                    subcheck(
                        "suspension_no_trade_null_zero_semantics",
                        "UNKNOWN",
                        "The hash-bound manual closes Trdsta 1-16 meanings, but "
                        "does not prove every NULL-versus-zero encoding for "
                        "suspension and no-trade observations.",
                        [
                            "daily.summary",
                            "daily.trade_status_values",
                            "external_evidence.csmar_stock_trading_manual",
                        ],
                    ),
                ],
            ),
            gate(
                "CORE03-Q3",
                "BLOCK",
                [
                    subcheck(
                        "nonnegative_volume_and_turnover_mechanics",
                        "PASS" if volume_mechanics_clean else "FAIL",
                        "No negative volume or turnover observation was found.",
                        ["daily.summary"],
                    ),
                    subcheck(
                        "core_price_volume_units",
                        "PASS",
                        "The hash-bound 2024-02-05 manual defines the core OHLC, "
                        "volume, turnover and market-cap units.",
                        [
                            "observed_fields.TRD_Dalyr",
                            "external_evidence.csmar_stock_trading_manual",
                        ],
                    ),
                    subcheck(
                        "unknown_not_encoded_as_zero",
                        "UNKNOWN",
                        "The manual and local mechanics do not prove that every "
                        "historical unknown value was never encoded as zero.",
                        ["daily.summary"],
                    ),
                ],
            ),
            gate(
                "CORE03-Q4",
                "BLOCK",
                [
                    subcheck(
                        "observed_date_market_matrix",
                        "PASS",
                        "An observed row/date/Markettype coverage matrix is "
                        "recomputable from the candidate.",
                        ["daily.market_coverage"],
                    ),
                    subcheck(
                        "expected_universe_state_distinctions",
                        "FAIL",
                        "There is no official expected security-day skeleton that "
                        "distinguishes not-listed, suspended, no-trade and missing.",
                        [
                            "daily.market_coverage",
                            "daily.trade_status_values",
                        ],
                    ),
                ],
            ),
            gate(
                "CORE03-Q5",
                "BLOCK",
                [
                    subcheck(
                        "close_availability_revision_chain",
                        (
                            "FAIL"
                            if missing_fields(
                                fields["TRD_Dalyr"], daily_pit_required
                            )
                            else "PASS"
                        ),
                        "exchange_close_at, available_at and revision_id are absent; "
                        "the no-leakage revision rule cannot be satisfied by the "
                        "candidate as-is.",
                        ["observed_fields.TRD_Dalyr"],
                    )
                ],
            ),
            gate(
                "CORE03-Q6",
                "REVIEW",
                [
                    subcheck(
                        "same_vendor_export_overlap",
                        "PASS",
                        "The existing structured-candidate audit found zero field "
                        "mismatch on the 2022-2024 overlap of two CSMAR exports.",
                        ["daily.summary"],
                    ),
                    subcheck(
                        "independent_cross_source_tolerance",
                        "UNKNOWN",
                        "No independent official/second-source price comparison and "
                        "conflict retention receipt is closed.",
                        ["calendar.daily_vs_vendor_open_date_mismatch"],
                    ),
                ],
            ),
        ],
        "CORE-06": [
            gate(
                "CORE06-Q1",
                "BLOCK",
                [
                    subcheck(
                        "vendor_trade_status_values_present",
                        "PASS",
                        "TRD_Dalyr retains observed Trdsta values and the "
                        "hash-bound manual defines codes 1-16; they are not "
                        "replaced with a boolean tradeable flag.",
                        [
                            "daily.trade_status_values",
                            "external_evidence.csmar_stock_trading_manual",
                        ],
                    ),
                    subcheck(
                        "complete_listed_security_day_explanation",
                        "FAIL",
                        "No official expected listed-security/day panel and state "
                        "dictionary distinguishes not-listed, suspended, no-trade "
                        "and missing for every exchange day.",
                        [
                            "daily.market_coverage",
                            "observed_fields.TRD_Dalyr",
                        ],
                    ),
                ],
            ),
            gate(
                "CORE06-Q2",
                "BLOCK",
                [
                    subcheck(
                        "announcement_not_after_opening_use",
                        "FAIL" if bdt_after_effective > 0 else "UNKNOWN",
                        "BDT_Trdchg contains announcement dates later than effective "
                        "dates and no available_at rule; using the state at the "
                        "earlier date would be a PIT hazard.",
                        ["status_and_rules.bdt_trdchg_summary"],
                    ),
                    subcheck(
                        "status_interval_overlap",
                        "UNKNOWN",
                        "Complete normalized status intervals do not yet exist, so "
                        "overlap testing cannot be completed.",
                        ["status_and_rules.bdt_trdchg_types"],
                    ),
                ],
            ),
            gate(
                "CORE06-Q3",
                "BLOCK",
                [
                    subcheck(
                        "daily_limit_fields_observed",
                        "PASS",
                        "PreClosePrice, LimitDown, LimitUp and LimitStatus are "
                        "present and their observed null counts are measured.",
                        ["daily.summary", "observed_fields.TRD_Dalyr"],
                    ),
                    subcheck(
                        "historical_limit_rule_recomputation_inputs",
                        "FAIL",
                        "Board, risk-state interval, rule_version, tick size and "
                        "rounding-rule inputs are not jointly available for full "
                        "historical recomputation.",
                        [
                            "observed_fields.TRD_Dalyr",
                            "observed_fields.BDT_Trdchg",
                        ],
                    ),
                ],
            ),
            gate(
                "CORE06-Q4",
                "BLOCK",
                [
                    subcheck(
                        "special_event_candidates_observed",
                        "PASS",
                        "TRD_NoLimit and BDT_Trdchg preserve dated reason/type "
                        "codes as separate local candidates; the hash-bound "
                        "manual defines P8601-P8610.",
                        [
                            "status_and_rules.no_limit_summary",
                            "status_and_rules.bdt_trdchg_types",
                            "external_evidence.csmar_stock_trading_manual",
                        ],
                    ),
                    subcheck(
                        "historical_special_rule_versions",
                        (
                            "FAIL"
                            if missing_fields(
                                fields["TRD_NoLimit"], status_pit_required
                            )
                            else "PASS"
                        ),
                        "IPO/relisting/ST/delisting/special/no-limit rule versions "
                        "and availability chains are not present.",
                        [
                            "observed_fields.TRD_NoLimit",
                            "status_and_rules.no_limit_reason_codes",
                        ],
                    ),
                ],
            ),
            gate(
                "CORE06-Q5",
                "REVIEW",
                [
                    subcheck(
                        "source_fields_not_fill_probability",
                        "PASS",
                        "The observed source candidates retain status, volume and "
                        "limit fields and do not contain an inferred fill "
                        "probability.",
                        ["observed_fields.TRD_Dalyr"],
                    ),
                    subcheck(
                        "formal_orderable_traded_tradeable_model",
                        "UNKNOWN",
                        "A formal versioned model separating orderable, traded and "
                        "strategy-tradeable states has not been published.",
                        ["daily.trade_status_values"],
                    ),
                ],
            ),
        ],
    }
    return {
        "targets": gates,
        "status_counts": {
            status_value: sum(
                gate_item["status"] == status_value
                for target_gates in gates.values()
                for gate_item in target_gates
            )
            for status_value in sorted(ALLOWED_STATUSES)
        },
        "blocking_status_counts": {
            status_value: sum(
                gate_item["status"] == status_value
                and gate_item["severity"] == "BLOCK"
                for target_gates in gates.values()
                for gate_item in target_gates
            )
            for status_value in sorted(ALLOWED_STATUSES)
        },
    }


def gate_evidence_bundle(
    evaluations: dict[str, Any],
    *,
    measurements_path: Path,
    workspace: Path,
) -> dict[str, Any]:
    measurement_binding = file_binding(measurements_path, workspace)
    targets: dict[str, Any] = {}
    for core_id, gate_rows in evaluations["targets"].items():
        targets[core_id] = {}
        for gate_row in gate_rows:
            subcheck_counts = {
                status_value: sum(
                    item["status"] == status_value
                    for item in gate_row["subchecks"]
                )
                for status_value in sorted(ALLOWED_STATUSES)
            }
            targets[core_id][gate_row["gate_id"]] = {
                "status": gate_row["status"],
                "evidence": [
                    {
                        **measurement_binding,
                        "assertion": (
                            "Complete gate status is derived from all clause-level "
                            f"subchecks; counts={subcheck_counts}. Mechanical "
                            "subcheck PASS does not override FAIL/UNKNOWN clauses."
                        ),
                    }
                ],
            }
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "purpose": (
            "Hash-bound local candidate evidence for selected CORE quality gates; "
            "not an admission or quality promotion."
        ),
        "targets": targets,
    }


def readme_text(
    evaluations: dict[str, Any],
    measurements: dict[str, Any],
) -> str:
    rows = []
    for core_id in TARGETS:
        for item in evaluations["targets"][core_id]:
            rows.append(
                f"| {core_id} | {item['gate_id']} | {item['severity']} | "
                f"{item['status']} |"
            )
    daily = measurements["daily"]["summary"]
    market64 = measurements["calendar"]["market64_pre_calendar_counterexample"]
    bdt = measurements["status_and_rules"]["bdt_trdchg_summary"]
    return "\n".join(
        [
            "# CORE-01/02/03/06 本地证据闭合审计",
            "",
            "**结论：仍不能正式准入。** 本报告只把现有候选中可复核的机械事实",
            "和直接反例变成哈希绑定证据；没有修改 catalog、质量状态、正式快照",
            "或发布目录。",
            "",
            "| CORE | gate_id | severity | status |",
            "|---|---|---:|---:|",
            *rows,
            "",
            "## 本地已经闭合的整门",
            "",
            "- `CORE02-Q3=PASS`：按 Markettype 的开市日序列严格递增，",
            "  previous/next 与月末、季末、年末标识可重算。",
            "- `CORE02-Q4=PASS`（REVIEW）：场所差异仍按 Markettype 分开，",
            "  未被强制合并。",
            "",
            "其余机械 PASS 只是门内子检验，例如未复权日行情键唯一、价格非负、",
            "OHLC 约束、成交量/额非负；它们不等于整门通过。",
            "",
            "## 关键实测",
            "",
            f"- 日行情非重叠并集：{daily['row_count']:,} 行、"
            f"{daily['securities']:,} 个代码、{daily['trade_dates']:,} 个日期，"
            f"{daily['first_date']} 至 {daily['last_date']}。",
            f"- 负价格、OHLC 违规、负成交量/额均为 0；"
            f"`LimitStatus` 空值 {daily['null_limit_status_rows']:,} 行。",
            "- 历史公司表存在本地代码复用：`600018` 对应两个不同",
            "  InstitutionID/上市阶段，证明代码不能直接充当永久 security_id。",
            f"- Markettype=64 有 {market64['distinct_dates']:,} 个日行情日期、"
            f"{market64['daily_rows']:,} 行早于供应商日历起点。",
            f"- BDT_Trdchg 有 {bdt['announcement_after_effective_rows']:,} 行"
            "  公告日期晚于生效日期，且无 available_at；直接使用生效日会有 PIT 风险。",
            "",
            "## 仍需外部闭合",
            "",
            "- 三交易所官方永久身份/退市与代码历史、正式场所映射及覆盖基准。",
            "- 官方交易日历、交易时段、临时停市/补充通知、发布时间和修订链。",
            "- CSMAR 字段字典、单位和 NULL/0/状态码语义及合法长期保存/自动化边界。",
            "- 历史涨跌停、ST、重上市、退市整理期、无涨跌幅限制的官方规则版本。",
            "- `available_at`、`revision_id`、保守决策时点规则及第二来源冲突回执。",
            "",
            "## 发布就绪度",
            "",
            "`NO`。四个正式 dataset identity 仍未在 qdata 研究意图下解析；",
            "本报告不会也不能生成正式快照。",
            "",
        ]
    )


def report_manifest(output: Path, workspace: Path) -> dict[str, Any]:
    payloads = sorted(
        path for path in output.iterdir() if path.name != "REPORT_MANIFEST.json"
    )
    files = [
        {
            "path": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in payloads
    ]
    generator = Path(__file__).resolve()
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "files": files,
        "canonical_payload_bundle_sha256": sha256_bytes(
            canonical_json_bytes(files)
        ),
        "generator": file_binding(generator, workspace),
        "manifest_policy": {
            "allowed_report_files": sorted(REPORT_FILES),
            "self_excluded_from_files": "REPORT_MANIFEST.json",
            "unregistered_residuals_rejected": True,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--output", default=DEFAULT_REPORT)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace only the registered files in the selected report directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace).resolve(strict=True)
    output = (workspace / args.output).resolve()
    require_report_path(output, workspace)
    if output.exists():
        if not args.replace:
            raise FileExistsError(
                f"report already exists; pass --replace after review: {output}"
            )
        reject_unexpected_report_residuals(output)
    else:
        output.mkdir(parents=True)

    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "duckdb is required; use the central data-engineering Python runtime"
        ) from exc

    static_inputs = {
        "contract": file_binding(workspace / CONTRACT, workspace),
        "closure_audit": file_binding(workspace / CLOSURE_AUDIT, workspace),
        "structured_measurements": file_binding(
            workspace / STRUCTURED_MEASUREMENTS, workspace
        ),
        "api_probe_readme": file_binding(
            workspace / API_PROBE_README, workspace
        ),
        "api_probe_verification": file_binding(
            workspace / API_PROBE_VERIFICATION, workspace
        ),
    }
    contract_json = load_json(workspace / CONTRACT)
    contract_targets = {
        item["plan_id"]: item
        for item in contract_json.get("datasets", [])
        if item.get("plan_id") in TARGETS
    }
    if set(contract_targets) != set(TARGETS):
        raise ValueError("contract target set does not match selected CORE set")

    bindings, parquet_paths = candidate_bindings(workspace)
    official_evidence = official_evidence_receipt()
    official_evidence_path = output / "OFFICIAL_EVIDENCE.json"
    write_json(official_evidence_path, official_evidence)
    connection = duckdb.connect(":memory:")
    create_views(connection, parquet_paths)
    observed = collect_measurements(connection)
    observed["external_evidence"] = {
        **official_evidence,
        "report_binding": file_binding(official_evidence_path, workspace),
        "api_entitlement_probe": {
            "readme": static_inputs["api_probe_readme"],
            "verification": static_inputs["api_probe_verification"],
            "conclusion": (
                "Python API listDbs returned -2501 for belong=0 and belong=1. "
                "This proves only that API entitlement is unavailable; it does "
                "not negate the separate manual Tsinghua web-export research "
                "access."
            ),
        },
    }
    evaluations = build_gate_evaluations(observed)
    measurements = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "scope": list(TARGETS),
        "candidate_status": "INSPECTION_ONLY__NOT_ADMITTED",
        "formal_mutations": False,
        "static_inputs": static_inputs,
        "candidate_bindings": bindings,
        "observations": observed,
        "gate_evaluations": evaluations,
        "contract_gate_ids": {
            core_id: [
                {
                    "gate_id": gate_item["gate_id"],
                    "severity": gate_item["severity"],
                }
                for gate_item in contract_targets[core_id]["quality_gates"]
            ]
            for core_id in TARGETS
        },
    }
    measurement_path = output / "MEASUREMENTS.json"
    write_json(measurement_path, measurements)
    write_json(
        output / "GATE_EVIDENCE.json",
        gate_evidence_bundle(
            evaluations,
            measurements_path=measurement_path,
            workspace=workspace,
        ),
    )
    (output / "README.md").write_text(
        readme_text(evaluations, observed),
        encoding="utf-8",
        newline="\n",
    )
    verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": "PASS_LOCAL_EVIDENCE_AUDIT__FORMAL_ADMISSION_BLOCKED",
        "checks": {
            "target_set_exact": set(evaluations["targets"]) == set(TARGETS),
            "candidate_only": True,
            "formal_mutations": False,
            "blocking_gate_pass_count": evaluations[
                "blocking_status_counts"
            ]["PASS"],
            "blocking_gate_fail_count": evaluations[
                "blocking_status_counts"
            ]["FAIL"],
            "blocking_gate_unknown_count": evaluations[
                "blocking_status_counts"
            ]["UNKNOWN"],
            "all_gate_statuses_allowed": all(
                gate_item["status"] in ALLOWED_STATUSES
                for gate_rows in evaluations["targets"].values()
                for gate_item in gate_rows
            ),
            "all_candidate_trees_content_hashed": all(
                item["curated_tree"]["file_count"] > 0
                and bool(item["curated_tree"]["tree_sha256"])
                for item in bindings.values()
            ),
            "formal_admission_ready": False,
        },
        "prohibitions_observed": [
            "no datasets.json write",
            "no source registry write",
            "no quality promotion",
            "no data/market/snapshots write",
            "no data/published write",
            "no candidate mutation",
        ],
    }
    write_json(output / "VERIFICATION.json", verification)
    write_json(
        output / "REPORT_MANIFEST.json",
        report_manifest(output, workspace),
    )
    reject_unexpected_report_residuals(output)
    print(
        json.dumps(
            {
                "run_id": RUN_ID,
                "output": workspace_relative(output, workspace),
                "status": verification["status"],
                "blocking_status_counts": evaluations[
                    "blocking_status_counts"
                ],
                "gate_evidence": workspace_relative(
                    output / "GATE_EVIDENCE.json", workspace
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
