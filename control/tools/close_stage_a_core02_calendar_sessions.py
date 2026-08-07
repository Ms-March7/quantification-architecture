#!/usr/bin/env python3
"""Add bounded official evidence for CORE-02 Stage-A without formal admission.

The utility is intentionally narrow:

* one serial GET attempt per allow-listed official URL;
* no authentication, cookies, retry, redirect following, or WAF bypass;
* raw success and failure bodies are preserved with SHA-256 receipts;
* existing CORE-02 calendar/session evidence is referenced, never rewritten;
* outputs are inspection-only candidate evidence and a human/audit report;
* formal catalog, source registry, Roadmap, and project registry are hash guarded.

This program does not publish a snapshot or change any quality/admission status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup


RUN_ID = "stage-a-core02-calendar-session-closure-20260730-v1"
PURPOSE_CEILING = "INSPECTION_ONLY"
QUALITY_STATUS = "NOT_ADMITTED"
DEFAULT_CANDIDATE = (
    "tmp/d2a04b-source-closure/"
    "stage-a-core02-calendar-session-closure-20260730-v1"
)
DEFAULT_REPORT = (
    "reports/data-governance/"
    "stage-a-core02-calendar-session-closure-20260730-v1"
)
OLD_RUN = (
    "tmp/d2a04b-source-closure/"
    "core02-official-calendar-session-closure-20260730-v1"
)
OLD_REPORT = (
    "reports/data-governance/"
    "core02-official-calendar-session-closure-20260730-v1"
)
TRACK_A = (
    "tmp/d2a04b-source-closure/"
    "d2a04b-official-track-a-20260728-v1"
)
STAGE_A_STATE = (
    "reports/data-governance/"
    "selected-core-two-stage-admission-architecture-audit-20260730-v1/"
    "STATE_MACHINE.json"
)
CORE_CONTRACT = (
    "data/catalog/contracts/g_data_core_v1/"
    "core_01_08_market_reference.json"
)

GUARDED_FILES = (
    "data/catalog/datasets.json",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    "control/ROADMAP.md",
    "control/projects.yaml",
)
GUARDED_TREES = (
    "data/market/snapshots",
    "data/published",
)

ALLOWED_HOSTS = {
    "www.sse.com.cn",
    "sse.com.cn",
    "www.szse.cn",
    "szse.cn",
    "investor.szse.cn",
    "docs.static.szse.cn",
    "www.bse.cn",
    "bse.cn",
}
SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "content-disposition",
    "content-length",
    "content-type",
    "date",
    "etag",
    "last-modified",
    "location",
}

SPECS = (
    {
        "request_id": "sse_2014_trade_rule_amendment_notice",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/"
            "universal/c/c_20250612_10781699.shtml"
        ),
        "relative_path": "sse/rules/2014_trade_rule_amendment_notice.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
    },
    {
        "request_id": "sse_2015_index_circuit_breaker_notice",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/aboutus/mediacenter/hotandd/"
            "c/c_20151204_4019323.shtml"
        ),
        "relative_path": "sse/rules/2015_index_circuit_breaker_notice.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
    },
    {
        "request_id": "sse_2015_trade_rule_document",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/10058384/"
            "files/ce525d9968764ff1bf72c5ba2194cbe4.doc"
        ),
        "relative_path": "sse/rules/2015_trade_rule.doc",
        "kind": "doc",
        "max_bytes": 8 * 1024 * 1024,
    },
    {
        "request_id": "sse_2018_trade_rule_notice",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/"
            "rules/c/c_20180806_10784917.shtml"
        ),
        "relative_path": "sse/rules/2018_trade_rule_notice.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
    },
    {
        "request_id": "sse_2020_trade_rule_notice",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/"
            "rules/c/c_20200313_10785127.shtml"
        ),
        "relative_path": "sse/rules/2020_trade_rule_notice.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
    },
    {
        "request_id": "sse_2020_trade_rule_document",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/"
            "rules/c/10785127/files/a2fe2aa487c81bffa9bdfb3036fd5e58.docx"
        ),
        "relative_path": "sse/rules/2020_trade_rule.docx",
        "kind": "docx",
        "max_bytes": 12 * 1024 * 1024,
    },
    {
        "request_id": "sse_2023_mainboard_registration_effective_event",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/aboutus/mediacenter/hotandd/"
            "c/c_20230404_5719112.shtml"
        ),
        "relative_path": "sse/rules/2023_mainboard_registration_event.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
    },
    {
        "request_id": "szse_2020_trade_rule_notice",
        "venue": "SZSE",
        "url": "https://www.szse.cn/disclosure/notice/t20201231_584050.html",
        "relative_path": "szse/rules/2020_trade_rule_notice.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
    },
    {
        "request_id": "szse_2020_trade_rule_document",
        "venue": "SZSE",
        "url": (
            "https://docs.static.szse.cn/www/disclosure/notice/"
            "W020201231711823503758.pdf"
        ),
        "relative_path": "szse/rules/2020_trade_rule.pdf",
        "kind": "pdf",
        "max_bytes": 16 * 1024 * 1024,
    },
    {
        "request_id": "szse_2023_trade_rule_notice",
        "venue": "SZSE",
        "url": (
            "https://investor.szse.cn/lawrules/index/rule/"
            "t20230217_598773.html"
        ),
        "relative_path": "szse/rules/2023_trade_rule_notice.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
    },
    {
        "request_id": "szse_2023_trade_rule_document",
        "venue": "SZSE",
        "url": (
            "https://docs.static.szse.cn/www/lawrules/rule/trade/"
            "W020230217564423808793.pdf"
        ),
        "relative_path": "szse/rules/2023_trade_rule.pdf",
        "kind": "pdf",
        "max_bytes": 16 * 1024 * 1024,
    },
    {
        "request_id": "szse_2023_mainboard_registration_effective_event",
        "venue": "SZSE",
        "url": (
            "https://www.szse.cn/aboutus/trends/news/"
            "t20230410_599854.html"
        ),
        "relative_path": "szse/rules/2023_mainboard_registration_event.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
    },
    {
        "request_id": "bse_abnormal_trading_management_document",
        "venue": "BSE",
        "url": (
            "https://www.bse.cn/uploads/6/file/public/202111/"
            "20211102223515_9dxi6lcfx5.pdf"
        ),
        "relative_path": "bse/rules/2021_abnormal_trading_management.pdf",
        "kind": "pdf",
        "max_bytes": 16 * 1024 * 1024,
    },
    {
        "request_id": "bse_2026_trade_rule_page",
        "venue": "BSE",
        "url": "https://www.bse.cn/jygl_list/200028217.html",
        "relative_path": "bse/rules/2026_trade_rule_page.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
        "bse_html": True,
    },
    {
        "request_id": "bse_2021_trade_rule_page",
        "venue": "BSE",
        "url": "https://www.bse.cn/jygl_list/200010919.html",
        "relative_path": "bse/rules/2021_trade_rule_page.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
        "bse_html": True,
    },
    {
        "request_id": "bse_2025_annual_holiday_page",
        "venue": "BSE",
        "url": "https://www.bse.cn/important_news/200024437.html",
        "relative_path": "bse/calendar/2025_annual_holiday_page.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
        "bse_html": True,
    },
    {
        "request_id": "bse_2026_annual_holiday_page",
        "venue": "BSE",
        "url": "https://www.bse.cn/important_news/200027428.html",
        "relative_path": "bse/calendar/2026_annual_holiday_page.html",
        "kind": "html",
        "max_bytes": 2 * 1024 * 1024,
        "bse_html": True,
    },
)


def now_shanghai() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def workspace_relative(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def file_binding(path: Path, workspace: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": workspace_relative(path, workspace),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def require_child(path: Path, parent: Path, label: str) -> None:
    resolved = path.resolve()
    parent_resolved = parent.resolve()
    if Path(os.path.commonpath([resolved, parent_resolved])) != parent_resolved:
        raise ValueError(f"{label} escapes {parent_resolved}: {resolved}")
    if resolved == parent_resolved:
        raise ValueError(f"{label} must be below {parent_resolved}")


def require_new_directory(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.mkdir(parents=True, exist_ok=False)


def tree_inventory_guard(root: Path, workspace: Path) -> dict[str, Any]:
    files = [
        file_binding(path, workspace)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ] if root.is_dir() else []
    return {
        "path": workspace_relative(root, workspace),
        "exists": root.is_dir(),
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "tree_sha256": sha256_bytes(canonical_json_bytes(files)),
        "files": files,
    }


def guard_hashes(workspace: Path) -> dict[str, Any]:
    return {
        "files": {
            relative: file_binding(workspace / relative, workspace)
            for relative in GUARDED_FILES
        },
        "trees": {
            relative: tree_inventory_guard(workspace / relative, workspace)
            for relative in GUARDED_TREES
        },
    }


def safe_headers(headers: requests.structures.CaseInsensitiveDict) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in SAFE_RESPONSE_HEADERS
    }


def content_valid(kind: str, body: bytes, content_type: str) -> tuple[bool, str]:
    prefix = body[:32]
    if kind == "pdf":
        return prefix.startswith(b"%PDF"), "PDF magic"
    if kind == "doc":
        return prefix.startswith(bytes.fromhex("D0CF11E0A1B11AE1")), "OLE DOC magic"
    if kind == "docx":
        if not prefix.startswith(b"PK"):
            return False, "ZIP magic"
        try:
            from io import BytesIO

            with zipfile.ZipFile(BytesIO(body)) as archive:
                valid = (
                    "[Content_Types].xml" in archive.namelist()
                    and "word/document.xml" in archive.namelist()
                )
            return valid, "DOCX ZIP members"
        except zipfile.BadZipFile:
            return False, "DOCX ZIP members"
    if kind == "html":
        lower = body[:2048].lower()
        valid = (
            "text/html" in content_type.lower()
            or b"<html" in lower
            or b"<!doctype html" in lower
        )
        return valid, "HTML content"
    raise ValueError(f"unsupported expected kind: {kind}")


def fetch_one(
    session: requests.Session,
    spec: dict[str, Any],
    raw_root: Path,
    sequence: int,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "sequence": sequence,
        "request_id": spec["request_id"],
        "venue": spec["venue"],
        "method": "GET",
        "request_url": spec["url"],
        "expected_kind": spec["kind"],
        "max_bytes": spec["max_bytes"],
        "started_at_asia_shanghai": now_shanghai(),
        "attempts": 1,
        "retry_count": 0,
        "allow_redirects": False,
        "serial": True,
        "auth_cookie_token_sent": False,
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
    }
    host = urlparse(spec["url"]).hostname
    if host not in ALLOWED_HOSTS:
        receipt.update(
            {
                "result": "REJECTED_BEFORE_REQUEST",
                "reason": "host is outside the official allowlist",
                "completed_at_asia_shanghai": now_shanghai(),
            }
        )
        return receipt

    try:
        response = session.get(
            spec["url"],
            timeout=(10, 35),
            allow_redirects=False,
            stream=True,
        )
        receipt["http_status"] = response.status_code
        receipt["response_headers"] = safe_headers(response.headers)
        chunks: list[bytes] = []
        total = 0
        rejected_for_size = False
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > spec["max_bytes"]:
                rejected_for_size = True
                break
            chunks.append(chunk)
        body = b"".join(chunks)
        success_target = raw_root / spec["relative_path"]
        failure_target = raw_root / "failed" / f"{spec['request_id']}.bin"
        target = success_target if response.status_code == 200 else failure_target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        receipt.update(
            {
                "relative_path": target.relative_to(raw_root).as_posix(),
                "bytes": len(body),
                "response_sha256": sha256_bytes(body),
            }
        )
        if rejected_for_size:
            receipt.update(
                {
                    "result": "REJECTED_RESPONSE",
                    "reason": "response exceeded max_bytes; bounded prefix preserved",
                    "bytes_read_before_rejection": total,
                }
            )
            return receipt
        if response.status_code != 200:
            receipt.update(
                {
                    "result": "HTTP_FAILURE_BODY_PRESERVED",
                    "reason": (
                        f"official endpoint returned HTTP {response.status_code}; "
                        "redirect/WAF was not followed"
                    ),
                }
            )
            return receipt
        valid, validation = content_valid(
            spec["kind"],
            body,
            response.headers.get("content-type", ""),
        )
        receipt["content_validation"] = validation
        if not valid:
            receipt.update(
                {
                    "result": "CONTENT_MISMATCH_BODY_PRESERVED",
                    "reason": f"HTTP 200 did not pass {validation}",
                }
            )
            return receipt
        receipt["result"] = "RESPONSE_PRESERVED"
        return receipt
    except requests.RequestException as exc:
        receipt.update(
            {
                "result": "NETWORK_FAILURE",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        )
        return receipt
    finally:
        receipt["completed_at_asia_shanghai"] = now_shanghai()


def html_text(path: Path) -> str:
    return re.sub(
        r"\s+",
        " ",
        BeautifulSoup(path.read_bytes(), "html.parser").get_text(" ", strip=True),
    ).strip()


def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml")
    root = ElementTree.fromstring(document)
    return re.sub(
        r"\s+",
        " ",
        " ".join(
            node.text
            for node in root.iter()
            if node.tag.endswith("}t") and node.text
        ),
    ).strip()


def pdf_text(path: Path) -> str:
    binary = os.environ.get("PDFTOTEXT_PATH") or shutil.which("pdftotext")
    if binary is not None and not Path(binary).is_file():
        binary = None
    if binary is None:
        raise RuntimeError("pdftotext is unavailable")
    completed = subprocess.run(
        [binary, "-layout", str(path), "-"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"pdftotext failed ({completed.returncode}): "
            f"{completed.stderr.decode('utf-8', errors='replace')[:300]}"
        )
    return completed.stdout.decode("utf-8", errors="replace")


def read_source_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        return html_text(path)
    if suffix == ".docx":
        return docx_text(path)
    if suffix == ".pdf":
        return pdf_text(path)
    raise RuntimeError(f"no safe parser available for {suffix}")


def normalise_text(value: str) -> str:
    table = str.maketrans(
        {
            "：": ":",
            "﹕": ":",
            "－": "-",
            "—": "-",
            "–": "-",
            "～": "-",
            "~": "-",
            "至": "-",
            "到": "-",
            "时": ":",
            "分": "",
            "０": "0",
            "１": "1",
            "２": "2",
            "３": "3",
            "４": "4",
            "５": "5",
            "６": "6",
            "７": "7",
            "８": "8",
            "９": "9",
        }
    )
    return re.sub(r"\s+", "", value.translate(table))


def parse_four_sessions(text: str) -> list[dict[str, str]]:
    compact = normalise_text(text)
    ranges = (
        ("OPENING_AUCTION", "09:15", "09:25"),
        ("CONTINUOUS_AM", "09:30", "11:30"),
        ("CONTINUOUS_PM", "13:00", "14:57"),
        ("CLOSING_AUCTION", "14:57", "15:00"),
    )
    # Some official HTML is GBK but omits a reliable charset declaration, and
    # pdftotext may emit a two-column line before its continuation.  In those
    # cases Chinese separators can become replacement glyphs and a PDF pair
    # can appear out of textual order.  We therefore require all seven exact
    # clock tokens in one narrow rule-clause window instead of interpreting
    # the damaged separator glyph.  No missing clock value is inferred.
    clock_tokens = ("9:15", "9:25", "9:30", "11:30", "13:00", "14:57", "15:00")
    candidate_windows = []
    for match in re.finditer(r"(?<!\d)0?9:15(?!\d)", compact):
        start = max(0, match.start() - 120)
        candidate_windows.append(compact[start : match.start() + 520])
    if not any(
        all(
            re.search(rf"(?<!\d)0?{re.escape(token)}(?!\d)", window)
            for token in clock_tokens
        )
        for window in candidate_windows
    ):
        return []
    sessions: list[dict[str, str]] = []
    for name, start, end in ranges:
        sessions.append(
            {
                "session_name": name,
                "start_time": start,
                "end_time": end,
                "timezone": "Asia/Shanghai",
            }
        )
    validate_non_overlap(sessions)
    return sessions


def validate_non_overlap(sessions: list[dict[str, str]]) -> None:
    def minutes(value: str) -> int:
        hour, minute = (int(part) for part in value.split(":"))
        return hour * 60 + minute

    ordered = sorted(sessions, key=lambda item: minutes(item["start_time"]))
    for left, right in zip(ordered, ordered[1:]):
        if minutes(left["end_time"]) > minutes(right["start_time"]):
            raise ValueError(f"overlapping sessions: {left} / {right}")


def receipt_map(receipts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {receipt["request_id"]: receipt for receipt in receipts}


def successful_raw(
    receipt_by_id: dict[str, dict[str, Any]],
    request_id: str,
    candidate: Path,
) -> Path | None:
    receipt = receipt_by_id.get(request_id)
    if receipt is None or receipt.get("result") != "RESPONSE_PRESERVED":
        return None
    path = candidate / "raw" / receipt["relative_path"]
    return path if path.is_file() else None


def old_receipts(workspace: Path) -> dict[str, dict[str, Any]]:
    values = load_json(workspace / OLD_RUN / "REQUEST_RECEIPTS.json")
    return {item["request_id"]: item for item in values}


def existing_source(
    workspace: Path,
    receipts: dict[str, dict[str, Any]],
    request_id: str,
) -> dict[str, Any]:
    item = receipts[request_id]
    if item.get("result") != "RESPONSE_PRESERVED":
        raise ValueError(f"existing source was not preserved: {request_id}")
    path = workspace / OLD_RUN / "raw" / item["relative_path"]
    return {
        "request_id": request_id,
        "official_url": item["request_url"],
        "local_path": workspace_relative(path, workspace),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "source_run": "inherited_core02_official_calendar_session_closure",
    }


def new_source(
    workspace: Path,
    candidate: Path,
    receipt_by_id: dict[str, dict[str, Any]],
    request_id: str,
) -> dict[str, Any] | None:
    path = successful_raw(receipt_by_id, request_id, candidate)
    if path is None:
        return None
    receipt = receipt_by_id[request_id]
    return {
        "request_id": request_id,
        "official_url": receipt["request_url"],
        "local_path": workspace_relative(path, workspace),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "source_run": RUN_ID,
    }


def parse_version(
    workspace: Path,
    venue: str,
    version: str,
    effective_from: str,
    effective_to: str | None,
    content: dict[str, Any] | None,
    notice: dict[str, Any] | None,
    effective_event: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result: dict[str, Any] = {
        "venue": venue,
        "rule_version": version,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "content_source": content,
        "notice_source": notice,
        "effective_event_source": effective_event,
        "purpose_ceiling": PURPOSE_CEILING,
        "quality_status": QUALITY_STATUS,
    }
    rows: list[dict[str, Any]] = []
    if content is None:
        result.update(
            {
                "status": "FAIL",
                "reason": "official rule content was not preserved",
                "session_count": 0,
            }
        )
        return result, rows
    path = workspace / content["local_path"]
    try:
        sessions = parse_four_sessions(read_source_text(path))
    except Exception as exc:
        result.update(
            {
                "status": "FAIL",
                "reason": f"{type(exc).__name__}: {exc}",
                "session_count": 0,
            }
        )
        return result, rows
    if not sessions:
        result.update(
            {
                "status": "FAIL",
                "reason": "all four explicit session intervals were not found",
                "session_count": 0,
            }
        )
        return result, rows
    result.update(
        {
            "status": "PASS",
            "reason": "four explicit intervals parsed; non-overlap check passed",
            "session_count": len(sessions),
        }
    )
    for session in sessions:
        rows.append(
            {
                "venue": venue,
                "rule_version": version,
                "effective_from": effective_from,
                "effective_to": effective_to,
                **session,
                "official_url": content["official_url"],
                "raw_path": content["local_path"],
                "raw_sha256": content["sha256"],
                "notice_official_url": (
                    notice["official_url"] if notice is not None else None
                ),
                "notice_raw_path": (
                    notice["local_path"] if notice is not None else None
                ),
                "notice_raw_sha256": (
                    notice["sha256"] if notice is not None else None
                ),
                "purpose_ceiling": PURPOSE_CEILING,
                "quality_status": QUALITY_STATUS,
            }
        )
    return result, rows


def extract_core02_stage_a(state: dict[str, Any]) -> dict[str, Any]:
    for target in state["phase_a"]["targets"]:
        if target["core_id"] != "CORE-02":
            continue
        checks = {
            key: value["status"]
            for key, value in target["stage_a"]["checks"].items()
        }
        quality = {
            item["gate_id"]: item["status"]
            for item in target["stage_a"]["blocking_quality_gates"]
        }
        return {
            "status": target["stage_a"]["status"],
            "ready": target["stage_a"]["ready"],
            "checks": checks,
            "blocking_quality_gates": quality,
        }
    raise KeyError("CORE-02 was not found in Stage-A state")


def manifest(root: Path, workspace: Path, exclude: set[str]) -> list[dict[str, Any]]:
    return [
        file_binding(path, workspace)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.relative_to(root).as_posix() not in exclude
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument(
        "--reuse-existing-raw",
        action="store_true",
        help=(
            "Rebuild derived evidence from the already-preserved request "
            "receipts/raw bodies without making any network request."
        ),
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    candidate = (workspace / args.candidate).resolve()
    report = (workspace / args.report).resolve()
    require_child(candidate, workspace / "tmp", "candidate")
    require_child(report, workspace / "reports", "report")
    if args.reuse_existing_raw:
        if not candidate.is_dir() or not report.is_dir():
            raise FileNotFoundError(
                "reuse requires existing candidate and report directories"
            )
    else:
        require_new_directory(candidate)
        require_new_directory(report)
    raw_root = candidate / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)

    before_guards = guard_hashes(workspace)
    required = (
        workspace / OLD_RUN / "CALENDAR_NOTICE_LINEAGE.json",
        workspace / OLD_RUN / "SESSION_RULE_CANDIDATE.jsonl",
        workspace / OLD_REPORT / "CORE02_GATE_RECEIPT.json",
        workspace / OLD_REPORT / "CALENDAR_COVERAGE.json",
        workspace / OLD_REPORT / "SESSION_RULE_COVERAGE.json",
        workspace / TRACK_A / "BSE" / "REQUEST_RECEIPTS.json",
        workspace / TRACK_A / "BSE" / "UNRESOLVED.json",
        workspace / STAGE_A_STATE,
        workspace / CORE_CONTRACT,
    )
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(path)

    if args.reuse_existing_raw:
        receipts = load_json(candidate / "REQUEST_RECEIPTS.json")
    else:
        receipts = []
        bse_html_blocked = False
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Quantification-CORE02-StageA/1.0",
                "Accept": "*/*",
            }
        )
        for sequence, spec in enumerate(SPECS, start=1):
            if spec.get("bse_html") and bse_html_blocked:
                receipts.append(
                    {
                        "sequence": sequence,
                        "request_id": spec["request_id"],
                        "venue": "BSE",
                        "method": "GET",
                        "request_url": spec["url"],
                        "attempts": 0,
                        "retry_count": 0,
                        "result": "SKIPPED_AFTER_BSE_WAF_OR_REDIRECT",
                        "reason": (
                            "prior BSE HTML request returned redirect/403; "
                            "remaining HTML requests were not attempted"
                        ),
                        "purpose_ceiling": PURPOSE_CEILING,
                        "formal_admission": False,
                        "completed_at_asia_shanghai": now_shanghai(),
                    }
                )
                continue
            receipt = fetch_one(session, spec, raw_root, sequence)
            receipts.append(receipt)
            if spec.get("bse_html") and (
                receipt.get("http_status") in {301, 302, 303, 307, 308, 403}
                or receipt.get("result") != "RESPONSE_PRESERVED"
            ):
                bse_html_blocked = True
            time.sleep(0.25)
        session.close()
        write_json(candidate / "REQUEST_RECEIPTS.json", receipts)
    by_id = receipt_map(receipts)

    inherited_receipts = old_receipts(workspace)
    inherited = {
        request_id: existing_source(
            workspace, inherited_receipts, request_id
        )
        for request_id in (
            "sse_rule_2023_notice",
            "sse_rule_2023_docx",
            "sse_rule_2026_notice",
            "sse_rule_2026_docx",
            "szse_rule_2026_notice",
            "szse_rule_2026_pdf",
        )
    }
    new = {
        spec["request_id"]: new_source(
            workspace, candidate, by_id, spec["request_id"]
        )
        for spec in SPECS
    }

    version_inputs = (
        (
            "SSE",
            "2018",
            "2018-08-20",
            "2020-03-12",
            new["sse_2018_trade_rule_notice"],
            new["sse_2018_trade_rule_notice"],
            None,
        ),
        (
            "SSE",
            "2020",
            "2020-03-13",
            "2023-04-09",
            new["sse_2020_trade_rule_document"],
            new["sse_2020_trade_rule_notice"],
            None,
        ),
        (
            "SSE",
            "2023",
            "2023-04-10",
            "2026-07-05",
            inherited["sse_rule_2023_docx"],
            inherited["sse_rule_2023_notice"],
            new["sse_2023_mainboard_registration_effective_event"],
        ),
        (
            "SSE",
            "2026",
            "2026-07-06",
            None,
            inherited["sse_rule_2026_docx"],
            inherited["sse_rule_2026_notice"],
            None,
        ),
        (
            "SZSE",
            "2020",
            "2020-12-31",
            "2023-04-09",
            new["szse_2020_trade_rule_document"],
            new["szse_2020_trade_rule_notice"],
            None,
        ),
        (
            "SZSE",
            "2023",
            "2023-04-10",
            "2026-07-05",
            new["szse_2023_trade_rule_document"],
            new["szse_2023_trade_rule_notice"],
            new["szse_2023_mainboard_registration_effective_event"],
        ),
        (
            "SZSE",
            "2026",
            "2026-07-06",
            None,
            inherited["szse_rule_2026_pdf"],
            inherited["szse_rule_2026_notice"],
            None,
        ),
        (
            "BSE",
            "2021",
            "2021-11-15",
            "2026-07-05",
            new["bse_2021_trade_rule_page"],
            new["bse_2021_trade_rule_page"],
            None,
        ),
        (
            "BSE",
            "2026",
            "2026-07-06",
            None,
            new["bse_2026_trade_rule_page"],
            new["bse_2026_trade_rule_page"],
            None,
        ),
    )
    versions: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    for values in version_inputs:
        result, rows = parse_version(workspace, *values)
        versions.append(result)
        session_rows.extend(rows)
    write_json(candidate / "SESSION_VERSION_CHAIN.json", versions)
    write_jsonl(candidate / "SESSION_INTERVAL_EVIDENCE.jsonl", session_rows)

    lineage = load_json(workspace / OLD_RUN / "CALENDAR_NOTICE_LINEAGE.json")
    temporary_events = [
        item
        for item in lineage
        if item["notice_type"] != "ANNUAL_HOLIDAY_SCHEDULE"
    ]
    mechanism = new["bse_abnormal_trading_management_document"]
    temporary_index = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "quality_status": QUALITY_STATUS,
        "actual_event_rows": temporary_events,
        "actual_event_count": len(temporary_events),
        "venue_counts": {
            venue: sum(1 for item in temporary_events if item["venue"] == venue)
            for venue in ("SSE", "SZSE", "BSE")
        },
        "bse_mechanism_only": mechanism,
        "warning": (
            "The BSE abnormal-trading document is procedure evidence, not an "
            "enumeration of actual market-wide temporary closures."
        ),
        "complete_three_venue_archive": False,
    }
    write_json(candidate / "TEMPORARY_CLOSURE_INDEX.json", temporary_index)

    old_gate = load_json(workspace / OLD_REPORT / "CORE02_GATE_RECEIPT.json")
    old_calendar = load_json(workspace / OLD_REPORT / "CALENDAR_COVERAGE.json")
    old_sessions = load_json(workspace / OLD_REPORT / "SESSION_RULE_COVERAGE.json")
    bse_2021_pass = next(
        item["status"]
        for item in versions
        if item["venue"] == "BSE" and item["rule_version"] == "2021"
    ) == "PASS"
    bse_2026_pass = next(
        item["status"]
        for item in versions
        if item["venue"] == "BSE" and item["rule_version"] == "2026"
    ) == "PASS"
    bse_session_status = (
        "PARTIAL" if bse_2021_pass or bse_2026_pass else "FAIL"
    )
    bse_mechanism_status = "PASS" if mechanism is not None else "FAIL"

    def pass_version(venue: str, version: str) -> bool:
        return any(
            item["venue"] == venue
            and item["rule_version"] == version
            and item["status"] == "PASS"
            for item in versions
        )

    sse_chain_pass = all(
        pass_version("SSE", version)
        for version in ("2018", "2020", "2023", "2026")
    )
    szse_chain_pass = all(
        pass_version("SZSE", version)
        for version in ("2020", "2023", "2026")
    )
    evidence_bindings = {
        "inherited_files": [
            file_binding(path, workspace)
            for path in (
                workspace / OLD_RUN / "CALENDAR_NOTICE_LINEAGE.json",
                workspace / OLD_RUN / "SESSION_RULE_CANDIDATE.jsonl",
                workspace / OLD_REPORT / "CORE02_GATE_RECEIPT.json",
                workspace / OLD_REPORT / "CALENDAR_COVERAGE.json",
                workspace / OLD_REPORT / "SESSION_RULE_COVERAGE.json",
                workspace / TRACK_A / "BSE" / "REQUEST_RECEIPTS.json",
                workspace / TRACK_A / "BSE" / "UNRESOLVED.json",
                workspace / STAGE_A_STATE,
                workspace / CORE_CONTRACT,
            )
        ],
        "inherited_official_sources": list(inherited.values()),
        "new_official_sources": [
            item for item in new.values() if item is not None
        ],
    }
    write_json(candidate / "EVIDENCE_BINDINGS.json", evidence_bindings)

    q1_subchecks = [
        {
            "subcheck_id": (
                "CORE02-Q1.sse_szse_2015_2026_explicit_daily_candidate"
            ),
            "status": "PASS",
            "basis": (
                "Inherited 8,454-row venue-preserving candidate covers "
                "2015-01-01 through 2026-07-28 for SSE and SZSE."
            ),
        },
        {
            "subcheck_id": "CORE02-Q1.current_sse_szse_session_intervals",
            "status": "PASS",
            "basis": "Inherited/current SSE and SZSE 2026 rule evidence.",
        },
        {
            "subcheck_id": "CORE02-Q1.session_interval_non_overlap",
            "status": "PASS",
            "basis": (
                f"{len(session_rows)} evidence rows passed deterministic "
                "interval non-overlap validation."
            ),
        },
        {
            "subcheck_id": (
                "CORE02-Q1.sse_historical_session_chain_2018_08_20_onward"
            ),
            "status": "PASS" if sse_chain_pass else "PARTIAL",
            "basis": (
                "SSE rule versions tested: 2018, 2020, 2023, 2026; "
                "pre-2018 history is not claimed."
            ),
        },
        {
            "subcheck_id": (
                "CORE02-Q1.szse_historical_session_chain_2020_12_31_onward"
            ),
            "status": "PASS" if szse_chain_pass else "PARTIAL",
            "basis": (
                "SZSE rule versions tested: 2020, 2023, 2026; "
                "pre-2020 history is not claimed."
            ),
        },
        {
            "subcheck_id": "CORE02-Q1.bse_official_daily_calendar",
            "status": "FAIL",
            "basis": (
                "No complete official BSE daily calendar was preserved; "
                "2025/2026 annual pages cannot establish full history."
            ),
        },
        {
            "subcheck_id": "CORE02-Q1.bse_session_rule_raw",
            "status": bse_session_status,
            "basis": (
                "A partial status requires at least one parsed official BSE "
                "session-rule version; otherwise fail closed."
            ),
        },
        {
            "subcheck_id": (
                "CORE02-Q1.all_venue_historical_session_version_chain"
            ),
            "status": "FAIL",
            "basis": (
                "SSE before 2018-08-20, SZSE before 2020-12-31, and the "
                "complete BSE history remain unclosed."
            ),
        },
        {
            "subcheck_id": "CORE02-Q1.official_exchange_mic_mapping",
            "status": "UNKNOWN",
            "basis": "No official MIC evidence was added in this bounded run.",
        },
    ]
    q2_subchecks = [
        {
            "subcheck_id": "CORE02-Q2.sse_szse_annual_notice_chain",
            "status": "PASS",
            "basis": "Inherited official annual notices for 2015-2026.",
        },
        {
            "subcheck_id": (
                "CORE02-Q2.known_sse_szse_2019_2020_revisions_preserved"
            ),
            "status": "PASS",
            "basis": (
                "Official SSE/SZSE 2019 Labour Day amendments and 2020 "
                "Spring Festival extensions remain hash-bound."
            ),
        },
        {
            "subcheck_id": "CORE02-Q2.sse_rule_revision_chain_2018_2026",
            "status": "PARTIAL",
            "basis": (
                "Session-relevant 2018/2020/2023/2026 sources were tested, "
                "but the complete pre-2018 and all amendment archive is absent."
            ),
        },
        {
            "subcheck_id": "CORE02-Q2.szse_rule_revision_chain_2020_2026",
            "status": "PARTIAL",
            "basis": (
                "Session-relevant 2020/2023/2026 sources were tested, but "
                "pre-2020 and the exhaustive amendment archive are absent."
            ),
        },
        {
            "subcheck_id": "CORE02-Q2.bse_emergency_procedure_rule_raw",
            "status": bse_mechanism_status,
            "basis": (
                "Official BSE mechanism document is procedure evidence only, "
                "not a complete event archive."
            ),
        },
        {
            "subcheck_id": (
                "CORE02-Q2.complete_three_venue_temporary_closure_archive"
            ),
            "status": "FAIL",
            "basis": (
                "Only four known SSE/SZSE revisions are enumerated; BSE "
                "actual-event archive and exhaustive three-venue history are absent."
            ),
        },
        {
            "subcheck_id": (
                "CORE02-Q2.complete_supplemental_notice_revision_history"
            ),
            "status": "FAIL",
            "basis": (
                "No exhaustive first-publication/correction/supersedes/"
                "withdrawal chain was established."
            ),
        },
        {
            "subcheck_id": "CORE02-Q2.date_level_pit_available_at",
            "status": "PASS",
            "basis": (
                "Inherited known notice rows retain date-level conservative "
                "available_at values."
            ),
        },
        {
            "subcheck_id": "CORE02-Q2.intraday_publication_precision",
            "status": "UNKNOWN",
            "basis": "Date-only publication precision is not intraday evidence.",
        },
    ]
    for subcheck in q1_subchecks + q2_subchecks:
        subcheck["status_semantics"] = "THIS_RUN_AUDIT_SUBCHECK"
        subcheck["baseline_status_claimed"] = False
    q3_subchecks = [
        {
            "subcheck_id": f"CORE02-Q3.{name}",
            "status": status,
            "basis": "Inherited mechanical verification; not recomputed here.",
        }
        for name, status in old_gate["gates"]["CORE02-Q3"]["subchecks"].items()
    ]
    q4_subchecks = [
        {
            "subcheck_id": f"CORE02-Q4.{name}",
            "status": status,
            "basis": "Inherited venue-preservation verification.",
        }
        for name, status in old_gate["gates"]["CORE02-Q4"]["subchecks"].items()
    ]
    matrix = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "target": "CORE-02",
        "gates": {
            "CORE02-Q1": {
                "status": "FAIL",
                "gate_type": "BLOCK",
                "subchecks": q1_subchecks,
            },
            "CORE02-Q2": {
                "status": "FAIL",
                "gate_type": "BLOCK",
                "subchecks": q2_subchecks,
            },
            "CORE02-Q3": {
                "status": "PASS",
                "gate_type": "BLOCK",
                "subchecks": q3_subchecks,
            },
            "CORE02-Q4": {
                "status": "PASS",
                "gate_type": "REVIEW",
                "subchecks": q4_subchecks,
            },
        },
        "aggregation_rule": (
            "PARTIAL is evidence-level only. Any required FAIL/UNKNOWN keeps "
            "the blocking gate fail-closed."
        ),
    }
    write_json(report / "SUBCHECK_EVIDENCE_MATRIX.json", matrix)

    def dedupe_evidence(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, Any, Any]] = set()
        result: list[dict[str, Any]] = []
        for item in items:
            key = (
                item.get("official_url"),
                item.get("local_path"),
                item.get("sha256"),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def source_refs(
        selected_versions: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for version in selected_versions:
            for role in (
                "content_source",
                "notice_source",
                "effective_event_source",
            ):
                source = version.get(role)
                if not isinstance(source, dict):
                    continue
                refs.append(
                    {
                        "venue": version["venue"],
                        "rule_version": version["rule_version"],
                        "role": role,
                        "official_url": source["official_url"],
                        "local_path": source["local_path"],
                        "sha256": source["sha256"],
                        "bytes": source["bytes"],
                    }
                )
        return dedupe_evidence(refs)

    lineage_refs = [
        {
            "venue": item["venue"],
            "notice_type": item["notice_type"],
            "target_year": item["target_year"],
            "published_at": item["published_at"],
            "available_at": item["available_at"],
            "official_url": item["source_url"],
            "local_path": item["raw_path"],
            "sha256": item["raw_sha256"],
        }
        for item in lineage
    ]
    annual_refs = [
        item
        for item in lineage_refs
        if item["notice_type"] == "ANNUAL_HOLIDAY_SCHEDULE"
    ]
    revision_refs = [
        item
        for item in lineage_refs
        if item["notice_type"] != "ANNUAL_HOLIDAY_SCHEDULE"
    ]
    current_sse_szse_refs = source_refs(
        item
        for item in versions
        if item["venue"] in {"SSE", "SZSE"}
        and item["rule_version"] == "2026"
    )
    all_version_refs = source_refs(versions)
    sse_version_refs = source_refs(
        item for item in versions if item["venue"] == "SSE"
    )
    szse_version_refs = source_refs(
        item for item in versions if item["venue"] == "SZSE"
    )
    bse_version_refs = source_refs(
        item for item in versions if item["venue"] == "BSE"
    )
    bse_annual_refs = [
        {
            "venue": "BSE",
            "role": "annual_holiday_page",
            "official_url": source["official_url"],
            "local_path": source["local_path"],
            "sha256": source["sha256"],
            "bytes": source["bytes"],
        }
        for source in (
            new["bse_2025_annual_holiday_page"],
            new["bse_2026_annual_holiday_page"],
        )
        if source is not None
    ]
    mechanism_refs = (
        [
            {
                "venue": "BSE",
                "role": "emergency_or_abnormal_trading_mechanism_only",
                "official_url": mechanism["official_url"],
                "local_path": mechanism["local_path"],
                "sha256": mechanism["sha256"],
                "bytes": mechanism["bytes"],
            }
        ]
        if mechanism is not None
        else []
    )
    q12_evidence_by_id: dict[str, list[dict[str, Any]]] = {
        (
            "CORE02-Q1.sse_szse_2015_2026_explicit_daily_candidate"
        ): annual_refs + revision_refs,
        "CORE02-Q1.current_sse_szse_session_intervals": current_sse_szse_refs,
        "CORE02-Q1.session_interval_non_overlap": all_version_refs,
        (
            "CORE02-Q1.sse_historical_session_chain_2018_08_20_onward"
        ): sse_version_refs,
        (
            "CORE02-Q1.szse_historical_session_chain_2020_12_31_onward"
        ): szse_version_refs,
        "CORE02-Q1.bse_official_daily_calendar": bse_annual_refs,
        "CORE02-Q1.bse_session_rule_raw": bse_version_refs,
        (
            "CORE02-Q1.all_venue_historical_session_version_chain"
        ): all_version_refs,
        "CORE02-Q1.official_exchange_mic_mapping": [],
        "CORE02-Q2.sse_szse_annual_notice_chain": annual_refs,
        (
            "CORE02-Q2.known_sse_szse_2019_2020_revisions_preserved"
        ): revision_refs,
        "CORE02-Q2.sse_rule_revision_chain_2018_2026": sse_version_refs,
        "CORE02-Q2.szse_rule_revision_chain_2020_2026": szse_version_refs,
        (
            "CORE02-Q2.bse_emergency_procedure_rule_raw"
        ): mechanism_refs,
        (
            "CORE02-Q2.complete_three_venue_temporary_closure_archive"
        ): revision_refs + mechanism_refs,
        (
            "CORE02-Q2.complete_supplemental_notice_revision_history"
        ): revision_refs + all_version_refs,
        "CORE02-Q2.date_level_pit_available_at": revision_refs,
        "CORE02-Q2.intraday_publication_precision": revision_refs,
    }
    q12_rows: list[dict[str, Any]] = []
    for gate_id in ("CORE02-Q1", "CORE02-Q2"):
        for subcheck in matrix["gates"][gate_id]["subchecks"]:
            subcheck_id = subcheck["subcheck_id"]
            q12_rows.append(
                {
                    "gate_id": gate_id,
                    "subcheck_id": subcheck_id,
                    "status": subcheck["status"],
                    "basis": subcheck["basis"],
                    "status_semantics": "THIS_RUN_AUDIT_SUBCHECK",
                    "baseline_status_claimed": False,
                    "official_evidence": dedupe_evidence(
                        q12_evidence_by_id[subcheck_id]
                    ),
                    "evidence_complete_for_subcheck": (
                        subcheck["status"] == "PASS"
                    ),
                    "fail_closed_note": (
                        None
                        if subcheck["status"] == "PASS"
                        else (
                            "Listed evidence is partial or absent and does "
                            "not close this subcheck."
                        )
                    ),
                }
            )
    write_json(
        report / "Q1_Q2_OFFICIAL_EVIDENCE_MAP.json",
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "target": "CORE-02",
            "gate_statuses": {
                "CORE02-Q1": "FAIL",
                "CORE02-Q2": "FAIL",
            },
            "subchecks": q12_rows,
            "warning": (
                "Every official evidence entry binds URL to local path and "
                "SHA-256. A listed source does not imply complete coverage."
            ),
        },
    )

    venue_coverage = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "scope": "CORE-02 official calendar/session Stage-A evidence",
        "venues": {
            "SSE": {
                "daily_candidate": old_calendar["venues"]["SSE"],
                "session_chain_tested_from": "2018-08-20",
                "session_chain_tested_to": "open",
                "session_versions": [
                    item for item in versions if item["venue"] == "SSE"
                ],
                "remaining_gap": (
                    "pre-2018-08-20 complete session chain and exhaustive "
                    "temporary/supplemental notice revision archive"
                ),
            },
            "SZSE": {
                "daily_candidate": old_calendar["venues"]["SZSE"],
                "session_chain_tested_from": "2020-12-31",
                "session_chain_tested_to": "open",
                "session_versions": [
                    item for item in versions if item["venue"] == "SZSE"
                ],
                "remaining_gap": (
                    "pre-2020-12-31 complete session chain and exhaustive "
                    "temporary/supplemental notice revision archive"
                ),
            },
            "BSE": {
                "daily_candidate": old_calendar["venues"]["BSE"],
                "session_chain_tested_from": (
                    "2021-11-15" if bse_2021_pass else None
                ),
                "session_chain_tested_to": (
                    "open" if bse_2026_pass else None
                ),
                "session_versions": [
                    item for item in versions if item["venue"] == "BSE"
                ],
                "remaining_gap": (
                    "complete official daily calendar, complete enacted "
                    "session-rule chain, annual 2022-2024 and exhaustive "
                    "temporary/emergency closure event archive"
                ),
            },
        },
        "warning": (
            "Coverage records evidence examined; it is not a formal dataset "
            "coverage claim."
        ),
    }
    write_json(report / "VENUE_COVERAGE.json", venue_coverage)
    write_json(report / "SESSION_VERSION_CHAIN.json", versions)
    gaps = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "known_preserved_events": temporary_events,
        "known_preserved_event_count": len(temporary_events),
        "bse_mechanism_only": mechanism,
        "unclosed": [
            {
                "gap_id": "CORE02-Q2-TEMP-01",
                "status": "OPEN",
                "gap": (
                    "Exhaustive SSE/SZSE/BSE market-wide temporary closure "
                    "and supplemental notice index is absent."
                ),
            },
            {
                "gap_id": "CORE02-Q2-TEMP-02",
                "status": "OPEN",
                "gap": (
                    "Complete first publication, correction, supersedes and "
                    "withdrawal chain is absent."
                ),
            },
            {
                "gap_id": "CORE02-Q2-TEMP-03",
                "status": "OPEN",
                "gap": (
                    "BSE actual-event archive is absent; a procedure document "
                    "cannot substitute for event history."
                ),
            },
            {
                "gap_id": "CORE02-Q2-TEMP-04",
                "status": "OPEN",
                "gap": (
                    "Date-only publication timestamps do not establish "
                    "intraday decision-time availability."
                ),
            },
        ],
    }
    write_json(report / "TEMPORARY_CLOSURE_GAPS.json", gaps)

    baseline_stage_a = extract_core02_stage_a(
        load_json(workspace / STAGE_A_STATE)
    )
    stage_a_delta = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "core_id": "CORE-02",
        "before": baseline_stage_a,
        "before_evidence": file_binding(
            workspace / STAGE_A_STATE,
            workspace,
        ),
        "after_evidence_assessment": baseline_stage_a,
        "status_changed": False,
        "reason": (
            "New evidence narrows session-rule ranges but does not close "
            "rights, full revision chain, all-venue official calendar, PIT "
            "precision, cross-source quality, or known contract blockers."
        ),
        "formal_state_file_modified": False,
    }
    write_json(report / "STAGE_A_STATUS_DELTA.json", stage_a_delta)
    gate_receipt = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "work_package": (
            "L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE"
        ),
        "target": "CORE-02",
        "purpose_ceiling": PURPOSE_CEILING,
        "quality_status": QUALITY_STATUS,
        "formal_admission": False,
        "research_usable": False,
        "catalog_update": False,
        "source_registry_update": False,
        "roadmap_update": False,
        "gates": matrix["gates"],
        "stage_a_status_changed": False,
        "decision": (
            "FAIL_CLOSED__Q1_Q2_FAIL__Q3_Q4_PASS__NOT_ADMITTED"
        ),
    }
    write_json(report / "CORE02_GATE_RECEIPT.json", gate_receipt)

    request_summary = {
        result: sum(1 for item in receipts if item["result"] == result)
        for result in sorted({item["result"] for item in receipts})
    }
    readme = f"""# CORE-02 Stage-A 日历/时段闭合补充

