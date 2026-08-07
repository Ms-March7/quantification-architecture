"""Read-only selected-symbol history adapter for broker-authorized QMT clients."""

from __future__ import annotations

import importlib.util
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


def qmt_symbol(symbol: str) -> str:
    clean = str(symbol).strip().upper()
    if clean.endswith((".SH", ".SZ", ".BJ")):
        return clean
    if clean.startswith(("SH", "SZ", "BJ")):
        return f"{clean[2:]}.{clean[:2]}"
    if clean.startswith("920"):
        raise ValueError("920xxx exchange is ambiguous; pass an explicit .BJ suffix")
    if clean.startswith(("6", "5", "9")):
        return f"{clean}.SH"
    if clean.startswith(("0", "1", "2", "3")):
        return f"{clean}.SZ"
    if clean.startswith(("4", "8")):
        return f"{clean}.BJ"
    raise ValueError(f"cannot infer QMT exchange for {symbol!r}")


@dataclass(frozen=True)
class QmtProbe:
    package_installed: bool
    client_process_running: bool
    userdata_path_present: bool
    account_config_present: bool
    market_data_session: str
    history_permission: str
    live_order_permission: str
    message: str

    @property
    def installed(self) -> bool:
        return self.package_installed

    @property
    def connected(self) -> bool:
        return self.market_data_session == "CONFIRMED"

    @property
    def history_api(self) -> bool:
        return self.history_permission == "CONFIRMED"


class QmtMarketDataSource:
    """Download/read QMT market data without touching orders or account state.

    The broker's QMT/MiniQMT client must be installed, running, logged in, and
    authorized for the requested history/real-time products.  This class never
    accepts an account password and exposes no trading method.
    """

    def __init__(self) -> None:
        self._xtdata: Any = None

    def probe(
        self,
        *,
        userdata_path: str | Path | None = None,
        account_config_present: bool = False,
    ) -> QmtProbe:
        """Perform a static probe without importing xtquant or claiming a session."""

        installed = importlib.util.find_spec("xtquant") is not None
        process_running = False
        try:
            output = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            ).stdout.lower()
            process_running = any(token in output for token in ("miniqmt", "qmt", "xtmini"))
        except (OSError, subprocess.SubprocessError):
            process_running = False
        path_present = bool(userdata_path and Path(userdata_path).is_dir())
        return QmtProbe(
            package_installed=installed,
            client_process_running=process_running,
            userdata_path_present=path_present,
            account_config_present=bool(account_config_present),
            market_data_session="NOT_TESTED",
            history_permission="NOT_TESTED",
            live_order_permission="NOT_TESTED",
            message=(
                "static probe only; a fresh quote/readback is required before any session or permission is confirmed"
                if installed
                else "broker-provided xtquant package is not installed"
            ),
        )

    def _require(self) -> Any:
        if self._xtdata is None:
            try:
                from xtquant import xtdata
            except ImportError as exc:
                raise RuntimeError("broker-provided xtquant package is not installed") from exc
            self._xtdata = xtdata
        return self._xtdata

    def stock_list(self) -> tuple[str, ...]:
        xtdata = self._require()
        candidates: set[str] = set()
        for sector in ("沪深A股", "沪A", "深A", "北证A股"):
            try:
                candidates.update(str(item).upper() for item in (xtdata.get_stock_list_in_sector(sector) or []))
            except Exception:
                continue
        def is_a_share(item: str) -> bool:
            code, _, exchange = item.partition(".")
            if exchange == "SH":
                return code.startswith(("600", "601", "603", "605", "688", "689"))
            if exchange == "SZ":
                return code.startswith(("000", "001", "002", "003", "300", "301"))
            return exchange == "BJ" and code.isdigit()

        stocks = sorted(item for item in candidates if is_a_share(item))
        if not stocks:
            raise RuntimeError("QMT returned no A-share instruments; verify terminal login and quote permissions")
        return tuple(stocks)

    def download_history(
        self,
        symbols: Sequence[str],
        *,
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
        batch_size: int = 200,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> Mapping[str, Any]:
        xtdata = self._require()
        codes = [qmt_symbol(symbol) for symbol in symbols]
        if hasattr(xtdata, "download_history_data2"):
            for offset in range(0, len(codes), max(1, batch_size)):
                batch = codes[offset:offset + max(1, batch_size)]
                callback = progress if progress is not None else (lambda _: None)
                xtdata.download_history_data2(batch, period, start_time, end_time, callback)
        elif hasattr(xtdata, "download_history_data"):
            for index, code in enumerate(codes, start=1):
                xtdata.download_history_data(code, period, start_time, end_time)
                if progress:
                    progress({"finished": index, "total": len(codes), "stockcode": code})
        else:
            raise RuntimeError("installed xtquant build has no historical download API")
        return {
            "status": "DOWNLOAD_ATTEMPTED_UNVERIFIED",
            "requested_symbols": len(codes),
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "warning": "callback return is not completeness evidence; read back and validate every symbol before COMPLETE",
        }

    def read_history(
        self,
        symbols: Sequence[str],
        *,
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
        count: int = -1,
        dividend_type: str = "front_ratio",
        fill_data: bool = False,
    ) -> Mapping[str, Any]:
        xtdata = self._require()
        codes = [qmt_symbol(symbol) for symbol in symbols]
        return xtdata.get_market_data_ex(
            field_list=["time", "open", "high", "low", "close", "volume", "amount", "settlementPrice", "openInterest"],
            stock_list=codes,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=fill_data,
        ) or {}


def batched(items: Iterable[str], size: int) -> Iterable[tuple[str, ...]]:
    buffer: list[str] = []
    for item in items:
        buffer.append(item)
        if len(buffer) >= size:
            yield tuple(buffer)
            buffer.clear()
    if buffer:
        yield tuple(buffer)
