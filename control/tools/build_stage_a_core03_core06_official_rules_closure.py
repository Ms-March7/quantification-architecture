#!/usr/bin/env python3
"""Build a fail-closed CORE-03/CORE-06 official-rules evidence package.

This utility is deliberately narrow and admission-neutral:

* it performs one serial unauthenticated GET against an explicit SSE/SZSE/BSE
  allow-list and preserves every response or failure receipt;
* it extracts searchable text only to verify that the preserved official
  document contains the cited rule language;
* it records partial effective-dated trading-rule facts and all known gaps;
* it keeps CORE03-Q2/Q3 UNKNOWN because exchange rules cannot establish CSMAR
  table-specific NULL/zero semantics;
* it does not mutate the catalog, source registry, Roadmap, project registry,
  formal snapshots, published data, or any quality/admission state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup


RUN_ID = "stage-a-core03-core06-official-rules-closure-20260730-v1"
PURPOSE_CEILING = "INSPECTION_ONLY"
QUALITY_STATUS = "NOT_ADMITTED"
DEFAULT_CANDIDATE = (
    "tmp/d2a04b-source-closure/"
    "stage-a-core03-core06-official-rules-closure-20260730-v1"
)
DEFAULT_REPORT = (
    "reports/data-governance/"
    "stage-a-core03-core06-official-rules-closure-20260730-v1"
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
    "star.sse.com.cn",
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
REPORT_FILES = {
    "EFFECTIVE_DATE_MATRIX.json",
    "GATE_RECEIPT.json",
    "HISTORICAL_GAPS.json",
    "OFFICIAL_RULE_COVERAGE.json",
    "README.md",
    "REPORT_MANIFEST.json",
    "SOURCE_BINDINGS.json",
    "VERIFICATION.json",
}
MARKER_NORMALIZATION_CONTRACT = {
    "contract_id": "MARKER_NORMALIZATION_V1",
    "canonical_expression": (
        "remove_all_whitespace_then_fullwidth_percent_to_ascii"
    ),
    "operations": [
        {
            "order": 1,
            "operation": "REMOVE_UNICODE_WHITESPACE",
            "regex": r"\s+",
            "replacement": "",
        },
        {
            "order": 2,
            "operation": "FULLWIDTH_PERCENT_TO_ASCII",
            "from_codepoint": "U+FF05",
            "to_codepoint": "U+0025",
        },
    ],
    "case_sensitive": True,
    "other_substitutions": False,
}


SOURCES: tuple[dict[str, Any], ...] = (
    {
        "source_id": "SSE-TRADE-RULE-2006-REPEALED",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/"
            "rules/c/c_20230418_5720136.shtml"
        ),
        "path": "raw/sse/sse_trade_rule_2006_repealed.html",
        "kind": "html",
        "max_bytes": 3 * 1024 * 1024,
        "markers": ("涨跌幅比例为10%", "四舍五入", "首个交易日无价格涨跌幅限制"),
    },
    {
        "source_id": "SSE-TRADE-RULE-2014-AMENDMENT",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/"
            "universal/c/c_20250612_10781699.shtml"
        ),
        "path": "raw/sse/sse_trade_rule_2014_amendment.html",
        "kind": "html",
        "max_bytes": 3 * 1024 * 1024,
        "markers": ("上海证券交易所交易规则", "2014"),
    },
    {
        "source_id": "SSE-TRADE-RULE-2018-AMENDMENT",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/"
            "rules/c/c_20180806_10784917.shtml"
        ),
        "path": "raw/sse/sse_trade_rule_2018_amendment.html",
        "kind": "html",
        "max_bytes": 3 * 1024 * 1024,
        "markers": ("上海证券交易所交易规则", "2018"),
    },
    {
        "source_id": "SSE-TRADE-RULE-2020-AMENDMENT",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/"
            "rules/c/c_20200313_10785127.shtml"
        ),
        "path": "raw/sse/sse_trade_rule_2020_amendment.html",
        "kind": "html",
        "max_bytes": 3 * 1024 * 1024,
        "markers": ("上海证券交易所交易规则", "2020"),
    },
    {
        "source_id": "SSE-TRADE-RULE-2023-DOC",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/"
            "rules/c/10824490/files/dcbe58edb194451d93f19b1f7dd8fb4c.docx"
        ),
        "path": "raw/sse/sse_trade_rule_2023.docx",
        "kind": "docx",
        "max_bytes": 8 * 1024 * 1024,
        "markers": ("上海证券交易所交易规则", "涨跌幅限制", "最小变动单位"),
    },
    {
        "source_id": "SSE-TRADE-RULE-2026-DOC",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/"
            "exchange/c/10816482/files/959da0158c65434daa8a43a6e32be7ba.docx"
        ),
        "path": "raw/sse/sse_trade_rule_2026.docx",
        "kind": "docx",
        "max_bytes": 8 * 1024 * 1024,
        "markers": ("2026年7月6日起施行", "涨跌幅限制", "最小变动单位"),
    },
    {
        "source_id": "SSE-STAR-SPECIAL-RULE-2019",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/"
            "rules/c/10118601/files/f6fc4a1d4c1f469183a013c4dc36a535.pdf"
        ),
        "path": "raw/sse/sse_star_special_rule_2019.pdf",
        "kind": "pdf",
        "max_bytes": 8 * 1024 * 1024,
        "markers": ("涨跌幅比例为20%", "上市后的前5个交易日不设价格涨跌幅限制"),
    },
    {
        "source_id": "SSE-RELIST-RULE-2013",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/"
            "rules/c/c_20230421_5720457.shtml"
        ),
        "path": "raw/sse/sse_relist_rule_2013.html",
        "kind": "html",
        "max_bytes": 3 * 1024 * 1024,
        "markers": ("重新上市首日股票交易不设涨跌幅限制",),
    },
    {
        "source_id": "SSE-RELIST-RULE-2020-NOTICE",
        "venue": "SSE",
        "url": (
            "https://www.sse.com.cn/aboutus/mediacenter/hotandd/"
            "c/c_20200529_5114434.shtml"
        ),
        "path": "raw/sse/sse_relist_rule_2020_notice.html",
        "kind": "html",
        "max_bytes": 3 * 1024 * 1024,
        "markers": ("重新上市", "上市首日无价格涨跌幅限制"),
    },
    {
        "source_id": "SZSE-TRADE-RULE-2011-DOC",
        "venue": "SZSE",
        "url": (
            "https://www.szse.cn/lawrules/rule/repeal/rules/"
            "P020231230545287939887.pdf"
        ),
        "path": "raw/szse/szse_trade_rule_2011.pdf",
        "kind": "pdf",
        "max_bytes": 12 * 1024 * 1024,
        "markers": ("A股交易的申报价格最小变动单位为0.01元人民币", "四舍五入"),
    },
    {
        "source_id": "SZSE-TRADE-RULE-2020-12-DOC",
        "venue": "SZSE",
        "url": (
            "https://www.szse.cn/lawrules/rule/repeal/rules/"
            "P020231230545293899442.pdf"
        ),
        "path": "raw/szse/szse_trade_rule_2020_12.pdf",
        "kind": "pdf",
        "max_bytes": 16 * 1024 * 1024,
        "markers": ("涨跌幅限制比例为10%", "价格最小变动单位", "四舍五入"),
    },
    {
        "source_id": "SZSE-CHINEXT-SPECIAL-RULE-2020",
        "venue": "SZSE",
        "url": (
            "https://www.szse.cn/lawrules/rule/repeal/rules/"
            "P020231230545310237980.pdf"
        ),
        "path": "raw/szse/szse_chinext_special_rule_2020.pdf",
        "kind": "pdf",
        "max_bytes": 12 * 1024 * 1024,
        "markers": ("涨跌幅限制比例为20", "上市后的前五个交易日不设价格涨跌幅限制"),
    },
    {
        "source_id": "SZSE-CHINEXT-EFFECTIVE-2020-08-24",
        "venue": "SZSE",
        "url": "https://www.szse.cn/aboutus/trends/news/t20200821_580924.html",
        "path": "raw/szse/szse_chinext_effective_2020_08_24.html",
        "kind": "html",
        "max_bytes": 3 * 1024 * 1024,
        "markers": ("8月24日调整为20%", "上市首五日不设涨跌幅限制"),
    },
    {
        "source_id": "SZSE-TRADE-RULE-2023-DOC-STALE-ENDPOINT",
        "venue": "SZSE",
        "url": (
            "https://docs.static.szse.cn/www/lawrules/rule/trade/"
            "W020230217564423808793.pdf"
        ),
        "path": "raw/szse/szse_trade_rule_2023.pdf",
        "kind": "pdf",
        "max_bytes": 16 * 1024 * 1024,
        "markers": ("深圳证券交易所交易规则",),
        "expected_failure": True,
    },
    {
        "source_id": "SZSE-REGISTRATION-EFFECTIVE-2023-04-10",
        "venue": "SZSE",
        "url": (
            "https://www.szse.cn/www/marketServices/technicalservice/doc/"
            "P020230724535729507237.pdf"
        ),
        "path": "raw/szse/szse_registration_effective_2023.pdf",
        "kind": "pdf",
        "max_bytes": 12 * 1024 * 1024,
        "markers": ("2023年4月10日", "同日正式实施"),
    },
    {
        "source_id": "SZSE-MAINBOARD-IPO-FAQ-2023",
        "venue": "SZSE",
        "url": "https://investor.szse.cn/knowledge/stock/deal/t20230525_600599.html",
        "path": "raw/szse/szse_mainboard_ipo_faq_2023.html",
        "kind": "html",
        "max_bytes": 3 * 1024 * 1024,
        "markers": ("前五个交易日不实行价格涨跌幅限制",),
    },
    {
        "source_id": "SZSE-TRADE-RULE-2026-DOC",
        "venue": "SZSE",
        "url": (
            "https://docs.static.szse.cn/www/lawrules/rule/trade/current/"
            "W020260424690713155663.pdf"
        ),
        "path": "raw/szse/szse_trade_rule_2026.pdf",
        "kind": "pdf",
        "max_bytes": 16 * 1024 * 1024,
        "markers": ("2026年7月6日起施行", "涨跌幅限制", "最小变动单位"),
    },
    {
        "source_id": "BSE-TRADE-RULE-2021",
        "venue": "BSE",
        "url": "https://www.bse.cn/jygl_list/200010919.html",
        "path": "raw/bse/bse_trade_rule_2021.html",
        "kind": "html",
        "max_bytes": 3 * 1024 * 1024,
        "markers": ("涨跌幅限制比例为30%", "申报价格最小变动单位为0.01元人民币", "退市整理期首日"),
    },
    {
        "source_id": "BSE-TRADE-RULE-2026",
        "venue": "BSE",
        "url": "https://www.bse.cn/jygl_list/200028217.html",
        "path": "raw/bse/bse_trade_rule_2026.html",
        "kind": "html",
        "max_bytes": 3 * 1024 * 1024,
        "markers": ("2026年7月6日起施行", "涨跌幅限制比例为30%", "退市整理期首日"),
    },
)


def now_shanghai() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def canonical_bytes(value: Any) -> bytes:
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
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def workspace_relative(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def file_binding(path: Path, workspace: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise RuntimeError(f"file changed while hashing: {path}")
    return {
        "path": workspace_relative(path, workspace),
        "bytes": after.st_size,
        "sha256": digest,
    }


def is_reparse(path: Path) -> bool:
    attrs = getattr(path.lstat(), "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attrs & flag)


def tree_inventory(root: Path, workspace: Path) -> dict[str, Any]:
    if not root.exists():
        return {
            "path": workspace_relative(root, workspace),
            "exists": False,
            "files": [],
            "file_count": 0,
            "total_bytes": 0,
            "tree_sha256": sha256_bytes(b"[]"),
        }
    if is_reparse(root) or not root.is_dir():
        raise RuntimeError(f"guarded tree is not a plain directory: {root}")
    files: list[dict[str, Any]] = []
    for item in sorted(root.rglob("*")):
        if is_reparse(item):
            raise RuntimeError(f"guarded tree contains a link/reparse point: {item}")
        if item.is_file():
            if len(files) >= 10_000:
                raise RuntimeError(f"guarded tree exceeds 10,000 files: {root}")
            files.append(file_binding(item, workspace))
    if sum(item["bytes"] for item in files) > 64 * 1024 * 1024:
        raise RuntimeError(f"guarded tree exceeds 64 MiB hash budget: {root}")
    return {
        "path": workspace_relative(root, workspace),
        "exists": True,
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "tree_sha256": sha256_bytes(canonical_bytes(files)),
    }


def guard_state(workspace: Path) -> dict[str, Any]:
    return {
        "files": {
            relative: file_binding(workspace / relative, workspace)
            for relative in GUARDED_FILES
        },
        "trees": {
            relative: tree_inventory(workspace / relative, workspace)
            for relative in GUARDED_TREES
        },
    }


def require_child(path: Path, parent: Path, label: str) -> None:
    resolved = path.resolve()
    parent_resolved = parent.resolve()
    if Path(os.path.commonpath([resolved, parent_resolved])) != parent_resolved:
        raise ValueError(f"{label} escapes {parent_resolved}: {resolved}")
    if resolved == parent_resolved:
        raise ValueError(f"{label} must be below {parent_resolved}")


def require_new_dir(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.mkdir(parents=True, exist_ok=False)


def safe_headers(headers: requests.structures.CaseInsensitiveDict) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in SAFE_RESPONSE_HEADERS
    }


def content_valid(kind: str, body: bytes, content_type: str) -> bool:
    if kind == "pdf":
        return body.startswith(b"%PDF")
    if kind == "docx":
        if not body.startswith(b"PK"):
            return False
        try:
            from io import BytesIO

            with zipfile.ZipFile(BytesIO(body)) as archive:
                return (
                    "[Content_Types].xml" in archive.namelist()
                    and "word/document.xml" in archive.namelist()
                )
        except zipfile.BadZipFile:
            return False
    if kind == "html":
        prefix = body[:4096].lower()
        return (
            "text/html" in content_type.lower()
            or b"<html" in prefix
            or b"<!doctype html" in prefix
        )
    raise ValueError(f"unsupported kind: {kind}")


def fetch_one(
    session: requests.Session,
    spec: dict[str, Any],
    candidate: Path,
    sequence: int,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "sequence": sequence,
        "source_id": spec["source_id"],
        "venue": spec["venue"],
        "method": "GET",
        "request_url": spec["url"],
        "expected_kind": spec["kind"],
        "max_bytes": spec["max_bytes"],
        "started_at_asia_shanghai": now_shanghai(),
        "attempts": 1,
        "retry_count": 0,
        "serial": True,
        "allow_redirects": False,
        "authentication_used": False,
        "cookies_used": False,
        "formal_admission": False,
        "purpose_ceiling": PURPOSE_CEILING,
        "expected_failure": bool(spec.get("expected_failure")),
    }
    host = urlparse(spec["url"]).hostname
    if host not in ALLOWED_HOSTS:
        receipt.update(
            result="REJECTED_BEFORE_REQUEST",
            reason="host outside official allow-list",
            completed_at_asia_shanghai=now_shanghai(),
        )
        return receipt
    try:
        response = session.get(
            spec["url"],
            timeout=(10, 40),
            allow_redirects=False,
            stream=True,
        )
        chunks: list[bytes] = []
        total = 0
        too_large = False
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > spec["max_bytes"]:
                too_large = True
                break
            chunks.append(chunk)
        body = b"".join(chunks)
        target = candidate / spec["path"]
        if response.status_code != 200:
            target = candidate / "raw/failed" / f"{spec['source_id']}.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        receipt.update(
            http_status=response.status_code,
            response_headers=safe_headers(response.headers),
            path=target.relative_to(candidate).as_posix(),
            bytes=len(body),
            sha256=sha256_bytes(body),
        )
        if too_large:
            receipt.update(
                result="REJECTED_RESPONSE",
                reason="bounded response exceeded max_bytes; prefix preserved",
            )
        elif response.status_code != 200:
            receipt.update(
                result="HTTP_FAILURE_BODY_PRESERVED",
                reason=f"official endpoint returned HTTP {response.status_code}",
            )
        elif not content_valid(
            spec["kind"],
            body,
            response.headers.get("content-type", ""),
        ):
            receipt.update(
                result="CONTENT_MISMATCH_BODY_PRESERVED",
                reason=f"HTTP 200 body failed {spec['kind']} validation",
            )
        else:
            receipt["result"] = "RESPONSE_PRESERVED"
    except requests.RequestException as exc:
        receipt.update(
            result="NETWORK_FAILURE",
            reason=f"{type(exc).__name__}: {exc}",
        )
    finally:
        receipt["completed_at_asia_shanghai"] = now_shanghai()
    return receipt


def extract_html(path: Path) -> str:
    return BeautifulSoup(path.read_bytes(), "html.parser").get_text(
        "\n", strip=True
    )


def extract_docx(path: Path) -> str:
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(
            node.text or "" for node in paragraph.iter(f"{namespace}t")
        )
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_pdf(path: Path, target: Path) -> tuple[str | None, str | None]:
    executable = shutil.which("pdftotext")
    if not executable:
        return None, "pdftotext not found"
    completed = subprocess.run(
        [executable, "-layout", str(path), str(target)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or not target.is_file():
        return None, (
            f"pdftotext exit={completed.returncode}: "
            f"{completed.stderr.strip()[:500]}"
        )
    return target.read_text(encoding="utf-8", errors="replace"), None


def normalize_marker(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("％", "%")


def marker_evidence(
    text: str,
    markers: Iterable[str],
) -> tuple[dict[str, bool], list[dict[str, Any]], str]:
    normalized_text = normalize_marker(text)
    checks: dict[str, bool] = {}
    evidence: list[dict[str, Any]] = []
    for marker in markers:
        normalized_marker = normalize_marker(marker)
        normalized_match_start = normalized_text.find(normalized_marker)
        matched = normalized_match_start >= 0
        checks[marker] = matched
        evidence.append(
            {
                "marker": marker,
                "normalized_marker": normalized_marker,
                "raw_exact_match": marker in text,
                "normalized_match": matched,
                "normalized_match_start": normalized_match_start,
            }
        )
    normalized_sha256 = hashlib.sha256(
        normalized_text.encode("utf-8")
    ).hexdigest()
    return checks, evidence, normalized_sha256


def extraction_entries(value: Any) -> list[dict[str, Any]]:
    """Read both the original list and self-describing v2 index."""

    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("entries"), list):
        return value["entries"]
    raise TypeError("EXTRACTED_TEXT_INDEX has an unsupported shape")


def independent_marker_recompute(
    candidate: Path,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Re-open persisted text and independently recompute every marker."""

    by_id = {row["source_id"]: row for row in entries}
    results: list[dict[str, Any]] = []
    for spec in SOURCES:
        row = by_id[spec["source_id"]]
        if "text_path" not in row:
            continue
        text_path = candidate / row["text_path"]
        text = text_path.read_text(encoding="utf-8", errors="strict")
        # Keep this implementation explicit rather than trusting serialized
        # marker booleans from the extraction pass.
        normalized_text = re.sub(r"\s+", "", text).replace("％", "%")
        for marker in spec["markers"]:
            normalized_marker = re.sub(r"\s+", "", marker).replace("％", "%")
            normalized_match_start = normalized_text.find(normalized_marker)
            results.append(
                {
                    "source_id": spec["source_id"],
                    "text_path": row["text_path"],
                    "text_bytes": row["text_bytes"],
                    "text_sha256": row["text_sha256"],
                    "marker": marker,
                    "normalized_marker": normalized_marker,
                    "raw_exact_match": marker in text,
                    "normalized_match": normalized_match_start >= 0,
                    "normalized_match_start": normalized_match_start,
                }
            )
    passed = sum(item["normalized_match"] for item in results)
    raw_exact_false_normalized_true = sum(
        not item["raw_exact_match"] and item["normalized_match"]
        for item in results
    )
    return {
        "method": (
            "REOPEN_PERSISTED_EXTRACTED_TEXT_AND_RECOMPUTE_WITH_DECLARED_CONTRACT"
        ),
        "normalization_contract_id": MARKER_NORMALIZATION_CONTRACT[
            "contract_id"
        ],
        "marker_count": len(results),
        "matched_count": passed,
        "all_matched": passed == len(results),
        "raw_exact_false_normalized_true_count": (
            raw_exact_false_normalized_true
        ),
        "results": results,
    }