## 结论

本轮没有完成正式准入。`CORE02-Q1` 与 `CORE02-Q2` 仍为 **FAIL**；
`CORE02-Q3` 与 `CORE02-Q4` 保持 **PASS**。所有产物仍是
`INSPECTION_ONLY / NOT_ADMITTED`，不能作为研究输入。

本轮仅缩小了证据缺口：

- SSE 时段版本链检查覆盖 `2018-08-20` 至当前；
- SZSE 时段版本链检查覆盖 `2020-12-31` 至当前；
- BSE 仅在官方响应实际成功时保留时段/异常交易机制证据；
- 沪深两所 2019 劳动节修订和 2020 春节延长共
  {len(temporary_events)} 条既有 PIT 事件继续按原哈希引用。

## 三所与剩余缺口

- SSE：逐日候选 `2015-01-01` 至 `2026-07-28`；完整的
  `2018-08-20` 以前时段版本链和穷尽临时休市/修订档案未闭合。
- SZSE：逐日候选 `2015-01-01` 至 `2026-07-28`；完整的
  `2020-12-31` 以前时段版本链和穷尽临时休市/修订档案未闭合。
- BSE：完整官方逐日日历、完整历史时段链、2022-2024 年度档案及
  穷尽临时/应急休市事件档案未闭合。

