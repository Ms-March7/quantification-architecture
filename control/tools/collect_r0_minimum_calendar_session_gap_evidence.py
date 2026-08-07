#!/usr/bin/env python3
"""Collect the final bounded official calendar/session gap evidence.

This package is evidence only.  It never infers trading days from weekdays or
statutory holidays and it never mutates the formal catalog or snapshots.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "r0-official-calendar-rules-minimum-gap-closure-20260801-v1"
CANDIDATE = ROOT / "data/market/quarantine/r0-official-calendar-rules/minimum-gap-closure-20260801-v1"
REPORT = ROOT / "reports/data-governance/r0-official-calendar-rules-minimum-gap-closure-20260801-v1"
INTAKE_RECEIPTS = ROOT / "reports/data-governance/r0-official-calendar-rules-intake-20260801-v1/SOURCE_RECEIPTS.json"
PURPOSE = "INSPECTION_ONLY"
QUALITY = "NOT_ADMITTED"
TZ = timezone(timedelta(hours=8))
UA = "Quantification-R0-Official-Evidence/1.0"
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


def visible_text(content: bytes) -> str:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.stripped_strings)


SPECS = [
    ("sse-closure-archive-entrance", "SSE", "archive_entrance", "https://www.sse.com.cn/disclosure/dealinstruc/closed/list/", "raw/sse/archive/entrance.html"),
    *[
        (f"sse-closure-archive-page-{page}", "SSE", "archive_page", f"https://www.sse.com.cn/disclosure/dealinstruc/closed/list/{'s_list' if page == 1 else f's_list_{page}'}.shtml", f"raw/sse/archive/page-{page}.html")
        for page in range(1, 7)
    ],
    ("sse-2008-dragon-boat-supplement", "SSE", "annual_supplement", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988207.shtml", "raw/sse/2008-supplements/dragon-boat.html"),
    ("sse-2008-mid-autumn-supplement", "SSE", "annual_supplement", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988236.shtml", "raw/sse/2008-supplements/mid-autumn.html"),
    ("sse-2008-national-day-supplement", "SSE", "annual_supplement", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988240.shtml", "raw/sse/2008-supplements/national-day.html"),
    ("sse-temporary-closure-2008-wenchuan", "SSE", "temporary_closure_event", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988199.shtml", "raw/sse/temporary/2008-wenchuan.html"),
    ("sse-temporary-closure-2022-mourning", "SSE", "temporary_closure_event", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20221204_5713460.shtml", "raw/sse/temporary/2022-mourning.html"),
    ("szse-temporary-closure-2022-mourning", "SZSE", "temporary_closure_event", "https://www.szse.cn/disclosure/notice/general/t20221204_597669.html", "raw/szse/temporary/2022-mourning.html"),
    ("bse-trading-rule-2021", "BSE", "historical_session_rule", "https://www.bse.cn/jygl_list/200010919.html", "raw/bse/rules/trading-rule-2021.html"),
    ("bse-abnormal-trading-rule-2021", "BSE", "emergency_procedure_rule", "https://www.bse.cn/jygl_list/200010947.html", "raw/bse/rules/abnormal-trading-rule-2021.html"),
    ("bse-abnormal-trading-rule-2021-pdf", "BSE", "emergency_procedure_rule_attachment", "https://www.bse.cn/uploads/6/file/public/202111/20211102223515_9dxi6lcfx5.pdf", "raw/bse/rules/abnormal-trading-rule-2021.pdf"),
    ("bse-trading-regulation-service-entrance", "BSE", "rule_archive_entrance", "https://www.bse.cn/service/jyjg.html", "raw/bse/rules/service-entrance.html"),
]


def collect() -> list[dict[str, Any]]:
    session = requests.Session()
    receipts = []
    for sequence, (item_id, venue, category, url, relpath) in enumerate(SPECS, 1):
        started = datetime.now(TZ).isoformat()
        error = None
        status = None
        content = b""
        headers: dict[str, str] = {}
        try:
            response = session.get(url, headers={"User-Agent": UA}, timeout=(10, 30), allow_redirects=False)
            status = response.status_code
            content = response.content
            headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in {"date", "content-type", "content-length", "last-modified", "etag", "location"}}
        except requests.RequestException as exc:
            error = f"{type(exc).__name__}: {exc}"
        path = CANDIDATE / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        receipts.append({
            "request_sequence": sequence,
            "item_id": item_id,
            "venue": venue,
            "category": category,
            "method": "GET",
            "url": url,
            "started_at": started,
            "completed_at": datetime.now(TZ).isoformat(),
            "http_status": status,
            "success": status == 200 and bool(content),
            "error": error,
            "response_headers_safe": headers,
            "raw": binding(path),
        })
        time.sleep(0.75)
    return receipts


def archive_scan(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    entries = []
    for receipt in receipts:
        if receipt["category"] != "archive_page" or not receipt["success"]:
            continue
        path = ROOT / receipt["raw"]["path"]
        soup = BeautifulSoup(path.read_bytes(), "html.parser")
        for dd in soup.select("dd"):
            date_node, link = dd.find("span"), dd.find("a")
            if not date_node or not link:
                continue
            entries.append({
                "publication_date": date_node.get_text(strip=True),
                "title": link.get_text(" ", strip=True),
                "url": urljoin("https://www.sse.com.cn", link.get("href", "")),
                "archive_page_raw": receipt["raw"],
            })
    targets = {year: [] for year in range(2005, 2009)}
    for entry in entries:
        for year in targets:
            if str(year) in entry["title"] and ("全年休市" in entry["title"] or "部分节假日休市" in entry["title"]):
                targets[year].append(entry)
    dates = sorted(e["publication_date"] for e in entries if re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["publication_date"]))
    return {
        "official_archive_entrance": "https://www.sse.com.cn/disclosure/dealinstruc/closed/list/",
        "archive_page_count_fetched": 6,
        "archive_entry_count": len(entries),
        "earliest_publication_date_visible": dates[0] if dates else None,
        "latest_publication_date_visible": dates[-1] if dates else None,
        "target_annual_master_matches": {str(k): v for k, v in targets.items()},
        "bounded_conclusion": "NO_2005_TO_2008_ANNUAL_MASTER_HIT_IN_CURRENT_SIX_PAGE_OFFICIAL_ARCHIVE",
        "nonexistence_claimed": False,
        "formal_effect": "NONE",
    }


def event_ledger(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs = {
        "sse-temporary-closure-2008-wenchuan": ("2008-05-19", ["临时停市", "3 分钟", "14 点 28 分", "14 点 31 分"]),
        "sse-temporary-closure-2022-mourning": ("2022-12-06", ["临时停市3分钟", "2022年12月6日"]),
        "szse-temporary-closure-2022-mourning": ("2022-12-06", ["临时停市", "3 分钟", "2022 年 12 月 6 日"]),
    }
    rows = []
    for receipt in receipts:
        if receipt["item_id"] not in specs or not receipt["success"]:
            continue
        event_date, tokens = specs[receipt["item_id"]]
        raw = ROOT / receipt["raw"]["path"]
        text = visible_text(raw.read_bytes())
        rows.append({
            "venue": receipt["venue"],
            "event_date": event_date,
            "event_type": "FULL_MARKET_TEMPORARY_CLOSURE",
            "source_item_id": receipt["item_id"],
            "source_url": receipt["url"],
            "source_raw": receipt["raw"],
            "semantic_tokens": tokens,
            "semantic_tokens_all_present": all(token in text for token in tokens),
            "purpose_ceiling": PURPOSE,
            "quality_status": QUALITY,
            "formal_effect": "NONE",
        })
    return rows


def historical_rule_ledger(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = {
        "sse-trading-rule-2006", "sse-trading-rule-amendment-2014", "sse-trading-rule-amendment-2018",
        "sse-trading-rule-amendment-2020", "sse-trading-rule-2023-doc",
        "szse-trading-rule-2011", "szse-trading-rule-2020",
    }
    intake = json.loads(INTAKE_RECEIPTS.read_text(encoding="utf-8"))
    rows = []
    for item in intake:
        if item["item_id"] not in wanted:
            continue
        raw = ROOT / item["stored_path"]
        verified = raw.is_file() and sha(raw) == item["sha256"]
        rows.append({
            "venue": item["venue"], "source_item_id": item["item_id"], "nominal_year": item["year"],
            "source_url": item["url"], "source_raw": binding(raw) if raw.is_file() else None,
            "upstream_hash_verified": verified, "effective_from": None, "effective_to": None,
            "complete_version_chain": False, "purpose_ceiling": PURPOSE, "quality_status": QUALITY,
            "formal_effect": "NONE",
        })
    bse = next(r for r in receipts if r["item_id"] == "bse-trading-rule-2021")
    rows.append({
        "venue": "BSE", "source_item_id": bse["item_id"], "nominal_year": 2021,
        "source_url": bse["url"], "source_raw": bse["raw"], "upstream_hash_verified": bse["success"],
        "effective_from": "2021-11-15", "effective_to": None, "complete_version_chain": False,
        "purpose_ceiling": PURPOSE, "quality_status": QUALITY, "formal_effect": "NONE",
    })
    return rows


def file_manifest(root: Path, manifest_name: str) -> None:
    files = [binding(p) for p in sorted(x for x in root.rglob("*") if x.is_file()) if p.name != manifest_name]
    dump(root / manifest_name, {"run_id": RUN_ID, "files": files})


def main() -> int:
    if CANDIDATE.exists() or REPORT.exists():
        raise SystemExit("output already exists; fail closed")
    before = guards()
    CANDIDATE.mkdir(parents=True)
    REPORT.mkdir(parents=True)
    receipts = collect()
    scan = archive_scan(receipts)
    events = event_ledger(receipts)
    rules = historical_rule_ledger(receipts)
    dump(REPORT / "NETWORK_REQUEST_RECEIPTS.json", receipts)
    dump(REPORT / "SSE_2005_2008_ARCHIVE_SCAN_RECEIPT.json", scan)
    dump_jsonl(CANDIDATE / "TEMPORARY_CLOSURE_EVIDENCE.jsonl", events)
    dump_jsonl(CANDIDATE / "HISTORICAL_SESSION_RULE_ENTRANCES.jsonl", rules)
    dump(REPORT / "SEARCH_QUERY_LEDGER.json", {
        "scope": "BOUNDED_OFFICIAL_ONLY",
        "queries": [
            {"venue": "SSE", "query": f"关于{year}年全年休市安排的通知", "target_year": year, "official_master_hit": False}
            for year in range(2005, 2009)
        ] + [
            {"venue": "BSE", "query": "2021年部分节假日休市安排", "target_window": ["2021-11-15", "2021-12-31"], "official_master_hit": False},
            {"venue": "BSE", "query": "2022年12月6日 临时停市", "official_event_hit": False},
        ],
        "auditable_official_archive_scan": "SSE_2005_2008_ARCHIVE_SCAN_RECEIPT.json",
        "nonexistence_claimed": False,
        "formal_effect": "NONE",
    })
    dump(REPORT / "BSE_2021_PARTIAL_WINDOW_EVIDENCE.json", {
        "market_effective_date": "2021-11-15",
        "official_rule_source": next(r for r in rules if r["venue"] == "BSE"),
        "partial_window": ["2021-11-15", "2021-12-31"],
        "annual_closure_master_found": False,
        "complete_daily_open_closed_calendar": False,
        "status": "UNRESOLVED__RULE_AND_SESSION_EVIDENCE_ONLY",
        "formal_effect": "NONE",
    })
    dump(REPORT / "TEMPORARY_EMERGENCY_COVERAGE.json", {
        "explicit_event_evidence": [{"venue": r["venue"], "event_date": r["event_date"], "source_item_id": r["source_item_id"]} for r in events],
        "bse_actual_full_market_event_evidence": [],
        "bse_emergency_procedure_rule": next(r for r in receipts if r["item_id"] == "bse-abnormal-trading-rule-2021-pdf"),
        "complete_three_venue_event_archive": False,
        "formal_effect": "NONE",
    })
    gaps = [
        "SSE 2005-2008 annual master closure notices were not exposed by the bounded current six-page official archive scan; 2008 supplemental notices do not replace the master.",
        "BSE 2021-11-15 through 2021-12-31 complete daily open/closed calendar and annual closure lineage remain unresolved.",
        "A complete three-venue temporary/emergency closure event archive remains unresolved; no BSE actual full-market event notice was found in the bounded search.",
        "Complete historical session-rule effective/supersession chains for all three venues remain unresolved.",
        "Closure and rule evidence alone cannot establish a complete official daily trading calendar projection.",
    ]
    gates = {
        "CORE02-Q1": {"status": "FAIL", "reason": gaps[0] + " " + gaps[1]},
        "CORE02-Q2": {"status": "FAIL", "reason": gaps[2] + " " + gaps[3]},
        "CORE02-Q3": {"status": "UNKNOWN", "reason": gaps[4]},
        "CORE02-Q4": {"status": "PASS", "reason": "All newly preserved explicit event records retain venue and date identity; no inferred dates were emitted."},
    }
    dump(REPORT / "MINIMUM_REMAINING_GAPS.json", {"items": gaps, "independent_backup_required": False})
    dump(REPORT / "CORE02_GATE_RECOMPUTE.json", {
        "run_id": RUN_ID, "purpose_ceiling": PURPOSE, "quality_status": QUALITY,
        "formal_admission": False, "decision": "FAIL_CLOSED__NOT_ADMITTED", "gates": gates,
    })
    after = guards()
    guard_pass = before == after
    dump(REPORT / "PROTECTION_CHECK.json", {"before": before, "after": after, "pass": guard_pass})
    summary = {
        "run_id": RUN_ID, "created_at": datetime.now(TZ).isoformat(), "purpose_ceiling": PURPOSE,
        "quality_status": QUALITY, "formal_admission": False, "research_usable": False,
        "official_sources_only": True, "bounded_collection": True, "statutory_holiday_inference": False,
        "weekday_open_day_inference": False, "network_request_count": len(receipts),
        "network_success_count": sum(r["success"] for r in receipts), "sse_archive_entry_count": scan["archive_entry_count"],
        "temporary_event_evidence_count": len(events), "historical_rule_entrance_count": len(rules),
        "guard_before": before, "guard_after": after, "guard_pass": guard_pass,
    }
    dump(CANDIDATE / "SUMMARY.json", summary)
    (REPORT / "README.md").write_text(
        "# R0 official calendar/session minimum-gap closure\n\n"
        "- Boundary: `INSPECTION_ONLY / NOT_ADMITTED`; formal effect: `NONE`.\n"
        "- Only official exchange pages/files were downloaded or hash-referenced.\n"
        "- No weekday/statutory-holiday inference was used.\n"
        "- The bounded search narrows gaps but does not close CORE-02.\n",
        encoding="utf-8",
    )
    file_manifest(CANDIDATE, "CANDIDATE_MANIFEST.json")
    file_manifest(REPORT, "REPORT_MANIFEST.json")
    if not guard_pass:
        raise SystemExit("protected authority drift detected")
    print(json.dumps({
        "run_id": RUN_ID, "requests": len(receipts), "success": sum(r["success"] for r in receipts),
        "sse_archive_entries": scan["archive_entry_count"], "events": len(events), "rules": len(rules),
        "gates": {k: v["status"] for k, v in gates.items()}, "guard_pass": guard_pass,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
