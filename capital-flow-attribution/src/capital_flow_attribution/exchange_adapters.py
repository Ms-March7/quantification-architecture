from __future__ import annotations

import csv
import re
from typing import Any, Iterable

import requests

from .accounting import implied_margin_repayment, reconcile_margin
from .collector import RawCollector
from .utils import clean_text, iso_date, next_weekday, number


SSE_MARGIN_URL = "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do"
SZSE_REPORT_URL = "https://www.szse.cn/api/report/ShowReport/data"
SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_SOA_URL = "https://query.sse.com.cn/commonSoaQuery.do"
SSE_CONNECT_QUOTE_URL = "https://query.sse.com.cn/ggt/getQuatationInfo.do"
HKEX_NB_SSE = "https://www.hkex.com.hk/-/media/HKEX-Market/Mutual-Market/Stock-Connect/Eligible-Stocks/View-All-Eligible-Securities/SSE_Securities.csv"
HKEX_NB_SZSE = "https://www.hkex.com.hk/-/media/HKEX-Market/Mutual-Market/Stock-Connect/Eligible-Stocks/View-All-Eligible-Securities/SZSE_Securities.csv"
BSE_MARGIN_PAGE = "https://www.bse.cn/disclosure/rzrq_trans_list.html"


def sse_hundred_million_hkd(value: Any) -> float | None:
    return number(value, 100_000_000.0)


def json_payload(response: requests.Response) -> Any:
    return response.json()


def szse_report(collector: RawCollector, catalog: str, raw_name: str, *, tab: str = "tab1", **params: Any) -> Any:
    query = {"SHOWTYPE": "JSON", "CATALOGID": catalog, "TABKEY": tab, **params}
    response = collector.get(
        source_id=f"szse_{catalog}",
        url=SZSE_REPORT_URL,
        params=query,
        relative_path=f"szse/{raw_name}.json",
        referer="https://www.szse.cn/",
        direct_no_proxy_fallback=True,
    )
    return json_payload(response)


def collect_sse_margin(collector: RawCollector, dates: tuple[str, ...]) -> list[dict[str, Any]]:
    response = collector.get(
        source_id="sse_margin_summary",
        url=SSE_MARGIN_URL,
        params={
            "isPagination": "true",
            "beginDate": dates[0].replace("-", ""),
            "endDate": dates[-1].replace("-", ""),
            "pageHelp.pageSize": "100",
            "pageHelp.pageNo": "1",
        },
        relative_path="sse/margin_20d.json",
        referer="https://www.sse.com.cn/market/othersdata/margin/sum/",
    )
    payload = json_payload(response)
    rows: list[dict[str, Any]] = []
    for item in payload.get("result") or []:
        date = iso_date(item.get("opDate"))
        if date not in dates:
            continue
        rows.append(
            {
                "date": date,
                "exchange": "SSE",
                "financing_balance_cny": number(item.get("rzye")),
                "financing_buy_cny": number(item.get("rzmre")),
                "financing_repayment_cny": number(item.get("rzche")),
                "securities_lending_balance_qty": number(item.get("rqyl")),
                "securities_lending_sell_qty": number(item.get("rqmcl")),
                "securities_lending_balance_cny": number(item.get("rqylje")),
                "margin_total_balance_cny": number(item.get("rzrqjyzl")),
                "repayment_evidence": "OBSERVED",
                "source_url": SSE_MARGIN_URL,
                "available_at": f"{next_weekday(date)}T09:00:00+08:00",
                "availability_basis": "contractual T+1 09:00 cutoff; publication timestamp not exposed",
            }
        )
    return sorted(rows, key=lambda row: row["date"])


