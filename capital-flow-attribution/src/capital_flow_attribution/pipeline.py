from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

from .accounting import reconcile_margin
from .collector import RawCollector
from .exchange_adapters import (
    collect_bse_probe,
    collect_connect_eligible,
    collect_connect_turnover,
    collect_fx_reference,
    collect_sse_etf_proxy,
    collect_sse_margin,
    collect_szse_etf_proxy,
    collect_szse_margin,
    finalize_margin,
)
from .ledger import build_daily_state, source_coverage
from .qlib import QlibContextReader, write_csv
from .reporting import render_report, size_bytes, write_json
from .source_policy import adapter_status, load_source_policy
from .storage import create_read_only_catalog, csv_to_parquet
from .utils import now_iso
from .validation import validate_unified_daily_state


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _materialize_parquet(
    *,
    csv_paths: dict[str, Path],
    parquet_root: Path,
    catalog_path: Path,
) -> tuple[dict[str, dict[str, object]], dict[str, int]]:
    parquet_manifests: dict[str, dict[str, object]] = {}
    parquet_paths: dict[str, Path] = {}
    for table_name, csv_path in csv_paths.items():
        parquet_path = parquet_root / f"{table_name}.parquet"
        parquet_manifests[table_name] = csv_to_parquet(csv_path, parquet_path)
        parquet_paths[table_name] = parquet_path
    counts = create_read_only_catalog(catalog_path, parquet_paths)
    return parquet_manifests, counts


def run_online(config: dict[str, Any], *, run_id_override: str | None = None) -> dict[str, Any]:
    run_id = run_id_override or str(config["run_id"])
    data_root = Path(config["data_root"]) / "s1" / run_id
    report_root = Path(config["report_root"]) / "s1" / run_id
    if data_root.exists() or report_root.exists():
        raise FileExistsError(f"immutable run already exists: {run_id}")
    free = shutil.disk_usage(Path(config["data_root"]).anchor).free
    if free < int(config["minimum_free_bytes"]):
        raise OSError("free-space floor would be violated before run")

    raw_root = data_root / "raw"
    normalized = data_root / "normalized"
    snapshots = data_root / "snapshots"
    manifests = data_root / "manifests"
    parquet_root = data_root / "parquet"
    catalog_root = data_root / "catalog"
    for path in (raw_root, normalized, snapshots, manifests, parquet_root, catalog_root):
        path.mkdir(parents=True, exist_ok=False)

    reader = QlibContextReader(config["qlib_store"])
    dates = reader.pilot_dates(config["pilot_end_date"], int(config["pilot_trading_days"]))
    collector = RawCollector(
        raw_root,
        int(config["request_timeout_seconds"]),
        str(config["user_agent"]),
        config.get("raw_fallback_roots") or (),
        bool(config.get("offline_raw_only", False)),
    )
    market = reader.market_context(dates)
    universe = reader.universe_rows()
    sse_margin = collect_sse_margin(collector, dates)
    szse_margin = collect_szse_margin(collector, dates)
    bse_quality, bse_note = collect_bse_probe(collector)
    margin = finalize_margin(sse_margin + szse_margin, dates)
    etf_proxy = collect_sse_etf_proxy(collector) + collect_szse_etf_proxy(collector)
    eligible = collect_connect_eligible(collector)
    turnover = collect_connect_turnover(collector, dates)
    fx = collect_fx_reference(collector, dates)
    coverage = source_coverage(
        bse_quality=bse_quality,
        bse_note=bse_note,
        margin=margin,
        etf_proxy=etf_proxy,
        eligible=eligible,
        turnover=turnover,
        fx=fx,
    )
    daily_state = build_daily_state(dates, market, margin, turnover)
    validate_unified_daily_state(pd.DataFrame(daily_state))

    csv_paths = {
        "universe_research_proxy": normalized / "universe_research_proxy.csv",
        "market_context": normalized / "market_context.csv",
        "margin_ledger": normalized / "margin_ledger.csv",
        "etf_scale_proxy": normalized / "etf_scale_proxy.csv",
        "stock_connect_eligible_current": normalized / "stock_connect_eligible_current.csv",
        "stock_connect_turnover": normalized / "stock_connect_turnover.csv",
        "fx_reference": normalized / "fx_reference.csv",
        "source_coverage": normalized / "source_coverage.csv",
        "unified_daily_state": snapshots / "unified_daily_state.csv",
    }
    write_csv(csv_paths["universe_research_proxy"], universe)
    write_csv(csv_paths["market_context"], market)
    write_csv(csv_paths["margin_ledger"], margin)
    write_csv(csv_paths["etf_scale_proxy"], etf_proxy)
    write_csv(csv_paths["stock_connect_eligible_current"], eligible)
    write_csv(csv_paths["stock_connect_turnover"], turnover)
    write_csv(
        csv_paths["fx_reference"],
        fx,
        [
            "date",
            "currency",
            "buy_reference_rate",
            "sell_reference_rate",
            "evidence_state",
            "quality",
            "notes",
            "source_url",
        ],
    )
    write_csv(csv_paths["source_coverage"], coverage)
    write_csv(csv_paths["unified_daily_state"], daily_state)

    catalog_path = catalog_root / "capital_flow.duckdb"
    parquet_manifests, catalog_counts = _materialize_parquet(
        csv_paths=csv_paths,
        parquet_root=parquet_root,
        catalog_path=catalog_path,
    )
    write_json(manifests / "raw_files.json", collector.manifest)
    write_json(manifests / "run_config.json", {**config, "run_id": run_id})
    write_json(manifests / "parquet_files.json", parquet_manifests)

    source_policy_path = Path(config.get("source_policy") or PROJECT_ROOT / "configs" / "source_policy.json")
    source_policy = load_source_policy(source_policy_path)
    source_status = adapter_status(source_policy)
    write_json(
        manifests / "source_adapter_status.json",
        {
            "schema_version": "s1r-adapter-status-v1",
            "generated_at": now_iso(),
            "policy": source_policy_path.as_posix(),
            "adapters": source_status,
            "credentials_persisted": False,
        },
    )

    dataset_manifest = {
        "schema_version": "s1r-manifest-v1",
        "run_id": run_id,
        "generated_at": now_iso(),
        "as_of": config["as_of"],
        "date_start": dates[0],
        "date_end": dates[-1],
        "trading_days": len(dates),
        "raw_files": len(collector.manifest),
        "quality_counts": {
            state: sum(row["quality"] == state for row in coverage)
            for state in ("OK", "MISSING", "STALE", "CONFLICT", "UNLICENSED")
        },
        "stage_gate": "REVIEW",
        "stage_gate_reason": "BSE margin, complete ETF shares+NAV, historical Connect eligibility, and SW2021 PIT mapping are missing; SSE margin has one accounting conflict",
        "qlib_read_only": True,
        "qlib_promotion_ceiling": "RESEARCH_ONLY",
        "architecture": "S1R modular adapters + Pandera + Parquet/DuckDB",
        "pandera_daily_state": "PASS",
        "parquet_tables": {name: manifest["row_count"] for name, manifest in parquet_manifests.items()},
        "duckdb_catalog": catalog_path.as_posix(),
        "duckdb_counts": catalog_counts,
        "optional_adapter_status": source_status,
    }
    write_json(manifests / "dataset_manifest.json", dataset_manifest)
    data_bytes = size_bytes(data_root)
    if data_bytes > int(config["storage_hard_cap_bytes"]):
        raise OSError("S1 hard cap exceeded")

    report_paths = {
        "统一状态账本": snapshots / "unified_daily_state.csv",
        "融资融券账本": normalized / "margin_ledger.csv",
        "ETF规模代理": normalized / "etf_scale_proxy.csv",
        "互联互通当前资格": normalized / "stock_connect_eligible_current.csv",
        "互联互通成交": normalized / "stock_connect_turnover.csv",
        "来源覆盖": normalized / "source_coverage.csv",
        "Parquet目录": parquet_root,
        "DuckDB目录": catalog_path,
        "数据清单": manifests / "dataset_manifest.json",
    }
    render_report(
        report_root,
        run_id=run_id,
        as_of=str(config["as_of"]),
        dates=dates,
        coverage=coverage,
        paths=report_paths,
        manifest_count=len(collector.manifest),
        data_bytes=data_bytes,
    )
    report_data = report_root / "data"
    report_data.mkdir(parents=True, exist_ok=True)
    shutil.copy2(normalized / "source_coverage.csv", report_data / "source_coverage.csv")
    shutil.copy2(manifests / "dataset_manifest.json", report_data / "dataset_manifest.json")
    return {
        **dataset_manifest,
        "data_root": data_root.as_posix(),
        "report_root": report_root.as_posix(),
        "data_bytes": data_bytes,
    }


