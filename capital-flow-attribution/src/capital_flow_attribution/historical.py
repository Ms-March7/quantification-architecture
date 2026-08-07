from __future__ import annotations

from typing import Any

import pandas as pd

from .collector import RawCollector


JIN10_MARGIN = {
    "SSE": "https://cdn.jin10.com/data_center/reports/fs_1.json",
    "SZSE": "https://cdn.jin10.com/data_center/reports/fs_2.json",
}


def collect_historical_margin_proxy(collector: RawCollector) -> tuple[pd.DataFrame, dict[str, Any]]:
    frames: list[pd.DataFrame] = []
    for exchange, url in JIN10_MARGIN.items():
        response = collector.get(
            source_id=f"jin10_margin_{exchange.lower()}",
            url=url,
            relative_path=f"jin10/margin_{exchange.lower()}.json",
            referer="https://datacenter.jin10.com/",
        )
        values = response.json().get("values") or {}
        rows = []
        for date, fields in values.items():
            if len(fields) < 6:
                continue
            rows.append(
                {
                    "date": str(date),
                    "exchange": exchange,
                    "financing_buy_cny": fields[0],
                    "financing_balance_cny": fields[1],
                    "securities_lending_sell_qty": fields[2],
                    "securities_lending_balance_qty": fields[3],
                    "securities_lending_balance_cny": fields[4],
                    "margin_total_balance_cny": fields[5],
                }
            )
        frames.append(pd.DataFrame(rows))
    long = pd.concat(frames, ignore_index=True)
    numeric = [column for column in long.columns if column not in {"date", "exchange"}]
    for column in numeric:
        long[column] = pd.to_numeric(long[column], errors="coerce")
    counts = long.groupby("date")["exchange"].nunique().rename("exchange_count")
    combined = long.groupby("date", as_index=False)[numeric].sum(min_count=1).sort_values("date")
    combined = combined.merge(counts, on="date", how="left")
    incomplete = combined["exchange_count"] < 2
    combined.loc[incomplete, numeric] = float("nan")
    combined["evidence_state"] = "PROXY"
    combined["quality"] = incomplete.map({True: "MISSING", False: "UNLICENSED"})
    combined["promotion_ceiling"] = "RESEARCH_ONLY"
    metadata = {
        "source": "Jin10 CDN aggregation of exchange margin summaries",
        "source_urls": JIN10_MARGIN,
        "license_state": "UNVERIFIED",
        "canonical": False,
        "rows_exchange": len(long),
        "rows_combined": len(combined),
        "complete_two_exchange_days": int((combined["exchange_count"] == 2).sum()),
        "incomplete_exchange_days": int(incomplete.sum()),
        "date_start": combined["date"].min(),
        "date_end": combined["date"].max(),
    }
    return combined, metadata
