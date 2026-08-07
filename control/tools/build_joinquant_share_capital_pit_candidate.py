#!/usr/bin/env python3
"""Build a conservative, date-level PIT share-capital candidate from sealed JQ rows.

The output is deliberately INSPECTION_ONLY.  Publication dates are mapped to
the next session of the exact formal R0 calendar; state cannot become usable
before the reported capital-change date.  Vendor units are preserved without
guessing a scale, and late disclosures of older states never overwrite a more
recent effective capital state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import uuid
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "data/market/quarantine/cn-a-share-corporate-actions-multisource/candidate-20260805-v2"
SOURCE = SOURCE_ROOT / "joinquant_raw/STK_CAPITAL_CHANGE.parquet"
SOURCE_MANIFEST = SOURCE_ROOT / "CANDIDATE_MANIFEST.json"
R0_SNAPSHOT = ROOT / "data/market/snapshots/cn-a-share-r0-shsz-pv-panel/20050104-20260803-v1"
RUN_ID = "joinquant-share-capital-pit-intervals-20260806-v1"
CANDIDATE_ID = "joinquant-share-capital-pit-intervals-20260806-v1"
PARENT = ROOT / "data/market/quarantine/cn-a-share-capital-pit"
FINAL = PARENT / CANDIDATE_ID
REPORT = ROOT / "reports/data-governance" / RUN_ID
EXPECTED_R0_HASH = "26309d36b7c08096fff55df100f1d27df8412b106ea48f282ce0f7caad37084b"


class BuildError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    connection = duckdb.connect(database=":memory:")
    try:
        connection.register("frame_to_write", frame)
        connection.execute(
            "COPY frame_to_write TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
            [str(temporary)],
        )
    finally:
        connection.close()
    os.replace(temporary, path)


def map_usable_session(publication: date, effective: date, sessions: list[date]) -> date | None:
    publication_index = bisect_right(sessions, publication)
    effective_index = bisect_left(sessions, effective)
    if publication_index >= len(sessions) or effective_index >= len(sessions):
        return None
    return max(sessions[publication_index], sessions[effective_index])


def _json_scalar(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value


def build_state_intervals(
    observations: pd.DataFrame,
    *,
    coverage_end_exclusive: date,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Reduce source observations to conservative half-open state intervals."""

    output: list[dict[str, Any]] = []
    ignored_stale = 0
    same_session_events = 0
    for order_book_id, group in observations.groupby("order_book_id", sort=True):
        records = group.sort_values(
            ["usable_from", "change_date", "pub_date", "source_id"], kind="mergesort"
        ).to_dict(orient="records")
        points: list[dict[str, Any]] = []
        active: dict[str, Any] | None = None
        active_change: date | None = None
        position = 0
        while position < len(records):
            usable = records[position]["usable_from"]
            end = position + 1
            while end < len(records) and records[end]["usable_from"] == usable:
                end += 1
            batch = records[position:end]
            same_session_events += max(0, len(batch) - 1)
            for record in batch:
                change = record["change_date"]
                if active_change is not None and change < active_change:
                    ignored_stale += 1
                    continue
                if active_change is None or change > active_change:
                    active = record
                    active_change = change
                elif active is None or (record["pub_date"], str(record["source_id"])) >= (
                    active["pub_date"], str(active["source_id"])
                ):
                    active = record
            if active is not None:
                state_material = {
                    "order_book_id": order_book_id,
                    "usable_from": active["usable_from"].isoformat(),
                    "source_id": str(active["source_id"]),
                    "change_date": active_change.isoformat() if active_change else None,
                    "share_total_reported": _json_scalar(active.get("share_total_reported")),
                    "share_trade_total_reported": _json_scalar(active.get("share_trade_total_reported")),
                    "share_rmb_reported": _json_scalar(active.get("share_rmb_reported")),
                }
                revision_id = f"jqcap-{canonical_sha(state_material)[:32]}"
                if not points or revision_id != points[-1]["local_state_revision_id"]:
                    points.append({**active, "local_state_revision_id": revision_id})
            position = end

        previous_revision: str | None = None
        for index, point in enumerate(points):
            valid_from = point["usable_from"]
            valid_to = (
                points[index + 1]["usable_from"]
                if index + 1 < len(points)
                else coverage_end_exclusive
            )
            if valid_to <= valid_from:
                raise BuildError(f"non-positive state interval for {order_book_id}")
            output.append(
                {
                    "order_book_id": order_book_id,
                    "valid_from": valid_from,
                    "valid_to_exclusive": valid_to,
                    "available_at": f"{valid_from.isoformat()}T09:30:00+08:00",
                    "source_change_date": point["change_date"],
                    "source_publication_date": point["pub_date"],
                    "change_reason_id": point.get("change_reason_id"),
                    "change_reason": point.get("change_reason"),
                    "share_total_reported": point.get("share_total_reported"),
                    "share_trade_total_reported": point.get("share_trade_total_reported"),
                    "share_rmb_reported": point.get("share_rmb_reported"),
                    "source_unit_semantics": "VENDOR_REPORTED_UNIT__SCALE_NOT_FORMALLY_ADMITTED",
                    "source_row_sha256": point["source_row_sha256"],
                    "local_state_revision_id": point["local_state_revision_id"],
                    "supersedes_local_state_revision_id": previous_revision,
                    "local_chain_semantics": "CONSERVATIVE_DATE_LEVEL_STATE_CHAIN__NOT_VENDOR_REVISION_CHAIN",
                }
            )
            previous_revision = point["local_state_revision_id"]
    frame = pd.DataFrame(output)
    if not frame.empty:
        duplicates = int(frame.duplicated(["order_book_id", "valid_from"]).sum())
        if duplicates:
            raise BuildError(f"duplicate PIT interval keys: {duplicates}")
    return frame, {
        "ignored_late_disclosures_of_older_effective_state": ignored_stale,
        "additional_source_events_same_usable_session": same_session_events,
    }


