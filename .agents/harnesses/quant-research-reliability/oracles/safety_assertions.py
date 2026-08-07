from __future__ import annotations

from pathlib import Path
from typing import Any


def result(name: str, status: str, message: str, evidence: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, "evidence": evidence or []}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def output_stays_under_run_dir(context: dict[str, Any]) -> dict[str, Any]:
    run_dir: Path = context["run_dir"]
    target = context.get("target")
    violations = []
    checked = []
    if target:
        for rel in target.get("expected_artifacts") or []:
            path = (run_dir / rel).resolve()
            checked.append(path.as_posix())
            if not _is_relative_to(path, run_dir):
                violations.append(path.as_posix())
    for command in context.get("command_results") or []:
        for token in command.get("command") or []:
            text = str(token)
            if "outputs/" in text or "outputs\\" in text:
                path = Path(text).resolve()
                checked.append(path.as_posix())
                if not _is_relative_to(path, run_dir):
                    violations.append(path.as_posix())
    if violations:
        return result("output_stays_under_run_dir", "FAIL", "output path escaped run directory", violations)
    return result("output_stays_under_run_dir", "PASS", f"checked {len(checked)} output path(s)", checked)


def generated_outputs_do_not_enter_skill_dirs(context: dict[str, Any]) -> dict[str, Any]:
    violations = []
    evidence = []
    for command in context.get("all_command_results") or context.get("command_results") or []:
        for token in command.get("command") or []:
            text = str(token).replace("\\", "/")
            if "/.agents/skills/" in text and ("outputs/" in text or "reports/" in text or "tmp/" in text):
                violations.append(text)
            elif "outputs/" in text or "outputs\\" in str(token):
                evidence.append(str(token))
    if violations:
        return result("generated_outputs_do_not_enter_skill_dirs", "FAIL", "output token points into a skill directory", violations)
    return result("generated_outputs_do_not_enter_skill_dirs", "PASS", "no generated output token enters skill directories", evidence)


ASSERTIONS = {
    "output_stays_under_run_dir": output_stays_under_run_dir,
    "generated_outputs_do_not_enter_skill_dirs": generated_outputs_do_not_enter_skill_dirs,
}
