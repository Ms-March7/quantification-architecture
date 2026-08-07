#!/usr/bin/env python3
"""Build the bounded CORE-01 official identity closure evidence package v2.

The work package is intentionally admission-neutral:

* only unauthenticated first-party SSE/SZSE/BSE HTTPS endpoints are requested;
* the already blocked BSE listed-company endpoint is not retried;
* every response body or request failure is receipted;
* all new facts remain INSPECTION_ONLY / NOT_ADMITTED;
* the formal catalog, five authority files, quality state, snapshots and
  published data are guarded and never written.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse

import requests
from bs4 import BeautifulSoup
from requests.auth import AuthBase


RUN_ID = "stage-a-core01-official-identity-closure-20260730-v2"
QA_REPAIR_ID = "qa-repair-after-independent-review-20260730-v1"
PURPOSE_CEILING = "INSPECTION_ONLY"
QUALITY_STATUS = "NOT_ADMITTED"
DEFAULT_CANDIDATE = (
    "tmp/d2a04b-source-closure/"
    "stage-a-core01-official-identity-closure-20260730-v2"
)
DEFAULT_REPORT = (
    "reports/data-governance/"
    "stage-a-core01-official-identity-closure-20260730-v2"
)
V1_REPORT = (
    "reports/data-governance/"
    "stage-a-core01-official-identity-closure-20260730-v1"
)
TRACK_A = (
    "tmp/d2a04b-source-closure/"
    "d2a04b-official-track-a-20260728-v1"
)
CORE_CONTRACT = (
    "data/catalog/contracts/g_data_core_v1/"
    "core_01_08_market_reference.json"
)

SHANGHAI = timezone(timedelta(hours=8))
GLOBAL_NETWORK_REQUEST_CAP = 40
PER_RESPONSE_MAX_BYTES = 2 * 1024 * 1024

GUARDED_FILES = (
    "data/catalog/datasets.json",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    "control/ROADMAP.md",
    "control/projects.yaml",
    "control/WORKSPACE_INDEX.md",
    "data/catalog/DATABASE_MANUAL.md",
    "data/catalog/DATA_QUALITY_POLICY.md",
)
GUARDED_TREES = (
    "data/market/snapshots",
    "data/published",
)
ALLOWED_HOSTS = {
    "www.sse.com.cn",
    "sse.com.cn",
    "query.sse.com.cn",
    "static.sse.com.cn",
    "www.szse.cn",
    "szse.cn",
    "docs.static.szse.cn",
    "disc.static.szse.cn",
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


class NoEndpointAuthorization(AuthBase):
    """Prevent requests/netrc from adding endpoint Authorization headers."""

    def __call__(
        self, request: requests.PreparedRequest
    ) -> requests.PreparedRequest:
        request.headers.pop("Authorization", None)
        return request

FIXED_GET_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "source_id": "SSE-RELIST-CHANGHANG-2018",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/disclosure/announcement/listing/"
            "c/c_20181227_4700819.shtml"
        ),
        "path": "raw/sse/relisting_changhang_2018.html",
        "kind": "html",
        "markers": ("601975", "2019年1月8日", "重新上市"),
    },
    {
        "source_id": "SSE-RELIST-SINOMACH-2020",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/disclosure/announcement/listing/"
            "c/c_20200529_5113769.shtml"
        ),
        "path": "raw/sse/relisting_sinomach_2020.html",
        "kind": "html",
        "markers": ("601399", "2020年6月8日", "重新上市"),
    },
    {
        "source_id": "SSE-DELIST-GUANDIAN-2026",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/disclosure/announcement/listing/"
            "stock/c/c_20260608_10821090.shtml"
        ),
        "path": "raw/sse/delisting_guandian_2026.html",
        "kind": "html",
        "markers": ("688287", "2026年6月10日", "摘牌"),
    },
    {
        "source_id": "BSE-CODE-MAPPING-CURRENT-PAGE",
        "venue": "BSE",
        "url": "https://www.bse.cn/service/code_mapping.html",
        "path": "raw/bse/code_mapping.html",
        "kind": "html",
        "markers": (
            "旧代码",
            "新代码",
            "上市日期",
            "原精选层",
            "挂牌日期",
        ),
    },
    {
        "source_id": "BSE-CODE-SWITCH-EFFECTIVE-NOTICE",
        "venue": "BSE",
        "url": "https://www.bse.cn/important_news/200026735.html",
        "path": "raw/bse/code_switch_effective_notice.html",
        "kind": "html",
        "markers": ("2025年10月9日", "新证券代码", "新旧代码对照表"),
    },
)

SZSE_SEARCHES: tuple[dict[str, Any], ...] = (
    {
        "family": "LISTING",
        "keyword": "股票上市交易",
        "channel": "ipocompany_news",
        "max_pages": 15,
    },
    {
        "family": "DELISTING",
        "keyword": "股票终止上市",
        "channel": "ipocompany_news",
        "max_pages": 5,
    },
    {
        "family": "RELISTING",
        "keyword": "重新上市",
        "channel": "ipocompany_news",
        "max_pages": 3,
    },
    {
        "family": "RESUME_LISTING",
        "keyword": "恢复上市",
        "channel": "ipocompany_news",
        "max_pages": 5,
    },
    {
        "family": "CODE_CHANGE",
        "keyword": "证券代码变更",
        "channel": "ipocompany_news",
        "max_pages": 5,
    },
    {
        "family": "SUSPEND_LISTING",
        "keyword": "暂停上市",
        "channel": "ipocompany_news",
        "max_pages": 5,
    },
)

SSE_RELIST_EPISODES: tuple[dict[str, str], ...] = (
    {
        "source_id": "SSE-RELIST-CHANGHANG-2018",
        "old_code": "600087",
        "new_code": "601975",
        "expected_delist_date": "2014-06-05",
        "expected_relist_date": "2019-01-08",
        "expected_page_date": "2018-12-28",
    },
    {
        "source_id": "SSE-RELIST-SINOMACH-2020",
        "old_code": "601268",
        "new_code": "601399",
        "expected_delist_date": "2015-05-21",
        "expected_relist_date": "2020-06-08",
        "expected_page_date": "2020-05-30",
    },
)

SSE_DELIST_EPISODES: tuple[dict[str, str], ...] = (
    {
        "source_id": "SSE-DELIST-GUANDIAN-2026",
        "local_code": "688287",
        "expected_listing_date": "2022-05-25",
        "expected_delisting_period_end_date": "2026-06-08",
        "expected_delist_date": "2026-06-10",
        "expected_page_date": "2026-06-08",
    },
)


def now_shanghai() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            )


def atomic_write_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(path.name + ".qa-repair.tmp")
    if temporary.exists():
        raise FileExistsError(temporary)
    temporary.write_bytes(value)
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        ),
    )


def atomic_write_jsonl(
    path: Path, rows: Iterable[dict[str, Any]]
) -> None:
    payload = b"".join(
        (
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        for row in rows
    )
    atomic_write_bytes(path, payload)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_binding(
    actual_path: Path,
    *,
    logical_path: str | None = None,
    workspace: Path | None = None,
) -> dict[str, Any]:
    if not actual_path.is_file():
        raise FileNotFoundError(actual_path)
    if logical_path is None:
        if workspace is None:
            raise ValueError("workspace required when logical_path is omitted")
        logical_path = actual_path.resolve().relative_to(
            workspace.resolve()
        ).as_posix()
    return {
        "path": logical_path.replace("\\", "/"),
        "bytes": actual_path.stat().st_size,
        "sha256": sha256_file(actual_path),
    }


def safe_headers(headers: requests.structures.CaseInsensitiveDict) -> dict[str, str]:
    return {
        key.lower(): str(value)
        for key, value in headers.items()
        if key.lower() in SAFE_RESPONSE_HEADERS
    }


def is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & flag)


def tree_inventory(workspace: Path, relative_root: str) -> dict[str, Any]:
    root = workspace / relative_root
    if not root.exists():
        return {
            "path": relative_root,
            "exists": False,
            "file_count": 0,
            "directory_count": 0,
            "total_bytes": 0,
            "entries_sha256": sha256_bytes(b"[]"),
        }
    if is_reparse_point(root) or not root.is_dir():
        raise RuntimeError(f"guarded tree is not a normal directory: {root}")
    entries: list[dict[str, Any]] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if is_reparse_point(path):
            raise RuntimeError(f"guarded tree contains reparse point: {path}")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append({"path": relative, "type": "directory"})
        elif path.is_file():
            size = path.stat().st_size
            total += size
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "bytes": size,
                    "sha256": sha256_file(path),
                }
            )
        else:
            raise RuntimeError(f"guarded tree contains non-file entry: {path}")
        if len(entries) > 20_000 or total > 128 * 1024 * 1024:
            raise RuntimeError(f"guarded tree inventory limit exceeded: {root}")
    encoded = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "path": relative_root,
        "exists": True,
        "file_count": sum(row["type"] == "file" for row in entries),
        "directory_count": sum(row["type"] == "directory" for row in entries),
        "total_bytes": total,
        "entries_sha256": sha256_bytes(encoded),
    }


def guard_state(workspace: Path) -> dict[str, Any]:
    return {
        "files": {
            relative: file_binding(workspace / relative, workspace=workspace)
            for relative in GUARDED_FILES
        },
        "trees": {
            relative: tree_inventory(workspace, relative)
            for relative in GUARDED_TREES
        },
    }


def require_new_output(path: Path, allowed_parent: Path) -> None:
    resolved = path.resolve()
    resolved.relative_to(allowed_parent.resolve())
    if resolved == allowed_parent.resolve():
        raise ValueError(f"output must be a child of {allowed_parent}")
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")


def content_valid(kind: str, body: bytes, content_type: str) -> bool:
    if kind == "json":
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(value, dict) and isinstance(value.get("data"), list)
    if kind == "html":
        prefix = body[:8192].lower()
        return (
            b"<html" in prefix
            or b"<!doctype html" in prefix
            or "text/html" in content_type.lower()
        )
    raise ValueError(kind)


def official_host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS


def fetch_one(
    session: requests.Session,
    *,
    candidate: Path,
    sequence: int,
    source_id: str,
    venue: str,
    method: str,
    url: str,
    relative_path: str,
    kind: str,
    markers: tuple[str, ...] = (),
    body_text: str | None = None,
    referer: str | None = None,
    max_bytes: int = PER_RESPONSE_MAX_BYTES,
) -> tuple[dict[str, Any], bytes | None]:
    receipt: dict[str, Any] = {
        "sequence": sequence,
        "source_id": source_id,
        "venue": venue,
        "method": method,
        "request_url": url,
        "request_body": body_text,
        "request_body_sha256": (
            sha256_bytes(body_text.encode("utf-8")) if body_text else None
        ),
        "referer": referer,
        "expected_kind": kind,
        "max_bytes": max_bytes,
        "started_at_asia_shanghai": now_shanghai(),
        "attempts": 1,
        "retry_count": 0,
        "serial": True,
        "allow_redirects": False,
        "endpoint_authentication_configured": False,
        "endpoint_authorization_header_policy": "REJECT_IF_PRESENT",
        "transport_proxy_authentication_fully_observable": False,
        "cookies_sent": False,
        "formal_admission": False,
        "purpose_ceiling": PURPOSE_CEILING,
    }
    if not official_host_allowed(url):
        receipt.update(
            result="REJECTED_BEFORE_REQUEST",
            reason="URL is outside the official HTTPS allow-list",
            completed_at_asia_shanghai=now_shanghai(),
        )
        return receipt, None

    session.cookies.clear()
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    if body_text is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        response = session.request(
            method,
            url,
            data=body_text.encode("utf-8") if body_text is not None else None,
            headers=headers,
            timeout=(10, 45),
            allow_redirects=False,
            stream=True,
        )
        chunks: list[bytes] = []
        total = 0
        exceeded = False
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                exceeded = True
                break
            chunks.append(chunk)
        body = b"".join(chunks)
        success_path = candidate / relative_path
        target = success_path
        if response.status_code != 200:
            target = (
                candidate
                / "raw"
                / "failed"
                / f"{sequence:03d}_{source_id}.bin"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        receipt.update(
            http_status=response.status_code,
            response_headers=safe_headers(response.headers),
            response_url=str(response.url),
            path=target.relative_to(candidate).as_posix(),
            bytes=len(body),
            sha256=sha256_bytes(body),
            response_cookie_headers_received=bool(
                response.headers.get("set-cookie")
            ),
        )
        requested_cookie = "Cookie" in response.request.headers
        endpoint_authorization_sent = (
            "Authorization" in response.request.headers
        )
        proxy_authorization_visible = (
            "Proxy-Authorization" in response.request.headers
        )
        receipt["cookies_sent"] = requested_cookie
        receipt["endpoint_authorization_sent"] = endpoint_authorization_sent
        receipt[
            "proxy_authorization_header_visible_to_requests"
        ] = proxy_authorization_visible
        marker_missing: list[str] = []
        if response.status_code == 200 and markers:
            text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
            marker_missing = [marker for marker in markers if marker not in text]
        receipt["required_markers_missing"] = marker_missing
        if exceeded:
            receipt.update(
                result="REJECTED_RESPONSE_PREFIX_PRESERVED",
                reason="response exceeded max_bytes; bounded prefix retained",
            )
        elif response.status_code != 200:
            receipt.update(
                result="HTTP_FAILURE_BODY_PRESERVED",
                reason=f"official endpoint returned HTTP {response.status_code}",
            )
        elif not content_valid(
            kind, body, response.headers.get("content-type", "")
        ):
            receipt.update(
                result="CONTENT_MISMATCH_BODY_PRESERVED",
                reason=f"HTTP 200 body failed {kind} validation",
            )
        elif marker_missing:
            receipt.update(
                result="CONTENT_MARKER_MISMATCH_BODY_PRESERVED",
                reason="one or more required official-text markers are absent",
            )
        elif requested_cookie:
            receipt.update(
                result="REJECTED_UNEXPECTED_COOKIE_SEND",
                reason="request unexpectedly carried a Cookie header",
            )
        elif endpoint_authorization_sent:
            receipt.update(
                result="REJECTED_UNEXPECTED_ENDPOINT_AUTHORIZATION",
                reason=(
                    "request unexpectedly carried an endpoint Authorization "
                    "header"
                ),
            )
        else:
            receipt["result"] = "RESPONSE_PRESERVED"
        receipt["completed_at_asia_shanghai"] = now_shanghai()
        session.cookies.clear()
        return receipt, body
    except requests.RequestException as exc:
        receipt.update(
            result="NETWORK_FAILURE",
            reason=f"{type(exc).__name__}: {exc}",
            completed_at_asia_shanghai=now_shanghai(),
        )
        session.cookies.clear()
        return receipt, None


def szse_search_body(
    keyword: str,
    channel: str,
    current_page: int,
    page_size: int = 100,
) -> str:
    start = int(
        datetime(1990, 1, 1, tzinfo=SHANGHAI).timestamp() * 1000
    )
    end = int(
        datetime(2026, 7, 30, 23, 59, 59, tzinfo=SHANGHAI).timestamp()
        * 1000
    )
    return urlencode(
        [
            ("keyword", keyword),
            ("time", str(start)),
            ("endTime", str(end)),
            ("range", "title"),
            ("channelCode[]", channel),
            ("currentPage", str(current_page)),
            ("pageSize", str(page_size)),
            ("scope", "1"),
        ]
    )


def clean_html_fragment(value: Any) -> str:
    return BeautifulSoup(str(value or ""), "html.parser").get_text(
        " ", strip=True
    )


def epoch_ms_to_shanghai(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=SHANGHAI).isoformat(
        timespec="seconds"
    )


def normalize_official_metadata_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.hostname in {"www.szse.cn", "szse.cn"}:
        return parsed._replace(scheme="https").geturl()
    if text.startswith("/"):
        return "https://www.szse.cn" + text
    return text


def date_from_official_metadata_url(value: str | None) -> str | None:
    match = re.search(r"/t(\d{8})_", value or "")
    return normalize_date(match.group(1)) if match else None


def normalize_szse_event(
    item: dict[str, Any],
    *,
    family: str,
    source_path: str,
    source_sha256: str,
) -> dict[str, Any]:
    title = clean_html_fragment(item.get("doctitle"))
    content = clean_html_fragment(item.get("doccontent"))
    document_url = normalize_official_metadata_url(item.get("docpuburl"))
    document_json_url = normalize_official_metadata_url(
        item.get("docpubjsonurl")
    )
    published_at_candidate = epoch_ms_to_shanghai(item.get("docpubtime"))
    metadata_url_date_candidate = date_from_official_metadata_url(
        document_json_url or document_url
    )
    docpubtime_date_candidate = (
        published_at_candidate[:10] if published_at_candidate else None
    )
    code_candidates = sorted(
        set(re.findall(r"(?<!\d)(?:00|30)\d{4}(?!\d)", title + " " + content))
    )
    return {
        "source_id": "SZSE_OFFICIAL",
        "search_family": family,
        "document_id": str(item.get("id") or ""),
        "title": title,
        "content_excerpt": content[:600],
        "document_url_from_official_metadata": document_url,
        "document_json_url_from_official_metadata": document_json_url,
        "official_field": "docpubtime",
        "docpubtime_raw_epoch_ms": item.get("docpubtime"),
        "published_at_candidate": published_at_candidate,
        "metadata_url_date_candidate": metadata_url_date_candidate,
        "docpubtime_date_candidate": docpubtime_date_candidate,
        "metadata_url_date_matches_docpubtime_date": (
            metadata_url_date_candidate == docpubtime_date_candidate
            if metadata_url_date_candidate and docpubtime_date_candidate
            else None
        ),
        "a_share_scope_assessment": (
            "TITLE_MENTIONS_STOCK__NOT_FULL_A_SHARE_PROOF"
            if "股票" in title
            else "NOT_ESTABLISHED_FROM_TITLE"
        ),
        "first_publication_semantics_proven": False,
        "available_at_promoted": None,
        "revision_id": None,
        "supersedes_revision_id": None,
        "local_code_candidates_not_identity": code_candidates,
        "source_response": {
            "path": source_path,
            "sha256": source_sha256,
        },
        "boundary": (
            "docpubtime is preserved exactly as the official search response "
            "field. It is not promoted to first-publication time or available_at "
            "without archive and revision semantics."
        ),
    }


def enrich_preserved_szse_event(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    document_url = enriched.get("document_url_from_official_metadata")
    document_json_url = enriched.get(
        "document_json_url_from_official_metadata"
    )
    published_at_candidate = enriched.get("published_at_candidate")
    metadata_url_date_candidate = date_from_official_metadata_url(
        document_json_url or document_url
    )
    docpubtime_date_candidate = (
        str(published_at_candidate)[:10]
        if published_at_candidate
        else None
    )
    enriched.update(
        {
            "metadata_url_date_candidate": metadata_url_date_candidate,
            "docpubtime_date_candidate": docpubtime_date_candidate,
            "metadata_url_date_matches_docpubtime_date": (
                metadata_url_date_candidate == docpubtime_date_candidate
                if metadata_url_date_candidate
                and docpubtime_date_candidate
                else None
            ),
            "a_share_scope_assessment": (
                "TITLE_MENTIONS_STOCK__NOT_FULL_A_SHARE_PROOF"
                if "股票" in str(enriched.get("title") or "")
                else "NOT_ESTABLISHED_FROM_TITLE"
            ),
        }
    )
    return enriched


def collect_szse_searches(
    session: requests.Session,
    candidate: Path,
    receipts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    coverage_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    hard_stop = False
    for spec in SZSE_SEARCHES:
        family = spec["family"]
        family_events: list[dict[str, Any]] = []
        page_totals: list[int] = []
        page_sizes: list[int] = []
        pages_preserved = 0
        stop_reason: str | None = None
        planned_pages: int | None = None
        for page in range(1, int(spec["max_pages"]) + 1):
            if hard_stop:
                stop_reason = "SZSE_SOURCE_HARD_STOP_FROM_PRIOR_REQUEST"
                break
            if len(receipts) >= GLOBAL_NETWORK_REQUEST_CAP:
                stop_reason = "GLOBAL_NETWORK_REQUEST_CAP_REACHED"
                break
            body = szse_search_body(
                spec["keyword"], spec["channel"], page, 100
            )
            receipt, response_body = fetch_one(
                session,
                candidate=candidate,
                sequence=len(receipts) + 1,
                source_id=f"SZSE-{family}-SEARCH-P{page:02d}",
                venue="SZSE",
                method="POST",
                url="https://www.szse.cn/api/search/content",
                relative_path=f"raw/szse/{family.lower()}/page-{page:02d}.json",
                kind="json",
                body_text=body,
                referer="https://www.szse.cn/disclosure/notice/company/",
            )
            receipts.append(receipt)
            if receipt["result"] != "RESPONSE_PRESERVED" or response_body is None:
                stop_reason = receipt["result"]
                if receipt.get("http_status") in {401, 403, 429, 451}:
                    hard_stop = True
                break
            payload = json.loads(response_body.decode("utf-8"))
            total = int(payload.get("totalSize") or 0)
            effective_page_size = int(payload.get("pageSize") or 0)
            data = payload.get("data")
            if not isinstance(data, list) or effective_page_size <= 0:
                stop_reason = "INVALID_PAGINATION_SCHEMA"
                break
            page_totals.append(total)
            page_sizes.append(effective_page_size)
            pages_preserved += 1
            planned_pages = math.ceil(total / effective_page_size) if total else 0
            logical_path = (
                f"{DEFAULT_CANDIDATE}/"
                f"raw/szse/{family.lower()}/page-{page:02d}.json"
            )
            for item in data:
                if isinstance(item, dict):
                    family_events.append(
                        normalize_szse_event(
                            item,
                            family=family,
                            source_path=logical_path,
                            source_sha256=receipt["sha256"],
                        )
                    )
            if not data or page >= planned_pages:
                break
            time.sleep(0.35)
        unique_by_id: dict[str, dict[str, Any]] = {}
        duplicate_ids: list[str] = []
        for row in family_events:
            key = row["document_id"] or sha256_bytes(
                canonical_json_bytes(row)
            )
            if key in unique_by_id:
                duplicate_ids.append(key)
            else:
                unique_by_id[key] = row
        unique_events = list(unique_by_id.values())
        event_rows.extend(unique_events)
        total_reported = page_totals[-1] if page_totals else None
        stable_total = len(set(page_totals)) <= 1
        complete_keyword_pagination = bool(
            planned_pages is not None
            and pages_preserved >= planned_pages
            and stable_total
            and len(unique_events) == total_reported
        )
        if (
            stop_reason is None
            and planned_pages is not None
            and pages_preserved < planned_pages
            and pages_preserved >= int(spec["max_pages"])
        ):
            stop_reason = "MAX_PAGES_LIMIT_REACHED"
        coverage_rows.append(
            {
                "source_id": "SZSE_OFFICIAL",
                "family": family,
                "keyword": spec["keyword"],
                "channel": spec["channel"],
                "requested_start": "1990-01-01T00:00:00+08:00",
                "requested_end": "2026-07-30T23:59:59+08:00",
                "requested_page_size": 100,
                "effective_page_sizes": sorted(set(page_sizes)),
                "page_total_observations": page_totals,
                "pages_preserved": pages_preserved,
                "max_pages": spec["max_pages"],
                "planned_pages_from_last_response": planned_pages,
                "rows_preserved": len(family_events),
                "unique_document_ids": len(unique_events),
                "duplicate_document_ids": sorted(set(duplicate_ids)),
                "total_reported": total_reported,
                "total_stable_across_pages": stable_total,
                "complete_keyword_pagination": complete_keyword_pagination,
                "complete_official_event_archive": False,
                "stop_reason": stop_reason,
                "boundary": (
                    "Complete keyword pagination, when true, applies only to the "
                    "specified keyword/channel/time query. It does not prove the "
                    "exchange's complete listing-status archive or revision chain."
                ),
            }
        )
    return coverage_rows, event_rows


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def unique_row_with_date(
    rows: Iterable[dict[str, str]],
    field: str,
    expected_date: str,
) -> dict[str, str] | None:
    matches = [
        row
        for row in rows
        if normalize_date(row.get(field)) == expected_date
    ]
    return matches[0] if len(matches) == 1 else None


def normalize_date(value: Any) -> str | None:
    text = str(value or "").strip()
    compact = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    separated = re.fullmatch(
        r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})",
        text,
    )
    match = compact or separated
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def iso_date_to_chinese(value: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if not match:
        raise ValueError(f"invalid ISO date: {value!r}")
    year, month, day = (int(part) for part in match.groups())
    return f"{year}年{month}月{day}日"


def successful_receipt(
    receipts: list[dict[str, Any]], source_id: str
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in receipts
            if row["source_id"] == source_id
            and row["result"] == "RESPONSE_PRESERVED"
        ),
        None,
    )


def parse_bse_code_mapping(
    candidate: Path,
    receipts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page_receipt = successful_receipt(
        receipts, "BSE-CODE-MAPPING-CURRENT-PAGE"
    )
    notice_receipt = successful_receipt(
        receipts, "BSE-CODE-SWITCH-EFFECTIVE-NOTICE"
    )
    if not page_receipt or not notice_receipt:
        return [], {
            "mapping_page_available": bool(page_receipt),
            "effective_notice_available": bool(notice_receipt),
            "rows": 0,
            "status": "UNRESOLVED__OFFICIAL_RESPONSE_NOT_ACCEPTED",
        }
    page_path = candidate / page_receipt["path"]
    notice_path = candidate / notice_receipt["path"]
    soup = BeautifulSoup(page_path.read_bytes(), "html.parser")
    page_text = soup.get_text(" ", strip=True)
    selected_layer_footnote_observed = (
        "原精选层" in page_text and "挂牌日期" in page_text
    )
    mappings: list[dict[str, Any]] = []
    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        old_code = re.sub(r"\D", "", cells[-2])
        new_code = re.sub(r"\D", "", cells[-1])
        if not (
            re.fullmatch(r"\d{6}", old_code)
            and re.fullmatch(r"\d{6}", new_code)
        ):
            continue
        listing_date = normalize_date(cells[-3])
        mappings.append(
            {
                "source_id": "BSE_OFFICIAL",
                "security_name": cells[-4],
                "listing_date_as_displayed": cells[-3],
                "table_listing_date_candidate": listing_date,
                "table_listing_date_semantics": (
                    "The official footnote says that, for companies "
                    "transferred from the former NEEQ Selected Layer, this "
                    "field is that former layer's listing date. It is not a "
                    "uniform BJSE listing-date field."
                ),
                "bjse_listing_date_proven": False,
                "old_local_code": old_code,
                "new_local_code": new_code,
            }
        )
    notice_soup = BeautifulSoup(notice_path.read_bytes(), "html.parser")
    notice_text = notice_soup.get_text(" ", strip=True)
    effective = (
        "2025-10-09" if "2025年10月9日" in notice_text else None
    )
    page_date_node = notice_soup.select_one(".date_span")
    page_date_candidate = normalize_date(
        page_date_node.get_text(" ", strip=True)
        if page_date_node is not None
        else None
    )
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in mappings:
        unique[(row["old_local_code"], row["new_local_code"])] = row
    mappings = list(unique.values())
    intervals: list[dict[str, Any]] = []
    if effective and selected_layer_footnote_observed:
        for row in mappings:
            intervals.append(
                {
                    **row,
                    "exchange_mic_candidate": "BJSE",
                    "old_code_valid_to_candidate": "2025-10-08",
                    "new_code_valid_from_candidate": effective,
                    "official_page_date_candidate": page_date_candidate,
                    "first_publication_semantics_proven": False,
                    "published_at": None,
                    "available_at": None,
                    "revision_id": None,
                    "permanent_security_id": None,
                    "interval_semantics_closed": False,
                    "source_bindings": [
                        {
                            "path": (
                                f"{DEFAULT_CANDIDATE}/"
                                f"{page_receipt['path']}"
                            ),
                            "sha256": page_receipt["sha256"],
                        },
                        {
                            "path": (
                                f"{DEFAULT_CANDIDATE}/"
                                f"{notice_receipt['path']}"
                            ),
                            "sha256": notice_receipt["sha256"],
                        },
                    ],
                    "boundary": (
                        "The official notice proves new-code use from "
                        "2025-10-09 and the official table binds old/new codes. "
                        "The candidate prior-day end is a mechanical interval "
                        "projection, not a permanent-ID or publication/PIT closure."
                    ),
                }
            )
    old_counts = Counter(row["old_local_code"] for row in mappings)
    new_counts = Counter(row["new_local_code"] for row in mappings)
    return intervals, {
        "mapping_page_available": True,
        "effective_notice_available": True,
        "rows": len(mappings),
        "interval_candidates": len(intervals),
        "old_code_duplicate_count": sum(value > 1 for value in old_counts.values()),
        "new_code_duplicate_count": sum(value > 1 for value in new_counts.values()),
        "new_codes_start_92_count": sum(
            row["new_local_code"].startswith("92") for row in mappings
        ),
        "table_listing_dates_parsed_count": sum(
            row["table_listing_date_candidate"] is not None
            for row in mappings
        ),
        "table_listing_dates_unparsed_count": sum(
            row["table_listing_date_candidate"] is None
            for row in mappings
        ),
        "selected_layer_transfer_footnote_observed": (
            selected_layer_footnote_observed
        ),
        "bjse_listing_date_semantics_complete": False,
        "effective_date": effective,
        "official_page_date_candidate": page_date_candidate,
        "first_publication_semantics_proven": False,
        "status": (
            "PARTIAL__OFFICIAL_CODE_SWITCH_INTERVAL_CANDIDATES"
            if intervals
            else (
                "UNRESOLVED__NO_EFFECTIVE_DATE"
                if not effective
                else "UNRESOLVED__TABLE_DATE_FOOTNOTE_NOT_BOUND"
            )
        ),
        "complete_current_master": False,
        "complete_delisted_history": False,
        "complete_relisted_history": False,
    }


def build_sse_relist_candidates(
    workspace: Path,
    candidate: Path,
    receipts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sse_root = workspace / TRACK_A / "SSE"
    current = read_csv_rows(
        sse_root / "derived/005_listed_main_a__股票.csv"
    )
    delisted = read_csv_rows(
        sse_root / "derived/008_delisted_main__终止上市公司.csv"
    )
    current_by_code: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in current:
        current_by_code[row["A股代码"]].append(row)
    delisted_by_code = defaultdict(list)
    for row in delisted:
        delisted_by_code[row["原公司代码"]].append(row)
    candidates: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for spec in SSE_RELIST_EPISODES:
        receipt = successful_receipt(receipts, spec["source_id"])
        old_rows = delisted_by_code.get(spec["old_code"], [])
        old_row = unique_row_with_date(
            old_rows,
            "终止上市日期",
            spec["expected_delist_date"],
        )
        current_row = unique_row_with_date(
            current_by_code.get(spec["new_code"], []),
            "上市日期",
            spec["expected_relist_date"],
        )
        evidence_ok = bool(receipt and old_row and current_row)
        if evidence_ok:
            body = (candidate / receipt["path"]).read_bytes()
            soup = BeautifulSoup(body, "html.parser")
            text = soup.get_text(" ", strip=True)
            page_date_node = soup.select_one(".article_opt i")
            page_date_candidate = normalize_date(
                page_date_node.get_text(" ", strip=True)
                if page_date_node is not None
                else None
            )
            evidence_ok = (
                spec["new_code"] in text
                and iso_date_to_chinese(spec["expected_relist_date"]) in text
                and iso_date_to_chinese(spec["expected_delist_date"]) in text
                and page_date_candidate == spec["expected_page_date"]
            )
        if not evidence_ok:
            gaps.append(
                {
                    "source_id": spec["source_id"],
                    "status": "UNRESOLVED__RELIST_EPISODE_MARKERS_INCOMPLETE",
                    "old_code": spec["old_code"],
                    "new_code": spec["new_code"],
                }
            )
            continue
        assert old_row is not None and current_row is not None
        candidates.append(
            {
                "source_id": "SSE_OFFICIAL",
                "candidate_type": "RELISTED_LISTING_EPISODE",
                "old_local_code": spec["old_code"],
                "old_short_name": old_row.get("原公司简称"),
                "initial_listing_date": normalize_date(
                    old_row.get("上市日期")
                ),
                "termination_date": normalize_date(
                    old_row.get("终止上市日期")
                ),
                "new_local_code": spec["new_code"],
                "current_short_name": current_row.get("证券简称"),
                "relisting_date": normalize_date(
                    current_row.get("上市日期")
                ),
                "official_page_date_candidate": page_date_candidate,
                "first_publication_semantics_proven": False,
                "published_at": None,
                "available_at": None,
                "permanent_security_id": None,
                "listing_episode_no": None,
                "revision_id": None,
                "interval_semantics_closed": False,
                "source_bindings": [
                    {
                        "path": f"{DEFAULT_CANDIDATE}/{receipt['path']}",
                        "sha256": receipt["sha256"],
                    },
                    file_binding(
                        sse_root / "raw/008_delisted_main.xls",
                        workspace=workspace,
                    ),
                    file_binding(
                        sse_root / "raw/005_listed_main_a.xls",
                        workspace=workspace,
                    ),
                ],
                "boundary": (
                    "The official relisting announcement plus official current "
                    "and terminated tables prove a bounded episode candidate. "
                    "No stable issuer/security ID, complete code/name history, "
                    "revision chain or exact available_at is inferred."
                ),
            }
        )
    return candidates, gaps


def build_sse_delist_candidates(
    workspace: Path,
    candidate: Path,
    receipts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sse_root = workspace / TRACK_A / "SSE"
    delisted = read_csv_rows(
        sse_root / "derived/009_delisted_star__终止上市公司.csv"
    )
    rows_by_code: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in delisted:
        rows_by_code[row["原公司代码"]].append(row)
    candidates: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for spec in SSE_DELIST_EPISODES:
        receipt = successful_receipt(receipts, spec["source_id"])
        row = unique_row_with_date(
            rows_by_code.get(spec["local_code"], []),
            "终止上市日期",
            spec["expected_delist_date"],
        )
        evidence_ok = bool(
            receipt
            and row
            and normalize_date(row.get("上市日期"))
            == spec["expected_listing_date"]
        )
        if evidence_ok:
            body = (candidate / receipt["path"]).read_bytes()
            soup = BeautifulSoup(body, "html.parser")
            text = soup.get_text(" ", strip=True)
            page_date_node = soup.select_one(".article_opt i")
            page_date_candidate = normalize_date(
                page_date_node.get_text(" ", strip=True)
                if page_date_node is not None
                else None
            )
            evidence_ok = (
                spec["local_code"] in text
                and iso_date_to_chinese(
                    spec["expected_delisting_period_end_date"]
                )
                in text
                and iso_date_to_chinese(spec["expected_delist_date"]) in text
                and page_date_candidate == spec["expected_page_date"]
            )
        if not evidence_ok:
            gaps.append(
                {
                    "source_id": spec["source_id"],
                    "status": "UNRESOLVED__DELIST_EPISODE_MARKERS_INCOMPLETE",
                    "local_code": spec["local_code"],
                }
            )
            continue
        assert row is not None
        candidates.append(
            {
                "source_id": "SSE_OFFICIAL",
                "candidate_type": "DELISTED_LISTING_EPISODE",
                "local_code": spec["local_code"],
                "short_name": row.get("原公司简称"),
                "listing_date_candidate": normalize_date(
                    row.get("上市日期")
                ),
                "delisting_period_end_date_candidate": spec[
                    "expected_delisting_period_end_date"
                ],
                "termination_date_candidate": normalize_date(
                    row.get("终止上市日期")
                ),
                "status_code_candidate": "TERMINATED_LISTING",
                "official_page_date_candidate": page_date_candidate,
                "first_publication_semantics_proven": False,
                "published_at": None,
                "available_at": None,
                "permanent_security_id": None,
                "listing_episode_no": None,
                "revision_id": None,
                "interval_semantics_closed": False,
                "source_bindings": [
                    {
                        "path": f"{DEFAULT_CANDIDATE}/{receipt['path']}",
                        "sha256": receipt["sha256"],
                    },
                    file_binding(
                        sse_root / "raw/009_delisted_star.xls",
                        workspace=workspace,
                    ),
                ],
                "boundary": (
                    "The official delisting announcement and the official "
                    "terminated STAR table prove one bounded delisting episode "
                    "candidate. No permanent identity, full status interval, "
                    "revision chain, first-publication time or available_at is "
                    "inferred."
                ),
            }
        )
    return candidates, gaps


def verify_reused_v1(workspace: Path) -> dict[str, Any]:
    verification_path = workspace / V1_REPORT / "VERIFICATION.json"
    verification = read_json(verification_path)
    checks: list[dict[str, Any]] = []
    for source in verification["source_manifest_verification"]["sources"]:
        for item in source["checks"]:
            path = workspace / item["path"]
            actual_exists = path.is_file()
            actual_bytes = path.stat().st_size if actual_exists else None
            actual_sha = sha256_file(path) if actual_exists else None
            passed = (
                actual_exists
                and actual_bytes == int(item["declared_bytes"])
                and actual_sha == item["declared_sha256"]
            )
            checks.append(
                {
                    "source_id": source["source_id"],
                    "path": item["path"],
                    "declared_bytes": int(item["declared_bytes"]),
                    "actual_bytes": actual_bytes,
                    "declared_sha256": item["declared_sha256"],
                    "actual_sha256": actual_sha,
                    "status": "PASS" if passed else "FAIL",
                }
            )
    failures = [row for row in checks if row["status"] != "PASS"]
    return {
        "v1_verification": file_binding(
            verification_path, workspace=workspace
        ),
        "declared_files": len(checks),
        "passed": len(checks) - len(failures),
        "failed": len(failures),
        "status": "PASS" if not failures else "FAIL",
        "checks": checks,
    }


def extract_core01_contract(workspace: Path) -> dict[str, Any]:
    contract = read_json(workspace / CORE_CONTRACT)
    dataset = next(
        item for item in contract["datasets"] if item["plan_id"] == "CORE-01"
    )
    return {
        "contract_file": file_binding(
            workspace / CORE_CONTRACT, workspace=workspace
        ),
        "plan_id": dataset["plan_id"],
        "dataset_id": dataset["dataset_id"],
        "quality_gates": dataset["quality_gates"],
        "known_blockers": dataset["known_blockers"],
        "pit_contract": dataset["pit_contract"],
    }


def summarize_receipts(
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "cap": GLOBAL_NETWORK_REQUEST_CAP,
        "attempted": len(receipts),
        "preserved": sum(
            row["result"] == "RESPONSE_PRESERVED" for row in receipts
        ),
        "failed": sum(
            row["result"] != "RESPONSE_PRESERVED" for row in receipts
        ),
        "status_counts": dict(
            sorted(Counter(row["result"] for row in receipts).items())
        ),
        "http_status_counts": dict(
            sorted(
                Counter(
                    str(row.get("http_status"))
                    for row in receipts
                    if row.get("http_status") is not None
                ).items()
            )
        ),
        "endpoint_authentication_configured": False,
        "endpoint_authorization_observation_complete": all(
            "endpoint_authorization_sent" in row for row in receipts
        ),
        "endpoint_authorization_headers_sent": sum(
            row.get("endpoint_authorization_sent") is True
            for row in receipts
        ),
        "transport_proxy_authentication_fully_observable": False,
        "cookies_sent": sum(
            row.get("cookies_sent") is True for row in receipts
        ),
        "paid_endpoints_used": 0,
    }


def analyze_szse_event_rows(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    document_counts = Counter(
        row["document_id"] for row in rows if row["document_id"]
    )
    return {
        "documents_across_multiple_families": sorted(
            key for key, count in document_counts.items() if count > 1
        ),
        "metadata_date_conflicts": [
            {
                "document_id": row["document_id"],
                "docpubtime_date_candidate": row[
                    "docpubtime_date_candidate"
                ],
                "metadata_url_date_candidate": row[
                    "metadata_url_date_candidate"
                ],
            }
            for row in rows
            if row["metadata_url_date_matches_docpubtime_date"] is False
        ],
        "title_scope_not_established": [
            {
                "document_id": row["document_id"],
                "search_family": row["search_family"],
                "title": row["title"],
            }
            for row in rows
            if row["a_share_scope_assessment"]
            == "NOT_ESTABLISHED_FROM_TITLE"
        ],
    }


def compute_gate_recomputation(
    *,
    bse_summary: dict[str, Any],
    sse_relist_count: int,
    szse_coverage: list[dict[str, Any]],
    szse_event_count: int,
) -> dict[str, Any]:
    interval_partial = (
        bse_summary.get("interval_candidates", 0) > 0
        or sse_relist_count > 0
    )
    publication_partial = szse_event_count > 0 or sse_relist_count > 0
    all_search_pages_complete = bool(szse_coverage) and all(
        row["complete_keyword_pagination"] for row in szse_coverage
    )
    gates = [
        {
            "gate_id": "CORE01-Q1",
            "severity": "BLOCK",
            "baseline_v1": "FAIL",
            "strict_status_v2": "FAIL",
            "closure_assessment": "UNRESOLVED",
            "subchecks": [
                {
                    "subcheck_id": "current_trd_co_code_unique",
                    "strict_status": "UNKNOWN",
                    "closure_assessment": "PARTIAL",
                    "reason": (
                        "SSE/SZSE current local-code uniqueness is reused. "
                        "BSE code-mapping rows are not a current security master."
                    ),
                },
                {
                    "subcheck_id": "local_code_is_not_permanent_identity",
                    "strict_status": "FAIL",
                    "closure_assessment": "PASS",
                    "reason": (
                        "Official relisting and BSE code-switch evidence adds "
                        "more direct counterexamples to local-code permanence."
                    ),
                },
                {
                    "subcheck_id": "permanent_identity_contract_fields",
                    "strict_status": "FAIL",
                    "closure_assessment": "UNRESOLVED",
                    "reason": (
                        "issuer_id, permanent security_id, listing_episode_no "
                        "and complete effective/revision fields remain absent."
                    ),
                },
                {
                    "subcheck_id": "uncertain_mapping_quarantine",
                    "strict_status": "UNKNOWN",
                    "closure_assessment": "PARTIAL",
                    "reason": (
                        "New mappings remain candidates with no auto-merge; "
                        "completeness of the uncertainty set is unproven."
                    ),
                },
            ],
        },
        {
            "gate_id": "CORE01-Q2",
            "severity": "BLOCK",
            "baseline_v1": "FAIL",
            "strict_status_v2": "FAIL",
            "closure_assessment": (
                "PARTIAL" if interval_partial else "UNRESOLVED"
            ),
            "subchecks": [
                {
                    "subcheck_id": "code_and_listing_status_intervals_exist",
                    "strict_status": "FAIL",
                    "closure_assessment": (
                        "PARTIAL" if interval_partial else "UNRESOLVED"
                    ),
                    "reason": (
                        "Bounded BSE code-switch and SSE relisting episode "
                        "candidates exist, but there is no full three-venue "
                        "permanent-ID interval history."
                    ),
                },
                {
                    "subcheck_id": "interval_overlap_and_gap_audit",
                    "strict_status": "UNKNOWN",
                    "closure_assessment": "UNRESOLVED",
                    "reason": (
                        "No permanent-identity-keyed overlap/gap audit can run. "
                        "Candidate-local code duplicate checks are not an "
                        "interval overlap/gap audit."
                    ),
                },
            ],
        },
        {
            "gate_id": "CORE01-Q3",
            "severity": "BLOCK",
            "baseline_v1": "UNKNOWN",
            "strict_status_v2": "FAIL",
            "closure_assessment": "UNRESOLVED",
            "subchecks": [
                {
                    "subcheck_id": "three_venue_current_rows_observed",
                    "strict_status": "FAIL",
                    "closure_assessment": "PARTIAL",
                    "reason": (
                        "BSE first-party business rows are now observed in its "
                        "code-mapping table, but no current BSE A-share master "
                        "or listing-status universe is accepted."
                    ),
                },
                {
                    "subcheck_id": "current_and_delisted_a_share_completeness",
                    "strict_status": "FAIL",
                    "closure_assessment": "UNRESOLVED",
                    "reason": (
                        "SSE current/terminated tables and SZSE keyword results "
                        "do not prove complete current+delisted+relisted coverage; "
                        "BSE current and delisted universes remain missing."
                    ),
                },
                {
                    "subcheck_id": "year_venue_board_coverage_matrix",
                    "strict_status": "FAIL",
                    "closure_assessment": "UNRESOLVED",
                    "reason": (
                        "No complete permanent-ID keyed year/venue/board matrix "
                        "can be generated from the bounded evidence."
                    ),
                },
            ],
        },
        {
            "gate_id": "CORE01-Q4",
            "severity": "BLOCK",
            "baseline_v1": "FAIL",
            "strict_status_v2": "FAIL",
            "closure_assessment": (
                "PARTIAL" if publication_partial else "UNRESOLVED"
            ),
            "subchecks": [
                {
                    "subcheck_id": "history_publication_and_availability",
                    "strict_status": "FAIL",
                    "closure_assessment": (
                        "PARTIAL" if publication_partial else "UNRESOLVED"
                    ),
                    "reason": (
                        "Official document-date/docpubtime fields are hash-bound, "
                        "but first-publication semantics, revision/supersedes "
                        "chains and conservative available_at remain unclosed."
                    ),
                }
            ],
        },
        {
            "gate_id": "CORE01-Q5",
            "severity": "REVIEW",
            "baseline_v1": "UNKNOWN",
            "strict_status_v2": "UNKNOWN",
            "closure_assessment": "PARTIAL",
            "subchecks": [
                {
                    "subcheck_id": "cross_source_conflicts_preserved",
                    "strict_status": "UNKNOWN",
                    "closure_assessment": "PARTIAL",
                    "reason": (
                        "All ambiguous and overlapping mappings are retained "
                        "without silent merge; complete cross-source adjudication "
                        "cannot run before three-venue history is complete."
                    ),
                }
            ],
        },
    ]
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
        "stage_a": {
            "status": "BLOCKED__PRE_ADMISSION_EVIDENCE_NOT_READY",
            "ready": False,
            "common_checks": {
                "phase_a_missing_items": "FAIL",
                "rights": "FAIL",
                "schema_semantics": "FAIL",
                "pit": "FAIL",
                "revision_chain": "FAIL",
                "official_cross_source_quality": "FAIL",
                "known_contract_blockers": "FAIL",
            },
            "blocking_common_check_count": 7,
            "blocking_quality_gates": [
                "CORE01-Q1",
                "CORE01-Q2",
                "CORE01-Q3",
                "CORE01-Q4",
            ],
            "blocking_quality_gate_count": 4,
            "total_blocking_reasons": 11,
            "status_transition_from_v1": False,
        },
        "gates": gates,
        "diagnostics": {
            "bounded_interval_evidence_present": interval_partial,
            "publication_field_evidence_present": publication_partial,
            "all_szse_keyword_pagination_complete": all_search_pages_complete,
            "keyword_pagination_is_not_archive_completeness": True,
        },
    }


def manifest_for(
    paths: Iterable[Path],
    *,
    logical_root: str,
    actual_root: Path,
    artifact_role: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted(paths):
        relative = path.relative_to(actual_root).as_posix()
        rows.append(
            file_binding(
                path,
                logical_path=f"{logical_root}/{relative}",
            )
        )
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "artifact_role": artifact_role,
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
        "files": rows,
    }


def verify_manifest(
    manifest: dict[str, Any],
    *,
    logical_root: str,
    actual_root: Path,
) -> list[str]:
    errors: list[str] = []
    prefix = logical_root.rstrip("/") + "/"
    declared_relatives: list[str] = []
    seen_logical_paths: set[str] = set()
    for row in manifest["files"]:
        logical = row["path"]
        if logical in seen_logical_paths:
            errors.append(f"duplicate logical path: {logical}")
            continue
        seen_logical_paths.add(logical)
        if not logical.startswith(prefix):
            errors.append(f"logical path outside root: {logical}")
            continue
        relative = logical[len(prefix) :]
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            errors.append(f"unsafe logical path: {logical}")
            continue
        declared_relatives.append(relative_path.as_posix())
        path = actual_root / relative
        if (
            not path.is_file()
            or path.stat().st_size != row["bytes"]
            or sha256_file(path) != row["sha256"]
        ):
            errors.append(logical)
    excluded = manifest.get("self_excluded_from_files")
    actual_relatives = {
        path.relative_to(actual_root).as_posix()
        for path in actual_root.rglob("*")
        if path.is_file()
        and (
            excluded is None
            or path.relative_to(actual_root).as_posix() != excluded
        )
    }
    declared_set = set(declared_relatives)
    for relative in sorted(actual_relatives - declared_set):
        errors.append(f"unexpected file not declared: {relative}")
    for relative in sorted(declared_set - actual_relatives):
        errors.append(f"declared file missing: {relative}")
    return errors


def run_unit_tests(workspace: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "unittest",
        "-v",
        "control.tests.test_build_core01_official_identity_closure_v2",
    ]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (completed.stdout + completed.stderr).strip()
    tests_run_match = re.search(r"Ran (\d+) tests?", output)
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "command": command,
        "exit_code": completed.returncode,
        "tests_run": (
            int(tests_run_match.group(1)) if tests_run_match else None
        ),
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "output": output,
    }


def render_readme(
    gate: dict[str, Any],
    coverage: dict[str, Any],
    gaps: dict[str, Any],
    request_summary: dict[str, Any],
    test_results: dict[str, Any],
) -> str:
    search_lines = "\n".join(
        (
            f"| {row['family']} | {row['total_reported']} | "
            f"{row['unique_document_ids']} | "
            f"{str(row['complete_keyword_pagination']).upper()} | "
            f"{row['stop_reason'] or '-'} |"
        )
        for row in coverage["SZSE"]["search_families"]
    )
    gate_lines = "\n".join(
        (
            f"| {row['gate_id']} | {row['severity']} | "
            f"{row['strict_status_v2']} | {row['closure_assessment']} |"
        )
        for row in gate["gates"]
    )
    endpoint_auth_observation = (
        "COMPLETE__ZERO_ENDPOINT_AUTHORIZATION_HEADERS"
        if request_summary["endpoint_authorization_observation_complete"]
        and request_summary["endpoint_authorization_headers_sent"] == 0
        else "INCOMPLETE__NO_ABSOLUTE_CREDENTIAL_ABSENCE_CLAIM"
    )
    return f"""# CORE-01 官方身份区间缺口闭合 v2

