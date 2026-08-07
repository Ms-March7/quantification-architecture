#!/usr/bin/env python3
"""Recompute the offline evidence ledger for L1-D2A-01.

This tool performs no network access and never reads or writes formal data
payload roots. It audits the three isolated source-admission runs plus the
explicitly referenced earlier capacity-pilot evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


RUN_IDS = (
    "d2a01-20260727-v1",
    "d2a01-20260727-v2",
    "d2a01-20260727-v3",
)
BUSINESS_FILES = (
    "sse-security-master-page-1.json",
    "szse-a-share-current.xlsx",
    "cninfo-corporate-action-metadata.json",
)
PUBLIC_PAGE_MARKERS = {
    "sse_2026_calendar_notice.html": ["休市安排", "上海证券交易所"],
    "szse_2026_calendar_notice.html": [
        "2026年部分节假日休市安排",
        "深圳证券交易所",
    ],
    "sse_general_legal_statement.html": ["法律声明", "上海证券交易所"],
    "sse_market_data_services.html": ["数据服务", "上海证券交易所"],
    "sse_trading_information_rule.html": [
        "上海证券交易所交易规则",
        "交易信息",
    ],
    "szse_general_legal_statement.html": ["法律声明", "深圳证券交易所"],
    "szse_data_services.html": ["Data Services", "market data"],
    "cninfo_legal_statement.html": ["巨潮资讯网", "法律声明"],
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, value: Any, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    if partial.exists():
        raise ValueError(f"stale partial output exists: {partial}")
    if path.exists() and not replace:
        raise ValueError(f"output exists; pass --replace to refresh: {path}")
    partial.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    partial.replace(path)


def xlsx_shape(path: Path) -> tuple[int, int]:
    import pandas as pd

    frame = pd.read_excel(path)
    return int(frame.shape[0]), int(frame.shape[1])


def run_ledger(run_root: Path) -> dict[str, Any]:
    results_path = run_root / "RESULTS.json"
    inventory_path = run_root / "INVENTORY.json"
    results = load_json(results_path)
    files = sorted(item for item in run_root.rglob("*") if item.is_file())
    business: dict[str, Any] = {}
    for name in BUSINESS_FILES:
        path = run_root / "business" / name
        record: dict[str, Any] = {
            "path": path.as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        if name == "sse-security-master-page-1.json":
            record["rows"] = len(load_json(path).get("result") or [])
        elif name == "cninfo-corporate-action-metadata.json":
            record["rows"] = len(load_json(path).get("announcements") or [])
        else:
            rows, columns = xlsx_shape(path)
            record.update({"rows": rows, "columns": columns})
        business[name] = record
    captured_bytes = 0
    captured_count = 0
    for probe in results["probes"].values():
        capture = probe.get("capture")
        if capture:
            captured_count += 1
            captured_bytes += int(capture["bytes"])
    return {
        "run_id": run_root.name,
        "results_path": results_path.as_posix(),
        "results_sha256": sha256_file(results_path),
        "inventory_path": inventory_path.as_posix(),
        "inventory_sha256": sha256_file(inventory_path),
        "actual_file_count": len(files),
        "actual_total_bytes": sum(item.stat().st_size for item in files),
        "inventory_self_excluded": len(load_json(inventory_path)["files"])
        == len(files) - 1,
        "attempted_http_requests": int(results["probe_summary"]["total"]),
        "successful_captures": int(results["probe_summary"]["passed"]),
        "errors": int(results["probe_summary"]["errors"]),
        "captured_response_bytes": captured_bytes,
        "captured_response_count": captured_count,
        "business_captures": business,
    }


def audit_cninfo_480(root: Path) -> dict[str, Any]:
    paths = sorted((root / "network" / "cninfo").glob("*.json"))
    timezone = ZoneInfo("Asia/Shanghai")
    rows: list[dict[str, Any]] = []
    files = []
    for path in paths:
        payload_rows = load_json(path).get("announcements") or []
        rows.extend(payload_rows)
        files.append(
            {
                "path": path.as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "rows": len(payload_rows),
            }
        )
    midnight = 0
    non_midnight = 0
    associate_non_null = 0
    correction_rows = 0
    correction_with_associate = 0
    title_pattern = re.compile(r"更正|修订|更新")
    for row in rows:
        timestamp = dt.datetime.fromtimestamp(
            float(row["announcementTime"]) / 1000,
            timezone,
        )
        if timestamp.time() == dt.time():
            midnight += 1
        else:
            non_midnight += 1
        associated = row.get("associateAnnouncement") not in (None, "")
        if associated:
            associate_non_null += 1
        corrected = bool(title_pattern.search(row.get("announcementTitle") or ""))
        if corrected:
            correction_rows += 1
            if associated:
                correction_with_associate += 1
    return {
        "input_files": files,
        "rows": len(rows),
        "midnight_valued_asia_shanghai": midnight,
        "non_midnight_valued": non_midnight,
        "timestamp_precision_conclusion": "UNKNOWN__MIDNIGHT_CLUSTER_DOES_NOT_PROVE_PLACEHOLDERS",
        "associate_announcement_non_null": associate_non_null,
        "correction_revision_update_title_rows": correction_rows,
        "correction_rows_with_associate_announcement": correction_with_associate,
    }


def audit_tencent_prior(root: Path, runner_path: Path) -> dict[str, Any]:
    results_path = root / "RESULTS.json"
    results = load_json(results_path)
    sample = results["network_probes"]["secondary_daily_sample"]
    source = runner_path.read_text(encoding="utf-8")
    request_contract = re.search(
        r'f"\{symbol\},day,([^,]+),([^,]+),(\d+),"',
        source,
    )
    if not request_contract:
        raise ValueError("cannot locate prior Tencent request contract")
    responses = sample["responses"]
    return {
        "results_path": results_path.as_posix(),
        "results_sha256": sha256_file(results_path),
        "inventory_path": (root / "INVENTORY.json").as_posix(),
        "inventory_sha256": sha256_file(root / "INVENTORY.json"),
        "runner_path": runner_path.as_posix(),
        "runner_sha256": sha256_file(runner_path),
        "requested_start": request_contract.group(1),
        "requested_end": request_contract.group(2),
        "requested_count": int(request_contract.group(3)),
        "rows": int(sample["rows"]),
        "symbols": len(sample["symbols"]),
        "observed_raw_row_widths": sample["observed_raw_row_widths"],
        "minimum_observed_trade_date": min(
            item["min_trade_date"] for item in responses
        ),
        "maximum_observed_trade_date": max(
            item["max_trade_date"] for item in responses
        ),
        "start_bound_conclusion": (
            "UNVERIFIED__RETAINED_EVIDENCE_DOES_NOT_SHOW_ROWS_BEFORE_REQUESTED_START"
        ),
        "coverage_conclusion": (
            "COUNT_AND_WINDOW_PARAMETERS_AFFECT_COVERAGE__LOCAL_CLIPPING_AND_"
            "COVERAGE_CHECKS_REQUIRED"
        ),
    }


def audit_v3_public_pages(root: Path) -> dict[str, Any]:
    pages = root / "public-pages"
    records = {}
    for name, markers in PUBLIC_PAGE_MARKERS.items():
        path = pages / name
        text = path.read_text(encoding="utf-8")
        folded = text.casefold()
        missing = [
            marker for marker in markers if marker.casefold() not in folded
        ]
        records[name] = {
            "path": path.as_posix(),
            "sha256": sha256_file(path),
            "expected_markers": markers,
            "missing_markers": missing,
            "passed": not missing,
        }
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "reports/data-governance/"
            "d2a-01-source-admission-pilot-20260727/EVIDENCE_AUDIT.json"
        ),
    )
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    workspace = args.workspace_root.resolve()
    output = (
        args.output.resolve()
        if args.output.is_absolute()
        else (workspace / args.output).resolve()
    )
    report_root = (
        workspace
        / "reports"
        / "data-governance"
        / "d2a-01-source-admission-pilot-20260727"
    ).resolve()
    output.relative_to(report_root)

    pilot_root = workspace / "tmp" / "source-admission-pilot"
    ledgers = [run_ledger(pilot_root / run_id) for run_id in RUN_IDS]
    capacity_root = (
        workspace / "tmp" / "data-capacity-pipeline-pilot" / "pilot-20260726-v6"
    )
    prior_runner = workspace / "control" / "tools" / "run_data_capacity_pipeline_pilot.py"
    executed_source = report_root / "executed-source"
    audit = {
        "schema_version": 1,
        "work_package": "L1-D2A-01_SOURCE_ADMISSION_AND_PILOT_ADAPTERS",
        "network_access_performed": False,
        "run_ledger": ledgers,
        "work_package_totals": {
            "runs": len(ledgers),
            "attempted_http_requests": sum(
                item["attempted_http_requests"] for item in ledgers
            ),
            "successful_captures": sum(
                item["successful_captures"] for item in ledgers
            ),
            "errors": sum(item["errors"] for item in ledgers),
            "business_requests": len(ledgers) * len(BUSINESS_FILES),
            "cninfo_metadata_requests": len(ledgers),
            "sse_security_master_requests": len(ledgers),
            "szse_security_master_requests": len(ledgers),
            "public_page_requests": sum(
                item["attempted_http_requests"] - len(BUSINESS_FILES)
                for item in ledgers
            ),
            "actual_files": sum(item["actual_file_count"] for item in ledgers),
            "actual_total_bytes": sum(
                item["actual_total_bytes"] for item in ledgers
            ),
            "captured_response_bytes": sum(
                item["captured_response_bytes"] for item in ledgers
            ),
        },
        "v3_public_page_marker_audit": audit_v3_public_pages(
            pilot_root / RUN_IDS[-1]
        ),
        "prior_capacity_pilot_provenance": {
            "cninfo_480": audit_cninfo_480(capacity_root),
            "tencent_secondary_daily": audit_tencent_prior(
                capacity_root,
                prior_runner,
            ),
        },
        "executed_source_recovery": {
            "runner_path": (
                executed_source / "v3-run_source_admission_pilot.py"
            ).as_posix(),
            "runner_sha256": sha256_file(
                executed_source / "v3-run_source_admission_pilot.py"
            ),
            "matrix_path": (
                executed_source / "v3-source_matrix.json"
            ).as_posix(),
            "matrix_sha256": sha256_file(
                executed_source / "v3-source_matrix.json"
            ),
            "recovery_method": (
                "Replayed recorded Add File and pre-v3 patch events; accepted "
                "only after hashes matched v3 RESULTS.json."
            ),
        },
    }
    atomic_write_json(output, audit, args.replace)
    print(json.dumps(audit["work_package_totals"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
