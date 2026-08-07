#!/usr/bin/env python3
"""Execute a hash-bound, one-call-per-source official inspection plan.

This wrapper deliberately reuses the validated request and parser primitives in
``run_source_admission_pilot.py`` without reactivating its historical source
matrix.  It writes only to a new reports/data-governance evidence directory and
never grants source admission, catalog registration, quality promotion, or
research use.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable


PROBE_ORDER = (
    "sse_current_security_identity_sample",
    "szse_current_security_identity_export",
    "cninfo_minimum_metadata_window",
)
SZSE_REQUIRED_FIELDS = frozenset({"A股代码", "A股简称", "A股上市日期"})
REQUIRED_UNCHANGED = {
    "control/ROADMAP.md",
    "control/projects.yaml",
    "data/catalog/datasets.json",
    "data/catalog/contracts/source_admission_v1/source_matrix.json",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def resolved_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def load_pilot(workspace: Path) -> Any:
    path = workspace / "control" / "tools" / "run_source_admission_pilot.py"
    spec = importlib.util.spec_from_file_location("bounded_probe_pilot", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load existing pilot primitives: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_plan(plan: dict[str, Any], workspace: Path, plan_path: Path) -> dict[str, str]:
    required = {
        "schema_version",
        "plan_id",
        "run_id",
        "status",
        "purpose_ceiling",
        "output_directory",
        "execution_authorization_active",
        "formal_source_admission",
        "catalog_write_authorized",
        "quality_promotion_authorized",
        "bulk_backfill_authorized",
        "paid_or_credentialed_access_authorized",
        "network_policy",
        "probes",
        "fail_closed_sources",
        "authority_bindings",
    }
    missing = sorted(required - set(plan))
    if missing:
        raise ValueError(f"plan missing keys: {missing}")
    if plan["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    if plan["status"] != "ACTIVE__BOUNDED_INSPECTION_ONLY":
        raise ValueError("plan is not active for bounded inspection")
    if plan["purpose_ceiling"] != "INSPECTION_ONLY":
        raise ValueError("purpose ceiling must be INSPECTION_ONLY")
    if plan["execution_authorization_active"] is not True:
        raise ValueError("execution authorization is not active")
    forbidden_grants = (
        "formal_source_admission",
        "catalog_write_authorized",
        "quality_promotion_authorized",
        "bulk_backfill_authorized",
        "paid_or_credentialed_access_authorized",
    )
    if any(plan[key] is not False for key in forbidden_grants):
        raise ValueError("plan attempts to grant a forbidden formal action")
    network = plan["network_policy"]
    if network != {
        "max_calls_per_probe": 1,
        "retry_count": 0,
        "credentials_forbidden": True,
        "pdf_body_downloads": 0,
    }:
        raise ValueError("network policy differs from the fixed bounded contract")
    probes = plan["probes"]
    if not isinstance(probes, list) or tuple(item.get("probe_id") for item in probes) != PROBE_ORDER:
        raise ValueError("probe order/scope differs from the executable plan")
    if any(item.get("max_calls") != 1 for item in probes):
        raise ValueError("every executable probe must have max_calls=1")
    fail_closed = plan["fail_closed_sources"]
    if {item.get("source_id") for item in fail_closed} != {"BSE_OFFICIAL", "CCER_CSMAR_INSTITUTIONAL_API"}:
        raise ValueError("fail-closed source set differs from the fixed contract")
    if any(item.get("business_calls") != 0 for item in fail_closed):
        raise ValueError("fail-closed sources must have business_calls=0")

    output = (workspace / plan["output_directory"]).resolve()
    reports_root = (workspace / "reports" / "data-governance").resolve()
    if not resolved_under(output, reports_root):
        raise ValueError("output must stay under reports/data-governance")
    if output != plan_path.parent.resolve():
        raise ValueError("execution plan must live in its own output directory")

    bindings: dict[str, str] = {}
    for item in plan["authority_bindings"]:
        relative = item.get("path")
        expected = item.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValueError("invalid authority binding")
        if relative in bindings:
            raise ValueError(f"duplicate authority binding: {relative}")
        path = (workspace / relative).resolve()
        if not resolved_under(path, workspace) or not path.is_file():
            raise ValueError(f"authority binding is missing/outside workspace: {relative}")
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(f"authority hash drift: {relative}")
        bindings[relative] = actual
    if not REQUIRED_UNCHANGED.issubset(bindings):
        raise ValueError("plan does not bind every protected authority file")
    runner_rel = "control/tools/run_bounded_official_source_probe.py"
    if bindings.get(runner_rel) != sha256_file(Path(__file__).resolve()):
        raise ValueError("plan does not bind this exact runner")
    return bindings


class SingleCallSession:
    """Reject any second HTTP call from a probe, including implicit retries."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.call_count = 0

    def request(self, *args: Any, **kwargs: Any) -> Any:
        if self.call_count >= 1:
            raise RuntimeError("bounded probe attempted more than one HTTP call")
        self.call_count += 1
        return self._session.request(*args, **kwargs)

    def close(self) -> None:
        self._session.close()