def _file_ref(path: Path, *, logical_root: Path | None = None, physical_root: Path | None = None) -> dict[str, Any]:
    logical = path
    if logical_root is not None and physical_root is not None:
        logical = logical_root / path.resolve().relative_to(physical_root.resolve())
    return {
        "path": logical.resolve().relative_to(ROOT.resolve()).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def inspect_input_bindings() -> dict[str, Any] | None:
    """Verify the fixed sealed local inputs without reading business payload rows."""

    source_exists = SOURCE.is_file()
    source_manifest_exists = SOURCE_MANIFEST.is_file()
    if not source_exists and not source_manifest_exists:
        return None
    if source_exists != source_manifest_exists:
        raise BuildError("partial JoinQuant local source: payload/manifest pair is incomplete")
    for path in (SOURCE, SOURCE_MANIFEST):
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise BuildError(f"JoinQuant local source reparse path is not allowed: {path}")
    snapshot_manifest_path = R0_SNAPSHOT / "manifest.json"
    if not snapshot_manifest_path.is_file():
        raise BuildError("formal R0 snapshot manifest is missing")
    source_manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    source_ref = source_manifest.get("artifacts", {}).get("STK_CAPITAL_CHANGE")
    if not isinstance(source_ref, dict) or source_ref.get("sha256") != sha256_file(SOURCE):
        raise BuildError("JQ capital-change payload hash mismatch")
    snapshot_manifest = json.loads(snapshot_manifest_path.read_text(encoding="utf-8"))
    if snapshot_manifest.get("content_hash") != EXPECTED_R0_HASH:
        raise BuildError("formal R0 snapshot identity mismatch")
    return {
        "joinquant_capital_change": _file_ref(SOURCE),
        "source_manifest": _file_ref(SOURCE_MANIFEST),
        "formal_calendar_snapshot": {
            "dataset_id": snapshot_manifest["dataset_id"],
            "snapshot_id": snapshot_manifest["snapshot_id"],
            "content_hash": snapshot_manifest["content_hash"],
            "manifest": _file_ref(snapshot_manifest_path),
        },
    }


def verify_committed_candidate(
    bindings: dict[str, Any],
    *,
    output_root: Path = FINAL,
    report_root: Path = REPORT,
) -> dict[str, Any]:
    """Verify an immutable committed candidate and its current source binding."""

    if not output_root.is_dir():
        raise BuildError("immutable candidate output is missing or not a directory")
    manifest_path = output_root / "manifest.json"
    committed_path = output_root / "COMMITTED.json"
    if not manifest_path.is_file() or not committed_path.is_file():
        raise BuildError("immutable candidate is not fully committed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != 1
        or manifest.get("candidate_id") != CANDIDATE_ID
        or manifest.get("status") != "COMPLETED__INSPECTION_ONLY__NOT_ADMITTED"
        or manifest.get("purpose_ceiling") != "INSPECTION_ONLY"
        or manifest.get("formal_admission") is not False
        or manifest.get("research_use") is not False
        or manifest.get("catalog_write") is not False
    ):
        raise BuildError("immutable candidate manifest violates the candidate-only contract")
    if manifest.get("source") != bindings:
        raise BuildError("sealed local source changed; a new immutable candidate version is required")
    artifacts = manifest.get("artifacts")
    expected_artifacts = {
        "normalized_source_observations",
        "pit_share_capital_intervals",
        "pending_after_r0_cutoff",
        "quality",
    }
    if not isinstance(artifacts, dict) or set(artifacts) != expected_artifacts:
        raise BuildError("immutable candidate artifact set mismatch")
    output_resolved = output_root.resolve()
    for name, reference in artifacts.items():
        if not isinstance(reference, dict):
            raise BuildError(f"invalid immutable candidate artifact reference: {name}")
        target = (ROOT / str(reference.get("path", ""))).resolve()
        if not target.is_relative_to(output_resolved):
            raise BuildError(f"immutable candidate artifact escapes output root: {name}")
        if not target.is_file() or target.stat().st_size != reference.get("bytes"):
            raise BuildError(f"immutable candidate artifact size mismatch: {name}")
        if sha256_file(target) != reference.get("sha256"):
            raise BuildError(f"immutable candidate artifact hash mismatch: {name}")
    manifest_sha = sha256_file(manifest_path)
    committed = json.loads(committed_path.read_text(encoding="utf-8"))
    if (
        committed.get("status") != "COMMITTED_INSPECTION_ONLY"
        or committed.get("candidate_id") != CANDIDATE_ID
        or committed.get("manifest_sha256") != manifest_sha
    ):
        raise BuildError("immutable candidate commit marker mismatch")
    report_verified = False
    if report_root.exists():
        result_path = report_root / "RESULT.json"
        if not report_root.is_dir() or not result_path.is_file():
            raise BuildError("existing report root is incomplete")
        report = json.loads(result_path.read_text(encoding="utf-8"))
        expected_candidate = output_root.resolve().relative_to(ROOT.resolve()).as_posix()
        if (
            report.get("candidate_root") != expected_candidate
            or report.get("manifest_sha256") != manifest_sha
            or report.get("formal_effect") != "NONE"
        ):
            raise BuildError("existing report does not bind the immutable candidate")
        report_verified = True
    return {
        "schema_version": 1,
        "status": "NO_CHANGE__SOURCE_UNCHANGED__COMMITTED_CANDIDATE_VERIFIED__NO_NETWORK",
        "candidate_root": output_root.resolve().relative_to(ROOT.resolve()).as_posix(),
        "manifest_sha256": manifest_sha,
        "report_verified": report_verified,
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_effect": "NONE",
        "catalog_write": False,
        "factor_runs": 0,
    }


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame, list[date], dict[str, Any]]:
    if inspect_input_bindings() is None:
        raise BuildError("required sealed JoinQuant local source is missing")
    source_manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    source_ref = source_manifest["artifacts"]["STK_CAPITAL_CHANGE"]
    if source_ref["sha256"] != sha256_file(SOURCE):
        raise BuildError("JQ capital-change payload hash mismatch")
    snapshot_manifest = json.loads((R0_SNAPSHOT / "manifest.json").read_text(encoding="utf-8"))
    if snapshot_manifest.get("content_hash") != EXPECTED_R0_HASH:
        raise BuildError("formal R0 snapshot identity mismatch")
    curated = ROOT / str(snapshot_manifest["curated_root"])
    payload_pattern = str(curated / "**/*.parquet")
    connection = duckdb.connect(database=":memory:")
    try:
        sessions = [
            row[0] for row in connection.execute(
                "SELECT DISTINCT trade_date FROM read_parquet(?, hive_partitioning=true) ORDER BY trade_date",
                [payload_pattern],
            ).fetchall()
        ]
        raw = connection.execute(
            """
            WITH universe AS (
              SELECT DISTINCT order_book_id
              FROM read_parquet(?, hive_partitioning=true)
            )
            SELECT
              c.id AS source_id,
              c.code AS order_book_id,
              try_cast(c.change_date AS DATE) AS change_date,
              try_cast(c.pub_date AS DATE) AS pub_date,
              c.change_reason_id,
              c.change_reason,
              try_cast(c.share_total AS DOUBLE) AS share_total_reported,
              try_cast(c.share_trade_total AS DOUBLE) AS share_trade_total_reported,
              try_cast(c.share_rmb AS DOUBLE) AS share_rmb_reported,
              c._source_row_sha256 AS source_row_sha256
            FROM read_parquet(?) c
            INNER JOIN universe u ON c.code = u.order_book_id
            ORDER BY c.code, try_cast(c.pub_date AS DATE), try_cast(c.change_date AS DATE), c.id
            """,
            [payload_pattern, str(SOURCE)],
        ).fetchdf()
    finally:
        connection.close()
    raw["change_date"] = pd.to_datetime(raw["change_date"], errors="coerce").dt.date
    raw["pub_date"] = pd.to_datetime(raw["pub_date"], errors="coerce").dt.date
    if not sessions or len(sessions) < 5000:
        raise BuildError("formal R0 calendar is unexpectedly small")
    if raw[["change_date", "pub_date", "share_total_reported"]].isna().any().any():
        raise BuildError("required JQ capital fields contain null or unparseable values")
    usable_rows: list[dict[str, Any]] = []
    pending_rows: list[dict[str, Any]] = []
    for row in raw.to_dict(orient="records"):
        usable = map_usable_session(row["pub_date"], row["change_date"], sessions)
        item = {**row, "usable_from": usable}
        (usable_rows if usable is not None else pending_rows).append(item)
    usable_frame = pd.DataFrame(usable_rows)
    pending_frame = pd.DataFrame(pending_rows)
    return usable_frame, pending_frame, sessions, snapshot_manifest


