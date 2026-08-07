#!/usr/bin/env python3
"""Run an isolated capacity and pipeline pilot for the Quantification workspace.

The pilot deliberately does not create formal data directories, change the
catalog, publish snapshots, or perform a historical backfill.  Runtime outputs
are confined to ``tmp/data-capacity-pipeline-pilot/<run_id>``.

Real samples are used for source/encoding measurements where access succeeds.
Small deterministic fixtures are used only to test compaction, PIT selection,
quality-rule detection, and a read-only DuckDB projection.  Fixture sizes must
not be used as estimates of source coverage or historical volume.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable


GIB = 1024**3
TIB = 1024**4


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_fingerprint(root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    canonical = json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "exists": root.exists(),
        "file_count": len(records),
        "total_bytes": sum(item["bytes"] for item in records),
        "contract_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def resolved_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def quote_sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def quantiles(values: list[int]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "max": None,
            "mean": None,
        }
    ordered = sorted(values)

    def nearest(q: float) -> int:
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
        return ordered[index]

    return {
        "count": len(ordered),
        "min": ordered[0],
        "p50": nearest(0.50),
        "p90": nearest(0.90),
        "p95": nearest(0.95),
        "max": ordered[-1],
        "mean": round(statistics.fmean(ordered), 3),
    }


def record_probe(
    results: dict[str, Any],
    name: str,
    operation: Callable[[], dict[str, Any]],
) -> None:
    started = time.perf_counter()
    try:
        payload = operation()
        results[name] = {
            "status": "PASS",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            **payload,
        }
    except Exception as exc:  # Source probes must not hide later local tests.
        results[name] = {
            "status": "ERROR",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def inspect_existing_parquet(
    workspace: Path, duckdb_module: Any
) -> dict[str, Any]:
    parquet_root = workspace / "data" / "capital-flow-attribution"
    paths = sorted(parquet_root.rglob("*.parquet")) if parquet_root.exists() else []
    connection = duckdb_module.connect()
    files: list[dict[str, Any]] = []
    try:
        for path in paths:
            row_count = int(
                connection.execute(
                    "SELECT count(*) FROM read_parquet(?)", [str(path.resolve())]
                ).fetchone()[0]
            )
            cursor = connection.execute(
                "SELECT * FROM read_parquet(?) LIMIT 0", [str(path.resolve())]
            )
            column_count = len(cursor.description)
            size = path.stat().st_size
            files.append(
                {
                    "path": path.relative_to(workspace).as_posix(),
                    "bytes": size,
                    "rows": row_count,
                    "columns": column_count,
                    "bytes_per_row": (
                        round(size / row_count, 6) if row_count else None
                    ),
                }
            )
    finally:
        connection.close()

    rows = sum(item["rows"] for item in files)
    size = sum(item["bytes"] for item in files)
    non_empty = [item for item in files if item["rows"]]
    return {
        "status": "PASS",
        "interpretation": (
            "Observed local Parquet evidence only; these are project artifacts, "
            "not admitted CORE snapshots."
        ),
        "file_count": len(files),
        "total_bytes": size,
        "total_rows_including_repeated_run_artifacts": rows,
        "weighted_bytes_per_row": round(size / rows, 6) if rows else None,
        "per_file_bytes_per_row": {
            "min": min(
                (item["bytes_per_row"] for item in non_empty), default=None
            ),
            "median": (
                round(
                    statistics.median(
                        item["bytes_per_row"] for item in non_empty
                    ),
                    6,
                )
                if non_empty
                else None
            ),
            "max": max(
                (item["bytes_per_row"] for item in non_empty), default=None
            ),
        },
        "files": files,
    }


def probe_akshare_reference(
    output: Path, ak_module: Any
) -> dict[str, Any]:
    tables: dict[str, tuple[Callable[[], Any], str]] = {
        "sse_main_current": (
            lambda: ak_module.stock_info_sh_name_code(symbol="主板A股"),
            "official endpoint through open-source adapter",
        ),
        "sse_star_current": (
            lambda: ak_module.stock_info_sh_name_code(symbol="科创板"),
            "official endpoint through open-source adapter",
        ),
        "szse_a_current": (
            lambda: ak_module.stock_info_sz_name_code(symbol="A股列表"),
            "official endpoint through open-source adapter",
        ),
        "bse_current": (
            ak_module.stock_info_bj_name_code,
            "official endpoint through open-source adapter; WAF may block",
        ),
        "sse_delisted": (
            ak_module.stock_info_sh_delist,
            "official endpoint through open-source adapter",
        ),
        "szse_delisted": (
            ak_module.stock_info_sz_delist,
            "official endpoint through open-source adapter",
        ),
        "trading_calendar": (
            ak_module.tool_trade_date_hist_sina,
            "secondary calendar adapter; not an official admitted calendar",
        ),
        "index_catalog": (
            ak_module.index_stock_info,
            "secondary index catalog; known to be incomplete",
        ),
    }
    output.mkdir(parents=True, exist_ok=True)
    probes: dict[str, Any] = {}
    for name, (operation, source_role) in tables.items():
        started = time.perf_counter()
        try:
            frame = operation()
            target = output / f"{name}.csv"
            frame.to_csv(target, index=False, encoding="utf-8-sig")
            payload: dict[str, Any] = {
                "status": "PASS",
                "source_role": source_role,
                "rows": int(len(frame)),
                "columns": [str(value) for value in frame.columns],
                "normalized_evidence_csv_bytes": target.stat().st_size,
                "evidence_sha256": sha256_file(target),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
            if name == "trading_calendar" and len(frame):
                column = frame.columns[0]
                dates = frame[column].astype(str)
                payload["min_value"] = dates.min()
                payload["max_value"] = dates.max()
                payload["warning"] = (
                    "A published future calendar is a schedule, not proof that "
                    "the future session actually occurred."
                )
            probes[name] = payload
        except Exception as exc:
            probes[name] = {
                "status": "ERROR",
                "source_role": source_role,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
    component_names = [
        "sse_main_current",
        "sse_star_current",
        "szse_a_current",
        "bse_current",
    ]
    component_rows = [
        probes[name]["rows"]
        for name in component_names
        if probes.get(name, {}).get("status") == "PASS"
    ]
    probes["a_share_current_component_sum"] = {
        "status": (
            "PASS"
            if len(component_rows) == len(component_names)
            else "PARTIAL"
        ),
        "source_role": (
            "Derived sum of the four exchange component probes; no extra "
            "aggregate request."
        ),
        "rows": sum(component_rows),
        "component_count_passed": len(component_rows),
        "component_count_expected": len(component_names),
    }
    return {
        "status": (
            "PASS"
            if any(item["status"] == "PASS" for item in probes.values())
            else "ERROR"
        ),
        "admission_status": "INSPECTION_ONLY__NOT_FORMALLY_ADMITTED",
        "probes": probes,
    }


def probe_tencent_daily(
    output: Path,
    requests_module: Any,
    pandas_module: Any,
    demjson_module: Any,
    duckdb_module: Any,
) -> dict[str, Any]:
    symbols = [
        "sh600000",
        "sz000001",
        "sh600519",
        "sz000858",
        "sz300750",
        "sh688981",
        "sh601988",
        "sz002594",
        "sh600009",
        "sz000002",
        "sh600036",
        "sz300059",
    ]
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    output.mkdir(parents=True, exist_ok=True)
    frames = []
    responses: list[dict[str, Any]] = []
    observed_row_widths: set[int] = set()
    session = requests_module.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Quantification engineering capacity pilot; "
                "bounded inspection sample; no bulk backfill"
            )
        }
    )
    for symbol in symbols:
        params = {
            "_var": "kline_day_capacity_pilot",
            "param": f"{symbol},day,2018-01-01,2026-12-31,640,",
            "r": "0.20260726",
        }
        started = time.perf_counter()
        response = session.get(url, params=params, timeout=30)
        raw_path = output / f"{symbol}.txt"
        raw_path.write_bytes(response.content)
        response.raise_for_status()
        marker = response.text.find("={")
        if marker < 0:
            raise ValueError(f"{symbol}: unexpected response wrapper")
        decoded = demjson_module.decode(response.text[marker + 1 :])
        payload = decoded["data"][symbol]
        rows = payload.get("day") or payload.get("qfqday") or payload.get("hfqday")
        if not rows:
            raise ValueError(f"{symbol}: no day rows in response")
        source_frame = pandas_module.DataFrame(rows)
        observed_row_widths.add(int(source_frame.shape[1]))
        if source_frame.shape[1] < 6:
            raise ValueError(
                f"{symbol}: expected at least 6 raw fields, "
                f"observed {source_frame.shape[1]}"
            )
        # Only the first five field meanings are used here.  Later raw fields
        # have changed across adapter versions, so they are deliberately kept
        # under neutral names until a source contract freezes their semantics
        # and units.
        frame = pandas_module.DataFrame(
            {
                "trade_date": source_frame.iloc[:, 0],
                "open": source_frame.iloc[:, 1],
                "close": source_frame.iloc[:, 2],
                "high": source_frame.iloc[:, 3],
                "low": source_frame.iloc[:, 4],
            }
        )
        for position in range(5, source_frame.shape[1]):
            column = f"source_field_{position:02d}"
            if position == 6:
                frame[column] = source_frame.iloc[:, position].map(
                    lambda value: json.dumps(
                        value, ensure_ascii=False, sort_keys=True
                    )
                )
            else:
                frame[column] = pandas_module.to_numeric(
                    source_frame.iloc[:, position], errors="coerce"
                )
        for column in ["open", "close", "high", "low"]:
            frame[column] = pandas_module.to_numeric(frame[column], errors="coerce")
        frame["trade_date"] = pandas_module.to_datetime(
            frame["trade_date"], errors="coerce"
        ).dt.date
        frame.insert(0, "security_id", symbol)
        frame["source_id"] = "TENCENT_SECONDARY_INSPECTION"
        frames.append(frame)
        responses.append(
            {
                "symbol": symbol,
                "status_code": response.status_code,
                "raw_bytes": raw_path.stat().st_size,
                "raw_sha256": sha256_file(raw_path),
                "rows": int(len(frame)),
                "raw_row_width": int(source_frame.shape[1]),
                "min_trade_date": str(frame["trade_date"].min()),
                "max_trade_date": str(frame["trade_date"].max()),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
        )
        time.sleep(0.10)

    sample = pandas_module.concat(frames, ignore_index=True)
    duplicate_primary_key_rows = int(
        sample.duplicated(["security_id", "trade_date"], keep=False).sum()
    )
    critical_columns = ["trade_date", "open", "close", "high", "low"]
    critical_null_rows = int(sample[critical_columns].isna().any(axis=1).sum())
    ohlc_invalid_rows = int(
        (
            (sample["high"] < sample[["open", "close", "low"]].max(axis=1))
            | (sample["low"] > sample[["open", "close", "high"]].min(axis=1))
        ).sum()
    )
    sample_csv = output / "normalized_sample.csv"
    sample.to_csv(sample_csv, index=False, encoding="utf-8")
    sample_parquet = output / "normalized_sample.parquet"
    connection = duckdb_module.connect()
    try:
        connection.register("daily_sample", sample)
        connection.execute(
            f"COPY daily_sample TO '{quote_sql_path(sample_parquet)}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        connection.close()

    raw_bytes = sum(item["raw_bytes"] for item in responses)
    rows = len(sample)
    schema_drift_detected = observed_row_widths != {6}
    return {
        "status": (
            "PASS_WITH_SCHEMA_DRIFT"
            if schema_drift_detected
            else "PASS"
        ),
        "admission_status": "SECONDARY_ENGINEERING_SAMPLE_ONLY",
        "warning": (
            "AKShare code is open source, but that does not grant rights to "
            "bulk-store or redistribute the Tencent underlying data.  Raw "
            "fields after OHLC retain neutral names until their semantics and "
            "units are admitted."
        ),
        "schema_drift_detected": schema_drift_detected,
        "observed_raw_row_widths": sorted(observed_row_widths),
        "local_adapter_risk": (
            "The installed AKShare 1.18.64 implementation truncates the "
            "observed 11-field rows to six fields and labels field 5 as "
            "amount.  Current AKShare documentation describes volume, "
            "turnover and amount separately.  A production adapter must not "
            "inherit this mapping without an explicit field-and-unit check."
        ),
        "symbols": symbols,
        "rows": rows,
        "raw_response_bytes": raw_bytes,
        "raw_response_bytes_per_row": round(raw_bytes / rows, 6),
        "normalized_csv_bytes": sample_csv.stat().st_size,
        "normalized_csv_bytes_per_row": round(sample_csv.stat().st_size / rows, 6),
        "parquet_zstd_bytes": sample_parquet.stat().st_size,
        "parquet_zstd_bytes_per_row": round(
            sample_parquet.stat().st_size / rows, 6
        ),
        "parquet_sha256": sha256_file(sample_parquet),
        "sample_quality_observation": {
            "duplicate_primary_key_rows": duplicate_primary_key_rows,
            "critical_null_rows": critical_null_rows,
            "ohlc_invalid_rows": ohlc_invalid_rows,
            "status": (
                "PASS_FOR_THE_BOUNDED_SAMPLE"
                if (
                    duplicate_primary_key_rows == 0
                    and critical_null_rows == 0
                    and ohlc_invalid_rows == 0
                )
                else "SAMPLE_HAS_QUALITY_FAILURES"
            ),
        },
        "responses": responses,
    }


def probe_cninfo_metadata_and_heads(
    output: Path, requests_module: Any
) -> dict[str, Any]:
    query_url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    static_root = "https://static.cninfo.com.cn/"
    years = [2010, 2015, 2020, 2025]
    categories = {
        "annual": "category_ndbg_szsh;",
        "semiannual": "category_bndbg_szsh;",
        "first_quarter": "category_yjdbg_szsh;",
        "third_quarter": "category_sjdbg_szsh;",
    }
    output.mkdir(parents=True, exist_ok=True)
    session = requests_module.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 Quantification bounded metadata capacity pilot"
            ),
            "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch",
            "Accept": "application/json, text/plain, */*",
        }
    )
    strata: list[dict[str, Any]] = []
    lengths: list[int] = []
    for year in years:
        for category_name, category_code in categories.items():
            data = {
                "pageNum": "1",
                "pageSize": "30",
                "column": "szse",
                "tabName": "fulltext",
                "plate": "",
                "stock": "",
                "searchkey": "",
                "secid": "",
                "category": category_code,
                "trade": "",
                "seDate": f"{year}-01-01~{year}-12-31",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            started = time.perf_counter()
            response = session.post(query_url, data=data, timeout=30)
            response.raise_for_status()
            raw_path = output / f"{year}-{category_name}.json"
            raw_path.write_bytes(response.content)
            payload = response.json()
            announcements = payload.get("announcements") or []
            head_results: list[dict[str, Any]] = []
            for item in announcements[:3]:
                relative = item.get("adjunctUrl")
                if not relative:
                    continue
                head_url = static_root + relative.lstrip("/")
                head_started = time.perf_counter()
                try:
                    head = session.head(
                        head_url, allow_redirects=True, timeout=30
                    )
                    length_text = head.headers.get("Content-Length")
                    length = int(length_text) if length_text else None
                    if length is not None:
                        lengths.append(length)
                    head_results.append(
                        {
                            "announcement_id": item.get("announcementId"),
                            "announcement_title": item.get("announcementTitle"),
                            "announcement_time": item.get("announcementTime"),
                            "status_code": head.status_code,
                            "content_type": head.headers.get("Content-Type"),
                            "content_length": length,
                            "elapsed_seconds": round(
                                time.perf_counter() - head_started, 3
                            ),
                        }
                    )
                except Exception as exc:
                    head_results.append(
                        {
                            "announcement_id": item.get("announcementId"),
                            "status": "ERROR",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                time.sleep(0.10)
            strata.append(
                {
                    "year": year,
                    "category": category_name,
                    "total_announcement": int(payload.get("totalAnnouncement") or 0),
                    "returned_rows": len(announcements),
                    "metadata_response_bytes": raw_path.stat().st_size,
                    "metadata_sha256": sha256_file(raw_path),
                    "head_samples": head_results,
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                }
            )
            time.sleep(0.15)
    return {
        "status": "PASS",
        "admission_status": "METADATA_INSPECTION_ONLY__PDFS_NOT_DOWNLOADED",
        "warning": (
            "The first three results in each stratum are a convenience sample, "
            "not a random or tail-complete document-size sample."
        ),
        "years": years,
        "categories": list(categories),
        "strata": strata,
        "document_content_length_bytes": quantiles(lengths),
    }


def run_compaction_fixture(
    output: Path, pandas_module: Any, duckdb_module: Any
) -> dict[str, Any]:
    delta_root = output / "daily_delta"
    delta_root.mkdir(parents=True, exist_ok=True)
    connection = duckdb_module.connect()
    delta_paths: list[Path] = []
    try:
        for day_index in range(22):
            trade_date = dt.date(2026, 7, 1) + dt.timedelta(days=day_index)
            frame = pandas_module.DataFrame(
                {
                    "security_id": [
                        f"CN.TEST.{index:06d}" for index in range(1, 201)
                    ],
                    "trade_date": [trade_date] * 200,
                    "close": [
                        round(
                            5.0
                            + ((index * 17 + day_index * 13) % 2000) / 100,
                            2,
                        )
                        for index in range(1, 201)
                    ],
                    "volume": [
                        1000 + ((index * 7919 + day_index * 104729) % 2_000_000)
                        for index in range(1, 201)
                    ],
                    "available_at": [
                        dt.datetime.combine(
                            trade_date, dt.time(16, 30), tzinfo=dt.timezone.utc
                        )
                    ]
                    * 200,
                }
            )
            path = delta_root / f"{trade_date.isoformat()}.delta.parquet"
            connection.register("delta_frame", frame)
            connection.execute(
                f"COPY delta_frame TO '{quote_sql_path(path)}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            connection.unregister("delta_frame")
            delta_paths.append(path)

        compacted = output / "2026-07.compacted.parquet"
        glob_path = quote_sql_path(delta_root / "*.parquet")
        connection.execute(
            f"COPY (SELECT * FROM read_parquet('{glob_path}')) "
            f"TO '{quote_sql_path(compacted)}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        delta_rows = int(
            connection.execute(
                f"SELECT count(*) FROM read_parquet('{glob_path}')"
            ).fetchone()[0]
        )
        compacted_rows = int(
            connection.execute(
                "SELECT count(*) FROM read_parquet(?)", [str(compacted)]
            ).fetchone()[0]
        )
        duplicate_keys = int(
            connection.execute(
                f"""
                SELECT count(*) - count(DISTINCT (security_id, trade_date))
                FROM read_parquet('{glob_path}')
                """
            ).fetchone()[0]
        )
        missing_from_compacted = int(
            connection.execute(
                f"""
                SELECT count(*) FROM (
                  (SELECT * FROM read_parquet('{glob_path}'))
                  EXCEPT ALL
                  (SELECT * FROM read_parquet('{quote_sql_path(compacted)}'))
                )
                """
            ).fetchone()[0]
        )
        extra_in_compacted = int(
            connection.execute(
                f"""
                SELECT count(*) FROM (
                  (SELECT * FROM read_parquet('{quote_sql_path(compacted)}'))
                  EXCEPT ALL
                  (SELECT * FROM read_parquet('{glob_path}'))
                )
                """
            ).fetchone()[0]
        )
    finally:
        connection.close()

    delta_bytes = sum(path.stat().st_size for path in delta_paths)
    compacted_bytes = compacted.stat().st_size
    passed = (
        delta_rows == compacted_rows
        and duplicate_keys == 0
        and missing_from_compacted == 0
        and extra_in_compacted == 0
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "fixture_only": True,
        "warning": "Do not use fixture byte sizes as a source-volume estimate.",
        "daily_files": len(delta_paths),
        "rows": delta_rows,
        "delta_bytes": delta_bytes,
        "compacted_bytes": compacted_bytes,
        "small_file_reduction_ratio": round(
            delta_bytes / compacted_bytes, 6
        ),
        "observed_input_plus_output_lower_bound_bytes": (
            delta_bytes + compacted_bytes
        ),
        "duplicate_primary_keys": duplicate_keys,
        "missing_from_compacted": missing_from_compacted,
        "extra_in_compacted": extra_in_compacted,
        "compacted_sha256": sha256_file(compacted),
    }


def run_pit_fixture(duckdb_module: Any) -> dict[str, Any]:
    connection = duckdb_module.connect()
    try:
        connection.execute(
            """
            CREATE TABLE pit_facts (
              issuer_id VARCHAR,
              report_period DATE,
              account_code VARCHAR,
              value DOUBLE,
              available_at TIMESTAMP,
              revision_id INTEGER
            )
            """
        )
        connection.execute(
            """
            INSERT INTO pit_facts VALUES
              ('CN.TEST.1', DATE '2022-12-31', 'revenue', 100.0,
               TIMESTAMP '2023-04-20 18:00:00', 1),
              ('CN.TEST.1', DATE '2022-12-31', 'revenue', 110.0,
               TIMESTAMP '2023-05-10 18:00:00', 2),
              ('CN.TEST.1', DATE '2023-03-31', 'revenue', 30.0,
               TIMESTAMP '2023-04-28 18:00:00', 1)
            """
        )

        def value_as_of(timestamp: str) -> float:
            row = connection.execute(
                """
                SELECT value
                FROM pit_facts
                WHERE issuer_id = 'CN.TEST.1'
                  AND report_period = DATE '2022-12-31'
                  AND account_code = 'revenue'
                  AND available_at <= CAST(? AS TIMESTAMP)
                QUALIFY row_number() OVER (
                  PARTITION BY issuer_id, report_period, account_code
                  ORDER BY available_at DESC, revision_id DESC
                ) = 1
                """,
                [timestamp],
            ).fetchone()
            return float(row[0]) if row else float("nan")

        before_revision = value_as_of("2023-04-30 00:00:00")
        after_revision = value_as_of("2023-05-20 00:00:00")
    finally:
        connection.close()
    passed = before_revision == 100.0 and after_revision == 110.0
    return {
        "status": "PASS" if passed else "FAIL",
        "fixture_only": True,
        "as_of_before_revision": before_revision,
        "as_of_after_revision": after_revision,
        "expected_before_revision": 100.0,
        "expected_after_revision": 110.0,
    }


def run_quality_fixture(pandas_module: Any) -> dict[str, Any]:
    frame = pandas_module.DataFrame(
        [
            {
                "security_id": "CN.TEST.1",
                "trade_date": "2026-07-01",
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "volume": 100,
                "source": "A",
            },
            {
                "security_id": "CN.TEST.1",
                "trade_date": "2026-07-01",
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.6,
                "volume": 100,
                "source": "B",
            },
            {
                "security_id": "CN.TEST.2",
                "trade_date": "2026-07-01",
                "open": 10.0,
                "high": 8.0,
                "low": 9.0,
                "close": 10.0,
                "volume": None,
                "source": "A",
            },
            {
                "security_id": "CN.TEST.3",
                "trade_date": "2026-07-01",
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "close": 0.0,
                "volume": 0,
                "source": "A",
            },
        ]
    )
    duplicate_rows = int(
        frame.duplicated(["security_id", "trade_date"], keep=False).sum()
    )
    ohlc_invalid = int(
        (
            (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
            | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
        ).sum()
    )
    null_volume = int(frame["volume"].isna().sum())
    zero_volume = int((frame["volume"] == 0).sum())
    conflicts = (
        frame.groupby(["security_id", "trade_date"])["close"].nunique().gt(1)
    )
    source_conflicts = int(conflicts.sum())
    passed = (
        duplicate_rows == 2
        and ohlc_invalid == 1
        and null_volume == 1
        and zero_volume == 1
        and source_conflicts == 1
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "fixture_only": True,
        "detected_duplicate_rows": duplicate_rows,
        "detected_ohlc_invalid_rows": ohlc_invalid,
        "null_volume_rows": null_volume,
        "zero_volume_rows": zero_volume,
        "null_and_zero_remain_distinct": null_volume == 1 and zero_volume == 1,
        "detected_multi_source_close_conflicts": source_conflicts,
    }


def run_read_only_projection(
    output: Path, sample_parquet: Path, duckdb_module: Any
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    database = output / "sample_projection.duckdb"
    connection = duckdb_module.connect(str(database))
    try:
        connection.execute(
            f"""
            CREATE VIEW daily_sample AS
            SELECT * FROM read_parquet('{quote_sql_path(sample_parquet)}')
            """
        )
    finally:
        connection.close()

    read_only = duckdb_module.connect(str(database), read_only=True)
    mutation_blocked = False
    mutation_error = None
    try:
        row_count = int(
            read_only.execute("SELECT count(*) FROM daily_sample").fetchone()[0]
        )
        try:
            read_only.execute("CREATE TABLE forbidden_write(value INTEGER)")
        except Exception as exc:
            mutation_blocked = True
            mutation_error = f"{type(exc).__name__}: {exc}"
    finally:
        read_only.close()
    return {
        "status": "PASS" if row_count > 0 and mutation_blocked else "FAIL",
        "row_count": row_count,
        "database_bytes": database.stat().st_size,
        "database_sha256": sha256_file(database),
        "read_only_mutation_blocked": mutation_blocked,
        "mutation_error": mutation_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument(
        "--run-id",
        default=f"pilot-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}",
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="Run bounded no-key inspection probes; never performs a backfill.",
    )
    args = parser.parse_args()

    workspace = args.workspace_root.resolve()
    allowed_root = (workspace / "tmp" / "data-capacity-pipeline-pilot").resolve()
    output = (allowed_root / args.run_id).resolve()
    if not resolved_under(output, allowed_root):
        raise SystemExit("Refusing output outside the isolated pilot root")
    if resolved_under(output, workspace / "data"):
        raise SystemExit("Refusing to write under the formal data root")
    if output.exists():
        raise SystemExit(f"Run directory already exists: {output}")
    output.mkdir(parents=True)

    try:
        import akshare as ak
        import duckdb
        import pandas as pd
        import requests
        from akshare.utils import demjson
    except ImportError as exc:
        raise SystemExit(
            "Pilot dependencies are missing. Run with the documented temporary "
            "project interpreter; do not install into the workspace implicitly. "
            f"Original error: {exc}"
        ) from exc

    catalog = workspace / "data" / "catalog" / "datasets.json"
    catalog_hash_before = sha256_file(catalog)
    formal_shared_roots = {
        "data/catalog": workspace / "data" / "catalog",
        "data/market": workspace / "data" / "market",
        "data/published": workspace / "data" / "published",
    }
    formal_shared_root_fingerprints_before = {
        name: tree_fingerprint(path)
        for name, path in formal_shared_roots.items()
    }
    formal_dirs = [
        workspace / "data" / "market" / name
        for name in ("raw", "curated", "quarantine")
    ]
    formal_dirs_before = {
        path.relative_to(workspace).as_posix(): path.exists()
        for path in formal_dirs
    }

    started_at = dt.datetime.now(dt.timezone.utc)
    results: dict[str, Any] = {
        "schema_version": 1,
        "run_id": args.run_id,
        "started_at_utc": started_at.isoformat(),
        "workspace_root": workspace.as_posix(),
        "output_root": output.as_posix(),
        "scope": "CAPACITY_AND_PIPELINE_PILOT_ONLY",
        "network_enabled": bool(args.network),
        "formal_data_created": False,
        "formal_catalog_modified": False,
        "bulk_backfill_performed": False,
        "source_admission_granted": False,
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "pandas": pd.__version__,
            "duckdb": duckdb.__version__,
            "akshare": ak.__version__,
            "requests": requests.__version__,
        },
        "integrity": {
            "catalog_sha256_before": catalog_hash_before,
            "formal_directories_before": formal_dirs_before,
            "formal_shared_root_fingerprints_before": (
                formal_shared_root_fingerprints_before
            ),
        },
        "observed_local_parquet": inspect_existing_parquet(workspace, duckdb),
        "network_probes": {},
        "pipeline_tests": {},
    }

    if args.network:
        record_probe(
            results["network_probes"],
            "reference_and_universe",
            lambda: probe_akshare_reference(output / "network/reference", ak),
        )
        record_probe(
            results["network_probes"],
            "secondary_daily_sample",
            lambda: probe_tencent_daily(
                output / "network/tencent_daily",
                requests,
                pd,
                demjson,
                duckdb,
            ),
        )
        record_probe(
            results["network_probes"],
            "cninfo_metadata_and_pdf_heads",
            lambda: probe_cninfo_metadata_and_heads(
                output / "network/cninfo", requests
            ),
        )
    else:
        results["network_probes"]["status"] = "SKIPPED"

    compaction_output = output / "fixtures" / "compaction"
    results["pipeline_tests"]["monthly_compaction"] = run_compaction_fixture(
        compaction_output, pd, duckdb
    )
    results["pipeline_tests"]["pit_revision_selection"] = run_pit_fixture(duckdb)
    results["pipeline_tests"]["quality_rule_detection"] = run_quality_fixture(pd)

    sample_parquet = (
        output / "network" / "tencent_daily" / "normalized_sample.parquet"
    )
    if not sample_parquet.exists():
        sample_parquet = compaction_output / "2026-07.compacted.parquet"
        projection_input = "fixture_compaction_output"
    else:
        projection_input = "real_secondary_inspection_sample"
    projection = run_read_only_projection(
        output / "fixtures" / "duckdb_projection", sample_parquet, duckdb
    )
    projection["input_role"] = projection_input
    results["pipeline_tests"]["duckdb_read_only_projection"] = projection

    catalog_hash_after = sha256_file(catalog)
    formal_shared_root_fingerprints_after = {
        name: tree_fingerprint(path)
        for name, path in formal_shared_roots.items()
    }
    formal_dirs_after = {
        path.relative_to(workspace).as_posix(): path.exists()
        for path in formal_dirs
    }
    results["integrity"].update(
        {
            "catalog_sha256_after": catalog_hash_after,
            "catalog_unchanged": catalog_hash_before == catalog_hash_after,
            "formal_directories_after": formal_dirs_after,
            "formal_directories_unchanged": (
                formal_dirs_before == formal_dirs_after
            ),
            "formal_shared_root_fingerprints_after": (
                formal_shared_root_fingerprints_after
            ),
            "formal_shared_roots_unchanged": (
                formal_shared_root_fingerprints_before
                == formal_shared_root_fingerprints_after
            ),
        }
    )
    results["formal_catalog_modified"] = catalog_hash_before != catalog_hash_after
    results["formal_data_created"] = formal_dirs_before != formal_dirs_after
    results["formal_shared_roots_modified"] = (
        formal_shared_root_fingerprints_before
        != formal_shared_root_fingerprints_after
    )

    pipeline_pass = all(
        item["status"] == "PASS"
        for item in results["pipeline_tests"].values()
    )
    integrity_pass = (
        results["integrity"]["catalog_unchanged"]
        and results["integrity"]["formal_directories_unchanged"]
        and results["integrity"]["formal_shared_roots_unchanged"]
    )
    results["mechanics_status"] = (
        "ISOLATED_FIXTURES_PASS"
        if pipeline_pass
        else "ISOLATED_FIXTURES_HAVE_FAILURES"
    )
    results["integrity_status"] = (
        "FORMAL_STATE_UNCHANGED"
        if integrity_pass
        else "FORMAL_STATE_CHANGED"
    )
    results["status"] = (
        "LOCAL_MECHANICS_PASS__SOURCE_ADMISSION_AND_END_TO_END_NOT_TESTED"
        if pipeline_pass and integrity_pass
        else "PILOT_HAS_FAILURES"
    )
    results["interpretation"] = {
        "allowed": [
            "Use real sample bytes per row as a preliminary encoding anchor.",
            "Use fixture tests as evidence that the tested mechanics work.",
            "Use source errors to plan adapters and retry behavior.",
        ],
        "forbidden": [
            "Do not claim that any source or dataset is formally admitted.",
            "Do not treat fixture bytes as a historical capacity estimate.",
            "Do not claim statistically adequate P1-P3 completion.",
            "Do not register these outputs in the formal dataset catalog.",
        ],
    }
    results["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    results["elapsed_seconds"] = round(
        (
            dt.datetime.fromisoformat(results["finished_at_utc"]) - started_at
        ).total_seconds(),
        3,
    )
    write_json(output / "RESULTS.json", results)

    inventory = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            inventory.append(
                {
                    "path": path.relative_to(output).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    write_json(
        output / "INVENTORY.json",
        {
            "schema_version": 1,
            "run_id": args.run_id,
            "files_excluding_inventory_itself": inventory,
            "total_bytes_excluding_inventory_itself": sum(
                item["bytes"] for item in inventory
            ),
        },
    )
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if results["status"]
        == "LOCAL_MECHANICS_PASS__SOURCE_ADMISSION_AND_END_TO_END_NOT_TESTED"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
