from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ID = "ricequant-r0-core04-core05-candidate-v1"
CANDIDATE_BASE = (
    ROOT / "data" / "market" / "quarantine" / "ricequant-r0-core04-core05"
)
DEFAULT_OUTPUT = CANDIDATE_BASE / "candidate-20260804-v1"
START_DATE = "2005-01-01"
BASE_END = "2024-12-31"
TAIL_START = "2025-01-01"
END_DATE = "2026-08-03"
HARD_LIMIT_BYTES = 40 * 1024 * 1024
SOFT_STOP_BYTES = 36 * 1024 * 1024
SAMPLE_SIZE = 32
MAX_BATCH_SIZE = 256
ENDPOINTS = ("dividend", "split", "allotment", "ex_factor")
SECRET_PATTERN = re.compile(
    r"(?i)(tcp://|rqdatad-pro|password|api[_-]?key|bearer\s+|license\s*[:=])"
)


class CandidateError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_bytes_atomic(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
    )


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    encoded = b"".join((canonical_json(row) + "\n").encode("utf-8") for row in rows)
    write_bytes_atomic(path, encoded)


def validate_output_root(path: Path) -> Path:
    base = Path(os.path.abspath(CANDIDATE_BASE))
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise CandidateError("output root must stay below the Ricequant quarantine root") from exc
    if len(relative.parts) != 1 or not relative.name.startswith("candidate-"):
        raise CandidateError("output root must be one explicit candidate directory")
    current = base
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise CandidateError("symlink output components are forbidden")
    return target


def safe_error(exc: Exception) -> dict[str, str]:
    message = str(exc).replace("\r", " ").replace("\n", " ")[:300]
    if SECRET_PATTERN.search(message):
        message = "redacted sensitive-looking exception text"
    return {"type": type(exc).__name__, "message": message}


def quota_bytes(value: Any) -> int:
    if not isinstance(value, dict):
        raise CandidateError("quota response is not a mapping")
    raw = value.get("bytes_used")
    if not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
        raise CandidateError("quota bytes_used is missing or non-numeric")
    result = int(raw)
    if result < 0:
        raise CandidateError("quota bytes_used is negative")
    return result


def _clean_value(value: Any) -> Any:
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _date_text(value: Any) -> str | None:
    clean = _clean_value(value)
    if clean is None:
        return None
    text = str(clean)[:10]
    if text in {"", "NaT", "None", "nan", "0000-00-00"}:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed.isoformat()