def build(*, output_root: Path = FINAL, report_root: Path = REPORT) -> dict[str, Any]:
    bindings = inspect_input_bindings()
    if bindings is None:
        return {
            "schema_version": 1,
            "status": "SKIPPED__LOCAL_JOINQUANT_SOURCE_NOT_AVAILABLE__NO_NETWORK_NO_WRITE",
            "candidate_id": CANDIDATE_ID,
            "formal_effect": "NONE",
            "catalog_write": False,
            "factor_runs": 0,
        }
    if output_root.exists():
        return verify_committed_candidate(bindings, output_root=output_root, report_root=report_root)
    if report_root.exists():
        raise BuildError("report exists without its immutable candidate output")
    stage = output_root.parent / f".{output_root.name}.staging-{uuid.uuid4().hex}"
    stage.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    try:
        observations, pending, sessions, snapshot_manifest = load_inputs()
        intervals, diagnostics = build_state_intervals(
            observations,
            coverage_end_exclusive=sessions[-1] + timedelta(days=1),
        )
        observation_path = stage / "normalized_source_observations.parquet"
        interval_path = stage / "pit_share_capital_intervals.parquet"
        pending_path = stage / "pending_after_r0_cutoff.parquet"
        atomic_parquet(observation_path, observations)
        atomic_parquet(interval_path, intervals)
        atomic_parquet(pending_path, pending)
        quality = {
            "status": "PASS__MECHANICAL_DATE_LEVEL_PIT_CANDIDATE__NOT_ADMITTED",
            "source_rows_in_formal_universe": int(len(observations) + len(pending)),
            "usable_source_rows": int(len(observations)),
            "pending_after_r0_cutoff_rows": int(len(pending)),
            "pit_intervals": int(len(intervals)),
            "unique_securities": int(intervals["order_book_id"].nunique()),
            "calendar_sessions": len(sessions),
            "calendar_start": sessions[0].isoformat(),
            "calendar_end": sessions[-1].isoformat(),
            "interval_start": intervals["valid_from"].min().isoformat(),
            "interval_end_exclusive": intervals["valid_to_exclusive"].max().isoformat(),
            "null_total_share_intervals": int(intervals["share_total_reported"].isna().sum()),
            "duplicate_interval_keys": int(intervals.duplicated(["order_book_id", "valid_from"]).sum()),
            **diagnostics,
        }
        atomic_json(stage / "quality.json", quality)
        finished = datetime.now(timezone.utc).isoformat()
        artifacts = {
            "normalized_source_observations": _file_ref(observation_path, logical_root=output_root, physical_root=stage),
            "pit_share_capital_intervals": _file_ref(interval_path, logical_root=output_root, physical_root=stage),
            "pending_after_r0_cutoff": _file_ref(pending_path, logical_root=output_root, physical_root=stage),
            "quality": _file_ref(stage / "quality.json", logical_root=output_root, physical_root=stage),
        }
        if inspect_input_bindings() != bindings:
            raise BuildError("sealed local source changed during candidate construction")
        manifest = {
            "schema_version": 1,
            "candidate_id": CANDIDATE_ID,
            "status": "COMPLETED__INSPECTION_ONLY__NOT_ADMITTED",
            "started_at_utc": started,
            "finished_at_utc": finished,
            "source": bindings,
            "derivation": {
                "available_at": "NEXT_FORMAL_R0_SESSION_09_30_AFTER_SOURCE_PUBLICATION_DATE",
                "effective_floor": "FIRST_FORMAL_R0_SESSION_ON_OR_AFTER_SOURCE_CHANGE_DATE",
                "state_selection": "MAX_EFFECTIVE_CHANGE_DATE_KNOWN_AS_OF_USABLE_SESSION",
                "late_older_disclosure": "PRESERVE_OBSERVATION__DO_NOT_OVERWRITE_CURRENT_STATE",
                "missing_policy": "NO_FILL",
                "unit_policy": "PRESERVE_VENDOR_REPORTED_VALUES__NO_SCALE_INFERENCE",
            },
            "quality": quality,
            "artifacts": artifacts,
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_admission": False,
            "research_use": False,
            "catalog_write": False,
            "factor_runs": 0,
            "credential_value_captured": False,
        }
        atomic_json(stage / "manifest.json", manifest)
        atomic_json(stage / "COMMITTED.json", {
            "status": "COMMITTED_INSPECTION_ONLY",
            "candidate_id": CANDIDATE_ID,
            "manifest_sha256": sha256_file(stage / "manifest.json"),
            "committed_at_utc": finished,
        })
        output_root.parent.mkdir(parents=True, exist_ok=True)
        if output_root.exists():
            raise BuildError("immutable output appeared during construction; refusing to overwrite")
        os.rename(stage, output_root)
        report_root.mkdir(parents=True, exist_ok=False)
        result = {
            "status": "PASS__SHARE_CAPITAL_PIT_CANDIDATE__FORMAL_EFFECT_NONE",
            "candidate_root": output_root.resolve().relative_to(ROOT.resolve()).as_posix(),
            "manifest_sha256": sha256_file(output_root / "manifest.json"),
            "quality": quality,
            "formal_effect": "NONE",
        }
        atomic_json(report_root / "RESULT.json", result)
        return result
    except Exception:
        if stage.exists():
            atomic_json(stage / "FAILURE.json", {"status": "FAILED_CLOSED", "failed_at_utc": datetime.now(timezone.utc).isoformat()})
        raise


