from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = (
    WORKSPACE_ROOT
    / "data/catalog/contracts/rqdata_r0_shsz_core01_core02_candidate_v1/contract.json"
)
DEFAULT_CANDIDATE = (
    WORKSPACE_ROOT
    / "data/market/quarantine/rqdata-r0-shsz-core01-core02/candidate-20260804-v1"
)
DEFAULT_REPORT = (
    WORKSPACE_ROOT
    / "reports/data-governance/rqdata-r0-shsz-core01-core02-20260804-v1"
)
RQDATAC_SITE_PACKAGES = (
    WORKSPACE_ROOT
    / "control/runtime/ricequant-rqsdk/.venv/Lib/site-packages"
)
BASE_START = date(2005, 1, 4)
BASE_END = date(2024, 12, 31)
INCREMENTAL_START = date(2025, 1, 1)
COMPLETE_THROUGH = date(2026, 8, 3)
MICS = ("XSHG", "XSHE")
SENSITIVE_PARTS = (
    "account",
    "credential",
    "license",
    "password",
    "secret",
    "token",
    "uri",
    "user",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_receipt(path: Path) -> dict[str, Any]:
    return {
        "path": path.resolve().relative_to(WORKSPACE_ROOT).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def safe_error(exc: Exception) -> dict[str, str]:
    message = re.sub(r"(?:tcp|rqdata|rqdatac)://\S+", "<redacted>", str(exc))
    return {"type": type(exc).__name__, "message": message[:300]}


def _date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value)[:10]
    if text in {"", "None", "NaT", "nan", "0000-00-00"}:
        return None
    return date.fromisoformat(text)


def _json_record(record: Mapping[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_json_record(row) + "\n")
            count += 1
    return count


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def normalize_master(frame: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    required = {
        "order_book_id",
        "symbol",
        "special_type",
        "exchange",
        "status",
        "type",
        "listed_date",
        "de_listed_date",
        "board_type",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"RQData master missing required fields: {missing}")

    fetched_xshg_xshe = 0
    after_window_filter = 0
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in frame.to_dict(orient="records"):
        order_book_id = str(raw["order_book_id"])
        if not order_book_id.endswith((".XSHG", ".XSHE")):
            continue
        fetched_xshg_xshe += 1
        local_code, mic = order_book_id.rsplit(".", 1)
        if mic not in MICS or str(raw["exchange"]) != mic or str(raw["type"]) != "CS":
            continue
        listed = _date_or_none(raw["listed_date"])
        delisted = _date_or_none(raw["de_listed_date"])
        if listed is None:
            raise ValueError(f"missing listed_date for {order_book_id}")
        if listed > COMPLETE_THROUGH:
            continue
        if order_book_id in seen:
            raise ValueError(f"duplicate order_book_id: {order_book_id}")
        seen.add(order_book_id)
        after_window_filter += 1
        overlaps_scope = (delisted is None or delisted >= BASE_START)
        interval_start = max(listed, BASE_START) if overlaps_scope else None
        interval_end = (
            min(delisted or COMPLETE_THROUGH, COMPLETE_THROUGH)
            if overlaps_scope
            else None
        )
        key_material = f"rqdata|{order_book_id}|{listed.isoformat()}|{delisted.isoformat() if delisted else 'OPEN'}"
        episode_id = "rqdata-listing-episode:v1:" + hashlib.sha256(
            key_material.encode("utf-8")
        ).hexdigest()[:32]
        rows.append(
            {
                "episode_id": episode_id,
                "order_book_id": order_book_id,
                "mic": mic,
                "local_code": local_code,
                "symbol": str(raw["symbol"]),
                "special_type": str(raw["special_type"]),
                "board_type": str(raw["board_type"]),
                "status": str(raw["status"]),
                "listed_date": listed.isoformat(),
                "de_listed_date": None if delisted is None else delisted.isoformat(),
                "bounded_interval_start_inclusive": (
                    None if interval_start is None else interval_start.isoformat()
                ),
                "bounded_interval_end_observed": (
                    None if interval_end is None else interval_end.isoformat()
                ),
                "right_censored_at_complete_through": delisted is None
                or delisted > COMPLETE_THROUGH,
                "overlaps_2005_2024_base": overlaps_scope
                and interval_start <= BASE_END
                and interval_end >= BASE_START,
                "overlaps_2025_2026_increment": overlaps_scope
                and interval_start <= COMPLETE_THROUGH
                and interval_end >= INCREMENTAL_START,
                "source_attribute_semantics": "LATEST_ATTRIBUTES_ONLY__NOT_HISTORICAL_PIT_MASTER",
            }
        )
    rows.sort(key=lambda item: item["order_book_id"])
    return rows, {
        "rqdata_xshg_xshe_cs_rows_before_window_filter": fetched_xshg_xshe,
        "normalized_historical_rows_listed_by_complete_through": after_window_filter,
    }


def normalize_calendar(values: Sequence[Any]) -> list[str]:
    dates = sorted({_date_or_none(value) for value in values})
    if None in dates:
        raise ValueError("RQData calendar contains an invalid date")
    result = [item.isoformat() for item in dates if item is not None]
    if not result or result[0] != BASE_START.isoformat() or result[-1] != COMPLETE_THROUGH.isoformat():
        raise ValueError("RQData calendar does not cover the frozen complete range")
    return result


def existing_base_calendar_dates(parquet_path: Path) -> list[str]:
    import duckdb

    sql = """
        SELECT calendar_date
        FROM read_parquet(?)
        WHERE venue_mic IN ('XSHG', 'XSHE')
          AND observed_vendor_state = 'O'
          AND calendar_date BETWEEN DATE '2005-01-04' AND DATE '2024-12-31'
        GROUP BY calendar_date
        HAVING COUNT(DISTINCT venue_mic) = 2
        ORDER BY calendar_date
    """
    values = duckdb.connect().execute(sql, [str(parquet_path)]).fetchall()
    return [item[0].isoformat() for item in values]


def _key(mic: str, local_code: str) -> str:
    return f"{mic}|{local_code}"


def join_baseline_episodes(
    episodes: Sequence[Mapping[str, Any]], master_rows: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in master_rows:
        index[_key(str(row["mic"]), str(row["local_code"]))].append(row)
    output: list[dict[str, Any]] = []
    matched = 0
    for row in sorted(episodes, key=lambda item: (item["mic"], item["local_code_observed"])):
        key = _key(str(row["mic"]), str(row["local_code_observed"]))
        hits = index.get(key, [])
        status = "EXACT_UNIQUE_MATCH" if len(hits) == 1 else (
            "NO_RQDATA_MASTER_MATCH" if not hits else "DUPLICATE_RQDATA_MASTER_KEY"
        )
        if len(hits) == 1:
            matched += 1
        hit = hits[0] if len(hits) == 1 else {}
        output.append(
            {
                "key": key,
                "baseline_episode_id": row["internal_listing_episode_id_candidate"],
                "join_status": status,
                "rqdata_episode_id": hit.get("episode_id"),
                "rqdata_listed_date": hit.get("listed_date"),
                "rqdata_de_listed_date": hit.get("de_listed_date"),
                "baseline_interval_start": row["bounded_valid_from"],
                "baseline_interval_end": row["bounded_valid_to_inclusive"],
                "baseline_pv_row_count": int(row["pv_row_count"]),
            }
        )
    return output, {"rows": len(output), "exact_unique_matches": matched}


def join_exclusion_ledger(
    ledger: Sequence[Mapping[str, Any]], master_rows: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in master_rows:
        index[_key(str(row["mic"]), str(row["local_code"]))].append(row)
    joined: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    matched = 0
    duplicates = 0
    for row in sorted(ledger, key=lambda item: (item["mic"], item["local_code_observed"])):
        mic = str(row["mic"])
        code = str(row["local_code_observed"])
        key = _key(mic, code)
        hits = index.get(key, [])
        if len(hits) == 1:
            status = "EXACT_UNIQUE_MATCH"
            matched += 1
            hit = hits[0]
        elif len(hits) > 1:
            status = "DUPLICATE_RQDATA_MASTER_KEY"
            duplicates += 1
            hit = {}
        else:
            status = "NO_RQDATA_MASTER_MATCH"
            hit = {}
            remaining.append(
                {
                    "key": key,
                    "classification": (
                        "NON_A_SHARE__XSHG_B_SHARE_900_NAMESPACE"
                        if mic == "XSHG" and code.startswith("900")
                        else "UNRESOLVED_DELISTED_OR_CROSS_CODE_TERMINATION_CONFLICT"
                    ),
                    "prior_exclusion_reasons": list(row["exclusion_reasons"]),
                    "pv_first_date": row.get("pv_first_date"),
                    "pv_last_date": row.get("pv_last_date"),
                    "pv_row_count": int(row.get("pv_row_count") or 0),
                    "decision": "KEEP_EXCLUDED_FAIL_CLOSED",
                }
            )
        joined.append(
            {
                "key": key,
                "join_status": status,
                "rqdata_episode_id": hit.get("episode_id"),
                "rqdata_listed_date": hit.get("listed_date"),
                "rqdata_de_listed_date": hit.get("de_listed_date"),
                "rqdata_symbol": hit.get("symbol"),
                "rqdata_board_type": hit.get("board_type"),
                "rqdata_special_type": hit.get("special_type"),
                "prior_exclusion_reasons": list(row["exclusion_reasons"]),
                "pv_first_date": row.get("pv_first_date"),
                "pv_last_date": row.get("pv_last_date"),
                "pv_row_count": int(row.get("pv_row_count") or 0),
                "recovery_interpretation": (
                    "MASTER_KEY_AND_INTERVAL_RECOVERED__NOT_PERMANENT_IDENTITY_PROOF"
                    if status == "EXACT_UNIQUE_MATCH"
                    else "NOT_RECOVERED__KEEP_EXCLUDED"
                ),
            }
        )
    return joined, remaining, {
        "rows": len(joined),
        "exact_unique_matches": matched,
        "duplicate_keys": duplicates,
        "still_unmatched": len(remaining),
    }


def reconcile_calendar(rq_dates: Sequence[str], baseline_dates: Sequence[str]) -> dict[str, Any]:
    rq_set = set(rq_dates)
    baseline_set = set(baseline_dates)
    rq_base = {item for item in rq_set if item <= BASE_END.isoformat()}
    incremental = sorted(item for item in rq_set if item >= INCREMENTAL_START.isoformat())
    return {
        "rqdata_full_count": len(rq_set),
        "rqdata_first": min(rq_set),
        "rqdata_last": max(rq_set),
        "baseline_existing_count": len(baseline_set),
        "rqdata_base_count": len(rq_base),
        "baseline_set_equal": rq_base == baseline_set,
        "only_in_rqdata_base": sorted(rq_base - baseline_set),
        "only_in_existing_base": sorted(baseline_set - rq_base),
        "incremental_2025_2026_count": len(incremental),
        "incremental_first": incremental[0] if incremental else None,
        "incremental_last": incremental[-1] if incremental else None,
    }


def validate_hard_checks(
    contract: Mapping[str, Any],
    episode_stats: Mapping[str, int],
    exclusion_stats: Mapping[str, int],
    remaining: Sequence[Mapping[str, Any]],
    calendar_result: Mapping[str, Any],
) -> None:
    expected = contract["hard_expected_checks"]
    actual = {
        "baseline_episode_rows": episode_stats["rows"],
        "baseline_exclusion_keys": exclusion_stats["rows"],
        "rqdata_exact_unique_matches_for_old_exclusions": exclusion_stats["exact_unique_matches"],
        "old_exclusions_still_unmatched": exclusion_stats["still_unmatched"],
        "baseline_calendar_dates": calendar_result["baseline_existing_count"],
        "baseline_calendar_set_equal": calendar_result["baseline_set_equal"],
        "unmatched_old_exclusion_keys": sorted(item["key"] for item in remaining),
    }
    failures = {
        key: {"expected": expected[key], "actual": value}
        for key, value in actual.items()
        if value != expected[key]
    }
    if failures:
        raise ValueError(f"hard expected checks failed: {json.dumps(failures, sort_keys=True)}")


def validate_output_path(path: Path, allowed_root: Path) -> Path:
    resolved = path.resolve()
    root = allowed_root.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"output must be below {root}")
    if resolved.exists():
        raise FileExistsError(f"immutable output already exists: {resolved}")
    return resolved


def _publish_stage(stage: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage, target)


def build_candidate(candidate_target: Path, report_target: Path) -> dict[str, Any]:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    inputs = {key: WORKSPACE_ROOT / value for key, value in contract["pinned_inputs"].items()}
    for path in inputs.values():
        if not path.is_file():
            raise FileNotFoundError(path)

    candidate_target = validate_output_path(
        candidate_target, WORKSPACE_ROOT / "data/market/quarantine"
    )
    report_target = validate_output_path(
        report_target, WORKSPACE_ROOT / "reports/data-governance"
    )
    if str(RQDATAC_SITE_PACKAGES) not in sys.path:
        sys.path.insert(0, str(RQDATAC_SITE_PACKAGES))
    import rqdatac

    observed_at = datetime.now(timezone.utc).isoformat()
    rqdatac.init(enable_bjse=False)
    master_frame = rqdatac.all_instruments(type="CS", date=None)
    calendar_values = rqdatac.get_trading_dates(
        BASE_START.isoformat(), COMPLETE_THROUGH.isoformat()
    )
    master_rows, master_stats = normalize_master(master_frame)
    rq_dates = normalize_calendar(list(calendar_values))
    baseline_dates = existing_base_calendar_dates(inputs["baseline_calendar"])
    calendar_result = reconcile_calendar(rq_dates, baseline_dates)
    baseline_episodes = read_jsonl(inputs["baseline_episodes"])
    exclusion_ledger = read_jsonl(inputs["baseline_exclusion_ledger"])
    episode_join, episode_stats = join_baseline_episodes(baseline_episodes, master_rows)
    exclusion_join, remaining, exclusion_stats = join_exclusion_ledger(
        exclusion_ledger, master_rows
    )
    validate_hard_checks(
        contract, episode_stats, exclusion_stats, remaining, calendar_result
    )

    stage = candidate_target.parent / f".{candidate_target.name}.staging-{uuid.uuid4().hex}"
    stage.mkdir(parents=True, exist_ok=False)
    artifact_paths: list[Path] = []
    for relative, rows in (
        ("normalized/rqdata_shsz_cs_master.jsonl", master_rows),
        (
            "normalized/rqdata_trading_dates.jsonl",
            [
                {
                    "trading_date": item,
                    "segment": "BASE_2005_2024"
                    if item <= BASE_END.isoformat()
                    else "INCREMENTAL_2025_2026",
                }
                for item in rq_dates
            ],
        ),
        ("derived/baseline_episode_exact_join.jsonl", episode_join),
        ("derived/prior_exclusion_exact_join.jsonl", exclusion_join),
        ("derived/still_excluded_non_a_or_conflict_keys.jsonl", remaining),
    ):
        path = stage / relative
        write_jsonl(path, rows)
        artifact_paths.append(path)

    source_receipt = {
        "schema_version": 1,
        "source": "RQData Python API",
        "observed_at_utc": observed_at,
        "business_api_calls": 2,
        "requests": [
            {"method": "all_instruments", "parameters": {"type": "CS", "date": None}},
            {
                "method": "get_trading_dates",
                "parameters": {
                    "start_date": BASE_START.isoformat(),
                    "end_date": COMPLETE_THROUGH.isoformat(),
                },
            },
        ],
        "credential_value_captured": False,
        "license_value_captured": False,
        "payload_fields_saved": [
            "order_book_id",
            "symbol",
            "special_type",
            "exchange/mic",
            "status",
            "type",
            "listed_date",
            "de_listed_date",
            "board_type",
        ],
    }
    source_path = stage / "SOURCE_RECEIPT.json"
    write_json(source_path, source_receipt)
    artifact_paths.append(source_path)

    result = {
        "schema_version": 1,
        "run_id": "rqdata-r0-shsz-core01-core02-candidate-20260804-v1",
        "status": "PASS__BOUNDED_QUARANTINE_CANDIDATE__FORMAL_EFFECT_NONE",
        "purpose_ceiling": "INSPECTION_ONLY",
        "quality_status": "NOT_ADMITTED",
        "formal_effect": "NONE",
        "catalog_registered": False,
        "research_ready": False,
        "observed_at_utc": observed_at,
        "scope": contract["scope"],
        "master": {**master_stats, "episode_id_rows": len(master_rows)},
        "baseline_episode_join": episode_stats,
        "old_exclusion_join": exclusion_stats,
        "still_excluded_keys": [item["key"] for item in remaining],
        "calendar": calendar_result,
        "survivorship_interpretation": (
            "367/375 old excluded keys obtain a unique RQData master interval; this improves "
            "bounded episode coverage but does not prove permanent identity or historical PIT attributes."
        ),
        "semantic_limits": contract["semantic_limits"],
    }
    result_path = stage / "BUILD_RESULT.json"
    write_json(result_path, result)
    artifact_paths.append(result_path)

    input_receipts = [file_receipt(CONTRACT_PATH)] + [
        file_receipt(path) for path in inputs.values()
    ]
    artifacts = []
    for path in sorted(artifact_paths):
        artifacts.append(
            {
                "path": path.relative_to(stage).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = {
        "schema_version": 1,
        "run_id": result["run_id"],
        "manifest_kind": "RQDATA_SHSZ_CORE01_CORE02_INSPECTION_CANDIDATE",
        "purpose_ceiling": "INSPECTION_ONLY",
        "quality_status": "NOT_ADMITTED",
        "source_admission": False,
        "catalog_registered": False,
        "formal_effect": "NONE",
        "research_ready": False,
        "credential_value_captured": False,
        "inputs": input_receipts,
        "artifacts": artifacts,
    }
    manifest_path = stage / "CANDIDATE_MANIFEST.json"
    write_json(manifest_path, manifest)
    _publish_stage(stage, candidate_target)

    report_stage = report_target.parent / f".{report_target.name}.staging-{uuid.uuid4().hex}"
    report_stage.mkdir(parents=True, exist_ok=False)
    candidate_manifest = candidate_target / "CANDIDATE_MANIFEST.json"
    report_result = {
        **result,
        "candidate_manifest": file_receipt(candidate_manifest),
        "writes_outside_candidate_and_report": 0,
        "datasets_json_modified": False,
        "roadmap_modified": False,
        "source_registry_modified": False,
        "factor_runs": 0,
    }
    report_result_path = report_stage / "RESULT.json"
    write_json(report_result_path, report_result)
    readme = f"""# RQData 沪深 CORE01/02 有界候选回执

- 结论：证据构建 `PASS`，正式准入仍为 `NOT_ADMITTED`。
- 范围：{BASE_START.isoformat()}—{BASE_END.isoformat()} 基座；{INCREMENTAL_START.isoformat()}—{COMPLETE_THROUGH.isoformat()} 增量。
- 主档：范围内 {len(master_rows)} 条，每条具有确定性 `episode_id` 和 RQData 上市/退市字段。
- 旧排除账本：{exclusion_stats['exact_unique_matches']}/375 精确唯一匹配；仍排除 {exclusion_stats['still_unmatched']} 键。
- 日历：2005—2024 的 {calendar_result['baseline_existing_count']} 日期集合完全一致；2025—2026 增量 {calendar_result['incremental_2025_2026_count']} 日期。
- 限制：RQData `date=None` 是最新属性主档，不是历史 PIT 主档；本候选不能运行因子或打开正式 R0 门。
"""
    readme_path = report_stage / "README.md"
    readme_path.write_text(readme, encoding="utf-8", newline="\n")
    report_manifest = {
        "schema_version": 1,
        "report_id": "rqdata-r0-shsz-core01-core02-20260804-v1",
        "candidate_manifest": file_receipt(candidate_manifest),
        "artifacts": [
            {
                "path": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in (readme_path, report_result_path)
        ],
    }
    write_json(report_stage / "REPORT_MANIFEST.json", report_manifest)
    _publish_stage(report_stage, report_target)
    return report_result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a bounded RQData SH/SZ CORE01/02 quarantine candidate."
    )
    parser.add_argument("--candidate-output", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--report-output", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build_candidate(args.candidate_output, args.report_output)
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": safe_error(exc)}, ensure_ascii=False))
        return 2
    summary = {
        "status": result["status"],
        "master_rows": result["master"]["episode_id_rows"],
        "old_exclusion_exact_matches": result["old_exclusion_join"]["exact_unique_matches"],
        "old_exclusions_still_unmatched": result["old_exclusion_join"]["still_unmatched"],
        "calendar": result["calendar"],
        "credential_value_captured": False,
        "formal_effect": "NONE",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
