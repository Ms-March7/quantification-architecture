from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .utils import now_iso
from .validation import validate_security_master


@dataclass(frozen=True)
class AdapterResult:
    source_id: str
    family: str
    status: str
    fetched_at: str
    source_reference: str
    license_status: str
    raw: pd.DataFrame
    normalized: pd.DataFrame
    note: str = ""

    def manifest(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "family": self.family,
            "status": self.status,
            "fetched_at": self.fetched_at,
            "source_reference": self.source_reference,
            "license_status": self.license_status,
            "raw_rows": len(self.raw),
            "normalized_rows": len(self.normalized),
            "note": self.note,
            "credentials_persisted": False,
        }


SECURITY_MASTER_COLUMNS = [
    "symbol",
    "source_symbol",
    "name",
    "exchange",
    "security_type",
    "snapshot_date",
    "available_at",
    "ingested_at",
    "source_name",
    "source_reference",
    "evidence_state",
    "quality",
    "license_status",
]


def _empty_security_master() -> pd.DataFrame:
    return pd.DataFrame(columns=SECURITY_MASTER_COLUMNS)


def _canonical_a_symbol(code: Any) -> tuple[str, str]:
    text = str(code or "").strip().upper()
    if text.startswith(("SH", "SZ", "BJ")) and len(text) > 2:
        prefix, raw = text[:2], text[2:]
        exchange = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}[prefix]
        return f"{raw.zfill(6)}.{prefix}", exchange
    if "." in text:
        raw, suffix = text.rsplit(".", 1)
        exchange = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix, suffix)
        return f"{raw.zfill(6)}.{suffix}", exchange
    raw = text.zfill(6)
    if raw.startswith(("4", "8")):
        return f"{raw}.BJ", "BSE"
    if raw.startswith("6"):
        return f"{raw}.SH", "SSE"
    return f"{raw}.SZ", "SZSE"


def _pick_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    raise ValueError(f"none of the required columns exist: {candidates}")


class AKShareAdapter:
    source_id = "akshare"

    def __init__(self, module: Any | None = None) -> None:
        if module is None:
            import akshare as module

        self.module = module

    def security_master(self) -> AdapterResult:
        fetched_at = now_iso()
        raw: pd.DataFrame | None = None
        source_reference = ""
        failures: list[str] = []
        for function_name in ("stock_info_a_code_name", "stock_zh_a_spot_em", "stock_zh_a_spot"):
            function = getattr(self.module, function_name, None)
            if function is None:
                continue
            try:
                candidate = function().copy()
                if candidate.empty or len(candidate.columns) < 2:
                    raise ValueError("empty or underspecified frame")
                raw = candidate
                source_reference = function_name
                break
            except Exception as exc:
                failures.append(f"{function_name}:{type(exc).__name__}")
        if raw is None:
            raise RuntimeError(f"all AKShare security-master candidates failed: {','.join(failures)}")
        try:
            code_column = _pick_column(raw, ("code", "代码", "证券代码", "股票代码"))
            name_column = _pick_column(raw, ("name", "名称", "证券简称", "股票简称"))
        except ValueError:
            code_column, name_column = str(raw.columns[0]), str(raw.columns[1])
        rows: list[dict[str, Any]] = []
        for record in raw.to_dict(orient="records"):
            symbol, exchange = _canonical_a_symbol(record.get(code_column))
            rows.append(
                {
                    "symbol": symbol,
                    "source_symbol": str(record.get(code_column) or "").zfill(6),
                    "name": str(record.get(name_column) or "").strip(),
                    "exchange": exchange,
                    "security_type": "STOCK",
                    "snapshot_date": fetched_at[:10],
                    "available_at": fetched_at,
                    "ingested_at": fetched_at,
                    "source_name": "AKShare",
                    "source_reference": source_reference,
                    "evidence_state": "PROXY",
                    "quality": "OK",
                    "license_status": "UNVERIFIED",
                }
            )
        normalized = validate_security_master(pd.DataFrame(rows, columns=SECURITY_MASTER_COLUMNS))
        return AdapterResult(
            source_id=self.source_id,
            family="security_master",
            status="partial" if not normalized.empty else "missing",
            fetched_at=fetched_at,
            source_reference=f"akshare.{source_reference}",
            license_status="UNVERIFIED",
            raw=raw,
            normalized=normalized,
            note=(
                "Current aggregator snapshot for cross-check only; not a PIT official security master."
                + (f" Fallbacks used after {','.join(failures)}." if failures else "")
            ),
        )


class TushareAdapter:
    source_id = "tushare_pro"

    def __init__(
        self,
        token: str | None = None,
        module: Any | None = None,
        client: Any | None = None,
    ) -> None:
        self.token = token if token is not None else os.environ.get("TUSHARE_TOKEN")
        self.module = module
        self.client = client

    def security_master(self) -> AdapterResult:
        fetched_at = now_iso()
        if not self.token and self.client is None:
            empty = _empty_security_master()
            return AdapterResult(
                source_id=self.source_id,
                family="security_master",
                status="SKIP",
                fetched_at=fetched_at,
                source_reference="tushare.pro.stock_basic",
                license_status="RESTRICTED",
                raw=pd.DataFrame(),
                normalized=empty,
                note="TUSHARE_TOKEN is not configured; no API request was made.",
            )
        client = self.client
        if client is None:
            module = self.module
            if module is None:
                import tushare as module

            client = module.pro_api(self.token)
        raw = client.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,area,industry,market,list_date",
        ).copy()
        code_column = _pick_column(raw, ("ts_code", "symbol"))
        name_column = _pick_column(raw, ("name",))
        rows: list[dict[str, Any]] = []
        for record in raw.to_dict(orient="records"):
            symbol, exchange = _canonical_a_symbol(record.get(code_column))
            rows.append(
                {
                    "symbol": symbol,
                    "source_symbol": str(record.get(code_column) or ""),
                    "name": str(record.get(name_column) or "").strip(),
                    "exchange": exchange,
                    "security_type": "STOCK",
                    "snapshot_date": fetched_at[:10],
                    "available_at": fetched_at,
                    "ingested_at": fetched_at,
                    "source_name": "Tushare Pro",
                    "source_reference": "pro.stock_basic",
                    "evidence_state": "PROXY",
                    "quality": "OK",
                    "license_status": "RESTRICTED",
                }
            )
        normalized = validate_security_master(pd.DataFrame(rows, columns=SECURITY_MASTER_COLUMNS))
        return AdapterResult(
            source_id=self.source_id,
            family="security_master",
            status="partial" if not normalized.empty else "missing",
            fetched_at=fetched_at,
            source_reference="tushare.pro.stock_basic",
            license_status="RESTRICTED",
            raw=raw,
            normalized=normalized,
            note="Authorized secondary snapshot for backfill/cross-check; never canonical over official disclosures.",
        )
