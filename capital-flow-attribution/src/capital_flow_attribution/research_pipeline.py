from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

from .backtest import (
    block_bootstrap_excess,
    cost_stress,
    diagnostic_change_points,
    evaluate_backtest,
    prepare_backtest_frame,
    walk_forward_exposure,
)
from .collector import RawCollector
from .exchange_adapters import (
    collect_connect_eligible,
    collect_connect_turnover,
    collect_fx_reference,
    collect_sse_margin,
    collect_szse_margin,
    finalize_margin,
)
from .g1_adapters import (
    build_etf_aum_ledger,
    collect_bse_margin,
    collect_eastmoney_etf_nav,
    collect_exchange_etf_shares,
    collect_hkex_connect_turnover,
)
from .historical import collect_historical_margin_proxy
from .qlib import QlibContextReader
from .reporting import size_bytes, write_json
from .storage import create_read_only_catalog, csv_to_parquet
from .utils import now_iso


def _write_frame(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _quality_count(rows: list[dict[str, Any]], quality: str) -> int:
    return sum(str(row.get("quality")) == quality for row in rows)


def _coverage_rows(
    dates: tuple[str, ...],
    margin: list[dict[str, Any]],
    etf_ledger: list[dict[str, Any]],
    turnover: list[dict[str, Any]],
    nav_stats: dict[str, Any],
) -> list[dict[str, Any]]:
    date_count = len(dates)
    expected_margin = date_count * 3
    expected_turnover = date_count * 4
    valid_turnover = sum(row.get("quality") == "OK" for row in turnover)
    etf_frame = pd.DataFrame(etf_ledger)
    duplicate_etf_keys = int(etf_frame.duplicated(["date", "symbol"]).sum()) if not etf_frame.empty else 0
    etf_rows = len(etf_frame) - duplicate_etf_keys
    date_position = {date: index for index, date in enumerate(dates)}
    expected_etf_rows = 0
    if not etf_frame.empty:
        for _, group in etf_frame.drop_duplicates(["date", "symbol"]).groupby("symbol"):
            observed_dates = sorted(date for date in group["date"] if date in date_position)
            if observed_dates:
                expected_etf_rows += date_position[observed_dates[-1]] - date_position[observed_dates[0]] + 1
    etf_nav = sum(pd.notna(row.get("nav")) for row in etf_ledger)
    etf_decomp = sum(row.get("quality") == "OK" for row in etf_ledger)
    has_tushare = bool(os.environ.get("TUSHARE_TOKEN"))
    return [
        {
            "dataset": "margin_sse_szse_bse_20d",
            "observed_rows": len(margin),
            "expected_rows": expected_margin,
            "coverage_ratio": len(margin) / expected_margin,
            "quality": "CONFLICT" if _quality_count(margin, "CONFLICT") else "OK",
            "canonical": True,
            "note": "BSE repayment is identity-implied; SSE conflict remains visible",
        },
        {
            "dataset": "etf_exchange_shares_20d",
            "observed_rows": etf_rows,
            "expected_rows": expected_etf_rows,
            "coverage_ratio": etf_rows / expected_etf_rows if expected_etf_rows else 0.0,
            "quality": "OK" if etf_rows == expected_etf_rows and duplicate_etf_keys == 0 else "MISSING",
            "canonical": True,
            "note": (
                "official SSE/SZSE daily fund-unit disclosures; expected keys are each symbol's "
                f"first-to-last observed trading-date interval; duplicate_keys={duplicate_etf_keys}"
            ),
        },
        {
            "dataset": "etf_nav_20d_secondary",
            "observed_rows": etf_nav,
            "expected_rows": expected_etf_rows,
            "coverage_ratio": etf_nav / expected_etf_rows if expected_etf_rows else 0.0,
            "quality": "UNLICENSED",
            "canonical": False,
            "note": f"Eastmoney NAV, {nav_stats['successful_symbols']}/{nav_stats['requested_symbols']} symbols responded",
        },
        {
            "dataset": "etf_aum_decomposition_20d",
            "observed_rows": etf_decomp,
            "expected_rows": expected_etf_rows,
            "coverage_ratio": etf_decomp / expected_etf_rows if expected_etf_rows else 0.0,
            "quality": "UNLICENSED",
            "canonical": False,
            "note": "numeric decomposition is research-only because NAV redistribution license is unverified",
        },
        {
            "dataset": "stock_connect_turnover_20d_hkex",
            "observed_rows": valid_turnover,
            "expected_rows": expected_turnover,
            "coverage_ratio": valid_turnover / expected_turnover,
            "quality": "OK" if valid_turnover == expected_turnover else "MISSING",
            "canonical": True,
            "note": "HKEX daily statistics cover both exchanges and both directions",
        },
        {
            "dataset": "stock_connect_eligible_pit",
            "observed_rows": 0,
            "expected_rows": date_count,
            "coverage_ratio": 0.0,
            "quality": "MISSING",
            "canonical": False,
            "note": "Tushare stock_hsgt adapter available but token absent" if not has_tushare else "token present; PIT collection not executed in this run",
        },
        {
            "dataset": "sw2021_membership_pit",
            "observed_rows": 0,
            "expected_rows": date_count,
            "coverage_ratio": 0.0,
            "quality": "MISSING",
            "canonical": False,
            "note": "Tushare index_member_all adapter available but token absent" if not has_tushare else "token present; PIT collection not executed in this run",
        },
    ]


def _write_catalog(data_root: Path, frames: dict[str, pd.DataFrame]) -> tuple[dict[str, Any], dict[str, int]]:
    normalized = data_root / "normalized"
    parquet_root = data_root / "parquet"
    parquet_paths: dict[str, Path] = {}
    manifests: dict[str, Any] = {}
    for name, frame in frames.items():
        csv_path = normalized / f"{name}.csv"
        parquet_path = parquet_root / f"{name}.parquet"
        _write_frame(csv_path, frame)
        manifests[name] = csv_to_parquet(csv_path, parquet_path)
        parquet_paths[name] = parquet_path
    catalog_path = data_root / "catalog" / "capital_flow_research.duckdb"
    counts = create_read_only_catalog(catalog_path, parquet_paths)
    return manifests, counts


def run_research(config: dict[str, Any]) -> dict[str, Any]:
    run_id = str(config["run_id"])
    data_root = Path(config["data_root"]) / "research" / run_id
    report_root = Path(config["report_root"]) / "research" / run_id
    if data_root.exists() or report_root.exists():
        raise FileExistsError(f"immutable research run already exists: {run_id}")
    free = shutil.disk_usage(Path(config["data_root"]).anchor).free
    if free < int(config["minimum_free_bytes"]):
        raise OSError("free-space floor would be violated before research run")
    for path in (data_root / "raw", data_root / "normalized", data_root / "parquet", data_root / "catalog", data_root / "manifests", report_root):
        path.mkdir(parents=True, exist_ok=False)

    reader = QlibContextReader(config["qlib_store"])
    dates_with_prior = reader.pilot_dates(config["pilot_end_date"], int(config["pilot_trading_days"]) + 1)
    dates = dates_with_prior[1:]
    collector = RawCollector(
        data_root / "raw",
        int(config["request_timeout_seconds"]),
        str(config["user_agent"]),
        config.get("raw_fallback_roots") or (),
        offline_raw_only=False,
        retry_attempts=int(config.get("retry_attempts", 5)),
        retry_wait_seconds=0.5,
    )

    sse = collect_sse_margin(collector, dates_with_prior)
    szse = collect_szse_margin(collector, dates_with_prior)
    bse = collect_bse_margin(collector, dates_with_prior)
    margin = [row for row in finalize_margin(sse + szse + bse, dates_with_prior) if row["date"] in dates]
    shares = collect_exchange_etf_shares(collector, dates_with_prior)
    nav, nav_stats = collect_eastmoney_etf_nav(
        collector,
        (row["symbol"] for row in shares),
        dates_with_prior[0],
        dates_with_prior[-1],
        max_workers=int(config.get("nav_workers", 8)),
    )
    etf_ledger = [row for row in build_etf_aum_ledger(shares, nav) if row["date"] in dates]
    if pd.DataFrame(etf_ledger).duplicated(["date", "symbol"]).any():
        raise ValueError("ETF ledger contains duplicate (date, symbol) keys")
    turnover = collect_hkex_connect_turnover(collector, dates)
    missing_dates = sorted({row["date"] for row in turnover if row.get("quality") == "MISSING"})
    if missing_dates:
        supplemental = collect_connect_turnover(collector, tuple(missing_dates))
        replacement = {
            (row["date"], row["direction"], row["channel"]): row
            for row in supplemental
            if row.get("quality") == "OK"
        }
        turnover = [replacement.get((row["date"], row["direction"], row["channel"]), row) for row in turnover]
    for row in turnover:
        if row.get("source_url") == "https://query.sse.com.cn/ggt/getQuatationInfo.do":
            total, buy, sell = (row.get("total_amount_hkd"), row.get("buy_amount_hkd"), row.get("sell_amount_hkd"))
            if total is not None and (total < 1_000_000 or abs(total - buy - sell) > 1.0):
                raise ValueError("SSE Stock Connect supplement failed absolute-HKD unit reconciliation")
    eligible_current = collect_connect_eligible(collector)
    fx = collect_fx_reference(collector, dates)
    coverage = _coverage_rows(dates, margin, etf_ledger, turnover, nav_stats)

    historical_margin, historical_meta = collect_historical_margin_proxy(collector)
    history_start = max(str(historical_margin["date"].min()), str(config["backtest_start_date"]))
    history_end = min(str(historical_margin["date"].max()), str(config["pilot_end_date"]))
    market_history = reader.market_return_series(history_start, history_end)
    backtest_frame = prepare_backtest_frame(historical_margin, market_history)
    predictions = walk_forward_exposure(
        backtest_frame,
        n_splits=int(config.get("backtest_folds", 6)),
        gap=int(config.get("backtest_gap_days", 5)),
        test_size=int(config.get("backtest_test_days", 252)),
    )
    backtest, metrics = evaluate_backtest(predictions, float(config.get("one_way_cost_bps", 10.0)))
    bootstrap = block_bootstrap_excess(backtest["net_return"] - backtest["benchmark_return"])
    stress = cost_stress(predictions)
    change_points = diagnostic_change_points(backtest_frame)

    frames = {
        "margin_ledger": pd.DataFrame(margin),
        "etf_aum_ledger": pd.DataFrame(etf_ledger),
        "stock_connect_turnover": pd.DataFrame(turnover),
        "stock_connect_eligible_current": pd.DataFrame(eligible_current),
        "fx_reference": pd.DataFrame(fx),
        "source_coverage": pd.DataFrame(coverage),
        "historical_margin_proxy": historical_margin,
        "market_history_proxy": market_history,
        "backtest_daily": backtest,
        "backtest_cost_stress": pd.DataFrame(stress),
    }
    parquet_manifests, catalog_counts = _write_catalog(data_root, frames)
    write_json(data_root / "manifests" / "raw_files.json", collector.manifest)
    write_json(data_root / "manifests" / "run_config.json", config)
    write_json(data_root / "manifests" / "historical_source.json", historical_meta)

    conflicts = [row for row in margin if row.get("quality") == "CONFLICT"]
    gate_reasons = [
        "ETF NAV redistribution license is unverified; decomposition remains RESEARCH_ONLY",
        "Stock Connect historical point-in-time eligibility is missing without an authorized Tushare/HKEX history source",
        "SW2021 historical point-in-time membership is missing without an authorized source",
    ]
    if conflicts:
        gate_reasons.append(f"margin accounting conflicts remain: {len(conflicts)} row(s)")
    missing_turnover = [row for row in turnover if row.get("quality") != "OK"]
    if missing_turnover:
        gate_reasons.append(f"Stock Connect daily channel rows remain missing: {len(missing_turnover)} row(s)")
    manifest = {
        "schema_version": "capital-flow-research-v1",
        "run_id": run_id,
        "generated_at": now_iso(),
        "pilot_dates": [dates[0], dates[-1]],
        "stage_gate": "REVIEW",
        "stage_gate_reasons": gate_reasons,
        "g1_remediated": [
            "BSE official margin coverage",
            "SZSE official direct-no-env-proxy fallback",
            "HKEX daily turnover with official-exchange fallback and explicit residual missingness",
            "SSE/SZSE official daily ETF shares",
            "research-only ETF NAV and AUM decomposition",
        ],
        "nav_stats": nav_stats,
        "coverage": coverage,
        "backtest_metrics": metrics,
        "bootstrap_excess": bootstrap,
        "diagnostic_change_points": change_points,
        "historical_source": historical_meta,
        "backtest_limitations": [
            "historical margin source is an unlicensed secondary aggregation",
            "original historical publication timestamps are unavailable; a conservative two-trading-day lag is used",
            "days without complete two-exchange margin inputs are kept in performance and assigned zero exposure",
            "equal-weight A-share return is a research proxy, not an executable index portfolio",
        ],
        "parquet_tables": {name: item["row_count"] for name, item in parquet_manifests.items()},
        "duckdb_counts": catalog_counts,
        "raw_files": len(collector.manifest),
        "credentials_persisted": False,
        "qlib_read_only": True,
        "multi_factor_published": False,
        "investment_recommendation": False,
    }
    write_json(data_root / "manifests" / "dataset_manifest.json", manifest)
    report = (
        "# G1修复、S2骨架与S3研究回测\n\n"
        f"- run_id: `{run_id}`\n"
        f"- 阶段门: `REVIEW`（不是PASS）\n"
        f"- 20日窗口: {dates[0]} 至 {dates[-1]}\n"
        f"- 北交所融资融券: {sum(row['exchange']=='BSE' for row in margin)} 行官方余额/买入，偿还额为恒等式代理\n"
        f"- ETF: {len(etf_ledger)} 行；NAV覆盖 {sum(pd.notna(row.get('nav')) for row in etf_ledger)}/{len(etf_ledger)}\n"
        f"- 互联互通成交: {sum(row.get('quality') == 'OK' for row in turnover)}/{len(dates)*4} 行通过质量检查\n"
        f"- 回测: {metrics['observations']} 个滚动样本外观测，{metrics['folds']} 折，gap={config.get('backtest_gap_days',5)}\n"
        f"- 策略累计收益: {metrics['strategy']['cumulative_return']:.2%}；基准: {metrics['benchmark']['cumulative_return']:.2%}\n"
        f"- 策略CAGR: {metrics['strategy']['cagr']:.2%}；最大回撤: {metrics['strategy']['maximum_drawdown']:.2%}\n"
        f"- 年化波动: {metrics['strategy']['annual_volatility']:.2%}；零利率Sharpe: {metrics['strategy']['sharpe_zero_rate']:.3f}\n"
        f"- Bootstrap年化超额95%区间: [{bootstrap['annual_excess_p025']:.2%}, {bootstrap['annual_excess_p975']:.2%}]\n\n"
        "## 尚未解决\n\n"
        + "\n".join(f"- {reason}" for reason in gate_reasons)
        + "\n\n## 解释边界\n\n"
        "回测只检验公开融资余额代理对全市场等权收益代理的风险暴露规则，不识别真实机构/散户身份，"
        "不构成投资建议，也不会写入多因子工程。历史原始发布时间不可得，因此采用保守的两交易日滞后；"
        "两交易所数据不完整日保留在绩效序列并置零仓位。ruptures变点仅作事后诊断，不进入信号。\n"
    )
    (report_root / "REPORT.md").write_text(report, encoding="utf-8")
    shutil.copy2(data_root / "manifests" / "dataset_manifest.json", report_root / "dataset_manifest.json")
    bytes_used = size_bytes(data_root)
    if bytes_used > int(config["storage_hard_cap_bytes"]):
        raise OSError("research run hard cap exceeded")
    return {**manifest, "data_root": data_root.as_posix(), "report_root": report_root.as_posix(), "bytes": bytes_used}
