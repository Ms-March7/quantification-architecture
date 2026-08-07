#!/usr/bin/env python3
"""Bounded dual-source inspection probe for one A-share trading date.

The runner intentionally uses only public web quote responses, performs at
most one batch request per source, writes only below ``tmp/``, and never
grants source admission, catalog registration, quality promotion, or research
use.  It is a source-feasibility probe, not a production downloader.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from urllib.parse import urlparse
from uuid import uuid4
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
ENGINE_ID = "free_web_daily_dual_source_probe/v1.1"
PURPOSE_CEILING = "INSPECTION_ONLY"
CONTRACT_PATH = (
    ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "free_web_daily_inspection_v1"
    / "contract.json"
)
CONTRACT_README_PATH = CONTRACT_PATH.with_name("README.md")
SYMBOL_RE = re.compile(r"^(?:sh|sz|bj)\d{6}$")
SINA_LINE_RE = re.compile(r'^var hq_str_([a-z]{2}\d{6})="(.*)";$')
TENCENT_LINE_RE = re.compile(r'^v_([a-z]{2}\d{6})="(.*)";$')
MAX_SYMBOLS = 20
PRICE_TOLERANCE = 0.000001
AMOUNT_TOLERANCE_CNY = 1.0
VOLUME_TOLERANCE_SHARES = 99
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SAME_DAY_FINALITY_CUTOFF = time(15, 45)
MINIMUM_FINAL_OBSERVED_TIME = time(15, 0)


class ProbeError(RuntimeError):
    """Fail-closed error for an invalid request or response."""


@dataclass(frozen=True)
class Quote:
    source_id: str
    symbol: str
    security_id: str
    trade_date: str
    observed_time: str
    name: str
    open: float
    high: float
    low: float
    close: float
    previous_close: float
    volume_shares: int
    amount_cny: float
    source_marker: str
    source_marker_semantics: str
    raw_volume_unit: str


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so one logical call is also one HTTP request."""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_symbols(values: Iterable[str]) -> list[str]:
    symbols: list[str] = []
    for raw in values:
        symbol = raw.strip().lower()
        if not symbol:
            continue
        if not SYMBOL_RE.fullmatch(symbol):
            raise ProbeError(f"invalid symbol: {raw!r}")
        if symbol in symbols:
            raise ProbeError(f"duplicate symbol: {symbol}")
        symbols.append(symbol)
    if not symbols:
        raise ProbeError("at least one symbol is required")
    if len(symbols) > MAX_SYMBOLS:
        raise ProbeError(f"bounded probe accepts at most {MAX_SYMBOLS} symbols")
    return symbols


def validate_trade_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ProbeError("trade date must be ISO YYYY-MM-DD") from exc
    if parsed > datetime.now(SHANGHAI_TZ).date():
        raise ProbeError("trade date may not be in the future")
    return parsed.isoformat()


def validate_same_day_finality(
    trade_date: str, *, now: datetime | None = None
) -> dict[str, str]:
    """Permit a same-day quote only after the Shanghai close buffer."""

    current = now or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        raise ProbeError("finality clock must be timezone-aware")
    local = current.astimezone(SHANGHAI_TZ)
    requested = date.fromisoformat(trade_date)
    if requested > local.date():
        raise ProbeError("trade date may not be in the future")
    if requested == local.date() and local.time() < SAME_DAY_FINALITY_CUTOFF:
        raise ProbeError(
            "same-day probe is forbidden before 15:45 Asia/Shanghai finality cutoff"
        )
    return {
        "timezone": "Asia/Shanghai",
        "cutoff_local_time": SAME_DAY_FINALITY_CUTOFF.isoformat(),
        "minimum_final_observed_time": MINIMUM_FINAL_OBSERVED_TIME.isoformat(),
        "validated_at_local": local.isoformat(),
    }


