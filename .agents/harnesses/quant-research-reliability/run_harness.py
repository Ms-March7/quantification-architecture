#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from adapters import mcp_probe_adapter, report_audit_adapter, skill_script_adapter
from oracles import finance_assertions, report_assertions, safety_assertions, schema_assertions


STATUS_ORDER = {"PASS": 0, "REVIEW": 1, "FAIL": 2}
ADAPTERS = {
    "skill_script_adapter": skill_script_adapter,
    "report_audit_adapter": report_audit_adapter,
    "mcp_probe_adapter": mcp_probe_adapter,
}
ASSERTIONS = {}
ASSERTIONS.update(schema_assertions.ASSERTIONS)
ASSERTIONS.update(report_assertions.ASSERTIONS)
ASSERTIONS.update(safety_assertions.ASSERTIONS)
ASSERTIONS.update(finance_assertions.ASSERTIONS)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def aggregate_status(statuses: list[str]) -> str:
    if not statuses:
        return "PASS"
    return max(statuses, key=lambda status: STATUS_ORDER.get(status, 2))


def select_cases(cases: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.case:
        wanted = set(args.case)
        selected = [case for case in cases if case.get("id") in wanted]
        missing = sorted(wanted - {case.get("id") for case in selected})
        if missing:
            raise SystemExit(f"Unknown case id(s): {', '.join(missing)}")
        return selected

    active = [case for case in cases if case.get("status") == "active"]
    if args.live:
        return [case for case in active if case.get("mode") == "live"]
    if args.full:
        return [case for case in active if case.get("mode") in {"quick", "full"}]
    return [case for case in active if case.get("mode") == "quick"]


def target_for_case(case: dict[str, Any], targets_by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    target_id = case.get("target")
    if not target_id:
        return None
    target = targets_by_id.get(str(target_id))
    if not target:
        raise SystemExit(f"Case {case.get('id')} references unknown target {target_id!r}")
    return target


def run_adapter(
    *,
    case: dict[str, Any],
    target: dict[str, Any] | None,
    harness_root: Path,
    run_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    if not target:
        return {"status": "PASS", "message": "registry-only case", "commands": [], "artifacts": []}
    adapter_name = target.get("adapter")
    if not adapter_name:
        return {"status": "PASS", "message": "target has no adapter", "commands": [], "artifacts": []}
    adapter = ADAPTERS.get(adapter_name)
    if not adapter:
        return {
            "status": "FAIL",
            "message": f"unknown adapter {adapter_name!r}",
            "commands": [],
            "artifacts": [],
        }
    return adapter.run_case(
        case=case,
        target=target,
        harness_root=harness_root,
        run_dir=run_dir,
        timeout_seconds=timeout_seconds,
    )


def run_assertions(
    *,
    case: dict[str, Any],
    target: dict[str, Any] | None,
    targets_by_id: dict[str, dict[str, Any]],
    harness_root: Path,
    run_dir: Path,
    adapter_result: dict[str, Any],
    all_command_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    context = {
        "case": case,
        "target": target,
        "targets_by_id": targets_by_id,
        "harness_root": harness_root,
        "run_dir": run_dir,
        "adapter_result": adapter_result,
        "command_results": adapter_result.get("commands") or [],
        "all_command_results": all_command_results,
    }
    results: list[dict[str, Any]] = []
    adapter_status = adapter_result.get("status", "PASS")
    if adapter_status != "PASS":
        results.append(
            {
                "name": "adapter_execution",
                "status": adapter_status,
                "message": adapter_result.get("message", "adapter did not pass"),
                "evidence": [],
            }
        )
    for assertion_name in case.get("assertions") or []:
        assertion = ASSERTIONS.get(assertion_name)
        if not assertion:
            results.append(
                {
                    "name": assertion_name,
                    "status": "FAIL",
                    "message": "unknown assertion",
                    "evidence": [],
                }
            )
            continue
        try:
            results.append(assertion(context))
        except Exception as exc:  # pragma: no cover - report unexpected oracle errors
            results.append(
                {
                    "name": assertion_name,
                    "status": "FAIL",
                    "message": f"assertion raised {type(exc).__name__}: {exc}",
                    "evidence": [],
                }
            )
    return results


def run_case(
    *,
    case: dict[str, Any],
    targets_by_id: dict[str, dict[str, Any]],
    harness_root: Path,
    run_dir: Path,
    timeout_seconds: int,
    all_command_results: list[dict[str, Any]],
) -> dict[str, Any]:
    start = time.monotonic()
    target = target_for_case(case, targets_by_id)
    case_run_dir = run_dir / "cases" / str(case.get("id"))
    case_run_dir.mkdir(parents=True, exist_ok=True)
    adapter_result = run_adapter(
        case=case,
        target=target,
        harness_root=harness_root,
        run_dir=case_run_dir,
        timeout_seconds=timeout_seconds,
    )
    all_command_results.extend(adapter_result.get("commands") or [])
    assertion_run_dir = run_dir if (not target and case.get("targets")) else case_run_dir
    assertion_results = run_assertions(
        case=case,
        target=target,
        targets_by_id=targets_by_id,
        harness_root=harness_root,
        run_dir=assertion_run_dir,
        adapter_result=adapter_result,
        all_command_results=all_command_results,
    )
    status = aggregate_status([item.get("status", "FAIL") for item in assertion_results])
    duration = time.monotonic() - start
    return {
        "id": case.get("id"),
        "group": case.get("group"),
        "mode": case.get("mode"),
        "target": case.get("target") or ",".join(case.get("targets") or []),
        "expected_status": str(case.get("expected_status", "")).upper(),
        "status": status,
        "run_dir": case_run_dir.as_posix(),
        "duration_seconds": duration,
        "adapter": (target or {}).get("adapter", "registry"),
        "adapter_result": adapter_result,
        "assertions": assertion_results,
        "reliability_question": case.get("reliability_question", "").strip(),
        "failure_meaning": case.get("failure_meaning", "").strip(),
    }


def markdown_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def write_report(
    *,
    report_path: Path,
    latest_path: Path,
    mode: str,
    overall: str,
    harness_root: Path,
    run_dir: Path,
    targets_path: Path,
    cases_path: Path,
    results: list[dict[str, Any]],
    command_results: list[dict[str, Any]],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Quant Research Reliability Harness Report",
        "",
        f"- generated_at: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        f"- mode: {mode}",
        f"- overall_status: {overall}",
        f"- harness_root: {harness_root.as_posix()}",
        f"- run_dir: {run_dir.as_posix()}",
        f"- targets: {targets_path.as_posix()}",
        f"- cases: {cases_path.as_posix()}",
        "",
        "## Case Results",
        "",
        "| Case | Target | Expected | Actual | Duration | Adapter | Case Run Dir |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for item in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_escape(item.get("id")),
                    markdown_escape(item.get("target")),
                    markdown_escape(item.get("expected_status") or ""),
                    markdown_escape(item.get("status")),
                    f"{item.get('duration_seconds', 0):.2f}s",
                    markdown_escape(item.get("adapter")),
                    markdown_escape(item.get("run_dir")),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Findings", ""])
    interesting = [item for item in results if item.get("status") in {"FAIL", "REVIEW"}]
    if not interesting:
        lines.append("- No FAIL or REVIEW cases.")
    for item in interesting:
        lines.extend([f"### {item.get('id')} - {item.get('status')}", ""])
        if item.get("reliability_question"):
            lines.append(f"- reliability_question: {item['reliability_question']}")
        if item.get("failure_meaning"):
            lines.append(f"- failure_meaning: {item['failure_meaning']}")
        for assertion in item.get("assertions") or []:
            if assertion.get("status") not in {"FAIL", "REVIEW"}:
                continue
            lines.append(
                f"- {assertion.get('status')} `{assertion.get('name')}`: {assertion.get('message')}"
            )
            for evidence in assertion.get("evidence") or []:
                lines.append(f"  - {evidence}")
        lines.append("")
    lines.extend(["## Assertion Detail", ""])
    for item in results:
        lines.extend([f"### {item.get('id')}", ""])
        lines.append("| Assertion | Status | Message |")
        lines.append("| --- | --- | --- |")
        for assertion in item.get("assertions") or []:
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_escape(assertion.get("name")),
                        markdown_escape(assertion.get("status")),
                        markdown_escape(assertion.get("message")),
                    ]
                )
                + " |"
            )
        lines.append("")
    lines.extend(["## Commands", ""])
    if not command_results:
        lines.append("- No commands were run.")
    else:
        lines.append("| Exit | Duration | CWD | Command |")
        lines.append("| ---: | ---: | --- | --- |")
        for command in command_results:
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_escape(command.get("exit_code")),
                        f"{float(command.get('duration_seconds', 0)):.2f}s",
                        markdown_escape(command.get("cwd")),
                        markdown_escape(" ".join(command.get("command") or [])),
                    ]
                )
                + " |"
            )
    lines.extend(
        [
            "",
            "## Safety Confirmation",
            "",
            "- No inventory apply was performed.",
            "- No skill folder was edited by the harness run.",
            "- No migration, archive, merge, delete, plugin packaging, or dependency installation was performed.",
            "- Live network probes were not run unless mode was explicitly live.",
            "",
            "## Remaining Risks",
            "",
            "- Offline fixtures are synthetic and intentionally small.",
            "- REVIEW cases indicate evidence quality or safety questions that need human interpretation.",
            "- Future live MCP checks are deferred until `quant_data` exists.",
        ]
    )
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(report_path, latest_path)


