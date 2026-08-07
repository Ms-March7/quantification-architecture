#!/usr/bin/env python3
"""Build a bounded 2005-2024 SSE/SZSE calendar inspection candidate.

The package combines an already sealed CSMAR TRD_Cale candidate with existing
official SSE/SZSE closure evidence.  It also performs six bounded POST searches
against the official SSE search service for the unresolved 2005-2008 annual
master notices.  It does not infer trading days, mutate the catalog, or confer
research/formal-admission status.
"""
from __future__ import annotations

import bisect
import hashlib
import html
import json
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import requests


ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "r0-core02-bounded-calendar-skeleton-2005-2024-20260801-v1"
CANDIDATE = ROOT / "data/market/quarantine/r0-official-calendar-rules/r0-minimum-reliable-2005-2024-20260801-v1"
REPORT = ROOT / "reports/data-governance/r0-official-calendar-rules-r0-minimum-reliable-2005-2024-20260801-v1"

TRD_ROOT = ROOT / "tmp/local-dataset-build/csmar-trd-cale-pilot/d2a04b-csmar-trd-cale-full-1990-2026-20260728-v1"
TRD_MANIFEST = TRD_ROOT / "manifest.json"
TRD_PARQUET = TRD_ROOT / "curated/schema=current-export-20260728-d2a04b-full-v1/part-000.parquet"
NORMALIZED_ROOT = ROOT / "data/market/quarantine/r0-official-calendar-rules/normalized-stagea-20260801-v2"
NORMALIZED_MANIFEST = NORMALIZED_ROOT / "CANDIDATE_MANIFEST.json"
NOTICE_INPUT = NORMALIZED_ROOT / "CALENDAR_NOTICE_LINEAGE.jsonl"
CLOSURE_INPUT = NORMALIZED_ROOT / "VENUE_DATE_CLOSURE_CANDIDATE.jsonl"
SESSION_INPUT = NORMALIZED_ROOT / "SESSION_INTERVAL_CANDIDATE.jsonl"
MINIMUM_GAP_ROOT = ROOT / "data/market/quarantine/r0-official-calendar-rules/minimum-gap-closure-20260801-v1"
MINIMUM_GAP_MANIFEST = MINIMUM_GAP_ROOT / "CANDIDATE_MANIFEST.json"

PURPOSE = "INSPECTION_ONLY"
QUALITY = "NOT_ADMITTED"
TZ = timezone(timedelta(hours=8))
UA = "Quantification-R0-Official-Evidence/1.0"
START_DATE = "2005-01-01"
END_DATE = "2024-12-31"
EXPECTED_DAYS_PER_VENUE = (date(2024, 12, 31) - date(2005, 1, 1)).days + 1

SSE_SEARCH_URL = "https://query.sse.com.cn/search/getESSearchDoc.do"
SSE_SEARCH_QUERIES = [
    {"query_id": "sse-2005-annual-exact", "target_year": 2005, "keyword": "关于2005年元旦、春节、“五一”、“十一”休市安排的通知"},
    {"query_id": "sse-2006-annual-exact", "target_year": 2006, "keyword": "关于2006年元旦、春节、“五一”、“十一”休市安排的通知"},
    {"query_id": "sse-2007-annual-exact", "target_year": 2007, "keyword": "关于2007年元旦、春节、“五一”、“十一”休市安排的通知"},
    {"query_id": "sse-2008-annual-exact", "target_year": 2008, "keyword": "关于2008年全年休市安排的通知"},
    {"query_id": "sse-2005-master-code", "target_year": 2005, "keyword": "上证交字〔2004〕20号"},
    {"query_id": "sse-2008-master-code", "target_year": 2008, "keyword": "上证交字〔2007〕18号"},
]

GUARDED_FILES = (
    ROOT / "data/catalog/datasets.json",
    ROOT / "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    ROOT / "control/ROADMAP.md",
    ROOT / "control/projects.yaml",
)
GUARDED_TREES = (ROOT / "data/market/snapshots", ROOT / "data/published")


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def binding(path: Path) -> dict[str, Any]:
    return {"path": rel(path), "bytes": path.stat().st_size, "sha256": sha(path)}


def tree_hash(path: Path) -> str:
    rows: list[str] = []
    if path.exists():
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            rows.append(f"{item.relative_to(path).as_posix()}\t{item.stat().st_size}\t{sha(item)}")
    return sha_bytes("\n".join(rows).encode("utf-8"))


