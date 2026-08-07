from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


RUN_ID = "core12-core19-bounded-source-evidence-20260730-v1"
WORK_PACKAGE_ID = "L1-D2A-04B_CORE12_CORE19_BOUNDED_SOURCE_EVIDENCE"
PURPOSE_CEILING = "INSPECTION_ONLY"
RAW_RELATIVE = Path(
    "data/market/quarantine/official-public"
) / RUN_ID / "raw"
CANDIDATE_RELATIVE = Path("tmp/core12-core19-source-evidence") / RUN_ID
REPORT_RELATIVE = Path("reports/data-governance") / RUN_ID
CATALOG_RELATIVE = Path("data/catalog/datasets.json")
MAX_RESPONSE_BYTES = 1_000_000

CNI_SAMPLE_START = "2026-07-20"
CNI_SAMPLE_END = "2026-07-24"
CNI_INDEXES = (
    ("399001", "broad_price_native"),
    ("399002", "broad_return_native_unresolved"),
    ("980017", "semiconductor_theme_native"),
)

SOURCES = (
    {
        "request_id": "cni_rights_page",
        "url": (
            "https://www.cnindex.com.cn/zh_services/data_services/"
            "index.html?act_menu=null&act_service=2"
        ),
        "raw_name": "cni_rights_page.html",
        "scope": "CORE-12",
        "role": "rights_and_product_service_evidence",
    },
    {
        "request_id": "pboc_policy_rate_announcement",
        "url": (
            "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/"
            "125431/125475/2025111413260267250/index.html"
        ),
        "raw_name": "pboc_omo_2025_216.html",
        "scope": "CORE-19",
        "role": "policy_rate_observation_sample",
    },
    {
        "request_id": "pboc_legal_statement",
        "url": (
            "https://jzcg.pbc.gov.cn/freecms/site/templet/flsm/index.html"
        ),
        "raw_name": "pboc_legal_statement.html",
        "scope": "CORE-19",
        "role": "rights_and_attribution_evidence",
    },
    {
        "request_id": "chinamoney_shibor_page",
        "url": "https://www.chinamoney.com.cn/english/bmkshb/",
        "raw_name": "chinamoney_shibor_page.html",
        "scope": "CORE-19",
        "role": "series_methodology_and_publication_schedule_evidence",
    },
    {
        "request_id": "chinamoney_services_page",
        "url": "https://www.chinamoney.com.cn/chinese/acserv/",
        "raw_name": "chinamoney_services_page.html",
        "scope": "CORE-19",
        "role": "official_service_and_access_channel_evidence",
    },
    {
        "request_id": "chinabond_curve_page",
        "url": "https://yield.chinabond.com.cn/cbweb-mn/pgxh/pgxhIndex",
        "raw_name": "chinabond_curve_page.html",
        "scope": "CORE-19",
        "role": "official_curve_page_discovery_only",
    },
)


class EvidenceError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    )


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )


def workspace_relative(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def decode_html(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise EvidenceError("HTML response is neither UTF-8 nor GB18030")


def visible_text(payload: bytes) -> str:
    text = decode_html(payload)
    text = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def fetch_one(
    *,
    workspace: Path,
    raw_root: Path,
    request_id: str,
    url: str,
    raw_name: str,
    scope: str,
    role: str,
) -> tuple[dict[str, Any], bytes]:
    requested_at = utc_now()
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.1",
            "User-Agent": "Mozilla/5.0 CodexBoundedEvidence/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise EvidenceError(
                f"{request_id} exceeded {MAX_RESPONSE_BYTES} bytes"
            )
        fetched_at = utc_now()
        status = int(response.status)
        final_url = response.geturl()
        headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower()
            in {
                "content-type",
                "date",
                "etag",
                "last-modified",
                "content-length",
            }
        }
    if status != 200:
        raise EvidenceError(f"{request_id} returned HTTP {status}")
    allowed_host = urllib.parse.urlparse(url).hostname
    final_host = urllib.parse.urlparse(final_url).hostname
    if final_host != allowed_host:
        raise EvidenceError(
            f"{request_id} redirected across hosts: {allowed_host} -> {final_host}"
        )
    raw_path = raw_root / raw_name
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(payload)
    receipt = {
        "request_id": request_id,
        "scope": scope,
        "role": role,
        "url": url,
        "final_url": final_url,
        "method": "GET",
        "status_code": status,
        "requested_at": requested_at,
        "fetched_at": fetched_at,
        "response_headers": headers,
        "raw_path": workspace_relative(raw_path, workspace),
        "raw_bytes": len(payload),
        "raw_sha256": sha256_bytes(payload),
        "retry_count": 0,
        "credentials_used": False,
        "purpose_ceiling": PURPOSE_CEILING,
    }
    return receipt, payload


