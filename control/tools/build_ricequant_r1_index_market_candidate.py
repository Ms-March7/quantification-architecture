from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[2]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from control.tools.build_ricequant_core11_industry_history_candidate import (
    BuildError,
    numeric_fields,
    protected_state,
    quota_value,
    safe_error,
    sha256_file,
    write_csv,
    write_json,
)


CONTRACT = WORKSPACE / "data/catalog/contracts/ricequant_r1_index_market_candidate_v1/contract.json"
RUN_ID = "ricequant-r1-index-market-candidate-20260805-v1"
CANDIDATE_ID = "ricequant-index-market-20050104-20220131-20260805-v1"
PARENT = WORKSPACE / "data/market/quarantine/r1-market-reference"
FINAL = PARENT / CANDIDATE_ID
STAGE = PARENT / f".{CANDIDATE_ID}.staging"
REPORT = WORKSPACE / "reports/data-governance" / RUN_ID


def clean_scalar(value: Any) -> Any:
    import pandas as pd

    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (AttributeError, ValueError):
            pass
    return value


def normalize_frame(frame: Any) -> tuple[list[dict[str, Any]], list[str]]:
    reset = frame.reset_index()
    reset.columns = [str(column or f"index_{index}") for index, column in enumerate(reset.columns)]
    if len(set(reset.columns)) != len(reset.columns):
        raise BuildError("duplicate columns after index reset")
    fields = sorted(reset.columns)
    rows = [
        {field: clean_scalar(row[field]) for field in fields}
        for row in reset.to_dict(orient="records")
    ]
    rows.sort(key=lambda row: tuple(str(row[field]) for field in fields))
    return rows, fields