def extraction_index(
    candidate: Path,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "normalization_contract": MARKER_NORMALIZATION_CONTRACT,
        "independent_recompute": independent_marker_recompute(
            candidate, entries
        ),
        "entries": entries,
    }


def extract_successes(
    candidate: Path,
    receipts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {item["source_id"]: item for item in SOURCES}
    rows: list[dict[str, Any]] = []
    for receipt in receipts:
        if receipt["result"] != "RESPONSE_PRESERVED":
            rows.append(
                {
                    "source_id": receipt["source_id"],
                    "status": "NOT_EXTRACTED",
                    "reason": receipt["result"],
                }
            )
            continue
        spec = by_id[receipt["source_id"]]
        source = candidate / receipt["path"]
        target = candidate / "extracted" / f"{spec['source_id']}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        error: str | None = None
        if spec["kind"] == "html":
            text = extract_html(source)
            target.write_text(text + "\n", encoding="utf-8", newline="\n")
        elif spec["kind"] == "docx":
            text = extract_docx(source)
            target.write_text(text + "\n", encoding="utf-8", newline="\n")
        else:
            text, error = extract_pdf(source, target)
        if error or text is None:
            rows.append(
                {
                    "source_id": spec["source_id"],
                    "status": "EXTRACTION_FAILED",
                    "reason": error,
                }
            )
            continue
        marker_checks, normalized_evidence, normalized_text_sha256 = (
            marker_evidence(text, spec["markers"])
        )
        rows.append(
            {
                "source_id": spec["source_id"],
                "status": (
                    "PASS" if all(marker_checks.values()) else "MARKER_MISMATCH"
                ),
                "text_path": target.relative_to(candidate).as_posix(),
                "text_bytes": target.stat().st_size,
                "text_sha256": sha256_file(target),
                "normalization_contract_id": MARKER_NORMALIZATION_CONTRACT[
                    "contract_id"
                ],
                "normalized_text_sha256": normalized_text_sha256,
                "marker_checks": marker_checks,
                "marker_evidence": normalized_evidence,
            }
        )
    return rows


def official_rule_coverage() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "scope": "CORE06-Q3/Q4 official rule evidence only",
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
        "facts": [
            {
                "fact_id": "SSE-MAINBOARD-HISTORICAL-10-5-ROUND",
                "venue": "SSE",
                "board": "MAINBOARD",
                "effective_interval_claim": "2006-07-01 onward only to the extent of preserved versions",
                "claim": (
                    "The preserved 2006 rule states 10% for stocks, 5% for "
                    "ST/*ST, A-share tick 0.01 RMB, and round-to-tick price limits."
                ),
                "source_ids": ["SSE-TRADE-RULE-2006-REPEALED"],
                "closure_effect": "PARTIAL_OFFICIAL_FACT_ONLY",
            },
            {
                "fact_id": "SSE-STAR-20-IPO5",
                "venue": "SSE",
                "board": "STAR",
                "effective_interval_claim": "2019 rule version; exact full amendment chain not claimed",
                "claim": (
                    "The preserved STAR special rule states a 20% limit and "
                    "no price limit for the first five trading days after IPO."
                ),
                "source_ids": ["SSE-STAR-SPECIAL-RULE-2019"],
                "closure_effect": "PARTIAL_OFFICIAL_FACT_ONLY",
            },
            {
                "fact_id": "SSE-RELIST-FIRST-DAY-NO-LIMIT",
                "venue": "SSE",
                "board": "MAINBOARD",
                "effective_interval_claim": "2013 and 2020 preserved versions",
                "claim": "Relisting first day has no price limit in the preserved rules.",
                "source_ids": [
                    "SSE-RELIST-RULE-2013",
                    "SSE-RELIST-RULE-2020-NOTICE",
                ],
                "closure_effect": "PARTIAL_OFFICIAL_FACT_ONLY",
            },
            {
                "fact_id": "SSE-2023-2026-RULE-BOUNDARY",
                "venue": "SSE",
                "board": "ALL_STOCK_BOARDS",
                "effective_interval_claim": "2023 rule replaced by 2026 rule on 2026-07-06",
                "claim": (
                    "The 2023 and 2026 official rule files are preserved; the "
                    "2026 rule states it takes effect on 2026-07-06."
                ),
                "source_ids": [
                    "SSE-TRADE-RULE-2023-DOC",
                    "SSE-TRADE-RULE-2026-DOC",
                ],
                "closure_effect": "PARTIAL_VERSION_BOUNDARY_ONLY",
            },
            {
                "fact_id": "SSE-2026-CURRENT-LIMIT-TICK-ROUND-NOLIMIT",
                "venue": "SSE",
                "board": "MAINBOARD_AND_STAR",
                "effective_interval_claim": "from 2026-07-06",
                "claim": (
                    "The current rule states A-share tick 0.01 RMB, main rule "
                    "limit 10%, STAR limit 20%, round-to-tick plus one-tick "
                    "minimum, and no limit for IPO first five days, first "
                    "delisting-arrangement day and first relisting day."
                ),
                "source_ids": ["SSE-TRADE-RULE-2026-DOC"],
                "closure_effect": "PASS__CURRENT_VERSION_FACT_ONLY",
            },
            {
                "fact_id": "SZSE-2011-2020-TICK-ROUND",
                "venue": "SZSE",
                "board": "MAINBOARD_AND_PRE_REFORM_CHINEXT",
                "effective_interval_claim": "selected 2011 and 2020-12 versions only",
                "claim": (
                    "Selected preserved rules state A-share tick 0.01 RMB, "
                    "round-to-tick, and a one-tick minimum move when rounding "
                    "would otherwise produce no change."
                ),
                "source_ids": [
                    "SZSE-TRADE-RULE-2011-DOC",
                    "SZSE-TRADE-RULE-2020-12-DOC",
                ],
                "closure_effect": "PARTIAL_OFFICIAL_FACT_ONLY",
            },
            {
                "fact_id": "SZSE-CHINEXT-20-IPO5",
                "venue": "SZSE",
                "board": "CHINEXT",
                "effective_interval_claim": "effective transition confirmed for 2020-08-24",
                "claim": (
                    "The preserved special rule and official transition notice "
                    "state 20% and no price limit for the first five IPO days."
                ),
                "source_ids": [
                    "SZSE-CHINEXT-SPECIAL-RULE-2020",
                    "SZSE-CHINEXT-EFFECTIVE-2020-08-24",
                ],
                "closure_effect": "PARTIAL_OFFICIAL_FACT_ONLY",
            },
            {
                "fact_id": "SZSE-2023-REGISTRATION-EFFECTIVE",
                "venue": "SZSE",
                "board": "MAINBOARD",
                "effective_interval_claim": "2023-04-10 event confirmed; full 2023 rule file not preserved",
                "claim": (
                    "Official technical material confirms the 2023 rule took "
                    "effect on 2023-04-10; the official FAQ confirms five IPO "
                    "days without a price limit."
                ),
                "source_ids": [
                    "SZSE-REGISTRATION-EFFECTIVE-2023-04-10",
                    "SZSE-MAINBOARD-IPO-FAQ-2023",
                ],
                "closure_effect": "PARTIAL_EVENT_AND_FAQ_ONLY",
            },
            {
                "fact_id": "SZSE-2026-RULE-BOUNDARY",
                "venue": "SZSE",
                "board": "ALL_STOCK_BOARDS",
                "effective_interval_claim": "from 2026-07-06",
                "claim": "The preserved 2026 rule states it takes effect on 2026-07-06.",
                "source_ids": ["SZSE-TRADE-RULE-2026-DOC"],
                "closure_effect": "PARTIAL_CURRENT_VERSION_ONLY",
            },
            {
                "fact_id": "SZSE-2026-CURRENT-LIMIT-TICK-ROUND-NOLIMIT",
                "venue": "SZSE",
                "board": "MAINBOARD_AND_CHINEXT",
                "effective_interval_claim": "from 2026-07-06",
                "claim": (
                    "The current rule states A-share tick 0.01 RMB, mainboard "
                    "10%, ChiNext 20%, round-to-tick plus one-tick minimum, "
                    "and no limit for IPO first five days, relisting first day "
                    "and the first day of the delisting arrangement."
                ),
                "source_ids": ["SZSE-TRADE-RULE-2026-DOC"],
                "closure_effect": "PASS__CURRENT_VERSION_FACT_ONLY",
            },
            {
                "fact_id": "BSE-30-TICK-IPO-DELISTING",
                "venue": "BSE",
                "board": "BSE",
                "effective_interval_claim": "2021-11-15 original and 2026-07-06 current versions",
                "claim": (
                    "The preserved BSE rules state a 0.01 RMB tick, 30% limit, "
                    "and no price limit on public-offering listing day and the "
                    "first day of the delisting arrangement."
                ),
                "source_ids": [
                    "BSE-TRADE-RULE-2021",
                    "BSE-TRADE-RULE-2026",
                ],
                "closure_effect": "PARTIAL_OFFICIAL_FACT_ONLY",
            },
        ],
        "non_claims": [
            "No source here proves CSMAR TRD_Dalyr NULL-versus-zero semantics.",
            "No source here proves CSMAR unknown values were never encoded as zero.",
            "No complete historical rule-version chain is claimed for any venue.",
            "No historical security-day rule recomputation or formal snapshot is produced.",
        ],
    }