def collect_szse_margin(collector: RawCollector, dates: tuple[str, ...]) -> list[dict[str, Any]]:
    raw_rows: list[dict[str, Any]] = []
    for date in dates:
        payload = szse_report(collector, "1837_xxpl", f"margin_{date}", txtDate=date)
        item = ((payload or [{}])[0].get("data") or [{}])[0]
        raw_rows.append(
            {
                "date": date,
                "exchange": "SZSE",
                "financing_balance_cny": number(item.get("jrrzye"), 100_000_000.0),
                "financing_buy_cny": number(item.get("jrrzmr"), 100_000_000.0),
                "financing_repayment_cny": None,
                "securities_lending_balance_qty": number(item.get("jrrjyl")),
                "securities_lending_sell_qty": number(item.get("jrrjmc")),
                "securities_lending_balance_cny": number(item.get("jrrjye"), 100_000_000.0),
                "margin_total_balance_cny": number(item.get("jrrzrjye"), 100_000_000.0),
                "repayment_evidence": "PROXY",
                "source_url": "https://www.szse.cn/disclosure/margin/margin/index.html",
                "available_at": f"{next_weekday(date)}T09:00:00+08:00",
                "availability_basis": "contractual T+1 09:00 cutoff; publication timestamp not exposed",
            }
        )
    for index in range(1, len(raw_rows)):
        previous = raw_rows[index - 1]["financing_balance_cny"]
        current = raw_rows[index]["financing_balance_cny"]
        buy = raw_rows[index]["financing_buy_cny"]
        raw_rows[index]["financing_repayment_cny"] = implied_margin_repayment(previous, current, buy)
    return raw_rows