def fetch_cni_samples(
    workspace: Path, raw_root: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    receipts: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    expected_fields = [
        "timestamp",
        "current",
        "high",
        "open",
        "low",
        "close",
        "chg",
        "percent",
        "amount",
        "volume",
        "avg",
    ]
    for code, sample_role in CNI_INDEXES:
        query = urllib.parse.urlencode(
            {
                "indexCode": code,
                "startDate": CNI_SAMPLE_START,
                "endDate": CNI_SAMPLE_END,
                "frequency": "day",
            }
        )
        url = (
            "https://hq.cnindex.com.cn/market/market/"
            f"getIndexDailyDataWithDataFormat?{query}"
        )
        receipt, payload = fetch_one(
            workspace=workspace,
            raw_root=raw_root,
            request_id=f"cni_index_daily_{code}",
            url=url,
            raw_name=f"cni_index_daily_{code}.json",
            scope="CORE-12",
            role=f"official_five_day_sample__{sample_role}",
        )
        receipts.append(receipt)
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"CNI {code} is not valid JSON") from exc
        if document.get("code") != 200:
            raise EvidenceError(f"CNI {code} business code is not 200")
        data = document.get("data")
        if not isinstance(data, dict):
            raise EvidenceError(f"CNI {code} data object is missing")
        fields = data.get("item")
        if fields != expected_fields:
            raise EvidenceError(
                f"CNI {code} field vector changed: {fields!r}"
            )
        vectors = data.get("data")
        if not isinstance(vectors, list) or len(vectors) != 5:
            raise EvidenceError(
                f"CNI {code} expected 5 rows, got "
                f"{len(vectors) if isinstance(vectors, list) else 'non-list'}"
            )
        seen_dates: set[str] = set()
        for position, vector in enumerate(vectors, start=1):
            if not isinstance(vector, list) or len(vector) != len(fields):
                raise EvidenceError(
                    f"CNI {code} row {position} width changed"
                )
            record = dict(zip(fields, vector, strict=True))
            trade_date = str(record["timestamp"])
            try:
                parsed_date = dt.date.fromisoformat(trade_date)
            except ValueError as exc:
                raise EvidenceError(
                    f"CNI {code} invalid trade_date {trade_date!r}"
                ) from exc
            if not (
                dt.date.fromisoformat(CNI_SAMPLE_START)
                <= parsed_date
                <= dt.date.fromisoformat(CNI_SAMPLE_END)
            ):
                raise EvidenceError(
                    f"CNI {code} date outside requested window: {trade_date}"
                )
            if trade_date in seen_dates:
                raise EvidenceError(
                    f"CNI {code} duplicate trade_date: {trade_date}"
                )
            seen_dates.add(trade_date)
            numeric_names = (
                "current",
                "high",
                "open",
                "low",
                "close",
                "chg",
                "amount",
                "volume",
            )
            for name in numeric_names:
                value = float(record[name])
                if not math.isfinite(value):
                    raise EvidenceError(
                        f"CNI {code} {trade_date} {name} is not finite"
                    )
            high = float(record["high"])
            low = float(record["low"])
            open_value = float(record["open"])
            close = float(record["close"])
            if not (
                close > 0
                and high >= max(open_value, close)
                and low <= min(open_value, close)
                and high >= low
            ):
                raise EvidenceError(
                    f"CNI {code} {trade_date} failed OHLC check"
                )
            percent_raw = str(record["percent"])
            if not percent_raw.endswith("%"):
                raise EvidenceError(
                    f"CNI {code} {trade_date} percent unit changed"
                )
            pct_fraction = float(percent_raw[:-1]) / 100.0
            rows.append(
                {
                    "source_id": "CNI_INDEX_OFFICIAL",
                    "provider_id": "CNI",
                    "provider_native_index_code": code,
                    "provider_native_index_name": data.get("indexName"),
                    "provider_native_index_name_en": data.get("indexEName"),
                    "sample_role": sample_role,
                    "return_variant": (
                        "UNKNOWN__PROVIDER_NATIVE_CODE_RETAINED"
                    ),
                    "currency": "CNY",
                    "methodology_version": None,
                    "trade_date": trade_date,
                    "open": open_value,
                    "high": high,
                    "low": low,
                    "close": close,
                    "change": float(record["chg"]),
                    "pct_change_fraction": pct_fraction,
                    "aggregate_amount_provider_display_unit": float(
                        record["amount"]
                    ),
                    "aggregate_volume_provider_display_unit": float(
                        record["volume"]
                    ),
                    "published_at": None,
                    "available_at": None,
                    "observed_available_at": receipt["fetched_at"],
                    "first_publication_at": None,
                    "history_regime": "UNKNOWN",
                    "revision_id": receipt["raw_sha256"],
                    "raw_path": receipt["raw_path"],
                    "raw_sha256": receipt["raw_sha256"],
                    "purpose_ceiling": PURPOSE_CEILING,
                    "admission_status": "NOT_ADMITTED",
                    "quality_flags": [
                        "PUBLICATION_TIME_UNRESOLVED",
                        "FIRST_PUBLICATION_AND_BACKCAST_BOUNDARY_UNRESOLVED",
                        "RETURN_VARIANT_SEMANTICS_UNRESOLVED",
                        "AMOUNT_VOLUME_DISPLAY_UNITS_UNRESOLVED",
                        "LOCAL_RETENTION_AND_INTERNAL_RESEARCH_RIGHTS_UNRESOLVED",
                    ],
                }
            )
        checks.append(
            {
                "index_code": code,
                "index_name": data.get("indexName"),
                "row_count": len(vectors),
                "date_min": min(seen_dates),
                "date_max": max(seen_dates),
                "raw_field_count": len(fields),
                "raw_schema_sha256": sha256_bytes(
                    canonical_json_bytes(fields)
                ),
                "business_total": document.get("total"),
                "mechanical_result": "PASS",
            }
        )
        time.sleep(0.25)
    return receipts, rows, checks


