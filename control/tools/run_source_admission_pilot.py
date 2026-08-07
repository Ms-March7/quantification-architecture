#!/usr/bin/env python3
"""Run the bounded real-source L1-D2A-01 admission pilot.

The runner captures only explicitly approved public-web evidence and writes
only below ``tmp/source-admission-pilot/<run_id>``.  It never writes formal
data, changes the catalog, fetches exchange daily prices, downloads CNINFO PDF
bodies, uses credentials, or performs a historical backfill.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ALLOWED_HOSTS = {
    "query.sse.com.cn",
    "www.sse.com.cn",
    "star.sse.com.cn",
    "www.szse.cn",
    "www.bse.cn",
    "www.cninfo.com.cn",
}
FORBIDDEN_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
}
FORBIDDEN_CREDENTIAL_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "cookie",
    "key",
    "password",
    "secret",
    "signature",
    "token",
}
ACTIVE_MATRIX_STATUS = "ACTIVE__PILOT_ONLY__NO_FORMAL_ADMISSION"
EXECUTABLE_BUSINESS_PROBE_IDS = {
    "sse_security_master_sample",
    "sse_2026_calendar_notice",
    "szse_security_master_current_export",
    "szse_2026_calendar_notice",
    "cninfo_corporate_action_metadata_sample",
}
ALLOWED_PILOT_DECISIONS = {
    "PILOT_ALLOWED__NONCOMMERCIAL_PUBLIC_WEB",
    "BLOCKED_BEFORE_BUSINESS_DATA_FETCH",
    "BLOCKED_AFTER_LOGIN__FIRST_COMPRESSED_BUSINESS_RESPONSE_FRAME_UNRESOLVED",
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolved_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def tree_fingerprint(root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return {
        "exists": root.exists(),
        "file_count": len(records),
        "total_bytes": sum(item["bytes"] for item in records),
        "contract_sha256": hashlib.sha256(canonical_json(records)).hexdigest(),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    if partial.exists() or path.exists():
        raise ValueError(f"refusing to overwrite evidence metadata: {path}")
    try:
        partial.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        partial.replace(path)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise


def load_source_matrix(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    required_top = {
        "schema_version",
        "contract_id",
        "work_package",
        "status",
        "execution_authorization_active",
        "resume_requires_user_start",
        "global_purpose_ceiling",
        "formal_admission",
        "bulk_backfill_authorized",
        "quality_promotion_authorized",
        "sources",
    }
    missing = sorted(required_top - set(value))
    if missing:
        raise ValueError(f"source matrix missing keys: {missing}")
    if value["schema_version"] != 1:
        raise ValueError("source matrix schema_version must be 1")
    if value["global_purpose_ceiling"] != "INSPECTION_ONLY":
        raise ValueError("global purpose ceiling must remain INSPECTION_ONLY")
    if not isinstance(value["execution_authorization_active"], bool):
        raise ValueError("execution_authorization_active must be boolean")
    if not isinstance(value["resume_requires_user_start"], bool):
        raise ValueError("resume_requires_user_start must be boolean")
    if (
        value["formal_admission"]
        or value["bulk_backfill_authorized"]
        or value["quality_promotion_authorized"]
    ):
        raise ValueError("pilot contract must not authorize formal actions")
    source_keys: set[str] = set()
    declared_business_probe_ids: set[str] = set()
    for source in value["sources"]:
        required_source = {
            "source_key",
            "source_id",
            "plan_ids",
            "pilot_decision",
            "business_probe_ids",
            "purpose_ceiling",
            "formal_admission",
            "formal_or_bulk_status",
            "rights",
            "pit_status",
            "field_semantics_status",
            "legal_evidence_urls",
            "blockers",
        }
        source_missing = sorted(required_source - set(source))
        if source_missing:
            raise ValueError(
                f"{source.get('source_key', '<unknown>')} missing keys: "
                f"{source_missing}"
            )
        if source["source_key"] in source_keys:
            raise ValueError(f"duplicate source_key: {source['source_key']}")
        source_keys.add(source["source_key"])
        if source["formal_admission"]:
            raise ValueError(
                f"{source['source_key']} cannot grant formal admission"
            )
        if source["pilot_decision"] not in ALLOWED_PILOT_DECISIONS:
            raise ValueError(
                f"{source['source_key']} has unsupported pilot decision"
            )
        business_probe_ids = source["business_probe_ids"]
        if not isinstance(business_probe_ids, list) or not all(
            isinstance(item, str) for item in business_probe_ids
        ):
            raise ValueError(
                f"{source['source_key']} business_probe_ids must be strings"
            )
        if (
            source["pilot_decision"].startswith("BLOCKED_")
            and business_probe_ids
        ):
            raise ValueError(
                f"{source['source_key']} is blocked but declares business probes"
            )
        overlap = declared_business_probe_ids.intersection(business_probe_ids)
        if overlap:
            raise ValueError(f"duplicate business_probe_ids: {sorted(overlap)}")
        declared_business_probe_ids.update(business_probe_ids)
    if declared_business_probe_ids != EXECUTABLE_BUSINESS_PROBE_IDS:
        raise ValueError(
            "source matrix business probe plan differs from the executable "
            f"plan: declared={sorted(declared_business_probe_ids)}, "
            f"executable={sorted(EXECUTABLE_BUSINESS_PROBE_IDS)}"
        )
    return value


def assert_execution_authorized(matrix: dict[str, Any]) -> None:
    if (
        matrix["status"] != ACTIVE_MATRIX_STATUS
        or not matrix["execution_authorization_active"]
    ):
        raise ValueError(
            "source pilot execution is paused; explicit user restart and "
            "reviewed contract reactivation are required"
        )


def summarize_cninfo_time_precision(rows: list[dict[str, Any]]) -> dict[str, Any]:
    timezone = ZoneInfo("Asia/Shanghai")
    midnight = 0
    non_midnight = 0
    missing_or_invalid = 0
    for row in rows:
        value = row.get("announcementTime")
        try:
            local = dt.datetime.fromtimestamp(float(value) / 1000, timezone)
        except (TypeError, ValueError, OSError, OverflowError):
            missing_or_invalid += 1
            continue
        if local.time() == dt.time():
            midnight += 1
        else:
            non_midnight += 1
    observed = midnight + non_midnight
    return {
        "timezone_used_for_observation": "Asia/Shanghai",
        "rows_observed": observed,
        "midnight_rows": midnight,
        "non_midnight_rows": non_midnight,
        "missing_or_invalid_rows": missing_or_invalid,
        "midnight_share": round(midnight / observed, 6) if observed else None,
        "decision": (
            "MIXED_OR_DATE_ONLY_PRECISION_RISK__DO_NOT_TREAT_ALL_VALUES_AS_EXACT_TIME"
            if midnight
            else "NO_MIDNIGHT_IN_THIS_SAMPLE__PRECISION_STILL_UNPROVEN"
        ),
    }


def validate_utf8_markers(path: Path, expected_markers: list[str]) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return {
            "utf8_decoded": False,
            "expected_markers": expected_markers,
            "missing_markers": expected_markers,
            "error": str(exc),
            "passed": False,
        }
    folded_text = text.casefold()
    missing = [
        marker for marker in expected_markers if marker.casefold() not in folded_text
    ]
    return {
        "utf8_decoded": True,
        "expected_markers": expected_markers,
        "missing_markers": missing,
        "passed": not missing,
    }


def validate_request(
    method: str,
    url: str,
    headers: dict[str, str] | None,
    output_path: Path,
    output_root: Path,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"endpoint is outside the HTTPS allowlist: {url}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL userinfo credentials are forbidden")
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query)}
    forbidden_query = sorted(query_keys & FORBIDDEN_CREDENTIAL_KEYS)
    if forbidden_query:
        raise ValueError(
            f"credential-bearing URL query keys are forbidden: {forbidden_query}"
        )
    if not resolved_under(output_path, output_root):
        raise ValueError("response path must stay inside the pilot run")
    if method.upper() not in {"GET", "POST"}:
        raise ValueError(f"unsupported request method: {method}")
    supplied = {key.lower() for key in (headers or {})}
    forbidden = sorted(supplied & FORBIDDEN_REQUEST_HEADERS)
    if forbidden:
        raise ValueError(f"credential-bearing headers are forbidden: {forbidden}")


def validate_payload_keys(values: dict[str, str] | None, role: str) -> None:
    supplied = {key.casefold() for key in (values or {})}
    forbidden = sorted(supplied & FORBIDDEN_CREDENTIAL_KEYS)
    if forbidden:
        raise ValueError(
            f"credential-bearing {role} keys are forbidden: {forbidden}"
        )


def harden_session(session: Any) -> dict[str, bool]:
    session.trust_env = False
    session.auth = None
    session.proxies.clear()
    session.cookies.clear()
    for header in list(session.headers):
        if header.casefold() in FORBIDDEN_REQUEST_HEADERS:
            session.headers.pop(header, None)
    return {
        "environment_proxy_and_netrc_disabled": session.trust_env is False,
        "session_auth_absent": session.auth is None,
        "session_proxies_empty": not session.proxies,
        "session_cookies_empty": not session.cookies,
    }


def bounded_request(
    session: Any,
    *,
    method: str,
    url: str,
    output_path: Path,
    output_root: Path,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    validate_request(method, url, headers, output_path, output_root)
    validate_payload_keys(params, "parameter")
    validate_payload_keys(data, "form")
    if max_bytes <= 0 or max_bytes > MAX_RESPONSE_BYTES:
        raise ValueError("invalid response byte cap")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_suffix(output_path.suffix + ".part")
    if partial.exists() or output_path.exists():
        raise ValueError(f"refusing to overwrite pilot evidence: {output_path}")
    started = time.perf_counter()
    response = session.request(
        method.upper(),
        url,
        headers=headers,
        params=params,
        data=data,
        timeout=(10, 30),
        allow_redirects=False,
        stream=True,
    )
    try:
        if 300 <= int(response.status_code) < 400:
            raise ValueError(
                "HTTP redirects are forbidden; review and allowlist the exact "
                f"destination before retrying: {response.headers.get('Location')}"
            )
        response.raise_for_status()
        declared_length = response.headers.get("Content-Length")
        if declared_length and int(declared_length) > max_bytes:
            raise ValueError(
                f"declared response exceeds cap: {declared_length} > {max_bytes}"
            )
        total = 0
        with partial.open("xb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        f"streamed response exceeds cap: {total} > {max_bytes}"
                    )
                handle.write(chunk)
        partial.replace(output_path)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    finally:
        response.close()
    return {
        "status": "HTTP_CAPTURED",
        "method": method.upper(),
        "request_url": url,
        "final_url": response.url,
        "request_params": params or {},
        "request_form": data or {},
        "request_form_keys": sorted((data or {}).keys()),
        "request_form_sha256": hashlib.sha256(
            canonical_json(data or {})
        ).hexdigest(),
        "retrieved_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status_code": int(response.status_code),
        "content_type": response.headers.get("Content-Type"),
        "content_length_header": response.headers.get("Content-Length"),
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
        "bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "relative_evidence_path": output_path.relative_to(output_root).as_posix(),
    }


def run_probe(
    probes: dict[str, Any],
    probe_id: str,
    operation: Any,
) -> None:
    started = time.perf_counter()
    try:
        result = operation()
        validation_passed = result.get("capture_validation_passed") is True
        probes[probe_id] = {
            "status": (
                "PASS_FOR_BOUNDED_CAPTURE"
                if validation_passed
                else "CONTENT_VALIDATION_FAILED"
            ),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            **result,
        }
    except Exception as exc:
        probes[probe_id] = {
            "status": "ERROR",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def probe_sse_security_master(session: Any, output: Path) -> dict[str, Any]:
    path = output / "business" / "sse-security-master-page-1.json"
    request = bounded_request(
        session,
        method="GET",
        url="https://query.sse.com.cn/sseQuery/commonQuery.do",
        output_path=path,
        output_root=output,
        headers={
            "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
            "User-Agent": "Quantification L1-D2A-01 bounded noncommercial pilot",
        },
        params={
            "STOCK_TYPE": "1",
            "REG_PROVINCE": "",
            "CSRC_CODE": "",
            "STOCK_CODE": "",
            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
            "COMPANY_STATUS": "2,4,5,7,8",
            "type": "inParams",
            "isPagination": "true",
            "pageHelp.beginPage": "1",
            "pageHelp.pageSize": "100",
            "pageHelp.pageNo": "1",
            "pageHelp.endPage": "1",
        },
        max_bytes=2 * 1024 * 1024,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("result") or []
    requested_page_size = 100
    page_help = payload.get("pageHelp") or {}
    required = {"A_STOCK_CODE", "SEC_NAME_CN", "LIST_DATE", "COMPANY_CODE"}
    observed = set().union(*(row.keys() for row in rows)) if rows else set()
    missing = sorted(required - observed)
    return {
        "source_id": "SSE_OFFICIAL",
        "plan_ids": ["CORE-01"],
        "decision": "PILOT_ALLOWED__NONCOMMERCIAL_PUBLIC_WEB",
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_admission": False,
        "raw_rows": len(rows),
        "requested_page_size": requested_page_size,
        "reported_page_size": page_help.get("pageSize"),
        "reported_total_rows": page_help.get("total"),
        "reported_page_count": page_help.get("pageCount"),
        "response_rows_exceed_requested_page_size": len(rows) > requested_page_size,
        "pagination_observation": (
            "The response row count exceeded the requested page size; byte "
            "caps and observed counts, not request pagination parameters, "
            "must bound this endpoint."
            if len(rows) > requested_page_size
            else "The bounded response did not exceed the requested page size."
        ),
        "required_raw_fields_present": not missing,
        "missing_required_raw_fields": missing,
        "capture_validation_passed": bool(rows) and not missing,
        "pit_observation": (
            "Current-list observation only; no historical validity or revision "
            "interval is established."
        ),
        "capture": request,
    }


def probe_szse_security_master(session: Any, output: Path) -> dict[str, Any]:
    path = output / "business" / "szse-a-share-current.xlsx"
    request = bounded_request(
        session,
        method="GET",
        url="https://www.szse.cn/api/report/ShowReport",
        output_path=path,
        output_root=output,
        headers={
            "User-Agent": "Quantification L1-D2A-01 bounded noncommercial pilot"
        },
        params={
            "SHOWTYPE": "xlsx",
            "CATALOGID": "1110",
            "TABKEY": "tab1",
            "random": "0.20260727",
        },
        max_bytes=4 * 1024 * 1024,
    )
    signature = path.read_bytes()[:4]
    return {
        "source_id": "SZSE_OFFICIAL",
        "plan_ids": ["CORE-01"],
        "decision": "PILOT_ALLOWED__NONCOMMERCIAL_PUBLIC_WEB",
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_admission": False,
        "xlsx_zip_signature_present": signature == b"PK\x03\x04",
        "capture_validation_passed": signature == b"PK\x03\x04",
        "pit_observation": (
            "Current export only; no complete historical identity, name, "
            "listing-status or revision intervals are established."
        ),
        "capture": request,
    }


def probe_public_page(
    session: Any,
    output: Path,
    *,
    probe_id: str,
    source_id: str,
    plan_ids: list[str],
    url: str,
    role: str,
    decision: str,
    expected_markers: list[str],
) -> dict[str, Any]:
    path = output / "public-pages" / f"{probe_id}.html"
    request = bounded_request(
        session,
        method="GET",
        url=url,
        output_path=path,
        output_root=output,
        headers={
            "User-Agent": "Quantification L1-D2A-01 public terms and notice audit"
        },
        max_bytes=4 * 1024 * 1024,
    )
    content_validation = validate_utf8_markers(path, expected_markers)
    return {
        "source_id": source_id,
        "plan_ids": plan_ids,
        "role": role,
        "decision": decision,
        "purpose_ceiling": "LEGAL_AND_ENDPOINT_INSPECTION_ONLY",
        "formal_admission": False,
        "content_validation": content_validation,
        "capture_validation_passed": (
            request["bytes"] > 0
            and "html" in (request["content_type"] or "").casefold()
            and content_validation["passed"]
        ),
        "capture": request,
    }


def probe_cninfo_metadata(session: Any, output: Path) -> dict[str, Any]:
    path = output / "business" / "cninfo-corporate-action-metadata.json"
    form = {
        "pageNum": "1",
        "pageSize": "10",
        "column": "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": "",
        "searchkey": "权益分派",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": "2025-01-01~2025-12-31",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    request = bounded_request(
        session,
        method="POST",
        url="https://www.cninfo.com.cn/new/hisAnnouncement/query",
        output_path=path,
        output_root=output,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch",
            "User-Agent": "Quantification L1-D2A-01 one-query metadata pilot",
        },
        data=form,
        max_bytes=2 * 1024 * 1024,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("announcements") or []
    required = {
        "announcementId",
        "announcementTitle",
        "announcementTime",
        "adjunctUrl",
        "secCode",
    }
    observed = set().union(*(row.keys() for row in rows)) if rows else set()
    missing = sorted(required - observed)
    return {
        "source_id": "CNINFO_OFFICIAL",
        "plan_ids": ["CORE-04"],
        "decision": "PILOT_ALLOWED__NONCOMMERCIAL_PUBLIC_WEB",
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_admission": False,
        "query_scope": {
            "date_start": "2025-01-01",
            "date_end": "2025-12-31",
            "keyword": "权益分派",
            "page_number": 1,
            "page_size": 10,
        },
        "returned_rows": len(rows),
        "reported_total": int(payload.get("totalAnnouncement") or 0),
        "required_metadata_fields_present": not missing,
        "missing_required_metadata_fields": missing,
        "capture_validation_passed": bool(rows) and not missing,
        "pdf_body_downloads": 0,
        "announcement_time_precision_observation": (
            summarize_cninfo_time_precision(rows)
        ),
        "pit_observation": (
            "announcementTime is observed, but its timezone semantics, "
            "revision/supersession chain and action effective time are not "
            "closed. Metadata is not normalized into company-action facts."
        ),
        "capture": request,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument(
        "--run-id",
        default=f"d2a01-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}",
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="Execute the exact bounded no-key requests in the source matrix.",
    )
    args = parser.parse_args()
    if not args.network:
        raise SystemExit("Refusing to run real-source pilot without --network")

    workspace = args.workspace_root.resolve()
    allowed_root = (workspace / "tmp" / "source-admission-pilot").resolve()
    output = (allowed_root / args.run_id).resolve()
    if not resolved_under(output, allowed_root):
        raise SystemExit("Refusing output outside the isolated pilot root")
    if resolved_under(output, workspace / "data"):
        raise SystemExit("Refusing to write below the formal data root")
    matrix_path = (
        workspace
        / "data"
        / "catalog"
        / "contracts"
        / "source_admission_v1"
        / "source_matrix.json"
    )
    try:
        matrix = load_source_matrix(matrix_path)
        assert_execution_authorized(matrix)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"Source-admission execution gate rejected run: {exc}") from exc

    if output.exists():
        raise SystemExit(f"Run directory already exists: {output}")
    output.mkdir(parents=True)

    try:
        import requests
    except ImportError as exc:
        raise SystemExit(
            "Pinned data-engineering runtime is required; do not install "
            f"dependencies implicitly. Original error: {exc}"
        ) from exc

    catalog_path = workspace / "data" / "catalog" / "datasets.json"
    planned_path = workspace / "data" / "catalog" / "planned_datasets.json"
    formal_roots = {
        "data/catalog/datasets.json": catalog_path,
        "data/catalog/planned_datasets.json": planned_path,
        "data/market": workspace / "data" / "market",
        "data/published": workspace / "data" / "published",
    }

    def fingerprint_target(path: Path) -> dict[str, Any]:
        if path.is_file():
            return {
                "exists": True,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        return tree_fingerprint(path)

    before = {name: fingerprint_target(path) for name, path in formal_roots.items()}
    session = requests.Session()
    session_guards = harden_session(session)
    started = dt.datetime.now(dt.timezone.utc)
    results: dict[str, Any] = {
        "schema_version": 1,
        "work_package": "L1-D2A-01_SOURCE_ADMISSION_AND_PILOT_ADAPTERS",
        "run_id": args.run_id,
        "started_at_utc": started.isoformat(),
        "scope": "BOUNDED_PUBLIC_REAL_SOURCE_PILOT_ONLY",
        "output_root": output.as_posix(),
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "requests": requests.__version__,
        },
        "contract": {
            "path": matrix_path.relative_to(workspace).as_posix(),
            "sha256": sha256_file(matrix_path),
            "contract_id": matrix["contract_id"],
            "purpose_ceiling": matrix["global_purpose_ceiling"],
        },
        "runner": {
            "path": Path(__file__).resolve().relative_to(workspace).as_posix(),
            "sha256": sha256_file(Path(__file__).resolve()),
        },
        "safety": {
            "paid_sources_used": False,
            "credentials_used": False,
            "credential_channels_disabled": session_guards,
            "historical_backfill_performed": False,
            "formal_catalog_registration_performed": False,
            "formal_quality_promotion_performed": False,
            "exchange_daily_price_requests": 0,
            "cninfo_pdf_body_downloads": 0,
        },
        "integrity": {"before": before},
        "probes": {},
        "blocked_before_fetch": [
            {
                "source_key": "bse_public_reference",
                "plan_ids": ["CORE-01", "CORE-02", "CORE-03"],
                "status": "BLOCKED_BEFORE_BUSINESS_DATA_FETCH",
                "reason": (
                    "A prior manual observation suggested a mixed static/quote "
                    "schema, but this work package retained no BSE raw "
                    "response; the business schema and rights remain "
                    "unverified."
                ),
            },
            {
                "source_key": "exchange_daily_trading_information",
                "plan_ids": ["CORE-03"],
                "status": "BLOCKED_BEFORE_BUSINESS_DATA_FETCH",
                "reason": (
                    "No exact no-key daily-history product licence, schema, "
                    "retention right and internal-use grant is admitted."
                ),
            },
            {
                "source_key": "tencent_secondary_daily",
                "plan_ids": ["CORE-03"],
                "status": "BLOCKED_BEFORE_BUSINESS_DATA_FETCH",
                "reason": (
                    "Reuse rights and the 11-field source schema/units remain "
                    "unresolved; prior smoke evidence is not fetched again."
                ),
            },
        ],
    }

    session.headers.update({"Accept-Encoding": "gzip, deflate"})
    probes = results["probes"]
    run_probe(
        probes,
        "sse_security_master_sample",
        lambda: probe_sse_security_master(session, output),
    )
    run_probe(
        probes,
        "szse_security_master_current_export",
        lambda: probe_szse_security_master(session, output),
    )
    run_probe(
        probes,
        "cninfo_corporate_action_metadata_sample",
        lambda: probe_cninfo_metadata(session, output),
    )

    page_specs = [
        (
            "sse_2026_calendar_notice",
            "SSE_OFFICIAL",
            ["CORE-02"],
            "https://www.sse.com.cn/disclosure/dealinstruc/closed/",
            "current-year official holiday notice",
            "PILOT_ALLOWED__NONCOMMERCIAL_PUBLIC_WEB",
            ["休市安排", "上海证券交易所"],
        ),
        (
            "szse_2026_calendar_notice",
            "SZSE_OFFICIAL",
            ["CORE-02"],
            "https://www.szse.cn/disclosure/notice/t20251222_618087.html",
            "current-year official holiday notice",
            "PILOT_ALLOWED__NONCOMMERCIAL_PUBLIC_WEB",
            ["2026年部分节假日休市安排", "深圳证券交易所"],
        ),
        (
            "sse_general_legal_statement",
            "SSE_OFFICIAL",
            ["CORE-01", "CORE-02", "CORE-03"],
            "https://www.sse.com.cn/home/legal/",
            "rights evidence",
            "LEGAL_EVIDENCE_ONLY",
            ["法律声明", "上海证券交易所"],
        ),
        (
            "sse_market_data_services",
            "SSE_OFFICIAL",
            ["CORE-03"],
            "https://star.sse.com.cn/transparency/services/",
            "market-data authorization evidence",
            "LEGAL_EVIDENCE_ONLY__BUSINESS_DATA_BLOCKED",
            ["数据服务", "上海证券交易所"],
        ),
        (
            "sse_trading_information_rule",
            "SSE_OFFICIAL",
            ["CORE-03"],
            "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml",
            "market-information ownership/use evidence",
            "LEGAL_EVIDENCE_ONLY__BUSINESS_DATA_BLOCKED",
            ["上海证券交易所交易规则", "交易信息"],
        ),
        (
            "szse_general_legal_statement",
            "SZSE_OFFICIAL",
            ["CORE-01", "CORE-02", "CORE-03"],
            "https://www.szse.cn/application/laws/index.html",
            "rights evidence",
            "LEGAL_EVIDENCE_ONLY",
            ["法律声明", "深圳证券交易所"],
        ),
        (
            "szse_data_services",
            "SZSE_OFFICIAL",
            ["CORE-03"],
            "https://www.szse.cn/English/services/dataServices/index.html",
            "market-data authorization evidence",
            "LEGAL_EVIDENCE_ONLY__BUSINESS_DATA_BLOCKED",
            ["Data Services", "market data"],
        ),
        (
            "cninfo_legal_statement",
            "CNINFO_OFFICIAL",
            ["CORE-04"],
            "https://www.cninfo.com.cn/new/snapshot/companyDetailCn?code=000060",
            "noncommercial browse/download evidence",
            "LEGAL_EVIDENCE_ONLY",
            ["巨潮资讯网", "法律声明"],
        ),
    ]
    for spec in page_specs:
        (
            probe_id,
            source_id,
            plan_ids,
            url,
            role,
            decision,
            expected_markers,
        ) = spec
        run_probe(
            probes,
            probe_id,
            lambda probe_id=probe_id,
            source_id=source_id,
            plan_ids=plan_ids,
            url=url,
            role=role,
            decision=decision,
            expected_markers=expected_markers: probe_public_page(
                session,
                output,
                probe_id=probe_id,
                source_id=source_id,
                plan_ids=plan_ids,
                url=url,
                role=role,
                decision=decision,
                expected_markers=expected_markers,
            ),
        )
        time.sleep(0.10)
    session.close()

    after = {name: fingerprint_target(path) for name, path in formal_roots.items()}
    integrity_unchanged = before == after
    results["integrity"].update(
        {
            "after": after,
            "formal_targets_unchanged": integrity_unchanged,
        }
    )
    probe_statuses = [value["status"] for value in probes.values()]
    successful_probe_status = "PASS_FOR_BOUNDED_CAPTURE"
    results["probe_summary"] = {
        "total": len(probe_statuses),
        "passed": sum(status == successful_probe_status for status in probe_statuses),
        "errors": sum(status == "ERROR" for status in probe_statuses),
        "content_validation_failures": sum(
            status == "CONTENT_VALIDATION_FAILED" for status in probe_statuses
        ),
    }
    probe_failures = len(probe_statuses) - results["probe_summary"]["passed"]
    if not integrity_unchanged:
        overall = "FAIL__FORMAL_TARGET_CHANGED"
    elif probe_failures:
        overall = (
            f"FAIL_WITH_{probe_failures}_CAPTURE_OR_CONTENT_ERRORS__"
            "REFERENCE_AND_CNINFO_PILOTS_CAPTURED__"
            "CORE03_AND_COMPLETE_PIT_BLOCKED"
        )
    else:
        overall = (
            "PARTIAL__PUBLIC_REFERENCE_AND_CNINFO_METADATA_PILOTS__"
            "CORE03_AND_COMPLETE_PIT_BLOCKED"
        )
    results["gate_status"] = {
        "overall": overall,
        "CORE-01": (
            "PARTIAL__SSE_SZSE_CURRENT_REFERENCE_ONLY__"
            "BSE_AND_HISTORICAL_PIT_BLOCKED"
        ),
        "CORE-02": "PARTIAL__CURRENT_YEAR_NOTICES_ONLY__HISTORICAL_SESSIONS_BLOCKED",
        "CORE-03": "BLOCKED_BEFORE_FETCH__LICENCE_AND_SCHEMA_NOT_ADMITTED",
        "CORE-04": (
            "PARTIAL__BOUNDED_METADATA_ONLY__"
            "REVISION_EFFECTIVE_TIME_AND_BULK_RIGHTS_BLOCKED"
        ),
        "P1": "BLOCKED__NO_ADMITTED_DAILY_PRICE_SOURCE",
        "P2": "PRELIMINARY__BOUNDED_METADATA_ONLY__NO_PDF_BODIES",
        "P3": "NOT_RUN__OFFICIAL_INDEX_SOURCE_ADMISSION_OUTSIDE_THIS_PROBE",
        "formal_source_admission": False,
        "formal_dataset_snapshots_created": 0,
        "quality_promotions": 0,
    }
    results["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    results["elapsed_seconds"] = round(
        (
            dt.datetime.fromisoformat(results["finished_at_utc"]) - started
        ).total_seconds(),
        3,
    )
    write_json(output / "RESULTS.json", results)
    inventory = {
        "schema_version": 1,
        "run_id": args.run_id,
        "self_exclusion": "INVENTORY.json is intentionally not listed in itself",
        "files": [
            {
                "path": path.relative_to(output).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(item for item in output.rglob("*") if item.is_file())
        ],
    }
    write_json(output / "INVENTORY.json", inventory)
    print(json.dumps(results["gate_status"], ensure_ascii=False, indent=2))
    if not integrity_unchanged:
        return 3
    if probe_failures:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