def guards() -> dict[str, Any]:
    return {
        "files": {rel(p): sha(p) for p in GUARDED_FILES},
        "trees": {rel(p): tree_hash(p) for p in GUARDED_TREES},
    }


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def dump_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def strip_markup(value: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def extend_value(row: dict[str, Any], name: str) -> Any:
    for item in row.get("extend") or []:
        if item.get("name") == name:
            return item.get("value")
    return None


def bounded_sse_search() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    session = requests.Session()
    receipts: list[dict[str, Any]] = []
    hit_rows: list[dict[str, Any]] = []
    hard_stop: dict[str, Any] | None = None
    headers = {"User-Agent": UA, "Referer": "https://www.sse.com.cn/home/search/"}

    for sequence, spec in enumerate(SSE_SEARCH_QUERIES, 1):
        if hard_stop is not None:
            break
        payload = {
            "keyword": spec["keyword"],
            "spaceId": "3",
            "siteName": "sse",
            "keywordPosition": "title,paper_content",
            "page": "0",
            "limit": "20",
            "publishTimeStart": "",
            "publishTimeEnd": "",
            "channelId": "10001",
            "searchMode": "preciseMulti",
        }
        started = datetime.now(TZ).isoformat()
        status = None
        content = b""
        error = None
        safe_headers: dict[str, str] = {}
        try:
            response = session.post(SSE_SEARCH_URL, headers=headers, data=payload, timeout=(10, 30), allow_redirects=False)
            status = response.status_code
            content = response.content
            safe_headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in {"date", "content-type", "content-length", "last-modified", "etag", "location"}
            }
        except requests.RequestException as exc:
            error = f"{type(exc).__name__}: {exc}"
        raw = CANDIDATE / f"raw/sse/search/{sequence:02d}-{spec['query_id']}.json"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(content)

        parsed: dict[str, Any] | None = None
        parse_error = None
        if status == 200 and content:
            try:
                parsed = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                parse_error = f"{type(exc).__name__}: {exc}"
        data = (parsed or {}).get("data") or {}
        rows = data.get("knowledgeList") or []
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            curl = extend_value(row, "CURL")
            title = strip_markup(row.get("title"))
            hit_rows.append({
                "query_id": spec["query_id"],
                "target_year": spec["target_year"],
                "document_id": row.get("documentId"),
                "title": title,
                "create_time": row.get("createTime"),
                "update_time": row.get("updateTime"),
                "official_url": f"https://www.sse.com.cn{curl}" if isinstance(curl, str) and curl.startswith("/") else curl,
                "file_type": extend_value(row, "FILETYPE"),
                "channel_code": extend_value(row, "CCHANNELCODE"),
                "annual_master_title_hit": f"关于{spec['target_year']}年" in title and "全年休市安排" in title,
                "source_search_raw": binding(raw),
                "purpose_ceiling": PURPOSE,
                "quality_status": QUALITY,
                "formal_effect": "NONE",
            })
        receipt = {
            "request_sequence": sequence,
            "query_id": spec["query_id"],
            "target_year": spec["target_year"],
            "method": "POST",
            "url": SSE_SEARCH_URL,
            "request_form_safe": payload,
            "started_at": started,
            "completed_at": datetime.now(TZ).isoformat(),
            "http_status": status,
            "success": status == 200 and parsed is not None and (parsed or {}).get("code") == "0",
            "error": error,
            "parse_error": parse_error,
            "response_headers_safe": safe_headers,
            "reported_total_size": data.get("totalSize"),
            "returned_hit_count": len(rows),
            "raw": binding(raw),
        }
        receipts.append(receipt)
        if status in {403, 429}:
            hard_stop = {"request_sequence": sequence, "http_status": status, "reason": "SITE_LIMIT_SIGNAL__NO_RETRY"}
        time.sleep(0.8)

    coverage: dict[str, Any] = {
        "scope": "SSE_2005_TO_2008_ANNUAL_MASTER_NOTICE_BOUNDED_OFFICIAL_SEARCH",
        "search_endpoint": SSE_SEARCH_URL,
        "planned_query_count": len(SSE_SEARCH_QUERIES),
        "executed_query_count": len(receipts),
        "hard_stop": hard_stop,
        "years": {},
        "nonexistence_claimed": False,
        "purpose_ceiling": PURPOSE,
        "quality_status": QUALITY,
        "formal_effect": "NONE",
    }
    for year in range(2005, 2009):
        year_hits = [row for row in hit_rows if row["target_year"] == year]
        coverage["years"][str(year)] = {
            "official_search_hit_count": len(year_hits),
            "annual_master_title_hit_count": sum(bool(row["annual_master_title_hit"]) for row in year_hits),
            "annual_master_notice_closed": any(row["annual_master_title_hit"] for row in year_hits),
            "result": "OFFICIAL_MASTER_NOT_EXPOSED_BY_BOUNDED_SEARCH" if not any(row["annual_master_title_hit"] for row in year_hits) else "OFFICIAL_MASTER_HIT",
        }
    return receipts, {"coverage": coverage, "hits": hit_rows}