def validate_fixture(fixture_path: Path, output_dir: Path) -> dict[str, Any]:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    for item in payload.get("margin", []):
        result = reconcile_margin(
            item.get("previous_balance"),
            item.get("current_balance"),
            item.get("financing_buy"),
            item.get("repayment"),
        )
        checks.append({"name": f"margin:{item.get('id')}", "quality": result.quality, "residual": result.residual})
    from .accounting import decompose_etf_aum

    for item in payload.get("etf", []):
        result = decompose_etf_aum(
            item.get("previous_shares"),
            item.get("current_shares"),
            item.get("previous_nav"),
            item.get("current_nav"),
        )
        checks.append({"name": f"etf:{item.get('id')}", "quality": result["quality"], "residual": result["residual"]})
    actual = "DONE" if checks and all(check["quality"] == "OK" for check in checks) else "REVIEW"
    expected = str(payload.get("expected_workflow_status", ""))
    matched = actual == expected
    result = {
        "schema_version": "s1-fixture-validation-v1",
        "fixture": fixture_path.as_posix(),
        "expected_workflow_status": expected,
        "actual_workflow_status": actual,
        "expectation_matched": matched,
        "checks": checks,
        "test_status": "PASS" if matched else "FAIL",
    }
    data_dir = output_dir / "data"
    results_dir = output_dir / "results"
    data_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    write_json(data_dir / "validation.json", result)
    (results_dir / "summary.md").write_text(
        "# Capital-flow attribution fixture\n\n"
        f"- test_status: `{result['test_status']}`\n"
        f"- expected_workflow_status: `{expected}`\n"
        f"- actual_workflow_status: `{actual}`\n"
        "- no investment recommendation is produced\n",
        encoding="utf-8",
    )
    return result
