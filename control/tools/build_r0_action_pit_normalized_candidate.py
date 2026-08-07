#!/usr/bin/env python3
"""Build a conservative, inspection-only action/PIT candidate.

The builder deliberately preserves unresolved publication precision,
availability, effectiveness and supersession fields as null/unknown. It never
promotes CNINFO metadata or title text into authoritative event semantics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


INPUT_REL = Path(
    "data/market/quarantine/r0-official-action-pit/intake-20260801-v1/"
    "normalized/cninfo_action_metadata.jsonl"
)
OUTPUT_REL = Path(
    "data/market/quarantine/r0-action-pit-normalized/"
    "candidate-20260801-v1"
)
REPORT_REL = Path(
    "reports/data-governance/r0-action-pit-normalized-candidate-20260801-v1"
)
GUARDED_FILES = (
    Path("control/ROADMAP.md"),
    Path("control/WORKSPACE_INDEX.md"),
    Path("control/projects.yaml"),
    Path("data/catalog/datasets.json"),
    Path("data/catalog/contracts/source_admission_v1/current_source_registry.json"),
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def file_record(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def guard_snapshot(root: Path) -> dict[str, str]:
    return {p.as_posix(): sha256_file(root / p) for p in GUARDED_FILES}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_no}: expected object")
            rows.append(value)
    return rows


def title_lifecycle_hint(title: str) -> str:
    if any(marker in title for marker in ("已取消", "撤回", "取消")):
        return "WITHDRAWAL_OR_CANCELLATION_TITLE_HINT_ONLY"
    if any(marker in title for marker in ("更正", "补充")):
        return "CORRECTION_OR_SUPPLEMENT_TITLE_HINT_ONLY"
    return "NO_AUTHORITATIVE_LIFECYCLE_STATE_FROM_TITLE"


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    announcement_id = str(row.get("announcement_id") or "").strip()
    security_code = str(row.get("security_code") or "").strip()
    if not announcement_id or not security_code:
        raise ValueError("announcement_id and security_code are required")
    matches = row.get("lifecycle_query_matches") or []
    roles = sorted(
        {
            str(item.get("lifecycle_role"))
            for item in matches
            if isinstance(item, dict) and item.get("lifecycle_role")
        }
    )
    related = str(row.get("associate_announcement_source_value") or "").strip()
    return {
        "event_candidate_id": f"CNINFO:{announcement_id}",
        "source": "CNINFO_OFFICIAL_DISCLOSURE_PLATFORM",
        "source_event_id": announcement_id,
        "local_security_code": security_code,
        "permanent_security_id": None,
        "security_name_observed": row.get("security_name"),
        "organization_id_observed": row.get("organization_id"),
        "announcement_title": row.get("announcement_title"),
        "publication_date_candidate": row.get("published_date_candidate"),
        "published_at_candidate": row.get("published_at_candidate_asia_shanghai"),
        "published_at_precision": "DATE_ONLY_OR_MIDNIGHT_RISK",
        "available_at": None,
        "effective_at": None,
        "revision_id": announcement_id,
        "supersedes": None,
        "related_source_ids_observed": [related] if related else [],
        "lifecycle_query_roles": roles,
        "lifecycle_title_hint": title_lifecycle_hint(
            str(row.get("announcement_title") or "")
        ),
        "withdrawal_state": "UNKNOWN",
        "economic_terms": None,
        "raw_record_sha256": row.get("raw_record_sha256"),
        "source_url": row.get("source_url"),
        "source_document_path_observed": row.get("adjunct_url_source_value"),
        "pit_status": (
            "CANDIDATE_ONLY__NO_AVAILABLE_AT__NO_EFFECTIVE_AT__"
            "NO_AUTHORITATIVE_SUPERSESSION"
        ),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build(root: Path) -> dict[str, Any]:
    input_path = root / INPUT_REL
    output_root = root / OUTPUT_REL
    report_root = root / REPORT_REL
    guard_before = guard_snapshot(root)
    source_rows = load_jsonl(input_path)
    normalized = sorted(
        (normalize_row(row) for row in source_rows),
        key=lambda row: row["source_event_id"],
    )
    ids = [row["event_candidate_id"] for row in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate event_candidate_id")

    output_path = output_root / "ACTION_EVENT_CANDIDATES.jsonl"
    write_jsonl(output_path, normalized)
    role_counts = Counter(
        role for row in normalized for role in row["lifecycle_query_roles"]
    )
    hint_counts = Counter(row["lifecycle_title_hint"] for row in normalized)
    summary = {
        "schema_version": 1,
        "run_id": "r0-action-pit-normalized-candidate-20260801-v1",
        "purpose_ceiling": "INSPECTION_ONLY",
        "quality_status": "NOT_ADMITTED",
        "formal_effect": "NONE",
        "input": {
            "path": INPUT_REL.as_posix(),
            "rows": len(source_rows),
            "bytes": input_path.stat().st_size,
            "sha256": sha256_file(input_path),
        },
        "output": {
            "path": OUTPUT_REL.joinpath(output_path.name).as_posix(),
            "rows": len(normalized),
            "unique_event_candidate_ids": len(set(ids)),
            "bytes": output_path.stat().st_size,
            "sha256": sha256_file(output_path),
        },
        "null_preservation": {
            "permanent_security_id_null": sum(
                row["permanent_security_id"] is None for row in normalized
            ),
            "available_at_null": sum(row["available_at"] is None for row in normalized),
            "effective_at_null": sum(row["effective_at"] is None for row in normalized),
            "supersedes_null": sum(row["supersedes"] is None for row in normalized),
            "economic_terms_null": sum(
                row["economic_terms"] is None for row in normalized
            ),
        },
        "lifecycle_query_role_counts": dict(sorted(role_counts.items())),
        "title_hint_counts": dict(sorted(hint_counts.items())),
        "limitations": [
            "Midnight/date-precision publication candidates are not promoted to available_at.",
            "Related announcement identifiers are not promoted to supersedes.",
            "Title text creates a diagnostic hint only and never an authoritative withdrawal state.",
            "No permanent identity, effective date or economic terms are inferred.",
            "The 118-record official metadata sample is bounded and not complete history.",
        ],
    }
    write_json(output_root / "CANDIDATE_SUMMARY.json", summary)
    manifest = {
        "schema_version": 1,
        "run_id": summary["run_id"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose_ceiling": "INSPECTION_ONLY",
        "quality_status": "NOT_ADMITTED",
        "formal_effect": "NONE",
        "entries": [
            file_record(root, output_path),
            file_record(root, output_root / "CANDIDATE_SUMMARY.json"),
        ],
    }
    write_json(output_root / "CANDIDATE_MANIFEST.json", manifest)

    report = {
        "schema_version": 1,
        "run_id": summary["run_id"],
        "status": "PASS_CONSERVATIVE_NORMALIZATION__PIT_NOT_CLOSED",
        "source_rows": len(source_rows),
        "candidate_rows": len(normalized),
        "all_unknown_semantics_preserved": all(
            row["available_at"] is None
            and row["effective_at"] is None
            and row["supersedes"] is None
            and row["withdrawal_state"] == "UNKNOWN"
            for row in normalized
        ),
        "core_04_assessment": "PARTIAL__EVENT_TERMS_AND_PIT_CHAIN_UNCLOSED",
        "core_05_assessment": "BLOCKED__NO_FORMAL_CORE04_ACTION_PROJECTION",
        "formal_effect": "NONE",
    }
    write_json(report_root / "VERIFICATION.json", report)
    readme = """# R0 action/PIT normalized candidate — 2026-08-01

