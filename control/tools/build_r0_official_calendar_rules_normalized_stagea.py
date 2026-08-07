#!/usr/bin/env python3
"""Normalize the 2026-08-01 official calendar/rule intake, fail closed.

Outputs are strictly INSPECTION_ONLY / NOT_ADMITTED.  Only dates explicitly
described as closed by an official notice are emitted; this program never
infers an exchange calendar from statutory holidays or weekdays.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
import requests


ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "r0-official-calendar-rules-normalized-stagea-20260801-v2"
UPSTREAM = ROOT / "data/market/quarantine/r0-official-calendar-rules/intake-20260801-v1"
CANDIDATE = ROOT / "data/market/quarantine/r0-official-calendar-rules/normalized-stagea-20260801-v2"
REPORT = ROOT / "reports/data-governance/r0-official-calendar-rules-normalized-stagea-20260801-v2"
SESSION_INPUT = ROOT / "tmp/d2a04b-source-closure/stage-a-core02-calendar-session-closure-20260730-v1/SESSION_INTERVAL_EVIDENCE.jsonl"
PURPOSE = "INSPECTION_ONLY"
QUALITY = "NOT_ADMITTED"
TZ = timezone(timedelta(hours=8))
GUARDED_FILES = (
    ROOT / "data/catalog/datasets.json",
    ROOT / "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    ROOT / "control/ROADMAP.md",
    ROOT / "control/projects.yaml",
)
GUARDED_TREES = (ROOT / "data/market/snapshots", ROOT / "data/published")
DATE_TOKEN = re.compile(
    r"(?:(?P<year>20\d{2})年)?(?:(?P<month>1[0-2]|0?[1-9])月)?(?P<day>3[01]|[12]\d|0?[1-9])日"
)
RANGE_MARK = re.compile(r"(?:至|到|—|–|－|--|~|～)")
BSE_ENDPOINT = "https://www.bse.cn/info/listse.do?callback=cb"
BSE_HEADERS = {
    "Referer": "https://www.bse.cn/news/important_news.html",
    "Origin": "https://www.bse.cn",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Quantification-R0-Official-Evidence/1.0",
    "Accept": "*/*",
}


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def binding(path: Path) -> dict[str, Any]:
    return {
        "path": path.resolve().relative_to(ROOT.resolve()).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha(path),
    }


def tree_hash(path: Path) -> str:
    rows = []
    if path.exists():
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            rows.append(f"{item.relative_to(path).as_posix()}\t{item.stat().st_size}\t{sha(item)}")
    return sha_bytes("\n".join(rows).encode("utf-8"))


def guards() -> dict[str, Any]:
    return {
        "files": {p.relative_to(ROOT).as_posix(): sha(p) for p in GUARDED_FILES},
        "trees": {p.relative_to(ROOT).as_posix(): tree_hash(p) for p in GUARDED_TREES},
    }


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def dump_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def visible_text(path: Path) -> str:
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.stripped_strings)


def collect_bse_annual_notices() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect the bounded official BSE search response and 2022-2024 masters.

    BSE ignores the requested pageSize and currently returns 20 rows per page,
    so exactly two pages are requested after page 0 reports 31 matches.  The
    2021-2024 publication window is applied locally from the official
    publishDate field because the endpoint returned zero rows when start/end
    fields were supplied in this environment.
    """
    session = requests.Session()
    receipts: list[dict[str, Any]] = []
    entries: dict[int, dict[str, Any]] = {}
    raw_root = CANDIDATE / "raw/bse/search"
    fields = [("nodeIds[]", "1289")]
    fields.extend(("needFields[]", name) for name in (
        "infoId", "title", "metaDescription", "linkUrl", "htmlUrl", "publishDate"
    ))
    total_pages = 1
    for page in range(2):
        if page >= total_pages:
            break
        started = datetime.now(TZ).isoformat()
        response = session.post(
            BSE_ENDPOINT,
            data=[("page", str(page)), ("pageSize", "100"), ("keywords", "休市安排"), *fields],
            headers=BSE_HEADERS,
            timeout=(10, 30),
            allow_redirects=False,
        )
        raw = response.content
        path = raw_root / f"page-{page}.jsonp"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        receipt = {
            "request_id": f"bse-official-search-page-{page}",
            "method": "POST",
            "url": BSE_ENDPOINT,
            "page": page,
            "keywords": "休市安排",
            "node_id": 1289,
            "requested_publication_window": ["2021-01-01", "2024-12-31"],
            "window_application": "LOCAL_FILTER_ON_OFFICIAL_publishDate",
            "started_at": started,
            "completed_at": datetime.now(TZ).isoformat(),
            "http_status": response.status_code,
            "success": response.status_code == 200,
            "raw": binding(path),
        }
        receipts.append(receipt)
        if response.status_code != 200:
            break
        match = re.match(rb"^cb\((.*)\)\s*$", raw, re.S)
        if not match:
            receipt["success"] = False
            receipt["error"] = "INVALID_JSONP_WRAPPER"
            break
        payload = json.loads(match.group(1).decode("utf-8"))[0]
        if not payload.get("result"):
            receipt["success"] = False
            receipt["error"] = "OFFICIAL_ENDPOINT_RESULT_FALSE"
            break
        data = payload["data"]
        total_pages = int(data["totalPages"])
        receipt["official_total_elements"] = int(data["totalElements"])
        receipt["official_total_pages"] = total_pages
        for entry in data["content"]:
            published = entry.get("publishDate", "")[:10]
            if "2021-01-01" <= published <= "2024-12-31":
                entries[int(entry["infoId"])] = entry
        time.sleep(0.75)
    selected = []
    annual_pattern = re.compile(r"关于(202[2-4])年部分节假日休市安排的公告")
    for entry in sorted(entries.values(), key=lambda item: item["publishDate"]):
        match = annual_pattern.fullmatch(entry["title"].strip())
        if not match:
            continue
        year = int(match.group(1))
        url = "https://www.bse.cn" + entry["htmlUrl"]
        started = datetime.now(TZ).isoformat()
        response = session.get(url, headers={"User-Agent": BSE_HEADERS["User-Agent"]}, timeout=(10, 30), allow_redirects=False)
        path = CANDIDATE / f"raw/bse/calendar/{year}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
        ok = response.status_code == 200 and bool(response.content)
        receipts.append({
            "request_id": f"bse-annual-calendar-{year}",
            "method": "GET",
            "url": url,
            "started_at": started,
            "completed_at": datetime.now(TZ).isoformat(),
            "http_status": response.status_code,
            "success": ok,
            "raw": binding(path),
            "official_index_entry": entry,
        })
        if ok:
            selected.append({
                "item_id": f"bse-calendar-{year}",
                "venue": "BSE",
                "year": year,
                "category": "annual_calendar",
                "title": entry["title"],
                "url": url,
                "stored_path": path.relative_to(ROOT).as_posix(),
                "success": True,
                "index_publish_date": entry["publishDate"],
                "index_info_id": entry["infoId"],
            })
        time.sleep(0.75)
    return selected, receipts


