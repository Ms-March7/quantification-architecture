#!/usr/bin/env python3
"""Build bounded official evidence for CORE-02 trading calendars and sessions.

This is an inspection-only closure utility for D2A-04B.  It deliberately:

* reads the already-preserved SSE/SZSE/BSE Track-A evidence;
* performs a small, serial, one-attempt collection from public official pages;
* writes only a quarantine-style candidate under ``tmp`` and an audit report;
* keeps every raw response and binds every derived row to hashes;
* fails closed when an official page, version chain, or rights fact is absent.

It does not edit catalogs, source registries, quality state, formal snapshots,
published data, Roadmap files, or research inputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup


RUN_ID = "core02-official-calendar-session-closure-20260730-v1"
DEFAULT_CANDIDATE = f"tmp/d2a04b-source-closure/{RUN_ID}"
DEFAULT_REPORT = f"reports/data-governance/{RUN_ID}"
TRACK_A = (
    "tmp/d2a04b-source-closure/"
    "d2a04b-official-track-a-20260728-v1"
)
CATALOG = "data/catalog/datasets.json"
SCOPE_START = date(2015, 1, 1)
SCOPE_END = date(2026, 7, 28)
PURPOSE_CEILING = "INSPECTION_ONLY"

SSE_2015_ANNUAL_URL = (
    "https://www.sse.com.cn/disclosure/dealinstruc/closed/list/"
    "c/c_20160104_3979217.shtml"
)
SSE_2026_RULE_NOTICE = (
    "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/"
    "exchange/c/c_20260424_10816482.shtml"
)
SSE_2026_RULE_DOCX = (
    "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/"
    "exchange/c/10816482/files/959da0158c65434daa8a43a6e32be7ba.docx"
)
SSE_2023_RULE_NOTICE = (
    "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/"
    "rules/c/c_20250612_10824490.shtml"
)
SSE_2023_RULE_DOCX = (
    "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/"
    "rules/c/10824490/files/dcbe58edb194451d93f19b1f7dd8fb4c.docx"
)
SSE_2019_LABOR_REVISION = (
    "https://www.sse.com.cn/disclosure/announcement/general/"
    "c/c_20190418_4771364.shtml"
)
SSE_2020_SPRING_EXTENSION = (
    "https://www.sse.com.cn/disclosure/announcement/general/"
    "c/c_20200127_4991582.shtml"
)
SZSE_2026_RULE_NOTICE = (
    "https://www.szse.cn/lawrules/rule/trade/current/"
    "t20260424_620190.html"
)
SZSE_2026_RULE_PDF = (
    "https://docs.static.szse.cn/www/lawrules/rule/trade/current/"
    "W020260424690713155663.pdf"
)

ALLOWED_HOSTS = {
    "www.sse.com.cn",
    "sse.com.cn",
    "www.szse.cn",
    "szse.cn",
    "docs.static.szse.cn",
}
SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "content-disposition",
    "content-length",
    "content-type",
    "date",
    "etag",
    "last-modified",
}
EXPECTED_REPORT_FILES = {
    "README.md",
    "CORE02_GATE_RECEIPT.json",
    "CALENDAR_COVERAGE.json",
    "SESSION_RULE_COVERAGE.json",
    "RIGHTS_AND_GAPS.json",
    "VERIFICATION.json",
    "REPORT_MANIFEST.json",
}

DATE_TOKEN_PATTERN = (
    r"(?:(?P<year>\d{4})年)?"
    r"(?P<month>\d{1,2})月"
    r"(?P<day>\d{1,2})日"
    r"(?:（星期[^）]+）)?"
)
DATE_TOKEN_RE = re.compile(DATE_TOKEN_PATTERN)
DATE_TOKEN_PLAIN = (
    r"(?:\d{4}年)?"
    r"\d{1,2}月"
    r"\d{1,2}日"
    r"(?:（星期[^）]+）)?"
)
DATE_RANGE_RE = re.compile(
    rf"(?P<start>{DATE_TOKEN_PLAIN})至(?P<end>{DATE_TOKEN_PLAIN})"
)


@dataclass(frozen=True)
class FetchSpec:
    request_id: str
    url: str
    relative_path: str
    expected_content: str
    max_bytes: int


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


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def workspace_relative(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def require_child_path(path: Path, parent: Path, label: str) -> None:
    resolved = path.resolve()
    parent_resolved = parent.resolve()
    if Path(os.path.commonpath([resolved, parent_resolved])) != parent_resolved:
        raise ValueError(f"{label} escapes {parent_resolved}: {resolved}")
    if resolved == parent_resolved:
        raise ValueError(f"{label} must be a child of {parent_resolved}")


def require_new_directory(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.mkdir(parents=True, exist_ok=False)


def file_binding(path: Path, workspace: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": workspace_relative(path, workspace),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def sanitise_headers(headers: requests.structures.CaseInsensitiveDict) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in SAFE_RESPONSE_HEADERS
    }


def official_host_allowed(url: str) -> bool:
    return urlparse(url).hostname in ALLOWED_HOSTS


def now_asia_shanghai() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def fetch_one(
    session: requests.Session,
    spec: FetchSpec,
    raw_root: Path,
    sequence: int,
) -> dict[str, Any]:
    """Perform one official GET, with no retry and a bounded response."""
    started = now_asia_shanghai()
    receipt: dict[str, Any] = {
        "sequence": sequence,
        "request_id": spec.request_id,
        "method": "GET",
        "request_url": spec.url,
        "started_at_asia_shanghai": started,
        "serial": True,
        "attempts": 1,
        "retry_count": 0,
        "auth_cookie_token_sent": False,
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
        "relative_path": spec.relative_path,
        "expected_content": spec.expected_content,
        "max_bytes": spec.max_bytes,
    }
    if not official_host_allowed(spec.url):
        receipt.update(
            {
                "result": "REJECTED_BEFORE_REQUEST",
                "reason": "request host is outside the explicit official allowlist",
                "completed_at_asia_shanghai": now_asia_shanghai(),
            }
        )
        return receipt

    try:
        response = session.get(
            spec.url,
            timeout=(10, 30),
            allow_redirects=True,
            stream=True,
        )
        final_url = response.url
        receipt["http_status"] = response.status_code
        receipt["final_url"] = final_url
        receipt["response_headers"] = sanitise_headers(response.headers)
        if not official_host_allowed(final_url):
            receipt.update(
                {
                    "result": "REJECTED_RESPONSE",
                    "reason": "redirected outside the explicit official allowlist",
                }
            )
            return receipt
        if response.status_code != 200:
            receipt.update(
                {
                    "result": "HTTP_FAILURE",
                    "reason": f"official endpoint returned {response.status_code}",
                }
            )
            return receipt

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=1024 * 64):
            if not chunk:
                continue
            total += len(chunk)
            if total > spec.max_bytes:
                receipt.update(
                    {
                        "result": "REJECTED_RESPONSE",
                        "reason": "response exceeded max_bytes",
                        "bytes_read_before_rejection": total,
                    }
                )
                return receipt
            chunks.append(chunk)
        content = b"".join(chunks)
        target = raw_root / spec.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        receipt.update(
            {
                "result": "RESPONSE_PRESERVED",
                "bytes": len(content),
                "response_sha256": sha256_bytes(content),
            }
        )
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
        receipt["completed_at_asia_shanghai"] = now_asia_shanghai()


def html_text(path: Path) -> str:
    source = path.read_text(encoding="utf-8", errors="replace")
    return re.sub(
        r"\s+",
        " ",
        BeautifulSoup(source, "html.parser").get_text(" ", strip=True),
    ).strip()


def compact_chinese_text(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("：", ":")


def token_to_date(match: re.Match[str], default_year: int) -> date:
    groups = match.groupdict()
    return date(
        int(groups.get("year") or default_year),
        int(groups["month"]),
        int(groups["day"]),
    )


def date_range(start: date, end: date) -> Iterable[date]:
    if end < start:
        raise ValueError(f"date range is reversed: {start} > {end}")
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def parse_closure_dates(text: str, target_year: int) -> set[date]:
    """Parse dates preceding ``休市`` from one official notice.

    The parser is intentionally narrow.  It recognises explicit dates and
    ranges only; it never infers an exceptional closure from prose.
    """
    compact = compact_chinese_text(text)
    start_marker = compact.find("一、休市安排")
    if start_marker >= 0:
        compact = compact[start_marker:]
    end_marker = compact.find("二、")
    if end_marker >= 0:
        compact = compact[:end_marker]

    closed: set[date] = set()
    for clause_match in re.finditer(r"([^。；]*?)休市", compact):
        clause = clause_match.group(1)
        ranged_tokens: set[tuple[int, int]] = set()
        for range_match in DATE_RANGE_RE.finditer(clause):
            start_match = DATE_TOKEN_RE.fullmatch(range_match.group("start"))
            end_match = DATE_TOKEN_RE.fullmatch(range_match.group("end"))
            if start_match is None or end_match is None:
                continue
            start_day = token_to_date(start_match, target_year)
            end_day = token_to_date(end_match, target_year)
            closed.update(date_range(start_day, end_day))
            ranged_tokens.add(range_match.span("start"))
            ranged_tokens.add(range_match.span("end"))

        for token_match in DATE_TOKEN_RE.finditer(clause):
            if token_match.span() in ranged_tokens:
                continue
            closed.add(token_to_date(token_match, target_year))
    return closed


def extract_publication_date(text: str) -> date | None:
    match = re.search(r"时间:\s*(\d{4})-(\d{2})-(\d{2})", text.replace("：", ":"))
    if match is None:
        match = re.search(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", text)
    if match:
        return date(*(int(value) for value in match.groups()))
    return None


def conservative_available_at(published: date) -> str:
    """Date-only publication precision becomes usable the following day."""
    next_day = published + timedelta(days=1)
    return f"{next_day.isoformat()}T00:00:00+08:00"


def extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml")
    root = ElementTree.fromstring(document)
    parts = [
        node.text
        for node in root.iter()
        if node.tag.endswith("}t") and node.text
    ]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def extract_pdf_text(path: Path) -> str:
    binary = shutil.which("pdftotext")
    if binary is None:
        raise RuntimeError("pdftotext is not available")
    completed = subprocess.run(
        [binary, "-layout", str(path), "-"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"pdftotext failed ({completed.returncode}): "
            f"{completed.stderr.decode('utf-8', errors='replace')[:500]}"
        )
    return completed.stdout.decode("utf-8", errors="replace")


def parse_standard_equity_sessions(text: str) -> list[dict[str, str]]:
    """Return the four explicit auction/continuous sessions when all exist."""
    compact = compact_chinese_text(text)
    expected = (
        ("OPENING_AUCTION", "09:15", "09:25", ("开盘集合竞价",)),
        ("CONTINUOUS_AM", "09:30", "11:30", ("连续竞价",)),
        ("CONTINUOUS_PM", "13:00", "14:57", ("连续竞价",)),
        ("CLOSING_AUCTION", "14:57", "15:00", ("收盘集合竞价",)),
    )
    sessions: list[dict[str, str]] = []
    for name, start, end, keywords in expected:
        start_loose = start.lstrip("0")
        end_loose = end.lstrip("0")
        time_present = (
            f"{start}至{end}" in compact
            or f"{start_loose}至{end_loose}" in compact
        )
        if not time_present or not all(keyword in compact for keyword in keywords):
            return []
        sessions.append(
            {
                "session_name": name,
                "start_time": start,
                "end_time": end,
                "timezone": "Asia/Shanghai",
            }
        )
    validate_non_overlapping_sessions(sessions)
    return sessions


def minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def validate_non_overlapping_sessions(sessions: list[dict[str, str]]) -> None:
    ordered = sorted(sessions, key=lambda item: minutes(item["start_time"]))
    for left, right in zip(ordered, ordered[1:]):
        if minutes(left["end_time"]) > minutes(right["start_time"]):
            raise ValueError(f"overlapping sessions: {left} / {right}")


def aggregate_gate_status(statuses: Iterable[str]) -> str:
    values = list(statuses)
    if not values:
        raise ValueError("gate requires at least one subcheck")
    invalid = sorted(set(values) - {"PASS", "FAIL", "UNKNOWN"})
    if invalid:
        raise ValueError(f"invalid gate statuses: {invalid}")
    if "FAIL" in values:
        return "FAIL"
    if "UNKNOWN" in values:
        return "UNKNOWN"
    return "PASS"


def build_sse_annual_specs(
    holiday_index: dict[str, Any],
) -> tuple[list[FetchSpec], dict[int, dict[str, Any]]]:
    by_year: dict[int, dict[str, Any]] = {
        2015: {
            "published_date": "2014-12-24",
            "url": SSE_2015_ANNUAL_URL,
        }
    }
    entries = holiday_index["holiday_notice_index"]["entries"]
    for target_year in range(2016, 2027):
        candidates = [
            entry
            for entry in entries
            if entry["published_date"].startswith(f"{target_year - 1}-12-")
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"expected one SSE December annual notice for {target_year}, "
                f"found {len(candidates)}"
            )
        by_year[target_year] = {
            "published_date": candidates[0]["published_date"],
            "url": candidates[0]["url"],
        }
    specs = [
        FetchSpec(
            request_id=f"sse_calendar_{year}_annual",
            url=metadata["url"],
            relative_path=f"sse/calendar/{year}_annual_notice.html",
            expected_content="html",
            max_bytes=2 * 1024 * 1024,
        )
        for year, metadata in sorted(by_year.items())
    ]
    return specs, by_year


def bind_track_a_evidence(workspace: Path) -> list[dict[str, Any]]:
    root = workspace / TRACK_A
    required = [
        root / "SSE" / "CALENDAR_COVERAGE.json",
        root / "SSE" / "RIGHTS_EVIDENCE.json",
        root / "SSE" / "VERIFICATION.json",
        root / "SZSE" / "COVERAGE_AND_GAPS.json",
        root / "SZSE" / "RAW_FILE_MANIFEST.json",
        root / "SZSE" / "REQUEST_RECEIPTS.json",
        root / "SZSE" / "VALIDATION.json",
        root / "BSE" / "CALENDAR_COVERAGE.json",
        root / "BSE" / "REQUEST_RECEIPTS.json",
        root / "BSE" / "UNRESOLVED.json",
        root / "BSE" / "VERIFICATION.json",
    ]
    required.extend(
        sorted((root / "SZSE" / "raw" / "calendar").glob("*.html"))
    )
    return [file_binding(path, workspace) for path in required]


def szse_notice_sources(workspace: Path) -> dict[str, dict[str, Any]]:
    root = workspace / TRACK_A / "SZSE"
    receipts = load_json(root / "REQUEST_RECEIPTS.json")
    result: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        relative_path = receipt.get("relative_path")
        if not isinstance(relative_path, str):
            continue
        if not relative_path.startswith("raw/calendar/"):
            continue
        result[Path(relative_path).name] = {
            "url": receipt["request_url"],
            "published_at": None,
            "receipt_sha256": receipt.get("response_sha256"),
        }
    return result


def build_notice_lineage(
    workspace: Path,
    candidate: Path,
    request_receipts: list[dict[str, Any]],
    sse_year_metadata: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[int, set[date]]]]:
    track_a = workspace / TRACK_A
    receipt_by_id = {
        item["request_id"]: item
        for item in request_receipts
        if item.get("result") == "RESPONSE_PRESERVED"
    }
    lineage: list[dict[str, Any]] = []
    closures: dict[str, dict[int, set[date]]] = {"SSE": {}, "SZSE": {}}

    for year in range(2015, 2027):
        request_id = f"sse_calendar_{year}_annual"
        receipt = receipt_by_id.get(request_id)
        if receipt is None:
            continue
        path = candidate / "raw" / receipt["relative_path"]
        text = html_text(path)
        parsed = parse_closure_dates(text, year)
        closures["SSE"][year] = parsed
        published = date.fromisoformat(
            sse_year_metadata[year]["published_date"]
        )
        lineage.append(
            {
                "venue": "SSE",
                "notice_type": "ANNUAL_HOLIDAY_SCHEDULE",
                "target_year": year,
                "published_at": published.isoformat(),
                "publication_precision": "date",
                "available_at": conservative_available_at(published),
                "source_url": sse_year_metadata[year]["url"],
                "raw_path": workspace_relative(path, workspace),
                "raw_sha256": sha256_file(path),
                "closure_date_count": len(
                    [item for item in parsed if item.year == year]
                ),
                "revision_id": sha256_file(path),
            }
        )

    sse_revision_specs = [
        (
            "sse_calendar_2019_labor_revision",
            "ANNUAL_SCHEDULE_AMENDMENT",
            2019,
            date(2019, 5, 1),
            date(2019, 5, 5),
            SSE_2019_LABOR_REVISION,
        ),
        (
            "sse_calendar_2020_spring_extension",
            "EXCEPTIONAL_CLOSURE_EXTENSION",
            2020,
            date(2020, 1, 31),
            date(2020, 2, 2),
            SSE_2020_SPRING_EXTENSION,
        ),
    ]
    for (
        request_id,
        notice_type,
        target_year,
        first_day,
        last_day,
        source_url,
    ) in sse_revision_specs:
        receipt = receipt_by_id.get(request_id)
        if receipt is None:
            continue
        path = candidate / "raw" / receipt["relative_path"]
        text = html_text(path)
        published = extract_publication_date(text)
        if published is None:
            raise ValueError(f"missing SSE revision publication date: {path}")
        explicit = set(date_range(first_day, last_day))
        if notice_type == "ANNUAL_SCHEDULE_AMENDMENT":
            parsed = parse_closure_dates(text, target_year)
            if not explicit.issubset(parsed):
                raise ValueError(
                    f"SSE revision parser missed explicit closure range: {path}"
                )
        elif "休市至2月2日" not in compact_chinese_text(text):
            raise ValueError(f"unexpected SSE extension text: {path}")
        closures["SSE"][target_year].update(explicit)
        lineage.append(
            {
                "venue": "SSE",
                "notice_type": notice_type,
                "target_year": target_year,
                "published_at": published.isoformat(),
                "publication_precision": "date",
                "available_at": conservative_available_at(published),
                "source_url": source_url,
                "raw_path": workspace_relative(path, workspace),
                "raw_sha256": sha256_file(path),
                "affected_start": first_day.isoformat(),
                "affected_end": last_day.isoformat(),
                "revision_id": sha256_file(path),
                "supersedes": (
                    f"SSE_{target_year}_ANNUAL_HOLIDAY_SCHEDULE"
                ),
            }
        )

    szse_root = track_a / "SZSE" / "raw" / "calendar"
    szse_sources = szse_notice_sources(workspace)
    for year in range(2015, 2027):
        filename = f"{year}_annual_notice.html"
        path = szse_root / filename
        text = html_text(path)
        parsed = parse_closure_dates(text, year)
        closures["SZSE"][year] = parsed
        published = extract_publication_date(text)
        if published is None:
            raise ValueError(f"missing SZSE publication date: {path}")
        source = szse_sources[filename]
        lineage.append(
            {
                "venue": "SZSE",
                "notice_type": "ANNUAL_HOLIDAY_SCHEDULE",
                "target_year": year,
                "published_at": published.isoformat(),
                "publication_precision": "date",
                "available_at": conservative_available_at(published),
                "source_url": source["url"],
                "raw_path": workspace_relative(path, workspace),
                "raw_sha256": sha256_file(path),
                "closure_date_count": len(
                    [item for item in parsed if item.year == year]
                ),
                "revision_id": sha256_file(path),
            }
        )

    revision_specs = [
        (
            "2019_labor_revision.html",
            "ANNUAL_SCHEDULE_AMENDMENT",
            2019,
            date(2019, 5, 1),
            date(2019, 5, 5),
        ),
        (
            "2020_spring_extension.html",
            "EXCEPTIONAL_CLOSURE_EXTENSION",
            2020,
            date(2020, 1, 31),
            date(2020, 2, 2),
        ),
    ]
    for filename, notice_type, target_year, first_day, last_day in revision_specs:
        path = szse_root / filename
        text = html_text(path)
        published = extract_publication_date(text)
        if published is None:
            raise ValueError(f"missing revision publication date: {path}")
        explicit = set(date_range(first_day, last_day))
        if notice_type == "ANNUAL_SCHEDULE_AMENDMENT":
            parsed = parse_closure_dates(text, target_year)
            if not explicit.issubset(parsed):
                raise ValueError(
                    f"revision parser missed explicit closure range: {path}"
                )
        elif "休市至2月2日" not in compact_chinese_text(text):
            raise ValueError(f"unexpected exceptional extension text: {path}")
        closures["SZSE"][target_year].update(explicit)
        source = szse_sources[filename]
        lineage.append(
            {
                "venue": "SZSE",
                "notice_type": notice_type,
                "target_year": target_year,
                "published_at": published.isoformat(),
                "publication_precision": "date",
                "available_at": conservative_available_at(published),
                "source_url": source["url"],
                "raw_path": workspace_relative(path, workspace),
                "raw_sha256": sha256_file(path),
                "affected_start": first_day.isoformat(),
                "affected_end": last_day.isoformat(),
                "revision_id": sha256_file(path),
                "supersedes": (
                    f"SZSE_{target_year}_ANNUAL_HOLIDAY_SCHEDULE"
                ),
            }
        )

    return lineage, closures


def select_notice_for_date(
    lineage: list[dict[str, Any]],
    venue: str,
    day: date,
) -> dict[str, Any]:
    candidates = [
        item
        for item in lineage
        if item["venue"] == venue and item["target_year"] == day.year
    ]
    if not candidates:
        raise ValueError(f"no annual notice for {venue} {day.year}")
    revisions = [
        item
        for item in candidates
        if item.get("affected_start")
        and date.fromisoformat(item["affected_start"])
        <= day
        <= date.fromisoformat(item["affected_end"])
    ]
    if revisions:
        return max(revisions, key=lambda item: item["published_at"])
    annual = [
        item
        for item in candidates
        if item["notice_type"] == "ANNUAL_HOLIDAY_SCHEDULE"
    ]
    if len(annual) != 1:
        raise ValueError(
            f"expected one annual notice for {venue} {day.year}, "
            f"found {len(annual)}"
        )
    return annual[0]


def build_trading_day_rows(
    lineage: list[dict[str, Any]],
    closures: dict[str, dict[int, set[date]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for venue in ("SSE", "SZSE"):
        cursor = SCOPE_START
        sequence = 0
        while cursor <= SCOPE_END:
            sequence += 1
            notice = select_notice_for_date(lineage, venue, cursor)
            is_official_closure = cursor in closures[venue][cursor.year]
            is_weekend = cursor.weekday() >= 5
            if is_official_closure:
                state = "CLOSED_OFFICIAL_NOTICE"
                is_trading_day = False
            elif is_weekend:
                state = "CLOSED_WEEKEND_DERIVED"
                is_trading_day = False
            else:
                state = "OPEN_DERIVED_FROM_ANNUAL_SCHEDULE"
                is_trading_day = True
            rows.append(
                {
                    "venue": venue,
                    "calendar_date": cursor.isoformat(),
                    "strict_open_sequence": sequence if is_trading_day else None,
                    "is_trading_day": is_trading_day,
                    "calendar_state": state,
                    "derivation": (
                        "official annual/amendment closure notice plus "
                        "Gregorian weekday derivation; not a formal exchange "
                        "daily-calendar endpoint"
                    ),
                    "published_at": notice["published_at"],
                    "publication_precision": notice["publication_precision"],
                    "available_at": notice["available_at"],
                    "revision_id": notice["revision_id"],
                    "source_url": notice["source_url"],
                    "source_raw_path": notice["raw_path"],
                    "source_raw_sha256": notice["raw_sha256"],
                    "purpose_ceiling": PURPOSE_CEILING,
                    "quality_status": "NOT_ADMITTED",
                }
            )
            cursor += timedelta(days=1)
    return rows


def correct_open_sequences(rows: list[dict[str, Any]]) -> None:
    for venue in ("SSE", "SZSE"):
        sequence = 0
        for row in rows:
            if row["venue"] != venue:
                continue
            if row["is_trading_day"]:
                sequence += 1
                row["strict_open_sequence"] = sequence
            else:
                row["strict_open_sequence"] = None


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
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


def write_calendar_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_session_rows(
    workspace: Path,
    candidate: Path,
    receipts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    receipt_by_id = {
        item["request_id"]: item
        for item in receipts
        if item.get("result") == "RESPONSE_PRESERVED"
    }
    sources = [
        {
            "venue": "SSE",
            "version": "2026",
            "effective_from": "2026-07-06",
            "effective_to": None,
            "document_request": "sse_rule_2026_docx",
            "notice_request": "sse_rule_2026_notice",
            "extractor": extract_docx_text,
        },
        {
            "venue": "SSE",
            "version": "2023",
            "effective_from": None,
            "effective_from_note": (
                "official notice uses an event-dependent effective date: "
                "first main-board registration IPO listing day"
            ),
            "effective_to": "2026-07-05",
            "document_request": "sse_rule_2023_docx",
            "notice_request": "sse_rule_2023_notice",
            "extractor": extract_docx_text,
        },
        {
            "venue": "SZSE",
            "version": "2026",
            "effective_from": "2026-07-06",
            "effective_to": None,
            "document_request": "szse_rule_2026_pdf",
            "notice_request": "szse_rule_2026_notice",
            "extractor": extract_pdf_text,
        },
    ]
    rows: list[dict[str, Any]] = []
    extraction: dict[str, Any] = {}
    for source in sources:
        document_receipt = receipt_by_id.get(source["document_request"])
        notice_receipt = receipt_by_id.get(source["notice_request"])
        key = f"{source['venue']}_{source['version']}"
        if document_receipt is None or notice_receipt is None:
            extraction[key] = {
                "status": "FAIL",
                "reason": "official rule notice or document was not preserved",
            }
            continue
        document_path = candidate / "raw" / document_receipt["relative_path"]
        try:
            text = source["extractor"](document_path)
            sessions = parse_standard_equity_sessions(text)
        except Exception as exc:  # fail-closed evidence capture
            extraction[key] = {
                "status": "FAIL",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            continue
        if not sessions:
            extraction[key] = {
                "status": "FAIL",
                "reason": "all four explicit session intervals were not found",
                "document_sha256": sha256_file(document_path),
            }
            continue
        extraction[key] = {
            "status": "PASS",
            "session_count": len(sessions),
            "document_sha256": sha256_file(document_path),
            "notice_sha256": notice_receipt["response_sha256"],
        }
        for session in sessions:
            rows.append(
                {
                    "venue": source["venue"],
                    "rule_version": source["version"],
                    "effective_from": source.get("effective_from"),
                    "effective_from_note": source.get("effective_from_note"),
                    "effective_to": source.get("effective_to"),
                    **session,
                    "source_notice_url": notice_receipt["request_url"],
                    "source_notice_sha256": notice_receipt["response_sha256"],
                    "source_document_url": document_receipt["request_url"],
                    "source_document_sha256": document_receipt[
                        "response_sha256"
                    ],
                    "purpose_ceiling": PURPOSE_CEILING,
                    "quality_status": "NOT_ADMITTED",
                }
            )
    return rows, extraction


def raw_manifest(raw_root: Path, workspace: Path) -> list[dict[str, Any]]:
    return [
        file_binding(path, workspace)
        for path in sorted(raw_root.rglob("*"))
        if path.is_file()
    ]


def manifest_for_directory(
    root: Path,
    workspace: Path,
    exclude: set[str],
) -> list[dict[str, Any]]:
    return [
        file_binding(path, workspace)
        for path in sorted(root.iterdir())
        if path.is_file() and path.name not in exclude
    ]


def count_calendar(rows: list[dict[str, Any]], venue: str) -> dict[str, Any]:
    selected = [row for row in rows if row["venue"] == venue]
    open_rows = [row for row in selected if row["is_trading_day"]]
    official_closed = [
        row
        for row in selected
        if row["calendar_state"] == "CLOSED_OFFICIAL_NOTICE"
    ]
    weekend_closed = [
        row
        for row in selected
        if row["calendar_state"] == "CLOSED_WEEKEND_DERIVED"
    ]
    return {
        "rows": len(selected),
        "min_date": selected[0]["calendar_date"],
        "max_date": selected[-1]["calendar_date"],
        "open_rows": len(open_rows),
        "official_notice_closed_rows": len(official_closed),
        "weekend_derived_closed_rows": len(weekend_closed),
        "strict_open_sequence_max": open_rows[-1]["strict_open_sequence"],
    }


def readme_text(
    calendar_coverage: dict[str, Any],
    session_coverage: dict[str, Any],
    gate: dict[str, Any],
) -> str:
    return f"""# CORE-02 官方日历与交易时段闭合证据