运行：`{RUN_ID}`

## 结论

本包没有使 CORE-01 通过 Stage A。严格状态仍为
`BLOCKED__PRE_ADMISSION_EVIDENCE_NOT_READY`，Stage A `ready=false`，
7 个通用检查和 `CORE01-Q1`～`Q4` 四个 BLOCK 门合计仍有 11 个阻断。
所有新证据固定为 `INSPECTION_ONLY/NOT_ADMITTED`；没有正式准入、catalog
写入、质量晋级、快照发布或研究读取。

本次真正补强了三类局部证据：

- 上交所两条重新上市公告与既有现存/终止上市表共同形成
  {coverage['SSE']['relisted_episode_candidates']} 条可验签的上市阶段候选；
- 上交所 688287 摘牌公告与既有科创板终止上市表共同形成
  {coverage['SSE']['delisted_episode_candidates_from_new_page']}
  条有界终止上市候选；
- 北交所官方新旧代码对照表与切换公告形成
  {coverage['BSE']['interval_candidates']} 条代码切换区间候选。

北交所表中“上市日期”已按原始脚注保存：原精选层平移公司显示的是原精选层
挂牌日期，禁止解释为统一的北交所上市日。局部证据证明“本地代码不是永久
身份”，并把 Q2/Q4 的补证闭合度推进到 `PARTIAL`；但没有永久
`security_id/issuer_id`、完整三所 current+delisted+
relisted 档案、完整上市状态区间、首次公布/修订语义和 `available_at`，
所以任何严格门都没有转为 PASS。