def plan(*, output_root: Path = FINAL, report_root: Path = REPORT) -> dict[str, Any]:
    bindings = inspect_input_bindings()
    if bindings is None:
        return {
            "schema_version": 1,
            "status": "SKIPPED__LOCAL_JOINQUANT_SOURCE_NOT_AVAILABLE__NO_NETWORK_NO_WRITE",
            "candidate_id": CANDIDATE_ID,
            "formal_effect": "NONE",
            "catalog_write": False,
            "factor_runs": 0,
        }
    if output_root.exists():
        return verify_committed_candidate(bindings, output_root=output_root, report_root=report_root)
    if report_root.exists():
        raise BuildError("report exists without its immutable candidate output")
    return {
        "schema_version": 1,
        "status": "PLAN__LOCAL_SOURCE_VERIFIED__IMMUTABLE_CANDIDATE_BUILD_NOT_RUN",
        "candidate_id": CANDIDATE_ID,
        "source": bindings,
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_effect": "NONE",
        "catalog_write": False,
        "factor_runs": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="verify local source/candidate without writing")
    parser.add_argument("--run", action="store_true", help="build the immutable candidate")
    args = parser.parse_args()
    if args.plan and args.run:
        parser.error("--plan and --run are mutually exclusive")
    try:
        result = build() if args.run else plan()
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": {"type": type(exc).__name__, "message": str(exc)[:400]}}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
