from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from probe_ricequant_environment import validate_output_path, write_json_atomic
from probe_ricequant_r0_closure import numeric_summary, safe_error as base_safe_error


CONTRACT_ID = "ricequant-r1-market-reference-probe-v1"
DEFAULT_OUTPUT = "live-r1-market-reference-20260804-v1/result.json"
MAX_CAPABILITY_CALLS = 9
MAX_RETURNED_ROWS_PER_CALL = 2000
MAX_TOTAL_RETURNED_ROWS = 4000
STOCK_ID = "603986.XSHG"
INDEX_ID = "000300.XSHG"
START_DATE = "2026-07-28"
END_DATE = "2026-08-03"
INDEX_ANCHOR_DATE = "2026-08-03"
INDUSTRY_SOURCE = "citics_2019"
INDUSTRY_LABEL = "银行"
INDUSTRY_LEVEL = 1
YIELD_TENOR = "1Y"
EXPOSURE_FACTORS = ("beta", "size", "momentum")
CAPABILITY_NAMES = (
    "core07_shares",
    "core08_turnover",
    "core10_index_components",
    "core10_index_weights",
    "core11_industry_change",
    "core11_industry_mapping",
    "core12_index_daily",
    "core19_yield_curve",
    "core20_factor_exposure",
)


def safe_error(exc: Exception) -> dict[str, str]:
    result = base_safe_error(exc)
    result["message"] = re.sub(
        r"(?i)\b(license|token|password|secret|api[_-]?key)\b\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        result["message"],
    )[:300]
    return result