118 bounded CNINFO metadata observations were normalized without promoting
date-precision timestamps, related IDs or title text into authoritative PIT or
event-lifecycle semantics. All `available_at`, `effective_at`, `supersedes`,
permanent-identity and economic-term fields remain null where source evidence is
absent.

Result: conservative candidate construction passes, but CORE-04 remains partial
and CORE-05 remains blocked. This package has no formal catalog, snapshot,
quality or research effect.
"""
    (report_root / "README.md").parent.mkdir(parents=True, exist_ok=True)
    (report_root / "README.md").write_text(readme, encoding="utf-8")
    guard_after = guard_snapshot(root)
    if guard_before != guard_after:
        raise RuntimeError("guarded authority files changed")
    report_manifest = {
        "schema_version": 1,
        "run_id": summary["run_id"],
        "guarded_authority_unchanged": True,
        "guard_before": guard_before,
        "guard_after": guard_after,
        "entries": [
            file_record(root, report_root / "README.md"),
            file_record(root, report_root / "VERIFICATION.json"),
            file_record(root, output_root / "CANDIDATE_MANIFEST.json"),
        ],
    }
    write_json(report_root / "REPORT_MANIFEST.json", report_manifest)
    return report


def verify(root: Path) -> None:
    output_root = root / OUTPUT_REL
    report_root = root / REPORT_REL
    manifest = json.loads(
        (output_root / "CANDIDATE_MANIFEST.json").read_text(encoding="utf-8")
    )
    declared = {entry["path"] for entry in manifest["entries"]}
    expected = {
        OUTPUT_REL.joinpath("ACTION_EVENT_CANDIDATES.jsonl").as_posix(),
        OUTPUT_REL.joinpath("CANDIDATE_SUMMARY.json").as_posix(),
    }
    if declared != expected:
        raise ValueError("candidate manifest path set mismatch")
    for entry in manifest["entries"]:
        path = root / entry["path"]
        if path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
            raise ValueError(f"candidate manifest mismatch: {entry['path']}")
    verification = json.loads(
        (report_root / "VERIFICATION.json").read_text(encoding="utf-8")
    )
    if verification["formal_effect"] != "NONE":
        raise ValueError("formal effect must remain NONE")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    root = args.workspace.resolve()
    if args.verify_only:
        verify(root)
        print("PASS")
    else:
        print(json.dumps(build(root), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