def list_cases(cases: list[dict[str, Any]]) -> None:
    print("case_id\tstatus\tmode\tgroup\ttarget")
    for case in cases:
        target = case.get("target") or ",".join(case.get("targets") or [])
        print(f"{case.get('id')}\t{case.get('status')}\t{case.get('mode')}\t{case.get('group')}\t{target}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Quantification research reliability harness cases.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="Run active quick cases. Default.")
    mode.add_argument("--full", action="store_true", help="Run active quick and full cases.")
    mode.add_argument("--live", action="store_true", help="Run active live cases only.")
    parser.add_argument("--case", action="append", help="Run a specific case id. May be repeated.")
    parser.add_argument("--list", action="store_true", help="List registered cases and exit.")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    harness_root = Path(__file__).resolve().parent
    targets_path = harness_root / "targets.yaml"
    cases_path = harness_root / "cases.yaml"
    targets_doc = load_yaml(targets_path)
    cases_doc = load_yaml(cases_path)
    targets = targets_doc.get("targets") or []
    cases = cases_doc.get("cases") or []
    if args.list:
        list_cases(cases)
        return 0

    selected = select_cases(cases, args)
    if not selected:
        print("No cases selected.")
        return 0

    targets_by_id = {target["id"]: target for target in targets}
    defaults = targets_doc.get("defaults") or {}
    temp_root = Path(defaults.get("temp_root") or (harness_root / "tmp")).resolve()
    report_root = Path(defaults.get("report_root") or (harness_root / "reports")).resolve()
    run_dir = temp_root / now_stamp()
    run_dir.mkdir(parents=True, exist_ok=False)

    command_results: list[dict[str, Any]] = []
    results = [
        run_case(
            case=case,
            targets_by_id=targets_by_id,
            harness_root=harness_root,
            run_dir=run_dir,
            timeout_seconds=args.timeout_seconds,
            all_command_results=command_results,
        )
        for case in selected
    ]
    overall = aggregate_status([item.get("status", "FAIL") for item in results])
    mode_name = "live" if args.live else "full" if args.full else "case" if args.case else "quick"
    report_path = report_root / f"harness-report-{run_dir.name}.md"
    latest_path = report_root / "harness-report.md"
    write_report(
        report_path=report_path,
        latest_path=latest_path,
        mode=mode_name,
        overall=overall,
        harness_root=harness_root,
        run_dir=run_dir,
        targets_path=targets_path,
        cases_path=cases_path,
        results=results,
        command_results=command_results,
    )

    print(f"mode={mode_name}")
    print(f"overall_status={overall}")
    print(f"run_dir={run_dir}")
    print(f"report={report_path}")
    return 1 if overall == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