def _scalar_text(value: Any) -> str | None:
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _canonical_object(value: Any) -> Any:
    import pandas as pd

    if isinstance(value, pd.Series):
        value = value.to_frame(name=str(value.name or "value"))
    if isinstance(value, pd.DataFrame):
        frame = value.reset_index()
        frame.columns = [str(item) for item in frame.columns]
        ordered = sorted(range(len(frame.columns)), key=lambda idx: (frame.columns[idx], idx))
        columns = [frame.columns[idx] for idx in ordered]
        rows = [
            [_scalar_text(row[idx]) for idx in ordered]
            for row in frame.itertuples(index=False, name=None)
        ]
        rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return {"columns": columns, "rows": rows}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_object(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set)):
        items = [_canonical_object(item) for item in value]
        if isinstance(value, set):
            items.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
        return items
    return _scalar_text(value)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        _canonical_object(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _date_values(value: Any) -> set[str]:
    import pandas as pd

    if isinstance(value, pd.Series):
        value = value.to_frame(name=str(value.name or "value"))
    candidates: list[Any] = []
    if isinstance(value, pd.DataFrame):
        for level in range(value.index.nlevels):
            name = str(value.index.names[level] or "").lower()
            level_values = value.index.get_level_values(level)
            inferred = pd.api.types.infer_dtype(level_values, skipna=True)
            if "date" in name or "time" in name or inferred in {
                "date",
                "datetime",
                "datetime64",
            }:
                candidates.extend(level_values.tolist())
        for column in value.columns:
            name = str(column).lower()
            if "date" in name or "time" in name:
                candidates.extend(value[column].tolist())
    elif isinstance(value, tuple) and len(value) == 2:
        candidates.append(value[1])
    result: set[str] = set()
    for item in candidates:
        converted = pd.to_datetime(item, errors="coerce")
        if not pd.isna(converted):
            result.add(converted.isoformat())
    return result


def _coverage(values: set[str]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "distinct_dates_or_timestamps": len(ordered),
        "min": ordered[0] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


def _frame_fields(value: Any) -> tuple[list[str], list[str]]:
    import pandas as pd

    if isinstance(value, pd.Series):
        return [str(value.name or "value")], [str(item or "") for item in value.index.names]
    if isinstance(value, pd.DataFrame):
        return [str(item) for item in value.columns], [str(item or "") for item in value.index.names]
    return [], []


def frame_summary(value: Any) -> dict[str, Any]:
    import pandas as pd

    if value is None:
        return {
            "returned": False,
            "rows": 0,
            "fields": [],
            "index_fields": [],
            "date_coverage": _coverage(set()),
            "canonical_sha256": canonical_sha256(None),
        }
    if isinstance(value, (pd.DataFrame, pd.Series)):
        fields, index_fields = _frame_fields(value)
        return {
            "returned": True,
            "rows": int(len(value)),
            "fields": fields,
            "index_fields": index_fields,
            "date_coverage": _coverage(_date_values(value)),
            "canonical_sha256": canonical_sha256(value),
        }
    return {
        "returned": True,
        "rows": 1,
        "fields": [],
        "index_fields": [],
        "date_coverage": _coverage(_date_values(value)),
        "canonical_sha256": canonical_sha256(value),
    }


def component_summary(value: Any) -> dict[str, Any]:
    components: Any = value
    fields = ["order_book_id"]
    if isinstance(value, tuple) and len(value) == 2:
        components = value[0]
        fields.append("rice_create_tm")
    rows = len(components) if isinstance(components, (list, tuple, set)) else 0
    return {
        "returned": value is not None,
        "rows": int(rows),
        "fields": fields,
        "index_fields": [],
        "date_coverage": _coverage(_date_values(value)),
        "canonical_sha256": canonical_sha256(value),
    }


def _is_permission_denied(exc: Exception) -> bool:
    return type(exc).__name__ == "PermissionDenied" or "permission denied" in str(exc).lower()


class BoundedProbeRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.total_rows = 0
        self.halted = False
        self.halt_reason: str | None = None
        self.results: dict[str, dict[str, Any]] = {}

    def call(
        self,
        name: str,
        function: Callable[[], Any],
        summarizer: Callable[[Any], dict[str, Any]] = frame_summary,
    ) -> Any:
        if name not in CAPABILITY_NAMES:
            raise ValueError(f"uncontracted capability name: {name}")
        if self.halted:
            self.results[name] = {
                "status": "SKIPPED_AFTER_HALT",
                "permission": "NOT_TESTED",
                "halt_reason": self.halt_reason,
            }
            return None
        if self.calls >= MAX_CAPABILITY_CALLS:
            raise RuntimeError("capability API call budget exceeded")
        self.calls += 1
        try:
            value = function()
            summary = summarizer(value)
            rows = int(summary.get("rows", 0))
            if rows > MAX_RETURNED_ROWS_PER_CALL or self.total_rows + rows > MAX_TOTAL_RETURNED_ROWS:
                self.halted = True
                self.halt_reason = "RETURNED_ROW_BOUND_EXCEEDED"
                self.results[name] = {
                    "status": "HALTED_RETURNED_ROW_BOUND_EXCEEDED",
                    "permission": "ALLOWED",
                    **summary,
                }
                return None
            self.total_rows += rows
            self.results[name] = {
                "status": "PASS_EMPTY" if not summary.get("returned") or rows == 0 else "PASS",
                "permission": "ALLOWED",
                **summary,
            }
            return value
        except Exception as exc:
            if _is_permission_denied(exc):
                self.results[name] = {
                    "status": "PERMISSION_DENIED",
                    "permission": "DENIED",
                    "error": safe_error(exc),
                }
                return None
            self.halted = True
            self.halt_reason = f"UNEXPECTED_{type(exc).__name__}"
            self.results[name] = {
                "status": "ERROR_HALTED",
                "permission": "UNKNOWN",
                "error": safe_error(exc),
            }
            return None


def _date_set(value: Any) -> set[str]:
    return {item[:10] for item in _date_values(value)}


def _component_ids(value: Any) -> set[str]:
    if isinstance(value, tuple) and len(value) == 2:
        value = value[0]
    return set(map(str, value)) if isinstance(value, (list, tuple, set)) else set()


def _index_ids(value: Any) -> set[str]:
    import pandas as pd

    if isinstance(value, (pd.Series, pd.DataFrame)):
        result: set[str] = set()
        for level in range(value.index.nlevels):
            values = {str(item) for item in value.index.get_level_values(level)}
            matched = {item for item in values if item.endswith((".XSHG", ".XSHE", ".BJSE"))}
            result.update(matched)
        return result
    return set()


def alignment_summary(left: set[str], right: set[str]) -> dict[str, Any]:
    union = left | right
    return {
        "left_count": len(left),
        "right_count": len(right),
        "intersection_count": len(left & right),
        "left_only_count": len(left - right),
        "right_only_count": len(right - left),
        "jaccard": None if not union else round(len(left & right) / len(union), 8),
    }


def _returned_fields(value: Any) -> set[str]:
    return set(_frame_fields(value)[0])


def quota_snapshot() -> dict[str, Any]:
    import rqdatac

    payload: dict[str, Any] = {
        "schema_version": 1,
        "probe_id": "ricequant-r1-market-reference-quota-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "contract_id": CONTRACT_ID,
        "credential_value_captured": False,
        "api_payload_persisted": False,
        "business_api_calls_made": 0,
    }
    try:
        rqdatac.init(enable_bjse=True)
        payload["initialization"] = "PASS"
        numeric = numeric_summary(rqdatac.user.get_quota())
        payload["business_api_calls_made"] = 1
        payload["quota_numeric_fields"] = numeric
        payload["status"] = "NUMERIC_QUOTA_SUMMARY_PASS"
    except Exception as exc:
        payload["initialization"] = "ERROR"
        payload["error"] = safe_error(exc)
        payload["status"] = "NUMERIC_QUOTA_SUMMARY_ERROR"
    return payload


def build_live_payload() -> dict[str, Any]:
    import rqdatac

    payload: dict[str, Any] = {
        "schema_version": 1,
        "probe_id": "ricequant-r1-market-reference-live-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "contract_id": CONTRACT_ID,
        "scope": "CORE07_08_10_11_12_19_20__SUMMARY_ONLY__NO_PAYLOAD_PERSISTENCE",
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_effect": "NONE",
        "formal_admission": False,
        "formal_catalog_write": False,
        "quality_promotion": False,
        "research_use": False,
        "bulk_download": False,
        "credential_value_captured": False,
        "api_payload_persisted": False,
        "maximum_capability_api_calls": MAX_CAPABILITY_CALLS,
    }
    try:
        rqdatac.init(enable_bjse=True)
        payload["initialization"] = "PASS"
    except Exception as exc:
        payload.update(
            {
                "initialization": "ERROR",
                "initialization_error": safe_error(exc),
                "business_api_calls_made": 0,
                "status": "INITIALIZATION_FAILED__NO_DATA_REQUESTED",
                "r1_gate_status": "NOT_MET",
            }
        )
        return payload

    runner = BoundedProbeRunner()
    shares = runner.call(
        "core07_shares",
        lambda: rqdatac.get_shares(
            STOCK_ID,
            start_date=START_DATE,
            end_date=END_DATE,
            fields=["total", "circulation_a", "free_circulation"],
        ),
    )
    turnover = runner.call(
        "core08_turnover",
        lambda: rqdatac.get_turnover_rate(
            STOCK_ID,
            start_date=START_DATE,
            end_date=END_DATE,
            fields=["today"],
        ),
    )
    components = runner.call(
        "core10_index_components",
        lambda: rqdatac.index_components(
            INDEX_ID,
            date=INDEX_ANCHOR_DATE,
            return_create_tm=True,
        ),
        component_summary,
    )
    weights = runner.call(
        "core10_index_weights",
        lambda: rqdatac.index_weights(INDEX_ID, date=INDEX_ANCHOR_DATE),
    )
    industry_change = runner.call(
        "core11_industry_change",
        lambda: rqdatac.get_industry_change(
            INDUSTRY_LABEL,
            source=INDUSTRY_SOURCE,
            level=INDUSTRY_LEVEL,
        ),
    )
    industry_mapping = runner.call(
        "core11_industry_mapping",
        lambda: rqdatac.get_industry_mapping(
            source=INDUSTRY_SOURCE,
            date=INDEX_ANCHOR_DATE,
        ),
    )
    index_daily = runner.call(
        "core12_index_daily",
        lambda: rqdatac.get_price(
            INDEX_ID,
            start_date=START_DATE,
            end_date=END_DATE,
            frequency="1d",
            fields=["open", "close", "volume", "total_turnover"],
            adjust_type="none",
            skip_suspended=False,
        ),
    )
    yield_curve = runner.call(
        "core19_yield_curve",
        lambda: rqdatac.get_yield_curve(
            start_date=START_DATE,
            end_date=END_DATE,
            tenor=YIELD_TENOR,
        ),
    )
    exposure = runner.call(
        "core20_factor_exposure",
        lambda: rqdatac.get_factor_exposure(
            STOCK_ID,
            start_date=START_DATE,
            end_date=END_DATE,
            factors=list(EXPOSURE_FACTORS),
            industry_mapping="sws_2021",
            model="v1",
        ),
    )

    exposure_fields = _returned_fields(exposure)
    payload.update(
        {
            "checks": runner.results,
            "business_api_calls_made": runner.calls,
            "returned_rows_total": runner.total_rows,
            "halted": runner.halted,
            "halt_reason": runner.halt_reason,
            "difference_summary": {
                "shares_vs_turnover_dates": alignment_summary(
                    _date_set(shares), _date_set(turnover)
                ),
                "components_vs_weights_ids": alignment_summary(
                    _component_ids(components), _index_ids(weights)
                ),
                "requested_vs_returned_exposure_fields": {
                    "requested_count": len(EXPOSURE_FACTORS),
                    "returned_count": len(exposure_fields),
                    "missing_count": len(set(EXPOSURE_FACTORS) - exposure_fields),
                    "extra_count": len(exposure_fields - set(EXPOSURE_FACTORS)),
                },
                "industry_change_vs_mapping": {
                    "change_rows": 0 if industry_change is None else int(len(industry_change)),
                    "mapping_rows": 0 if industry_mapping is None else int(len(industry_mapping)),
                    "shared_field_count": len(
                        _returned_fields(industry_change) & _returned_fields(industry_mapping)
                    ),
                },
                "index_daily_returned_date_count": len(_date_set(index_daily)),
                "yield_curve_returned_date_count": len(_date_set(yield_curve)),
            },
            "formal_limits": [
                "BOUNDED_RESPONSE_IS_NOT_COMPLETE_HISTORY_OR_STABILITY_PROOF",
                "RICE_CREATE_TM_IS_NOT_ASSUMED_LEGAL_AVAILABLE_AT",
                "RETENTION_DERIVATION_AND_IMMUTABLE_EXPORT_RIGHTS_UNVERIFIED",
                "VENDOR_RISK_EXPOSURE_IS_CALIBRATION_ONLY_NOT_INTERNAL_CORE20",
                "FORMAL_CORE_SNAPSHOTS_AND_L2_SAFETY_GATES_ABSENT",
            ],
            "r1_gate_status": "NOT_MET__FORMAL_SNAPSHOTS_RIGHTS_PIT_AND_L2_GATES_ABSENT",
        }
    )
    statuses = [item["status"] for item in runner.results.values()]
    payload["status"] = (
        "BOUNDED_R1_MARKET_REFERENCE_CAPABILITY_PROBE_COMPLETE__FORMAL_EFFECT_NONE"
        if runner.calls == MAX_CAPABILITY_CALLS
        and all(status in {"PASS", "PASS_EMPTY", "PERMISSION_DENIED"} for status in statuses)
        else "PARTIAL__FAIL_STOP_TRIGGERED_OR_CALL_PLAN_INCOMPLETE__FORMAL_EFFECT_NONE"
    )
    return payload


def offline_check() -> dict[str, Any]:
    import rqdatac

    names = (
        "get_shares",
        "get_turnover_rate",
        "index_components",
        "index_weights",
        "get_industry_change",
        "get_industry_mapping",
        "get_price",
        "get_yield_curve",
        "get_factor_exposure",
    )
    entrypoints = {name: callable(getattr(rqdatac, name, None)) for name in names}
    entrypoints["user.get_quota"] = callable(
        getattr(getattr(rqdatac, "user", None), "get_quota", None)
    )
    return {
        "status": "OFFLINE_ENTRYPOINT_CHECK_PASS"
        if all(entrypoints.values())
        else "OFFLINE_ENTRYPOINT_CHECK_FAIL",
        "network_requests": 0,
        "business_api_calls": 0,
        "credential_value_captured": False,
        "entrypoints": entrypoints,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe bounded Ricequant R1 market/reference endpoint capabilities."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline-check", action="store_true")
    mode.add_argument("--quota-only", action="store_true")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.offline_check:
        payload = offline_check()
    elif args.quota_only:
        payload = quota_snapshot()
    else:
        payload = build_live_payload()
    if not args.offline_check:
        output_path = validate_output_path(args.output)
        payload["output_path"] = str(output_path)
        write_json_atomic(output_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if args.offline_check:
        return 0 if payload.get("status") == "OFFLINE_ENTRYPOINT_CHECK_PASS" else 2
    return 0 if "ERROR" not in str(payload.get("status", "")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