## 逐门重算

| 门 | 严重度 | 严格状态 | 补证闭合度 |
|---|---|---|---|
{gate_lines}

逐 subcheck 理由见 `GATE_RECOMPUTATION.json`。

## 新官方响应

- 总请求硬上限：`{GLOBAL_NETWORK_REQUEST_CAP}`；
- 实际请求：`{request_summary['attempted']}`；
- 成功保留：`{request_summary['preserved']}`；
- HTTP/内容/网络失败：`{request_summary['failed']}`；
- 端点认证/登录/密钥未由工具配置；Authorization 逐请求观察：
  `{endpoint_auth_observation}`；
- 首轮 transport/proxy 认证并非完全可观察，故不作“所有传输凭据绝对不存在”
  的超范围声明；Cookie 发送
  `{request_summary['cookies_sent']}` 次，付费端点使用
  `{request_summary['paid_endpoints_used']}` 次；
- 旧北交所 `nq/listedcompany.html` WAF/302 端点：未重试；
- 所有响应或失败均在
  `{DEFAULT_CANDIDATE}/REQUEST_RECEIPTS.json` 记录 URL、请求时间、状态、
  字节数和 SHA-256；原始正文见同包 `raw/`。

### 深交所关键词分页

| 查询族 | 官方 totalSize | 唯一文档 | 该查询分页完整 | 停止原因 |
|---|---:|---:|---|---|
{search_lines}