def observed_fields(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({key for row in rows for key in row})


def parse_szse_security_master(path: Path) -> dict[str, Any]:
    """Parse the SZSE workbook without trusting its declared used range.

    The official export has been observed with ``dimension ref=\"A1\"`` even
    when the worksheet contains thousands of rows.  In read-only mode
    openpyxl otherwise trusts that declaration and exposes only A1.  Resetting
    dimensions is therefore necessary before iterating such a workbook.
    Content still fails closed: a ZIP/XLSX signature alone is not sufficient.
    """

    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        declared_dimension = sheet.calculate_dimension()
        dimensions_reset = declared_dimension.upper() in {"A1", "A1:A1"}
        if dimensions_reset:
            sheet.reset_dimensions()

        fields: list[str] = []
        business_rows = 0
        for row in sheet.iter_rows(values_only=True):
            nonempty = [
                value
                for value in row
                if value is not None and str(value).strip()
            ]
            if not nonempty:
                continue
            if not fields:
                fields = [
                    str(value).strip()
                    for value in row
                    if value is not None and str(value).strip()
                ]
                continue
            business_rows += 1
    finally:
        workbook.close()

    missing = sorted(SZSE_REQUIRED_FIELDS - set(fields))
    content_valid = business_rows > 0 and not missing
    return {
        "declared_worksheet_dimension": declared_dimension,
        "worksheet_dimensions_reset": dimensions_reset,
        "observed_row_count": business_rows,
        "business_rows": business_rows,
        "observed_fields": fields,
        "required_business_fields": sorted(SZSE_REQUIRED_FIELDS),
        "required_business_fields_present": not missing,
        "missing_required_business_fields": missing,
        "post_capture_content_validation_passed": content_valid,
    }


def execute_probe(
    probe_id: str,
    operation: Callable[[Any, Path], dict[str, Any]],
    raw_root: Path,
    requests_module: Any,
    pilot: Any,
) -> dict[str, Any]:
    session = requests_module.Session()
    guards = pilot.harden_session(session)
    session.headers.update({"Accept-Encoding": "gzip, deflate"})
    bounded = SingleCallSession(session)
    try:
        result = operation(bounded, raw_root)
        status = "PASS_FOR_BOUNDED_CAPTURE" if result.get("capture_validation_passed") else "CONTENT_VALIDATION_FAILED"
        return {
            "probe_id": probe_id,
            "status": status,
            "http_call_count": bounded.call_count,
            "retry_count": 0,
            "credential_guards": guards,
            **result,
        }
    except Exception as exc:
        return {
            "probe_id": probe_id,
            "status": "FAIL_CLOSED",
            "http_call_count": bounded.call_count,
            "retry_count": 0,
            "credential_guards": guards,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    finally:
        bounded.close()


def enrich_result(result: dict[str, Any], raw_root: Path) -> None:
    probe_id = result["probe_id"]
    try:
        if probe_id == PROBE_ORDER[0]:
            payload = load_json(raw_root / "business" / "sse-security-master-page-1.json")
            rows = payload.get("result") or []
            result["observed_row_count"] = len(rows)
            result["observed_fields"] = observed_fields(rows)
        elif probe_id == PROBE_ORDER[1]:
            path = raw_root / "business" / "szse-a-share-current.xlsx"
            parsed = parse_szse_security_master(path)
            result.update(parsed)
            if parsed["post_capture_content_validation_passed"]:
                result["capture_validation_passed"] = True
                result["post_capture_parse_status"] = "PASS_LOCAL_CONTENT_GATE"
                result["status"] = "PASS_FOR_BOUNDED_CAPTURE"
            else:
                result["capture_validation_passed"] = False
                result["post_capture_parse_status"] = "FAIL_CLOSED"
                result["status"] = "FAIL_CLOSED"
        elif probe_id == PROBE_ORDER[2]:
            payload = load_json(raw_root / "business" / "cninfo-corporate-action-metadata.json")
            rows = payload.get("announcements") or []
            result["observed_row_count"] = len(rows)
            result["observed_fields"] = observed_fields(rows)
    except Exception as exc:
        result["post_capture_parse_status"] = "FAIL_CLOSED"
        result["post_capture_parse_error"] = f"{type(exc).__name__}: {exc}"
        if probe_id == PROBE_ORDER[1]:
            result["capture_validation_passed"] = False
            result["status"] = "FAIL_CLOSED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--validate-plan-only", action="store_true")
    parser.add_argument("--network", action="store_true")
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parents[2]
    plan_path = args.plan.resolve()
    if not plan_path.is_file():
        raise SystemExit(f"missing execution plan: {plan_path}")
    plan = load_json(plan_path)
    bindings = validate_plan(plan, workspace, plan_path)
    if args.validate_plan_only:
        print(json.dumps({"plan_id": plan["plan_id"], "status": "VALID", "authority_bindings": len(bindings)}, ensure_ascii=False))
        return 0
    if not args.network:
        raise SystemExit("refusing business calls without --network")

    output = plan_path.parent.resolve()
    for relative in ("RESULTS.json", "REPORT_MANIFEST.json"):
        if (output / relative).exists():
            raise SystemExit(f"refusing to overwrite evidence: {relative}")
    raw_root = output / "raw"
    if raw_root.exists():
        raise SystemExit("refusing to reuse an existing raw directory")
    raw_root.mkdir(parents=True)

    pilot = load_pilot(workspace)
    try:
        import requests
    except ImportError as exc:
        raise SystemExit(f"pinned requests runtime is required: {exc}") from exc

    before = {path: sha256_file(workspace / path) for path in bindings}
    started = dt.datetime.now(dt.timezone.utc)
    operations = {
        PROBE_ORDER[0]: pilot.probe_sse_security_master,
        PROBE_ORDER[1]: pilot.probe_szse_security_master,
        PROBE_ORDER[2]: pilot.probe_cninfo_metadata,
    }
    results: list[dict[str, Any]] = []
    for probe_id in PROBE_ORDER:
        result = execute_probe(probe_id, operations[probe_id], raw_root, requests, pilot)
        if result.get("status") != "FAIL_CLOSED":
            enrich_result(result, raw_root)
        results.append(result)

    after = {path: sha256_file(workspace / path) for path in bindings}
    unchanged = before == after
    payload = {
        "schema_version": 1,
        "plan_id": plan["plan_id"],
        "run_id": plan["run_id"],
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_effect": "NONE",
        "started_at_utc": started.isoformat(),
        "finished_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "execution_plan": {"path": plan_path.relative_to(workspace).as_posix(), "sha256": sha256_file(plan_path)},
        "probes": results,
        "fail_closed_sources": plan["fail_closed_sources"],
        "safety": {
            "formal_source_admission": False,
            "catalog_writes": 0,
            "quality_promotions": 0,
            "paid_or_credentialed_calls": 0,
            "pdf_body_downloads": 0,
            "historical_backfill": False,
            "authority_files_unchanged": unchanged,
        },
        "summary": {
            "bounded_capture_passes": sum(item["status"] == "PASS_FOR_BOUNDED_CAPTURE" for item in results),
            "content_validation_failures": sum(item["status"] == "CONTENT_VALIDATION_FAILED" for item in results),
            "fail_closed": sum(item["status"] == "FAIL_CLOSED" for item in results),
            "formal_source_admissions": 0,
        },
        "reusability": "RAW_AND_RECEIPTS_REUSABLE_FOR_INSPECTION_ONLY__NOT_RESEARCH_INPUT",
    }
    pilot.write_json(output / "RESULTS.json", payload)
    manifest = {
        "schema_version": 1,
        "plan_id": plan["plan_id"],
        "self_exclusion": "REPORT_MANIFEST.json is not listed in itself",
        "files": [
            {"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
            for path in sorted(item for item in output.rglob("*") if item.is_file() and item.name != "REPORT_MANIFEST.json")
        ],
        "formal_effect": "NONE",
    }
    pilot.write_json(output / "REPORT_MANIFEST.json", manifest)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    if not unchanged:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
