from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from numbers import Number
from pathlib import Path
from typing import Any, Mapping


WORKSPACE = Path(__file__).resolve().parents[2]
CONTRACT = WORKSPACE / "data/catalog/contracts/ricequant_core11_industry_history_candidate_v1/contract.json"
RUN_ID = "ricequant-core11-industry-history-candidate-20260805-v1"
CANDIDATE_ID = "ricequant-citics2019-level1-history-20260805-v1"
PARENT = WORKSPACE / "data/market/quarantine/cn-industry-classification-pit"
FINAL = PARENT / CANDIDATE_ID
STAGE = PARENT / f".{CANDIDATE_ID}.staging"
REPORT = WORKSPACE / "reports/data-governance" / RUN_ID
PROTECTED = (
    "data/catalog/datasets.json",
    "control/ROADMAP.md",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
)


class BuildError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json(path: Path, value: Any) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_bytes(path, payload)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def safe_error(exc: Exception) -> dict[str, str]:
    message = re.sub(r"(?:tcp|rqdata|rqdatac)://\S+", "<redacted>", str(exc))
    message = re.sub(
        r"(?i)\b(license|token|password|secret|api[_-]?key)\b\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        message,
    )
    return {"type": type(exc).__name__, "message": message[:400]}


def protected_state() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for relative in PROTECTED:
        path = WORKSPACE / relative
        if not path.is_file():
            raise BuildError(f"protected authority file missing: {relative}")
        result[relative] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return result


def numeric_fields(value: Any) -> dict[str, float]:
    result: dict[str, float] = {}

    def visit(item: Any, prefix: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                visit(child, f"{prefix}.{key}" if prefix else str(key))
        elif isinstance(item, Number) and not isinstance(item, complex):
            result[prefix] = float(item)

    visit(value)
    return result


def quota_value(fields: Mapping[str, float], suffix: str) -> float | None:
    matches = [value for key, value in fields.items() if key.lower().endswith(suffix.lower())]
    return matches[0] if len(matches) == 1 else None


def scalar(value: Any) -> Any:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    if hasattr(value, "item"):
        try:
            return value.item()
        except (AttributeError, ValueError):
            pass
    return value


def frame_rows(frame: Any) -> list[dict[str, Any]]:
    reset = frame.reset_index()
    reset.columns = [str(column or "index") for column in reset.columns]
    return [
        {str(key): scalar(value) for key, value in row.items()}
        for row in reset.to_dict(orient="records")
    ]


def normalize_mapping(frame: Any) -> list[dict[str, Any]]:
    required = [
        "first_industry_code", "first_industry_name",
        "second_industry_code", "second_industry_name",
        "third_industry_code", "third_industry_name",
    ]
    missing = [field for field in required if field not in frame.columns]
    if missing:
        raise BuildError(f"industry mapping missing fields: {missing}")
    rows = [
        {field: str(row[field]).strip() for field in required}
        for _, row in frame[required].drop_duplicates().iterrows()
    ]
    rows.sort(key=lambda row: tuple(row[field] for field in required))
    return rows


def normalize_memberships(frame: Any, code: str, name: str) -> list[dict[str, Any]]:
    rows = frame_rows(frame)
    normalized: list[dict[str, Any]] = []
    for row in rows:
        order_book_id = str(row.get("order_book_id") or row.get("index") or "").strip()
        start_date = str(row.get("start_date") or "")[:10]
        cancel_date = str(row.get("cancel_date") or "")[:10]
        if not re.fullmatch(r"\d{6}\.(?:XSHG|XSHE)", order_book_id):
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date):
            raise BuildError(f"invalid start_date for {order_book_id}: {start_date}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cancel_date):
            raise BuildError(f"invalid cancel_date for {order_book_id}: {cancel_date}")
        if cancel_date <= start_date:
            raise BuildError(f"non-positive membership interval for {order_book_id}")
        normalized.append({
            "order_book_id": order_book_id,
            "first_industry_code": code,
            "first_industry_name": name,
            "start_date": start_date,
            "cancel_date": cancel_date,
            "source": "citics_2019",
            "level": 1,
        })
    normalized.sort(key=lambda row: (
        row["order_book_id"], row["start_date"], row["cancel_date"], row["first_industry_code"]
    ))
    return normalized


def overlap_count(rows: list[dict[str, Any]]) -> int:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["order_book_id"]), []).append(row)
    count = 0
    for items in grouped.values():
        items.sort(key=lambda row: (row["start_date"], row["cancel_date"]))
        for left, right in zip(items, items[1:]):
            if right["start_date"] < left["cancel_date"] and right["first_industry_code"] != left["first_industry_code"]:
                count += 1
    return count


