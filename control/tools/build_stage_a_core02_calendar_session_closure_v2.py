#!/usr/bin/env python3
"""Build a bounded CORE-02 official calendar/session evidence increment.

The program is deliberately fail-closed.  It:

* performs one serial GET per allow-listed official URL without explicitly
  configuring application credentials, cookies or authorization headers;
* follows no redirects and performs no retry or WAF bypass;
* preserves every response (including failure bodies) with a SHA-256 receipt;
* hash-binds, but never rewrites, already sealed official evidence packages;
* writes only an inspection candidate under ``tmp`` and an audit report;
* leaves catalogs, registries, Roadmap, snapshots and published data unchanged.

The output is evidence for the D2A-04B review.  It is not formal admission.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


RUN_ID = "stage-a-core02-calendar-session-closure-20260730-v2"
SECOND_REPAIR_ID = f"{RUN_ID}__independent-qa-repair-v2"
REPAIR_HISTORY_DIR = f"repair_history/{SECOND_REPAIR_ID}"
PURPOSE_CEILING = "INSPECTION_ONLY"
QUALITY_STATUS = "NOT_ADMITTED"
DEFAULT_CANDIDATE = f"tmp/d2a04b-source-closure/{RUN_ID}"
DEFAULT_REPORT = f"reports/data-governance/{RUN_ID}"

BASELINE_REPORT = (
    "reports/data-governance/"
    "stage-a-core02-calendar-session-closure-20260730-v1"
)
BASELINE_CANDIDATE = (
    "tmp/d2a04b-source-closure/"
    "stage-a-core02-calendar-session-closure-20260730-v1"
)
LEGACY_CANDIDATE = (
    "tmp/d2a04b-source-closure/"
    "core02-official-calendar-session-closure-20260730-v1"
)
RULES_REPORT = (
    "reports/data-governance/"
    "stage-a-core03-core06-official-rules-closure-20260730-v1"
)
MIC_REPORT = (
    "reports/data-governance/"
    "stage-a-core02-iso-mic-evidence-20260730-v1"
)
EFFECTIVE_DATE_MATRIX = (
    "reports/data-governance/"
    "stage-a-core03-core06-official-rules-closure-20260730-v1/"
    "EFFECTIVE_DATE_MATRIX.json"
)
LIVE_CALENDAR_CSV = (
    "tmp/d2a04b-source-closure/"
    "core02-official-calendar-session-closure-20260730-v1/"
    "TRADING_DAY_CANDIDATE.csv"
)
LINEAGE_JSON = (
    "tmp/d2a04b-source-closure/"
    "core02-official-calendar-session-closure-20260730-v1/"
    "CALENDAR_NOTICE_LINEAGE.json"
)
GENERATOR_PATH = (
    "control/tools/build_stage_a_core02_calendar_session_closure_v2.py"
)
TEST_PATH = (
    "control/tests/test_build_stage_a_core02_calendar_session_closure_v2.py"
)
TEST_RUNNER_PATH = "control/tools/run_stage_a_core02_calendar_session_closure_v2_tests.py"
TEST_RESULTS_NAME = "TEST_RESULTS.json"

GUARDED_FILES = (
    "data/catalog/datasets.json",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    "control/ROADMAP.md",
    "control/projects.yaml",
    "control/WORKSPACE_INDEX.md",
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
    "server",
}

SPECS: tuple[dict[str, Any], ...] = (
    {
        "request_id": "sse_2013_annual_holiday_notice",
        "venue": "SSE",
        "evidence_type": "ANNUAL_HOLIDAY_SCHEDULE",
        "target_year": 2013,
        "url": (
            "https://www.sse.com.cn/aboutus/mediacenter/hotandd/"
            "c/c_20150912_3988635.shtml"
        ),
        "relative_path": "sse/calendar/2013_annual_notice.html",
        "required_token_groups": (
            ("2013",),
            ("休市",),
            ("元旦",),
            ("春节",),
            ("清明", "清明节"),
            ("劳动", "劳动节"),
            ("端午", "端午节"),
            ("中秋", "中秋节"),
            ("国庆", "国庆节"),
        ),
    },
    {
        "request_id": "sse_2014_annual_holiday_notice",
        "venue": "SSE",
        "evidence_type": "ANNUAL_HOLIDAY_SCHEDULE",
        "target_year": 2014,
        "url": (
            "https://www.sse.com.cn/aboutus/mediacenter/hotandd/"
            "c/c_20150912_3988725.shtml"
        ),
        "relative_path": "sse/calendar/2014_annual_notice.html",
        "required_token_groups": (
            ("2014",),
            ("休市",),
            ("元旦",),
            ("春节",),
            ("清明", "清明节"),
            ("劳动", "劳动节"),
            ("端午", "端午节"),
            ("中秋", "中秋节"),
            ("国庆", "国庆节"),
        ),
    },
    {
        "request_id": "szse_2013_annual_holiday_notice",
        "venue": "SZSE",
        "evidence_type": "ANNUAL_HOLIDAY_SCHEDULE",
        "target_year": 2013,
        "url": (
            "https://www.szse.cn/disclosure/notice/general/"
            "t20121225_501056.html"
        ),
        "relative_path": "szse/calendar/2013_annual_notice.html",
        "required_token_groups": (
            ("2013",),
            ("休市",),
            ("元旦",),
            ("春节",),
            ("清明", "清明节"),
            ("劳动", "劳动节"),
            ("端午", "端午节"),
            ("中秋", "中秋节"),
            ("国庆", "国庆节"),
        ),
    },
    {
        "request_id": "szse_2014_dragon_boat_notice",
        "venue": "SZSE",
        "evidence_type": "ANNUAL_NOTICE_FRAGMENT",
        "target_year": 2014,
        "url": (
            "https://www.szse.cn/disclosure/notice/general/"
            "t20140522_501236.html"
        ),
        "relative_path": "szse/calendar/2014_dragon_boat_notice.html",
        "required_token_groups": (("2014",), ("休市",), ("端午",)),
    },
    {
        "request_id": "szse_2014_mid_autumn_national_notice",
        "venue": "SZSE",
        "evidence_type": "ANNUAL_NOTICE_FRAGMENT",
        "target_year": 2014,
        "url": (
            "https://www.szse.cn/disclosure/notice/general/"
            "t20140828_501270.html"
        ),
        "relative_path": (
            "szse/calendar/2014_mid_autumn_national_notice.html"
        ),
        "required_token_groups": (
            ("2014",),
            ("休市",),
            ("中秋",),
            ("国庆", "国庆节"),
        ),
    },
    {
        "request_id": "szse_2006_trade_rule_notice",
        "venue": "SZSE",
        "evidence_type": "SESSION_RULE_NOTICE",
        "target_year": 2006,
        "url": (
            "https://www.szse.cn/disclosure/notice/general/"
            "t20060515_499577.html"
        ),
        "relative_path": "szse/rules/2006_trade_rule_notice.html",
        "required_token_groups": (
            ("交易规则",),
            ("2006",),
        ),
        "session_token_groups": (
            ("9:15", "09:15"),
            ("9:25", "09:25"),
            ("9:30", "09:30"),
            ("11:30",),
            ("13:00",),
            ("14:57",),
            ("15:00",),
        ),
    },
    {
        "request_id": "bse_2024_annual_holiday_notice",
        "venue": "BSE",
        "evidence_type": "ANNUAL_HOLIDAY_SCHEDULE",
        "target_year": 2024,
        "url": "https://www.bse.cn/Rest_arrangement/200020259.html",
        "relative_path": "bse/calendar/2024_annual_notice.html",
        "required_token_groups": (
            ("2024",),
            ("休市",),
            ("元旦",),
            ("春节",),
            ("清明", "清明节"),
            ("劳动", "劳动节"),
            ("端午", "端午节"),
            ("中秋", "中秋节"),
            ("国庆", "国庆节"),
        ),
    },
)

MAX_RESPONSE_BYTES = 3 * 1024 * 1024
SHANGHAI_TZ = timezone(timedelta(hours=8))
SEVEN_HOLIDAY_GROUPS = (
    ("元旦",),
    ("春节",),
    ("清明", "清明节"),
    ("劳动", "劳动节"),
    ("端午", "端午节"),
    ("中秋", "中秋节"),
    ("国庆", "国庆节"),
)
TRANSPORT_BOUNDARY = {
    "application_explicit_credentials_configured": False,
    "application_explicit_authorization_header_configured": False,
    "application_explicit_cookie_configured": False,
    "proxy_and_credential_origin_status": "UNKNOWN",
    "environment_proxy_observed": "UNKNOWN",
    "netrc_observed": "UNKNOWN",
    "direct_connection_claimed": False,
    "absolute_no_credentials_claimed": False,
    "legacy_receipt_field_interpretation": (
        "auth_cookie_token_sent=false records only that this application code "
        "did not explicitly attach credentials, cookies or tokens. It does "
        "not establish whether environment proxy or netrc configuration was "
        "consulted by the HTTP stack."
    ),
}
BASE_REPORT_FILES = {
    "ANNUAL_COMPLETENESS_EVIDENCE.json",
    "ANNUAL_HOLIDAY_NOTICE_CHAIN.json",
    "CORE02_GATE_RECEIPT.json",
    "OFFICIAL_EVIDENCE_MAP.json",
    "PIT_PUBLICATION_ASSESSMENT.json",
    "Q3_Q4_LIVE_RECOMPUTE.json",
    "QA_REPAIR_RECEIPT.json",
    "README.md",
    "REPORT_MANIFEST.json",
    "REQUEST_RECEIPTS.json",
    "SESSION_VERSION_CHAIN.json",
    "SOURCE_MANIFEST.json",
    "STAGE_A_STATUS_DELTA.json",
    "SUBCHECK_EVIDENCE_MATRIX.json",
    "TEMPORARY_CLOSURE_GAPS.json",
    TEST_RESULTS_NAME,
    "VERIFICATION.json",
}
CANDIDATE_HISTORY_FILES = {
    "CANDIDATE_MANIFEST.json",
    "REQUEST_RECEIPTS.json",
    "SOURCE_MANIFEST.json",
}
EXPECTED_REPORT_FILES = BASE_REPORT_FILES | {
    f"{REPAIR_HISTORY_DIR}/{name}"
    for name in (BASE_REPORT_FILES - {TEST_RESULTS_NAME})
}


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


def now_shanghai() -> str:
    return datetime.now(tz=SHANGHAI_TZ).isoformat(timespec="seconds")


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


def tree_inventory(root: Path, workspace: Path) -> dict[str, Any]:
    files = (
        [
            file_binding(path, workspace)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
        if root.is_dir()
        else []
    )
    return {
        "path": workspace_relative(root, workspace),
        "exists": root.is_dir(),
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "tree_sha256": sha256_bytes(canonical_json_bytes(files)),
    }


def guard_hashes(workspace: Path) -> dict[str, Any]:
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


def manifest_files(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("files"), list):
        return value["files"]
    raise ValueError("unsupported report-manifest schema")


def build_input_bindings(workspace: Path) -> list[dict[str, Any]]:
    return [
        {
            "role": role,
            **file_binding(workspace / relative, workspace),
        }
        for role, relative in (
            ("GENERATOR", GENERATOR_PATH),
            ("TEST_SOURCE", TEST_PATH),
            ("TEST_RUNNER", TEST_RUNNER_PATH),
        )
    ]


def actual_relative_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def base_candidate_files() -> set[str]:
    return {
        "CANDIDATE_MANIFEST.json",
        "REQUEST_RECEIPTS.json",
        "SOURCE_MANIFEST.json",
        *(f"raw/{spec['relative_path']}" for spec in SPECS),
    }


def expected_candidate_files() -> set[str]:
    return base_candidate_files() | {
        f"{REPAIR_HISTORY_DIR}/{name}" for name in CANDIDATE_HISTORY_FILES
    }


def preserve_pre_second_repair(
    workspace: Path, candidate: Path, report: Path
) -> dict[str, Any]:
    """Preserve the first repaired package before any second-repair write."""
    candidate_history = candidate / REPAIR_HISTORY_DIR
    report_history = report / REPAIR_HISTORY_DIR
    if candidate_history.exists() or report_history.exists():
        if not candidate_history.is_dir() or not report_history.is_dir():
            raise RuntimeError("repair history path is not a directory")
        return {
            "repair_id": SECOND_REPAIR_ID,
            "preserved_existing_history": True,
            "candidate_history": [file_binding(candidate_history / name, workspace) for name in sorted(CANDIDATE_HISTORY_FILES)],
            "report_history": [file_binding(report_history / name, workspace) for name in sorted(BASE_REPORT_FILES - {TEST_RESULTS_NAME})],
        }
    candidate_history.mkdir(parents=True, exist_ok=False)
    report_history.mkdir(parents=True, exist_ok=False)
    for name in sorted(CANDIDATE_HISTORY_FILES):
        shutil.copy2(candidate / name, candidate_history / name)
    for name in sorted(BASE_REPORT_FILES):
        shutil.copy2(report / name, report_history / name)
    return {
        "repair_id": SECOND_REPAIR_ID,
        "candidate_history": [
            file_binding(candidate_history / name, workspace)
            for name in sorted(CANDIDATE_HISTORY_FILES)
        ],
        "report_history": [
            file_binding(report_history / name, workspace)
            for name in sorted(BASE_REPORT_FILES)
        ],
    }


def reject_extra_or_missing(
    root: Path,
    expected: set[str],
    *,
    allow_missing: set[str] | None = None,
) -> dict[str, Any]:
    actual = actual_relative_files(root)
    missing = expected - actual
    if allow_missing:
        missing -= allow_missing
    extra = actual - expected
    if missing or extra:
        raise RuntimeError(
            f"strict file set failed for {root}: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
    return {
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing": [],
        "extra": [],
        "status": "PASS",
    }


def validate_bound_manifest(
    workspace: Path,
    root: Path,
    manifest_name: str,
    expected: set[str],
) -> dict[str, Any]:
    reject_extra_or_missing(root, expected)
    manifest_path = root / manifest_name
    manifest_value = load_json(manifest_path)
    items = manifest_files(manifest_value)
    observed_paths = {item["path"] for item in items}
    expected_paths = {
        workspace_relative(root / relative, workspace)
        for relative in expected
        if relative != manifest_name
    }
    errors: list[str] = []
    if observed_paths != expected_paths:
        errors.append("manifest file set does not equal strict disk file set")
    for item in items:
        path = workspace / item["path"]
        if not path.is_file():
            errors.append(f"missing:{item['path']}")
            continue
        if path.stat().st_size != item["bytes"]:
            errors.append(f"bytes:{item['path']}")
        if sha256_file(path) != item["sha256"]:
            errors.append(f"sha256:{item['path']}")
    expected_inputs = build_input_bindings(workspace)
    if manifest_value.get("build_inputs") != expected_inputs:
        errors.append("build input hash binding mismatch")
    return {
        "manifest": file_binding(manifest_path, workspace),
        "file_count": len(items),
        "build_input_count": len(expected_inputs),
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }


def verify_report_manifest(
    workspace: Path, report_relative: str
) -> dict[str, Any]:
    manifest_path = workspace / report_relative / "REPORT_MANIFEST.json"
    items = manifest_files(load_json(manifest_path))
    errors: list[str] = []
    for item in items:
        path = workspace / item["path"]
        if not path.is_file():
            errors.append(f"missing:{item['path']}")
            continue
        if path.stat().st_size != item["bytes"]:
            errors.append(f"bytes:{item['path']}")
        if sha256_file(path) != item["sha256"]:
            errors.append(f"sha256:{item['path']}")
    return {
        "report": report_relative,
        "manifest": file_binding(manifest_path, workspace),
        "file_count": len(items),
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }


def safe_headers(headers: requests.structures.CaseInsensitiveDict) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in SAFE_RESPONSE_HEADERS
    }


def html_text(body: bytes) -> str:
    text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def token_groups_status(
    text: str, groups: tuple[tuple[str, ...], ...]
) -> tuple[bool, list[dict[str, Any]]]:
    checks = [
        {
            "alternatives": list(group),
            "matched": any(token in text for token in group),
        }
        for group in groups
    ]
    return all(item["matched"] for item in checks), checks


def looks_like_waf_or_challenge(text: str, status: int) -> bool:
    lower = text.lower()
    markers = (
        "wswaf",
        "访问违规",
        "解除封禁",
        "access denied",
        "captcha",
        "人机验证",
    )
    return status in {403, 429} or any(marker in lower for marker in markers)


def classify_html_response(
    spec: dict[str, Any], status: int, body: bytes, content_type: str
) -> dict[str, Any]:
    text = html_text(body)
    content_ok = (
        "text/html" in content_type.lower()
        or b"<html" in body[:4096].lower()
        or b"<!doctype html" in body[:4096].lower()
    )
    required_ok, required_checks = token_groups_status(
        text, spec["required_token_groups"]
    )
    session_checks: list[dict[str, Any]] = []
    session_ok: bool | None = None
    if "session_token_groups" in spec:
        session_ok, session_checks = token_groups_status(
            text, spec["session_token_groups"]
        )
    waf = looks_like_waf_or_challenge(text, status)
    if status != 200:
        result = "HTTP_FAILURE_BODY_PRESERVED"
    elif waf:
        result = "WAF_OR_CHALLENGE_BODY_PRESERVED"
    elif not content_ok:
        result = "CONTENT_MISMATCH_BODY_PRESERVED"
    elif not required_ok:
        result = "SEMANTIC_INSUFFICIENT_BODY_PRESERVED"
    else:
        result = "RESPONSE_PRESERVED"
    return {
        "result": result,
        "content_type_valid": content_ok,
        "waf_or_challenge_detected": waf,
        "required_token_groups": required_checks,
        "semantic_required_groups_pass": required_ok,
        "session_token_groups": session_checks,
        "session_intervals_explicit": session_ok,
        "text_character_count": len(text),
    }


def fetch_one(
    session: requests.Session,
    spec: dict[str, Any],
    raw_root: Path,
    sequence: int,
) -> dict[str, Any]:
    host = urlparse(spec["url"]).hostname
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"host outside official allowlist: {host}")
    receipt: dict[str, Any] = {
        "sequence": sequence,
        "request_id": spec["request_id"],
        "venue": spec["venue"],
        "evidence_type": spec["evidence_type"],
        "target_year": spec["target_year"],
        "method": "GET",
        "request_url": spec["url"],
        "started_at_asia_shanghai": now_shanghai(),
        "attempts": 1,
        "retry_count": 0,
        "allow_redirects": False,
        "serial": True,
        "application_explicit_credentials_configured": False,
        "application_explicit_authorization_header_configured": False,
        "application_explicit_cookie_configured": False,
        "proxy_and_credential_origin_status": "UNKNOWN",
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_admission": False,
    }
    response_body = b""
    response_status: int | None = None
    response_headers: dict[str, str] = {}
    failure_reason: str | None = None
    try:
        response = session.get(
            spec["url"],
            timeout=(10, 35),
            allow_redirects=False,
            stream=True,
        )
        response_status = response.status_code
        response_headers = safe_headers(response.headers)
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                failure_reason = (
                    "response exceeded max bytes; bounded prefix preserved"
                )
                break
            chunks.append(chunk)
        response_body = b"".join(chunks)
    except requests.RequestException as exc:
        failure_reason = f"{type(exc).__name__}: {exc}"

    if response_status == 200 and failure_reason is None:
        relative_path = spec["relative_path"]
    else:
        relative_path = f"failed/{spec['request_id']}.bin"
    target = raw_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response_body)
    receipt.update(
        {
            "http_status": response_status,
            "response_headers": response_headers,
            "relative_path": relative_path,
            "bytes": len(response_body),
            "response_sha256": sha256_bytes(response_body),
            "completed_at_asia_shanghai": now_shanghai(),
        }
    )
    if response_status is None:
        receipt.update(
            {
                "result": "NETWORK_FAILURE_EMPTY_BODY_PRESERVED",
                "reason": failure_reason,
            }
        )
        return receipt
    if failure_reason is not None:
        receipt.update(
            {
                "result": "REJECTED_RESPONSE_BODY_PRESERVED",
                "reason": failure_reason,
            }
        )
        return receipt
    receipt.update(
        classify_html_response(
            spec,
            response_status,
            response_body,
            response_headers.get("content-type", ""),
        )
    )
    return receipt


def receipt_binding(
    receipt: dict[str, Any], candidate: Path, workspace: Path
) -> dict[str, Any]:
    path = candidate / "raw" / receipt["relative_path"]
    if sha256_file(path) != receipt["response_sha256"]:
        raise ValueError(f"response hash mismatch: {receipt['request_id']}")
    return {
        "source_id": receipt["request_id"],
        "venue": receipt["venue"],
        "evidence_type": receipt["evidence_type"],
        "target_year": receipt["target_year"],
        "request_url": receipt["request_url"],
        "result": receipt["result"],
        "http_status": receipt["http_status"],
        **file_binding(path, workspace),
        "source_run": RUN_ID,
    }


def sealed_rule_bindings(workspace: Path) -> list[dict[str, Any]]:
    values = load_json(workspace / RULES_REPORT / "SOURCE_BINDINGS.json")
    selected = {
        "SSE-TRADE-RULE-2006-REPEALED",
        "SSE-TRADE-RULE-2014-AMENDMENT",
        "SSE-TRADE-RULE-2018-AMENDMENT",
        "SSE-TRADE-RULE-2020-AMENDMENT",
        "SSE-TRADE-RULE-2023-DOC",
        "SSE-TRADE-RULE-2026-DOC",
        "SZSE-TRADE-RULE-2011-DOC",
        "SZSE-TRADE-RULE-2020-12-DOC",
        "SZSE-TRADE-RULE-2026-DOC",
        "BSE-TRADE-RULE-2021",
        "BSE-TRADE-RULE-2026",
    }
    result: list[dict[str, Any]] = []
    by_id = {item["source_id"]: item for item in values["receipts"]}
    missing = selected - set(by_id)
    if missing:
        raise KeyError(f"sealed source binding missing: {sorted(missing)}")
    for source_id in sorted(selected):
        item = by_id[source_id]
        path = workspace / item["path"]
        if sha256_file(path) != item["sha256"]:
            raise ValueError(f"sealed source hash mismatch: {source_id}")
        result.append(
            {
                **item,
                "source_run": (
                    "stage-a-core03-core06-official-rules-closure-20260730-v1"
                ),
                "reuse_verified": True,
            }
        )
    return result


def baseline_receipt_bindings(workspace: Path) -> list[dict[str, Any]]:
    receipts = load_json(
        workspace / BASELINE_CANDIDATE / "REQUEST_RECEIPTS.json"
    )
    selected = {
        "bse_abnormal_trading_management_document",
        "bse_2025_annual_holiday_page",
        "bse_2026_annual_holiday_page",
    }
    by_id = {item["request_id"]: item for item in receipts}
    result: list[dict[str, Any]] = []
    for source_id in sorted(selected):
        item = by_id[source_id]
        if item["result"] != "RESPONSE_PRESERVED":
            raise ValueError(f"baseline response is not preserved: {source_id}")
        path = workspace / BASELINE_CANDIDATE / "raw" / item["relative_path"]
        if sha256_file(path) != item["response_sha256"]:
            raise ValueError(f"baseline response hash mismatch: {source_id}")
        result.append(
            {
                "source_id": source_id,
                "venue": item["venue"],
                "request_url": item["request_url"],
                "result": item["result"],
                "http_status": item["http_status"],
                **file_binding(path, workspace),
                "source_run": (
                    "stage-a-core02-calendar-session-closure-20260730-v1"
                ),
                "reuse_verified": True,
            }
        )
    return result


def legacy_calendar_lineage_binding(
    workspace: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = workspace / LEGACY_CANDIDATE / "CALENDAR_NOTICE_LINEAGE.json"
    rows = load_json(path)
    for row in rows:
        raw = workspace / row["raw_path"]
        if sha256_file(raw) != row["raw_sha256"]:
            raise ValueError(
                f"legacy calendar raw hash mismatch: {row['raw_path']}"
            )
    return (
        {
            "source_id": "LEGACY-CALENDAR-NOTICE-LINEAGE-2015-2026",
            **file_binding(path, workspace),
            "row_count": len(rows),
            "raw_row_hashes_verified": True,
            "source_run": (
                "core02-official-calendar-session-closure-20260730-v1"
            ),
        },
        rows,
    )


def mic_bindings(workspace: Path) -> list[dict[str, Any]]:
    evidence = load_json(workspace / MIC_REPORT / "MIC_MAPPING_EVIDENCE.json")
    result: list[dict[str, Any]] = []
    for item in evidence["raw_bindings"]:
        path = workspace / item["path"]
        if sha256_file(path) != item["sha256"]:
            raise ValueError(f"MIC raw hash mismatch: {item['path']}")
        result.append(
            {
                "source_id": f"ISO10383-{path.name}",
                **item,
                "source_run": "stage-a-core02-iso-mic-evidence-20260730-v1",
                "reuse_verified": True,
            }
        )
    return result


def compose_source_manifest(
    workspace: Path,
    response_bindings: list[dict[str, Any]],
    rule_bindings: list[dict[str, Any]],
    baseline_bindings: list[dict[str, Any]],
    lineage_binding: dict[str, Any],
    iso_bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "run_id": RUN_ID,
        "path_contract": "CANONICAL_WORKSPACE_RELATIVE",
        "transport_boundary": dict(TRANSPORT_BOUNDARY),
        "new_official_responses": response_bindings,
        "reused_sealed_rule_sources": rule_bindings,
        "reused_baseline_sources": baseline_bindings,
        "reused_calendar_lineage": lineage_binding,
        "reused_iso10383_sources": iso_bindings,
        "build_inputs": build_input_bindings(workspace),
        "formal_admission": False,
    }


def verify_source_manifest_live(
    workspace: Path,
    candidate: Path,
    report: Path,
) -> dict[str, Any]:
    """Recursively revalidate every SOURCE_MANIFEST binding from live bytes."""
    errors: list[str] = []
    candidate_path = candidate / "SOURCE_MANIFEST.json"
    report_path = report / "SOURCE_MANIFEST.json"
    copies_byte_identical = (
        candidate_path.is_file()
        and report_path.is_file()
        and sha256_file(candidate_path) == sha256_file(report_path)
    )
    if not copies_byte_identical:
        errors.append("candidate/report SOURCE_MANIFEST copies differ")
    try:
        receipts = load_json(candidate / "REQUEST_RECEIPTS.json")
        if len(receipts) != len(SPECS):
            errors.append(
                f"official response receipt count {len(receipts)} != {len(SPECS)}"
            )
        response_bindings = [
            receipt_binding(receipt, candidate, workspace)
            for receipt in receipts
        ]
        rule_bindings = sealed_rule_bindings(workspace)
        baseline_bindings = baseline_receipt_bindings(workspace)
        lineage_binding, lineage_rows = legacy_calendar_lineage_binding(
            workspace
        )
        iso_bindings = mic_bindings(workspace)
        expected = compose_source_manifest(
            workspace,
            response_bindings,
            rule_bindings,
            baseline_bindings,
            lineage_binding,
            iso_bindings,
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"recursive external binding verification failed: {exc}")
        response_bindings = []
        rule_bindings = []
        baseline_bindings = []
        lineage_rows = []
        iso_bindings = []
        expected = None

    stored: dict[str, Any] | None = None
    try:
        stored = load_json(candidate_path)
    except (FileNotFoundError, json.JSONDecodeError, TypeError) as exc:
        errors.append(f"candidate SOURCE_MANIFEST cannot be loaded: {exc}")
    manifest_matches_live = expected is not None and stored == expected
    if not manifest_matches_live:
        errors.append("SOURCE_MANIFEST differs from live recursive bindings")

    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "candidate_report_copies_byte_identical": copies_byte_identical,
        "manifest_matches_live_recursive_bindings": manifest_matches_live,
        "direct_binding_counts": {
            "new_official_responses": len(response_bindings),
            "sealed_rule_sources": len(rule_bindings),
            "baseline_sources": len(baseline_bindings),
            "iso10383_sources": len(iso_bindings),
            "build_inputs": len(build_input_bindings(workspace)),
        },
        "lineage_binding_count": 1 if expected is not None else 0,
        "lineage_raw_row_count": len(lineage_rows),
        "transport_boundary": dict(TRANSPORT_BOUNDARY),
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }


def reassess_preserved_responses(
    workspace: Path,
    candidate: Path,
    receipts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    spec_by_id = {spec["request_id"]: spec for spec in SPECS}
    if {item["request_id"] for item in receipts} != set(spec_by_id):
        raise ValueError("preserved receipt ids do not match the seven specs")
    assessments: list[dict[str, Any]] = []
    for item in receipts:
        spec = spec_by_id[item["request_id"]]
        path = candidate / "raw" / item["relative_path"]
        if not path.is_file():
            raise FileNotFoundError(path)
        if sha256_file(path) != item["response_sha256"]:
            raise ValueError(f"response hash mismatch: {item['request_id']}")
        body = path.read_bytes()
        current = classify_html_response(
            spec,
            int(item["http_status"]),
            body,
            item.get("response_headers", {}).get("content-type", ""),
        )
        text = html_text(body)
        seven_pass, seven_checks = token_groups_status(
            text, SEVEN_HOLIDAY_GROUPS
        )
        is_master = spec["evidence_type"] == "ANNUAL_HOLIDAY_SCHEDULE"
        assessments.append(
            {
                "request_id": item["request_id"],
                "venue": spec["venue"],
                "target_year": spec["target_year"],
                "evidence_type": spec["evidence_type"],
                "original_result": item["result"],
                "reassessed_result": current["result"],
                "master_schedule_eligible": (
                    is_master
                    and current["result"] == "RESPONSE_PRESERVED"
                    and current["semantic_required_groups_pass"]
                    and seven_pass
                ),
                "fragment_only": (
                    spec["evidence_type"] == "ANNUAL_NOTICE_FRAGMENT"
                ),
                "seven_holiday_groups_pass": seven_pass if is_master else False,
                "seven_holiday_groups": seven_checks,
                "session_intervals_explicit": current.get(
                    "session_intervals_explicit"
                ),
                "raw": file_binding(path, workspace),
            }
        )
    return assessments


def recompute_q3_q4(workspace: Path) -> dict[str, Any]:
    path = workspace / LIVE_CALENDAR_CSV
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("calendar CSV has no header")
        required = {
            "venue",
            "calendar_date",
            "strict_open_sequence",
            "is_trading_day",
            "calendar_state",
        }
        if not required.issubset(reader.fieldnames):
            raise ValueError(
                f"calendar CSV missing fields: {sorted(required-set(reader.fieldnames))}"
            )
        rows.extend(reader)

    key_counts: dict[tuple[str, str], int] = {}
    venue_rows: dict[str, list[dict[str, str]]] = {}
    invalid_dates: list[str] = []
    invalid_flags: list[str] = []
    weekend_open: list[dict[str, str]] = []
    for row in rows:
        venue = row["venue"]
        key = (venue, row["calendar_date"])
        key_counts[key] = key_counts.get(key, 0) + 1
        venue_rows.setdefault(venue, []).append(row)
        try:
            parsed_date = date.fromisoformat(row["calendar_date"])
        except ValueError:
            invalid_dates.append(row["calendar_date"])
            continue
        if row["is_trading_day"] not in {"True", "False"}:
            invalid_flags.append(row["is_trading_day"])
            continue
        if row["is_trading_day"] == "True" and parsed_date.weekday() >= 5:
            weekend_open.append(
                {"venue": venue, "calendar_date": row["calendar_date"]}
            )

    duplicate_keys = [
        {"venue": key[0], "calendar_date": key[1], "count": count}
        for key, count in key_counts.items()
        if count != 1
    ]
    sequence_checks: dict[str, Any] = {}
    venue_date_sets: dict[str, set[str]] = {}
    venue_flag_maps: dict[str, dict[str, str]] = {}
    for venue, values in sorted(venue_rows.items()):
        ordered = sorted(values, key=lambda item: item["calendar_date"])
        open_rows = [
            item for item in ordered if item["is_trading_day"] == "True"
        ]
        actual_sequence: list[int | None] = []
        for item in open_rows:
            try:
                actual_sequence.append(int(item["strict_open_sequence"]))
            except (TypeError, ValueError):
                actual_sequence.append(None)
        expected_sequence = list(range(1, len(open_rows) + 1))
        closed_nonblank = sum(
            1
            for item in ordered
            if item["is_trading_day"] == "False"
            and item["strict_open_sequence"].strip()
        )
        sequence_checks[venue] = {
            "row_count": len(ordered),
            "open_row_count": len(open_rows),
            "min_date": ordered[0]["calendar_date"] if ordered else None,
            "max_date": ordered[-1]["calendar_date"] if ordered else None,
            "sequence_min": min(actual_sequence) if actual_sequence else None,
            "sequence_max": max(actual_sequence) if actual_sequence else None,
            "strict_sequence_exact": actual_sequence == expected_sequence,
            "closed_rows_with_nonblank_sequence": closed_nonblank,
        }
        venue_date_sets[venue] = {item["calendar_date"] for item in ordered}
        venue_flag_maps[venue] = {
            item["calendar_date"]: item["is_trading_day"] for item in ordered
        }

    venues = sorted(venue_rows)
    shared_dates: set[str] = set()
    cross_venue_flag_differences: list[dict[str, str]] = []
    if venues == ["SSE", "SZSE"]:
        shared_dates = venue_date_sets["SSE"] & venue_date_sets["SZSE"]
        cross_venue_flag_differences = [
            {
                "calendar_date": day,
                "SSE": venue_flag_maps["SSE"][day],
                "SZSE": venue_flag_maps["SZSE"][day],
            }
            for day in sorted(shared_dates)
            if venue_flag_maps["SSE"][day] != venue_flag_maps["SZSE"][day]
        ]

    mechanical_calendar_checks_pass = (
        len(rows) == 8454
        and len(key_counts) == 8454
        and not duplicate_keys
        and not invalid_dates
        and not invalid_flags
        and not weekend_open
        and set(sequence_checks) == {"SSE", "SZSE"}
        and all(
            item["strict_sequence_exact"]
            and item["closed_rows_with_nonblank_sequence"] == 0
            for item in sequence_checks.values()
        )
    )
    q4_pass = (
        venues == ["SSE", "SZSE"]
        and len(venue_rows["SSE"]) == 4227
        and len(venue_rows["SZSE"]) == 4227
        and venue_date_sets["SSE"] == venue_date_sets["SZSE"]
        and len(shared_dates) == 4227
        and len(key_counts) == len(rows)
    )
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "input": file_binding(path, workspace),
        "algorithm": {
            "Q3": (
                "Parse all rows and recompute bounded mechanical checks for "
                "unique (venue,calendar_date), exact 1..N open sequence by "
                "venue, blank sequence on closed rows, valid ISO "
                "dates/booleans and zero weekend-open rows. These local checks "
                "do not satisfy the CORE-02 Q3 contract, which also requires "
                "recomputable previous/next trading days, month/quarter/year-end "
                "markers, BSE coverage and historical boundary closure."
            ),
            "Q4": (
                "Test an explicit non-null venue dimension with exactly SSE "
                "and SZSE, 4,227 rows each, identical date support and unique "
                "composite keys; no cross-venue row overwrite is permitted."
            ),
        },
        "measurements": {
            "row_count": len(rows),
            "unique_venue_date_keys": len(key_counts),
            "duplicate_key_count": len(duplicate_keys),
            "invalid_date_count": len(invalid_dates),
            "invalid_flag_count": len(invalid_flags),
            "weekend_open_count": len(weekend_open),
            "venue_counts": {
                venue: len(values) for venue, values in sorted(venue_rows.items())
            },
            "sequence_checks": sequence_checks,
            "shared_date_count": len(shared_dates),
            "cross_venue_open_flag_difference_count": len(
                cross_venue_flag_differences
            ),
        },
        "counterexamples": {
            "duplicate_keys": duplicate_keys[:20],
            "weekend_open": weekend_open[:20],
            "cross_venue_open_flag_differences": (
                cross_venue_flag_differences[:20]
            ),
        },
        "mechanical_subchecks": {
            "calendar_key_uniqueness": (
                "PASS"
                if len(key_counts) == len(rows) and not duplicate_keys
                else "FAIL"
            ),
            "strict_open_sequence": (
                "PASS"
                if sequence_checks
                and all(
                    item["strict_sequence_exact"]
                    and item["closed_rows_with_nonblank_sequence"] == 0
                    for item in sequence_checks.values()
                )
                else "FAIL"
            ),
            "weekend_open_counterexample": (
                "PASS" if not weekend_open else "FAIL"
            ),
            "bounded_calendar_shape": (
                "PASS" if mechanical_calendar_checks_pass else "FAIL"
            ),
        },
        "q3_contract_requirements": {
            "previous_next_trading_day_recomputable": "UNKNOWN",
            "month_quarter_year_end_markers_recomputable": "UNKNOWN",
            "bse_calendar_coverage": "FAIL",
            "historical_exchange_boundary_coverage": "UNKNOWN",
        },
        "q3_status_reason": (
            "The 8,454-row input supports only bounded SSE/SZSE key, sequence "
            "and weekend checks. Required previous/next trading-day fields, "
            "month/quarter/year-end markers, BSE rows and closed historical "
            "boundaries are absent or unproven."
        ),
        "Q3": "UNKNOWN",
        "Q4": "PASS" if q4_pass else "FAIL",
        "formal_admission": False,
    }


def annual_completeness_evidence(
    workspace: Path,
    candidate: Path,
    receipts: list[dict[str, Any]],
    lineage_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    assessments = reassess_preserved_responses(
        workspace, candidate, receipts
    )
    new_rows = [
        item
        for item in assessments
        if item["request_id"]
        in {
            "sse_2013_annual_holiday_notice",
            "sse_2014_annual_holiday_notice",
            "szse_2013_annual_holiday_notice",
            "szse_2014_dragon_boat_notice",
            "szse_2014_mid_autumn_national_notice",
            "bse_2024_annual_holiday_notice",
        }
    ]
    legacy: list[dict[str, Any]] = []
    for row in lineage_rows:
        if row["notice_type"] != "ANNUAL_HOLIDAY_SCHEDULE":
            continue
        path = workspace / row["raw_path"]
        text = html_text(path.read_bytes())
        passed, checks = token_groups_status(text, SEVEN_HOLIDAY_GROUPS)
        legacy.append(
            {
                "venue": row["venue"],
                "target_year": row["target_year"],
                "master_schedule_eligible": passed,
                "seven_holiday_groups": checks,
                "raw": file_binding(path, workspace),
            }
        )
    fragments = [
        item["request_id"] for item in new_rows if item["fragment_only"]
    ]
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "required_holiday_classes": [
            list(group) for group in SEVEN_HOLIDAY_GROUPS
        ],
        "new_response_assessments": new_rows,
        "legacy_master_assessments": legacy,
        "legacy_master_count": len(legacy),
        "legacy_master_pass_count": sum(
            item["master_schedule_eligible"] for item in legacy
        ),
        "fragment_request_ids": fragments,
        "fragment_master_prohibition": (
            "A holiday-specific fragment is never counted as an annual master "
            "schedule, even when its own holiday tokens are valid."
        ),
        "status": (
            "PASS_BOUNDED_COMPLETENESS_CHECK"
            if all(item["master_schedule_eligible"] for item in legacy)
            and all(
                item["master_schedule_eligible"]
                for item in new_rows
                if not item["fragment_only"]
            )
            else "FAIL_CLOSED"
        ),
        "formal_admission": False,
    }


def pit_publication_assessment(
    workspace: Path, lineage_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    rows = []
    for row in lineage_rows:
        rows.append(
            {
                "venue": row["venue"],
                "target_year": row["target_year"],
                "notice_type": row["notice_type"],
                "page_displayed_date": row["published_at"],
                "legacy_mechanical_available_at": row["available_at"],
                "publication_precision": row["publication_precision"],
                "first_publication_proven": False,
                "available_at_status": "UNKNOWN",
                "reason": (
                    "A date displayed on the currently retrieved historical "
                    "page does not prove first-publication time. Mechanical "
                    "+1 day is retained only as legacy inspection metadata."
                ),
                "raw": file_binding(workspace / row["raw_path"], workspace),
            }
        )
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "row_count": len(rows),
        "first_publication_proven_count": 0,
        "available_at_status": "UNKNOWN",
        "intraday_publication_status": "UNKNOWN",
        "rows": rows,
        "formal_admission": False,
    }


def make_session_chain(
    workspace: Path,
    rule_sources: list[dict[str, Any]],
    new_receipts: list[dict[str, Any]],
    response_bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    by_id = {item["source_id"]: item for item in rule_sources}
    new_by_id = {item["request_id"]: item for item in new_receipts}
    response_by_id = {
        item["source_id"]: item for item in response_bindings
    }
    matrix_path = workspace / EFFECTIVE_DATE_MATRIX
    matrix = load_json(matrix_path)
    matrix_rows = matrix["rows"]

    def matrix_row(venue: str, family: str) -> dict[str, Any]:
        matches = [
            item
            for item in matrix_rows
            if item["venue"] == venue and item["rule_family"] == family
        ]
        if len(matches) != 1:
            raise ValueError(
                f"effective-date matrix row mismatch: {venue}/{family}"
            )
        return matches[0]

    def node(
        venue: str,
        version: str,
        source_id: str,
        explicit_intervals: bool,
        *,
        effective_from: str | None,
        effective_to: str | None,
        boundary_status: str,
        boundary_basis: str,
    ) -> dict[str, Any]:
        return {
            "venue": venue,
            "rule_version": version,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "source": by_id[source_id],
            "explicit_session_intervals": explicit_intervals,
            "boundary_status": boundary_status,
            "boundary_basis": boundary_basis,
            "continuous_exact_interval_claimed": False,
        }

    sse_2006 = matrix_row("SSE", "general trading rule")
    sse_2023 = matrix_row("SSE", "general trading rule 2023")
    sse_2026 = matrix_row("SSE", "general trading rule 2026")
    szse_selected = matrix_row("SZSE", "selected general trading rules")
    szse_2023 = matrix_row("SZSE", "general trading rule 2023")
    szse_2026 = matrix_row("SZSE", "general trading rule 2026")
    bse_2021 = matrix_row("BSE", "general trading rule trial")
    bse_2026 = matrix_row("BSE", "general trading rule 2026")

    chains = {
        "SSE": [
            node(
                "SSE",
                "2006",
                "SSE-TRADE-RULE-2006-REPEALED",
                True,
                effective_from=sse_2006["effective_from"],
                effective_to=sse_2006["effective_to"],
                boundary_status=sse_2006["status"],
                boundary_basis=(
                    "The official version states its start, but later "
                    "intermediate revisions are not materialized."
                ),
            ),
            node(
                "SSE",
                "2014-amendment",
                "SSE-TRADE-RULE-2014-AMENDMENT",
                False,
                effective_from="UNKNOWN__AMENDMENT_ONLY",
                effective_to="UNKNOWN__INTERMEDIATE_REVISIONS_NOT_MATERIALIZED",
                boundary_status="PARTIAL",
                boundary_basis=(
                    "Selected amendment evidence is not a complete restated "
                    "rule and cannot establish a continuous exact interval."
                ),
            ),
            node(
                "SSE",
                "2018-amendment",
                "SSE-TRADE-RULE-2018-AMENDMENT",
                True,
                effective_from="UNKNOWN__SELECTED_2018_VERSION",
                effective_to="UNKNOWN__INTERMEDIATE_REVISIONS_NOT_MATERIALIZED",
                boundary_status="PARTIAL",
                boundary_basis=(
                    "A selected version exists, but this run does not prove "
                    "all intervening amendments or its exact supersession."
                ),
            ),
            node(
                "SSE",
                "2020-amendment",
                "SSE-TRADE-RULE-2020-AMENDMENT",
                False,
                effective_from="UNKNOWN__SELECTED_2020_AMENDMENT",
                effective_to="UNKNOWN__INTERMEDIATE_REVISIONS_NOT_MATERIALIZED",
                boundary_status="PARTIAL",
                boundary_basis=(
                    "The notice does not preserve a complete restated session "
                    "rule or an exact continuous effective interval."
                ),
            ),
            node(
                "SSE",
                "2023",
                "SSE-TRADE-RULE-2023-DOC",
                True,
                effective_from=sse_2023["effective_from"],
                effective_to=sse_2023["effective_to"],
                boundary_status=sse_2023["status"],
                boundary_basis=(
                    "The start is conditional on the first registered "
                    "main-board IPO day, not a universal exact calendar date."
                ),
            ),
            node(
                "SSE",
                "2026",
                "SSE-TRADE-RULE-2026-DOC",
                True,
                effective_from=sse_2026["effective_from"],
                effective_to=sse_2026["effective_to"],
                boundary_status=sse_2026["status"],
                boundary_basis="Current selected version boundary.",
            ),
        ],
        "SZSE": [
            {
                "venue": "SZSE",
                "rule_version": "2006",
                "effective_from": "2006-07-01",
                "effective_to": (
                    "UNKNOWN__INTERMEDIATE_REVISIONS_NOT_MATERIALIZED"
                ),
                "source": response_by_id["szse_2006_trade_rule_notice"],
                "explicit_session_intervals": True,
                "boundary_status": "PARTIAL",
                "boundary_basis": (
                    "The official page states implementation on 2006-07-01 "
                    "and contains the seven clock tokens. Its supersession "
                    "boundary and intervening versions remain unknown."
                ),
                "continuous_exact_interval_claimed": False,
            },
            node(
                "SZSE",
                "2011",
                "SZSE-TRADE-RULE-2011-DOC",
                True,
                effective_from=szse_selected["effective_from"],
                effective_to="UNKNOWN__INTERMEDIATE_REVISIONS_NOT_MATERIALIZED",
                boundary_status=szse_selected["status"],
                boundary_basis=(
                    "The matrix identifies a selected version, not a "
                    "continuous revision-complete interval."
                ),
            ),
            node(
                "SZSE",
                "2020-12",
                "SZSE-TRADE-RULE-2020-12-DOC",
                True,
                effective_from=szse_selected["effective_to"],
                effective_to="UNKNOWN__INTERMEDIATE_REVISIONS_NOT_MATERIALIZED",
                boundary_status=szse_selected["status"],
                boundary_basis=(
                    "The matrix identifies a selected version boundary; it "
                    "does not prove an exact interval through 2023."
                ),
            ),
            {
                "venue": "SZSE",
                "rule_version": "2023",
                "effective_from": szse_2023["effective_from"],
                "effective_to": szse_2023["effective_to"],
                "source_ids": szse_2023["source_ids"],
                "source": None,
                "explicit_session_intervals": False,
                "boundary_status": szse_2023["status"],
                "boundary_basis": (
                    "An event boundary is registered, but the official rule "
                    "text endpoint was not preserved. No session interval is "
                    "admitted from this row."
                ),
                "continuous_exact_interval_claimed": False,
            },
            node(
                "SZSE",
                "2026",
                "SZSE-TRADE-RULE-2026-DOC",
                True,
                effective_from=szse_2026["effective_from"],
                effective_to=szse_2026["effective_to"],
                boundary_status=szse_2026["status"],
                boundary_basis="Current selected version boundary.",
            ),
        ],
        "BSE": [
            node(
                "BSE",
                "2021",
                "BSE-TRADE-RULE-2021",
                True,
                effective_from=bse_2021["effective_from"],
                effective_to=bse_2021["effective_to"],
                boundary_status=bse_2021["status"],
                boundary_basis=(
                    "The start is explicit; intermediate modifications and "
                    "the exact supersession boundary are not materialized."
                ),
            ),
            node(
                "BSE",
                "2026",
                "BSE-TRADE-RULE-2026",
                True,
                effective_from=bse_2026["effective_from"],
                effective_to=bse_2026["effective_to"],
                boundary_status=bse_2026["status"],
                boundary_basis="Current selected version boundary.",
            ),
        ],
    }
    szse_2006 = new_by_id["szse_2006_trade_rule_notice"]
    exploratory = {
        "request_id": szse_2006["request_id"],
        "result": szse_2006["result"],
        "session_intervals_explicit": szse_2006.get(
            "session_intervals_explicit"
        ),
        "boundary": (
            "The notice is preserved, but it extends the parsed session chain "
            "only if all seven clock tokens are explicit."
        ),
    }
    if (
        szse_2006["result"] == "RESPONSE_PRESERVED"
        and szse_2006.get("session_intervals_explicit") is True
    ):
        exploratory["status"] = "PASS"
    else:
        exploratory["status"] = "SEMANTIC_INSUFFICIENT"
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "effective_date_matrix": file_binding(matrix_path, workspace),
        "chains": chains,
        "new_szse_2006_notice_assessment": exploratory,
        "coverage_status": {
            "SSE": "PARTIAL",
            "SZSE": "PARTIAL",
            "BSE": "PARTIAL",
        },
        "complete_effective_dated_chain": False,
        "gap": (
            "Exact effective/supersession boundaries and every intervening "
            "restated rule version are not proven for all three venues."
        ),
    }


def annual_chain(
    annual_evidence: dict[str, Any],
    baseline_bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    new_by_id = {
        item["request_id"]: item
        for item in annual_evidence["new_response_assessments"]
    }
    legacy_complete_years = {
        venue: sorted(
            {
                int(row["target_year"])
                for row in annual_evidence["legacy_master_assessments"]
                if row["venue"] == venue
                and row["master_schedule_eligible"]
            }
        )
        for venue in ("SSE", "SZSE")
    }
    bse_bound = {
        item["source_id"]: item
        for item in baseline_bindings
        if "annual_holiday" in item["source_id"]
    }
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "completeness_evidence": "ANNUAL_COMPLETENESS_EVIDENCE.json",
        "venues": {
            "SSE": {
                "sealed_complete_years": legacy_complete_years["SSE"],
                "new_attempts": {
                    "2013": new_by_id[
                        "sse_2013_annual_holiday_notice"
                    ]["master_schedule_eligible"],
                    "2014": new_by_id[
                        "sse_2014_annual_holiday_notice"
                    ]["master_schedule_eligible"],
                },
                "status": "PARTIAL",
                "gap": "No exhaustive annual official chain from exchange inception.",
            },
            "SZSE": {
                "sealed_complete_years": legacy_complete_years["SZSE"],
                "new_attempts": {
                    "2013": new_by_id[
                        "szse_2013_annual_holiday_notice"
                    ]["master_schedule_eligible"],
                    "2014_dragon_boat_fragment": "FRAGMENT_ONLY__NOT_MASTER",
                    "2014_mid_autumn_national_fragment": (
                        "FRAGMENT_ONLY__NOT_MASTER"
                    ),
                },
                "master_schedule_2014": "MISSING",
                "status": "PARTIAL",
                "gap": (
                    "2014 master annual notice and the exhaustive pre-2013 "
                    "chain are not preserved."
                ),
            },
            "BSE": {
                "sealed_years": [
                    2025 if "2025" in source_id else 2026
                    for source_id in sorted(bse_bound)
                ],
                "new_attempts": {
                    "2024": new_by_id[
                        "bse_2024_annual_holiday_notice"
                    ]["master_schedule_eligible"]
                },
                "status": "FAIL",
                "gap": (
                    "No complete official daily calendar and no complete "
                    "2021-2026 annual notice chain; 2022/2023 remain absent."
                ),
            },
        },
        "complete_three_venue_annual_chain": False,
    }


def update_subcheck(
    gates: dict[str, Any],
    gate_id: str,
    subcheck_id: str,
    status: str,
    basis: str,
) -> None:
    for item in gates[gate_id]["subchecks"]:
        if item["subcheck_id"] == subcheck_id:
            item["status"] = status
            item["basis"] = basis
            item["status_semantics"] = "THIS_RUN_AUDIT_SUBCHECK"
            item["baseline_status_claimed"] = False
            return
    gates[gate_id]["subchecks"].append(
        {
            "subcheck_id": subcheck_id,
            "status": status,
            "basis": basis,
            "status_semantics": "THIS_RUN_AUDIT_SUBCHECK",
            "baseline_status_claimed": False,
        }
    )


def build_gate_receipt(
    workspace: Path,
    annual: dict[str, Any],
    session_chain: dict[str, Any],
    pit: dict[str, Any],
    q3_q4: dict[str, Any],
) -> dict[str, Any]:
    baseline = load_json(workspace / BASELINE_REPORT / "CORE02_GATE_RECEIPT.json")
    gates = baseline["gates"]
    update_subcheck(
        gates,
        "CORE02-Q1",
        "CORE02-Q1.sse_historical_session_chain_2018_08_20_onward",
        "PARTIAL",
        (
            "Sealed official 2006, 2014, 2018, 2020, 2023 and 2026 SSE "
            "sources extend the evidence, but exact complete version and "
            "effective-boundary continuity remains unproven."
        ),
    )
    update_subcheck(
        gates,
        "CORE02-Q1",
        "CORE02-Q1.szse_historical_session_chain_2020_12_31_onward",
        "PARTIAL",
        (
            "Sealed official 2011, 2020 and 2026 rules plus the bounded 2006 "
            "notice attempt extend evidence; exact complete version and "
            "effective-boundary continuity remains unproven."
        ),
    )
    update_subcheck(
        gates,
        "CORE02-Q1",
        "CORE02-Q1.bse_official_daily_calendar",
        "FAIL",
        annual["venues"]["BSE"]["gap"],
    )
    update_subcheck(
        gates,
        "CORE02-Q1",
        "CORE02-Q1.all_venue_historical_session_version_chain",
        "FAIL",
        session_chain["gap"],
    )
    update_subcheck(
        gates,
        "CORE02-Q1",
        "CORE02-Q1.official_exchange_mic_mapping",
        "PASS",
        (
            "Hash-bound ISO 10383 Registration Authority evidence maps XSHG, "
            "XSHE and BJSE as active operating MICs."
        ),
    )
    update_subcheck(
        gates,
        "CORE02-Q2",
        "CORE02-Q2.sse_szse_annual_notice_chain",
        "PARTIAL",
        (
            "The sealed 2015-2026 chain remains complete for its bounded "
            "scope; new official attempts add 2013/2014 evidence, but the "
            "exchange-inception chain is not exhaustive."
        ),
    )
    update_subcheck(
        gates,
        "CORE02-Q2",
        "CORE02-Q2.sse_rule_revision_chain_2018_2026",
        "PARTIAL",
        (
            "Official 2006/2014 evidence narrows the earlier gap, but every "
            "restatement, amendment and exact supersession boundary is absent."
        ),
    )
    update_subcheck(
        gates,
        "CORE02-Q2",
        "CORE02-Q2.szse_rule_revision_chain_2020_2026",
        "PARTIAL",
        (
            "Official 2011 rule and bounded 2006 notice attempt narrow the "
            "earlier gap, but every restatement, amendment and exact "
            "supersession boundary is absent."
        ),
    )
    update_subcheck(
        gates,
        "CORE02-Q2",
        "CORE02-Q2.complete_three_venue_temporary_closure_archive",
        "FAIL",
        (
            "Known SSE/SZSE 2019/2020 revisions and a BSE emergency procedure "
            "remain preserved; an exhaustive three-venue event archive is absent."
        ),
    )
    update_subcheck(
        gates,
        "CORE02-Q2",
        "CORE02-Q2.complete_supplemental_notice_revision_history",
        "FAIL",
        (
            "No exhaustive first-publication/correction/supersedes/withdrawal "
            "chain was established for all annual and temporary notices."
        ),
    )
    update_subcheck(
        gates,
        "CORE02-Q2",
        "CORE02-Q2.intraday_publication_precision",
        "UNKNOWN",
        (
            "The 28 historical pages do not establish authoritative intraday "
            "first-publication timestamps."
        ),
    )
    update_subcheck(
        gates,
        "CORE02-Q2",
        "CORE02-Q2.date_level_pit_available_at",
        pit["available_at_status"],
        (
            "All 28 displayed page dates fail to prove first-publication. "
            "The legacy mechanical +1 day values remain inspection metadata "
            "and are not admitted as available_at."
        ),
    )
    q3_basis = (
        "Bounded mechanical recomputation from the live 8,454-row SSE/SZSE "
        "CSV. This is not the complete CORE-02 Q3 contract."
    )
    q4_basis = (
        "Recomputed from the live 8,454-row CSV: explicit SSE/SZSE venue "
        "dimension, 4,227 rows per venue, common date support and no "
        "composite-key collision."
    )
    for subcheck_id, mechanical_id in (
        (
            "CORE02-Q3.calendar_key_uniqueness",
            "calendar_key_uniqueness",
        ),
        (
            "CORE02-Q3.strict_open_sequence",
            "strict_open_sequence",
        ),
        (
            "CORE02-Q3.weekend_open_counterexample",
            "weekend_open_counterexample",
        ),
    ):
        update_subcheck(
            gates,
            "CORE02-Q3",
            subcheck_id,
            q3_q4["mechanical_subchecks"][mechanical_id],
            q3_basis,
        )
    for subcheck_id, requirement_id, basis in (
        (
            "CORE02-Q3.previous_next_trading_day_recomputable",
            "previous_next_trading_day_recomputable",
            (
                "The source CSV has no previous/next trading-day fields and "
                "the complete three-venue history required to recompute them "
                "is not closed."
            ),
        ),
        (
            "CORE02-Q3.month_quarter_year_end_markers_recomputable",
            "month_quarter_year_end_markers_recomputable",
            (
                "The source CSV has no month/quarter/year-end markers and the "
                "complete calendar history required to recompute them is not "
                "closed."
            ),
        ),
        (
            "CORE02-Q3.bse_calendar_coverage",
            "bse_calendar_coverage",
            "The 8,454-row source contains SSE and SZSE only; BSE is absent.",
        ),
        (
            "CORE02-Q3.historical_exchange_boundary_coverage",
            "historical_exchange_boundary_coverage",
            (
                "Exchange-inception and rule-effective historical boundaries "
                "remain incomplete or conditional."
            ),
        ),
    ):
        update_subcheck(
            gates,
            "CORE02-Q3",
            subcheck_id,
            q3_q4["q3_contract_requirements"][requirement_id],
            basis,
        )
    for subcheck_id in (
        "CORE02-Q4.cross_venue_values_not_silently_overwritten",
        "CORE02-Q4.venue_dimension_preserved",
    ):
        update_subcheck(
            gates,
            "CORE02-Q4",
            subcheck_id,
            q3_q4["Q4"],
            q4_basis,
        )
    gates["CORE02-Q1"]["status"] = "FAIL"
    gates["CORE02-Q2"]["status"] = "FAIL"
    gates["CORE02-Q3"]["status"] = q3_q4["Q3"]
    gates["CORE02-Q4"]["status"] = q3_q4["Q4"]
    decision = (
        "FAIL_CLOSED__Q1_FAIL__Q2_FAIL__Q3_UNKNOWN__Q4_PASS__"
        "STAGE_A_NOT_READY__NOT_ADMITTED"
    )
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "target": "CORE-02",
        "work_package": (
            "L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE"
        ),
        "decision": decision,
        "gates": gates,
        "purpose_ceiling": PURPOSE_CEILING,
        "quality_status": QUALITY_STATUS,
        "formal_admission": False,
        "research_usable": False,
        "catalog_update": False,
        "source_registry_update": False,
        "roadmap_update": False,
        "stage_a_status_changed": False,
    }


def bound_manifest(
    root: Path,
    workspace: Path,
    *,
    manifest_type: str,
    excluded_name: str,
) -> dict[str, Any]:
    excluded_path = root / excluded_name
    files = [
        file_binding(path, workspace)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != excluded_path
    ]
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "manifest_type": manifest_type,
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "files": files,
        "build_inputs": build_input_bindings(workspace),
        "formal_admission": False,
    }


def report_manifest(root: Path, workspace: Path) -> dict[str, Any]:
    return bound_manifest(
        root,
        workspace,
        manifest_type="REPORT",
        excluded_name="REPORT_MANIFEST.json",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument(
        "--reuse-existing-raw",
        action="store_true",
        help="Rebuild only from the already-preserved v2 raw responses.",
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    candidate = (workspace / args.candidate).resolve()
    report = (workspace / args.report).resolve()
    require_child(candidate, workspace / "tmp", "candidate")
    require_child(report, workspace / "reports", "report")

    required_reports = (BASELINE_REPORT, RULES_REPORT, MIC_REPORT)
    report_verification = [
        verify_report_manifest(workspace, item) for item in required_reports
    ]
    if any(item["status"] != "PASS" for item in report_verification):
        raise RuntimeError("a sealed upstream report manifest failed verification")

    if args.reuse_existing_raw:
        if not candidate.is_dir():
            raise FileNotFoundError(candidate)
        receipts = load_json(candidate / "REQUEST_RECEIPTS.json")
        if report.exists():
            raise FileExistsError(
                "reuse requires a new report path; candidate is read-only input"
            )
        require_new_directory(report)
    else:
        require_new_directory(candidate)
        require_new_directory(report)
        raw_root = candidate / "raw"
        raw_root.mkdir(parents=True, exist_ok=False)
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Quantification-CORE02-OfficialEvidence/2.0",
                "Accept": "text/html,application/xhtml+xml",
            }
        )
        receipts = [
            fetch_one(session, spec, raw_root, sequence)
            for sequence, spec in enumerate(SPECS, start=1)
        ]
        write_json(candidate / "REQUEST_RECEIPTS.json", receipts)

    before_guards = guard_hashes(workspace)
    raw_root = candidate / "raw"
    response_bindings = [
        receipt_binding(receipt, candidate, workspace) for receipt in receipts
    ]
    rule_bindings = sealed_rule_bindings(workspace)
    baseline_bindings = baseline_receipt_bindings(workspace)
    lineage_binding, lineage_rows = legacy_calendar_lineage_binding(workspace)
    iso_bindings = mic_bindings(workspace)

    source_manifest = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "path_contract": "CANONICAL_WORKSPACE_RELATIVE",
        "new_official_responses": response_bindings,
        "reused_sealed_rule_sources": rule_bindings,
        "reused_baseline_sources": baseline_bindings,
        "reused_calendar_lineage": lineage_binding,
        "reused_iso10383_sources": iso_bindings,
        "formal_admission": False,
    }
    session_chain = make_session_chain(rule_bindings, receipts)
    annual = annual_chain(receipts, lineage_rows, baseline_bindings)
    gate = build_gate_receipt(workspace, annual, session_chain)
    subchecks = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "target": "CORE-02",
        "aggregation_rule": (
            "PARTIAL is evidence-level only. Any required FAIL/UNKNOWN keeps "
            "the blocking gate fail-closed."
        ),
        "gates": gate["gates"],
    }
    temporary_gaps = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "known_hash_bound_evidence": [
            "SSE and SZSE 2019 Labour Day amendments",
            "SSE and SZSE 2020 Spring Festival extensions",
            "SSE 2015 index circuit-breaker mechanism notice",
            "BSE abnormal-trading/emergency procedure document",
        ],
        "unclosed": [
            "exhaustive three-venue temporary/emergency closure event archive",
            "first-publication/correction/supersedes/withdrawal chain",
            "authoritative intraday publication timestamps",
            "BSE actual-event archive",
        ],
        "status": "FAIL_CLOSED",
        "boundary": (
            "A procedure or one known event cannot establish the absence of "
            "other historical events."
        ),
    }
    evidence_map = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "Q1": {
            "new_response_ids": [
                item["request_id"] for item in receipts
            ],
            "session_chain_file": "SESSION_VERSION_CHAIN.json",
            "MIC_package": MIC_REPORT,
            "status": "FAIL",
        },
        "Q2": {
            "annual_chain_file": "ANNUAL_HOLIDAY_NOTICE_CHAIN.json",
            "temporary_gap_file": "TEMPORARY_CLOSURE_GAPS.json",
            "status": "FAIL",
        },
        "Q3_Q4": {
            "basis": BASELINE_REPORT,
            "status": "INHERITED_PASS",
        },
    }
    stage_delta = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "target": "CORE-02",
        "before": {
            "Q1": "FAIL",
            "Q2": "FAIL",
            "Q3": "PASS",
            "Q4": "PASS",
            "Stage_A": "NOT_READY",
        },
        "after": {
            "Q1": "FAIL",
            "Q2": "FAIL",
            "Q3": "PASS",
            "Q4": "PASS",
            "Stage_A": "NOT_READY",
        },
        "change": "NO_GATE_STATUS_CHANGE__EVIDENCE_GAPS_NARROWED",
        "formal_admission": False,
        "research_usable": False,
    }

    if not args.reuse_existing_raw:
        write_json(candidate / "SOURCE_MANIFEST.json", source_manifest)
        candidate_files = [
            file_binding(path, workspace)
            for path in sorted(candidate.rglob("*"))
            if path.is_file() and path.name != "CANDIDATE_MANIFEST.json"
        ]
        write_json(
            candidate / "CANDIDATE_MANIFEST.json",
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "purpose_ceiling": PURPOSE_CEILING,
                "quality_status": QUALITY_STATUS,
                "formal_admission": False,
                "files": candidate_files,
            },
        )

    write_json(report / "REQUEST_RECEIPTS.json", receipts)
    write_json(report / "SOURCE_MANIFEST.json", source_manifest)
    write_json(report / "SESSION_VERSION_CHAIN.json", session_chain)
    write_json(report / "ANNUAL_HOLIDAY_NOTICE_CHAIN.json", annual)
    write_json(report / "TEMPORARY_CLOSURE_GAPS.json", temporary_gaps)
    write_json(report / "OFFICIAL_EVIDENCE_MAP.json", evidence_map)
    write_json(report / "SUBCHECK_EVIDENCE_MATRIX.json", subchecks)
    write_json(report / "CORE02_GATE_RECEIPT.json", gate)
    write_json(report / "STAGE_A_STATUS_DELTA.json", stage_delta)

    after_guards = guard_hashes(workspace)
    guard_unchanged = before_guards == after_guards
    response_hashes_ok = all(
        sha256_file(raw_root / item["relative_path"])
        == item["response_sha256"]
        for item in receipts
    )
    source_manifest_live = verify_source_manifest_live(
        workspace, candidate, report
    )
    verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "generated_at_asia_shanghai": now_shanghai(),
        "sealed_upstream_reports": report_verification,
        "official_request_count": len(receipts),
        "response_body_hashes_verified": response_hashes_ok,
        "source_manifest_live_recursive_validation": source_manifest_live,
        "transport_boundary": dict(TRANSPORT_BOUNDARY),
        "new_response_result_counts": {
            value: sum(1 for item in receipts if item["result"] == value)
            for value in sorted({item["result"] for item in receipts})
        },
        "reused_rule_source_count": len(rule_bindings),
        "reused_rule_source_hashes_verified": True,
        "reused_baseline_source_count": len(baseline_bindings),
        "reused_baseline_source_hashes_verified": True,
        "reused_calendar_lineage_rows": len(lineage_rows),
        "reused_calendar_raw_hashes_verified": True,
        "reused_iso_source_count": len(iso_bindings),
        "reused_iso_source_hashes_verified": True,
        "formal_guards_unchanged": guard_unchanged,
        "before_guards": before_guards,
        "after_guards": after_guards,
        "purpose_ceiling": PURPOSE_CEILING,
        "quality_status": QUALITY_STATUS,
        "formal_admission": False,
    }
    write_json(report / "VERIFICATION.json", verification)
    readme = f"""# CORE-02 官方日历/交易时段收口 v2