def effective_date_matrix() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "rows": [
            {
                "venue": "SSE",
                "rule_family": "general trading rule",
                "effective_from": "2006-07-01",
                "effective_to": "UNKNOWN__INTERMEDIATE_REVISIONS_NOT_MATERIALIZED",
                "source_ids": ["SSE-TRADE-RULE-2006-REPEALED"],
                "status": "PARTIAL",
            },
            {
                "venue": "SSE",
                "rule_family": "STAR 20% and IPO first five days no limit",
                "effective_from": "UNKNOWN__2019_RULE_VERSION_ONLY",
                "effective_to": "UNKNOWN__FULL_AMENDMENT_CHAIN_NOT_MATERIALIZED",
                "source_ids": ["SSE-STAR-SPECIAL-RULE-2019"],
                "status": "PARTIAL",
            },
            {
                "venue": "SSE",
                "rule_family": "general trading rule 2023",
                "effective_from": "CONDITIONAL__FIRST_REGISTERED_MAINBOARD_IPO_DAY",
                "effective_to": "2026-07-05",
                "source_ids": ["SSE-TRADE-RULE-2023-DOC"],
                "status": "PARTIAL",
            },
            {
                "venue": "SSE",
                "rule_family": "general trading rule 2026",
                "effective_from": "2026-07-06",
                "effective_to": None,
                "source_ids": ["SSE-TRADE-RULE-2026-DOC"],
                "status": "PASS__VERSION_BOUNDARY_ONLY",
            },
            {
                "venue": "SZSE",
                "rule_family": "selected general trading rules",
                "effective_from": "2011-02-28__SELECTED_VERSION",
                "effective_to": "2020-12-31__SELECTED_VERSION",
                "source_ids": [
                    "SZSE-TRADE-RULE-2011-DOC",
                    "SZSE-TRADE-RULE-2020-12-DOC",
                ],
                "status": "PARTIAL",
            },
            {
                "venue": "SZSE",
                "rule_family": "ChiNext 20% transition",
                "effective_from": "2020-08-24",
                "effective_to": "UNKNOWN__SUPERSESSION_CHAIN_INCOMPLETE",
                "source_ids": [
                    "SZSE-CHINEXT-SPECIAL-RULE-2020",
                    "SZSE-CHINEXT-EFFECTIVE-2020-08-24",
                ],
                "status": "PARTIAL",
            },
            {
                "venue": "SZSE",
                "rule_family": "general trading rule 2023",
                "effective_from": "2023-04-10",
                "effective_to": "2026-07-05",
                "source_ids": [
                    "SZSE-REGISTRATION-EFFECTIVE-2023-04-10",
                    "SZSE-TRADE-RULE-2023-DOC-STALE-ENDPOINT",
                ],
                "status": "FAIL__RULE_TEXT_ENDPOINT_NOT_PRESERVED",
            },
            {
                "venue": "SZSE",
                "rule_family": "general trading rule 2026",
                "effective_from": "2026-07-06",
                "effective_to": None,
                "source_ids": ["SZSE-TRADE-RULE-2026-DOC"],
                "status": "PASS__VERSION_BOUNDARY_ONLY",
            },
            {
                "venue": "BSE",
                "rule_family": "general trading rule trial",
                "effective_from": "2021-11-15",
                "effective_to": "UNKNOWN__INTERMEDIATE_MODIFICATIONS_NOT_MATERIALIZED",
                "source_ids": ["BSE-TRADE-RULE-2021"],
                "status": "PARTIAL__INTERMEDIATE_AMENDMENTS_NOT_ENUMERATED",
            },
            {
                "venue": "BSE",
                "rule_family": "general trading rule 2026",
                "effective_from": "2026-07-06",
                "effective_to": None,
                "source_ids": ["BSE-TRADE-RULE-2026"],
                "status": "PASS__VERSION_BOUNDARY_ONLY",
            },
        ],
        "formal_admission": False,
    }


