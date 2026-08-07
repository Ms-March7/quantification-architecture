from __future__ import annotations

import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

import pandas as pd
import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .accounting import decompose_etf_aum, implied_margin_repayment, reconcile_margin
from .collector import RawCollector
from .utils import clean_text, next_weekday, number


BSE_MARGIN_PAGE = "https://www.bse.cn/disclosure/rzrq_trans_list.html"
BSE_MARGIN_SUMMARY = "https://www.bse.cn/rzrqjyyexxController/summaryInfoResult.do"
HKEX_DAILY_PATTERN = "https://www.hkex.com.hk/eng/csm/DailyStat/data_tab_daily_{date}e.js"
SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_REPORT_URL = "https://www.szse.cn/api/report/ShowReport"
EASTMONEY_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"


def parse_jsonp(text: str) -> Any:
    """Parse a JSON or JSONP response without executing JavaScript."""

    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        return json.loads(stripped)
    match = re.match(r"^[^(]+\((.*)\)\s*;?\s*$", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError("response is neither JSON nor JSONP")
    body = re.sub(r"'(\d{4}-\d{2}-\d{2})'", r'"\1"', match.group(1))
    return json.loads(body)


def _first_mapping(value: Any) -> dict[str, Any]:
    while isinstance(value, list) and value:
        value = value[0]
    return value if isinstance(value, dict) else {}


def collect_bse_margin(collector: RawCollector, dates: Iterable[str]) -> list[dict[str, Any]]:
    selected = tuple(dates)
    collector.session.headers.update({"X-Requested-With": "XMLHttpRequest"})
    collector.get(
        source_id="bse_margin_page_warmup",
        url=BSE_MARGIN_PAGE,
        relative_path="bse/margin_page_v2.html",
        referer="https://www.bse.cn/",
        direct_no_proxy_fallback=True,
    )
    time.sleep(0.8)
    rows: list[dict[str, Any]] = []
    for date in selected:
        callback = f"capitalFlow{date.replace('-', '')}"
        response = collector.get(
            source_id="bse_margin_summary",
            url=BSE_MARGIN_SUMMARY,
            params={"transDate": date, "page": 0, "callback": callback},
            relative_path=f"bse/margin_summary_{date}.jsonp",
            referer=BSE_MARGIN_PAGE,
            direct_no_proxy_fallback=True,
        )
        time.sleep(0.8)
        item = _first_mapping(parse_jsonp(response.text))
        rows.append(
            {
                "date": date,
                "exchange": "BSE",
                "financing_balance_cny": number(item.get("rzye")),
                "financing_buy_cny": number(item.get("rzmre")),
                "financing_repayment_cny": None,
                "securities_lending_balance_qty": number(item.get("rqyl")),
                "securities_lending_sell_qty": number(item.get("rqmcl")),
                "securities_lending_balance_cny": number(item.get("rqye")),
                "margin_total_balance_cny": number(item.get("rzrqye")),
                "repayment_evidence": "PROXY",
                "source_url": BSE_MARGIN_PAGE,
                "available_at": f"{next_weekday(date)}T09:00:00+08:00",
                "availability_basis": "contractual T+1 09:00 cutoff; repayment inferred from balance identity",
            }
        )
    rows.sort(key=lambda row: row["date"])
    for index, row in enumerate(rows):
        if index == 0:
            row.update(
                reconciliation_residual_cny=None,
                tolerance_cny=None,
                quality="MISSING",
                notes="previous pilot-day balance unavailable; official balance and buy are observed",
            )
            continue
        previous = rows[index - 1]["financing_balance_cny"]
        row["financing_repayment_cny"] = implied_margin_repayment(
            previous, row["financing_balance_cny"], row["financing_buy_cny"]
        )
        check = reconcile_margin(
            previous,
            row["financing_balance_cny"],
            row["financing_buy_cny"],
            row["financing_repayment_cny"],
            absolute_tolerance=100.0,
        )
        row.update(
            reconciliation_residual_cny=check.residual,
            tolerance_cny=check.tolerance,
            quality=check.quality,
            notes="financing repayment is identity-implied, not directly disclosed",
        )
    return rows


def _hkex_number(value: Any, scale: float = 1_000_000.0) -> float | None:
    parsed = number(value)
    return None if parsed is None else parsed * scale


def parse_hkex_daily(text: str, date: str) -> list[dict[str, Any]]:
    payload = json.loads(text.split("=", 1)[1].strip().rstrip(";"))
    rows: list[dict[str, Any]] = []
    for market in payload:
        label = clean_text(market.get("market"))
        if not label:
            continue
        channel, direction = label.split(maxsplit=1)
        is_north = direction == "Northbound"
        if not market.get("tradingDay"):
            rows.append(
                {
                    "date": date,
                    "direction": direction.upper(),
                    "channel": channel,
                    "buy_amount_hkd": None if is_north else 0.0,
                    "sell_amount_hkd": None if is_north else 0.0,
                    "total_amount_hkd": None if is_north else 0.0,
                    "northbound_total_amount_cny": 0.0 if is_north else None,
                    "etf_turnover_local_currency": 0.0,
                    "currency": "CNY" if is_north else "HKD",
                    "market_status": "CLOSED",
                    "evidence_state": "OBSERVED",
                    "quality": "OK",
                    "source_url": HKEX_DAILY_PATTERN.format(date=date.replace("-", "")),
                    "available_at": f"{next_weekday(date)}T09:00:00+08:00",
                }
            )
            continue
        table = ((market.get("content") or [{}])[0].get("table") or {})
        schema = ((table.get("schema") or [[]])[0])
        values = [((item.get("td") or [[None]])[0] or [None])[0] for item in table.get("tr") or []]
        fields = dict(zip(schema, values))
        rows.append(
            {
                "date": date,
                "direction": direction.upper(),
                "channel": channel,
                "buy_amount_hkd": None if is_north else _hkex_number(fields.get("Buy Turnover")),
                "sell_amount_hkd": None if is_north else _hkex_number(fields.get("Sell Turnover")),
                "total_amount_hkd": None if is_north else _hkex_number(fields.get("Total Turnover")),
                "northbound_total_amount_cny": _hkex_number(fields.get("Total Turnover")) if is_north else None,
                "etf_turnover_local_currency": _hkex_number(fields.get("ETF Turnover")),
                "currency": "CNY" if is_north else "HKD",
                "market_status": "OPEN",
                "evidence_state": "OBSERVED",
                "quality": "OK",
                "source_url": HKEX_DAILY_PATTERN.format(date=date.replace("-", "")),
                "available_at": f"{next_weekday(date)}T09:00:00+08:00",
            }
        )
    return rows


def collect_hkex_connect_turnover(collector: RawCollector, dates: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for date in dates:
        compact = date.replace("-", "")
        url = HKEX_DAILY_PATTERN.format(date=compact)
        parsed: list[dict[str, Any]] | None = None
        for attempt in range(1, 4):
            response = collector.get(
                source_id="hkex_stock_connect_daily",
                url=url,
                relative_path=f"hkex_v2/daily_{date}_attempt{attempt}.js",
                referer="https://www.hkex.com.hk/Mutual-Market/Stock-Connect/Statistics/Historical-Daily?sc_lang=en",
            )
            try:
                candidate = parse_hkex_daily(response.text, date)
            except (IndexError, json.JSONDecodeError, UnicodeError, ValueError):
                candidate = []
            if len(candidate) == 4:
                parsed = candidate
                break
            time.sleep(0.5)
        if parsed is None:
            parsed = []
            for channel in ("SSE", "SZSE"):
                for direction in ("NORTHBOUND", "SOUTHBOUND"):
                    is_north = direction == "NORTHBOUND"
                    parsed.append(
                        {
                            "date": date,
                            "direction": direction,
                            "channel": channel,
                            "buy_amount_hkd": None,
                            "sell_amount_hkd": None,
                            "total_amount_hkd": None,
                            "northbound_total_amount_cny": None,
                            "etf_turnover_local_currency": None,
                            "currency": "CNY" if is_north else "HKD",
                            "market_status": "UNKNOWN",
                            "evidence_state": "NOT_IDENTIFIED",
                            "quality": "MISSING",
                            "source_url": url,
                            "available_at": None,
                            "notes": "HKEX returned a structurally invalid HTTP-200 body in three attempts",
                        }
                    )
        rows.extend(parsed)
    return rows


def collect_exchange_etf_shares(
    collector: RawCollector, dates: Iterable[str]
) -> list[dict[str, Any]]:
    selected = tuple(dates)
    rows: list[dict[str, Any]] = []
    for date in selected:
        response = collector.get(
            source_id="sse_etf_daily_shares",
            url=SSE_QUERY_URL,
            params={
                "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
                "isPagination": "false",
                "STAT_DATE": date,
            },
            relative_path=f"sse/etf_shares_{date}.json",
            referer="https://www.sse.com.cn/market/funddata/overview/day/",
        )
        for item in response.json().get("result") or []:
            shares = number(item.get("TOT_VOL"), 10_000.0)
            rows.append(
                {
                    "date": date,
                    "exchange": "SSE",
                    "symbol": f"{str(item.get('SEC_CODE', '')).zfill(6)}.SH",
                    "name": clean_text(item.get("SEC_NAME")),
                    "shares": shares,
                    "shares_evidence": "OBSERVED",
                    "shares_source_url": "https://www.sse.com.cn/market/funddata/overview/day/",
                }
            )
    response = collector.get(
        source_id="szse_etf_daily_shares",
        url=SZSE_REPORT_URL,
        params={
            "SHOWTYPE": "xlsx",
            "CATALOGID": "scsj_fund_jjgm",
            "TABKEY": "tab1",
            "txtStart": selected[0],
            "txtEnd": selected[-1],
            "jjlb": "ETF",
        },
        relative_path="szse/etf_shares_20d.xlsx",
        referer="https://www.szse.cn/market/fund/volume/etf/index.html",
        direct_no_proxy_fallback=True,
    )
    frame = pd.read_excel(io.BytesIO(response.content), dtype=str)
    for values in frame.itertuples(index=False, name=None):
        if len(values) < 4:
            continue
        date = str(values[0])[:10]
        code = str(values[1]).split(".")[0].zfill(6)
        shares = number(values[3])
        if date not in selected or shares is None:
            continue
        rows.append(
            {
                "date": date,
                "exchange": "SZSE",
                "symbol": f"{code}.SZ",
                "name": clean_text(values[2]),
                "shares": shares,
                "shares_evidence": "OBSERVED",
                "shares_source_url": "https://www.szse.cn/market/fund/volume/etf/index.html",
            }
        )
    return sorted(rows, key=lambda row: (row["date"], row["symbol"]))


def _fetch_nav(code: str, start: str, end: str, timeout: int) -> dict[str, Any]:
    retrying = Retrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    headers = {
        "User-Agent": "Mozilla/5.0 capital-flow-attribution research",
        "Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html",
    }
    response = retrying(
        requests.get,
        EASTMONEY_NAV_URL,
        params={
            "fundCode": code,
            "pageIndex": 1,
            "pageSize": 100,
            "startDate": start,
            "endDate": end,
        },
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    return {"code": code, "url": response.url, "payload": response.json()}


def nav_bundle_matches(bundle: dict[str, Any], codes: Iterable[str], start: str, end: str) -> bool:
    requested = {str(code) for code in codes}
    returned = {str(item.get("code")) for item in bundle.get("responses") or []}
    return bundle.get("start") == start and bundle.get("end") == end and returned == requested


def collect_eastmoney_etf_nav(
    collector: RawCollector,
    symbols: Iterable[str],
    start: str,
    end: str,
    *,
    max_workers: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    codes = sorted({symbol.split(".")[0] for symbol in symbols})
    relative_path = "eastmoney/etf_nav_bundle.json"
    cached = next((root / relative_path for root in collector.fallback_roots if (root / relative_path).is_file()), None)
    if cached is not None:
        try:
            cached_bundle = json.loads(cached.read_text(encoding="utf-8"))
            if not nav_bundle_matches(cached_bundle, codes, start, end):
                cached = None
        except (OSError, UnicodeError, json.JSONDecodeError):
            cached = None
    if cached is not None:
        response = collector.get(
            source_id="eastmoney_etf_nav_bundle",
            url=EASTMONEY_NAV_URL,
            relative_path=relative_path,
        )
        bundle = response.json()
        responses = list(bundle.get("responses") or [])
        failures = list(bundle.get("failures") or [])
    else:
        responses = []
        failures: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_nav, code, start, end, collector.timeout): code for code in codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    responses.append(future.result())
                except Exception as exc:  # the failure class is evidence; secrets are never included
                    failures.append({"code": code, "error_type": type(exc).__name__})
        bundle = {"start": start, "end": end, "responses": responses, "failures": failures}
        collector.record_bytes(
            source_id="eastmoney_etf_nav_bundle",
            url=EASTMONEY_NAV_URL,
            relative_path=relative_path,
            content=json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            content_type="application/json",
            retrieval_mode="secondary_adapter_bundle",
        )
    nav_rows: list[dict[str, Any]] = []
    for record in responses:
        code = record["code"]
        payload = record["payload"]
        for item in ((payload.get("Data") or {}).get("LSJZList") or []):
            date = str(item.get("FSRQ") or "")[:10]
            nav = number(item.get("DWJZ"))
            if nav is None:
                continue
            suffix = "SH" if code.startswith(("5", "58")) else "SZ"
            nav_rows.append(
                {
                    "date": date,
                    "symbol": f"{code}.{suffix}",
                    "nav": nav,
                    "nav_evidence": "PROXY",
                    "nav_quality": "UNLICENSED",
                    "nav_source_url": EASTMONEY_NAV_URL,
                }
            )
    stats = {
        "requested_symbols": len(codes),
        "successful_symbols": len(responses),
        "failed_symbols": len(failures),
        "nav_rows": len(nav_rows),
        "license_state": "UNVERIFIED",
    }
    return nav_rows, stats


def build_etf_aum_ledger(
    share_rows: Iterable[dict[str, Any]], nav_rows: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    shares = pd.DataFrame(share_rows)
    nav = pd.DataFrame(nav_rows)
    if shares.empty:
        return []
    merged = shares.merge(nav, on=["date", "symbol"], how="left")
    merged = merged.sort_values(["symbol", "date"])
    merged["previous_shares"] = merged.groupby("symbol")["shares"].shift(1)
    merged["previous_nav"] = merged.groupby("symbol")["nav"].shift(1)
    output: list[dict[str, Any]] = []
    for item in merged.to_dict("records"):
        result = decompose_etf_aum(
            item.get("previous_shares"), item.get("shares"), item.get("previous_nav"), item.get("nav")
        )
        has_nav = pd.notna(item.get("nav"))
        quality = result["quality"] if has_nav else "MISSING"
        output.append(
            {
                **item,
                "aum_cny": item["shares"] * item["nav"] if has_nav else None,
                "flow_component_cny": result["flow_component"],
                "market_component_cny": result["market_component"],
                "delta_aum_cny": result["delta_aum"],
                "reconciliation_residual_cny": result["residual"],
                "evidence_state": "PARTIALLY_IDENTIFIED" if has_nav else "NOT_IDENTIFIED",
                "quality": quality,
                "promotion_ceiling": "RESEARCH_ONLY",
                "notes": "official exchange shares; secondary NAV with unverified redistribution license",
            }
        )
    return output