def file_ref(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    stage_root = STAGE.resolve()
    if resolved.is_relative_to(stage_root):
        logical = FINAL.resolve() / resolved.relative_to(stage_root)
    else:
        logical = resolved
    return {
        "path": logical.relative_to(WORKSPACE.resolve()).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def main() -> int:
    if FINAL.exists() or STAGE.exists() or REPORT.exists():
        raise BuildError("immutable final, prior staging, or report path already exists")
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    before_protected = protected_state()
    STAGE.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    calls = 0
    try:
        import rqdatac

        rqdatac.init(enable_bjse=False)
        before_quota = numeric_fields(rqdatac.user.get_quota())
        calls += 1
        used_before = quota_value(before_quota, "bytes_used")
        limit = quota_value(before_quota, "bytes_limit")
        if used_before is not None and limit is not None and limit - used_before < 30 * 1024 * 1024:
            raise BuildError("less than 30 MiB quota remains before candidate build")

        mapping_frame = rqdatac.get_industry_mapping(source="citics_2019", date="2026-08-03")
        calls += 1
        mapping = normalize_mapping(mapping_frame)
        level1 = sorted({
            (row["first_industry_code"], row["first_industry_name"])
            for row in mapping
        })
        if not 20 <= len(level1) <= 38:
            raise BuildError(f"unexpected level-1 industry count: {len(level1)}")

        memberships: list[dict[str, Any]] = []
        response_hashes: dict[str, str] = {}
        for code, name in level1:
            if calls >= int(contract["limits"]["maximum_business_api_calls"]) - 1:
                raise BuildError("business API call cap would be exceeded")
            frame = rqdatac.get_industry_change(name, source="citics_2019", level=1)
            calls += 1
            rows = normalize_memberships(frame, code, name)
            memberships.extend(rows)
            response_hashes[code] = hashlib.sha256(
                json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            write_json(STAGE / "progress.json", {
                "status": "RUNNING_INSPECTION_ONLY",
                "completed_industries": len(response_hashes),
                "total_industries": len(level1),
                "membership_rows": len(memberships),
                "business_api_calls": calls,
            })

        if len(memberships) > int(contract["limits"]["maximum_returned_membership_rows"]):
            raise BuildError("membership row cap exceeded")
        unique_keys = {
            (row["order_book_id"], row["first_industry_code"], row["start_date"], row["cancel_date"])
            for row in memberships
        }
        if len(unique_keys) != len(memberships):
            raise BuildError("duplicate normalized membership intervals")

        memberships.sort(key=lambda row: (
            row["order_book_id"], row["start_date"], row["cancel_date"], row["first_industry_code"]
        ))
        mapping_path = STAGE / "mapping.csv"
        memberships_path = STAGE / "memberships_level1.csv"
        write_csv(mapping_path, mapping, list(mapping[0]))
        write_csv(memberships_path, memberships, list(memberships[0]))

        after_quota = numeric_fields(rqdatac.user.get_quota())
        calls += 1
        used_after = quota_value(after_quota, "bytes_used")
        increment = None if used_before is None or used_after is None else int(used_after - used_before)
        cap = int(contract["limits"]["maximum_measured_quota_increment_bytes"])
        if increment is not None and increment > cap:
            raise BuildError(f"measured quota increment exceeds contract: {increment} > {cap}")

        active = [row for row in memberships if row["start_date"] <= "2026-08-03" < row["cancel_date"]]
        quality = {
            "mapping_rows": len(mapping),
            "level1_industries": len(level1),
            "membership_rows": len(memberships),
            "unique_securities": len({row["order_book_id"] for row in memberships}),
            "history_start": min(row["start_date"] for row in memberships),
            "history_cancel_max": max(row["cancel_date"] for row in memberships),
            "active_rows_at_anchor": len(active),
            "active_unique_securities_at_anchor": len({row["order_book_id"] for row in active}),
            "cross_industry_interval_overlaps": overlap_count(memberships),
            "sentinel_2200_rows": sum(row["cancel_date"] == "2200-12-31" for row in memberships),
        }
        write_json(STAGE / "quality.json", quality)
        finished = datetime.now(timezone.utc).isoformat()
        manifest = {
            "schema_version": 1,
            "candidate_id": CANDIDATE_ID,
            "status": "COMPLETED__INSPECTION_ONLY__NOT_ADMITTED",
            "contract": file_ref(CONTRACT),
            "source": "Ricequant RQData",
            "classification": "citics_2019",
            "level": 1,
            "mapping_anchor_date": "2026-08-03",
            "started_at_utc": started,
            "finished_at_utc": finished,
            "business_api_calls": calls,
            "quota": {
                "numeric_before": before_quota,
                "numeric_after": after_quota,
                "measured_increment_bytes": increment,
                "contract_cap_bytes": cap,
            },
            "quality": quality,
            "response_hashes_by_first_industry_code": response_hashes,
            "artifacts": {
                "mapping": file_ref(mapping_path),
                "memberships_level1": file_ref(memberships_path),
                "quality": file_ref(STAGE / "quality.json"),
            },
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_admission": False,
            "catalog_write": False,
            "research_use": False,
            "available_at_proven": False,
            "revision_chain_proven": False,
            "credential_value_captured": False,
            "protected_state_before": before_protected,
        }
        after_protected = protected_state()
        if after_protected != before_protected:
            raise BuildError("protected authority state changed during build")
        manifest["protected_state_unchanged"] = True
        write_json(STAGE / "manifest.json", manifest)
        write_json(STAGE / "COMMITTED.json", {
            "status": "COMMITTED_INSPECTION_ONLY",
            "candidate_id": CANDIDATE_ID,
            "manifest_sha256": sha256_file(STAGE / "manifest.json"),
            "committed_at_utc": finished,
        })
        (STAGE / "progress.json").unlink(missing_ok=True)
        PARENT.mkdir(parents=True, exist_ok=True)
        os.replace(STAGE, FINAL)
        REPORT.mkdir(parents=True, exist_ok=False)
        result = {
            "status": "PASS__INSPECTION_ONLY_CANDIDATE_COMMITTED__FORMAL_EFFECT_NONE",
            "candidate_root": FINAL.resolve().relative_to(WORKSPACE.resolve()).as_posix(),
            "manifest": file_ref(FINAL / "manifest.json"),
            "quality": quality,
            "quota_increment_bytes": increment,
            "business_api_calls": calls,
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
            "business_api_calls": calls,
            "staging_retained": STAGE.exists(),
            "credential_value_captured": False,
        }
        write_json(STAGE / "FAILURE.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