def historical_gaps() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "gate_effect": "CORE06-Q3/Q4 remain FAIL; CORE03-Q2/Q3 remain UNKNOWN",
        "gaps": [
            {
                "gap_id": "SSE-COMPLETE-VERSION-CHAIN",
                "gate_ids": ["CORE06-Q3", "CORE06-Q4"],
                "status": "FAIL",
                "detail": (
                    "Selected 2006/2014/2018/2020/2023/2026 materials do not "
                    "materialize every revision, repeal, delayed clause, board "
                    "exception, and effective interval."
                ),
            },
            {
                "gap_id": "SZSE-2023-RULE-TEXT-AND-FULL-CHAIN",
                "gate_ids": ["CORE06-Q3", "CORE06-Q4"],
                "status": "FAIL",
                "detail": (
                    "The previously indexed official 2023 rule PDF endpoint "
                    "returns 404 in this run; event and FAQ evidence do not "
                    "replace the full rule text or the complete 2001-2026 chain."
                ),
            },
            {
                "gap_id": "BSE-INTERMEDIATE-AMENDMENTS-AND-ROUNDING",
                "gate_ids": ["CORE06-Q3", "CORE06-Q4"],
                "status": "UNKNOWN",
                "detail": (
                    "The preserved 2021 and 2026 rules do not establish every "
                    "intermediate amendment. The collected stock clauses state "
                    "tick, percentage and formula but do not close a historical "
                    "rounding convention sufficient for exact recomputation."
                ),
            },
            {
                "gap_id": "HISTORICAL-RISK-STATE-INTERVALS",
                "gate_ids": ["CORE06-Q3", "CORE06-Q4"],
                "status": "FAIL",
                "detail": (
                    "No complete effective-dated security-level ST/*ST/risk "
                    "warning/delisting/relisting interval ledger is created."
                ),
            },
            {
                "gap_id": "SPECIAL-NO-LIMIT-EXHAUSTIVENESS",
                "gate_ids": ["CORE06-Q4"],
                "status": "FAIL",
                "detail": (
                    "IPO, relisting/recovery, delisting arrangement and board "
                    "exceptions are only partially sampled; no exhaustive "
                    "first-publication/supersedes/withdrawal chain exists."
                ),
            },
            {
                "gap_id": "CSMAR-TRD-DALYR-NULL-ZERO-SEMANTICS",
                "gate_ids": ["CORE03-Q2", "CORE03-Q3"],
                "status": "UNKNOWN",
                "detail": (
                    "Exchange trading rules cannot prove CSMAR table-version "
                    "encoding of suspension/no-trade NULLs, zeros or unknowns."
                ),
            },
        ],
    }