“该查询分页完整”只表示指定关键词、频道与时间窗的分页结果完整，不能证明
交易所完整历史档案、首次公布语义或修订链完整。

## 仍未闭合

- 北交所当前 A 股主表、退市/重新上市全历史和永久身份仍缺失；
- 深交所关键词/频道检索不是完整事件本体，`docpubtime` 未被提升为
  first-publication 或 `available_at`；
  {coverage['SZSE']['metadata_url_docpubtime_date_conflicts']} 条
  `docpubtime` 日期与元数据 URL 日期冲突已显式保留；
- 上交所重新上市仅是两条有界样本，不能证明重新上市档案全集；
- 三所都没有满足合同的永久身份、完整代码/名称/板块/状态有效区间和修订链；
- 来源公开可访问不等于自动批量保存、内部共享与再利用权已经闭合。

完整异常和缺口见 `ANOMALIES_AND_GAPS.json`，共
{len(gaps['open_gaps'])} 项显式缺口。

## 验证

- v1 复用官方源文件重新验签：
  `{coverage['reused_v1']['passed']}/{coverage['reused_v1']['declared_files']}`；
- 新 raw 清单、响应 SHA-256、候选清单和报告清单均已复核；
- 实际执行的生成器与测试源码 SHA-256 已写入 `SOURCE_BINDINGS.json`；
- 无网络单测：`{test_results['status']}`，
  `{test_results['tests_run']}` 项，退出码 `{test_results['exit_code']}`；