def _float_value(value: Any) -> float | None:
    clean = _clean_value(value)
    if clean is None:
        return None
    try:
        result = float(clean)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def frame_records(value: Any) -> list[dict[str, Any]]:
    import pandas as pd

    if not isinstance(value, pd.DataFrame) or value.empty:
        return []
    frame = value.reset_index()
    frame.columns = [str(item) for item in frame.columns]
    return [
        {str(key): _clean_value(item) for key, item in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _common_row(
    raw: dict[str, Any],
    endpoint: str,
    event_type: str,
    event_date: str | None,
    announcement_date: str | None,
    registration_date: str | None,
    ex_date: str | None,
) -> dict[str, Any] | None:
    security_id = str(raw.get("order_book_id") or "")
    if not security_id.endswith((".XSHG", ".XSHE")) or event_date is None:
        return None
    if event_date < START_DATE or event_date > END_DATE:
        return None
    return {
        "schema_version": 1,
        "source_id": "RQDATA",
        "source_endpoint": endpoint,
        "security_id": security_id,
        "local_security_code": security_id.split(".", 1)[0],
        "exchange": security_id.rsplit(".", 1)[-1],
        "event_type": event_type,
        "event_date": event_date,
        "announcement_date": announcement_date,
        "registration_date": registration_date,
        "ex_date": ex_date,
        "segment": "BASE_2005_2024" if event_date <= BASE_END else "TAIL_2025_20260803",
        "source_row_sha256": sha256_bytes(canonical_json(raw).encode("utf-8")),
        "research_eligible": False,
    }


def normalize_frame(endpoint: str, value: Any) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    rejected = 0
    for raw in frame_records(value):
        if endpoint == "dividend":
            event_date = _date_text(raw.get("ex_dividend_date"))
            row = _common_row(
                raw,
                endpoint,
                "DIVIDEND",
                event_date,
                _date_text(raw.get("declaration_announcement_date")),
                _date_text(raw.get("book_closure_date")),
                event_date,
            )
            if row is not None:
                cash = _float_value(raw.get("dividend_cash_before_tax"))
                round_lot = _float_value(raw.get("round_lot"))
                row.update(
                    {
                        "payment_or_listing_date": _date_text(raw.get("payable_date")),
                        "report_quarter": raw.get("quarter"),
                        "cash_before_tax": cash,
                        "round_lot": round_lot,
                        "cash_per_share": (
                            cash / round_lot
                            if cash is not None and round_lot not in {None, 0.0}
                            else None
                        ),
                    }
                )
        elif endpoint == "split":
            event_date = _date_text(raw.get("ex_dividend_date"))
            row = _common_row(
                raw,
                endpoint,
                "SPLIT",
                event_date,
                None,
                _date_text(raw.get("book_closure_date")),
                event_date,
            )
            if row is not None:
                coefficient_from = _float_value(raw.get("split_coefficient_from"))
                coefficient_to = _float_value(raw.get("split_coefficient_to"))
                row.update(
                    {
                        "payment_or_listing_date": _date_text(raw.get("payable_date")),
                        "split_coefficient_from": coefficient_from,
                        "split_coefficient_to": coefficient_to,
                        "split_increment": (
                            coefficient_to / coefficient_from - 1.0
                            if coefficient_to is not None
                            and coefficient_from not in {None, 0.0}
                            else None
                        ),
                        "split_cumulative_factor": _float_value(raw.get("cum_factor")),
                    }
                )
        elif endpoint == "allotment":
            event_date = _date_text(raw.get("ex_right_date"))
            row = _common_row(
                raw,
                endpoint,
                "ALLOTMENT",
                event_date,
                _date_text(raw.get("declaration_announcement_date")),
                _date_text(raw.get("book_closure_date")),
                event_date,
            )
            if row is not None:
                row.update(
                    {
                        "payment_or_listing_date": None,
                        "allotment_proportion": _float_value(raw.get("proportion")),
                        "allotted_proportion": _float_value(raw.get("allotted_proportion")),
                        "allotted_shares": _float_value(raw.get("allotted_shares")),
                        "allotment_price": _float_value(raw.get("allotment_price")),
                        "stock_type": raw.get("stock_type"),
                    }
                )
        elif endpoint == "ex_factor":
            event_date = _date_text(raw.get("ex_date"))
            row = _common_row(
                raw,
                endpoint,
                "ADJUSTMENT_FACTOR",
                event_date,
                None,
                _date_text(raw.get("announcement_date")),
                event_date,
            )
            if row is not None:
                row.update(
                    {
                        "payment_or_listing_date": None,
                        "factor_period_end_date": _date_text(raw.get("ex_end_date")),
                        "ex_factor": _float_value(raw.get("ex_factor")),
                        "ex_cum_factor": _float_value(raw.get("ex_cum_factor")),
                    }
                )
        else:
            raise CandidateError(f"unsupported endpoint: {endpoint}")
        if row is None:
            rejected += 1
        else:
            rows.append(row)
    rows.sort(
        key=lambda item: (
            item["security_id"],
            item["event_date"],
            item["event_type"],
            item["source_row_sha256"],
        )
    )
    return rows, rejected


def stable_sample_key(order_book_id: str) -> str:
    return sha256_bytes(f"rqdata-r0-action-sample-v1|{order_book_id}".encode("ascii"))


def build_batches(order_book_ids: Iterable[str]) -> list[list[str]]:
    universe = sorted(set(order_book_ids))
    if not universe:
        raise CandidateError("empty input universe")
    sample = sorted(universe, key=stable_sample_key)[: min(SAMPLE_SIZE, len(universe))]
    sample_set = set(sample)
    remainder = [item for item in universe if item not in sample_set]
    batches = [sample]
    batches.extend(
        remainder[offset : offset + MAX_BATCH_SIZE]
        for offset in range(0, len(remainder), MAX_BATCH_SIZE)
    )
    flattened = [item for batch in batches for item in batch]
    if len(flattened) != len(universe) or set(flattened) != set(universe):
        raise CandidateError("batch plan does not cover the universe exactly once")
    return batches


def _endpoint_call(rqdatac: Any, endpoint: str, ids: list[str]) -> Any:
    if endpoint == "dividend":
        return rqdatac.get_dividend(ids, START_DATE, END_DATE, adjusted=False)
    if endpoint == "split":
        return rqdatac.get_split(ids, START_DATE, END_DATE)
    if endpoint == "allotment":
        return rqdatac.get_allotment(ids, START_DATE, END_DATE)
    if endpoint == "ex_factor":
        return rqdatac.get_ex_factor(ids, START_DATE, END_DATE)
    raise CandidateError(f"unsupported endpoint: {endpoint}")


def _load_universe(output_root: Path) -> tuple[dict[str, Any], list[str]]:
    path = output_root / "INPUT_UNIVERSE.json"
    if not path.is_file():
        raise CandidateError("INPUT_UNIVERSE.json must be prepared first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    ids = payload.get("order_book_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
        raise CandidateError("invalid order_book_ids in INPUT_UNIVERSE.json")
    expected = payload.get("order_book_ids_sha256")
    actual = sha256_bytes(("\n".join(ids) + "\n").encode("ascii"))
    if expected != actual:
        raise CandidateError("input universe hash mismatch")
    if any(not item.endswith((".XSHG", ".XSHE")) for item in ids):
        raise CandidateError("input universe contains a non-Shanghai/Shenzhen id")
    return payload, ids


def _aggregate_rows(output_root: Path) -> dict[str, Any]:
    batch_files = sorted((output_root / "normalized").glob("*/batch-*.jsonl"))
    groups: dict[str, list[dict[str, Any]]] = {
        "ACTION_EVENTS_BASE_2005_2024.jsonl": [],
        "ACTION_EVENTS_TAIL_2025_20260803.jsonl": [],
        "EX_FACTORS_BASE_2005_2024.jsonl": [],
        "EX_FACTORS_TAIL_2025_20260803.jsonl": [],
    }
    for path in batch_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            row = json.loads(line)
            factor = row.get("event_type") == "ADJUSTMENT_FACTOR"
            tail = row.get("segment") == "TAIL_2025_20260803"
            key = (
                "EX_FACTORS_" if factor else "ACTION_EVENTS_"
            ) + ("TAIL_2025_20260803.jsonl" if tail else "BASE_2005_2024.jsonl")
            groups[key].append(row)
    summaries: dict[str, Any] = {}
    aggregate_root = output_root / "aggregate"
    for name, rows in groups.items():
        rows.sort(
            key=lambda item: (
                item["security_id"],
                item["event_date"],
                item["event_type"],
                item["source_row_sha256"],
            )
        )
        path = aggregate_root / name
        write_jsonl_atomic(path, rows)
        summaries[name] = {
            "path": path.relative_to(ROOT).as_posix(),
            "rows": len(rows),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    return summaries


def _write_fetch_manifest(output_root: Path) -> None:
    entries = []
    for path in sorted(output_root.rglob("*")):
        if not path.is_file() or path.name == "FETCH_MANIFEST.json":
            continue
        entries.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    write_json_atomic(
        output_root / "FETCH_MANIFEST.json",
        {
            "schema_version": 1,
            "contract_id": CONTRACT_ID,
            "purpose_ceiling": "INSPECTION_ONLY",
            "entries": entries,
            "formal_catalog_write": False,
            "quality_promotion": False,
            "research_eligible": False,
        },
    )


def run(output_root: Path, resume: bool = False) -> dict[str, Any]:
    import rqdatac

    output_root = validate_output_root(output_root)
    universe_payload, order_book_ids = _load_universe(output_root)
    batches = build_batches(order_book_ids)
    state_path = output_root / "RUN_STATE.json"
    if state_path.exists():
        if not resume:
            raise CandidateError("RUN_STATE.json exists; use --resume for a verified continuation")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("inflight_call") is not None:
            raise CandidateError("prior call completion is uncertain; automatic retry is forbidden")
        if state.get("universe_sha256") != universe_payload["order_book_ids_sha256"]:
            raise CandidateError("resume universe hash mismatch")
    else:
        state = {
            "schema_version": 1,
            "contract_id": CONTRACT_ID,
            "status": "INITIALIZING",
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "universe_sha256": universe_payload["order_book_ids_sha256"],
            "universe_count": len(order_book_ids),
            "batch_count": len(batches),
            "completed_calls": [],
            "endpoint_status": {endpoint: "UNTESTED" for endpoint in ENDPOINTS},
            "inflight_call": None,
            "business_api_calls": 0,
            "quota_api_calls": 0,
            "api_payload_persisted": False,
            "credential_value_captured": False,
        }
    rqdatac.init(enable_bjse=False)
    current_quota = quota_bytes(rqdatac.user.get_quota())
    state["quota_api_calls"] = int(state.get("quota_api_calls", 0)) + 1
    if "quota_start_bytes" not in state:
        state["quota_start_bytes"] = current_quota
    if current_quota < int(state["quota_start_bytes"]):
        state["status"] = "STOPPED__QUOTA_COUNTER_RESET__NO_AUTOMATIC_CONTINUATION"
        state["quota_current_bytes"] = current_quota
        write_json_atomic(state_path, state)
        return state
    state["quota_current_bytes"] = current_quota
    state["incremental_flow_bytes"] = current_quota - int(state["quota_start_bytes"])
    state["status"] = "RUNNING"
    write_json_atomic(state_path, state)

    completed = set(state.get("completed_calls", []))
    stop_reason: str | None = None
    for batch_index, ids in enumerate(batches):
        for endpoint in ENDPOINTS:
            call_key = f"batch-{batch_index:04d}:{endpoint}"
            if call_key in completed:
                continue
            endpoint_status = state["endpoint_status"].get(endpoint, "UNTESTED")
            if batch_index > 0 and endpoint_status != "SUPPORTED":
                continue
            incremental = int(state["quota_current_bytes"]) - int(state["quota_start_bytes"])
            if incremental >= SOFT_STOP_BYTES:
                stop_reason = "SOFT_STOP_36_MIB_REACHED_BEFORE_NEXT_CALL"
                break
            state["inflight_call"] = {
                "call_key": call_key,
                "batch_index": batch_index,
                "endpoint": endpoint,
                "id_count": len(ids),
                "id_sha256": sha256_bytes(("\n".join(ids) + "\n").encode("ascii")),
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "retry_count": 0,
            }
            write_json_atomic(state_path, state)
            quota_before = int(state["quota_current_bytes"])
            receipt: dict[str, Any] = {
                "schema_version": 1,
                "contract_id": CONTRACT_ID,
                "call_key": call_key,
                "batch_index": batch_index,
                "endpoint": endpoint,
                "id_count": len(ids),
                "id_sha256": state["inflight_call"]["id_sha256"],
                "start_date": START_DATE,
                "end_date": END_DATE,
                "retry_count": 0,
                "raw_api_payload_persisted": False,
                "quota_before_bytes": quota_before,
            }
            try:
                value = _endpoint_call(rqdatac, endpoint, ids)
                normalized, rejected = normalize_frame(endpoint, value)
                normalized_path = (
                    output_root / "normalized" / endpoint / f"batch-{batch_index:04d}.jsonl"
                )
                write_jsonl_atomic(normalized_path, normalized)
                receipt.update(
                    {
                        "status": "PASS" if normalized else "PASS_EMPTY",
                        "normalized_rows": len(normalized),
                        "rejected_rows": rejected,
                        "normalized_path": normalized_path.relative_to(ROOT).as_posix(),
                        "normalized_bytes": normalized_path.stat().st_size,
                        "normalized_sha256": sha256_file(normalized_path),
                    }
                )
                if batch_index == 0:
                    state["endpoint_status"][endpoint] = "SUPPORTED"
            except Exception as exc:
                receipt.update({"status": "ERROR__NO_RETRY", "error": safe_error(exc)})
                state["endpoint_status"][endpoint] = "DISABLED_AFTER_FIRST_ERROR__NO_RETRY"
            state["business_api_calls"] = int(state.get("business_api_calls", 0)) + 1
            try:
                current_quota = quota_bytes(rqdatac.user.get_quota())
                state["quota_api_calls"] = int(state.get("quota_api_calls", 0)) + 1
                receipt["quota_after_bytes"] = current_quota
                receipt["call_incremental_flow_bytes"] = max(0, current_quota - quota_before)
                state["quota_current_bytes"] = current_quota
                state["incremental_flow_bytes"] = current_quota - int(state["quota_start_bytes"])
            except Exception as exc:
                receipt["quota_status"] = "ERROR__STOPPED"
                receipt["quota_error"] = safe_error(exc)
                stop_reason = "QUOTA_MEASUREMENT_FAILED_AFTER_BUSINESS_CALL"
            receipt_path = output_root / "receipts" / f"{call_key.replace(':', '-')}.json"
            write_json_atomic(receipt_path, receipt)
            completed.add(call_key)
            state["completed_calls"] = sorted(completed)
            state["inflight_call"] = None
            state["last_receipt"] = receipt_path.relative_to(ROOT).as_posix()
            write_json_atomic(state_path, state)
            if stop_reason:
                break
            if int(state["incremental_flow_bytes"]) > HARD_LIMIT_BYTES:
                stop_reason = "HARD_LIMIT_40_MIB_BREACH_DETECTED__STOPPED"
                break
        if stop_reason:
            break

    aggregates = _aggregate_rows(output_root)
    disabled = {
        key: value
        for key, value in state["endpoint_status"].items()
        if value != "SUPPORTED"
    }
    expected_calls = len(batches) * len(ENDPOINTS)
    fully_complete = not stop_reason and not disabled and len(completed) == expected_calls
    state.update(
        {
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": (
                "COMPLETE__BOUNDED_CANDIDATE__FORMAL_EFFECT_NONE"
                if fully_complete
                else "PARTIAL__CURSOR_PRESERVED__FORMAL_EFFECT_NONE"
            ),
            "stop_reason": stop_reason,
            "disabled_endpoints": disabled,
            "aggregate_outputs": aggregates,
            "hard_limit_bytes": HARD_LIMIT_BYTES,
            "soft_stop_bytes": SOFT_STOP_BYTES,
            "hard_limit_respected": int(state.get("incremental_flow_bytes", 0)) <= HARD_LIMIT_BYTES,
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_effect": "NONE",
            "formal_catalog_write": False,
            "roadmap_write": False,
            "source_registry_write": False,
            "quality_promotion": False,
            "research_eligible": False,
        }
    )
    write_json_atomic(state_path, state)
    _write_fetch_manifest(output_root)
    return state


def offline_check() -> dict[str, Any]:
    import rqdatac

    entrypoints = {
        "get_dividend": callable(getattr(rqdatac, "get_dividend", None)),
        "get_split": callable(getattr(rqdatac, "get_split", None)),
        "get_allotment": callable(getattr(rqdatac, "get_allotment", None)),
        "get_ex_factor": callable(getattr(rqdatac, "get_ex_factor", None)),
        "user.get_quota": callable(getattr(getattr(rqdatac, "user", None), "get_quota", None)),
    }
    return {
        "status": "OFFLINE_ENTRYPOINT_CHECK_PASS" if all(entrypoints.values()) else "OFFLINE_ENTRYPOINT_CHECK_FAIL",
        "entrypoints": entrypoints,
        "network_requests": 0,
        "business_api_calls": 0,
        "credential_value_captured": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--offline-check", action="store_true")
    args = parser.parse_args()
    if args.offline_check:
        print(json.dumps(offline_check(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    result = run(args.output_root, resume=args.resume)
    print(
        canonical_json(
            {
                "status": result["status"],
                "incremental_flow_bytes": result.get("incremental_flow_bytes"),
                "business_api_calls": result.get("business_api_calls"),
                "quota_api_calls": result.get("quota_api_calls"),
                "hard_limit_respected": result.get("hard_limit_respected"),
                "stop_reason": result.get("stop_reason"),
                "output": args.output_root.relative_to(ROOT).as_posix(),
            }
        )
    )
    return 0 if result.get("hard_limit_respected", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