def gate_receipt() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "target": ["CORE-03", "CORE-06"],
        "decision": "FAIL_CLOSED__NO_STAGE_A_CHANGE__NOT_ADMITTED",
        "gates": {
            "CORE03-Q2": {
                "status": "UNKNOWN",
                "basis": (
                    "Official exchange rules do not establish CSMAR "
                    "TRD_Dalyr NULL/zero/suspension/no-trade encodings."
                ),
            },
            "CORE03-Q3": {
                "status": "UNKNOWN",
                "basis": (
                    "Official exchange rules do not prove that every historical "
                    "unknown vendor value was never encoded as zero."
                ),
            },
            "CORE06-Q3": {
                "status": "FAIL",
                "basis": (
                    "Selected official versions close partial board/tick/limit "
                    "facts, but not the complete historical rule and risk-state "
                    "inputs required to recompute every historical limit."
                ),
            },
            "CORE06-Q4": {
                "status": "FAIL",
                "basis": (
                    "Selected official versions close partial IPO/relisting/"
                    "delisting no-limit facts, but not the exhaustive effective-"
                    "dated publication and revision chain."
                ),
            },
        },
        "stage_a_status_changed": False,
        "formal_admission": False,
        "quality_status": QUALITY_STATUS,
        "purpose_ceiling": PURPOSE_CEILING,
        "research_usable": False,
        "catalog_update": False,
        "source_registry_update": False,
        "roadmap_update": False,
        "projects_update": False,
        "snapshot_published": False,
    }


