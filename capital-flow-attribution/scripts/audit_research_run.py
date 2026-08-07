#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit one immutable capital-flow research run.")
    parser.add_argument("run_root", type=Path)
    args = parser.parse_args()
    root = args.run_root
    manifest = json.loads((root / "manifests" / "raw_files.json").read_text(encoding="utf-8"))
    hash_bad = []
    for item in manifest:
        path = root / item["relative_path"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != item["sha256"]:
            hash_bad.append(path.as_posix())
    database = root / "catalog" / "capital_flow_research.duckdb"
    with duckdb.connect(str(database), read_only=True) as connection:
        result = {
            "raw_files": len(manifest),
            "raw_hash_mismatches": hash_bad,
            "tables": [row[0] for row in connection.execute(
                "SELECT table_name FROM information_schema.tables ORDER BY 1"
            ).fetchall()],
            "margin_conflicts": connection.execute(
                "SELECT date, exchange, reconciliation_residual_cny, tolerance_cny "
                "FROM margin_ledger WHERE quality = 'CONFLICT'"
            ).fetchall(),
            "connect_missing": connection.execute(
                "SELECT date, direction, channel, quality, notes "
                "FROM stock_connect_turnover WHERE quality = 'MISSING'"
            ).fetchall(),
            "connect_unit_suspects": connection.execute(
                "SELECT date, direction, channel, buy_amount_hkd, sell_amount_hkd, total_amount_hkd "
                "FROM stock_connect_turnover WHERE source_url = "
                "'https://query.sse.com.cn/ggt/getQuatationInfo.do' AND "
                "(total_amount_hkd < 1000000 OR abs(total_amount_hkd - buy_amount_hkd - sell_amount_hkd) > 1)"
            ).fetchall(),
            "etf_nav_missing": connection.execute(
                "SELECT count(*) FROM etf_aum_ledger WHERE nav IS NULL"
            ).fetchone()[0],
            "etf_duplicate_keys": connection.execute(
                "SELECT count(*) FROM (SELECT date, symbol, count(*) n FROM etf_aum_ledger "
                "GROUP BY 1, 2 HAVING n > 1)"
            ).fetchone()[0],
            "backtest_duplicate_execution_dates": connection.execute(
                "SELECT count(*) FROM (SELECT execution_date, count(*) n FROM backtest_daily "
                "GROUP BY 1 HAVING n > 1)"
            ).fetchone()[0],
            "backtest_chronology_violations": connection.execute(
                "SELECT count(*) FROM backtest_daily WHERE execution_date <= signal_date"
            ).fetchone()[0],
            "backtest_missing_signal_rows": connection.execute(
                "SELECT count(*) FROM backtest_daily WHERE signal_available = false"
            ).fetchone()[0],
            "backtest_missing_signal_nonzero_exposure": connection.execute(
                "SELECT count(*) FROM backtest_daily WHERE signal_available = false AND exposure <> 0"
            ).fetchone()[0],
            "cost_stress": connection.execute(
                "SELECT one_way_cost_bps, cumulative_return, cagr, maximum_drawdown "
                "FROM backtest_cost_stress ORDER BY 1"
            ).fetchall(),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if hash_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