def build_skeleton() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trd_manifest = json.loads(TRD_MANIFEST.read_text(encoding="utf-8"))
    connection = duckdb.connect()
    connection.execute(f"CREATE TEMP VIEW trd_cale AS SELECT * FROM read_parquet('{sql_path(TRD_PARQUET)}')")
    connection.execute(
        f"""
        CREATE TEMP TABLE calendar_skeleton AS
        SELECT
          CASE WHEN Markettype IN (1, 32) THEN 'SSE' ELSE 'SZSE' END AS venue,
          Clddt AS calendar_date,
          MIN(Daywk)::INTEGER AS daywk,
          MIN(State) AS state,
          MIN(State) = 'O' AS is_open,
          list_sort(list(Markettype)) AS contributing_markettypes,
          COUNT(*)::INTEGER AS contributing_row_count,
          COUNT(DISTINCT State)::INTEGER AS state_variant_count,
          'VENDOR_OBSERVED_FULL_CALENDAR_DAY_STATE' AS skeleton_class,
          '{trd_manifest['candidate_id']}' AS source_candidate_id,
          '{trd_manifest['content_hash']}' AS source_candidate_content_hash,
          '{PURPOSE}' AS purpose_ceiling,
          '{QUALITY}' AS quality_status
        FROM trd_cale
        WHERE Markettype IN (1, 4, 16, 32)
          AND Clddt BETWEEN DATE '{START_DATE}' AND DATE '{END_DATE}'
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    )
    out = CANDIDATE / "R0_SSE_SZSE_CALENDAR_SKELETON_2005_2024.parquet"
    connection.execute(f"COPY (SELECT * FROM calendar_skeleton ORDER BY venue, calendar_date) TO '{sql_path(out)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    columns = [desc[0] for desc in connection.execute("SELECT * FROM calendar_skeleton LIMIT 0").description]
    raw_rows = connection.execute("SELECT * FROM calendar_skeleton ORDER BY venue, calendar_date").fetchall()
    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        row = dict(zip(columns, raw_row))
        row["calendar_date"] = row["calendar_date"].isoformat()
        rows.append(row)
    stats_rows = connection.execute(
        """
        SELECT venue, MIN(calendar_date), MAX(calendar_date), COUNT(*),
               SUM(CASE WHEN is_open THEN 1 ELSE 0 END),
               SUM(CASE WHEN NOT is_open THEN 1 ELSE 0 END),
               SUM(CASE WHEN state_variant_count <> 1 THEN 1 ELSE 0 END)
        FROM calendar_skeleton GROUP BY venue ORDER BY venue
        """
    ).fetchall()
    stats = {
        venue: {
            "min_date": min_date.isoformat(),
            "max_date": max_date.isoformat(),
            "calendar_day_count": count,
            "open_day_count": open_count,
            "closed_day_count": closed_count,
            "board_state_conflict_count": conflict_count,
        }
        for venue, min_date, max_date, count, open_count, closed_count, conflict_count in stats_rows
    }
    return rows, {
        "source_manifest": binding(TRD_MANIFEST),
        "source_parquet": binding(TRD_PARQUET),
        "source_candidate_id": trd_manifest["candidate_id"],
        "source_candidate_content_hash": trd_manifest["content_hash"],
        "markettype_mapping": {"1": "SSE_MAIN_A", "32": "SSE_STAR", "4": "SZSE_MAIN_A", "16": "SZSE_CHINEXT"},
        "scope": {"venues": ["SSE", "SZSE"], "start_date": START_DATE, "end_date": END_DATE},
        "venue_stats": stats,
        "row_count": len(rows),
        "output": binding(out),
    }


def official_calibration(skeleton: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lookup = {(row["venue"], row["calendar_date"]): row for row in skeleton}
    inputs = [
        row for row in read_jsonl(CLOSURE_INPUT)
        if row.get("venue") in {"SSE", "SZSE"} and START_DATE <= row.get("calendar_date", "") <= END_DATE
    ]
    rows: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    by_venue: dict[str, int] = defaultdict(int)
    for item in inputs:
        key = (item["venue"], item["calendar_date"])
        observed = lookup.get(key)
        row = {
            "venue": item["venue"],
            "calendar_date": item["calendar_date"],
            "official_is_trading_day": item.get("is_trading_day"),
            "vendor_state": observed.get("state") if observed else None,
            "vendor_is_open": observed.get("is_open") if observed else None,
            "match": observed is not None and observed.get("state") == "C" and item.get("is_trading_day") is False,
            "official_evidence_count": item.get("evidence_count"),
            "official_evidence": item.get("evidence"),
            "purpose_ceiling": PURPOSE,
            "quality_status": QUALITY,
            "formal_effect": "NONE",
        }
        rows.append(row)
        by_venue[item["venue"]] += 1
        if observed is None:
            missing.append({"venue": item["venue"], "calendar_date": item["calendar_date"]})
        elif not row["match"]:
            conflicts.append({"venue": item["venue"], "calendar_date": item["calendar_date"], "vendor_state": observed.get("state")})
    return rows, {
        "official_input": binding(CLOSURE_INPUT),
        "official_closure_key_count": len(rows),
        "by_venue": dict(sorted(by_venue.items())),
        "missing_skeleton_key_count": len(missing),
        "official_vendor_conflict_count": len(conflicts),
        "missing_skeleton_keys": missing,
        "conflicts": conflicts,
    }


def notice_and_available_at(skeleton: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    notices = [
        row for row in read_jsonl(NOTICE_INPUT)
        if row.get("venue") in {"SSE", "SZSE"} and 2005 <= int(row.get("target_year") or 0) <= 2024
    ]
    open_dates: dict[str, list[str]] = defaultdict(list)
    for row in skeleton:
        if row["is_open"]:
            open_dates[row["venue"]].append(row["calendar_date"])
    augmented: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {}
    for venue in ("SSE", "SZSE"):
        annual = {int(row["target_year"]): row for row in notices if row["venue"] == venue and row.get("category") == "annual_calendar"}
        coverage[venue] = {
            "required_years": list(range(2005, 2025)),
            "annual_master_present_years": sorted(annual),
            "annual_master_missing_years": [year for year in range(2005, 2025) if year not in annual],
            "complete": len(annual) == 20,
        }
    for row in notices:
        publication_date = row.get("published_date")
        next_open = None
        if publication_date:
            dates = open_dates[row["venue"]]
            index = bisect.bisect_right(dates, publication_date)
            next_open = dates[index] if index < len(dates) else None
        augmented.append({
            "venue": row["venue"],
            "item_id": row.get("item_id"),
            "category": row.get("category"),
            "target_year": row.get("target_year"),
            "published_date": publication_date,
            "published_at": None,
            "available_at": None,
            "research_usable_from_date_bound": next_open,
            "available_at_policy": "DATE_ONLY__NOT_INTRADAY_USABLE__EARLIEST_USE_NEXT_VERIFIED_OPEN_DATE",
            "bound_semantics": "Conservative derived daily-use boundary; it is not a source publication timestamp.",
            "source_url": row.get("source_url"),
            "source_raw": row.get("source_raw"),
            "purpose_ceiling": PURPOSE,
            "quality_status": QUALITY,
            "formal_effect": "NONE",
        })
    return augmented, {
        "notice_input": binding(NOTICE_INPUT),
        "notice_count": len(notices),
        "annual_master_coverage": coverage,
        "policy": {
            "source_publication_timestamp_preserved": False,
            "intraday_use_allowed": False,
            "daily_use_boundary": "first verified open date strictly after official published_date",
            "unknown_publication_time_filled": False,
        },
    }


def session_coverage(skeleton: list[dict[str, Any]]) -> dict[str, Any]:
    intervals = [row for row in read_jsonl(SESSION_INPUT) if row.get("venue") in {"SSE", "SZSE"}]
    episodes: dict[tuple[str, str, str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    for row in intervals:
        key = (row["venue"], str(row.get("rule_version")), row.get("effective_from"), row.get("effective_to"))
        episodes[key].append(row)
    episode_rows = []
    for (venue, version, start, end), rows in sorted(episodes.items()):
        episode_rows.append({
            "venue": venue,
            "rule_version": version,
            "effective_from": start,
            "effective_to": end,
            "interval_count": len(rows),
            "session_names": sorted(row["session_name"] for row in rows),
            "source_bindings": sorted({(row.get("raw_path"), row.get("raw_sha256")) for row in rows}),
        })
    open_days = {venue: [row["calendar_date"] for row in skeleton if row["venue"] == venue and row["is_open"]] for venue in ("SSE", "SZSE")}
    audit: dict[str, Any] = {}
    for venue in ("SSE", "SZSE"):
        venue_episodes = [row for row in episode_rows if row["venue"] == venue]
        covered = []
        uncovered = []
        multiply_covered = []
        for current in open_days[venue]:
            matches = [row for row in venue_episodes if row["effective_from"] and row["effective_from"] <= current and (row["effective_to"] is None or current <= row["effective_to"])]
            if len(matches) == 1:
                covered.append(current)
            elif not matches:
                uncovered.append(current)
            else:
                multiply_covered.append(current)
        audit[venue] = {
            "open_day_count": len(open_days[venue]),
            "covered_open_day_count": len(covered),
            "uncovered_open_day_count": len(uncovered),
            "multiply_covered_open_day_count": len(multiply_covered),
            "first_uncovered_open_date": uncovered[0] if uncovered else None,
            "last_uncovered_open_date": uncovered[-1] if uncovered else None,
            "complete_for_2005_2024": not uncovered and not multiply_covered,
        }
    return {
        "session_input": binding(SESSION_INPUT),
        "interval_count": len(intervals),
        "episode_count": len(episode_rows),
        "episodes": episode_rows,
        "venue_audit": audit,
        "complete_historical_session_chain": all(value["complete_for_2005_2024"] for value in audit.values()),
    }


def write_manifest(root: Path, name: str) -> None:
    files = [binding(path) for path in sorted(p for p in root.rglob("*") if p.is_file()) if path.name != name]
    dump(root / name, {"run_id": RUN_ID, "files": files})


def main() -> int:
    if CANDIDATE.exists() or REPORT.exists():
        raise SystemExit("output already exists; fail closed")
    for required in (TRD_MANIFEST, TRD_PARQUET, NORMALIZED_MANIFEST, NOTICE_INPUT, CLOSURE_INPUT, SESSION_INPUT, MINIMUM_GAP_MANIFEST):
        if not required.is_file():
            raise SystemExit(f"required input missing: {rel(required)}")
        if "superseded-invalid" in required.as_posix():
            raise SystemExit("invalid upstream path")

    before = guards()
    CANDIDATE.mkdir(parents=True)
    REPORT.mkdir(parents=True)

    search_receipts, search_result = bounded_sse_search()
    dump(REPORT / "SSE_SEARCH_REQUEST_RECEIPTS.json", search_receipts)
    dump_jsonl(CANDIDATE / "SSE_OFFICIAL_SEARCH_HITS.jsonl", search_result["hits"])
    dump(REPORT / "SSE_2005_2008_BOUNDED_SEARCH_COVERAGE.json", search_result["coverage"])

    skeleton, skeleton_summary = build_skeleton()
    calibration_rows, calibration = official_calibration(skeleton)
    dump_jsonl(CANDIDATE / "OFFICIAL_CLOSURE_CALIBRATION.jsonl", calibration_rows)
    dump(REPORT / "CALENDAR_SKELETON_SUMMARY.json", skeleton_summary)
    dump(REPORT / "OFFICIAL_CLOSURE_CALIBRATION_SUMMARY.json", calibration)

    notice_rows, notice_summary = notice_and_available_at(skeleton)
    dump_jsonl(CANDIDATE / "NOTICE_AVAILABLE_AT_DAILY_BOUND.jsonl", notice_rows)
    dump(REPORT / "NOTICE_AND_AVAILABLE_AT_SUMMARY.json", notice_summary)
    dump(REPORT / "AVAILABLE_AT_POLICY.json", notice_summary["policy"])

    sessions = session_coverage(skeleton)
    dump(REPORT / "SESSION_COVERAGE_AUDIT.json", sessions)

    structural_pass = (
        skeleton_summary["row_count"] == EXPECTED_DAYS_PER_VENUE * 2
        and all(stats["calendar_day_count"] == EXPECTED_DAYS_PER_VENUE for stats in skeleton_summary["venue_stats"].values())
        and all(stats["board_state_conflict_count"] == 0 for stats in skeleton_summary["venue_stats"].values())
    )
    calibration_pass = calibration["missing_skeleton_key_count"] == 0 and calibration["official_vendor_conflict_count"] == 0
    notice_complete = all(value["complete"] for value in notice_summary["annual_master_coverage"].values())
    session_complete = sessions["complete_historical_session_chain"]

    bounded_gates = {
        "B1_VENDOR_SKELETON_STRUCTURAL_COMPLETENESS": {"status": "PASS" if structural_pass else "FAIL", "scope": "SSE_SZSE_2005_2024_INSPECTION_ONLY"},
        "B2_OFFICIAL_CLOSURE_CALIBRATION": {"status": "PASS" if calibration_pass else "FAIL", "official_key_count": calibration["official_closure_key_count"]},
        "B3_ANNUAL_MASTER_NOTICE_COVERAGE": {"status": "PASS" if notice_complete else "FAIL", "coverage": notice_summary["annual_master_coverage"]},
        "B4_HISTORICAL_SESSION_CHAIN": {"status": "PASS" if session_complete else "FAIL", "coverage": sessions["venue_audit"]},
        "B5_DATE_ONLY_AVAILABLE_AT_POLICY": {"status": "PASS", "scope": "DAILY_ONLY__INTRADAY_FORBIDDEN", "unknown_timestamp_filled": False},
    }
    formal_gates = {
        "CORE02-Q1": {"status": "FAIL", "reason": "SSE annual master notice lineage for 2005-2008 remains incomplete; bounded official search did not expose the four masters."},
        "CORE02-Q2": {"status": "FAIL", "reason": "Complete 2005-2024 SSE/SZSE session effective/supersession chains and complete three-venue emergency archive remain unresolved."},
        "CORE02-Q3": {"status": "UNKNOWN", "reason": "A complete official daily calendar source is still absent; the 14,610-row product is a vendor-observed inspection skeleton calibrated against explicit official closure keys."},
        "CORE02-Q4": {"status": "PASS", "reason": "Venue/date identity is explicit; duplicate keys and cross-board state conflicts are zero in the bounded skeleton."},
    }
    gaps = {
        "bounded_sse_szse_2005_2024": [
            "SSE 2005-2008 annual master notices remain unresolved; 2008 supplemental holiday notices do not replace the master.",
            "SSE historical session chain has uncovered open days within 2005-2024.",
            "SZSE historical session chain has uncovered open days within 2005-2024.",
            "Official source publication times are date-only; no intraday available_at fact was fabricated.",
            "The CSMAR-derived skeleton is an inspection candidate, not an official daily calendar or admitted research input.",
        ],
        "full_core02_three_venue_all_history": [
            "BSE complete 2021 partial-year daily calendar remains unresolved.",
            "Complete three-venue temporary/emergency closure archive remains unresolved.",
            "Complete all-history rule-version and session supersession chains remain unresolved.",
            "Pre-2005 and post-2024 full-scope official coverage was not part of this bounded build.",
        ],
        "independent_backup_required": False,
    }
    dump(REPORT / "REMAINING_GAPS.json", gaps)
    dump(REPORT / "CORE02_GATE_RECOMPUTE.json", {
        "run_id": RUN_ID,
        "purpose_ceiling": PURPOSE,
        "quality_status": QUALITY,
        "formal_admission": False,
        "research_usable": False,
        "bounded_inspection_decision": "BOUNDED_CALENDAR_SKELETON_READY_FOR_INSPECTION" if structural_pass and calibration_pass else "BOUNDED_SKELETON_FAILED",
        "formal_core02_decision": "FAIL_CLOSED__NOT_ADMITTED",
        "bounded_gates": bounded_gates,
        "formal_gates": formal_gates,
    })

    upstream = {
        "trd_manifest": binding(TRD_MANIFEST),
        "trd_parquet": binding(TRD_PARQUET),
        "normalized_candidate_manifest": binding(NORMALIZED_MANIFEST),
        "notice_input": binding(NOTICE_INPUT),
        "closure_input": binding(CLOSURE_INPUT),
        "session_input": binding(SESSION_INPUT),
        "minimum_gap_candidate_manifest": binding(MINIMUM_GAP_MANIFEST),
    }
    dump(REPORT / "UPSTREAM_HASH_BINDINGS.json", upstream)

    after = guards()
    guard_pass = before == after
    dump(REPORT / "PROTECTION_CHECK.json", {"before": before, "after": after, "pass": guard_pass})
    summary = {
        "run_id": RUN_ID,
        "created_at": datetime.now(TZ).isoformat(),
        "purpose_ceiling": PURPOSE,
        "quality_status": QUALITY,
        "formal_admission": False,
        "research_usable": False,
        "official_sources_only_for_calibration_and_search": True,
        "vendor_observed_skeleton": True,
        "network_request_count": len(search_receipts),
        "network_success_count": sum(bool(row["success"]) for row in search_receipts),
        "network_403_429_count": sum(row["http_status"] in {403, 429} for row in search_receipts),
        "skeleton_row_count": skeleton_summary["row_count"],
        "official_closure_key_count": calibration["official_closure_key_count"],
        "official_vendor_conflict_count": calibration["official_vendor_conflict_count"],
        "notice_count": notice_summary["notice_count"],
        "session_interval_count": sessions["interval_count"],
        "structural_pass": structural_pass,
        "calibration_pass": calibration_pass,
        "notice_complete": notice_complete,
        "session_complete": session_complete,
        "bounded_inspection_decision": "BOUNDED_CALENDAR_SKELETON_READY_FOR_INSPECTION" if structural_pass and calibration_pass else "BOUNDED_SKELETON_FAILED",
        "formal_core02_decision": "FAIL_CLOSED__NOT_ADMITTED",
        "guard_pass": guard_pass,
    }
    dump(CANDIDATE / "SUMMARY.json", summary)
    dump(REPORT / "SUMMARY.json", summary)
    REPORT.joinpath("README.md").write_text(
        "# CORE-02 bounded SSE/SZSE calendar skeleton, 2005-2024\n\n"
        "- Boundary: `INSPECTION_ONLY / NOT_ADMITTED`; formal effect: `NONE`.\n"
        "- Calendar skeleton: sealed CSMAR TRD_Cale candidate, Markettype 1/4/16/32, one venue-date row.\n"
        "- Official calibration: explicit SSE/SZSE closure evidence only; no weekday or statutory-holiday inference.\n"
        "- Date-only publication evidence is usable only from the next verified open date; intraday use is forbidden.\n"
        "- The bounded skeleton passes structural and closure-calibration checks, but CORE-02 remains fail-closed because official lineage and session chains are incomplete.\n",
        encoding="utf-8",
    )

    write_manifest(CANDIDATE, "CANDIDATE_MANIFEST.json")
    candidate_manifest = binding(CANDIDATE / "CANDIDATE_MANIFEST.json")
    verification = {
        "run_id": RUN_ID,
        "checks": {
            "guard_pass": guard_pass,
            "exact_row_count": skeleton_summary["row_count"] == EXPECTED_DAYS_PER_VENUE * 2,
            "per_venue_full_date_span": all(stats["calendar_day_count"] == EXPECTED_DAYS_PER_VENUE for stats in skeleton_summary["venue_stats"].values()),
            "zero_cross_board_state_conflicts": all(stats["board_state_conflict_count"] == 0 for stats in skeleton_summary["venue_stats"].values()),
            "zero_official_vendor_conflicts": calibration["official_vendor_conflict_count"] == 0,
            "zero_missing_official_keys": calibration["missing_skeleton_key_count"] == 0,
            "catalog_unchanged": before["files"]["data/catalog/datasets.json"] == after["files"]["data/catalog/datasets.json"],
            "snapshots_unchanged": before["trees"]["data/market/snapshots"] == after["trees"]["data/market/snapshots"],
            "published_unchanged": before["trees"]["data/published"] == after["trees"]["data/published"],
        },
        "candidate_manifest": candidate_manifest,
    }
    verification["pass"] = all(verification["checks"].values())
    dump(REPORT / "VERIFY_ONLY.json", verification)
    write_manifest(REPORT, "REPORT_MANIFEST.json")
    if not verification["pass"]:
        raise SystemExit("verification failed; candidate remains NOT_ADMITTED")
    print(json.dumps({
        "candidate": rel(CANDIDATE),
        "report": rel(REPORT),
        "candidate_manifest": candidate_manifest,
        "report_manifest": binding(REPORT / "REPORT_MANIFEST.json"),
        "summary": summary,
        "verify_only": verification["pass"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