def resolved_tokens(prefix: str, target_year: int) -> list[tuple[date, tuple[int, int], dict[str, Any]]]:
    matches = list(DATE_TOKEN.finditer(prefix))
    parsed: list[tuple[int | None, int | None, int, re.Match[str]]] = []
    last_month = None
    for match in matches:
        year = int(match.group("year")) if match.group("year") else None
        month = int(match.group("month")) if match.group("month") else last_month
        if month is None:
            continue
        last_month = month
        parsed.append((year, month, int(match.group("day")), match))
    if not parsed:
        return []
    # Grammatical cross-year resolution only; no statutory-calendar inference.
    if len(parsed) >= 2 and parsed[0][0] is None and parsed[1][0] is None and parsed[0][1] > parsed[1][1]:
        years = [target_year - 1, target_year] + [target_year] * (len(parsed) - 2)
    else:
        years = []
        active_year = target_year
        active_month = None
        for explicit_year, month, _day, _match in parsed:
            if explicit_year is not None:
                active_year = explicit_year
            elif active_month is not None and month < active_month and active_month == 12:
                active_year += 1
            years.append(active_year)
            active_month = month
    result = []
    for index, ((explicit_year, month, day, match), year) in enumerate(zip(parsed, years)):
        try:
            result.append((date(year, month, day), match.span(), {
                "explicit_year": explicit_year,
                "inferred_year": year if explicit_year is None else None,
                "token_index": index,
            }))
        except ValueError:
            continue
    return result