def parse_pboc_policy_rate(
    receipt: dict[str, Any], payload: bytes
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text = visible_text(payload)
    published_match = re.search(
        r"文章来源[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        text,
    )
    date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    operation_match = re.search(
        (
            r"(\d+)\s*天\s*"
            r"([0-9]+(?:\s*\.\s*[0-9]+)?)\s*%\s*"
            r"([0-9]+)\s*亿元\s*([0-9]+)\s*亿元"
        ),
        text,
    )
    if not (published_match and date_match and operation_match):
        raise EvidenceError("PBOC policy-rate announcement shape changed")
    year, month, day = (int(value) for value in date_match.groups())
    observation_date = dt.date(year, month, day).isoformat()
    published_at = (
        published_match.group(1).replace(" ", "T") + "+08:00"
    )
    tenor_days = int(operation_match.group(1))
    rate_value = float(re.sub(r"\s+", "", operation_match.group(2)))
    bid_amount = int(operation_match.group(3))
    awarded_amount = int(operation_match.group(4))
    row = {
        "source_id": "PBOC_OFFICIAL",
        "series_id": f"PBOC_OMO_REVERSE_REPO_P{tenor_days}D_POLICY_RATE",
        "series_family": "PBOC_OMO_POLICY_RATE",
        "provider_id": "PBOC",
        "tenor": f"P{tenor_days}D",
        "rate_type": "POLICY_RATE",
        "observation_date": observation_date,
        "rate_value_annual_percent": rate_value,
        "bid_amount_cny_100m": bid_amount,
        "awarded_amount_cny_100m": awarded_amount,
        "published_at": published_at,
        "available_at": None,
        "observed_available_at": receipt["fetched_at"],
        "revised_at": None,
        "revision_id": receipt["raw_sha256"],
        "raw_path": receipt["raw_path"],
        "raw_sha256": receipt["raw_sha256"],
        "purpose_ceiling": PURPOSE_CEILING,
        "admission_status": "NOT_ADMITTED",
        "quality_flags": [
            "POLICY_RATE_NOT_MARKET_RISK_FREE_RETURN",
            "CONTEMPORANEOUS_LOCAL_AVAILABILITY_NOT_PROVEN",
            "REVISION_CHAIN_UNRESOLVED",
            "AUTOMATED_RETENTION_AND_DERIVATIVE_USE_RIGHTS_UNRESOLVED",
        ],
    }
    check = {
        "row_count": 1,
        "observation_date": observation_date,
        "published_at": published_at,
        "tenor": f"P{tenor_days}D",
        "rate_value_annual_percent": rate_value,
        "mechanical_result": "PASS",
        "economic_classification": (
            "POLICY_RATE_SAMPLE__NOT_A_MARKET_RISK_FREE_SERIES"
        ),
    }
    return [row], check


def file_manifest(
    workspace: Path, paths: list[Path]
) -> list[dict[str, Any]]:
    records = []
    for path in sorted({item.resolve() for item in paths}):
        records.append(
            {
                "path": workspace_relative(path, workspace),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def build_readme(results: dict[str, Any]) -> str:
    core12 = results["core12"]
    core19 = results["core19"]
    return f"""# CORE-12 / CORE-19 有界官方来源补证

运行：`{RUN_ID}`  
工作包：`{WORK_PACKAGE_ID}`  
用途上限：`{PURPOSE_CEILING}`  
正式准入：`0`

## 结论

- CORE-12：国证官方日行情接口的三个 provider-native 指数各取得
  {core12["normalized_row_count"] // 3} 个交易日，共
  {core12["normalized_row_count"]} 行；字段宽度、日期、有限值和 OHLC
  机械检查通过。`399001`、`399002` 保持不同身份，未把价格/收益变体合并。
- CORE-19：中国人民银行公开市场公告形成 1 条 7 日逆回购政策利率样本；它是政策
  利率，不是市场无风险收益。中国货币网和中债只保存页面/服务/权利发现证据，没有
  摄取 Shibor、FR/FDR 或国债曲线数值。
- 两项都未闭合许可、完整历史、正式发布时间、修订链和 provider-wide 覆盖，因此
  均为 `INSPECTION_ONLY/NOT_ADMITTED`，没有写正式 catalog。

## CORE-12 已证明

- 官方端点在本次低频最小窗口可达；
- API 自描述 11 个原始字段；
- 三条指数共 {core12["normalized_row_count"]} 行，机械检查全部通过；
- 国证芯片 `980017` 提供一个行业主题样本；
- provider-native 代码和名称原样保留。

## CORE-12 未证明

- 历史批量本地保存和跨项目研究使用权；
- 价格、全收益、净全收益的正式语义映射；
- `published_at`、首次发布日期和回溯历史边界；
- 中证REITs全收益、中证/上证/北证及全部指数的完整覆盖；
- 官方修订链、方法版本、成交量和成交额展示单位。

## CORE-19 已证明

- 央行公告含发布日期时间、操作日期、期限、操作利率、投标量和中标量；
- 中国货币网官方说明 Shibor 的 8 个期限和约 11:00 发布日程；
- 中国货币网服务页和中债曲线页当次可达。

## CORE-19 未证明

- 中国货币网/中债允许自动复制、长期本地保存、内部共享和派生使用；
- Shibor、FR/FDR、3M/6M/1Y 国债曲线的正式数值样本；
- 完整历史、修订行为和逐日官方发布时间；
- 研究应选择哪一条现金收益/无风险序列。L1 不静默选择。

## 边界

- 无付费、无新密钥、无登录、无重试、无全历史回补；
- 无 `datasets.json` 写入、无质量晋级、无快照发布、无研究；
- 原始响应保存在 quarantine，标准化样本只在 tmp，报告在 reports。

## 机器结果

- 原始响应：{results["raw_response_count"]} 个，
  {results["raw_response_bytes"]} B；
- CORE-12：`{core12["status"]}`；
- CORE-19：`{core19["status"]}`；
- 正式 catalog SHA-256 前后相同：`{results["formal_catalog_unchanged"]}`。
"""


def execute(workspace: Path) -> dict[str, Any]:
    raw_root = workspace / RAW_RELATIVE
    candidate_root = workspace / CANDIDATE_RELATIVE
    report_root = workspace / REPORT_RELATIVE
    for root in (raw_root.parent, candidate_root, report_root):
        if root.exists():
            raise EvidenceError(f"refusing to overwrite existing path: {root}")
    raw_root.mkdir(parents=True, exist_ok=False)
    candidate_root.mkdir(parents=True, exist_ok=False)
    report_root.mkdir(parents=True, exist_ok=False)

    catalog_path = workspace / CATALOG_RELATIVE
    catalog_hash_before = sha256_file(catalog_path)
    started_at = utc_now()

    receipts, index_rows, index_checks = fetch_cni_samples(
        workspace, raw_root
    )
    for source in SOURCES:
        receipt, payload = fetch_one(
            workspace=workspace,
            raw_root=raw_root,
            **source,
        )
        receipts.append(receipt)
        if source["request_id"] == "pboc_policy_rate_announcement":
            pboc_rows, pboc_check = parse_pboc_policy_rate(receipt, payload)
        time.sleep(0.25)

    index_path = candidate_root / "normalized" / "index_daily_sample.jsonl"
    pboc_path = candidate_root / "normalized" / "pboc_policy_rate_sample.jsonl"
    receipts_path = candidate_root / "REQUEST_RECEIPTS.json"
    rights_path = candidate_root / "RIGHTS_AND_PIT_ASSESSMENT.json"
    write_jsonl(index_path, index_rows)
    write_jsonl(pboc_path, pboc_rows)
    write_json(receipts_path, {"run_id": RUN_ID, "requests": receipts})

    rights_and_pit = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "assessments": [
            {
                "scope": "CORE-12",
                "source_id": "CNI_INDEX_OFFICIAL",
                "official_display_and_download_observed": True,
                "external_display_requires_authorization": True,
                "automated_historical_collection": "UNRESOLVED",
                "local_retention": "UNRESOLVED",
                "internal_cross_project_research": "UNRESOLVED",
                "external_redistribution": "NOT_AUTHORIZED_BY_THIS_EVIDENCE",
                "published_at": "MISSING_FROM_API_SAMPLE",
                "first_publication_at": "MISSING_FROM_API_SAMPLE",
                "revision_chain": "UNRESOLVED",
                "decision": "BLOCK_FORMAL_ADMISSION",
            },
            {
                "scope": "CORE-19",
                "source_id": "PBOC_OFFICIAL",
                "public_announcement_observed": True,
                "attribution_requirement_observed": True,
                "automated_series_collection": "UNRESOLVED",
                "local_retention": "REVIEW_REQUIRED",
                "internal_cross_project_research": "REVIEW_REQUIRED",
                "external_redistribution": "NOT_AUTHORIZED_BY_THIS_EVIDENCE",
                "published_at": "PRESENT_IN_SAMPLE_PAGE",
                "revision_chain": "UNRESOLVED",
                "decision": "POLICY_RATE_SAMPLE_ONLY__BLOCK_FORMAL_ADMISSION",
            },
            {
                "scope": "CORE-19",
                "source_id": "CHINAMONEY_OFFICIAL",
                "public_methodology_and_service_pages_observed": True,
                "numeric_observations_ingested": False,
                "automated_historical_collection": "LICENSE_BLOCKED",
                "local_retention": "LICENSE_BLOCKED",
                "internal_cross_project_research": "LICENSE_BLOCKED",
                "derivative_use": "LICENSE_BLOCKED",
                "decision": "DISCOVERY_ONLY__NO_RATE_DATA_INGESTION",
            },
            {
                "scope": "CORE-19",
                "source_id": "CHINABOND_OFFICIAL",
                "public_curve_page_observed": True,
                "numeric_observations_ingested": False,
                "automated_historical_collection": "LICENSE_BLOCKED",
                "local_retention": "LICENSE_BLOCKED",
                "internal_cross_project_research": "LICENSE_BLOCKED",
                "derivative_use": "LICENSE_BLOCKED",
                "decision": "DISCOVERY_ONLY__NO_CURVE_DATA_INGESTION",
            },
        ],
        "formal_admission": False,
        "quality_promotion": False,
        "research_use": False,
    }
    write_json(rights_path, rights_and_pit)

    catalog_hash_after = sha256_file(catalog_path)
    raw_paths = [workspace / item["raw_path"] for item in receipts]
    results = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "work_package_id": WORK_PACKAGE_ID,
        "started_at": started_at,
        "completed_at": utc_now(),
        "purpose_ceiling": PURPOSE_CEILING,
        "raw_response_count": len(receipts),
        "raw_response_bytes": sum(item["raw_bytes"] for item in receipts),
        "request_retry_count": sum(item["retry_count"] for item in receipts),
        "credentials_used": False,
        "core12": {
            "status": (
                "LIVE_MINIMAL_STRUCTURE_PASS__RIGHTS_PIT_VARIANTS_"
                "AND_PROVIDER_COVERAGE_OPEN__NOT_ADMITTED"
            ),
            "source_id": "CNI_INDEX_OFFICIAL",
            "provider_native_series_count": len(CNI_INDEXES),
            "normalized_row_count": len(index_rows),
            "sample_start": CNI_SAMPLE_START,
            "sample_end": CNI_SAMPLE_END,
            "mechanical_checks": index_checks,
            "normalized_path": workspace_relative(index_path, workspace),
            "normalized_sha256": sha256_file(index_path),
            "formal_admission": False,
        },
        "core19": {
            "status": (
                "PBOC_POLICY_RATE_SAMPLE_PASS__CHINAMONEY_CHINABOND_"
                "DISCOVERY_ONLY_LICENSE_BLOCKED__NOT_ADMITTED"
            ),
            "pboc_policy_rate_row_count": len(pboc_rows),
            "pboc_mechanical_check": pboc_check,
            "normalized_path": workspace_relative(pboc_path, workspace),
            "normalized_sha256": sha256_file(pboc_path),
            "chinamoney_numeric_rows": 0,
            "chinabond_numeric_rows": 0,
            "formal_admission": False,
        },
        "formal_catalog_sha256_before": catalog_hash_before,
        "formal_catalog_sha256_after": catalog_hash_after,
        "formal_catalog_unchanged": catalog_hash_before == catalog_hash_after,
        "formal_catalog_write": False,
        "formal_snapshot_publish": False,
        "quality_promotion": False,
        "research_use": False,
        "bulk_backfill": False,
        "paid_source": False,
        "new_key": False,
    }
    results_path = report_root / "RESULTS.json"
    readme_path = report_root / "README.md"
    verification_path = report_root / "VERIFICATION.json"
    write_json(results_path, results)
    readme_path.write_text(build_readme(results), encoding="utf-8")

    verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": (
            "PASS__BOUNDED_SOURCE_EVIDENCE_ONLY__FORMAL_ADMISSION_0"
        ),
        "checks": {
            "expected_request_count": len(receipts) == 9,
            "all_http_200": all(
                item["status_code"] == 200 for item in receipts
            ),
            "all_raw_hashes_match": all(
                sha256_file(workspace / item["raw_path"])
                == item["raw_sha256"]
                for item in receipts
            ),
            "core12_three_series": len(index_checks) == 3,
            "core12_fifteen_rows": len(index_rows) == 15,
            "core12_all_mechanical_pass": all(
                item["mechanical_result"] == "PASS"
                for item in index_checks
            ),
            "core19_one_pboc_policy_rate_row": len(pboc_rows) == 1,
            "core19_chinamoney_chinabond_numeric_not_ingested": True,
            "catalog_unchanged": catalog_hash_before == catalog_hash_after,
            "formal_admission_false": True,
            "quality_promotion_false": True,
            "research_use_false": True,
        },
        "blocking_gaps": {
            "CORE-12": [
                "provider and return-variant coverage incomplete",
                "publication and first-publication timestamps unresolved",
                "history/backcast and revision chain unresolved",
                "automated retention and internal-research rights unresolved",
            ],
            "CORE-19": [
                "ChinaMoney and ChinaBond numeric ingestion license blocked",
                "short government and interbank observation samples absent",
                "complete history, revisions and publication timestamps unresolved",
                "research cash benchmark must be pinned later",
            ],
        },
    }
    if not all(verification["checks"].values()):
        verification["status"] = "FAIL_CLOSED"
    write_json(verification_path, verification)

    manifest_paths = (
        raw_paths
        + [
            index_path,
            pboc_path,
            receipts_path,
            rights_path,
            results_path,
            readme_path,
            verification_path,
        ]
    )
    report_manifest = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "files": file_manifest(workspace, manifest_paths),
        "formal_admission": False,
    }
    write_json(report_root / "REPORT_MANIFEST.json", report_manifest)
    return {
        "status": verification["status"],
        "raw_response_count": len(receipts),
        "core12_rows": len(index_rows),
        "core19_rows": len(pboc_rows),
        "report_root": workspace_relative(report_root, workspace),
        "catalog_unchanged": catalog_hash_before == catalog_hash_after,
    }


def verify_existing(workspace: Path) -> dict[str, Any]:
    report_root = workspace / REPORT_RELATIVE
    manifest_path = report_root / "REPORT_MANIFEST.json"
    if not manifest_path.is_file():
        raise EvidenceError(f"manifest not found: {manifest_path}")
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    checked = 0
    for item in document.get("files", []):
        path = workspace / item["path"]
        checked += 1
        if not path.is_file():
            failures.append(f"missing: {item['path']}")
            continue
        if path.stat().st_size != item["bytes"]:
            failures.append(f"size mismatch: {item['path']}")
        if sha256_file(path) != item["sha256"]:
            failures.append(f"hash mismatch: {item['path']}")
    return {
        "status": "PASS" if not failures else "FAIL",
        "checked_files": checked,
        "failures": failures,
        "manifest_sha256": sha256_file(manifest_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    try:
        result = (
            verify_existing(workspace)
            if args.verify_only
            else execute(workspace)
        )
    except (EvidenceError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] in {
        "PASS",
        "PASS__BOUNDED_SOURCE_EVIDENCE_ONLY__FORMAL_ADMISSION_0",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
