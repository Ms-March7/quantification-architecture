from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def result(name: str, status: str, message: str, evidence: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, "evidence": evidence or []}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def missing_prices_are_flagged(context: dict[str, Any]) -> dict[str, Any]:
    path = context["run_dir"] / "outputs/event-study/event_windows.csv"
    rows = _read_csv(path)
    statuses = sorted({row.get("status", "") for row in rows})
    missing = [status for status in statuses if status in {"missing_price_series", "no_reaction_date", "no_base_close"}]
    if missing:
        return result("missing_prices_are_flagged", "REVIEW", "missing price/reaction status surfaced", missing)
    return result("missing_prices_are_flagged", "FAIL", "missing price/reaction issue was not surfaced", statuses)


def sample_size_is_visible(context: dict[str, Any]) -> dict[str, Any]:
    summary = context["run_dir"] / "outputs/event-study/summary.md"
    text = summary.read_text(encoding="utf-8", errors="replace") if summary.exists() else ""
    if "retained_events" in text and "usable_events" in text:
        return result("sample_size_is_visible", "PASS", "sample size fields visible")
    return result("sample_size_is_visible", "FAIL", "sample size fields not visible")


def duplicate_events_are_visible_or_deduped(context: dict[str, Any]) -> dict[str, Any]:
    filtered = context["run_dir"] / "outputs/event-study/filtered_out_events.csv"
    rows = _read_csv(filtered)
    reasons = sorted({row.get("filtered_reason", "") for row in rows})
    if any("overlap_deduped" in reason or "duplicate" in reason for reason in reasons):
        return result("duplicate_events_are_visible_or_deduped", "PASS", "duplicate/overlap handling visible", reasons)
    return result("duplicate_events_are_visible_or_deduped", "FAIL", "duplicate/overlap handling not visible", reasons)


def filtered_out_events_are_explained(context: dict[str, Any]) -> dict[str, Any]:
    filtered = context["run_dir"] / "outputs/event-study/filtered_out_events.csv"
    rows = _read_csv(filtered)
    if not rows:
        return result("filtered_out_events_are_explained", "REVIEW", "no filtered rows to explain")
    missing = [str(index) for index, row in enumerate(rows, start=2) if not row.get("filtered_reason")]
    if missing:
        return result("filtered_out_events_are_explained", "FAIL", "filtered rows missing reasons", missing)
    return result("filtered_out_events_are_explained", "PASS", "filtered rows include reasons")


def attribution_fixture_expectation_matches(context: dict[str, Any]) -> dict[str, Any]:
    path = context["run_dir"] / "outputs/capital-flow-attribution/data/validation.json"
    if not path.exists():
        return result("attribution_fixture_expectation_matches", "FAIL", "validation.json not found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return result("attribution_fixture_expectation_matches", "FAIL", f"invalid validation.json: {exc}")
    evidence = [
        f"expected={payload.get('expected_workflow_status')}",
        f"actual={payload.get('actual_workflow_status')}",
        f"test_status={payload.get('test_status')}",
    ]
    if payload.get("expectation_matched") is True and payload.get("test_status") == "PASS":
        return result("attribution_fixture_expectation_matches", "PASS", "workflow state matched fixture expectation", evidence)
    return result("attribution_fixture_expectation_matches", "FAIL", "workflow state did not match fixture expectation", evidence)


def mcp_tool_exists(context: dict[str, Any]) -> dict[str, Any]:
    return result("mcp_tool_exists", "REVIEW", "quant_data MCP is deferred")


def probe_response_has_schema_version(context: dict[str, Any]) -> dict[str, Any]:
    return result("probe_response_has_schema_version", "REVIEW", "quant_data MCP is deferred")


def probe_response_has_source_metadata(context: dict[str, Any]) -> dict[str, Any]:
    return result("probe_response_has_source_metadata", "REVIEW", "quant_data MCP is deferred")


def probe_response_handles_empty_results(context: dict[str, Any]) -> dict[str, Any]:
    return result("probe_response_handles_empty_results", "REVIEW", "quant_data MCP is deferred")


ASSERTIONS = {
    "missing_prices_are_flagged": missing_prices_are_flagged,
    "sample_size_is_visible": sample_size_is_visible,
    "duplicate_events_are_visible_or_deduped": duplicate_events_are_visible_or_deduped,
    "filtered_out_events_are_explained": filtered_out_events_are_explained,
    "attribution_fixture_expectation_matches": attribution_fixture_expectation_matches,
    "mcp_tool_exists": mcp_tool_exists,
    "probe_response_has_schema_version": probe_response_has_schema_version,
    "probe_response_has_source_metadata": probe_response_has_source_metadata,
    "probe_response_handles_empty_results": probe_response_handles_empty_results,
}