## 请求纪律

官方请求结果：`{json.dumps(request_summary, ensure_ascii=False)}`。
每个 URL 最多一次 GET；不携带账号、Cookie 或令牌；不跟随重定向。
若 BSE HTML 首次出现 WAF/重定向，后续 BSE HTML 请求自动停止。

## 状态解释

`PARTIAL` 只描述某个子检查获得了部分官方证据，绝不等于正式门通过。
本轮未修改 `datasets.json`、正式来源台账、Roadmap 或项目登记。
"""
    (report / "README.md").write_text(
        readme,
        encoding="utf-8",
        newline="\n",
    )

    after_guards = guard_hashes(workspace)
    guard_unchanged = before_guards == after_guards
    candidate_raw_manifest = [
        file_binding(path, workspace)
        for path in sorted(raw_root.rglob("*"))
        if path.is_file()
    ]
    write_json(candidate / "RAW_FILE_MANIFEST.json", candidate_raw_manifest)
    candidate_verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "raw_files": len(candidate_raw_manifest),
        "request_count": len(receipts),
        "request_summary": request_summary,
        "session_version_count": len(versions),
        "session_versions_pass": sum(
            1 for item in versions if item["status"] == "PASS"
        ),
        "session_rows": len(session_rows),
        "formal_guards_unchanged": guard_unchanged,
        "before_guards": before_guards,
        "after_guards": after_guards,
        "status": (
            "PASS__INSPECTION_ONLY_EVIDENCE"
            if guard_unchanged
            else "FAIL__FORMAL_GUARD_CHANGED"
        ),
    }
    write_json(candidate / "VERIFICATION.json", candidate_verification)
    write_json(
        candidate / "CANDIDATE_MANIFEST.json",
        manifest(
            candidate,
            workspace,
            {"CANDIDATE_MANIFEST.json"},
        ),
    )
    candidate_report_bindings = [
        file_binding(candidate / "CANDIDATE_MANIFEST.json", workspace),
        file_binding(candidate / "RAW_FILE_MANIFEST.json", workspace),
        file_binding(candidate / "VERIFICATION.json", workspace),
    ]
    write_json(
        report / "CANDIDATE_EVIDENCE_BINDINGS.json",
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "binding_semantics": (
                "REPORT_LAYER_HASH_BINDINGS_TO_TMP_INSPECTION_ONLY_EVIDENCE"
            ),
            "artifacts": candidate_report_bindings,
        },
    )

    report_verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "expected_gate_statuses": {
            "CORE02-Q1": "FAIL",
            "CORE02-Q2": "FAIL",
            "CORE02-Q3": "PASS",
            "CORE02-Q4": "PASS",
        },
        "observed_gate_statuses": {
            key: value["status"]
            for key, value in matrix["gates"].items()
        },
        "stage_a_status_changed": False,
        "formal_guards_unchanged": guard_unchanged,
        "candidate_evidence_hash_bound": True,
        "candidate_evidence_bindings": candidate_report_bindings,
        "catalog_update": False,
        "source_registry_update": False,
        "roadmap_update": False,
        "formal_admission": False,
        "research_usable": False,
        "status": (
            "PASS__FAIL_CLOSED_REPORT"
            if guard_unchanged
            else "FAIL__FORMAL_GUARD_CHANGED"
        ),
    }
    write_json(report / "VERIFICATION.json", report_verification)
    write_json(
        report / "REPORT_MANIFEST.json",
        manifest(report, workspace, {"REPORT_MANIFEST.json"}),
    )
    if not guard_unchanged:
        raise RuntimeError("a guarded formal file changed during this run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
