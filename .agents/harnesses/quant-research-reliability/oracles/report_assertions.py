from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


def result(name: str, status: str, message: str, evidence: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, "evidence": evidence or []}


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _case_texts(context: dict[str, Any]) -> dict[str, str]:
    run_dir: Path = context["run_dir"]
    texts = {}
    for path in run_dir.rglob("*.md"):
        texts[path.as_posix()] = _read_text(path)
    for path in run_dir.rglob("*.html"):
        texts[path.as_posix()] = _read_text(path)
    for artifact in context.get("adapter_result", {}).get("artifacts") or []:
        path = Path(artifact)
        if path.suffix.lower() in {".md", ".html", ".txt"}:
            texts[path.as_posix()] = _read_text(path)
    return texts


def _combined_text(context: dict[str, Any]) -> str:
    return "\n".join(_case_texts(context).values()).lower()


def date_range_is_explicit(context: dict[str, Any]) -> dict[str, Any]:
    params = context.get("case", {}).get("params") or {}
    start = str(params.get("start_date", "")).strip()
    end = str(params.get("end_date", "")).strip()
    if not start and not end:
        return result("date_range_is_explicit", "PASS", "case has no explicit date range requirement")
    text = _combined_text(context)
    missing = [value for value in [start, end] if value and value.lower() not in text]
    if missing:
        return result("date_range_is_explicit", "FAIL", "date range not visible in report text", missing)
    return result("date_range_is_explicit", "PASS", "date range visible", [start, end])


def report_mentions_source_and_adjustment(context: dict[str, Any]) -> dict[str, Any]:
    text = _combined_text(context)
    has_source = "source" in text or "csv:" in text or "data source" in text
    has_adjust = "adjust" in text or "qfq" in text or "csv:" in text
    if not has_source or not has_adjust:
        return result("report_mentions_source_and_adjustment", "FAIL", "source or adjustment/source mode not visible")
    return result("report_mentions_source_and_adjustment", "PASS", "source and adjustment/source mode visible")


def report_mentions_stale_data(context: dict[str, Any]) -> dict[str, Any]:
    run_dir: Path = context["run_dir"]
    coverage = run_dir / "outputs/capital-flow/data/coverage.csv"
    text = _combined_text(context)
    stale_visible = "stale" in text or "older than" in text
    if coverage.exists():
        coverage_text = coverage.read_text(encoding="utf-8-sig", errors="replace").lower()
        stale_visible = stale_visible or "stale" in coverage_text
    if stale_visible:
        return result("report_mentions_stale_data", "REVIEW", "stale evidence is visible")
    return result("report_mentions_stale_data", "FAIL", "stale evidence was not surfaced")


def source_timestamp_is_visible(context: dict[str, Any]) -> dict[str, Any]:
    text = _combined_text(context)
    run_dir: Path = context["run_dir"]
    for path in run_dir.rglob("*.csv"):
        text += "\n" + path.read_text(encoding="utf-8-sig", errors="replace").lower()
    if "fetch_time" in text or re.search(r"[0-9]{4}-[0-9]{2}-[0-9]{2}t[0-9]{2}:", text):
        return result("source_timestamp_is_visible", "PASS", "source timestamp is visible")
    return result("source_timestamp_is_visible", "FAIL", "source timestamp not visible")


def no_strong_conclusion(context: dict[str, Any]) -> dict[str, Any]:
    text = _combined_text(context)
    banned = ["must buy", "guaranteed", "will definitely", "no risk", "all in"]
    found = [term for term in banned if term in text]
    if found:
        return result("no_strong_conclusion", "FAIL", "strong conclusion language found", found)
    return result("no_strong_conclusion", "PASS", "no strong investment-style conclusion found")


def weak_evidence_does_not_get_strong_language(context: dict[str, Any]) -> dict[str, Any]:
    contract_path = context["harness_root"] / "contracts/report-safety.schema.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) if contract_path.exists() else {}
    text = _combined_text(context)
    weak_terms = [term for term in contract.get("weak_evidence_markers", []) if term in text]
    strong_terms = [term for term in contract.get("strong_claim_terms", []) if term in text]
    if weak_terms and strong_terms:
        return result(
            "weak_evidence_does_not_get_strong_language",
            "REVIEW",
            "weak evidence appears with strong conclusion language",
            strong_terms,
        )
    return result("weak_evidence_does_not_get_strong_language", "PASS", "no weak-evidence overclaim detected")


def report_distinguishes_observation_from_advice(context: dict[str, Any]) -> dict[str, Any]:
    text = _combined_text(context)
    markers = ["not investment advice", "historical observation", "does not constitute", "evidence"]
    if any(marker in text for marker in markers):
        return result("report_distinguishes_observation_from_advice", "PASS", "advice boundary visible")
    return result("report_distinguishes_observation_from_advice", "REVIEW", "advice boundary is not explicit")


def sample_limitations_are_visible(context: dict[str, Any]) -> dict[str, Any]:
    text = _combined_text(context)
    markers = ["sample", "usable_events", "small sample", "limited evidence", "stale", "missing"]
    if any(marker in text for marker in markers):
        return result("sample_limitations_are_visible", "PASS", "sample limitation marker visible")
    return result("sample_limitations_are_visible", "REVIEW", "sample limitation marker not visible")


def data_source_and_date_range_are_visible(context: dict[str, Any]) -> dict[str, Any]:
    text = _combined_text(context)
    has_source = "source" in text or "fixture://" in text
    has_date = bool(re.search(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text))
    if has_source and has_date:
        return result("data_source_and_date_range_are_visible", "PASS", "source and date markers visible")
    return result("data_source_and_date_range_are_visible", "FAIL", "source or date marker missing")


ASSERTIONS = {
    "date_range_is_explicit": date_range_is_explicit,
    "report_mentions_source_and_adjustment": report_mentions_source_and_adjustment,
    "report_mentions_stale_data": report_mentions_stale_data,
    "source_timestamp_is_visible": source_timestamp_is_visible,
    "no_strong_conclusion": no_strong_conclusion,
    "weak_evidence_does_not_get_strong_language": weak_evidence_does_not_get_strong_language,
    "report_distinguishes_observation_from_advice": report_distinguishes_observation_from_advice,
    "sample_limitations_are_visible": sample_limitations_are_visible,
    "data_source_and_date_range_are_visible": data_source_and_date_range_are_visible,
}
