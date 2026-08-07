#!/usr/bin/env python3
"""Read-only, fail-closed health verification for free web daily probe runs.

The verifier accepts only immutable-looking run directories below
``tmp/free-web-daily-probe``.  It performs no network access and writes no
files.  A zero exit code means only that the bounded inspection receipt is
internally complete and hash-consistent; it never grants formal source
admission, catalog registration, quality promotion, or research use.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import stat
import sys
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
ALLOWED_ROOT = ROOT / "tmp" / "free-web-daily-probe"
MONITOR_ID = "free_web_daily_probe_run_health/v1.1"
LEGACY_ENGINE_ID = "free_web_daily_dual_source_probe/v1"
HARDENED_ENGINE_ID = "free_web_daily_dual_source_probe/v1.1"
SUPPORTED_ENGINE_IDS = {LEGACY_ENGINE_ID, HARDENED_ENGINE_ID}
EXPECTED_STATUS = "PASS_BOUNDED_DUAL_SOURCE_INSPECTION_ONLY"
PURPOSE_CEILING = "INSPECTION_ONLY"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
INTEGER_RE = re.compile(r"^-?\d+$")
REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

EXPECTED_FILES = {
    "RESULTS.json",
    "normalized/reconciled.csv",
    "quality/findings.json",
    "raw/sina.response",
    "raw/tencent.response",
}
EXPECTED_DISK_FILES = EXPECTED_FILES | {"MANIFEST.json"}
EXPECTED_CAPTURE_FILES = {
    "SINA_WEB_QUOTE": ("raw/sina.response", "hq.sinajs.cn"),
    "TENCENT_WEB_QUOTE": ("raw/tencent.response", "qt.gtimg.cn"),
}
EXPECTED_BOUNDARY = {
    "purpose_ceiling": PURPOSE_CEILING,
    "credentials_used": False,
    "paid_access_used": False,
    "formal_source_admission": False,
    "formal_catalog_written": False,
    "quality_promotion": False,
    "scheduler_created": False,
    "research_ready": False,
}
FORBIDDEN_TRUE_KEYS = {
    "credentials_used",
    "paid_access_used",
    "formal_source_admission",
    "formal_catalog_written",
    "quality_promotion",
    "scheduler_created",
    "research_ready",
    "source_admission",
    "catalog_registration",
}
LEGACY_NORMALIZED_FIELDS = [
    "security_id",
    "market_prefix",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "previous_close",
    "volume_shares",
    "amount_cny",
    "sina_observed_time",
    "tencent_observed_time",
    "sina_status",
    "tencent_status",
    "volume_cross_source_delta_shares",
    "amount_cross_source_delta_cny",
    "source_count",
]
HARDENED_NORMALIZED_FIELDS = [
    "security_id",
    "market_prefix",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "previous_close",
    "volume_shares",
    "amount_cny",
    "sina_observed_time",
    "tencent_observed_time",
    "sina_source_marker",
    "sina_source_marker_semantics",
    "tencent_route_code",
    "tencent_route_code_semantics",
    "volume_cross_source_delta_shares",
    "amount_cross_source_delta_cny",
    "source_count",
]


class HealthError(RuntimeError):
    """Controlled fail-closed health verification error."""


def _load_probe_module() -> Any:
    module_name = "_free_web_daily_probe_for_health_monitor"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = ROOT / "control" / "tools" / "run_free_web_daily_probe.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise HealthError("cannot load the probe parser")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


PROBE = _load_probe_module()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_constant(value: str) -> None:
    raise HealthError(f"non-finite JSON constant is forbidden: {value}")


def _strict_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HealthError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except HealthError:
        raise
    except Exception as exc:
        raise HealthError(f"invalid UTF-8 JSON: {path.name}: {exc}") from exc


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HealthError(f"cannot lstat path: {path}: {exc}") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & REPARSE_ATTRIBUTE)


def validate_run_root(value: Path) -> Path:
    allowed = ALLOWED_ROOT.resolve(strict=True)
    lexical = Path(os.path.abspath(value))
    try:
        relative = lexical.relative_to(allowed)
    except ValueError as exc:
        raise HealthError("run root must stay below tmp/free-web-daily-probe") from exc
    if not relative.parts:
        raise HealthError("run root must be a child run directory, not the shared root")

    current = allowed
    for part in relative.parts:
        current = current / part
        if not current.exists():
            raise HealthError(f"run root component does not exist: {current}")
        if _is_reparse(current):
            raise HealthError(f"reparse or symlink path is forbidden: {current}")

    resolved = lexical.resolve(strict=True)
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise HealthError("resolved run root escapes the inspection root") from exc
    if not resolved.is_dir():
        raise HealthError("run root must be a directory")
    return resolved


def _safe_relative_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise HealthError("manifest paths must be non-empty POSIX relative paths")
    parsed = PurePosixPath(raw)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise HealthError(f"unsafe manifest path: {raw!r}")
    normalized = parsed.as_posix()
    if normalized != raw:
        raise HealthError(f"non-canonical manifest path: {raw!r}")
    return normalized


def _safe_file(root: Path, relative: str) -> Path:
    parsed = PurePosixPath(relative)
    current = root
    for part in parsed.parts:
        current = current / part
        if not current.exists():
            raise HealthError(f"required file is missing: {relative}")
        if _is_reparse(current):
            raise HealthError(f"reparse or symlink entry is forbidden: {relative}")
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HealthError(f"file escapes the run root: {relative}") from exc
    if not resolved.is_file():
        raise HealthError(f"required path is not a regular file: {relative}")
    return resolved


def _disk_file_set(root: Path) -> set[str]:
    files: set[str] = set()
    for current_raw, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_raw)
        for name in list(dirnames):
            child = current / name
            if _is_reparse(child):
                raise HealthError(f"reparse directory is forbidden: {child}")
        for name in filenames:
            child = current / name
            if _is_reparse(child):
                raise HealthError(f"reparse file is forbidden: {child}")
            if not child.is_file():
                raise HealthError(f"non-regular entry is forbidden: {child}")
            files.add(child.relative_to(root).as_posix())
    return files


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HealthError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HealthError(f"{label} must be an array")
    return value


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise HealthError(f"{label} must be an integer >= {minimum}")
    return value


def _require_iso_date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise HealthError(f"{label} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HealthError(f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise HealthError(f"{label} must be a canonical ISO date")
    return value


def _assert_no_grants(value: Any, label: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_TRUE_KEYS and child is not False:
                raise HealthError(f"{label}.{key} must be exactly false")
            if key == "purpose_ceiling" and child != PURPOSE_CEILING:
                raise HealthError(f"{label}.purpose_ceiling must be INSPECTION_ONLY")
            _assert_no_grants(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_grants(child, f"{label}[{index}]")


def _verify_boundary(document: dict[str, Any], label: str) -> str:
    if document.get("schema_version") != 1:
        raise HealthError(f"{label}.schema_version must be 1")
    engine_id = document.get("engine_id")
    if engine_id not in SUPPORTED_ENGINE_IDS:
        raise HealthError(f"{label}.engine_id is not a supported bounded probe engine")
    if document.get("status") != EXPECTED_STATUS:
        raise HealthError(f"{label}.status is not a complete bounded PASS")
    for key, expected in EXPECTED_BOUNDARY.items():
        if document.get(key) is not expected and document.get(key) != expected:
            raise HealthError(f"{label}.{key} must be exactly {expected!r}")
    _assert_no_grants(document, label)
    return str(engine_id)


def _verify_manifest(
    root: Path, manifest: dict[str, Any]
) -> tuple[dict[str, Path], str]:
    engine_id = _verify_boundary(manifest, "MANIFEST")
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str):
        raise HealthError("MANIFEST.generated_at must be a timestamp")
    try:
        timestamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HealthError("MANIFEST.generated_at is not RFC3339-compatible") from exc
    if timestamp.tzinfo is None:
        raise HealthError("MANIFEST.generated_at must include an offset")

    entries = _require_list(manifest.get("files"), "MANIFEST.files")
    if len(entries) != len(EXPECTED_FILES):
        raise HealthError("MANIFEST.files must bind exactly the five business files")
    bound: dict[str, Path] = {}
    for index, raw_entry in enumerate(entries):
        entry = _require_mapping(raw_entry, f"MANIFEST.files[{index}]")
        relative = _safe_relative_path(entry.get("path"))
        if relative in bound:
            raise HealthError(f"duplicate MANIFEST file path: {relative}")
        size = _require_int(entry.get("bytes"), f"MANIFEST.files[{index}].bytes")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or HASH_RE.fullmatch(digest) is None:
            raise HealthError(f"invalid SHA-256 for {relative}")
        path = _safe_file(root, relative)
        if path.stat().st_size != size:
            raise HealthError(f"byte count mismatch: {relative}")
        if sha256_file(path) != digest:
            raise HealthError(f"SHA-256 mismatch: {relative}")
        bound[relative] = path
    if set(bound) != EXPECTED_FILES:
        missing = sorted(EXPECTED_FILES - set(bound))
        extra = sorted(set(bound) - EXPECTED_FILES)
        raise HealthError(f"manifest file set mismatch; missing={missing}, extra={extra}")
    disk_files = _disk_file_set(root)
    if disk_files != EXPECTED_DISK_FILES:
        missing = sorted(EXPECTED_DISK_FILES - disk_files)
        extra = sorted(disk_files - EXPECTED_DISK_FILES)
        raise HealthError(f"disk file set mismatch; missing={missing}, extra={extra}")
    return bound, engine_id


def _verify_hardened_provenance(
    manifest: dict[str, Any], results: dict[str, Any]
) -> None:
    provenance = _require_mapping(
        manifest.get("execution_provenance"), "MANIFEST.execution_provenance"
    )
    script = _require_mapping(provenance.get("script"), "execution_provenance.script")
    expected_script_path = "control/tools/run_free_web_daily_probe.py"
    if script.get("path") != expected_script_path:
        raise HealthError("probe script path binding mismatch")
    expected_script_hash = sha256_file(ROOT / expected_script_path)
    if script.get("sha256") != expected_script_hash:
        raise HealthError("probe script hash binding mismatch")

    try:
        expected_contract = PROBE.contract_binding()
    except Exception as exc:
        raise HealthError(f"current probe contract is invalid: {exc}") from exc
    if provenance.get("contract") != expected_contract:
        raise HealthError("probe contract/policy binding mismatch")
    if provenance.get("same_day_finality_receipt") != results.get(
        "same_day_finality_receipt"
    ):
        raise HealthError("same-day finality receipt binding mismatch")
    expected_request = {
        "trade_date": results.get("trade_date"),
        "symbols": results.get("requested_symbols"),
        "symbol_count": results.get("requested_symbol_count"),
    }
    if provenance.get("probe_request") != expected_request:
        raise HealthError("probe request binding mismatch")
    if provenance.get("request_metadata") != results.get("captures"):
        raise HealthError("capture metadata binding mismatch")


def _capture_map(results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    captures = _require_list(results.get("captures"), "RESULTS.captures")
    if len(captures) != 2:
        raise HealthError("RESULTS.captures must contain exactly two sources")
    mapped: dict[str, dict[str, Any]] = {}
    for index, raw_capture in enumerate(captures):
        capture = _require_mapping(raw_capture, f"RESULTS.captures[{index}]")
        source_id = capture.get("source_id")
        if source_id not in EXPECTED_CAPTURE_FILES or source_id in mapped:
            raise HealthError(f"unknown or duplicate capture source: {source_id!r}")
        mapped[source_id] = capture
    if set(mapped) != set(EXPECTED_CAPTURE_FILES):
        raise HealthError("both Sina and Tencent captures are required")
    return mapped


def _verify_raw_sources(
    bound: dict[str, Path],
    results: dict[str, Any],
    requested_symbols: list[str],
    trade_date: str,
    engine_id: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    captures = _capture_map(results)
    parsed_by_source: dict[str, dict[str, Any]] = {}
    parsers = {
        "SINA_WEB_QUOTE": PROBE.parse_sina,
        "TENCENT_WEB_QUOTE": PROBE.parse_tencent,
    }
    for source_id, (relative, expected_host) in EXPECTED_CAPTURE_FILES.items():
        capture = captures[source_id]
        if capture.get("response_file") != relative:
            raise HealthError(f"{source_id} response_file mismatch")
        path = bound[relative]
        digest = sha256_file(path)
        if capture.get("sha256") != digest:
            raise HealthError(f"{source_id} capture SHA-256 mismatch")
        if engine_id == LEGACY_ENGINE_ID:
            if capture.get("http_status") != "200":
                raise HealthError(f"{source_id} legacy HTTP status is not 200")
            if capture.get("content_length") != str(path.stat().st_size):
                raise HealthError(f"{source_id} legacy content length mismatch")
        else:
            requested_url = capture.get("requested_url")
            if not isinstance(requested_url, str):
                raise HealthError(f"{source_id} requested_url is missing")
            try:
                PROBE.validate_request_metadata(
                    capture,
                    requested_url=requested_url,
                    payload=path.read_bytes(),
                )
            except Exception as exc:
                raise HealthError(f"{source_id} capture metadata is invalid: {exc}") from exc
            parsed_url = urlparse(requested_url)
            if parsed_url.scheme != "https" or parsed_url.hostname != expected_host:
                raise HealthError(f"{source_id} requested URL host/scheme mismatch")
        redacted = _require_mapping(
            capture.get("request_url_redacted_to_host_and_symbol_count"),
            f"{source_id}.request_url_redacted_to_host_and_symbol_count",
        )
        if redacted.get("host") != expected_host:
            raise HealthError(f"{source_id} host mismatch")
        if redacted.get("symbol_count") != len(requested_symbols):
            raise HealthError(f"{source_id} requested symbol count mismatch")
        try:
            parsed = parsers[source_id](path.read_bytes())
        except Exception as exc:
            raise HealthError(f"{source_id} raw response cannot be parsed: {exc}") from exc
        if list(parsed) != requested_symbols:
            raise HealthError(f"{source_id} symbols/order do not match RESULTS")
        if capture.get("row_count") != len(parsed):
            raise HealthError(f"{source_id} capture row_count mismatch")
        for symbol, quote in parsed.items():
            if quote.trade_date != trade_date:
                raise HealthError(f"{source_id} {symbol} trade date mismatch")
        parsed_by_source[source_id] = parsed

    try:
        recomputed, findings = PROBE.reconcile(
            requested_symbols,
            trade_date,
            parsed_by_source["SINA_WEB_QUOTE"],
            parsed_by_source["TENCENT_WEB_QUOTE"],
        )
    except Exception as exc:
        raise HealthError(f"dual-source reconciliation failed: {exc}") from exc
    if findings:
        raise HealthError("raw sources no longer recompute to zero findings")
    return (
        parsed_by_source["SINA_WEB_QUOTE"],
        parsed_by_source["TENCENT_WEB_QUOTE"],
        recomputed,
    )


def _read_normalized(path: Path, engine_id: str) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except Exception as exc:
        raise HealthError(f"normalized CSV is not strict UTF-8: {exc}") from exc
    if "\x00" in text:
        raise HealthError("normalized CSV contains NUL")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    expected_fields = (
        LEGACY_NORMALIZED_FIELDS
        if engine_id == LEGACY_ENGINE_ID
        else HARDENED_NORMALIZED_FIELDS
    )
    if reader.fieldnames != expected_fields:
        raise HealthError("normalized CSV header does not match the v1 contract")
    rows: list[dict[str, str]] = []
    for index, raw_row in enumerate(reader, start=2):
        if None in raw_row or any(value is None for value in raw_row.values()):
            raise HealthError(f"normalized CSV row {index} has width drift")
        rows.append({str(key): str(value) for key, value in raw_row.items()})
    return rows


def _serialized_row(row: dict[str, Any], engine_id: str) -> dict[str, str]:
    if engine_id == LEGACY_ENGINE_ID:
        legacy = {
            **row,
            "sina_status": row["sina_source_marker"],
            "tencent_status": row["tencent_route_code"],
        }
        return {field: str(legacy[field]) for field in LEGACY_NORMALIZED_FIELDS}
    return {field: str(row[field]) for field in HARDENED_NORMALIZED_FIELDS}


def _verify_normalized(
    path: Path,
    recomputed: list[dict[str, Any]],
    requested_symbols: list[str],
    trade_date: str,
    engine_id: str,
) -> int:
    rows = _read_normalized(path, engine_id)
    if rows != [_serialized_row(row, engine_id) for row in recomputed]:
        raise HealthError("normalized rows do not exactly match raw-source reconciliation")
    symbols: list[str] = []
    for index, row in enumerate(rows, start=2):
        if row["trade_date"] != trade_date:
            raise HealthError(f"normalized row {index} trade date mismatch")
        symbol = row["market_prefix"] + row["security_id"]
        symbols.append(symbol)
        if row["source_count"] != "2":
            raise HealthError(f"normalized row {index} is not dual-source")
        for field in (
            "volume_shares",
            "volume_cross_source_delta_shares",
            "source_count",
        ):
            if INTEGER_RE.fullmatch(row[field]) is None:
                raise HealthError(f"normalized row {index} invalid integer: {field}")
        for field in (
            "open",
            "high",
            "low",
            "close",
            "previous_close",
            "amount_cny",
            "amount_cross_source_delta_cny",
        ):
            try:
                number = float(row[field])
            except ValueError as exc:
                raise HealthError(
                    f"normalized row {index} invalid numeric: {field}"
                ) from exc
            if not math.isfinite(number):
                raise HealthError(f"normalized row {index} non-finite numeric: {field}")
    if symbols != requested_symbols:
        raise HealthError("normalized symbols/order do not match RESULTS")
    return len(rows)


def verify_run(run_root: Path, target_trade_date: str) -> dict[str, Any]:
    target_trade_date = _require_iso_date(target_trade_date, "target_trade_date")
    root = validate_run_root(run_root)
    manifest_path = _safe_file(root, "MANIFEST.json")
    manifest = _require_mapping(load_json(manifest_path), "MANIFEST")
    bound, manifest_engine_id = _verify_manifest(root, manifest)

    results = _require_mapping(load_json(bound["RESULTS.json"]), "RESULTS")
    results_engine_id = _verify_boundary(results, "RESULTS")
    if results_engine_id != manifest_engine_id:
        raise HealthError("MANIFEST and RESULTS engine IDs disagree")
    if results_engine_id == HARDENED_ENGINE_ID:
        _verify_hardened_provenance(manifest, results)
    results_trade_date = _require_iso_date(results.get("trade_date"), "RESULTS.trade_date")
    if results_trade_date != target_trade_date:
        raise HealthError("RESULTS trade date does not match the target trade date")

    raw_symbols = _require_list(results.get("requested_symbols"), "RESULTS.requested_symbols")
    if not all(isinstance(value, str) for value in raw_symbols):
        raise HealthError("RESULTS.requested_symbols must contain only strings")
    try:
        requested_symbols = PROBE.validate_symbols(raw_symbols)
    except Exception as exc:
        raise HealthError(f"invalid RESULTS requested_symbols: {exc}") from exc
    requested_count = _require_int(
        results.get("requested_symbol_count"), "RESULTS.requested_symbol_count", minimum=1
    )
    if requested_count != len(requested_symbols):
        raise HealthError("RESULTS requested symbol count mismatch")
    if results.get("network_call_count") != 2 or results.get("retry_count") != 0:
        raise HealthError("healthy v1 run requires exactly two calls and zero retries")
    if results_engine_id == HARDENED_ENGINE_ID:
        if results.get("redirect_count") != 0:
            raise HealthError("healthy hardened run requires zero redirects")
        try:
            current_binding = PROBE.contract_binding()
        except Exception as exc:
            raise HealthError(f"current probe contract is invalid: {exc}") from exc
        if results.get("policy_sha256") != current_binding.get("policy_sha256"):
            raise HealthError("RESULTS policy hash mismatch")
    if results.get("canonical_row_source") != (
        "SINA_WEB_QUOTE_AFTER_TENCENT_CROSS_CHECK"
    ):
        raise HealthError("unexpected canonical row source")

    accepted_count = _require_int(
        results.get("accepted_row_count"), "RESULTS.accepted_row_count"
    )
    finding_count = _require_int(results.get("finding_count"), "RESULTS.finding_count")
    if accepted_count != requested_count or finding_count != 0:
        raise HealthError("healthy run requires all requested rows accepted and zero findings")

    findings = load_json(bound["quality/findings.json"])
    if findings != []:
        raise HealthError("quality/findings.json must be an empty array for a healthy run")

    _sina, _tencent, recomputed = _verify_raw_sources(
        bound, results, requested_symbols, target_trade_date, results_engine_id
    )
    if len(recomputed) != accepted_count:
        raise HealthError("recomputed accepted count does not match RESULTS")
    normalized_count = _verify_normalized(
        bound["normalized/reconciled.csv"],
        recomputed,
        requested_symbols,
        target_trade_date,
        results_engine_id,
    )
    if normalized_count != accepted_count:
        raise HealthError("normalized row count does not match accepted_row_count")

    return {
        "schema_version": 1,
        "monitor_id": MONITOR_ID,
        "status": "PASS_HEALTHY_BOUNDED_INSPECTION_ONLY",
        "purpose_ceiling": PURPOSE_CEILING,
        "read_only": True,
        "network_requests_performed": 0,
        "files_written": 0,
        "formal_source_admission": False,
        "formal_catalog_written": False,
        "quality_promotion": False,
        "scheduler_created": False,
        "research_ready": False,
        "trade_date": target_trade_date,
        "requested_symbol_count": requested_count,
        "accepted_row_count": accepted_count,
        "raw_source_count": 2,
        "probe_engine_id": results_engine_id,
        "manifest_bound_business_file_count": len(bound),
        "run_root": root.relative_to(ROOT).as_posix(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--trade-date", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = verify_run(args.run_root, args.trade_date)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "monitor_id": MONITOR_ID,
            "status": "FAIL_CLOSED",
            "purpose_ceiling": PURPOSE_CEILING,
            "read_only": True,
            "network_requests_performed": 0,
            "files_written": 0,
            "formal_source_admission": False,
            "formal_catalog_written": False,
            "quality_promotion": False,
            "scheduler_created": False,
            "research_ready": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        print(
            json.dumps(failure, ensure_ascii=False, indent=2, allow_nan=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