def finalize_margin(rows: list[dict[str, Any]], dates: tuple[str, ...]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    by_exchange: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_exchange.setdefault(str(row["exchange"]), []).append(row)
    for exchange_rows in by_exchange.values():
        exchange_rows.sort(key=lambda row: row["date"])
        for index, row in enumerate(exchange_rows):
            if index == 0:
                quality, residual, tolerance = "MISSING", None, None
                notes = "previous pilot-day balance unavailable for in-window identity"
            else:
                previous = exchange_rows[index - 1]["financing_balance_cny"]
                result = reconcile_margin(
                    previous,
                    row["financing_balance_cny"],
                    row["financing_buy_cny"],
                    row["financing_repayment_cny"],
                    absolute_tolerance=100.0,
                )
                quality, residual, tolerance, notes = result.quality, result.residual, result.tolerance, result.detail
            output.append(
                {
                    **row,
                    "reconciliation_residual_cny": residual,
                    "tolerance_cny": tolerance,
                    "quality": quality,
                    "notes": notes,
                }
            )
    present = {(str(row["date"]), str(row["exchange"])) for row in output}
    for date in dates:
        if (date, "BSE") in present:
            continue
        output.append(
            {
                "date": date,
                "exchange": "BSE",
                "financing_balance_cny": None,
                "financing_buy_cny": None,
                "financing_repayment_cny": None,
                "securities_lending_balance_qty": None,
                "securities_lending_sell_qty": None,
                "securities_lending_balance_cny": None,
                "margin_total_balance_cny": None,
                "repayment_evidence": "NOT_IDENTIFIED",
                "source_url": BSE_MARGIN_PAGE,
                "available_at": None,
                "availability_basis": "official page unavailable during probe",
                "reconciliation_residual_cny": None,
                "tolerance_cny": None,
                "quality": "MISSING",
                "notes": "BSE official page returned maintenance content on 2026-07-18; no vendor substitution",
            }
        )
    return sorted(output, key=lambda row: (row["date"], row["exchange"]))


def collect_bse_probe(collector: RawCollector) -> tuple[str, str]:
    try:
        response = collector.get(
            source_id="bse_margin_page",
            url=BSE_MARGIN_PAGE,
            relative_path="bse/margin_page.html",
            referer="https://www.bse.cn/",
            allow_http_error=True,
        )
    except requests.RequestException as exc:
        return "MISSING", f"official page network failure during probe: {type(exc).__name__}"
    if response.status_code >= 400:
        return "MISSING", f"official page returned HTTP {response.status_code} during probe"
    text = response.content.decode(response.encoding or "utf-8", errors="replace")
    if "网站维护" in text or "maintenance" in text.lower() or "bad gateway" in text.lower():
        return "MISSING", "official page returned maintenance content"
    return "REVIEW", "official page reachable but structured historical endpoint not verified"


def collect_sse_etf_proxy(collector: RawCollector) -> list[dict[str, Any]]:
    response = collector.get(
        source_id="sse_etf_scale",
        url=SSE_QUERY_URL,
        params={
            "sqlId": "COMMON_JJZWZ_SCSJ_CJGK_JJCPMXB_L",
            "isPagination": "true",
            "pageHelp.pageSize": "5000",
            "pageHelp.pageNo": "1",
        },
        relative_path="sse/etf_scale_current.json",
        referer="https://etf.sse.com.cn/marketdata/",
    )
    payload = json_payload(response)
    return [
        {
            "snapshot_date": str(item.get("TRADE_DATE", ""))[:10],
            "exchange": "SSE",
            "symbol": f"{item.get('FUND_CODE')}.SH",
            "name": clean_text(item.get("FUND_ABBR")),
            "scale_value": number(item.get("SCALE")),
            "scale_unit": "official page display unit; market-value scale",
            "daily_scale_change": number(item.get("DAY_SCALE_CHANGE")),
            "shares": None,
            "nav": None,
            "evidence_state": "PROXY",
            "quality": "MISSING",
            "source_url": "https://etf.sse.com.cn/marketdata/",
            "notes": "not a verified daily shares-plus-NAV series; cannot enter ETF AUM decomposition",
        }
        for item in payload.get("result") or []
    ]


def collect_szse_etf_proxy(collector: RawCollector) -> list[dict[str, Any]]:
    try:
        first = szse_report(collector, "1945", "etf_list_page_1", PAGENO=1)
    except requests.RequestException:
        return []
    report = (first or [{}])[0]
    page_count = int((report.get("metadata") or {}).get("pagecount") or 1)
    snapshot_date = str((report.get("metadata") or {}).get("subname") or "")[:10]
    items = list(report.get("data") or [])
    for page in range(2, page_count + 1):
        try:
            payload = szse_report(collector, "1945", f"etf_list_page_{page}", PAGENO=page)
        except requests.RequestException:
            break
        items.extend(((payload or [{}])[0].get("data") or []))
    output: list[dict[str, Any]] = []
    for item in items:
        code_match = re.search(r"\b(1\d{5})\b", str(item.get("sys_key", "")))
        if not code_match:
            continue
        output.append(
            {
                "snapshot_date": snapshot_date,
                "exchange": "SZSE",
                "symbol": f"{code_match.group(1)}.SZ",
                "name": clean_text(item.get("kzjcurl")),
                "scale_value": number(item.get("dqgm")),
                "scale_unit": "10,000 fund units",
                "daily_scale_change": None,
                "shares": number(item.get("dqgm"), 10_000.0),
                "nav": None,
                "evidence_state": "PARTIALLY_IDENTIFIED",
                "quality": "MISSING",
                "source_url": "https://www.szse.cn/market/product/list/etfList/index.html",
                "notes": "current shares observed, NAV and 20-day history not collected; decomposition unavailable",
            }
        )
    return output


def parse_hkex_eligible(response: requests.Response, market: str) -> list[dict[str, Any]]:
    text = response.content.decode("utf-16", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    header_index = next((index for index, line in enumerate(lines) if line.startswith("No.\t")), -1)
    if header_index < 0:
        return []
    reader = csv.reader(lines[header_index:], delimiter="\t")
    next(reader)
    output: list[dict[str, Any]] = []
    for row in reader:
        if len(row) < 6 or not row[1].strip().isdigit():
            continue
        code = row[1].strip().zfill(6)
        output.append(
            {
                "snapshot_date": "2026-07-17",
                "direction": "NORTHBOUND",
                "channel": market,
                "symbol": f"{code}.{'SH' if market == 'SSE' else 'SZ'}",
                "name": row[3].strip(),
                "instrument_type": row[5].strip(),
                "trade_status": row[6].strip() if len(row) > 6 else "ELIGIBLE",
                "point_in_time_history": False,
                "evidence_state": "OBSERVED",
                "quality": "OK",
                "source_url": response.url,
            }
        )
    return output


def collect_connect_eligible(collector: RawCollector) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for market, url in (("SSE", HKEX_NB_SSE), ("SZSE", HKEX_NB_SZSE)):
        response = collector.get(
            source_id=f"hkex_northbound_{market.lower()}",
            url=url,
            relative_path=f"hkex/northbound_{market.lower()}_eligible.csv",
            referer="https://www.hkex.com.hk/mutual-market/stock-connect/eligible-stocks/view-all-eligible-securities?sc_lang=en",
        )
        rows.extend(parse_hkex_eligible(response, market))
    response = collector.get(
        source_id="sse_southbound_eligible",
        url=SSE_QUERY_URL,
        params={
            "sqlId": "COMMON_SSE_JYFW_HGT_XXPL_BDZQQD_L",
            "isPagination": "true",
            "pageHelp.pageSize": "5000",
            "pageHelp.pageNo": "1",
        },
        relative_path="sse/southbound_eligible.json",
        referer="https://www.sse.com.cn/services/hkexsc/disclo/eligible/",
    )
    payload = json_payload(response)
    rows.extend(
        {
            "snapshot_date": str(item.get("UPDATE_DATE", ""))[:10],
            "direction": "SOUTHBOUND",
            "channel": "SSE",
            "symbol": f"{str(item.get('SECURITY_CODE', '')).zfill(5)}.HK",
            "name": clean_text(item.get("ABBR_CN")),
            "instrument_type": clean_text(item.get("SECURITY_TYPE")),
            "trade_status": clean_text(item.get("TRADE_FLAG")) or "ELIGIBLE",
            "point_in_time_history": False,
            "evidence_state": "OBSERVED",
            "quality": "OK",
            "source_url": "https://www.sse.com.cn/services/hkexsc/disclo/eligible/",
        }
        for item in payload.get("result") or []
    )
    try:
        first = szse_report(collector, "SGT_GGTBDQD", "southbound_eligible_page_1", PAGENO=1)
    except requests.RequestException:
        return rows
    report = (first or [{}])[0]
    page_count = int((report.get("metadata") or {}).get("pagecount") or 1)
    sb_items = list(report.get("data") or [])
    for page in range(2, page_count + 1):
        try:
            current = szse_report(collector, "SGT_GGTBDQD", f"southbound_eligible_page_{page}", PAGENO=page)
        except requests.RequestException:
            break
        sb_items.extend(((current or [{}])[0].get("data") or []))
    snapshot_date = str((report.get("metadata") or {}).get("subname") or "")[:10]
    for item in sb_items:
        code = clean_text(item.get("zqdm"))
        if not code:
            continue
        rows.append(
            {
                "snapshot_date": snapshot_date,
                "direction": "SOUTHBOUND",
                "channel": "SZSE",
                "symbol": f"{code.zfill(5)}.HK",
                "name": clean_text(item.get("zqjc")),
                "instrument_type": clean_text(item.get("zqlb")),
                "trade_status": "ELIGIBLE",
                "point_in_time_history": False,
                "evidence_state": "OBSERVED",
                "quality": "OK",
                "source_url": "https://www.szse.cn/szhk/hkbussiness/underlylist/",
            }
        )
    return rows


def label_map(items: Iterable[dict[str, Any]], value_key: str) -> dict[str, float | None]:
    return {clean_text(item.get("label")): number(item.get(value_key), 100_000_000.0) for item in items}


def collect_connect_turnover(collector: RawCollector, dates: tuple[str, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for date in dates:
        compact = date.replace("-", "")
        response = collector.get(
            source_id="sse_southbound_daily_turnover",
            url=SSE_CONNECT_QUOTE_URL,
            params={"tradeDate": compact},
            relative_path=f"sse/southbound_daily_{date}.json",
            referer="https://www.sse.com.cn/services/hkexsc/disclo/quote/",
        )
        result_items = json_payload(response).get("result") or []
        result = result_items[0] if result_items and isinstance(result_items[0], dict) else {}
        rows.append(
            {
                "date": date,
                "direction": "SOUTHBOUND",
                "channel": "SSE",
                "buy_amount_hkd": sse_hundred_million_hkd(result.get("BUY_AMOUNT")),
                "sell_amount_hkd": sse_hundred_million_hkd(result.get("SELL_AMOUNT")),
                "total_amount_hkd": sse_hundred_million_hkd(result.get("TOTAL_AMOUNT")),
                "northbound_total_amount_cny": None,
                "evidence_state": "OBSERVED",
                "quality": "OK" if result else "MISSING",
                "source_url": SSE_CONNECT_QUOTE_URL,
                "notes": "SSE endpoint display unit converted from HKD 100 million to absolute HKD",
            }
        )
        try:
            deep = szse_report(collector, "SGT_SGTJYRB", f"northbound_daily_{date}", txtDate=date)
            deep_items = ((deep or [{}])[0].get("data") or [])
        except requests.RequestException:
            deep_items = []
        deep_values = label_map(deep_items, "total")
        north_total = next((value for label, value in deep_values.items() if "交易总额" in label and "ETF" not in label), None)
        rows.append(
            {
                "date": date,
                "direction": "NORTHBOUND",
                "channel": "SZSE",
                "buy_amount_hkd": None,
                "sell_amount_hkd": None,
                "total_amount_hkd": None,
                "northbound_total_amount_cny": north_total,
                "evidence_state": "OBSERVED",
                "quality": "OK" if north_total is not None else "MISSING",
                "source_url": "https://www.szse.cn/szhk/szhktradeinfo/szdaily/index.html",
            }
        )
        try:
            south = szse_report(collector, "SGT_GGTJYRB", f"southbound_daily_{date}", txtDate=date)
            south_items = ((south or [{}])[0].get("data") or [])
        except requests.RequestException:
            south_items = []
        south_values = label_map(south_items, "je")
        buy = next((value for label, value in south_values.items() if "买入交易金额" in label), None)
        sell = next((value for label, value in south_values.items() if "卖出交易金额" in label), None)
        rows.append(
            {
                "date": date,
                "direction": "SOUTHBOUND",
                "channel": "SZSE",
                "buy_amount_hkd": buy,
                "sell_amount_hkd": sell,
                "total_amount_hkd": (buy + sell) if buy is not None and sell is not None else None,
                "northbound_total_amount_cny": None,
                "evidence_state": "OBSERVED",
                "quality": "OK" if buy is not None and sell is not None else "MISSING",
                "source_url": "https://www.szse.cn/szhk/szhktradeinfo/hkdaily/index.html",
            }
        )
    return rows


def collect_fx_reference(collector: RawCollector, dates: tuple[str, ...]) -> list[dict[str, Any]]:
    try:
        response = collector.get(
            source_id="sse_stock_connect_reference_fx",
            url=SSE_SOA_URL,
            params={
                "isPagination": "false",
                "sqlId": "FW_HGT_GGTHL",
                "updateDate": dates[0].replace("-", ""),
                "updateDateEnd": dates[-1].replace("-", ""),
            },
            relative_path="sse/connect_reference_fx.json",
            referer="https://www.sse.com.cn/services/hkexsc/disclo/ratios/",
        )
    except requests.RequestException:
        return []
    payload = json_payload(response)
    rows: list[dict[str, Any]] = []
    for item in payload.get("result") or []:
        rows.append(
            {
                "date": str(item.get("validDate") or item.get("VALID_DATE") or "")[:10],
                "currency": clean_text(item.get("currencyType") or item.get("CURRENCY_TYPE")),
                "buy_reference_rate": number(item.get("buyPrice") or item.get("BUY_PRICE")),
                "sell_reference_rate": number(item.get("sellPrice") or item.get("SELL_PRICE")),
                "evidence_state": "PROXY",
                "quality": "OK",
                "notes": "Stock Connect reference exchange rate; not general spot FX",
                "source_url": "https://www.sse.com.cn/services/hkexsc/disclo/ratios/",
            }
        )
    return rows