def report_readme() -> str:
    return f"""# CORE-03 / CORE-06 official trading-rule closure

Run: `{RUN_ID}`

## Decision

This is a bounded, fail-closed Stage-A evidence package. It preserves selected
official SSE, SZSE and BSE rule versions and records partial effective-dated
facts. It does **not** establish a complete historical rule chain.

- `CORE06-Q3 = FAIL`
- `CORE06-Q4 = FAIL`
- `CORE03-Q2 = UNKNOWN`
- `CORE03-Q3 = UNKNOWN`
- formal admission: `false`
- purpose ceiling: `{PURPOSE_CEILING}`
- quality status: `{QUALITY_STATUS}`

Exchange rules can describe market mechanics, but they cannot prove CSMAR
table-specific NULL/zero semantics. Missing historical versions, per-security
risk-state intervals, exact BSE historical rounding evidence and exhaustive
special/no-limit revision chains remain explicit gaps.

No catalog, source-registry, Roadmap, project-registry, formal snapshot or
published-data mutation is authorized or performed.
"""


def manifest_for(
    files: Iterable[Path],
    workspace: Path,
    run_id: str,
    manifest_type: str,
) -> dict[str, Any]:
    entries = [file_binding(path, workspace) for path in sorted(files)]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "manifest_type": manifest_type,
        "file_count": len(entries),
        "total_bytes": sum(item["bytes"] for item in entries),
        "files": entries,
    }


