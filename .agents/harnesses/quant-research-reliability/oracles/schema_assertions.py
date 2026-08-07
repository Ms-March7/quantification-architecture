from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

import yaml


def result(name: str, status: str, message: str, evidence: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, "evidence": evidence or []}


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def _contracts(context: dict[str, Any], target: dict[str, Any] | None) -> list[tuple[Path, dict[str, Any]]]:
    harness_root: Path = context["harness_root"]
    contracts = []
    for rel in (target or {}).get("contracts") or []:
        path = (harness_root / rel).resolve()
        if path.exists():
            contracts.append((path, _load_yaml(path)))
    return contracts


def required_artifacts_exist(context: dict[str, Any]) -> dict[str, Any]:
    target = context.get("target")
    run_dir: Path = context["run_dir"]
    missing = []
    checked = []
    if target:
        for rel in target.get("expected_artifacts") or []:
            path = (run_dir / rel).resolve()
            checked.append(path.as_posix())
            if not path.exists():
                missing.append(path.as_posix())
    if not target and context.get("adapter_result"):
        checked = list(context["adapter_result"].get("artifacts") or [])
        missing = [path for path in checked if not Path(path).exists()]
    if missing:
        return result("required_artifacts_exist", "FAIL", "missing required artifacts", missing)
    return result("required_artifacts_exist", "PASS", f"checked {len(checked)} artifact(s)", checked)


def csv_has_required_columns(context: dict[str, Any]) -> dict[str, Any]:
    target = context.get("target")
    run_dir: Path = context["run_dir"]
    failures = []
    evidence = []
    for path, contract in _contracts(context, target):
        if contract.get("type") != "csv":
            continue
        artifact = contract.get("artifact")
        if not artifact:
            continue
        csv_path = (run_dir / artifact).resolve()
        if not csv_path.exists():
            failures.append(f"{csv_path}: missing")
            continue
        columns, rows = _read_csv(csv_path)
        required = list(contract.get("required_columns") or [])
        missing = [column for column in required if column not in columns]
        if missing:
            failures.append(f"{csv_path}: missing columns {missing}")
        non_empty = list(contract.get("required_non_empty") or [])
        for column in non_empty:
            if column in columns and any(not str(row.get(column, "")).strip() for row in rows):
                failures.append(f"{csv_path}: empty values in {column}")
        numeric = list(contract.get("numeric_columns") or [])
        for column in numeric:
            for index, row in enumerate(rows, start=2):
                text = str(row.get(column, "")).strip()
                if not text:
                    continue
                try:
                    float(text)
                except ValueError:
                    failures.append(f"{csv_path}:{index}: {column} is not numeric")
                    break
        evidence.append(f"{contract.get('name', path.name)} -> {csv_path} rows={len(rows)}")
    if failures:
        return result("csv_has_required_columns", "FAIL", "CSV contract violation", failures)
    return result("csv_has_required_columns", "PASS", "CSV contracts satisfied", evidence)


def symbols_match_requested_universe(context: dict[str, Any]) -> dict[str, Any]:
    params = context.get("case", {}).get("params") or {}
    requested = [item.strip() for item in str(params.get("symbols", "")).split(",") if item.strip()]
    if not requested:
        return result("symbols_match_requested_universe", "PASS", "no requested symbol set in case")
    ranking = context["run_dir"] / "outputs/stock-relative-dominance/ranking.csv"
    if not ranking.exists():
        return result("symbols_match_requested_universe", "FAIL", "ranking.csv not found")
    _, rows = _read_csv(ranking)
    actual = sorted({row.get("symbol", "").strip() for row in rows if row.get("symbol")})
    expected = sorted(requested)
    if actual != expected:
        return result("symbols_match_requested_universe", "FAIL", f"expected {expected}, got {actual}")
    return result("symbols_match_requested_universe", "PASS", "ranking symbols match requested universe", actual)


def dates_use_iso_format(context: dict[str, Any]) -> dict[str, Any]:
    run_dir: Path = context["run_dir"]
    date_columns = {"date", "event_date", "reaction_date", "reaction_date_used", "base_date", "trading_date"}
    pattern = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    failures = []
    checked = []
    for path in run_dir.rglob("*.csv"):
        columns, rows = _read_csv(path)
        columns_to_check = [column for column in columns if column in date_columns]
        for column in columns_to_check:
            checked.append(f"{path.name}:{column}")
            for index, row in enumerate(rows, start=2):
                value = str(row.get(column, "")).strip()
                if value and not pattern.match(value[:10]):
                    failures.append(f"{path}:{index}: {column}={value!r}")
                    break
    if failures:
        return result("dates_use_iso_format", "FAIL", "non-ISO date values found", failures)
    return result("dates_use_iso_format", "PASS", f"checked {len(checked)} date column(s)", checked)


def shared_symbol_format_is_documented(context: dict[str, Any]) -> dict[str, Any]:
    path = context["harness_root"] / "contracts/symbol-date.schema.yaml"
    if not path.exists():
        return result("shared_symbol_format_is_documented", "FAIL", "missing symbol-date contract")
    contract = _load_yaml(path)
    patterns = contract.get("allowed_symbol_patterns") or []
    if not patterns:
        return result("shared_symbol_format_is_documented", "FAIL", "symbol-date contract has no symbol patterns")
    return result("shared_symbol_format_is_documented", "PASS", f"documented {len(patterns)} symbol pattern(s)")


def outputs_have_status_or_quality_fields(context: dict[str, Any]) -> dict[str, Any]:
    run_dir: Path = context["run_dir"]
    expected = {
        "outputs/capital-flow/data/normalized_capital_flow.csv": ["missing_flags"],
        "outputs/event-study/event_windows.csv": ["status"],
        "outputs/stock-relative-dominance/failed_symbols.csv": ["error"],
    }
    missing = []
    evidence = []
    for rel, columns_needed in expected.items():
        path = run_dir / rel
        if not path.exists():
            continue
        columns, _ = _read_csv(path)
        absent = [column for column in columns_needed if column not in columns]
        if absent:
            missing.append(f"{rel}: missing {absent}")
        else:
            evidence.append(f"{rel}: {columns_needed}")
    if missing:
        return result("outputs_have_status_or_quality_fields", "FAIL", "missing quality/status fields", missing)
    return result("outputs_have_status_or_quality_fields", "PASS", "quality/status fields present where outputs exist", evidence)


def downstream_contracts_are_parseable(context: dict[str, Any]) -> dict[str, Any]:
    harness_root: Path = context["harness_root"]
    failures = []
    evidence = []
    for path in sorted((harness_root / "contracts").glob("*.yaml")):
        try:
            payload = _load_yaml(path)
        except Exception as exc:  # pragma: no cover - defensive report path
            failures.append(f"{path}: {exc}")
            continue
        if not payload.get("name"):
            failures.append(f"{path}: missing name")
        evidence.append(path.name)
    if failures:
        return result("downstream_contracts_are_parseable", "FAIL", "contract parse failures", failures)
    return result("downstream_contracts_are_parseable", "PASS", f"parsed {len(evidence)} contract(s)", evidence)


ASSERTIONS = {
    "required_artifacts_exist": required_artifacts_exist,
    "csv_has_required_columns": csv_has_required_columns,
    "symbols_match_requested_universe": symbols_match_requested_universe,
    "dates_use_iso_format": dates_use_iso_format,
    "shared_symbol_format_is_documented": shared_symbol_format_is_documented,
    "outputs_have_status_or_quality_fields": outputs_have_status_or_quality_fields,
    "downstream_contracts_are_parseable": downstream_contracts_are_parseable,
}