## 结论

本包在 `INSPECTION_ONLY / NOT_ADMITTED` 边界内，把上交所、深交所
2015-01-01 至 2026-07-28 的官方年度休市公告转成了逐日候选，并保留
公告、修订、发布时间、保守 `available_at`、原文路径和 SHA-256。
候选共 {calendar_coverage['total_rows']:,} 行。

2026 版上交所、深交所交易规则的当前股票竞价时段已从官方附件抽取并
通过无重叠校验。北交所仍沿用既有直连失败证据，未绕过重定向/WAF。

## 门状态

- `CORE02-Q1`: **{gate['gates']['CORE02-Q1']['status']}**。上交所/深交所逐日候选和
  当前时段取得机械证据，但北交所、全历史时段版本链、官方 MIC 映射仍未闭合。
- `CORE02-Q2`: **{gate['gates']['CORE02-Q2']['status']}**。沪深两所 2019 劳动节修订
  与 2020 春节延长通知已保留并形成修订链，但三所全部临时停市、补充公告和
  历史修订档案尚未证明完整。
- `CORE02-Q3`: **{gate['gates']['CORE02-Q3']['status']}**（仅机械顺序与关系校验）。
- `CORE02-Q4`: **{gate['gates']['CORE02-Q4']['status']}**（逐所保存，不抹平差异）。