def file_ref(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if resolved.is_relative_to(STAGE.resolve()):
        resolved = FINAL.resolve() / resolved.relative_to(STAGE.resolve())
    return {
        "path": resolved.relative_to(WORKSPACE.resolve()).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def canonical_rows_hash(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def main() -> int:
    if FINAL.exists() or STAGE.exists() or REPORT.exists():
        raise BuildError("immutable final, prior staging, or report path already exists")
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    before_protected = protected_state()
    STAGE.mkdir(parents=True, exist_ok=False)
    calls = 0
    started = datetime.now(timezone.utc).isoformat()
    try:
        import rqdatac

        rqdatac.init(enable_bjse=False)
        before_quota = numeric_fields(rqdatac.user.get_quota())
        calls += 1
        used_before = quota_value(before_quota, "bytes_used")
        limit = quota_value(before_quota, "bytes_limit")
        if used_before is not None and limit is not None and limit - used_before < 30 * 1024 * 1024:
            raise BuildError("less than 30 MiB quota remains before candidate build")

        scope = contract["scope"]
        master_frame = rqdatac.all_instruments(type="INDX", date=scope["index_master_anchor_date"])
        calls += 1
        master, master_fields = normalize_frame(master_frame)
        master_ids = {str(row.get("order_book_id", "")) for row in master}
        requested = list(scope["index_ids"])
        available = [item for item in requested if item in master_ids]
        if len(available) < 6:
            raise BuildError(f"fewer than six requested benchmark indices available: {available}")

        daily_frame = rqdatac.get_price(
            available,
            start_date=scope["index_daily_start"],
            end_date=scope["index_daily_end"],
            frequency="1d",
            fields=["open", "high", "low", "close", "prev_close", "volume", "total_turnover"],
            adjust_type="none",
            skip_suspended=False,
        )
        calls += 1
        daily, daily_fields = normalize_frame(daily_frame)
        if not daily or len(daily) > int(contract["limits"]["maximum_index_daily_rows"]):
            raise BuildError(f"invalid index daily row count: {len(daily)}")

        yield_frame = rqdatac.get_yield_curve(
            start_date=scope["index_daily_start"],
            end_date=scope["index_daily_end"],
        )
        calls += 1
        yields, yield_fields = normalize_frame(yield_frame)
        if not yields or len(yields) > int(contract["limits"]["maximum_yield_rows"]):
            raise BuildError(f"invalid yield row count: {len(yields)}")

        master_path = STAGE / "index_master.csv"
        daily_path = STAGE / "index_daily.csv"
        yield_path = STAGE / "yield_curve.csv"
        write_csv(master_path, master, master_fields)
        write_csv(daily_path, daily, daily_fields)
        write_csv(yield_path, yields, yield_fields)

        after_quota = numeric_fields(rqdatac.user.get_quota())
        calls += 1
        if calls > int(contract["limits"]["maximum_business_api_calls_including_quota"]):
            raise BuildError("API call cap exceeded")
        used_after = quota_value(after_quota, "bytes_used")
        increment = None if used_before is None or used_after is None else int(used_after - used_before)
        cap = int(contract["limits"]["maximum_measured_quota_increment_bytes"])
        if increment is not None and increment > cap:
            raise BuildError(f"quota increment exceeds contract: {increment} > {cap}")

        date_field = next((field for field in daily_fields if "date" in field.lower()), None)
        daily_dates = sorted({str(row[date_field])[:10] for row in daily}) if date_field else []
        oid_field = next((field for field in daily_fields if field.lower() == "order_book_id"), None)
        returned_ids = sorted({str(row[oid_field]) for row in daily}) if oid_field else []
        quality = {
            "index_master_rows": len(master),
            "index_master_fields": master_fields,
            "requested_index_ids": requested,
            "available_requested_index_ids": available,
            "index_daily_rows": len(daily),
            "index_daily_returned_ids": returned_ids,
            "index_daily_date_start": daily_dates[0] if daily_dates else None,
            "index_daily_date_end": daily_dates[-1] if daily_dates else None,
            "yield_rows": len(yields),
            "yield_fields": yield_fields,
        }
        write_json(STAGE / "quality.json", quality)
        if protected_state() != before_protected:
            raise BuildError("protected authority state changed during build")
        finished = datetime.now(timezone.utc).isoformat()
        manifest = {
            "schema_version": 1,
            "candidate_id": CANDIDATE_ID,
            "status": "COMPLETED__INSPECTION_ONLY__NOT_ADMITTED",
            "contract": file_ref(CONTRACT),
            "source": "Ricequant RQData",
            "started_at_utc": started,
            "finished_at_utc": finished,
            "business_api_calls_including_quota": calls,
            "quota": {
                "numeric_before": before_quota,
                "numeric_after": after_quota,
                "measured_increment_bytes": increment,
                "contract_cap_bytes": cap,
            },
            "quality": quality,
            "normalized_response_hashes": {
                "index_master": canonical_rows_hash(master),
                "index_daily": canonical_rows_hash(daily),
                "yield_curve": canonical_rows_hash(yields),
            },
            "artifacts": {
                "index_master": file_ref(master_path),
                "index_daily": file_ref(daily_path),
                "yield_curve": file_ref(yield_path),
                "quality": file_ref(STAGE / "quality.json"),
            },
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_admission": False,
            "catalog_write": False,
            "research_use": False,
            "available_at_proven": False,
            "revision_chain_proven": False,
            "credential_value_captured": False,
            "protected_state_unchanged": True,
        }
        write_json(STAGE / "manifest.json", manifest)
        write_json(STAGE / "COMMITTED.json", {
            "status": "COMMITTED_INSPECTION_ONLY",
            "candidate_id": CANDIDATE_ID,
            "manifest_sha256": sha256_file(STAGE / "manifest.json"),
            "committed_at_utc": finished,
        })
        PARENT.mkdir(parents=True, exist_ok=True)
        os.replace(STAGE, FINAL)
        REPORT.mkdir(parents=True, exist_ok=False)
        result = {
            "status": "PASS__INSPECTION_ONLY_CANDIDATE_COMMITTED__FORMAL_EFFECT_NONE",
            "candidate_root": FINAL.relative_to(WORKSPACE).as_posix(),
            "manifest": file_ref(FINAL / "manifest.json"),
            "quality": quality,
            "quota_increment_bytes": increment,
            "business_api_calls_including_quota": calls,
            "protected_state_unchanged": True,
        }
        write_json(REPORT / "RESULT.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        failure = {
            "status": "FAILED_CLOSED__NO_CATALOG_EFFECT",
            "run_id": RUN_ID,
            "error": safe_error(exc),
            "business_api_calls_including_quota": calls,
            "staging_retained": STAGE.exists(),
            "credential_value_captured": False,
        }
        if STAGE.exists():
            write_json(STAGE / "FAILURE.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