def parse_notice(receipt: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = ROOT / receipt["stored_path"]
    text = visible_text(path)
    target_year = int(receipt["year"])
    published = re.search(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)", text)
    publication_date = published.group(0) if published else None
    evidence: list[dict[str, Any]] = []
    parse_warnings: list[str] = []
    clauses = [x.strip() for x in re.split(r"[。；;]", text) if "休市" in x]
    for clause_index, clause in enumerate(clauses):
        prefix = clause.split("休市", 1)[0]
        parse_prefix = re.sub(r"\s+", "", prefix)
        tokens = resolved_tokens(parse_prefix, target_year)
        if not tokens:
            continue
        # Use only the closest date expression before the closure word.  This
        # prevents navigation/title dates from being interpreted as closures.
        if len(prefix) > 260:
            prefix = prefix[-260:]
            parse_prefix = re.sub(r"\s+", "", prefix)
            tokens = resolved_tokens(parse_prefix, target_year)
        if not tokens:
            continue
        rows: list[date] = []
        if len(tokens) >= 2 and RANGE_MARK.search(parse_prefix[tokens[-2][1][1]:tokens[-1][1][0]]):
            start, end = tokens[-2][0], tokens[-1][0]
            span = (end - start).days
            if span < 0 or span > 40:
                parse_warnings.append(f"rejected_range:{start}:{end}")
                continue
            rows = [start + timedelta(days=offset) for offset in range(span + 1)]
            parse_rule = "EXPLICIT_OFFICIAL_CLOSURE_RANGE_INCLUSIVE"
            token_meta = [tokens[-2][2], tokens[-1][2]]
        else:
            # Comma-separated dates expressly followed by '休市'.
            rows = [token[0] for token in tokens[-8:]]
            parse_rule = "EXPLICIT_OFFICIAL_CLOSURE_DATE_LIST"
            token_meta = [token[2] for token in tokens[-8:]]
        for closed_date in rows:
            evidence.append({
                "venue": receipt["venue"],
                "calendar_date": closed_date.isoformat(),
                "is_trading_day": False,
                "closure_evidence_only": True,
                "source_item_id": receipt["item_id"],
                "source_url": receipt["url"],
                "source_raw": binding(path),
                "source_clause": clause[-360:],
                "source_clause_index": clause_index,
                "normalization_rule": parse_rule,
                "date_resolution": token_meta,
                "published_date": publication_date,
                "published_at": None,
                "available_at": None,
                "pit_status": "UNRESOLVED_DATE_PRECISION_ONLY" if publication_date else "UNRESOLVED_PUBLICATION_DATE",
                "purpose_ceiling": PURPOSE,
                "quality_status": QUALITY,
            })
    lineage = {
        "item_id": receipt["item_id"],
        "venue": receipt["venue"],
        "target_year": target_year,
        "category": receipt["category"],
        "source_url": receipt["url"],
        "source_raw": binding(path),
        "published_date": publication_date,
        "explicit_closure_row_count_before_dedup": len(evidence),
        "parse_warnings": parse_warnings,
        "official_document_semantic_check": target_year in {date.fromisoformat(x["calendar_date"]).year for x in evidence} or bool(evidence),
        "purpose_ceiling": PURPOSE,
        "quality_status": QUALITY,
    }
    return evidence, lineage


def import_sessions() -> tuple[list[dict[str, Any]], list[str]]:
    imported, rejected = [], []
    if not SESSION_INPUT.is_file():
        return imported, ["session_input_missing"]
    for line_no, line in enumerate(SESSION_INPUT.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        raw = ROOT / row["raw_path"]
        if not raw.is_file() or sha(raw) != row["raw_sha256"]:
            rejected.append(f"line_{line_no}_raw_binding_failed")
            continue
        notice = ROOT / row["notice_raw_path"]
        if not notice.is_file() or sha(notice) != row["notice_raw_sha256"]:
            rejected.append(f"line_{line_no}_notice_binding_failed")
            continue
        row = dict(row)
        row["imported_from"] = binding(SESSION_INPUT)
        row["formal_effect"] = "NONE"
        imported.append(row)
    return imported, rejected


def file_manifest(root: Path, manifest_name: str) -> None:
    files = [binding(p) for p in sorted(x for x in root.rglob("*") if x.is_file()) if p.name != manifest_name]
    dump(root / manifest_name, {"run_id": RUN_ID, "files": files})


def main() -> int:
    if CANDIDATE.exists() or REPORT.exists():
        raise SystemExit("output already exists; fail closed")
    if not (UPSTREAM / "MANIFEST.json").is_file():
        raise SystemExit("upstream manifest missing")
    before = guards()
    CANDIDATE.mkdir(parents=True)
    REPORT.mkdir(parents=True)
    upstream = json.loads((UPSTREAM / "MANIFEST.json").read_text(encoding="utf-8"))
    bse_annual, bse_receipts = collect_bse_annual_notices()
    annual = [r for r in upstream["receipts"] if r["success"] and r["category"] in {"annual_calendar", "calendar_revision"}]
    annual.extend(bse_annual)
    closure_rows, lineage = [], []
    for receipt in annual:
        rows, source = parse_notice(receipt)
        closure_rows.extend(rows)
        lineage.append(source)
    # Aggregate duplicate evidence without inventing a trading-day universe.
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in closure_rows:
        grouped[(row["venue"], row["calendar_date"])].append(row)
    normalized = []
    for (venue, calendar_date), rows in sorted(grouped.items()):
        normalized.append({
            "venue": venue,
            "calendar_date": calendar_date,
            "is_trading_day": False,
            "closure_evidence_only": True,
            "evidence_count": len(rows),
            "evidence": rows,
            "purpose_ceiling": PURPOSE,
            "quality_status": QUALITY,
            "formal_effect": "NONE",
        })
    rules = []
    for receipt in upstream["receipts"]:
        if not receipt["success"] or receipt["category"] in {"annual_calendar", "calendar_revision", "reachability_only"}:
            continue
        raw = ROOT / receipt["stored_path"]
        rules.append({
            "venue": receipt["venue"],
            "nominal_year": receipt["year"],
            "rule_category": receipt["category"],
            "source_item_id": receipt["item_id"],
            "source_url": receipt["url"],
            "source_raw": binding(raw),
            "effective_from": None,
            "effective_to": None,
            "effective_date_status": "UNRESOLVED__DOCUMENT_PRESENCE_ONLY",
            "complete_version_chain": False,
            "purpose_ceiling": PURPOSE,
            "quality_status": QUALITY,
            "formal_effect": "NONE",
        })
    sessions, session_rejections = import_sessions()
    dump_jsonl(CANDIDATE / "VENUE_DATE_CLOSURE_CANDIDATE.jsonl", normalized)
    dump_jsonl(CANDIDATE / "CALENDAR_NOTICE_LINEAGE.jsonl", lineage)
    dump_jsonl(CANDIDATE / "RULE_VERSION_CANDIDATE.jsonl", rules)
    dump_jsonl(CANDIDATE / "SESSION_INTERVAL_CANDIDATE.jsonl", sessions)
    dump(REPORT / "NETWORK_REQUEST_RECEIPTS.json", bse_receipts)
    bounded = {
        "sse_2005_2008": {
            "status": "BOUNDED_OFFICIAL_SEARCH_COMPLETED__ANNUAL_MASTER_NOT_CLOSED",
            "annual_master_found": [],
            "official_2008_supplemental_urls_found_not_ingested": [
                "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988207.shtml",
                "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988236.shtml",
                "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988240.shtml",
            ],
            "formal_calendar_effect": "NONE",
        },
        "bse_2021_2024": {
            "status": "BOUNDED_OFFICIAL_LIST_AND_ANNUAL_PAGE_COLLECTION_COMPLETED__2021_PARTIAL_YEAR_UNCLOSED",
            "official_index_endpoint": "https://www.bse.cn/info/listse.do",
            "official_index_keyword": "休市安排",
            "official_index_matches_reported": max((x.get("official_total_elements", 0) for x in bse_receipts), default=0),
            "publication_window": ["2021-01-01", "2024-12-31"],
            "annual_calendar_evidence_ingested": [x["item_id"] for x in bse_annual],
            "existing_official_session_rows_hash_verified": sum(x["venue"] == "BSE" for x in sessions),
            "temporary_closure_evidence_ingested": [],
            "network_receipts": "reports/data-governance/r0-official-calendar-rules-normalized-stagea-20260801-v2/NETWORK_REQUEST_RECEIPTS.json",
            "formal_calendar_effect": "NONE",
        },
        "request_boundary": "bounded, serial, no authentication, no bypass, no guessed URL",
    }
    dump(REPORT / "BOUNDED_SEARCH_RECEIPTS.json", bounded)
    venues = sorted({x["venue"] for x in normalized})
    duplicate_keys = len(normalized) - len({(x["venue"], x["calendar_date"]) for x in normalized})
    gates = {
        "CORE02-Q1": {"status": "FAIL", "reason": "BSE 2022-2024 annual notices now narrow the gap, but SSE 2005-2008 annual masters, BSE's 2021 partial year, complete official daily calendars and complete three-venue historical session chains remain unclosed."},
        "CORE02-Q2": {"status": "FAIL", "reason": "Complete three-venue temporary/emergency closure, revision and publication-time PIT chains remain unclosed."},
        "CORE02-Q3": {"status": "UNKNOWN", "reason": "Closure-only evidence cannot prove full coverage, previous/next trading dates or period-end fields."},
        "CORE02-Q4": {"status": "PASS", "reason": "Every candidate key preserves explicit venue and normalized venue-date keys are unique."},
    }
    after = guards()
    guard_pass = before == after
    manifest = {
        "run_id": RUN_ID,
        "created_at": datetime.now(TZ).isoformat(),
        "purpose_ceiling": PURPOSE,
        "quality_status": QUALITY,
        "formal_admission": False,
        "research_usable": False,
        "supersedes_nonadmitted_candidate": "data/market/quarantine/r0-official-calendar-rules/normalized-stagea-20260801-v1",
        "supersession_reason": "v1 parser did not normalize whitespace inside SZSE Chinese date tokens; v2 is the only current result.",
        "official_sources_only": True,
        "statutory_holiday_inference": False,
        "weekday_open_day_inference": False,
        "upstream_manifest": binding(UPSTREAM / "MANIFEST.json"),
        "annual_and_revision_notice_count": len(annual),
        "normalized_closure_key_count": len(normalized),
        "normalized_venues": venues,
        "rule_source_count": len(rules),
        "session_interval_count": len(sessions),
        "bse_annual_notice_count": len(bse_annual),
        "bse_network_request_count": len(bse_receipts),
        "session_import_rejections": session_rejections,
        "duplicate_venue_date_key_count": duplicate_keys,
        "guard_before": before,
        "guard_after": after,
        "guard_pass": guard_pass,
    }
    dump(CANDIDATE / "NORMALIZATION_SUMMARY.json", manifest)
    dump(REPORT / "CORE02_STAGE_A_RECOMPUTE.json", {
        "run_id": RUN_ID,
        "target": "CORE-02",
        "purpose_ceiling": PURPOSE,
        "quality_status": QUALITY,
        "formal_admission": False,
        "decision": "FAIL_CLOSED__Q1_FAIL__Q2_FAIL__Q3_UNKNOWN__Q4_PASS__NOT_ADMITTED",
        "gates": gates,
        "metrics": {
            "normalized_closure_key_count": len(normalized),
            "normalized_venues": venues,
            "rule_source_count": len(rules),
            "session_interval_count": len(sessions),
            "duplicate_venue_date_key_count": duplicate_keys,
        },
    })
    dump(REPORT / "MINIMUM_REMAINING_EXTERNAL_EVIDENCE.json", {
        "items": [
            "SSE official annual master closure notices or equivalent official archival set for 2005-2008.",
            "BSE official annual calendar evidence for 2021-2024 since establishment, with authoritative publication lineage.",
            "Complete three-venue temporary/emergency closure and supplemental revision archive.",
            "Complete three-venue historical session-rule version chain with effective and supersession dates.",
            "Publication timestamps or conservative official availability evidence sufficient to close PIT ordering.",
            "A complete official daily open/closed calendar projection; closure notices alone are insufficient.",
        ],
        "independent_backup_required": False,
    })
    dump(REPORT / "PROTECTION_CHECK.json", {"before": before, "after": after, "pass": guard_pass})
    (REPORT / "README.md").write_text(
        "# R0 official calendar/rules normalization and CORE-02 Stage-A recompute\n\n"
        f"- Purpose: `{PURPOSE}`; quality: `{QUALITY}`; formal effect: `NONE`.\n"
        "- Only dates explicitly stated as closed by official notices were normalized.\n"
        "- No statutory-holiday or weekday inference was used.\n"
        "- CORE-02 result remains `Q1 FAIL / Q2 FAIL / Q3 UNKNOWN / Q4 PASS`.\n"
        "- This package is not admitted and is not research-usable.\n",
        encoding="utf-8",
    )
    file_manifest(CANDIDATE, "CANDIDATE_MANIFEST.json")
    file_manifest(REPORT, "REPORT_MANIFEST.json")
    if not guard_pass:
        raise SystemExit("protected authority drift detected")
    print(json.dumps({
        "run_id": RUN_ID,
        "closure_keys": len(normalized),
        "venues": venues,
        "rules": len(rules),
        "sessions": len(sessions),
        "session_rejections": len(session_rejections),
        "gates": {k: v["status"] for k, v in gates.items()},
        "guard_pass": guard_pass,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