- 运行：`{RUN_ID}`
- 结论：`Q1=FAIL`、`Q2=FAIL`、`Q3=PASS`、`Q4=PASS`。
- 状态：`INSPECTION_ONLY / NOT_ADMITTED`，不构成正式准入或研究输入。
- 新增：SSE 2013/2014 年度休市页、SZSE 2013 年度页与 2014 两个分节页、SZSE 2006 交易规则通知、BSE 2024 年度页的单次官方公开请求；成功与失败响应均按 SHA-256 封存。
- 复用：已封存的 SSE/SZSE/BSE 交易规则、2015—2026 日历公告链、BSE 2025/2026 公告及 ISO 10383 MIC 证据，均按原路径和哈希引用。
- 未闭合：三市场完整历史交易时段版本链、BSE 完整日历、三市场临时/应急闭市全集、公告首发/更正/替代/撤回链以及权威盘中发布时间。
- 边界：没有修改 catalog、source registry、Roadmap、项目登记、正式快照、published 或质量状态。
"""
    (report / "README.md").write_text(readme, encoding="utf-8")
    write_json(report / "REPORT_MANIFEST.json", report_manifest(report, workspace))

    if not response_hashes_ok:
        raise RuntimeError("a preserved response hash failed verification")
    if not guard_unchanged:
        raise RuntimeError("a guarded formal file/tree changed during the run")
    print(
        json.dumps(
            {
                "run_id": RUN_ID,
                "candidate": workspace_relative(candidate, workspace),
                "report": workspace_relative(report, workspace),
                "requests": len(receipts),
                "decision": gate["decision"],
                "guarded_unchanged": guard_unchanged,
            },
            ensure_ascii=False,
        )
    )
    return 0


def verify_existing_artifacts(
    workspace: Path, candidate: Path, report: Path
) -> dict[str, Any]:
    """Verify the repaired artifact set without any write or network call."""
    errors: list[str] = []
    candidate_check = validate_bound_manifest(
        workspace,
        candidate,
        "CANDIDATE_MANIFEST.json",
        expected_candidate_files(),
    )
    report_check = validate_bound_manifest(
        workspace,
        report,
        "REPORT_MANIFEST.json",
        EXPECTED_REPORT_FILES,
    )
    errors.extend(candidate_check["errors"])
    errors.extend(report_check["errors"])

    candidate_receipts_path = candidate / "REQUEST_RECEIPTS.json"
    report_receipts_path = report / "REQUEST_RECEIPTS.json"
    receipts_equal = (
        sha256_file(candidate_receipts_path)
        == sha256_file(report_receipts_path)
    )
    if not receipts_equal:
        errors.append("candidate/report request receipt bytes differ")
    receipts = load_json(candidate_receipts_path)
    if len(receipts) != 7:
        errors.append(f"request receipt count is {len(receipts)}, expected 7")
    response_hashes_ok = all(
        sha256_file(candidate / "raw" / item["relative_path"])
        == item["response_sha256"]
        for item in receipts
    )
    if not response_hashes_ok:
        errors.append("preserved response body hash mismatch")

    source_manifest_live = verify_source_manifest_live(
        workspace, candidate, report
    )
    errors.extend(source_manifest_live["errors"])

    live_q3_q4 = recompute_q3_q4(workspace)
    stored_q3_q4 = load_json(report / "Q3_Q4_LIVE_RECOMPUTE.json")
    live_q3_q4_match = live_q3_q4 == stored_q3_q4
    if not live_q3_q4_match:
        errors.append("live Q3/Q4 recomputation differs from stored evidence")

    verification = load_json(report / "VERIFICATION.json")
    current_guards = guard_hashes(workspace)
    guards_match = current_guards == verification["after_guards"]
    if not guards_match:
        errors.append("current formal guards differ from recorded after_guards")

    repair_receipt = load_json(report / "QA_REPAIR_RECEIPT.json")
    claimed_payload_hash = repair_receipt.get("receipt_payload_sha256")
    unsigned_receipt = dict(repair_receipt)
    unsigned_receipt.pop("receipt_payload_sha256", None)
    payload_hash_ok = claimed_payload_hash == sha256_bytes(
        canonical_json_bytes(unsigned_receipt)
    )
    if not payload_hash_ok:
        errors.append("QA repair receipt payload hash mismatch")

    test_results_path = report / TEST_RESULTS_NAME
    test_results = load_json(test_results_path)
    expected_command = [str(workspace / "control/runtime/data-engineering/.venv/Scripts/python.exe"), str(workspace / TEST_PATH)]
    test_results_ok = (
        test_results.get("status") == "PASS" and test_results.get("exit_code") == 0
        and test_results.get("tests_run") == 15 and test_results.get("bootstrap") is False
        and test_results.get("exact_command") == expected_command
        and test_results.get("network_requests_performed") == 0
        and test_results.get("zero_network_declaration") is True
        and test_results.get("artifacts") == {"builder": file_binding(workspace / GENERATOR_PATH, workspace), "test": file_binding(workspace / TEST_PATH, workspace), "runner": file_binding(workspace / TEST_RUNNER_PATH, workspace)}
    )
    if not test_results_ok:
        errors.append("TEST_RESULTS receipt is not a current passing local run")
    test_binding = file_binding(test_results_path, workspace)
    if verification.get("test_results") != test_binding:
        errors.append("VERIFICATION does not bind TEST_RESULTS")
    if repair_receipt.get("test_results") != test_binding:
        errors.append("QA repair receipt does not bind TEST_RESULTS")

    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "mode": "VERIFY_ONLY",
        "candidate_manifest": candidate_check,
        "report_manifest": report_check,
        "request_receipts_byte_identical": receipts_equal,
        "official_request_count": len(receipts),
        "response_body_hashes_verified": response_hashes_ok,
        "source_manifest_live_recursive_validation": source_manifest_live,
        "live_q3_q4_match": live_q3_q4_match,
        "live_guards_match": guards_match,
        "qa_receipt_payload_hash_verified": payload_hash_ok,
        "test_results_verified": test_results_ok,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "writes_performed": 0,
        "network_requests_performed": 0,
    }


def main_v2() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--repair-existing",
        action="store_true",
        help="Repair derived v2 evidence in place with zero network requests.",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify strict artifact sets without writes or networking.",
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    candidate = (workspace / args.candidate).resolve()
    report = (workspace / args.report).resolve()
    require_child(candidate, workspace / "tmp", "candidate")
    require_child(report, workspace / "reports", "report")

    if args.verify_only:
        result = verify_existing_artifacts(workspace, candidate, report)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if result["status"] != "PASS":
            raise RuntimeError("; ".join(result["errors"]))
        return 0

    if not candidate.is_dir() or not report.is_dir():
        raise FileNotFoundError("repair requires existing candidate and report")
    reject_extra_or_missing(
        candidate,
        expected_candidate_files()
        if (candidate / REPAIR_HISTORY_DIR).is_dir()
        else base_candidate_files(),
    )
    reject_extra_or_missing(
        report,
        (EXPECTED_REPORT_FILES - {f"{REPAIR_HISTORY_DIR}/{TEST_RESULTS_NAME}"})
        if (report / REPAIR_HISTORY_DIR).is_dir() else BASE_REPORT_FILES,
        allow_missing={
            "ANNUAL_COMPLETENESS_EVIDENCE.json",
            "PIT_PUBLICATION_ASSESSMENT.json",
            "Q3_Q4_LIVE_RECOMPUTE.json",
            "QA_REPAIR_RECEIPT.json",
            TEST_RESULTS_NAME,
        },
    )
    candidate_receipts_path = candidate / "REQUEST_RECEIPTS.json"
    report_receipts_path = report / "REQUEST_RECEIPTS.json"
    if sha256_file(candidate_receipts_path) != sha256_file(
        report_receipts_path
    ):
        raise RuntimeError("candidate/report request receipt copies differ")
    receipts = load_json(candidate_receipts_path)
    if len(receipts) != 7:
        raise RuntimeError("repair requires the original seven receipts")

    required_reports = (BASELINE_REPORT, RULES_REPORT, MIC_REPORT)
    report_verification = [
        verify_report_manifest(workspace, item) for item in required_reports
    ]
    if any(item["status"] != "PASS" for item in report_verification):
        raise RuntimeError("a sealed upstream report manifest failed verification")

    raw_root = candidate / "raw"
    raw_before = {
        workspace_relative(path, workspace): sha256_file(path)
        for path in sorted(raw_root.rglob("*"))
        if path.is_file()
    }
    candidate_receipt_before = file_binding(
        candidate_receipts_path, workspace
    )
    report_receipt_before = file_binding(report_receipts_path, workspace)
    before_guards = guard_hashes(workspace)

    response_bindings = [
        receipt_binding(receipt, candidate, workspace) for receipt in receipts
    ]
    rule_bindings = sealed_rule_bindings(workspace)
    baseline_bindings = baseline_receipt_bindings(workspace)
    lineage_binding, lineage_rows = legacy_calendar_lineage_binding(workspace)
    iso_bindings = mic_bindings(workspace)
    source_manifest = compose_source_manifest(
        workspace,
        response_bindings,
        rule_bindings,
        baseline_bindings,
        lineage_binding,
        iso_bindings,
    )
    annual_evidence = annual_completeness_evidence(
        workspace, candidate, receipts, lineage_rows
    )
    pit = pit_publication_assessment(workspace, lineage_rows)
    q3_q4 = recompute_q3_q4(workspace)
    session_chain = make_session_chain(
        workspace, rule_bindings, receipts, response_bindings
    )
    annual = annual_chain(annual_evidence, baseline_bindings)
    gate = build_gate_receipt(
        workspace, annual, session_chain, pit, q3_q4
    )
    subchecks = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "target": "CORE-02",
        "aggregation_rule": (
            "PARTIAL is evidence-level only. Any required FAIL/UNKNOWN keeps "
            "the blocking gate fail-closed."
        ),
        "gates": gate["gates"],
    }
    temporary_gaps = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "known_hash_bound_evidence": [
            "SSE and SZSE 2019 Labour Day amendments",
            "SSE and SZSE 2020 Spring Festival extensions",
            "SSE 2015 index circuit-breaker mechanism notice",
            "BSE abnormal-trading/emergency procedure document",
        ],
        "unclosed": [
            "exhaustive three-venue temporary/emergency closure event archive",
            "first-publication/correction/supersedes/withdrawal chain",
            "authoritative intraday publication timestamps",
            "BSE actual-event archive",
        ],
        "status": "FAIL_CLOSED",
        "boundary": (
            "A procedure or one known event cannot establish the absence of "
            "other historical events."
        ),
    }
    evidence_map = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "Q1": {
            "new_response_ids": [item["request_id"] for item in receipts],
            "session_chain_file": "SESSION_VERSION_CHAIN.json",
            "effective_date_matrix": EFFECTIVE_DATE_MATRIX,
            "MIC_package": MIC_REPORT,
            "status": "FAIL",
        },
        "Q2": {
            "annual_chain_file": "ANNUAL_HOLIDAY_NOTICE_CHAIN.json",
            "annual_completeness_file": "ANNUAL_COMPLETENESS_EVIDENCE.json",
            "pit_assessment_file": "PIT_PUBLICATION_ASSESSMENT.json",
            "temporary_gap_file": "TEMPORARY_CLOSURE_GAPS.json",
            "status": "FAIL",
        },
        "Q3_Q4": {
            "basis": "Q3_Q4_LIVE_RECOMPUTE.json",
            "input": LIVE_CALENDAR_CSV,
            "Q3": q3_q4["Q3"],
            "Q4": q3_q4["Q4"],
            "status": "LIVE_RECOMPUTED",
        },
    }
    stage_delta = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "target": "CORE-02",
        "before": {
            "Q1": "FAIL",
            "Q2": "FAIL",
            "Q3": "PASS",
            "Q4": "PASS",
            "Stage_A": "NOT_READY",
        },
        "after": {
            "Q1": "FAIL",
            "Q2": "FAIL",
            "Q3": q3_q4["Q3"],
            "Q4": q3_q4["Q4"],
            "Stage_A": "NOT_READY",
        },
        "change": (
            "CORRECTED_Q3_PASS_TO_UNKNOWN__LOCAL_MECHANICAL_SUBCHECKS_"
            "RETAINED__STAGE_A_STILL_NOT_READY"
        ),
        "formal_admission": False,
        "research_usable": False,
    }

    repair_history = preserve_pre_second_repair(
        workspace, candidate, report
    )
    write_json(candidate / "SOURCE_MANIFEST.json", source_manifest)
    candidate_manifest = bound_manifest(
        candidate,
        workspace,
        manifest_type="CANDIDATE",
        excluded_name="CANDIDATE_MANIFEST.json",
    )
    candidate_manifest.update(
        {
            "purpose_ceiling": PURPOSE_CEILING,
            "quality_status": QUALITY_STATUS,
        }
    )
    write_json(candidate / "CANDIDATE_MANIFEST.json", candidate_manifest)
    candidate_manifest_check = validate_bound_manifest(
        workspace,
        candidate,
        "CANDIDATE_MANIFEST.json",
        expected_candidate_files(),
    )
    if candidate_manifest_check["status"] != "PASS":
        raise RuntimeError(str(candidate_manifest_check["errors"]))

    write_json(report / "SOURCE_MANIFEST.json", source_manifest)
    write_json(report / "SESSION_VERSION_CHAIN.json", session_chain)
    write_json(report / "ANNUAL_COMPLETENESS_EVIDENCE.json", annual_evidence)
    write_json(report / "ANNUAL_HOLIDAY_NOTICE_CHAIN.json", annual)
    write_json(report / "PIT_PUBLICATION_ASSESSMENT.json", pit)
    write_json(report / "Q3_Q4_LIVE_RECOMPUTE.json", q3_q4)
    write_json(report / "TEMPORARY_CLOSURE_GAPS.json", temporary_gaps)
    write_json(report / "OFFICIAL_EVIDENCE_MAP.json", evidence_map)
    write_json(report / "SUBCHECK_EVIDENCE_MATRIX.json", subchecks)
    write_json(report / "CORE02_GATE_RECEIPT.json", gate)
    write_json(report / "STAGE_A_STATUS_DELTA.json", stage_delta)

    after_guards = guard_hashes(workspace)
    raw_after = {
        workspace_relative(path, workspace): sha256_file(path)
        for path in sorted(raw_root.rglob("*"))
        if path.is_file()
    }
    response_hashes_ok = all(
        sha256_file(raw_root / item["relative_path"])
        == item["response_sha256"]
        for item in receipts
    )
    source_manifest_live = verify_source_manifest_live(
        workspace, candidate, report
    )
    write_json(report / "REPORT_MANIFEST.json", report_manifest(report, workspace))
    runner_base = [str(workspace / "control/runtime/data-engineering/.venv/Scripts/python.exe"), str(workspace / TEST_RUNNER_PATH), "--workspace", str(workspace), "--output", str(report / TEST_RESULTS_NAME)]
    for command in (runner_base + ["--bootstrap"], runner_base):
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"local TEST_RESULTS runner failed: {completed.stderr.strip()}")
        write_json(report / "REPORT_MANIFEST.json", report_manifest(report, workspace))

    verification = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "generated_at_asia_shanghai": now_shanghai(),
        "operation_mode": "OFFLINE_REPAIR_EXISTING",
        "network_requests_this_execution": 0,
        "sealed_upstream_reports": report_verification,
        "official_request_count": len(receipts),
        "response_body_hashes_verified": response_hashes_ok,
        "source_manifest_live_recursive_validation": source_manifest_live,
        "transport_boundary": dict(TRANSPORT_BOUNDARY),
        "raw_tree_hashes_unchanged": raw_before == raw_after,
        "request_receipts_unchanged": (
            candidate_receipt_before
            == file_binding(candidate_receipts_path, workspace)
            and report_receipt_before
            == file_binding(report_receipts_path, workspace)
        ),
        "new_response_result_counts": {
            value: sum(1 for item in receipts if item["result"] == value)
            for value in sorted({item["result"] for item in receipts})
        },
        "reused_rule_source_count": len(rule_bindings),
        "reused_rule_source_hashes_verified": True,
        "reused_baseline_source_count": len(baseline_bindings),
        "reused_baseline_source_hashes_verified": True,
        "reused_calendar_lineage_rows": len(lineage_rows),
        "reused_calendar_raw_hashes_verified": True,
        "reused_iso_source_count": len(iso_bindings),
        "reused_iso_source_hashes_verified": True,
        "candidate_manifest_validation": candidate_manifest_check,
        "live_q3_q4": {"Q3": q3_q4["Q3"], "Q4": q3_q4["Q4"]},
        "formal_guards_unchanged": before_guards == after_guards,
        "before_guards": before_guards,
        "after_guards": after_guards,
        "purpose_ceiling": PURPOSE_CEILING,
        "quality_status": QUALITY_STATUS,
        "formal_admission": False,
        "test_results": file_binding(report / TEST_RESULTS_NAME, workspace),
    }
    write_json(report / "VERIFICATION.json", verification)

    repair_receipt = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "repair_id": SECOND_REPAIR_ID,
        "operation_mode": "OFFLINE_REPAIR_EXISTING",
        "network_requests_this_execution": 0,
        "raw_before": raw_before,
        "raw_after": raw_after,
        "raw_byte_identical": raw_before == raw_after,
        "candidate_request_receipt_before": candidate_receipt_before,
        "candidate_request_receipt_after": file_binding(
            candidate_receipts_path, workspace
        ),
        "report_request_receipt_before": report_receipt_before,
        "report_request_receipt_after": file_binding(
            report_receipts_path, workspace
        ),
        "build_inputs": build_input_bindings(workspace),
        "repair_history": repair_history,
        "transport_boundary": dict(TRANSPORT_BOUNDARY),
        "source_manifest_live_recursive_validation": source_manifest_live,
        "candidate_manifest": file_binding(
            candidate / "CANDIDATE_MANIFEST.json", workspace
        ),
        "test_results": file_binding(report / TEST_RESULTS_NAME, workspace),
        "repaired_outputs": [
            file_binding(report / name, workspace)
            for name in (
                "SESSION_VERSION_CHAIN.json",
                "ANNUAL_COMPLETENESS_EVIDENCE.json",
                "ANNUAL_HOLIDAY_NOTICE_CHAIN.json",
                "PIT_PUBLICATION_ASSESSMENT.json",
                "Q3_Q4_LIVE_RECOMPUTE.json",
                "CORE02_GATE_RECEIPT.json",
                "VERIFICATION.json",
                TEST_RESULTS_NAME,
            )
        ],
        "formal_guards_unchanged": before_guards == after_guards,
        "gate_result": {
            "Q1": "FAIL",
            "Q2": "FAIL",
            "Q3": q3_q4["Q3"],
            "Q4": q3_q4["Q4"],
            "Stage_A": "NOT_READY",
        },
        "formal_admission": False,
        "status": "PASS" if (
            raw_before == raw_after
            and before_guards == after_guards
            and response_hashes_ok
            and source_manifest_live["status"] == "PASS"
            and candidate_manifest_check["status"] == "PASS"
        ) else "FAIL",
    }
    repair_receipt["receipt_payload_sha256"] = sha256_bytes(
        canonical_json_bytes(repair_receipt)
    )
    write_json(report / "QA_REPAIR_RECEIPT.json", repair_receipt)

    readme = f"""# CORE-02 official calendar/session closure v2