def validate_output_root(path: Path) -> Path:
    lexical = Path(os.path.abspath(path))
    allowed = (ROOT / "tmp" / "free-web-daily-probe").resolve()
    resolved_parent = lexical.parent.resolve()
    try:
        resolved_parent.relative_to(allowed)
    except ValueError as exc:
        raise ProbeError("output must stay below tmp/free-web-daily-probe") from exc
    if lexical.exists():
        raise ProbeError("output directory already exists; runs are immutable")
    return lexical


def request_once(url: str, *, referer: str) -> tuple[bytes, dict[str, Any]]:
    started_at = datetime.now(timezone.utc)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Quantification bounded inspection)",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Referer": referer,
        },
    )
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=20) as response:
            final_url = response.geturl()
            payload = response.read()
            completed_at = datetime.now(timezone.utc)
            if response.status != 200:
                raise ProbeError(f"unexpected HTTP status: {response.status}")
            if final_url != url:
                raise ProbeError("redirect or final URL mutation is forbidden")
            if not payload:
                raise ProbeError("empty response")
            metadata = {
                "http_status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "content_length": len(payload),
                "requested_url": url,
                "final_url": final_url,
                "redirect_count": 0,
                "network_attempt_count": 1,
                "request_started_at_utc": started_at.isoformat(),
                "response_completed_at_utc": completed_at.isoformat(),
                "response_completed_at_asia_shanghai": completed_at.astimezone(
                    SHANGHAI_TZ
                ).isoformat(),
            }
            return payload, metadata
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ProbeError(f"redirect forbidden: HTTP {exc.code}") from exc
        raise ProbeError(f"HTTP request failed: {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ProbeError(f"network request failed: {exc.reason}") from exc


def _float(value: str, label: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise ProbeError(f"invalid numeric {label}: {value!r}") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise ProbeError(f"non-finite numeric {label}")
    return result


def _int(value: str, label: str) -> int:
    number = _float(value, label)
    if not number.is_integer():
        raise ProbeError(f"non-integral {label}: {value!r}")
    return int(number)


def _observed_time(value: str, label: str) -> str:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ProbeError(f"invalid observed time {label}: {value!r}") from exc
    if parsed.tzinfo is not None or parsed.isoformat() != value:
        raise ProbeError(f"observed time must be canonical HH:MM:SS: {label}")
    return value


def _offset_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ProbeError(f"{label} must be an offset timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProbeError(f"invalid offset timestamp {label}") from exc
    if parsed.tzinfo is None:
        raise ProbeError(f"{label} must include an offset")
    return parsed


def validate_request_metadata(
    metadata: Mapping[str, Any], *, requested_url: str, payload: bytes
) -> dict[str, Any]:
    """Validate and normalize the one-attempt, no-redirect capture receipt."""

    required = {
        "http_status",
        "content_type",
        "content_length",
        "requested_url",
        "final_url",
        "redirect_count",
        "network_attempt_count",
        "request_started_at_utc",
        "response_completed_at_utc",
        "response_completed_at_asia_shanghai",
    }
    missing = sorted(required - set(metadata))
    if missing:
        raise ProbeError(f"capture metadata missing required fields: {missing}")
    if type(metadata["http_status"]) is not int or metadata["http_status"] != 200:
        raise ProbeError("capture metadata http_status must be integer 200")
    if not isinstance(metadata["content_type"], str):
        raise ProbeError("capture metadata content_type must be a string")
    if (
        type(metadata["content_length"]) is not int
        or metadata["content_length"] != len(payload)
    ):
        raise ProbeError("capture metadata content_length mismatch")
    if metadata["requested_url"] != requested_url:
        raise ProbeError("capture metadata requested URL mismatch")
    if metadata["final_url"] != requested_url:
        raise ProbeError("capture metadata final URL or redirect mismatch")
    if type(metadata["redirect_count"]) is not int or metadata["redirect_count"] != 0:
        raise ProbeError("capture metadata must record zero redirects")
    if (
        type(metadata["network_attempt_count"]) is not int
        or metadata["network_attempt_count"] != 1
    ):
        raise ProbeError("capture metadata must record exactly one network attempt")

    started = _offset_datetime(metadata["request_started_at_utc"], "request_started_at_utc")
    completed = _offset_datetime(
        metadata["response_completed_at_utc"], "response_completed_at_utc"
    )
    completed_local = _offset_datetime(
        metadata["response_completed_at_asia_shanghai"],
        "response_completed_at_asia_shanghai",
    )
    if started.utcoffset() != timezone.utc.utcoffset(started):
        raise ProbeError("request_started_at_utc must use UTC")
    if completed.utcoffset() != timezone.utc.utcoffset(completed):
        raise ProbeError("response_completed_at_utc must use UTC")
    if completed < started:
        raise ProbeError("response completion precedes request start")
    if completed_local.astimezone(timezone.utc) != completed.astimezone(timezone.utc):
        raise ProbeError("UTC and Asia/Shanghai completion timestamps disagree")
    if completed_local.utcoffset() != SHANGHAI_TZ.utcoffset(completed_local):
        raise ProbeError("response_completed_at_asia_shanghai has the wrong offset")
    return {key: metadata[key] for key in sorted(required)}


def parse_sina(payload: bytes) -> dict[str, Quote]:
    text = payload.decode("gb18030", errors="strict")
    quotes: dict[str, Quote] = {}
    for line in text.splitlines():
        match = SINA_LINE_RE.fullmatch(line.strip())
        if not match:
            raise ProbeError("unrecognized Sina response line")
        symbol, body = match.groups()
        fields = body.split(",")
        if len(fields) < 33:
            raise ProbeError(f"Sina row too narrow for {symbol}: {len(fields)}")
        quote = Quote(
            source_id="SINA_WEB_QUOTE",
            symbol=symbol,
            security_id=symbol[2:],
            trade_date=fields[30],
            observed_time=_observed_time(fields[31], "sina.observed_time"),
            name=fields[0],
            open=_float(fields[1], "sina.open"),
            high=_float(fields[4], "sina.high"),
            low=_float(fields[5], "sina.low"),
            close=_float(fields[3], "sina.close"),
            previous_close=_float(fields[2], "sina.previous_close"),
            volume_shares=_int(fields[8], "sina.volume_shares"),
            amount_cny=_float(fields[9], "sina.amount_cny"),
            source_marker=fields[32],
            source_marker_semantics=(
                "UNRESOLVED__OBSERVED_SINA_FIELD_32__NOT_USED_AS_TRADING_STATUS"
            ),
            raw_volume_unit="SHARE",
        )
        if symbol in quotes:
            raise ProbeError(f"duplicate Sina symbol: {symbol}")
        quotes[symbol] = quote
    return quotes


def parse_tencent(payload: bytes) -> dict[str, Quote]:
    text = payload.decode("gb18030", errors="strict")
    quotes: dict[str, Quote] = {}
    for line in text.splitlines():
        match = TENCENT_LINE_RE.fullmatch(line.strip())
        if not match:
            raise ProbeError("unrecognized Tencent response line")
        symbol, body = match.groups()
        fields = body.split("~")
        if len(fields) < 36:
            raise ProbeError(f"Tencent row too narrow for {symbol}: {len(fields)}")
        timestamp = fields[30]
        if not re.fullmatch(r"\d{14}", timestamp):
            raise ProbeError(f"invalid Tencent timestamp for {symbol}")
        composite = fields[35].split("/")
        if len(composite) != 3:
            raise ProbeError(f"invalid Tencent close/volume/amount composite for {symbol}")
        volume_lots = _int(composite[1], "tencent.volume_lots")
        quote = Quote(
            source_id="TENCENT_WEB_QUOTE",
            symbol=symbol,
            security_id=symbol[2:],
            trade_date=f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}",
            observed_time=_observed_time(
                f"{timestamp[8:10]}:{timestamp[10:12]}:{timestamp[12:14]}",
                "tencent.observed_time",
            ),
            name=fields[1],
            open=_float(fields[5], "tencent.open"),
            high=_float(fields[33], "tencent.high"),
            low=_float(fields[34], "tencent.low"),
            close=_float(fields[3], "tencent.close"),
            previous_close=_float(fields[4], "tencent.previous_close"),
            volume_shares=volume_lots * 100,
            amount_cny=_float(composite[2], "tencent.amount_cny"),
            source_marker=fields[0],
            source_marker_semantics=(
                "UNRESOLVED__OBSERVED_TENCENT_FIELD_0_ROUTE_CODE__"
                "NOT_TRADING_STATUS"
            ),
            raw_volume_unit="BOARD_LOT_100_SHARES",
        )
        if symbol in quotes:
            raise ProbeError(f"duplicate Tencent symbol: {symbol}")
        quotes[symbol] = quote
    return quotes


def reconcile(
    symbols: list[str],
    trade_date: str,
    sina: dict[str, Quote],
    tencent: dict[str, Quote],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requested = set(symbols)
    for source_id, quotes in (("SINA", sina), ("TENCENT", tencent)):
        unexpected = sorted(set(quotes) - requested)
        if unexpected:
            raise ProbeError(
                f"{source_id} response contains unexpected symbols: {unexpected}"
            )
    accepted: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for symbol in symbols:
        left = sina.get(symbol)
        right = tencent.get(symbol)
        reasons: list[str] = []
        if left is None:
            reasons.append("SINA_SYMBOL_MISSING")
        if right is None:
            reasons.append("TENCENT_SYMBOL_MISSING")
        if left is not None and right is not None:
            if left.trade_date != trade_date:
                reasons.append("SINA_DATE_MISMATCH")
            if right.trade_date != trade_date:
                reasons.append("TENCENT_DATE_MISMATCH")
            if time.fromisoformat(left.observed_time) < MINIMUM_FINAL_OBSERVED_TIME:
                reasons.append("SINA_INTRADAY_OBSERVATION")
            if time.fromisoformat(right.observed_time) < MINIMUM_FINAL_OBSERVED_TIME:
                reasons.append("TENCENT_INTRADAY_OBSERVATION")
            for field in ("open", "high", "low", "close", "previous_close"):
                if abs(getattr(left, field) - getattr(right, field)) > PRICE_TOLERANCE:
                    reasons.append(f"{field.upper()}_MISMATCH")
            if abs(left.amount_cny - right.amount_cny) > AMOUNT_TOLERANCE_CNY:
                reasons.append("AMOUNT_MISMATCH")
            volume_delta = abs(left.volume_shares - right.volume_shares)
            if volume_delta > VOLUME_TOLERANCE_SHARES:
                reasons.append("VOLUME_MISMATCH")
            if min(left.open, left.high, left.low, left.close) <= 0:
                reasons.append("SINA_NONPOSITIVE_PRICE")
            if min(right.open, right.high, right.low, right.close) <= 0:
                reasons.append("TENCENT_NONPOSITIVE_PRICE")
            if left.low > min(left.open, left.close) or left.high < max(left.open, left.close):
                reasons.append("SINA_OHLC_INVALID")
            if right.low > min(right.open, right.close) or right.high < max(right.open, right.close):
                reasons.append("TENCENT_OHLC_INVALID")
            if left.volume_shares < 0:
                reasons.append("SINA_NEGATIVE_VOLUME")
            if right.volume_shares < 0:
                reasons.append("TENCENT_NEGATIVE_VOLUME")
            if left.amount_cny < 0:
                reasons.append("SINA_NEGATIVE_AMOUNT")
            if right.amount_cny < 0:
                reasons.append("TENCENT_NEGATIVE_AMOUNT")

        if reasons:
            findings.append(
                {
                    "symbol": symbol,
                    "status": "FAIL_CLOSED",
                    "reasons": reasons,
                    "sina": asdict(left) if left else None,
                    "tencent": asdict(right) if right else None,
                }
            )
            continue

        assert left is not None and right is not None
        accepted.append(
            {
                "security_id": left.security_id,
                "market_prefix": symbol[:2],
                "trade_date": trade_date,
                "open": left.open,
                "high": left.high,
                "low": left.low,
                "close": left.close,
                "previous_close": left.previous_close,
                "volume_shares": left.volume_shares,
                "amount_cny": left.amount_cny,
                "sina_observed_time": left.observed_time,
                "tencent_observed_time": right.observed_time,
                "sina_source_marker": left.source_marker,
                "sina_source_marker_semantics": left.source_marker_semantics,
                "tencent_route_code": right.source_marker,
                "tencent_route_code_semantics": right.source_marker_semantics,
                "volume_cross_source_delta_shares": abs(
                    left.volume_shares - right.volume_shares
                ),
                "amount_cross_source_delta_cny": abs(
                    left.amount_cny - right.amount_cny
                ),
                "source_count": 2,
            }
        )
    return accepted, findings


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def boundary(status: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "status": status,
        "purpose_ceiling": PURPOSE_CEILING,
        "credentials_used": False,
        "paid_access_used": False,
        "formal_source_admission": False,
        "formal_catalog_written": False,
        "quality_promotion": False,
        "scheduler_created": False,
        "research_ready": False,
    }


def probe_policy() -> dict[str, Any]:
    return {
        "max_symbols": MAX_SYMBOLS,
        "logical_network_call_limit": 2,
        "redirects_allowed": False,
        "retries": 0,
        "same_day_finality": {
            "timezone": "Asia/Shanghai",
            "cutoff_local_time": SAME_DAY_FINALITY_CUTOFF.isoformat(),
            "minimum_final_observed_time": MINIMUM_FINAL_OBSERVED_TIME.isoformat(),
        },
        "source_requests": [
            {
                "source_id": "SINA_WEB_QUOTE",
                "scheme": "https",
                "host": "hq.sinajs.cn",
                "path": "/list",
            },
            {
                "source_id": "TENCENT_WEB_QUOTE",
                "scheme": "https",
                "host": "qt.gtimg.cn",
                "path": "/q",
            },
        ],
        "tolerances": {
            "price_absolute": PRICE_TOLERANCE,
            "amount_cny_absolute": AMOUNT_TOLERANCE_CNY,
            "volume_shares_absolute": VOLUME_TOLERANCE_SHARES,
        },
    }


def contract_binding() -> dict[str, Any]:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    expected = probe_policy()
    if contract.get("purpose_ceiling") != PURPOSE_CEILING:
        raise ProbeError("contract/script purpose ceiling mismatch")
    if contract.get("tolerances", {}).get("price_absolute") != PRICE_TOLERANCE:
        raise ProbeError("contract/script price tolerance mismatch")
    if (
        contract.get("tolerances", {}).get("amount_cny_absolute")
        != AMOUNT_TOLERANCE_CNY
    ):
        raise ProbeError("contract/script amount tolerance mismatch")
    if (
        contract.get("tolerances", {}).get("volume_shares_absolute")
        != VOLUME_TOLERANCE_SHARES
    ):
        raise ProbeError("contract/script volume tolerance mismatch")
    limits = contract.get("network_limits", {})
    if (
        limits.get("max_symbols") != MAX_SYMBOLS
        or limits.get("calls_per_source") != 1
        or limits.get("retries") != 0
        or limits.get("redirects_allowed") is not False
        or limits.get("credentials_forbidden") is not True
        or limits.get("paid_access_forbidden") is not True
    ):
        raise ProbeError("contract/script network policy mismatch")
    finality = contract.get("same_day_finality", {})
    if (
        finality.get("timezone") != "Asia/Shanghai"
        or finality.get("cutoff_local_time") != SAME_DAY_FINALITY_CUTOFF.isoformat()
        or finality.get("minimum_final_observed_time")
        != MINIMUM_FINAL_OBSERVED_TIME.isoformat()
    ):
        raise ProbeError("contract/script same-day finality mismatch")
    if contract.get("output_root") != "tmp/free-web-daily-probe/":
        raise ProbeError("contract/script output root mismatch")
    formal_effect = contract.get("formal_effect", {})
    if not formal_effect or any(value is not False for value in formal_effect.values()):
        raise ProbeError("contract must deny every formal effect")
    sources = {
        source.get("source_id"): source for source in contract.get("sources", [])
    }
    if (
        sources.get("TENCENT_WEB_QUOTE", {}).get("field_0_semantics")
        != "UNRESOLVED_ROUTE_CODE__NOT_TRADING_STATUS"
    ):
        raise ProbeError("Tencent field 0 must not be labeled as trading status")
    return {
        "contract_id": contract.get("contract_id"),
        "contract_path": CONTRACT_PATH.relative_to(ROOT).as_posix(),
        "contract_sha256": sha256_file(CONTRACT_PATH),
        "contract_readme_path": CONTRACT_README_PATH.relative_to(ROOT).as_posix(),
        "contract_readme_sha256": sha256_file(CONTRACT_README_PATH),
        "policy": expected,
        "policy_sha256": sha256_bytes(canonical_json(expected).encode("utf-8")),
    }


def execute(trade_date: str, symbols: list[str], output_root: Path) -> dict[str, Any]:
    trade_date = validate_trade_date(trade_date)
    symbols = validate_symbols(symbols)
    final_root = validate_output_root(output_root)
    finality_receipt = validate_same_day_finality(trade_date)
    binding = contract_binding()
    script_binding = {
        "path": Path(__file__).resolve().relative_to(ROOT).as_posix(),
        "sha256": sha256_file(Path(__file__).resolve()),
    }
    final_root.parent.mkdir(parents=True, exist_ok=True)
    final_root = validate_output_root(final_root)
    staging_root = final_root.parent / f".{final_root.name}.staging-{uuid4().hex}"
    staging_root.mkdir()
    try:
        result = _execute_into(
            trade_date,
            symbols,
            staging_root,
            finality_receipt=finality_receipt,
            binding=binding,
            script_binding=script_binding,
        )
        if final_root.exists():
            raise ProbeError("output directory appeared during execution")
        os.replace(staging_root, final_root)
        return result
    except Exception:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise


def _execute_into(
    trade_date: str,
    symbols: list[str],
    output_root: Path,
    *,
    finality_receipt: dict[str, str],
    binding: dict[str, Any],
    script_binding: dict[str, str],
) -> dict[str, Any]:
    raw_root = output_root / "raw"
    raw_root.mkdir()
    joined = ",".join(symbols)
    calls = [
        (
            "sina",
            f"https://hq.sinajs.cn/list={joined}",
            "https://finance.sina.com.cn/",
            parse_sina,
        ),
        (
            "tencent",
            f"https://qt.gtimg.cn/q={joined}",
            "https://gu.qq.com/",
            parse_tencent,
        ),
    ]
    parsed: dict[str, dict[str, Quote]] = {}
    captures: list[dict[str, Any]] = []
    network_call_count = 0
    for source, url, referer, parser in calls:
        network_call_count += 1
        payload, raw_metadata = request_once(url, referer=referer)
        metadata = validate_request_metadata(
            raw_metadata, requested_url=url, payload=payload
        )
        path = raw_root / f"{source}.response"
        path.write_bytes(payload)
        parsed[source] = parser(payload)
        captures.append(
            {
                "source_id": source.upper() + "_WEB_QUOTE",
                "logical_call_index": network_call_count,
                "request_url_redacted_to_host_and_symbol_count": {
                    "host": urlparse(url).hostname,
                    "symbol_count": len(symbols),
                },
                "response_file": path.relative_to(output_root).as_posix(),
                "sha256": sha256_bytes(payload),
                "row_count": len(parsed[source]),
                **metadata,
            }
        )

    accepted, findings = reconcile(
        symbols, trade_date, parsed["sina"], parsed["tencent"]
    )
    normalized_fields = [
        "security_id",
        "market_prefix",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "previous_close",
        "volume_shares",
        "amount_cny",
        "sina_observed_time",
        "tencent_observed_time",
        "sina_source_marker",
        "sina_source_marker_semantics",
        "tencent_route_code",
        "tencent_route_code_semantics",
        "volume_cross_source_delta_shares",
        "amount_cross_source_delta_cny",
        "source_count",
    ]
    normalized_path = output_root / "normalized" / "reconciled.csv"
    write_csv(normalized_path, accepted, normalized_fields)
    findings_path = output_root / "quality" / "findings.json"
    findings_path.parent.mkdir(parents=True)
    findings_path.write_text(
        json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if findings and not accepted:
        status = "FAIL_CLOSED"
    elif findings:
        status = "PARTIAL__SOME_SYMBOLS_FAIL_CLOSED"
    else:
        status = "PASS_BOUNDED_DUAL_SOURCE_INSPECTION_ONLY"
    result = {
        **boundary(status),
        "trade_date": trade_date,
        "requested_symbols": symbols,
        "requested_symbol_count": len(symbols),
        "accepted_row_count": len(accepted),
        "finding_count": len(findings),
        "network_call_count": network_call_count,
        "retry_count": 0,
        "redirect_count": sum(capture["redirect_count"] for capture in captures),
        "same_day_finality_receipt": finality_receipt,
        "policy_sha256": binding["policy_sha256"],
        "canonical_row_source": "SINA_WEB_QUOTE_AFTER_TENCENT_CROSS_CHECK",
        "known_limitations": [
            "UNOFFICIAL_UNDOCUMENTED_WEB_ENDPOINTS",
            "NO_SOURCE_RIGHTS_OR_RETENTION_DECISION",
            "NO_FULL_MARKET_COVERAGE_PROOF",
            "NO_SUSPENSION_OR_DELISTED_HISTORY_PROOF",
            "TENCENT_VOLUME_OBSERVED_AS_ROUNDED_BOARD_LOTS",
            "NO_PRODUCTION_DOWNLOADER_OR_SCHEDULER",
            "SOURCE_UPSTREAM_INDEPENDENCE_UNVERIFIED",
        ],
        "captures": captures,
    }
    results_path = output_root / "RESULTS.json"
    results_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    inventory = []
    for path in sorted(p for p in output_root.rglob("*") if p.is_file()):
        if path.name == "MANIFEST.json":
            continue
        inventory.append(
            {
                "path": path.relative_to(output_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = {
        **boundary(status),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_provenance": {
            "script": script_binding,
            "contract": binding,
            "probe_request": {
                "trade_date": trade_date,
                "symbols": symbols,
                "symbol_count": len(symbols),
            },
            "same_day_finality_receipt": finality_receipt,
            "request_metadata": captures,
        },
        "files": inventory,
    }
    (output_root / "MANIFEST.json").write_text(
        canonical_json(manifest) + "\n", encoding="utf-8"
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument(
        "--symbols",
        required=True,
        help="comma-separated symbols with sh/sz/bj prefixes",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        trade_date = validate_trade_date(args.trade_date)
        symbols = validate_symbols(args.symbols.split(","))
        output = validate_output_root(args.output)
        if not args.execute:
            print(
                json.dumps(
                    {
                        **boundary("PLAN"),
                        "trade_date": trade_date,
                        "symbols": symbols,
                        "output": str(output),
                        "network_call_limit": 2,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        result = execute(trade_date, symbols, output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"].startswith("PASS") else 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    **boundary("FAIL_CLOSED"),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=os.sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