def validate_source_bindings(
    coverage: dict[str, Any],
    receipts: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    known = {item["source_id"] for item in receipts}
    errors: list[str] = []
    for fact in coverage["facts"]:
        for source_id in fact["source_ids"]:
            if source_id not in known:
                errors.append(f"unknown source_id {source_id} in {fact['fact_id']}")
    return not errors, errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--candidate-output", default=DEFAULT_CANDIDATE)
    parser.add_argument("--report-output", default=DEFAULT_REPORT)
    parser.add_argument(
        "--repair-source-bindings",
        action="store_true",
        help=(
            "Repair only canonical workspace-relative raw paths in the "
            "tool-owned report and re-hash verification/manifest."
        ),
    )
    parser.add_argument(
        "--repair-normalization-evidence",
        action="store_true",
        help=(
            "Recompute persisted marker evidence under the declared "
            "normalization contract and rebuild candidate/report manifests."
        ),
    )
    return parser.parse_args()


def repair_source_bindings(
    workspace: Path,
    candidate: Path,
    report: Path,
) -> int:
    """Repair report bindings without changing candidate evidence or gates."""

    if not candidate.is_dir() or not report.is_dir():
        raise FileNotFoundError("canonical candidate/report package is absent")
    pre = guard_state(workspace)
    receipts = read_json(candidate / "REQUEST_RECEIPTS.json")
    extraction = read_json(candidate / "EXTRACTED_TEXT_INDEX.json")
    extraction_by_id = {
        row["source_id"]: row for row in extraction_entries(extraction)
    }
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in receipts:
        relative = item.get("path")
        if not relative:
            errors.append(f"{item['source_id']}: missing receipt path")
            continue
        raw_path = (candidate / relative).resolve()
        try:
            raw_path.relative_to(candidate.resolve())
        except ValueError:
            errors.append(f"{item['source_id']}: path escapes candidate")
            continue
        if not raw_path.is_file():
            errors.append(f"{item['source_id']}: raw path absent")
            continue
        actual_bytes = raw_path.stat().st_size
        actual_sha256 = sha256_file(raw_path)
        raw_verified = (
            actual_bytes == item.get("bytes")
            and actual_sha256 == item.get("sha256")
        )
        if not raw_verified:
            errors.append(f"{item['source_id']}: receipt bytes/hash mismatch")
        rows.append(
            {
                "source_id": item["source_id"],
                "venue": item["venue"],
                "request_url": item["request_url"],
                "result": item["result"],
                "http_status": item.get("http_status"),
                "path": workspace_relative(raw_path, workspace),
                "bytes": actual_bytes,
                "sha256": actual_sha256,
                "receipt_bytes_sha256_verified": raw_verified,
                "extraction_status": extraction_by_id[item["source_id"]][
                    "status"
                ],
            }
        )
    if len(rows) != len(SOURCES):
        errors.append(
            f"binding count {len(rows)} does not match source count {len(SOURCES)}"
        )
    if errors:
        raise RuntimeError("source binding repair failed: " + "; ".join(errors))

    source_bindings = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "path_contract": "CANONICAL_WORKSPACE_RELATIVE",
        "receipt_count": len(receipts),
        "raw_path_verified_count": sum(
            row["receipt_bytes_sha256_verified"] for row in rows
        ),
        "receipts": rows,
        "formal_admission": False,
    }
    write_json(report / "SOURCE_BINDINGS.json", source_bindings)

    post = guard_state(workspace)
    if pre != post:
        raise RuntimeError("formal guard changed during source-binding repair")
    verification = read_json(report / "VERIFICATION.json")
    verification.update(
        {
            "source_binding_path_contract": "CANONICAL_WORKSPACE_RELATIVE",
            "source_binding_count": len(rows),
            "source_binding_bytes_sha256_verified_count": sum(
                row["receipt_bytes_sha256_verified"] for row in rows
            ),
            "source_binding_paths_all_exist": all(
                (workspace / row["path"]).is_file() for row in rows
            ),
            "source_binding_repair_guard_pre": pre,
            "source_binding_repair_guard_post": post,
            "source_binding_repair_guard_unchanged": pre == post,
        }
    )
    write_json(report / "VERIFICATION.json", verification)
    report_files = [
        path
        for path in report.iterdir()
        if path.is_file() and path.name != "REPORT_MANIFEST.json"
    ]
    write_json(
        report / "REPORT_MANIFEST.json",
        manifest_for(report_files, workspace, RUN_ID, "human_audit_report"),
    )

    manifest = read_json(report / "REPORT_MANIFEST.json")
    manifest_errors: list[str] = []
    for binding in manifest["files"]:
        path = workspace / binding["path"]
        if (
            not path.is_file()
            or path.stat().st_size != binding["bytes"]
            or sha256_file(path) != binding["sha256"]
        ):
            manifest_errors.append(binding["path"])
    if manifest_errors:
        raise RuntimeError(
            "report manifest mismatch after repair: "
            + ", ".join(manifest_errors)
        )
    if {item.name for item in report.iterdir() if item.is_file()} != REPORT_FILES:
        raise RuntimeError("report file-set changed during source-binding repair")
    print(
        json.dumps(
            {
                "run_id": RUN_ID,
                "mode": "repair_source_bindings",
                "canonical_workspace_relative_bindings": len(rows),
                "bytes_sha256_verified": sum(
                    row["receipt_bytes_sha256_verified"] for row in rows
                ),
                "report_manifest_files_verified": len(manifest["files"]),
                "formal_guard_unchanged": pre == post,
                "business_conclusion_changed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def verify_manifest_rows(workspace: Path, manifest_path: Path) -> int:
    manifest = read_json(manifest_path)
    errors: list[str] = []
    for binding in manifest["files"]:
        path = workspace / binding["path"]
        if (
            not path.is_file()
            or path.stat().st_size != binding["bytes"]
            or sha256_file(path) != binding["sha256"]
        ):
            errors.append(binding["path"])
    if errors:
        raise RuntimeError(
            f"manifest mismatch in {manifest_path.name}: " + ", ".join(errors)
        )
    return len(manifest["files"])


def repair_normalization_evidence(
    workspace: Path,
    candidate: Path,
    report: Path,
) -> int:
    """Make marker normalization evidence self-describing and re-hash all layers."""

    if not candidate.is_dir() or not report.is_dir():
        raise FileNotFoundError("canonical candidate/report package is absent")
    pre = guard_state(workspace)
    old_index = read_json(candidate / "EXTRACTED_TEXT_INDEX.json")
    old_entries = extraction_entries(old_index)
    by_id = {row["source_id"]: row for row in old_entries}
    repaired_entries: list[dict[str, Any]] = []
    for spec in SOURCES:
        old = dict(by_id[spec["source_id"]])
        if "text_path" not in old:
            repaired_entries.append(old)
            continue
        text_path = candidate / old["text_path"]
        if not text_path.is_file():
            raise FileNotFoundError(text_path)
        if (
            text_path.stat().st_size != old["text_bytes"]
            or sha256_file(text_path) != old["text_sha256"]
        ):
            raise RuntimeError(
                f"persisted extracted-text binding changed: {spec['source_id']}"
            )
        text = text_path.read_text(encoding="utf-8", errors="strict")
        checks, evidence, normalized_text_sha256 = marker_evidence(
            text, spec["markers"]
        )
        old.update(
            {
                "status": (
                    "PASS" if all(checks.values()) else "MARKER_MISMATCH"
                ),
                "normalization_contract_id": MARKER_NORMALIZATION_CONTRACT[
                    "contract_id"
                ],
                "normalized_text_sha256": normalized_text_sha256,
                "marker_checks": checks,
                "marker_evidence": evidence,
            }
        )
        repaired_entries.append(old)

    index = extraction_index(candidate, repaired_entries)
    recompute = index["independent_recompute"]
    if (
        recompute["marker_count"] != 41
        or recompute["matched_count"] != 41
        or not recompute["all_matched"]
        or recompute["raw_exact_false_normalized_true_count"] != 8
    ):
        raise RuntimeError(
            "independent marker recompute failed: "
            + json.dumps(recompute, ensure_ascii=False)
        )
    write_json(candidate / "EXTRACTED_TEXT_INDEX.json", index)

    raw_files = [
        path
        for path in candidate.rglob("*")
        if path.is_file()
        and path.name not in {
            "CANDIDATE_MANIFEST.json",
            "VERIFICATION.json",
            "RAW_FILE_MANIFEST.json",
        }
    ]
    write_json(
        candidate / "RAW_FILE_MANIFEST.json",
        manifest_for(raw_files, workspace, RUN_ID, "raw_and_extracted_evidence"),
    )
    candidate_core = [
        path
        for path in candidate.rglob("*")
        if path.is_file()
        and path.name not in {"CANDIDATE_MANIFEST.json", "VERIFICATION.json"}
    ]
    write_json(
        candidate / "CANDIDATE_MANIFEST.json",
        manifest_for(candidate_core, workspace, RUN_ID, "candidate_evidence"),
    )

    # Rebuild canonical report-to-raw bindings before binding the refreshed
    # candidate manifests into the report verification.
    repair_source_bindings(workspace, candidate, report)
    post = guard_state(workspace)
    if pre != post:
        raise RuntimeError("formal guard changed during normalization repair")

    candidate_verification = read_json(candidate / "VERIFICATION.json")
    candidate_verification.update(
        {
            "marker_normalization_contract": MARKER_NORMALIZATION_CONTRACT,
            "independent_marker_recompute": recompute,
            "marker_recompute_count": recompute["marker_count"],
            "marker_recompute_matched_count": recompute["matched_count"],
            "marker_recompute_all_matched": recompute["all_matched"],
            "marker_raw_exact_false_normalized_true_count": recompute[
                "raw_exact_false_normalized_true_count"
            ],
            "normalization_repair_guard_pre": pre,
            "normalization_repair_guard_post": post,
            "normalization_repair_guard_unchanged": pre == post,
        }
    )
    write_json(candidate / "VERIFICATION.json", candidate_verification)

    report_verification = read_json(report / "VERIFICATION.json")
    report_verification.update(
        {
            "marker_normalization_contract": MARKER_NORMALIZATION_CONTRACT,
            "independent_marker_recompute": recompute,
            "marker_recompute_count": recompute["marker_count"],
            "marker_recompute_matched_count": recompute["matched_count"],
            "marker_recompute_all_matched": recompute["all_matched"],
            "marker_raw_exact_false_normalized_true_count": recompute[
                "raw_exact_false_normalized_true_count"
            ],
            "candidate_evidence_bindings": [
                file_binding(candidate / name, workspace)
                for name in (
                    "CANDIDATE_MANIFEST.json",
                    "RAW_FILE_MANIFEST.json",
                    "REQUEST_RECEIPTS.json",
                    "EXTRACTED_TEXT_INDEX.json",
                    "VERIFICATION.json",
                )
            ],
            "normalization_repair_guard_pre": pre,
            "normalization_repair_guard_post": post,
            "normalization_repair_guard_unchanged": pre == post,
        }
    )
    write_json(report / "VERIFICATION.json", report_verification)
    report_files = [
        path
        for path in report.iterdir()
        if path.is_file() and path.name != "REPORT_MANIFEST.json"
    ]
    write_json(
        report / "REPORT_MANIFEST.json",
        manifest_for(report_files, workspace, RUN_ID, "human_audit_report"),
    )

    raw_verified = verify_manifest_rows(
        workspace, candidate / "RAW_FILE_MANIFEST.json"
    )
    candidate_verified = verify_manifest_rows(
        workspace, candidate / "CANDIDATE_MANIFEST.json"
    )
    report_verified = verify_manifest_rows(
        workspace, report / "REPORT_MANIFEST.json"
    )
    if {item.name for item in report.iterdir() if item.is_file()} != REPORT_FILES:
        raise RuntimeError("report file-set changed during normalization repair")
    print(
        json.dumps(
            {
                "run_id": RUN_ID,
                "mode": "repair_normalization_evidence",
                "normalization_contract_id": MARKER_NORMALIZATION_CONTRACT[
                    "contract_id"
                ],
                "independent_marker_recompute": (
                    f"{recompute['matched_count']}/{recompute['marker_count']}"
                ),
                "raw_exact_false_normalized_true": recompute[
                    "raw_exact_false_normalized_true_count"
                ],
                "raw_manifest_files_verified": raw_verified,
                "candidate_manifest_files_verified": candidate_verified,
                "report_manifest_files_verified": report_verified,
                "formal_guard_unchanged": pre == post,
                "business_conclusion_changed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    args = parse_args()
    workspace = (
        Path(args.workspace).resolve()
        if args.workspace
        else Path(__file__).resolve().parents[2]
    )
    candidate = (workspace / args.candidate_output).resolve()
    report = (workspace / args.report_output).resolve()
    require_child(
        candidate,
        (workspace / "tmp/d2a04b-source-closure").resolve(),
        "candidate output",
    )
    require_child(
        report,
        (workspace / "reports/data-governance").resolve(),
        "report output",
    )
    if args.repair_source_bindings:
        return repair_source_bindings(workspace, candidate, report)
    if args.repair_normalization_evidence:
        return repair_normalization_evidence(workspace, candidate, report)
    pre = guard_state(workspace)
    require_new_dir(candidate)
    try:
        require_new_dir(report)
    except Exception:
        candidate.rmdir()
        raise

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Quantification-StageA-OfficialRules-Audit/1.0 "
                "(unauthenticated; one serial request per official URL)"
            ),
            "Accept": "*/*",
        }
    )
    receipts = [
        fetch_one(session, spec, candidate, index)
        for index, spec in enumerate(SOURCES, start=1)
    ]
    write_json(candidate / "REQUEST_RECEIPTS.json", receipts)
    extraction_rows = extract_successes(candidate, receipts)
    extraction = extraction_index(candidate, extraction_rows)
    write_json(candidate / "EXTRACTED_TEXT_INDEX.json", extraction)

    raw_files = [
        path
        for path in candidate.rglob("*")
        if path.is_file()
        and path.name not in {
            "CANDIDATE_MANIFEST.json",
            "VERIFICATION.json",
            "RAW_FILE_MANIFEST.json",
        }
    ]
    write_json(
        candidate / "RAW_FILE_MANIFEST.json",
        manifest_for(raw_files, workspace, RUN_ID, "raw_and_extracted_evidence"),
    )

    coverage = official_rule_coverage()
    dates = effective_date_matrix()
    gaps = historical_gaps()
    gate = gate_receipt()
    write_json(report / "OFFICIAL_RULE_COVERAGE.json", coverage)
    write_json(report / "EFFECTIVE_DATE_MATRIX.json", dates)
    write_json(report / "HISTORICAL_GAPS.json", gaps)
    write_json(report / "GATE_RECEIPT.json", gate)
    (report / "README.md").write_text(
        report_readme(),
        encoding="utf-8",
        newline="\n",
    )

    source_bindings = {
        "run_id": RUN_ID,
        "receipts": [
            {
                "source_id": item["source_id"],
                "venue": item["venue"],
                "request_url": item["request_url"],
                "result": item["result"],
                "http_status": item.get("http_status"),
                "path": (
                    workspace_relative(candidate / item["path"], workspace)
                    if item.get("path")
                    else None
                ),
                "bytes": item.get("bytes"),
                "sha256": item.get("sha256"),
                "extraction_status": next(
                    row["status"]
                    for row in extraction_rows
                    if row["source_id"] == item["source_id"]
                ),
            }
            for item in receipts
        ],
        "formal_admission": False,
    }
    write_json(report / "SOURCE_BINDINGS.json", source_bindings)

    candidate_core = [
        path
        for path in candidate.rglob("*")
        if path.is_file()
        and path.name not in {"CANDIDATE_MANIFEST.json", "VERIFICATION.json"}
    ]
    write_json(
        candidate / "CANDIDATE_MANIFEST.json",
        manifest_for(candidate_core, workspace, RUN_ID, "candidate_evidence"),
    )

    post = guard_state(workspace)
    guard_unchanged = pre == post
    bindings_ok, binding_errors = validate_source_bindings(coverage, receipts)
    expected_failure_ok = any(
        item["source_id"] == "SZSE-TRADE-RULE-2023-DOC-STALE-ENDPOINT"
        and item["result"] == "HTTP_FAILURE_BODY_PRESERVED"
        and item.get("http_status") == 404
        for item in receipts
    )
    preserved = [
        item for item in receipts if item["result"] == "RESPONSE_PRESERVED"
    ]
    marker_pass = [
        item for item in extraction_rows if item["status"] == "PASS"
    ]
    marker_recompute = extraction["independent_recompute"]
    candidate_verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": (
            "PASS__FAIL_CLOSED_CANDIDATE"
            if guard_unchanged
            and bindings_ok
            and expected_failure_ok
            and len(preserved) >= 15
            and len(marker_pass) >= 13
            and marker_recompute["marker_count"] == 41
            and marker_recompute["matched_count"] == 41
            and marker_recompute["all_matched"]
            and marker_recompute[
                "raw_exact_false_normalized_true_count"
            ]
            == 8
            else "FAIL"
        ),
        "request_count": len(receipts),
        "response_preserved_count": len(preserved),
        "marker_pass_count": len(marker_pass),
        "marker_normalization_contract": MARKER_NORMALIZATION_CONTRACT,
        "independent_marker_recompute": marker_recompute,
        "marker_recompute_count": marker_recompute["marker_count"],
        "marker_recompute_matched_count": marker_recompute["matched_count"],
        "marker_recompute_all_matched": marker_recompute["all_matched"],
        "marker_raw_exact_false_normalized_true_count": marker_recompute[
            "raw_exact_false_normalized_true_count"
        ],
        "expected_404_preserved": expected_failure_ok,
        "formal_guard_pre": pre,
        "formal_guard_post": post,
        "formal_guard_unchanged": guard_unchanged,
        "source_bindings_valid": bindings_ok,
        "source_binding_errors": binding_errors,
        "formal_admission": False,
        "purpose_ceiling": PURPOSE_CEILING,
    }
    write_json(candidate / "VERIFICATION.json", candidate_verification)

    report_verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": (
            "PASS__FAIL_CLOSED_REPORT"
            if candidate_verification["status"]
            == "PASS__FAIL_CLOSED_CANDIDATE"
            and gate["gates"]["CORE03-Q2"]["status"] == "UNKNOWN"
            and gate["gates"]["CORE03-Q3"]["status"] == "UNKNOWN"
            and gate["gates"]["CORE06-Q3"]["status"] == "FAIL"
            and gate["gates"]["CORE06-Q4"]["status"] == "FAIL"
            else "FAIL"
        ),
        "candidate_evidence_bindings": [
            file_binding(candidate / name, workspace)
            for name in (
                "CANDIDATE_MANIFEST.json",
                "RAW_FILE_MANIFEST.json",
                "REQUEST_RECEIPTS.json",
                "EXTRACTED_TEXT_INDEX.json",
                "VERIFICATION.json",
            )
        ],
        "marker_normalization_contract": MARKER_NORMALIZATION_CONTRACT,
        "independent_marker_recompute": marker_recompute,
        "marker_recompute_count": marker_recompute["marker_count"],
        "marker_recompute_matched_count": marker_recompute["matched_count"],
        "marker_recompute_all_matched": marker_recompute["all_matched"],
        "marker_raw_exact_false_normalized_true_count": marker_recompute[
            "raw_exact_false_normalized_true_count"
        ],
        "formal_guard_unchanged": guard_unchanged,
        "expected_gate_statuses": {
            "CORE03-Q2": "UNKNOWN",
            "CORE03-Q3": "UNKNOWN",
            "CORE06-Q3": "FAIL",
            "CORE06-Q4": "FAIL",
        },
        "observed_gate_statuses": {
            key: value["status"] for key, value in gate["gates"].items()
        },
        "stage_a_status_changed": False,
        "formal_admission": False,
        "research_usable": False,
        "catalog_update": False,
        "source_registry_update": False,
        "roadmap_update": False,
        "projects_update": False,
        "snapshot_published": False,
    }
    write_json(report / "VERIFICATION.json", report_verification)
    report_files = [
        path
        for path in report.iterdir()
        if path.is_file() and path.name != "REPORT_MANIFEST.json"
    ]
    write_json(
        report / "REPORT_MANIFEST.json",
        manifest_for(report_files, workspace, RUN_ID, "human_audit_report"),
    )

    actual = {item.name for item in report.iterdir() if item.is_file()}
    if actual != REPORT_FILES:
        raise RuntimeError(
            f"report file-set mismatch: missing={REPORT_FILES - actual}, "
            f"extra={actual - REPORT_FILES}"
        )
    if report_verification["status"] != "PASS__FAIL_CLOSED_REPORT":
        raise RuntimeError(
            "fail-closed report verification did not pass: "
            + json.dumps(report_verification, ensure_ascii=False)
        )
    print(
        json.dumps(
            {
                "run_id": RUN_ID,
                "candidate": workspace_relative(candidate, workspace),
                "report": workspace_relative(report, workspace),
                "official_responses_preserved": len(preserved),
                "marker_pass": len(marker_pass),
                "gate_statuses": report_verification[
                    "observed_gate_statuses"
                ],
                "formal_guard_unchanged": guard_unchanged,
                "formal_admission": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
