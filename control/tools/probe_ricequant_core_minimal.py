from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from numbers import Number
from typing import Any

from probe_ricequant_environment import validate_output_path, write_json_atomic


ORDER_BOOK_ID = "603986.XSHG"
START_DATE = "2026-07-31"
END_DATE = "2026-08-03"
DEFAULT_OUTPUT = "live-core-minimal-20260803-v1/result.json"
SECRET_KEY_PARTS = (
    "account",
    "conf",
    "credential",
    "license",
    "password",
    "secret",
    "token",
    "uri",
    "user",
)


def _safe_error(exc: Exception) -> dict[str, str]:
    message = str(exc)
    message = re.sub(r"(?:tcp|rqdata|rqdatac)://\S+", "<redacted>", message)
    return {"type": type(exc).__name__, "message": message[:300]}


def _numeric_quota_summary(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in SECRET_KEY_PARTS):
                continue
            cleaned = _numeric_quota_summary(item)
            if cleaned is not None:
                result[key_text] = cleaned
        return result
    if isinstance(value, Number) and not isinstance(value, complex):
        return value
    if isinstance(value, (list, tuple)):
        cleaned_items = [_numeric_quota_summary(item) for item in value]
        return [item for item in cleaned_items if item is not None]
    return None


def _instrument_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return {"returned": False}
    fields = (
        "order_book_id",
        "symbol",
        "type",
        "listed_date",
        "de_listed_date",
        "board_type",
        "exchange",
        "status",
    )
    summary: dict[str, Any] = {"returned": True}
    for field in fields:
        item = getattr(value, field, None)
        if item is not None:
            summary[field] = str(item)
    return summary


def _record_check(payload: dict[str, Any], name: str, function: Any) -> None:
    try:
        payload["checks"][name] = {"status": "PASS", **function()}
    except Exception as exc:  # capability probe must preserve partial evidence
        payload["checks"][name] = {"status": "ERROR", "error": _safe_error(exc)}


def build_live_payload() -> dict[str, Any]:
    import rqdatac

    payload: dict[str, Any] = {
        "schema_version": 1,
        "probe_id": "ricequant-core-minimal-live-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "ONE_SECURITY__TWO_TRADING_DATES__SUMMARY_ONLY",
        "formal_effect": "NONE",
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_admission": False,
        "bulk_download": False,
        "license_value_captured": False,
        "maximum_business_api_calls": 4,
        "checks": {},
    }

    try:
        rqdatac.init()
        payload["initialization"] = "PASS"
    except Exception as exc:
        payload["initialization"] = "ERROR"
        payload["initialization_error"] = _safe_error(exc)
        payload["status"] = "INITIALIZATION_FAILED__NO_DATA_REQUESTED"
        return payload

    _record_check(
        payload,
        "instrument_identity_current",
        lambda: _instrument_summary(rqdatac.instruments(ORDER_BOOK_ID)),
    )
    _record_check(
        payload,
        "trading_calendar",
        lambda: {
            "count": len(rqdatac.get_trading_dates(START_DATE, END_DATE)),
            "start_date": START_DATE,
            "end_date": END_DATE,
        },
    )

    def price_summary() -> dict[str, Any]:
        frame = rqdatac.get_price(
            ORDER_BOOK_ID,
            start_date=START_DATE,
            end_date=END_DATE,
            frequency="1d",
            adjust_type="none",
        )
        return {
            "rows": 0 if frame is None else int(len(frame)),
            "columns": [] if frame is None else [str(item) for item in frame.columns],
            "adjust_type_requested": "none",
        }

    _record_check(payload, "daily_price_unadjusted", price_summary)
    _record_check(
        payload,
        "quota_numeric_summary",
        lambda: {"numeric_fields": _numeric_quota_summary(rqdatac.user.get_quota())},
    )

    statuses = [item["status"] for item in payload["checks"].values()]
    payload["status"] = (
        "BOUNDED_PASS__SUMMARY_ONLY__FORMAL_EFFECT_NONE"
        if statuses and all(status == "PASS" for status in statuses)
        else "PARTIAL__ONE_OR_MORE_CAPABILITIES_UNAVAILABLE__FORMAL_EFFECT_NONE"
    )
    return payload


def offline_check() -> dict[str, Any]:
    import rqdatac

    names = (
        "instruments",
        "get_trading_dates",
        "get_price",
    )
    presence = {name: callable(getattr(rqdatac, name, None)) for name in names}
    presence["user.get_quota"] = callable(
        getattr(getattr(rqdatac, "user", None), "get_quota", None)
    )
    return {
        "status": "OFFLINE_ENTRYPOINT_CHECK_PASS"
        if all(presence.values())
        else "OFFLINE_ENTRYPOINT_CHECK_FAIL",
        "network_requests": 0,
        "license_value_captured": False,
        "entrypoints": presence,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a bounded RQData CORE capability probe without bulk data."
    )
    parser.add_argument("--offline-check", action="store_true")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = offline_check() if args.offline_check else build_live_payload()
    if not args.offline_check:
        output_path = validate_output_path(args.output)
        write_json_atomic(output_path, payload)
        payload["output_path"] = str(output_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if "FAIL" not in str(payload.get("status", "")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
