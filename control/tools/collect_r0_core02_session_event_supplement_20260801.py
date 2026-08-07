#!/usr/bin/env python3
"""Collect bounded official session-rule and temporary-event evidence.

New network requests are limited to official SSE/SZSE pages that are not
already cached locally. Existing official files are hash-referenced in place.
Any 403/429 stops the request loop without retrying. The result is strictly an
INSPECTION_ONLY / NOT_ADMITTED supplemental package.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "r0-core02-session-event-supplement-20260801-v1"
CANDIDATE = ROOT / "data/market/quarantine/r0-official-calendar-rules/session-event-supplement-20260801-v1"
REPORT = ROOT / "reports/data-governance/r0-official-calendar-rules-session-event-supplement-20260801-v1"
PURPOSE = "INSPECTION_ONLY"
QUALITY = "NOT_ADMITTED"
TZ = timezone(timedelta(hours=8))
UA = "Quantification-R0-Official-Evidence/1.0"

INTAKE = ROOT / "data/market/quarantine/r0-official-calendar-rules/intake-20260801-v1"
NORMALIZED = ROOT / "data/market/quarantine/r0-official-calendar-rules/normalized-stagea-20260801-v2"
STAGE_A = ROOT / "tmp/d2a04b-source-closure/stage-a-core02-calendar-session-closure-20260730-v1"
CORE02_OLD = ROOT / "tmp/d2a04b-source-closure/core02-official-calendar-session-closure-20260730-v1"

GUARDED_FILES = (
    ROOT / "data/catalog/datasets.json",
    ROOT / "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    ROOT / "control/ROADMAP.md",
    ROOT / "control/projects.yaml",
)
GUARDED_TREES = (ROOT / "data/market/snapshots", ROOT / "data/published")


DOWNLOAD_SPECS = [
    {
        "item_id": "szse-trading-rule-2006",
        "venue": "SZSE",
        "category": "historical_session_rule",
        "url": "https://www.szse.cn/disclosure/notice/general/t20060515_499577.html",
        "relpath": "raw/szse/rules/trading-rule-2006.html",
        "tokens": ["自2006年7月1日起施行", "9:15至9:25", "9:30至11:30", "13:00至14:57", "14:57至15:00"],
    },
    {
        "item_id": "sse-trading-rule-2020-docx",
        "venue": "SSE",
        "category": "historical_session_rule_attachment",
        "url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/10785127/files/b39b8db2daa74eac916d8b3d6907af51.docx",
        "relpath": "raw/sse/rules/trading-rule-2020.docx",
        "tokens": ["9:15至9:25", "9:30至11:30", "13:00至14:57", "14:57至15:00"],
    },
    {
        "item_id": "szse-temporary-closure-2008-wenchuan",
        "venue": "SZSE",
        "category": "temporary_closure_event",
        "url": "https://www.szse.cn/disclosure/notice/general/t20080519_500058.html",
        "relpath": "raw/szse/events/2008-wenchuan.html",
        "tokens": ["2008年5月19日", "临时停市"],
    },
    {
        "item_id": "sse-circuit-breaker-20160104-a",
        "venue": "SSE",
        "category": "circuit_breaker_event",
        "url": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20160104_4031711.shtml",
        "relpath": "raw/sse/events/2016-01-04-a.html",
        "tokens": ["2016", "熔断"],
    },
    {
        "item_id": "sse-circuit-breaker-20160104-b",
        "venue": "SSE",
        "category": "circuit_breaker_event",
        "url": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20160104_4031718.shtml",
        "relpath": "raw/sse/events/2016-01-04-b.html",
        "tokens": ["2016", "熔断"],
    },
    {
        "item_id": "szse-circuit-breaker-20160104-a",
        "venue": "SZSE",
        "category": "circuit_breaker_event",
        "url": "https://www.szse.cn/disclosure/notice/general/t20160104_501611.html",
        "relpath": "raw/szse/events/2016-01-04-a.html",
        "tokens": ["2016", "熔断"],
    },
    {
        "item_id": "szse-circuit-breaker-20160104-b",
        "venue": "SZSE",
        "category": "circuit_breaker_event",
        "url": "https://www.szse.cn/disclosure/notice/general/t20160104_501612.html",
        "relpath": "raw/szse/events/2016-01-04-b.html",
        "tokens": ["2016", "熔断"],
    },
    {
        "item_id": "sse-circuit-breaker-20160107-a",
        "venue": "SSE",
        "category": "circuit_breaker_event",
        "url": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20160107_4033036.shtml",
        "relpath": "raw/sse/events/2016-01-07-a.html",
        "tokens": ["2016", "熔断"],
    },
    {
        "item_id": "sse-circuit-breaker-20160107-b",
        "venue": "SSE",
        "category": "circuit_breaker_event",
        "url": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20160107_4033042.shtml",
        "relpath": "raw/sse/events/2016-01-07-b.html",
        "tokens": ["2016", "熔断"],
    },
    {
        "item_id": "szse-circuit-breaker-20160107-a",
        "venue": "SZSE",
        "category": "circuit_breaker_event",
        "url": "https://www.szse.cn/disclosure/notice/general/t20160107_501616.html",
        "relpath": "raw/szse/events/2016-01-07-a.html",
        "tokens": ["2016", "熔断"],
    },
    {
        "item_id": "szse-circuit-breaker-20160107-b",
        "venue": "SZSE",
        "category": "circuit_breaker_event",
        "url": "https://www.szse.cn/disclosure/notice/general/t20160107_501617.html",
        "relpath": "raw/szse/events/2016-01-07-b.html",
        "tokens": ["2016", "熔断"],
    },
]


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
    rows = []
    if path.exists():
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            rows.append(f"{item.relative_to(path).as_posix()}\t{item.stat().st_size}\t{sha(item)}")
    return sha_bytes("\n".join(rows).encode("utf-8"))


def guards() -> dict[str, Any]:
    return {
        "files": {rel(path): sha(path) for path in GUARDED_FILES},
        "trees": {rel(path): tree_hash(path) for path in GUARDED_TREES},
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
    suffix = path.suffix.lower()
    if suffix in {".html", ".shtml", ".htm"}:
        soup = BeautifulSoup(path.read_bytes(), "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return " ".join(soup.stripped_strings)
    if suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        soup = BeautifulSoup(xml, "xml")
        return " ".join(node.get_text() for node in soup.find_all("w:t"))
    if suffix == ".pdf":
        executable = shutil.which("pdftotext")
        if not executable:
            raise RuntimeError("pdftotext unavailable")
        result = subprocess.run([executable, "-layout", str(path), "-"], capture_output=True, check=True)
        return result.stdout.decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def normalized_text(path: Path) -> str:
    text = visible_text(path)
    text = text.replace("：", ":").replace("︰", ":").replace("﹕", ":")
    text = re.sub(r"\s+", "", text)
    return text


def collect() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    session = requests.Session()
    receipts = []
    hard_stop = None
    for sequence, spec in enumerate(DOWNLOAD_SPECS, 1):
        if hard_stop is not None:
            break
        started = datetime.now(TZ).isoformat()
        status = None
        content = b""
        error = None
        safe_headers = {}
        try:
            response = session.get(spec["url"], headers={"User-Agent": UA}, timeout=(10, 30), allow_redirects=False)
            status = response.status_code
            content = response.content
            safe_headers = {
                key.lower(): value for key, value in response.headers.items()
                if key.lower() in {"date", "content-type", "content-length", "last-modified", "etag", "location"}
            }
        except requests.RequestException as exc:
            error = f"{type(exc).__name__}: {exc}"
        raw = CANDIDATE / spec["relpath"]
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(content)
        token_check = None
        semantic_error = None
        if status == 200 and content:
            try:
                text = normalized_text(raw)
                token_check = {token: re.sub(r"\s+", "", token).replace("：", ":").replace("︰", ":") in text for token in spec["tokens"]}
            except Exception as exc:  # evidence receipt, not silent acceptance
                semantic_error = f"{type(exc).__name__}: {exc}"
        receipt = {
            "request_sequence": sequence,
            "item_id": spec["item_id"],
            "venue": spec["venue"],
            "category": spec["category"],
            "method": "GET",
            "url": spec["url"],
            "started_at": started,
            "completed_at": datetime.now(TZ).isoformat(),
            "http_status": status,
            "success": status == 200 and bool(content),
            "error": error,
            "semantic_error": semantic_error,
            "semantic_tokens": spec["tokens"],
            "semantic_token_check": token_check,
            "semantic_tokens_all_present": bool(token_check) and all(token_check.values()),
            "response_headers_safe": safe_headers,
            "raw": binding(raw),
        }
        receipts.append(receipt)
        if status in {403, 429}:
            hard_stop = {"request_sequence": sequence, "item_id": spec["item_id"], "http_status": status, "reason": "SITE_LIMIT_SIGNAL__NO_RETRY"}
        time.sleep(0.8)
    return receipts, hard_stop


def cached_anchor_specs(download_receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    downloaded = {row["item_id"]: ROOT / row["raw"]["path"] for row in download_receipts if row["success"]}
    return [
        {
            "venue": "SSE", "rule_version": "2006", "source_revision_date": "2006-05-15", "effective_from": "2006-07-01",
            "effective_date_status": "EXPLICIT_IMPLEMENTATION_DATE", "raw": INTAKE / "raw/sse/rules/trading-rule-2006.html",
            "tokens": ["自2006年7月1日起实施", "9:15至9:25", "9:30至11:30", "13:00至15:00"],
            "intervals": [("OPENING_AUCTION", "09:15", "09:25"), ("CONTINUOUS_AM", "09:30", "11:30"), ("CONTINUOUS_PM", "13:00", "15:00")],
        },
        {
            "venue": "SSE", "rule_version": "2014", "source_revision_date": "2014-09-26", "effective_from": "2014-09-26",
            "effective_date_status": "EXPLICIT_PUBLISH_DATE_IMPLEMENTATION", "raw": INTAKE / "raw/sse/rules/trading-rule-amendment-2014.html",
            "tokens": ["自发布之日起实施", "9:15至9:25", "9:30至11:30", "13:00至15:00"],
            "intervals": [("OPENING_AUCTION", "09:15", "09:25"), ("CONTINUOUS_AM", "09:30", "11:30"), ("CONTINUOUS_PM", "13:00", "15:00")],
        },
        {
            "venue": "SSE", "rule_version": "2018", "source_revision_date": "2018-08-06", "effective_from": "2018-08-20",
            "effective_date_status": "EXPLICIT_IMPLEMENTATION_DATE", "raw": INTAKE / "raw/sse/rules/trading-rule-amendment-2018.html",
            "tokens": ["自2018年8月20日起实施", "9:15至9:25", "9:30至11:30", "13:00至14:57", "14:57至15:00"],
            "intervals": [("OPENING_AUCTION", "09:15", "09:25"), ("CONTINUOUS_AM", "09:30", "11:30"), ("CONTINUOUS_PM", "13:00", "14:57"), ("CLOSING_AUCTION", "14:57", "15:00")],
        },
        {
            "venue": "SSE", "rule_version": "2020", "source_revision_date": "2020-03-13", "effective_from": "2020-03-13",
            "effective_date_status": "NOTICE_EXPLICIT__ATTACHMENT_SESSION_TEXT", "raw": downloaded.get("sse-trading-rule-2020-docx"),
            "tokens": ["9:15至9:25", "9:30至11:30", "13:00至14:57", "14:57至15:00"],
            "intervals": [("OPENING_AUCTION", "09:15", "09:25"), ("CONTINUOUS_AM", "09:30", "11:30"), ("CONTINUOUS_PM", "13:00", "14:57"), ("CLOSING_AUCTION", "14:57", "15:00")],
        },
        {
            "venue": "SSE", "rule_version": "2023", "source_revision_date": "2023-04-10", "effective_from": "2023-04-10",
            "effective_date_status": "UPSTREAM_EXPLICIT", "raw": CORE02_OLD / "raw/sse/rules/2023_trade_rule_repealed.docx",
            "tokens": ["9:15至9:25", "9:30至11:30", "13:00至14:57", "14:57至15:00"],
            "intervals": [("OPENING_AUCTION", "09:15", "09:25"), ("CONTINUOUS_AM", "09:30", "11:30"), ("CONTINUOUS_PM", "13:00", "14:57"), ("CLOSING_AUCTION", "14:57", "15:00")],
        },
        {
            "venue": "SZSE", "rule_version": "2006", "source_revision_date": "2006-05-15", "effective_from": "2006-07-01",
            "effective_date_status": "EXPLICIT_IMPLEMENTATION_DATE", "raw": downloaded.get("szse-trading-rule-2006"),
            "tokens": ["自2006年7月1日起施行", "9:15至9:25", "9:30至11:30", "13:00至14:57", "14:57至15:00"],
            "intervals": [("OPENING_AUCTION", "09:15", "09:25"), ("CONTINUOUS_AM", "09:30", "11:30"), ("CONTINUOUS_PM", "13:00", "14:57"), ("CLOSING_AUCTION", "14:57", "15:00")],
        },
        {
            "venue": "SZSE", "rule_version": "2011", "source_revision_date": "2011-01-17", "effective_from": None,
            "effective_date_status": "REVISION_DATE_ONLY__IMPLEMENTATION_BOUNDARY_NOT_ASSERTED", "raw": INTAKE / "raw/szse/rules/trading-rule-2011.pdf",
            "tokens": ["2011年1月17日第二次修订", "9:15至9:25", "9:30至11:30", "13:00至14:57", "14:57至15:00"],
            "intervals": [("OPENING_AUCTION", "09:15", "09:25"), ("CONTINUOUS_AM", "09:30", "11:30"), ("CONTINUOUS_PM", "13:00", "14:57"), ("CLOSING_AUCTION", "14:57", "15:00")],
        },
        {
            "venue": "SZSE", "rule_version": "2020-12", "source_revision_date": "2020-12-31", "effective_from": "2020-12-31",
            "effective_date_status": "UPSTREAM_EXPLICIT", "raw": INTAKE / "raw/szse/rules/trading-rule-2020.pdf",
            "tokens": ["2020年12月31日", "9:15至9:25", "9:30至11:30", "13:00至14:57", "14:57至15:00"],
            "intervals": [("OPENING_AUCTION", "09:15", "09:25"), ("CONTINUOUS_AM", "09:30", "11:30"), ("CONTINUOUS_PM", "13:00", "14:57"), ("CLOSING_AUCTION", "14:57", "15:00")],
        },
    ]


def build_anchors(download_receipts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    anchors = []
    intervals = []
    for spec in cached_anchor_specs(download_receipts):
        raw = spec["raw"]
        exists = isinstance(raw, Path) and raw.is_file()
        token_check = None
        semantic_error = None
        if exists:
            try:
                text = normalized_text(raw)
                token_check = {token: re.sub(r"\s+", "", token).replace("：", ":").replace("︰", ":") in text for token in spec["tokens"]}
            except Exception as exc:
                semantic_error = f"{type(exc).__name__}: {exc}"
        anchor_id = f"{spec['venue'].lower()}-{spec['rule_version']}"
        anchor = {
            "anchor_id": anchor_id,
            "venue": spec["venue"],
            "rule_version": spec["rule_version"],
            "source_revision_date": spec["source_revision_date"],
            "effective_from": spec["effective_from"],
            "effective_to": None,
            "effective_date_status": spec["effective_date_status"],
            "anchor_only": True,
            "continuous_coverage_claimed": False,
            "complete_version_chain": False,
            "source_raw": binding(raw) if exists else None,
            "semantic_tokens": spec["tokens"],
            "semantic_token_check": token_check,
            "semantic_tokens_all_present": bool(token_check) and all(token_check.values()),
            "semantic_error": semantic_error,
            "purpose_ceiling": PURPOSE,
            "quality_status": QUALITY,
            "formal_effect": "NONE",
        }
        anchors.append(anchor)
        for session_name, start, end in spec["intervals"]:
            intervals.append({
                "anchor_id": anchor_id,
                "venue": spec["venue"],
                "rule_version": spec["rule_version"],
                "effective_from": spec["effective_from"],
                "effective_to": None,
                "session_name": session_name,
                "start_time": start,
                "end_time": end,
                "timezone": "Asia/Shanghai",
                "anchor_only": True,
                "continuous_coverage_claimed": False,
                "source_raw": anchor["source_raw"],
                "purpose_ceiling": PURPOSE,
                "quality_status": QUALITY,
                "formal_effect": "NONE",
            })
    return anchors, intervals


def event_rows(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dates = {
        "szse-temporary-closure-2008-wenchuan": "2008-05-19",
        "sse-circuit-breaker-20160104-a": "2016-01-04",
        "sse-circuit-breaker-20160104-b": "2016-01-04",
        "szse-circuit-breaker-20160104-a": "2016-01-04",
        "szse-circuit-breaker-20160104-b": "2016-01-04",
        "sse-circuit-breaker-20160107-a": "2016-01-07",
        "sse-circuit-breaker-20160107-b": "2016-01-07",
        "szse-circuit-breaker-20160107-a": "2016-01-07",
        "szse-circuit-breaker-20160107-b": "2016-01-07",
    }
    rows = []
    for receipt in receipts:
        if receipt["item_id"] not in dates:
            continue
        rows.append({
            "venue": receipt["venue"],
            "event_date": dates[receipt["item_id"]],
            "event_evidence_type": "TEMPORARY_CLOSURE_NOTICE" if "temporary-closure" in receipt["item_id"] else "INDEX_CIRCUIT_BREAKER_OPERATIONAL_NOTICE",
            "source_item_id": receipt["item_id"],
            "source_url": receipt["url"],
            "source_raw": receipt["raw"],
            "http_success": receipt["success"],
            "semantic_tokens_all_present": receipt["semantic_tokens_all_present"],
            "event_completeness_claimed": False,
            "purpose_ceiling": PURPOSE,
            "quality_status": QUALITY,
            "formal_effect": "NONE",
        })
    return rows


def write_manifest(root: Path, name: str) -> None:
    files = [binding(path) for path in sorted(p for p in root.rglob("*") if p.is_file()) if path.name != name]
    dump(root / name, {"run_id": RUN_ID, "files": files})


def main() -> int:
    if CANDIDATE.exists() or REPORT.exists():
        raise SystemExit("output already exists; fail closed")
    before = guards()
    CANDIDATE.mkdir(parents=True)
    REPORT.mkdir(parents=True)
    receipts, hard_stop = collect()
    anchors, intervals = build_anchors(receipts)
    events = event_rows(receipts)
    dump(REPORT / "NETWORK_REQUEST_RECEIPTS.json", receipts)
    dump_jsonl(CANDIDATE / "SESSION_RULE_ANCHOR_EVIDENCE.jsonl", anchors)
    dump_jsonl(CANDIDATE / "SESSION_INTERVAL_ANCHOR_EVIDENCE.jsonl", intervals)
    dump_jsonl(CANDIDATE / "TEMPORARY_EVENT_EVIDENCE.jsonl", events)
    after = guards()
    guard_pass = before == after
    semantic_failures = [row["item_id"] for row in receipts if row["success"] and not row["semantic_tokens_all_present"]]
    anchor_failures = [row["anchor_id"] for row in anchors if not row["semantic_tokens_all_present"]]
    summary = {
        "run_id": RUN_ID,
        "created_at": datetime.now(TZ).isoformat(),
        "purpose_ceiling": PURPOSE,
        "quality_status": QUALITY,
        "formal_admission": False,
        "research_usable": False,
        "planned_network_request_count": len(DOWNLOAD_SPECS),
        "executed_network_request_count": len(receipts),
        "network_success_count": sum(row["success"] for row in receipts),
        "network_403_429_count": sum(row["http_status"] in {403, 429} for row in receipts),
        "hard_stop": hard_stop,
        "network_semantic_failure_item_ids": semantic_failures,
        "session_rule_anchor_count": len(anchors),
        "session_interval_anchor_count": len(intervals),
        "session_anchor_semantic_failure_ids": anchor_failures,
        "temporary_event_evidence_count": len(events),
        "continuous_session_coverage_claimed": False,
        "complete_event_archive_claimed": False,
        "remaining_gaps": [
            "SSE 2005-2008 annual master notice lineage remains incomplete.",
            "SSE/SZSE pre-2006-07-01 session sources remain unresolved for the 2005-2024 bounded window.",
            "Intermediate SZSE amendments and exact replacement boundaries remain incomplete.",
            "The temporary closure/circuit-breaker ledger is evidence expansion, not a complete event archive.",
            "BSE complete calendar/session/event history remains unresolved.",
        ],
        "formal_core02_decision": "FAIL_CLOSED__NOT_ADMITTED",
        "guard_pass": guard_pass,
    }
    dump(CANDIDATE / "SUMMARY.json", summary)
    dump(REPORT / "SUMMARY.json", summary)
    dump(REPORT / "PROTECTION_CHECK.json", {"before": before, "after": after, "pass": guard_pass})
    dump(REPORT / "CORE02_SUPPLEMENTAL_GATE_EFFECT.json", {
        "formal_effect": "NONE",
        "evidence_effect": "ADDS_OFFICIAL_SESSION_ANCHORS_AND_2008_2016_EVENT_PAGES",
        "CORE02-Q1": "FAIL",
        "CORE02-Q2": "FAIL",
        "CORE02-Q3": "UNKNOWN",
        "CORE02-Q4": "PASS",
        "decision": "FAIL_CLOSED__NOT_ADMITTED",
    })
    REPORT.joinpath("README.md").write_text(
        "# CORE-02 official session/event supplemental evidence\n\n"
        "- Boundary: `INSPECTION_ONLY / NOT_ADMITTED`; formal effect `NONE`.\n"
        "- Existing official rule files were hash-referenced; only missing official pages/files were requested.\n"
        "- Any 403/429 stops collection without retry.\n"
        "- Session rows are version anchors, not a continuous or complete historical chain.\n"
        "- Event rows expand the evidence ledger but do not claim an exhaustive archive.\n",
        encoding="utf-8",
    )
    write_manifest(CANDIDATE, "CANDIDATE_MANIFEST.json")
    candidate_manifest = binding(CANDIDATE / "CANDIDATE_MANIFEST.json")
    verify = {
        "run_id": RUN_ID,
        "checks": {
            "guard_pass": guard_pass,
            "no_403_429": summary["network_403_429_count"] == 0,
            "all_requests_executed": len(receipts) == len(DOWNLOAD_SPECS),
            "all_network_requests_successful": summary["network_success_count"] == len(DOWNLOAD_SPECS),
            "catalog_unchanged": before["files"]["data/catalog/datasets.json"] == after["files"]["data/catalog/datasets.json"],
            "snapshots_unchanged": before["trees"]["data/market/snapshots"] == after["trees"]["data/market/snapshots"],
            "published_unchanged": before["trees"]["data/published"] == after["trees"]["data/published"],
        },
        "candidate_manifest": candidate_manifest,
    }
    verify["pass"] = all(verify["checks"].values())
    dump(REPORT / "VERIFY_ONLY.json", verify)
    write_manifest(REPORT, "REPORT_MANIFEST.json")
    print(json.dumps({
        "candidate": rel(CANDIDATE),
        "report": rel(REPORT),
        "candidate_manifest": candidate_manifest,
        "report_manifest": binding(REPORT / "REPORT_MANIFEST.json"),
        "summary": summary,
        "verify_only": verify,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