- datasets.json、五权威/质量政策与正式 snapshot/published 根前后状态一致。

测试命令及输出见 `TEST_RESULTS.json`；正式边界与守卫见
`VERIFICATION.json`。
"""


def build(workspace: Path, candidate: Path, report: Path) -> int:
    require_new_output(
        candidate, workspace / "tmp" / "d2a04b-source-closure"
    )
    require_new_output(report, workspace / "reports" / "data-governance")
    candidate_stage = candidate.with_name(candidate.name + ".staging")
    report_stage = report.with_name(report.name + ".staging")
    require_new_output(
        candidate_stage, workspace / "tmp" / "d2a04b-source-closure"
    )
    require_new_output(
        report_stage, workspace / "reports" / "data-governance"
    )
    candidate_stage.mkdir(parents=True)
    report_stage.mkdir(parents=True)

    protected_pre = guard_state(workspace)
    reused = verify_reused_v1(workspace)
    if reused["status"] != "PASS":
        raise RuntimeError("v1 reused evidence failed byte/hash verification")
    contract = extract_core01_contract(workspace)

    session = requests.Session()
    session.auth = NoEndpointAuthorization()
    session.headers.update(
        {
            "User-Agent": (
                "Quantification-CORE01-OfficialIdentity-v2/1.0 "
                "(unauthenticated; bounded noncommercial inspection)"
            ),
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
        }
    )
    receipts: list[dict[str, Any]] = []
    for spec in FIXED_GET_SOURCES:
        receipt, _ = fetch_one(
            session,
            candidate=candidate_stage,
            sequence=len(receipts) + 1,
            source_id=spec["source_id"],
            venue=spec["venue"],
            method="GET",
            url=spec["url"],
            relative_path=spec["path"],
            kind=spec["kind"],
            markers=spec["markers"],
        )
        receipts.append(receipt)
        time.sleep(0.35)
    szse_coverage, szse_events = collect_szse_searches(
        session, candidate_stage, receipts
    )
    session.close()

    write_json(candidate_stage / "REQUEST_RECEIPTS.json", receipts)
    bse_intervals, bse_summary = parse_bse_code_mapping(
        candidate_stage, receipts
    )
    sse_relists, sse_relist_gaps = build_sse_relist_candidates(
        workspace, candidate_stage, receipts
    )
    sse_delists, sse_delist_gaps = build_sse_delist_candidates(
        workspace, candidate_stage, receipts
    )
    interval_candidates = sse_relists + sse_delists + bse_intervals
    write_jsonl(
        candidate_stage / "IDENTITY_INTERVAL_CANDIDATES.jsonl",
        interval_candidates,
    )
    write_jsonl(
        candidate_stage / "FIRST_PUBLICATION_EVIDENCE.jsonl",
        szse_events,
    )
    write_json(candidate_stage / "SEARCH_COVERAGE.json", szse_coverage)
    write_json(
        candidate_stage / "REUSED_EVIDENCE_MANIFEST.json", reused
    )

    raw_files = [
        path
        for path in candidate_stage.rglob("*")
        if path.is_file() and path.is_relative_to(candidate_stage / "raw")
    ]
    raw_manifest = manifest_for(
        raw_files,
        logical_root=f"{DEFAULT_CANDIDATE}/raw",
        actual_root=candidate_stage / "raw",
        artifact_role="new_official_raw_responses_and_failure_bodies",
    )
    write_json(candidate_stage / "RAW_FILE_MANIFEST.json", raw_manifest)

    raw_errors = verify_manifest(
        raw_manifest,
        logical_root=f"{DEFAULT_CANDIDATE}/raw",
        actual_root=candidate_stage / "raw",
    )
    receipt_hash_errors: list[str] = []
    for receipt in receipts:
        if not receipt.get("path") or receipt.get("sha256") is None:
            continue
        path = candidate_stage / receipt["path"]
        if (
            not path.is_file()
            or path.stat().st_size != receipt["bytes"]
            or sha256_file(path) != receipt["sha256"]
        ):
            receipt_hash_errors.append(receipt["source_id"])
    candidate_verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "verified_at_asia_shanghai": now_shanghai(),
        "status": (
            "PASS__RAW_AND_RECEIPTS_HASH_BOUND__NOT_ADMITTED"
            if not raw_errors and not receipt_hash_errors
            else "FAIL__RAW_OR_RECEIPT_MISMATCH"
        ),
        "raw_manifest_errors": raw_errors,
        "receipt_hash_errors": receipt_hash_errors,
        "request_cap": GLOBAL_NETWORK_REQUEST_CAP,
        "request_count": len(receipts),
        "allowed_hosts": sorted(ALLOWED_HOSTS),
        "blocked_bse_endpoint_retried": False,
        "endpoint_authentication_configured": False,
        "endpoint_authorization_observation_complete": all(
            "endpoint_authorization_sent" in row for row in receipts
        ),
        "endpoint_authorization_headers_sent": sum(
            row.get("endpoint_authorization_sent") is True
            for row in receipts
        ),
        "transport_proxy_authentication_fully_observable": False,
        "paid_source_used": False,
        "formal_admission": False,
    }
    write_json(candidate_stage / "VERIFICATION.json", candidate_verification)
    if candidate_verification["status"].startswith("FAIL"):
        raise RuntimeError("candidate raw/receipt verification failed")

    candidate_files = [
        path
        for path in candidate_stage.rglob("*")
        if path.is_file()
        and path != candidate_stage / "CANDIDATE_MANIFEST.json"
    ]
    candidate_manifest = manifest_for(
        candidate_files,
        logical_root=DEFAULT_CANDIDATE,
        actual_root=candidate_stage,
        artifact_role="core01_v2_inspection_candidate",
    )
    candidate_manifest[
        "self_excluded_from_files"
    ] = "CANDIDATE_MANIFEST.json"
    write_json(
        candidate_stage / "CANDIDATE_MANIFEST.json", candidate_manifest
    )
    candidate_manifest_errors = verify_manifest(
        candidate_manifest,
        logical_root=DEFAULT_CANDIDATE,
        actual_root=candidate_stage,
    )
    if candidate_manifest_errors:
        raise RuntimeError(
            "candidate manifest verification failed: "
            + ", ".join(candidate_manifest_errors)
        )

    v1_coverage = read_json(
        workspace / V1_REPORT / "OFFICIAL_COVERAGE_MATRIX.json"
    )
    szse_analysis = analyze_szse_event_rows(szse_events)
    duplicate_across_families = szse_analysis[
        "documents_across_multiple_families"
    ]
    szse_metadata_date_conflicts = szse_analysis[
        "metadata_date_conflicts"
    ]
    szse_title_scope_not_established = szse_analysis[
        "title_scope_not_established"
    ]
    request_summary = summarize_receipts(receipts)
    coverage = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
        "SSE": {
            **v1_coverage["venues"]["SSE"],
            "relisted_episode_candidates": len(sse_relists),
            "relisted_episode_archive_complete": False,
            "delisted_episode_candidates_from_new_page": len(sse_delists),
            "bounded_episode_candidates_v2": (
                len(sse_relists) + len(sse_delists)
            ),
        },
        "SZSE": {
            **v1_coverage["venues"]["SZSE"],
            "search_families": szse_coverage,
            "event_rows_preserved_v2": len(szse_events),
            "event_documents_across_multiple_families": len(
                duplicate_across_families
            ),
            "metadata_url_docpubtime_date_conflicts": len(
                szse_metadata_date_conflicts
            ),
            "title_scope_not_established_count": len(
                szse_title_scope_not_established
            ),
            "first_publication_semantics_complete": False,
            "status_interval_archive_complete": False,
        },
        "BSE": {
            **bse_summary,
            "previous_blocked_endpoint_retried": False,
            "previous_waf_business_rows": 0,
        },
        "reused_v1": {
            "declared_files": reused["declared_files"],
            "passed": reused["passed"],
            "failed": reused["failed"],
        },
        "three_venue_any_identity_business_evidence_observed": (
            bse_summary.get("rows", 0) > 0
        ),
        "three_venue_current_master_observed": False,
        "complete_current_and_delisted_a_share_universe": False,
        "complete_current_delisted_relisted_history": False,
        "complete_year_venue_board_matrix": False,
    }
    gate = compute_gate_recomputation(
        bse_summary=bse_summary,
        sse_relist_count=len(sse_relists),
        szse_coverage=szse_coverage,
        szse_event_count=len(szse_events),
    )
    open_gaps = [
        {
            "gap_id": "CORE01-G1-PERMANENT-IDENTITY",
            "blocks": ["CORE01-Q1", "CORE01-Q2"],
            "status": "OPEN",
            "description": (
                "No official three-venue issuer_id/permanent security_id and "
                "listing_episode_no mapping is complete."
            ),
        },
        {
            "gap_id": "CORE01-G2-COMPLETE-CODE-NAME-BOARD-INTERVALS",
            "blocks": ["CORE01-Q2"],
            "status": "OPEN",
            "description": (
                "Complete code, name and board valid_from/valid_to histories "
                "keyed by permanent identity remain unavailable."
            ),
        },
        {
            "gap_id": "CORE01-G3-COMPLETE-LISTING-STATUS-INTERVALS",
            "blocks": ["CORE01-Q2", "CORE01-Q3"],
            "status": "OPEN",
            "description": (
                "Listing, suspension, delisting, relisting and transfer events "
                "do not form a complete three-venue effective interval chain."
            ),
        },
        {
            "gap_id": "CORE01-G4-BSE-CURRENT-DELISTED-RELISTED",
            "blocks": ["CORE01-Q3"],
            "status": "OPEN",
            "description": (
                "The accessible official BSE code table is not a current "
                "security master and does not contain complete delisted or "
                "relisted histories. Its listing-date field is not a uniform "
                "BJSE listing date because the official footnote assigns the "
                "former Selected Layer date to transferred companies. The old "
                "WAF endpoint was not retried."
            ),
        },
        {
            "gap_id": "CORE01-G5-SZSE-ARCHIVE-TAXONOMY",
            "blocks": ["CORE01-Q3", "CORE01-Q4"],
            "status": "OPEN",
            "description": (
                "Keyword/channel pagination cannot prove the complete official "
                "event taxonomy or that no event is omitted."
            ),
        },
        {
            "gap_id": "CORE01-G6-FIRST-PUBLICATION-SEMANTICS",
            "blocks": ["CORE01-Q4"],
            "status": "OPEN",
            "description": (
                "docpubtime and page dates are preserved but not proven to be "
                "first publication rather than an update/current document time."
            ),
        },
        {
            "gap_id": "CORE01-G7-REVISION-SUPERSEDES",
            "blocks": ["CORE01-Q4"],
            "status": "OPEN",
            "description": (
                "No complete revision_id/supersedes_revision_id chain exists "
                "for identity or listing-status events."
            ),
        },
        {
            "gap_id": "CORE01-G8-AVAILABLE-AT",
            "blocks": ["CORE01-Q4"],
            "status": "OPEN",
            "description": (
                "No candidate timestamp or date is promoted to available_at; "
                "calendar and conservative next-session rules remain unpinned."
            ),
        },
        {
            "gap_id": "CORE01-G9-RIGHTS",
            "blocks": ["STAGE-A-rights"],
            "status": "OPEN",
            "description": (
                "Public access does not close automated retrieval, local "
                "retention, internal sharing or reuse rights."
            ),
        },
        {
            "gap_id": "CORE01-G10-CROSS-SOURCE-ADJUDICATION",
            "blocks": ["CORE01-Q5", "STAGE-A-official-cross-source-quality"],
            "status": "OPEN",
            "description": (
                "All conflicts and ambiguities are retained, but complete "
                "cross-source adjudication is impossible before history closes."
            ),
        },
        {
            "gap_id": "CORE01-G11-YEAR-VENUE-BOARD-MATRIX",
            "blocks": ["CORE01-Q3"],
            "status": "OPEN",
            "description": (
                "A complete year/venue/board matrix cannot be built from "
                "current snapshots, keyword results and bounded episode samples."
            ),
        },
    ]
    gaps = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": "FAIL_CLOSED__OPEN_GAPS_RETAINED",
        "open_gaps": open_gaps,
        "anomalies": {
            "sse_duplicate_delisted_local_codes_reused_from_v1": v1_coverage[
                "venues"
            ]["SSE"]["duplicate_delisted_codes"],
            "sse_relisting_capture_gaps": sse_relist_gaps,
            "sse_delisting_capture_gaps": sse_delist_gaps,
            "szse_documents_in_multiple_query_families": duplicate_across_families,
            "szse_total_size_drift_families": [
                row["family"]
                for row in szse_coverage
                if not row["total_stable_across_pages"]
            ],
            "szse_max_pages_limit_reached_families": [
                row["family"]
                for row in szse_coverage
                if row["stop_reason"] == "MAX_PAGES_LIMIT_REACHED"
            ],
            "szse_metadata_url_docpubtime_date_conflicts": (
                szse_metadata_date_conflicts
            ),
            "szse_title_scope_not_established": (
                szse_title_scope_not_established
            ),
            "bse_old_code_duplicate_count": bse_summary.get(
                "old_code_duplicate_count"
            ),
            "bse_new_code_duplicate_count": bse_summary.get(
                "new_code_duplicate_count"
            ),
            "bse_table_listing_dates_unparsed_count": bse_summary.get(
                "table_listing_dates_unparsed_count"
            ),
            "bse_selected_layer_transfer_footnote_observed": bse_summary.get(
                "selected_layer_transfer_footnote_observed"
            ),
        },
        "silent_identity_merges": 0,
        "silent_identity_splits": 0,
        "timestamps_promoted_to_available_at": 0,
        "unknowns_filled_with_zero": 0,
    }
    rights = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "overall_status": "FAIL__FORMAL_SOURCE_RIGHTS_NOT_CLOSED",
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
        "network_policy": {
            "official_first_party_only": True,
            "https_only": True,
            "allowed_hosts": sorted(ALLOWED_HOSTS),
            "global_request_cap": GLOBAL_NETWORK_REQUEST_CAP,
            "serial_requests": True,
            "redirects_followed": False,
            "endpoint_authentication_configured": False,
            "endpoint_authorization_headers_sent": sum(
                row.get("endpoint_authorization_sent") is True
                for row in receipts
            ),
            "endpoint_authorization_observation_complete": all(
                "endpoint_authorization_sent" in row for row in receipts
            ),
            "transport_proxy_authentication_fully_observable": False,
            "cookies_sent": False,
            "paid_sources_used": False,
            "blocked_bse_endpoint_retried": False,
        },
        "decision": (
            "New responses remain in the inspection package. Public reachability "
            "does not grant formal automated-ingest, internal-sharing or "
            "redistribution rights."
        ),
    }
    write_json(report_stage / "GATE_RECOMPUTATION.json", gate)
    write_json(report_stage / "OFFICIAL_COVERAGE_MATRIX.json", coverage)
    write_json(report_stage / "ANOMALIES_AND_GAPS.json", gaps)
    write_json(report_stage / "RIGHTS_AND_BOUNDARIES.json", rights)
    source_bindings = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "executed_generator_source": file_binding(
            workspace
            / "control/tools/build_core01_official_identity_closure_v2.py",
            workspace=workspace,
        ),
        "executed_test_source": file_binding(
            workspace
            / "control/tests/test_build_core01_official_identity_closure_v2.py",
            workspace=workspace,
        ),
        "candidate_manifest": file_binding(
            candidate_stage / "CANDIDATE_MANIFEST.json",
            logical_path=f"{DEFAULT_CANDIDATE}/CANDIDATE_MANIFEST.json",
        ),
        "request_receipts": file_binding(
            candidate_stage / "REQUEST_RECEIPTS.json",
            logical_path=f"{DEFAULT_CANDIDATE}/REQUEST_RECEIPTS.json",
        ),
        "raw_manifest": file_binding(
            candidate_stage / "RAW_FILE_MANIFEST.json",
            logical_path=f"{DEFAULT_CANDIDATE}/RAW_FILE_MANIFEST.json",
        ),
        "reused_v1_evidence": {
            "verification": file_binding(
                workspace / V1_REPORT / "VERIFICATION.json",
                workspace=workspace,
            ),
            "report_manifest": file_binding(
                workspace / V1_REPORT / "REPORT_MANIFEST.json",
                workspace=workspace,
            ),
            "source_files_verified": reused["passed"],
        },
        "core01_contract": contract,
        "new_request_summary": request_summary,
    }
    write_json(report_stage / "SOURCE_BINDINGS.json", source_bindings)

    test_results = run_unit_tests(workspace)
    write_json(report_stage / "TEST_RESULTS.json", test_results)
    if test_results["status"] != "PASS":
        raise RuntimeError("CORE-01 v2 unit tests failed")

    protected_post = guard_state(workspace)
    if protected_pre != protected_post:
        changed = [
            key
            for key in ("files", "trees")
            if protected_pre[key] != protected_post[key]
        ]
        raise RuntimeError(
            "protected formal state changed during build: " + ", ".join(changed)
        )
    report_verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "verified_at_asia_shanghai": now_shanghai(),
        "status": (
            "PASS__EVIDENCE_HASH_BOUND__FORMAL_STATE_UNCHANGED__"
            "STAGE_A_BLOCKED"
        ),
        "purpose_ceiling": PURPOSE_CEILING,
        "quality_status": QUALITY_STATUS,
        "formal_admission": False,
        "source_checks": {
            "reused_v1_files_declared": reused["declared_files"],
            "reused_v1_files_passed": reused["passed"],
            "reused_v1_files_failed": reused["failed"],
            "new_raw_manifest_errors": raw_errors,
            "new_receipt_hash_errors": receipt_hash_errors,
            "candidate_manifest_errors": candidate_manifest_errors,
            "new_network_request_count": len(receipts),
            "network_request_cap": GLOBAL_NETWORK_REQUEST_CAP,
        },
        "semantic_checks": {
            "strict_block_gates_passed": 0,
            "stage_a_ready": False,
            "stage_a_blocking_reasons": 11,
            "formal_status_transition": False,
            "identity_candidates_are_not_formal_intervals": True,
            "permanent_identity_overlap_gap_audit_executed": False,
            "bse_selected_layer_date_footnote_preserved": bse_summary[
                "selected_layer_transfer_footnote_observed"
            ],
            "bse_table_listing_dates_unparsed": bse_summary[
                "table_listing_dates_unparsed_count"
            ],
            "docpubtime_not_promoted_to_first_publication": True,
            "szse_metadata_date_conflicts_preserved": len(
                szse_metadata_date_conflicts
            ),
            "available_at_promotions": 0,
            "silent_identity_merges": 0,
            "unknowns_filled_with_zero": 0,
        },
        "boundary_guards": {
            "catalog_writes": 0,
            "source_registry_mutations": 0,
            "five_authority_mutations": 0,
            "quality_promotions": 0,
            "formal_snapshots": 0,
            "research_runs": 0,
            "paid_sources": 0,
            "new_keys_or_credentials": 0,
            "endpoint_authorization_observation_complete": request_summary[
                "endpoint_authorization_observation_complete"
            ],
            "endpoint_authorization_headers_sent": request_summary[
                "endpoint_authorization_headers_sent"
            ],
            "transport_proxy_authentication_fully_observable": False,
            "csmar_requests": 0,
            "deletions": 0,
        },
        "formal_state_guard": {
            "pre": protected_pre,
            "post": protected_post,
            "unchanged": True,
        },
        "test_results": {
            "status": test_results["status"],
            "tests_run": test_results["tests_run"],
            "exit_code": test_results["exit_code"],
        },
    }
    write_json(report_stage / "VERIFICATION.json", report_verification)
    (report_stage / "README.md").write_text(
        render_readme(
            gate, coverage, gaps, request_summary, test_results
        ),
        encoding="utf-8",
        newline="\n",
    )

    report_files = [
        path
        for path in report_stage.rglob("*")
        if path.is_file()
        and path != report_stage / "REPORT_MANIFEST.json"
    ]
    report_manifest = manifest_for(
        report_files,
        logical_root=DEFAULT_REPORT,
        actual_root=report_stage,
        artifact_role="core01_v2_human_audit_report",
    )
    report_manifest["formal_state"] = {
        "core_id": "CORE-01",
        "stage_a_ready": False,
        "stage_a_blocking_reasons": 11,
        "formal_admission": False,
        "research_ready": False,
        "status_transition": False,
    }
    report_manifest["self_excluded_from_files"] = "REPORT_MANIFEST.json"
    write_json(report_stage / "REPORT_MANIFEST.json", report_manifest)
    manifest_errors = verify_manifest(
        report_manifest,
        logical_root=DEFAULT_REPORT,
        actual_root=report_stage,
    )
    if manifest_errors:
        raise RuntimeError(
            "report manifest verification failed: " + ", ".join(manifest_errors)
        )

    os.replace(candidate_stage, candidate)
    os.replace(report_stage, report)
    print(
        json.dumps(
            {
                "run_id": RUN_ID,
                "candidate": candidate.as_posix(),
                "report": report.as_posix(),
                "network_requests": len(receipts),
                "responses_preserved": request_summary["preserved"],
                "request_failures": request_summary["failed"],
                "sse_relisted_episode_candidates": len(sse_relists),
                "sse_delisted_episode_candidates": len(sse_delists),
                "bse_code_interval_candidates": len(bse_intervals),
                "szse_event_documents": len(szse_events),
                "stage_a_ready": False,
                "blocking_reasons": 11,
                "formal_mutations": 0,
                "tests": test_results["status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def preserve_repair_snapshot(
    workspace: Path,
    root: Path,
    relative_paths: tuple[str, ...],
) -> list[dict[str, Any]]:
    history = root / "repair_history" / QA_REPAIR_ID
    if history.exists():
        raise FileExistsError(history)
    history.mkdir(parents=True)
    preserved: list[dict[str, Any]] = []
    for relative in relative_paths:
        source = root / relative
        if not source.is_file():
            raise FileNotFoundError(source)
        target = history / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        preserved.append(
            {
                "original_path": source.relative_to(workspace).as_posix(),
                "original_sha256": sha256_file(source),
                "preserved_copy": file_binding(
                    target,
                    workspace=workspace,
                ),
            }
        )
    return preserved


def repair_existing(
    workspace: Path,
    candidate: Path,
    report: Path,
) -> int:
    if not candidate.is_dir() or not report.is_dir():
        raise FileNotFoundError("existing v2 candidate/report are required")
    protected_pre = guard_state(workspace)

    old_candidate_manifest = read_json(
        candidate / "CANDIDATE_MANIFEST.json"
    )
    candidate_manifest_for_check = dict(old_candidate_manifest)
    candidate_manifest_for_check[
        "self_excluded_from_files"
    ] = "CANDIDATE_MANIFEST.json"
    old_candidate_errors = verify_manifest(
        candidate_manifest_for_check,
        logical_root=DEFAULT_CANDIDATE,
        actual_root=candidate,
    )
    old_report_manifest = read_json(report / "REPORT_MANIFEST.json")
    old_report_errors = verify_manifest(
        old_report_manifest,
        logical_root=DEFAULT_REPORT,
        actual_root=report,
    )
    if old_candidate_errors or old_report_errors:
        raise RuntimeError(
            "pre-repair manifest verification failed: "
            + ", ".join(old_candidate_errors + old_report_errors)
        )

    candidate_preserved = preserve_repair_snapshot(
        workspace,
        candidate,
        (
            "IDENTITY_INTERVAL_CANDIDATES.jsonl",
            "FIRST_PUBLICATION_EVIDENCE.jsonl",
            "SEARCH_COVERAGE.json",
            "VERIFICATION.json",
            "CANDIDATE_MANIFEST.json",
        ),
    )
    report_preserved = preserve_repair_snapshot(
        workspace,
        report,
        (
            "README.md",
            "GATE_RECOMPUTATION.json",
            "OFFICIAL_COVERAGE_MATRIX.json",
            "ANOMALIES_AND_GAPS.json",
            "RIGHTS_AND_BOUNDARIES.json",
            "SOURCE_BINDINGS.json",
            "TEST_RESULTS.json",
            "VERIFICATION.json",
            "REPORT_MANIFEST.json",
        ),
    )

    receipts = read_json(candidate / "REQUEST_RECEIPTS.json")
    reused = verify_reused_v1(workspace)
    if reused["status"] != "PASS":
        raise RuntimeError("v1 reused evidence failed during repair")
    contract = extract_core01_contract(workspace)

    raw_manifest = read_json(candidate / "RAW_FILE_MANIFEST.json")
    raw_errors = verify_manifest(
        raw_manifest,
        logical_root=f"{DEFAULT_CANDIDATE}/raw",
        actual_root=candidate / "raw",
    )
    receipt_hash_errors: list[str] = []
    for receipt in receipts:
        if not receipt.get("path") or receipt.get("sha256") is None:
            continue
        path = candidate / receipt["path"]
        if (
            not path.is_file()
            or path.stat().st_size != receipt["bytes"]
            or sha256_file(path) != receipt["sha256"]
        ):
            receipt_hash_errors.append(receipt["source_id"])
    if raw_errors or receipt_hash_errors:
        raise RuntimeError("raw or receipt hash mismatch during repair")

    bse_intervals, bse_summary = parse_bse_code_mapping(
        candidate, receipts
    )
    sse_relists, sse_relist_gaps = build_sse_relist_candidates(
        workspace, candidate, receipts
    )
    sse_delists, sse_delist_gaps = build_sse_delist_candidates(
        workspace, candidate, receipts
    )
    interval_candidates = sse_relists + sse_delists + bse_intervals
    atomic_write_jsonl(
        candidate / "IDENTITY_INTERVAL_CANDIDATES.jsonl",
        interval_candidates,
    )

    szse_events = [
        enrich_preserved_szse_event(row)
        for row in read_jsonl(
            candidate / "FIRST_PUBLICATION_EVIDENCE.jsonl"
        )
    ]
    atomic_write_jsonl(
        candidate / "FIRST_PUBLICATION_EVIDENCE.jsonl",
        szse_events,
    )
    szse_coverage = read_json(candidate / "SEARCH_COVERAGE.json")
    for row in szse_coverage:
        if (
            row.get("stop_reason") is None
            and row.get("planned_pages_from_last_response") is not None
            and row["pages_preserved"]
            < row["planned_pages_from_last_response"]
            and row["pages_preserved"] >= row["max_pages"]
        ):
            row["stop_reason"] = "MAX_PAGES_LIMIT_REACHED"
    atomic_write_json(
        candidate / "SEARCH_COVERAGE.json",
        szse_coverage,
    )

    request_summary = summarize_receipts(receipts)
    candidate_verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "verified_at_asia_shanghai": now_shanghai(),
        "status": (
            "PASS__RAW_AND_RECEIPTS_HASH_BOUND__NOT_ADMITTED__"
            "INITIAL_AUTH_HEADER_OBSERVATION_INCOMPLETE"
        ),
        "raw_manifest_errors": raw_errors,
        "receipt_hash_errors": receipt_hash_errors,
        "request_cap": GLOBAL_NETWORK_REQUEST_CAP,
        "request_count": len(receipts),
        "allowed_hosts": sorted(ALLOWED_HOSTS),
        "blocked_bse_endpoint_retried": False,
        "endpoint_authentication_configured_by_tool": False,
        "endpoint_authorization_observation_complete": request_summary[
            "endpoint_authorization_observation_complete"
        ],
        "endpoint_authorization_headers_sent": request_summary[
            "endpoint_authorization_headers_sent"
        ],
        "transport_proxy_authentication_fully_observable": False,
        "paid_source_used": False,
        "formal_admission": False,
        "qa_repair": {
            "repair_id": QA_REPAIR_ID,
            "network_requests_during_repair": 0,
            "raw_files_modified": 0,
            "bse_table_listing_dates_parsed": bse_summary[
                "table_listing_dates_parsed_count"
            ],
            "bse_table_listing_dates_unparsed": bse_summary[
                "table_listing_dates_unparsed_count"
            ],
            "bse_selected_layer_footnote_preserved": bse_summary[
                "selected_layer_transfer_footnote_observed"
            ],
            "sse_delisted_episode_candidates_added": len(sse_delists),
        },
    }
    atomic_write_json(
        candidate / "VERIFICATION.json",
        candidate_verification,
    )

    candidate_files = [
        path
        for path in candidate.rglob("*")
        if path.is_file()
        and path != candidate / "CANDIDATE_MANIFEST.json"
    ]
    candidate_manifest = manifest_for(
        candidate_files,
        logical_root=DEFAULT_CANDIDATE,
        actual_root=candidate,
        artifact_role="core01_v2_inspection_candidate",
    )
    candidate_manifest[
        "self_excluded_from_files"
    ] = "CANDIDATE_MANIFEST.json"
    candidate_manifest["qa_repair"] = {
        "repair_id": QA_REPAIR_ID,
        "pre_repair_candidate_manifest_sha256": (
            old_candidate_manifest["files"]
            and next(
                row["original_sha256"]
                for row in candidate_preserved
                if row["original_path"].endswith(
                    "/CANDIDATE_MANIFEST.json"
                )
            )
        ),
        "raw_files_modified": 0,
        "deletions": 0,
    }
    atomic_write_json(
        candidate / "CANDIDATE_MANIFEST.json",
        candidate_manifest,
    )
    candidate_manifest_errors = verify_manifest(
        candidate_manifest,
        logical_root=DEFAULT_CANDIDATE,
        actual_root=candidate,
    )
    if candidate_manifest_errors:
        raise RuntimeError(
            "repaired candidate manifest failed: "
            + ", ".join(candidate_manifest_errors)
        )

    v1_coverage = read_json(
        workspace / V1_REPORT / "OFFICIAL_COVERAGE_MATRIX.json"
    )
    szse_analysis = analyze_szse_event_rows(szse_events)
    duplicate_across_families = szse_analysis[
        "documents_across_multiple_families"
    ]
    szse_metadata_date_conflicts = szse_analysis[
        "metadata_date_conflicts"
    ]
    szse_title_scope_not_established = szse_analysis[
        "title_scope_not_established"
    ]
    coverage = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
        "SSE": {
            **v1_coverage["venues"]["SSE"],
            "relisted_episode_candidates": len(sse_relists),
            "relisted_episode_archive_complete": False,
            "delisted_episode_candidates_from_new_page": len(sse_delists),
            "bounded_episode_candidates_v2": (
                len(sse_relists) + len(sse_delists)
            ),
        },
        "SZSE": {
            **v1_coverage["venues"]["SZSE"],
            "search_families": szse_coverage,
            "event_rows_preserved_v2": len(szse_events),
            "event_documents_across_multiple_families": len(
                duplicate_across_families
            ),
            "metadata_url_docpubtime_date_conflicts": len(
                szse_metadata_date_conflicts
            ),
            "title_scope_not_established_count": len(
                szse_title_scope_not_established
            ),
            "first_publication_semantics_complete": False,
            "status_interval_archive_complete": False,
        },
        "BSE": {
            **bse_summary,
            "previous_blocked_endpoint_retried": False,
            "previous_waf_business_rows": 0,
        },
        "reused_v1": {
            "declared_files": reused["declared_files"],
            "passed": reused["passed"],
            "failed": reused["failed"],
        },
        "three_venue_any_identity_business_evidence_observed": (
            bse_summary.get("rows", 0) > 0
        ),
        "three_venue_current_master_observed": False,
        "complete_current_and_delisted_a_share_universe": False,
        "complete_current_delisted_relisted_history": False,
        "complete_year_venue_board_matrix": False,
    }
    gate = compute_gate_recomputation(
        bse_summary=bse_summary,
        sse_relist_count=len(sse_relists),
        szse_coverage=szse_coverage,
        szse_event_count=len(szse_events),
    )

    gaps = read_json(report / "ANOMALIES_AND_GAPS.json")
    for gap in gaps["open_gaps"]:
        if gap["gap_id"] == "CORE01-G4-BSE-CURRENT-DELISTED-RELISTED":
            gap["description"] = (
                "The accessible official BSE code table is not a current "
                "security master and does not contain complete delisted or "
                "relisted histories. Its listing-date field is not a uniform "
                "BJSE listing date because the official footnote assigns the "
                "former Selected Layer date to transferred companies. The old "
                "WAF endpoint was not retried."
            )
    gaps["anomalies"].update(
        {
            "sse_relisting_capture_gaps": sse_relist_gaps,
            "sse_delisting_capture_gaps": sse_delist_gaps,
            "szse_documents_in_multiple_query_families": (
                duplicate_across_families
            ),
            "szse_total_size_drift_families": [
                row["family"]
                for row in szse_coverage
                if not row["total_stable_across_pages"]
            ],
            "szse_max_pages_limit_reached_families": [
                row["family"]
                for row in szse_coverage
                if row["stop_reason"] == "MAX_PAGES_LIMIT_REACHED"
            ],
            "szse_metadata_url_docpubtime_date_conflicts": (
                szse_metadata_date_conflicts
            ),
            "szse_title_scope_not_established": (
                szse_title_scope_not_established
            ),
            "bse_old_code_duplicate_count": bse_summary[
                "old_code_duplicate_count"
            ],
            "bse_new_code_duplicate_count": bse_summary[
                "new_code_duplicate_count"
            ],
            "bse_table_listing_dates_unparsed_count": bse_summary[
                "table_listing_dates_unparsed_count"
            ],
            "bse_selected_layer_transfer_footnote_observed": bse_summary[
                "selected_layer_transfer_footnote_observed"
            ],
        }
    )

    rights = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "overall_status": "FAIL__FORMAL_SOURCE_RIGHTS_NOT_CLOSED",
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
        "network_policy": {
            "official_first_party_only": True,
            "https_only": True,
            "allowed_hosts": sorted(ALLOWED_HOSTS),
            "global_request_cap": GLOBAL_NETWORK_REQUEST_CAP,
            "serial_requests": True,
            "redirects_followed": False,
            "endpoint_authentication_configured": False,
            "endpoint_authorization_headers_sent": request_summary[
                "endpoint_authorization_headers_sent"
            ],
            "endpoint_authorization_observation_complete": request_summary[
                "endpoint_authorization_observation_complete"
            ],
            "transport_proxy_authentication_fully_observable": False,
            "cookies_sent": False,
            "paid_sources_used": False,
            "blocked_bse_endpoint_retried": False,
        },
        "decision": (
            "New responses remain in the inspection package. Public reachability "
            "does not grant formal automated-ingest, internal-sharing or "
            "redistribution rights. The initial collector did not capture the "
            "outgoing Authorization header explicitly, so this repair does not "
            "make an absolute transport-credential-absence claim."
        ),
    }

    atomic_write_json(report / "GATE_RECOMPUTATION.json", gate)
    atomic_write_json(report / "OFFICIAL_COVERAGE_MATRIX.json", coverage)
    atomic_write_json(report / "ANOMALIES_AND_GAPS.json", gaps)
    atomic_write_json(report / "RIGHTS_AND_BOUNDARIES.json", rights)

    repair_receipt = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "repair_id": QA_REPAIR_ID,
        "repaired_at_asia_shanghai": now_shanghai(),
        "trigger": "independent read-only evidence-integrity review",
        "network_requests_during_repair": 0,
        "raw_files_modified": 0,
        "formal_authority_files_modified": 0,
        "formal_snapshot_or_published_files_modified": 0,
        "deletions": 0,
        "pre_repair_candidate_files": candidate_preserved,
        "pre_repair_report_files": report_preserved,
        "changes": [
            "Parsed all YYYY/M/D BSE table dates while retaining the official former-Selected-Layer footnote.",
            "Renamed page dates as candidates and kept first-publication semantics false.",
            "Added the bounded SSE 688287 delisting episode candidate.",
            "Removed SSE relisting fallback and required exact table/page dates.",
            "Kept the Q2 overlap/gap audit UNRESOLVED because no permanent-ID audit ran.",
            "Recorded SZSE metadata-date conflicts and the explicit LISTING page cap.",
            "Added live guard, strict manifest and executed source/test hash bindings.",
            "Downgraded the initial endpoint-authorization observation to incomplete instead of asserting absence.",
        ],
        "candidate_manifest_after_repair": file_binding(
            candidate / "CANDIDATE_MANIFEST.json",
            workspace=workspace,
        ),
        "formal_state_guard_before_repair": protected_pre,
    }
    atomic_write_json(report / "QA_REPAIR_RECEIPT.json", repair_receipt)

    source_bindings = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "executed_generator_source": file_binding(
            workspace
            / "control/tools/build_core01_official_identity_closure_v2.py",
            workspace=workspace,
        ),
        "executed_test_source": file_binding(
            workspace
            / "control/tests/test_build_core01_official_identity_closure_v2.py",
            workspace=workspace,
        ),
        "candidate_manifest": file_binding(
            candidate / "CANDIDATE_MANIFEST.json",
            workspace=workspace,
        ),
        "request_receipts": file_binding(
            candidate / "REQUEST_RECEIPTS.json",
            workspace=workspace,
        ),
        "raw_manifest": file_binding(
            candidate / "RAW_FILE_MANIFEST.json",
            workspace=workspace,
        ),
        "qa_repair_receipt": file_binding(
            report / "QA_REPAIR_RECEIPT.json",
            workspace=workspace,
        ),
        "reused_v1_evidence": {
            "verification": file_binding(
                workspace / V1_REPORT / "VERIFICATION.json",
                workspace=workspace,
            ),
            "report_manifest": file_binding(
                workspace / V1_REPORT / "REPORT_MANIFEST.json",
                workspace=workspace,
            ),
            "source_files_verified": reused["passed"],
        },
        "core01_contract": contract,
        "new_request_summary": request_summary,
    }
    atomic_write_json(report / "SOURCE_BINDINGS.json", source_bindings)

    test_results = run_unit_tests(workspace)
    atomic_write_json(report / "TEST_RESULTS.json", test_results)
    if test_results["status"] != "PASS":
        raise RuntimeError("CORE-01 v2 tests failed during repair")

    protected_post = guard_state(workspace)
    if protected_pre != protected_post:
        raise RuntimeError("protected formal state changed during repair")
    report_verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "verified_at_asia_shanghai": now_shanghai(),
        "status": (
            "PASS__EVIDENCE_HASH_BOUND__FORMAL_STATE_UNCHANGED__"
            "STAGE_A_BLOCKED__INITIAL_AUTH_OBSERVATION_INCOMPLETE"
        ),
        "purpose_ceiling": PURPOSE_CEILING,
        "quality_status": QUALITY_STATUS,
        "formal_admission": False,
        "source_checks": {
            "reused_v1_files_declared": reused["declared_files"],
            "reused_v1_files_passed": reused["passed"],
            "reused_v1_files_failed": reused["failed"],
            "new_raw_manifest_errors": raw_errors,
            "new_receipt_hash_errors": receipt_hash_errors,
            "candidate_manifest_errors": candidate_manifest_errors,
            "new_network_request_count": len(receipts),
            "network_request_cap": GLOBAL_NETWORK_REQUEST_CAP,
            "executed_generator_source_bound": True,
            "executed_test_source_bound": True,
        },
        "semantic_checks": {
            "strict_block_gates_passed": 0,
            "stage_a_ready": False,
            "stage_a_blocking_reasons": 11,
            "formal_status_transition": False,
            "identity_candidates_are_not_formal_intervals": True,
            "permanent_identity_overlap_gap_audit_executed": False,
            "bse_selected_layer_date_footnote_preserved": bse_summary[
                "selected_layer_transfer_footnote_observed"
            ],
            "bse_table_listing_dates_unparsed": bse_summary[
                "table_listing_dates_unparsed_count"
            ],
            "docpubtime_not_promoted_to_first_publication": True,
            "szse_metadata_date_conflicts_preserved": len(
                szse_metadata_date_conflicts
            ),
            "available_at_promotions": 0,
            "silent_identity_merges": 0,
            "unknowns_filled_with_zero": 0,
        },
        "boundary_guards": {
            "catalog_writes": 0,
            "source_registry_mutations": 0,
            "five_authority_mutations": 0,
            "quality_promotions": 0,
            "formal_snapshots": 0,
            "research_runs": 0,
            "paid_sources": 0,
            "new_keys_or_credentials": 0,
            "endpoint_authorization_observation_complete": request_summary[
                "endpoint_authorization_observation_complete"
            ],
            "endpoint_authorization_headers_sent": request_summary[
                "endpoint_authorization_headers_sent"
            ],
            "transport_proxy_authentication_fully_observable": False,
            "csmar_requests": 0,
            "deletions": 0,
        },
        "formal_state_guard": {
            "pre": protected_pre,
            "post": protected_post,
            "unchanged": True,
        },
        "qa_repair": {
            "repair_id": QA_REPAIR_ID,
            "network_requests_during_repair": 0,
            "raw_files_modified": 0,
            "pre_repair_bytes_preserved": True,
        },
        "test_results": {
            "status": test_results["status"],
            "tests_run": test_results["tests_run"],
            "exit_code": test_results["exit_code"],
        },
    }
    atomic_write_json(report / "VERIFICATION.json", report_verification)
    atomic_write_bytes(
        report / "README.md",
        render_readme(
            gate, coverage, gaps, request_summary, test_results
        ).encode("utf-8"),
    )

    report_files = [
        path
        for path in report.rglob("*")
        if path.is_file() and path != report / "REPORT_MANIFEST.json"
    ]
    report_manifest = manifest_for(
        report_files,
        logical_root=DEFAULT_REPORT,
        actual_root=report,
        artifact_role="core01_v2_human_audit_report",
    )
    report_manifest["formal_state"] = {
        "core_id": "CORE-01",
        "stage_a_ready": False,
        "stage_a_blocking_reasons": 11,
        "formal_admission": False,
        "research_ready": False,
        "status_transition": False,
    }
    report_manifest["qa_repair"] = {
        "repair_id": QA_REPAIR_ID,
        "raw_files_modified": 0,
        "deletions": 0,
    }
    report_manifest["self_excluded_from_files"] = "REPORT_MANIFEST.json"
    atomic_write_json(report / "REPORT_MANIFEST.json", report_manifest)
    report_manifest_errors = verify_manifest(
        report_manifest,
        logical_root=DEFAULT_REPORT,
        actual_root=report,
    )
    if report_manifest_errors:
        raise RuntimeError(
            "repaired report manifest failed: "
            + ", ".join(report_manifest_errors)
        )

    print(
        json.dumps(
            {
                "run_id": RUN_ID,
                "repair_id": QA_REPAIR_ID,
                "candidate_manifest_errors": candidate_manifest_errors,
                "report_manifest_errors": report_manifest_errors,
                "raw_files_modified": 0,
                "network_requests_during_repair": 0,
                "bse_interval_candidates": len(bse_intervals),
                "bse_table_listing_dates_unparsed": bse_summary[
                    "table_listing_dates_unparsed_count"
                ],
                "sse_relisted_episode_candidates": len(sse_relists),
                "sse_delisted_episode_candidates": len(sse_delists),
                "szse_metadata_date_conflicts": len(
                    szse_metadata_date_conflicts
                ),
                "stage_a_ready": False,
                "blocking_reasons": 11,
                "formal_state_guard_unchanged": True,
                "tests": test_results["status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def verify_existing(workspace: Path, candidate: Path, report: Path) -> int:
    candidate_manifest = read_json(candidate / "CANDIDATE_MANIFEST.json")
    report_manifest = read_json(report / "REPORT_MANIFEST.json")
    candidate_errors = verify_manifest(
        candidate_manifest,
        logical_root=DEFAULT_CANDIDATE,
        actual_root=candidate,
    )
    report_errors = verify_manifest(
        report_manifest,
        logical_root=DEFAULT_REPORT,
        actual_root=report,
    )
    gate = read_json(report / "GATE_RECOMPUTATION.json")
    verification = read_json(report / "VERIFICATION.json")
    live_guard = guard_state(workspace)
    recorded_guard_pre = verification["formal_state_guard"]["pre"]
    recorded_guard_post = verification["formal_state_guard"]["post"]
    live_guard_matches_recorded = (
        live_guard == recorded_guard_pre == recorded_guard_post
    )
    result = {
        "run_id": RUN_ID,
        "candidate_manifest_errors": candidate_errors,
        "report_manifest_errors": report_errors,
        "stage_a_ready": gate["stage_a"]["ready"],
        "blocking_reasons": gate["stage_a"]["total_blocking_reasons"],
        "formal_state_guard_unchanged": verification["formal_state_guard"][
            "unchanged"
        ],
        "live_formal_state_matches_recorded_guard": (
            live_guard_matches_recorded
        ),
        "status": (
            "PASS"
            if not candidate_errors
            and not report_errors
            and gate["stage_a"]["ready"] is False
            and gate["stage_a"]["total_blocking_reasons"] == 11
            and verification["formal_state_guard"]["unchanged"] is True
            and live_guard_matches_recorded
            else "FAIL"
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--repair-existing", action="store_true")
    args = parser.parse_args()
    if args.verify_only and args.repair_existing:
        parser.error("--verify-only and --repair-existing are mutually exclusive")
    workspace = Path(args.workspace).resolve()
    candidate = (workspace / args.candidate).resolve()
    report = (workspace / args.report).resolve()
    if args.verify_only:
        return verify_existing(workspace, candidate, report)
    if args.repair_existing:
        return repair_existing(workspace, candidate, report)
    return build(workspace, candidate, report)


if __name__ == "__main__":
    raise SystemExit(main())
