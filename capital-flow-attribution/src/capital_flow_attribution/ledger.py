from __future__ import annotations

from typing import Any


def build_daily_state(
    dates: tuple[str, ...],
    market: list[dict[str, Any]],
    margin: list[dict[str, Any]],
    turnover: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in market:
        rows.append(
            {
                "date": item["date"],
                "family": "market_context",
                "market": item["exchange"],
                "metric": "valid_stock_count",
                "value": item["valid_count"],
                "unit": "securities",
                "evidence_state": item["evidence_state"],
                "quality": item["quality"],
                "source": item["source"],
            }
        )
        rows.append(
            {
                "date": item["date"],
                "family": "market_context",
                "market": item["exchange"],
                "metric": "advance_decline_net",
                "value": item["advancers"] - item["decliners"],
                "unit": "securities",
                "evidence_state": item["evidence_state"],
                "quality": item["quality"],
                "source": item["source"],
            }
        )
    for item in margin:
        rows.append(
            {
                "date": item["date"],
                "family": "margin",
                "market": item["exchange"],
                "metric": "financing_balance",
                "value": item["financing_balance_cny"],
                "unit": "CNY",
                "evidence_state": item["repayment_evidence"],
                "quality": item["quality"],
                "source": item["source_url"],
            }
        )
    for item in turnover:
        value = item["northbound_total_amount_cny"] if item["direction"] == "NORTHBOUND" else item["total_amount_hkd"]
        unit = "CNY" if item["direction"] == "NORTHBOUND" else "HKD"
        rows.append(
            {
                "date": item["date"],
                "family": "stock_connect_turnover",
                "market": item["channel"],
                "metric": f"{str(item['direction']).lower()}_total_turnover",
                "value": value,
                "unit": unit,
                "evidence_state": item["evidence_state"],
                "quality": item["quality"],
                "source": item["source_url"],
            }
        )
    for date in dates:
        rows.append(
            {
                "date": date,
                "family": "etf_aum_decomposition",
                "market": "SSE+SZSE",
                "metric": "coverage",
                "value": None,
                "unit": "status",
                "evidence_state": "NOT_IDENTIFIED",
                "quality": "MISSING",
                "source": "official exchange current scale probes",
            }
        )
        rows.append(
            {
                "date": date,
                "family": "industry_classification",
                "market": "CN",
                "metric": "sw2021_mapping_coverage",
                "value": None,
                "unit": "status",
                "evidence_state": "NOT_IDENTIFIED",
                "quality": "MISSING",
                "source": "not licensed/verified in S1 probe",
            }
        )
    return sorted(rows, key=lambda row: (row["date"], row["family"], row["market"], row["metric"]))


def source_coverage(
    *,
    bse_quality: str,
    bse_note: str,
    margin: list[dict[str, Any]],
    etf_proxy: list[dict[str, Any]],
    eligible: list[dict[str, Any]],
    turnover: list[dict[str, Any]],
    fx: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sse_margin_quality = "CONFLICT" if any(
        row["exchange"] == "SSE" and row["quality"] == "CONFLICT" for row in margin
    ) else ("OK" if any(row["exchange"] == "SSE" for row in margin) else "MISSING")
    szse_margin_quality = "CONFLICT" if any(
        row["exchange"] == "SZSE" and row["quality"] == "CONFLICT" for row in margin
    ) else ("OK" if any(row["exchange"] == "SZSE" for row in margin) else "MISSING")
    return [
        {"family": "market_context", "source": "registered Qlib snapshot", "coverage": "20 days; SSE/SZSE/BSE heuristic A-share codes", "evidence_state": "PROXY", "quality": "OK", "license": "Apache-2.0 registered snapshot", "notes": "RESEARCH_ONLY; intervals are not official listing lifecycles"},
        {"family": "margin_sse", "source": "SSE official", "coverage": f"{sum(row['exchange']=='SSE' for row in margin)} rows", "evidence_state": "OBSERVED", "quality": sse_margin_quality, "license": "official public disclosure; reuse terms not separately verified", "notes": "repayment is directly observed; 2026-06-22 identity residual is retained as a possible adjustment/conflict"},
        {"family": "margin_szse", "source": "SZSE official", "coverage": f"{sum(row['exchange']=='SZSE' for row in margin)} rows", "evidence_state": "PARTIALLY_IDENTIFIED", "quality": szse_margin_quality, "license": "official public disclosure; reuse terms not separately verified", "notes": "repayment is identity-implied PROXY"},
        {"family": "margin_bse", "source": "BSE official", "coverage": "0 observed rows", "evidence_state": "NOT_IDENTIFIED", "quality": bse_quality, "license": "official public disclosure; reuse terms not separately verified", "notes": bse_note},
        {"family": "etf_shares_nav", "source": "SSE/SZSE official current product pages", "coverage": f"{len(etf_proxy)} current proxy rows; 0 complete 20-day shares+NAV rows", "evidence_state": "PARTIALLY_IDENTIFIED", "quality": "MISSING", "license": "official public disclosure; reuse terms not separately verified", "notes": "no decomposition is emitted without both shares and NAV"},
        {"family": "stock_connect_eligibility", "source": "HKEX/SSE/SZSE official", "coverage": f"{len(eligible)} current eligible rows; no historical daily membership", "evidence_state": "PARTIALLY_IDENTIFIED", "quality": "MISSING", "license": "official public disclosure; historical HKEX daily data may be paid", "notes": "current snapshot cannot be backcast into 20-day point-in-time membership"},
        {"family": "stock_connect_turnover", "source": "SSE/SZSE official", "coverage": f"{sum(row['quality']=='OK' for row in turnover)}/{len(turnover)} rows", "evidence_state": "OBSERVED", "quality": "OK" if turnover and all(row["quality"] == "OK" for row in turnover) else "MISSING", "license": "official public disclosure; reuse terms not separately verified", "notes": "turnover is not investor identity or net capital flow"},
        {"family": "fx_reference", "source": "SSE Stock Connect reference rate", "coverage": f"{len(fx)} rows", "evidence_state": "PROXY", "quality": "OK" if fx else "MISSING", "license": "official public disclosure; reuse terms not separately verified", "notes": "not general spot FX"},
        {"family": "industry_sw2021", "source": "none verified", "coverage": "0 rows", "evidence_state": "NOT_IDENTIFIED", "quality": "MISSING", "license": "not acquired", "notes": "classification choice frozen, but licensed PIT mapping unavailable in S1"},
    ]