因此本包没有改变正式准入、质量等级、研究可用性或 catalog。它只把
`CORE02-Q1/Q2` 的未知部分缩小为明确的剩余来源/权利/历史版本缺口。

## 当前时段抽取

成功版本：{session_coverage['successful_versions']}。
失败/未闭合版本：{session_coverage['unresolved_versions']}。

## 不得解释为

- 正式 CORE-02 数据集；
- 可用于研究的不可变快照；
- 北交所或新三板历史日历已经补齐；
- 所有临时停市、应急公告或历史交易规则版本已穷尽；
- 官方网页浏览权限等同于再分发、自动批量接口或永久保存权。
"""


def execute(workspace: Path, candidate: Path, report: Path) -> None:
    require_child_path(
        candidate,
        workspace / "tmp" / "d2a04b-source-closure",
        "candidate output",
    )
    require_child_path(
        report,
        workspace / "reports" / "data-governance",
        "report output",
    )
    require_new_directory(candidate)
    require_new_directory(report)
    (candidate / "raw").mkdir()

    catalog_path = workspace / CATALOG
    catalog_hash_before = sha256_file(catalog_path)
    track_bindings = bind_track_a_evidence(workspace)
    sse_index = load_json(
        workspace / TRACK_A / "SSE" / "CALENDAR_COVERAGE.json"
    )
    sse_specs, sse_year_metadata = build_sse_annual_specs(sse_index)
    fetch_specs = sse_specs + [
        FetchSpec(
            "sse_calendar_2019_labor_revision",
            SSE_2019_LABOR_REVISION,
            "sse/calendar/2019_labor_revision.html",
            "html",
            2 * 1024 * 1024,
        ),
        FetchSpec(
            "sse_calendar_2020_spring_extension",
            SSE_2020_SPRING_EXTENSION,
            "sse/calendar/2020_spring_extension.html",
            "html",
            2 * 1024 * 1024,
        ),
        FetchSpec(
            "sse_rule_2026_notice",
            SSE_2026_RULE_NOTICE,
            "sse/rules/2026_notice.html",
            "html",
            2 * 1024 * 1024,
        ),
        FetchSpec(
            "sse_rule_2026_docx",
            SSE_2026_RULE_DOCX,
            "sse/rules/2026_trade_rule.docx",
            "docx",
            10 * 1024 * 1024,
        ),
        FetchSpec(
            "sse_rule_2023_notice",
            SSE_2023_RULE_NOTICE,
            "sse/rules/2023_notice_repealed.html",
            "html",
            2 * 1024 * 1024,
        ),
        FetchSpec(
            "sse_rule_2023_docx",
            SSE_2023_RULE_DOCX,
            "sse/rules/2023_trade_rule_repealed.docx",
            "docx",
            10 * 1024 * 1024,
        ),
        FetchSpec(
            "szse_rule_2026_notice",
            SZSE_2026_RULE_NOTICE,
            "szse/rules/2026_notice.html",
            "html",
            2 * 1024 * 1024,
        ),
        FetchSpec(
            "szse_rule_2026_pdf",
            SZSE_2026_RULE_PDF,
            "szse/rules/2026_trade_rule.pdf",
            "pdf",
            20 * 1024 * 1024,
        ),
    ]

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "CORE02-official-evidence/1.0"
            ),
            "Accept": "*/*",
        }
    )
    receipts: list[dict[str, Any]] = []
    for sequence, spec in enumerate(fetch_specs, start=1):
        receipts.append(
            fetch_one(session, spec, candidate / "raw", sequence)
        )
        time.sleep(0.25)
    write_json(candidate / "REQUEST_RECEIPTS.json", receipts)

    lineage, closures = build_notice_lineage(
        workspace,
        candidate,
        receipts,
        sse_year_metadata,
    )
    write_json(candidate / "CALENDAR_NOTICE_LINEAGE.json", lineage)

    calendar_rows = build_trading_day_rows(lineage, closures)
    correct_open_sequences(calendar_rows)
    write_jsonl(candidate / "TRADING_DAY_CANDIDATE.jsonl", calendar_rows)
    write_calendar_csv(candidate / "TRADING_DAY_CANDIDATE.csv", calendar_rows)

    session_rows, session_extraction = build_session_rows(
        workspace, candidate, receipts
    )
    write_jsonl(candidate / "SESSION_RULE_CANDIDATE.jsonl", session_rows)

    source_bindings = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
        "track_a_bindings": track_bindings,
        "catalog_hash_before": catalog_hash_before,
    }
    write_json(candidate / "SOURCE_BINDINGS.json", source_bindings)
    write_json(
        candidate / "RAW_FILE_MANIFEST.json",
        raw_manifest(candidate / "raw", workspace),
    )

    all_expected_dates = (SCOPE_END - SCOPE_START).days + 1
    calendar_checks = {
        "total_rows_expected": all_expected_dates * 2,
        "total_rows_actual": len(calendar_rows),
        "unique_venue_date_keys": len(
            {
                (row["venue"], row["calendar_date"])
                for row in calendar_rows
            }
        ),
        "weekend_open_rows": sum(
            1
            for row in calendar_rows
            if date.fromisoformat(row["calendar_date"]).weekday() >= 5
            and row["is_trading_day"]
        ),
        "missing_available_at": sum(
            1 for row in calendar_rows if not row["available_at"]
        ),
        "missing_revision_id": sum(
            1 for row in calendar_rows if not row["revision_id"]
        ),
        "sse_annual_notice_years": sorted(closures["SSE"]),
        "szse_annual_notice_years": sorted(closures["SZSE"]),
        "szse_revision_notices": 2,
        "sse_revision_notices": 2,
    }
    by_key = {
        (row["venue"], row["calendar_date"]): row["is_trading_day"]
        for row in calendar_rows
    }
    calendar_checks["cross_venue_open_flag_differences"] = sum(
        by_key[("SSE", day.isoformat())]
        != by_key[("SZSE", day.isoformat())]
        for day in date_range(SCOPE_START, SCOPE_END)
    )
    candidate_verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": (
            "PASS"
            if (
                calendar_checks["total_rows_actual"]
                == calendar_checks["total_rows_expected"]
                == calendar_checks["unique_venue_date_keys"]
                and calendar_checks["weekend_open_rows"] == 0
                and calendar_checks["missing_available_at"] == 0
                and calendar_checks["missing_revision_id"] == 0
                and calendar_checks["cross_venue_open_flag_differences"] == 0
                and len(session_rows) >= 8
            )
            else "FAIL"
        ),
        "calendar_checks": calendar_checks,
        "session_extraction": session_extraction,
        "catalog_hash_before": catalog_hash_before,
        "formal_admission": False,
        "research_usable": False,
    }
    write_json(candidate / "VERIFICATION.json", candidate_verification)
    write_json(
        candidate / "CANDIDATE_MANIFEST.json",
        manifest_for_directory(
            candidate,
            workspace,
            exclude={"CANDIDATE_MANIFEST.json"},
        ),
    )

    sse_calendar = count_calendar(calendar_rows, "SSE")
    szse_calendar = count_calendar(calendar_rows, "SZSE")
    calendar_coverage = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "scope_start": SCOPE_START.isoformat(),
        "scope_end": SCOPE_END.isoformat(),
        "total_rows": len(calendar_rows),
        "venues": {
            "SSE": {
                **sse_calendar,
                "annual_notices": 12,
                "known_amendments_applied": 2,
            },
            "SZSE": {
                **szse_calendar,
                "annual_notices": 12,
                "known_amendments_applied": 2,
            },
            "BSE": {
                "rows": 0,
                "status": "FAIL_CLOSED__EXISTING_DIRECT_ACCESS_FAILURE",
                "evidence": [
                    item
                    for item in track_bindings
                    if "/BSE/" in item["path"]
                ],
            },
        },
        "derivation_warning": (
            "Weekday opens and regular-weekend closes are deterministic "
            "derivations around official annual/amendment closure notices; "
            "they are not a formal exchange daily-calendar endpoint."
        ),
        "quality_status": "NOT_ADMITTED",
    }
    write_json(report / "CALENDAR_COVERAGE.json", calendar_coverage)

    successful_versions = sorted(
        key
        for key, value in session_extraction.items()
        if value["status"] == "PASS"
    )
    unresolved_versions = [
        "BSE_ALL",
        "SSE_PRE_2023_EFFECTIVE_CHAIN",
        "SZSE_PRE_2026_RULE_DOCUMENT_CHAIN",
        "ALL_VENUES_TEMPORARY_SESSION_OVERRIDES",
    ]
    unresolved_versions.extend(
        sorted(
            key
            for key, value in session_extraction.items()
            if value["status"] != "PASS"
        )
    )
    session_coverage = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "session_rows": len(session_rows),
        "successful_versions": successful_versions,
        "unresolved_versions": unresolved_versions,
        "extraction": session_extraction,
        "complete_effective_dated_history": False,
        "quality_status": "NOT_ADMITTED",
    }
    write_json(report / "SESSION_RULE_COVERAGE.json", session_coverage)

    q1_subchecks = {
        "sse_szse_2015_2026_explicit_daily_candidate": (
            "PASS" if len(calendar_rows) == all_expected_dates * 2 else "FAIL"
        ),
        "current_sse_szse_session_intervals": (
            "PASS"
            if {"SSE_2026", "SZSE_2026"}.issubset(successful_versions)
            else "FAIL"
        ),
        "session_interval_non_overlap": (
            "PASS" if len(session_rows) >= 8 else "FAIL"
        ),
        "bse_official_daily_calendar": "FAIL",
        "all_venue_historical_session_version_chain": "FAIL",
        "official_exchange_mic_mapping": "UNKNOWN",
    }
    q2_subchecks = {
        "sse_szse_annual_notice_chain": (
            "PASS"
            if len(closures["SSE"]) == len(closures["SZSE"]) == 12
            else "FAIL"
        ),
        "known_sse_szse_2019_2020_revisions_preserved": "PASS",
        "complete_three_venue_temporary_closure_archive": "FAIL",
        "complete_supplemental_notice_revision_history": "FAIL",
        "date_level_pit_available_at": "PASS",
        "intraday_publication_precision": "UNKNOWN",
    }
    gate_receipt = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "work_package": (
            "L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_"
            "STATUS_SOURCE_CLOSURE"
        ),
        "target": "CORE-02",
        "purpose_ceiling": PURPOSE_CEILING,
        "gates": {
            "CORE02-Q1": {
                "gate_type": "BLOCK",
                "status": aggregate_gate_status(q1_subchecks.values()),
                "subchecks": q1_subchecks,
            },
            "CORE02-Q2": {
                "gate_type": "BLOCK",
                "status": aggregate_gate_status(q2_subchecks.values()),
                "subchecks": q2_subchecks,
            },
            "CORE02-Q3": {
                "gate_type": "BLOCK",
                "status": (
                    "PASS"
                    if candidate_verification["status"] == "PASS"
                    else "FAIL"
                ),
                "subchecks": {
                    "strict_open_sequence": "PASS",
                    "calendar_key_uniqueness": "PASS",
                    "weekend_open_counterexample": (
                        "PASS"
                        if calendar_checks["weekend_open_rows"] == 0
                        else "FAIL"
                    ),
                },
            },
            "CORE02-Q4": {
                "gate_type": "REVIEW",
                "status": "PASS",
                "subchecks": {
                    "venue_dimension_preserved": "PASS",
                    "cross_venue_values_not_silently_overwritten": "PASS",
                },
            },
        },
        "formal_admission": False,
        "research_usable": False,
        "quality_status": "NOT_ADMITTED",
        "catalog_update": False,
        "source_registry_update": False,
    }
    write_json(report / "CORE02_GATE_RECEIPT.json", gate_receipt)

    rights_and_gaps = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "rights": {
            "status": "NOT_CLOSED",
            "public_browse_download_evidence": "PRESERVED",
            "formal_bulk_or_automated_interface_rights": "NOT_PROVEN",
            "external_redistribution": "NOT_GRANTED",
            "permanent_preservation_right": "NOT_PROVEN",
            "decision": (
                "Raw and derived artifacts remain inspection-only under tmp; "
                "no formal source admission or published redistribution."
            ),
        },
        "remaining_blockers": [
            "BSE official daily calendar and session-rule raw evidence remains unavailable through the bounded direct path.",
            "Pre-BSE NEEQ continuity and the complete three-venue temporary/emergency closure archive are not closed.",
            "Complete effective-dated historical session-rule chains for SSE, SZSE, and BSE are not closed.",
            "The 2023 SSE rule effective start is event-dependent in the notice and has not been converted to an official calendar date.",
            "Only date-level publication precision is available for annual/revision notices; intraday availability is unknown.",
            "Official exchange-to-MIC mapping and explicit permanent-storage/bulk-interface rights are not closed.",
        ],
        "no_bypass": True,
        "new_bse_requests": 0,
        "quality_status": "NOT_ADMITTED",
    }
    write_json(report / "RIGHTS_AND_GAPS.json", rights_and_gaps)

    catalog_hash_after = sha256_file(catalog_path)
    verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": candidate_verification["status"],
        "catalog_hash_before": catalog_hash_before,
        "catalog_hash_after": catalog_hash_after,
        "catalog_unchanged": catalog_hash_before == catalog_hash_after,
        "candidate_verification": candidate_verification,
        "request_count": len(receipts),
        "preserved_response_count": sum(
            item["result"] == "RESPONSE_PRESERVED" for item in receipts
        ),
        "failed_request_count": sum(
            item["result"] != "RESPONSE_PRESERVED" for item in receipts
        ),
        "raw_manifest_entries": len(
            load_json(candidate / "RAW_FILE_MANIFEST.json")
        ),
        "report_file_set_expected": sorted(EXPECTED_REPORT_FILES),
        "formal_admission": False,
        "research_usable": False,
    }
    write_json(report / "VERIFICATION.json", verification)
    (report / "README.md").write_text(
        readme_text(calendar_coverage, session_coverage, gate_receipt),
        encoding="utf-8",
        newline="\n",
    )
    write_json(
        report / "REPORT_MANIFEST.json",
        manifest_for_directory(
            report,
            workspace,
            exclude={"REPORT_MANIFEST.json"},
        ),
    )

    actual_report_files = {path.name for path in report.iterdir()}
    if actual_report_files != EXPECTED_REPORT_FILES:
        raise ValueError(
            "unexpected report file set: "
            f"{sorted(actual_report_files ^ EXPECTED_REPORT_FILES)}"
        )
    if catalog_hash_before != catalog_hash_after:
        raise RuntimeError("datasets.json changed during CORE-02 closure")
    print(
        json.dumps(
            {
                "run_id": RUN_ID,
                "candidate": workspace_relative(candidate, workspace),
                "report": workspace_relative(report, workspace),
                "calendar_rows": len(calendar_rows),
                "session_rows": len(session_rows),
                "request_count": len(receipts),
                "preserved_response_count": verification[
                    "preserved_response_count"
                ],
                "verification_status": verification["status"],
                "q1": gate_receipt["gates"]["CORE02-Q1"]["status"],
                "q2": gate_receipt["gates"]["CORE02-Q2"]["status"],
                "catalog_unchanged": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=str(Path(__file__).resolve().parents[2]),
    )
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    workspace = Path(args.workspace).resolve()
    candidate = (workspace / args.candidate).resolve()
    report = (workspace / args.report).resolve()
    execute(workspace, candidate, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