- Run: `{RUN_ID}`
- Gate: `Q1=FAIL`, `Q2=FAIL`, `Q3={q3_q4["Q3"]}`, `Q4={q3_q4["Q4"]}`.
- Stage A remains `NOT_READY`; this is `INSPECTION_ONLY / NOT_ADMITTED`.
- The seven original HTTP responses and both request-receipt copies were
  preserved byte-for-byte during offline repair.
- Historical rule boundaries follow the newer effective-date matrix. Unknown
  intermediate revisions remain `UNKNOWN` or `CONDITIONAL`; no continuous
  exact interval is inferred.
- Annual master pages must cover all seven holiday classes. Two SZSE 2014
  fragments are never counted as annual masters.
- The 8,454-row calendar CSV is recomputed live. Its bounded SSE/SZSE key,
  sequence and weekend subchecks pass, but Q3 remains `UNKNOWN`: previous/next
  trading days, month/quarter/year-end markers, BSE coverage and historical
  exchange boundaries are absent or unproven. Q4 remains `PASS`.
- The application did not explicitly configure credentials, cookies or an
  Authorization header. Proxy and credential origin remain `UNKNOWN`; this
  package does not claim a direct connection or absolute absence of transport
  credentials.
- The 28 displayed historical page dates do not prove first publication.
  Mechanical `+1 day` values remain inspection metadata; date-level and
  intraday PIT stay `UNKNOWN`, so Q2 remains `FAIL`.
- No catalog, source registry, Roadmap, project index, formal snapshot,
  published tree or quality state was changed.
"""
    (report / "README.md").write_text(readme, encoding="utf-8")
    write_json(report / "REPORT_MANIFEST.json", report_manifest(report, workspace))

    if not response_hashes_ok:
        raise RuntimeError("a preserved response hash failed verification")
    if raw_before != raw_after:
        raise RuntimeError("a preserved raw response changed during the run")
    if before_guards != after_guards:
        raise RuntimeError("a guarded formal file/tree changed during the run")
    result = verify_existing_artifacts(workspace, candidate, report)
    if result["status"] != "PASS":
        raise RuntimeError("; ".join(result["errors"]))
    print(
        json.dumps(
            {
                "run_id": RUN_ID,
                "candidate": workspace_relative(candidate, workspace),
                "report": workspace_relative(report, workspace),
                "network_requests_this_execution": 0,
                "decision": gate["decision"],
                "guarded_unchanged": True,
                "verify_only_equivalent": "PASS",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main_v2())
