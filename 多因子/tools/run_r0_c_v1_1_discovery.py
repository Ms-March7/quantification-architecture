#!/usr/bin/env python3
"""Run immutable, Discovery-only R0 C-v1.1 single-factor statistics."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import re
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from factor_factory.accelerator import accelerator_signature  # noqa: E402
from factor_factory.evaluation import (  # noqa: E402
    FactorMetrics,
    benjamini_yekutieli,
    evaluate_factor,
    forward_total_returns,
    minimum_trading_age_mask,
    winsorized_cross_sectional_rank,
)
from factor_factory.expressions import evaluate_expression  # noqa: E402
from factor_factory.factor_safety import assess_factor_safety  # noqa: E402
from factor_factory.generation import FactorGenerator  # noqa: E402
from factor_factory.r0_dense_panel import load_r0_shsz_panel_dense  # noqa: E402


CONFIG = PROJECT / "configs/r0_shsz_pv_c_v1_1_discovery_20260805.json"
CATALOG = PROJECT / "configs/r0_shsz_pv_factor_catalog_512_v1.json"
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,95}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"invalid checkpoint JSONL line {line_number}") from error
            if not isinstance(value, dict):
                raise RuntimeError("checkpoint JSONL rows must be objects")
            records.append(value)
    return records


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with path.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _rewrite_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        for value in records:
            handle.write((json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_candidates(count: int) -> list[Any]:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    if catalog.get("catalog_content_hash") != "6216ca0af144998d97032de715467ba61cec46cdb877af9bbb419ed582e0cb64":
        raise RuntimeError("frozen factor catalog content hash mismatch")
    generator = FactorGenerator(
        ("open", "high", "low", "close", "volume", "amount"),
        seed=20260804,
        max_depth=4,
        required_output_unit=(0, 0),
    )
    candidates = generator.initial_population(512)
    frozen = catalog["candidates"]
    for index, candidate in enumerate(candidates):
        row = frozen[index]
        if row["candidate_id"] != candidate.candidate_id or row["formula"] != candidate.formula:
            raise RuntimeError(f"factor catalog regeneration mismatch at index {index}")
    return candidates[:count]


def _apply_by(records: list[dict[str, Any]], horizons: tuple[int, ...]) -> None:
    for horizon in horizons:
        metrics = [FactorMetrics(**row["horizons"][str(horizon)]) for row in records]
        adjusted = benjamini_yekutieli(metrics)
        for row, value in zip(records, adjusted):
            row["horizons"][str(horizon)] = value.to_dict()


def _oriented_cost(record: dict[str, Any], horizon: int, cost_bps: float) -> float:
    metrics = record["horizons"][str(horizon)]
    if float(metrics["mean_ic"]) >= 0.0:
        oriented = float(metrics["top_excess_mean"])
    else:
        oriented = float(metrics["top_excess_mean"]) - float(metrics["long_short_spread_mean"])
    return oriented - cost_bps / 10_000.0 * float(metrics["turnover"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=("calibration", "discovery"), required=True)
    parser.add_argument("--factor-count", type=int)
    parser.add_argument("--permutations", type=int, default=999)
    args = parser.parse_args()
    if not RUN_ID.fullmatch(args.run_id):
        raise SystemExit("invalid run id")
    expected = 96 if args.mode == "calibration" else 512
    factor_count = expected if args.factor_count is None else int(args.factor_count)
    if factor_count < 1 or factor_count > expected:
        raise SystemExit(f"factor-count must be within 1..{expected} for {args.mode}")
    if args.permutations < 0:
        raise SystemExit("permutations must be non-negative")

    final = PROJECT / "research_runs/factor_factory" / args.run_id
    stage = PROJECT / ".tmp/r0-c-v1-1-discovery" / args.run_id
    if final.exists():
        raise SystemExit(f"immutable final run already exists: {final}")
    stage.mkdir(parents=True, exist_ok=True)
    records_path = stage / "factors.jsonl"
    existing = _jsonl(records_path)
    if len({row.get("candidate_id") for row in existing}) != len(existing):
        raise RuntimeError("checkpoint contains duplicate candidate ids")

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    candidates = _load_candidates(factor_count)
    expected_ids = [candidate.candidate_id for candidate in candidates]
    if [row.get("candidate_id") for row in existing] != expected_ids[: len(existing)]:
        raise RuntimeError("checkpoint prefix does not match frozen candidate order")

    started_at = datetime.now(timezone.utc).isoformat()
    runtime = {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "accelerator": accelerator_signature("gpu"),
    }
    _atomic_json(stage / "RUNNING.json", {
        "status": "RUNNING__DISCOVERY_ONLY",
        "run_id": args.run_id,
        "mode": args.mode,
        "started_at_utc": started_at,
        "factor_count": factor_count,
        "completed_on_resume": len(existing),
        "validation_consumed": False,
        "holdout_opened": False,
    })

    t0 = time.perf_counter()
    window = config["discovery_window"]
    panel = load_r0_shsz_panel_dense(
        start_date=window["load_start"],
        end_date=window["decision_end"],
        max_panel_cells=30_000_000,
    )
    age = minimum_trading_age_mask(
        panel, int(config["universe"]["minimum_observed_trading_sessions"])
    )
    horizons = (
        int(config["labels"]["primary_horizon_sessions"]),
        *(int(value) for value in config["labels"]["diagnostic_horizons_sessions"]),
    )
    horizons = tuple(dict.fromkeys(horizons))
    targets = {
        horizon: forward_total_returns(panel, horizon, decision_eligible=age)
        for horizon in horizons
    }
    indices = np.asarray([
        index
        for index, value in enumerate(panel.dates)
        if window["decision_start"] <= value <= window["decision_end"]
    ], dtype=np.int64)
    load_seconds = time.perf_counter() - t0

    records = list(existing)
    completed = {row["candidate_id"] for row in records}
    factor_started = time.perf_counter()
    for index, candidate in enumerate(candidates):
        if candidate.candidate_id in completed:
            continue
        item_started = time.perf_counter()
        safety = assess_factor_safety(candidate.expression)
        values = evaluate_expression(
            candidate.expression,
            panel.fields,
            eligible=age,
            accelerator="gpu",
            gpu_column_chunk=int(config["accelerator"]["gpu_column_chunk"]),
        )
        values = np.where(age, values, np.nan)
        ranked = winsorized_cross_sectional_rank(
            values,
            age,
            minimum_assets=int(config["universe"]["minimum_assets_per_date"]),
        )
        metrics: dict[str, dict[str, int | float]] = {}
        for horizon in horizons:
            value = evaluate_factor(
                ranked,
                targets[horizon],
                indices,
                minimum_assets=int(config["universe"]["minimum_assets_per_date"]),
                quantile_fraction=float(config["statistics"]["quantile_fraction"]),
                round_trip_cost_bps=15.0,
                fold_count=8,
                permutations=int(args.permutations),
                permutation_block_length=int(
                    config["statistics"]["permutation_minimum_block_sessions"]
                ),
                seed=20260805 + index * 101 + horizon,
                accelerator="gpu",
            )
            metrics[str(horizon)] = value.to_dict()
        record = {
            "candidate_index": index,
            "candidate_id": candidate.candidate_id,
            "formula": candidate.formula,
            "origin": candidate.origin,
            "generation": candidate.generation,
            "safety": safety.to_dict(),
            "horizons": metrics,
            "seconds": time.perf_counter() - item_started,
            "validation_consumed": False,
            "holdout_opened": False,
        }
        _append_jsonl(records_path, record)
        records.append(record)
        del values, ranked
        gc.collect()
        completed_count = len(records)
        elapsed = time.perf_counter() - factor_started
        eta = elapsed / max(1, completed_count - len(existing)) * (factor_count - completed_count)
        _atomic_json(stage / "progress.json", {
            "status": "RUNNING__DISCOVERY_ONLY",
            "completed": completed_count,
            "total": factor_count,
            "percent": round(100.0 * completed_count / factor_count, 2),
            "elapsed_seconds": elapsed,
            "eta_seconds": max(0.0, eta),
            "last_candidate_id": candidate.candidate_id,
        })
        if completed_count == 1 or completed_count % 8 == 0 or completed_count == factor_count:
            print(json.dumps({"progress": f"{completed_count}/{factor_count}", "eta_seconds": round(max(0.0, eta), 1), "last": candidate.candidate_id}), flush=True)

    _apply_by(records, horizons)
    primary = int(config["labels"]["primary_horizon_sessions"])
    for row in records:
        row["primary_orientation"] = 1 if row["horizons"][str(primary)]["mean_ic"] >= 0 else -1
        row["oriented_cost_adjusted_top_excess_bps"] = {
            str(cost): _oriented_cost(row, primary, float(cost))
            for cost in config["execution_and_cost"]["round_trip_cost_bps"]
        }
    _rewrite_jsonl(records_path, records)
    ranked_records = sorted(
        records,
        key=lambda row: (
            float(row["horizons"][str(primary)]["q_value"]),
            -abs(float(row["horizons"][str(primary)]["mean_ic"])),
        ),
    )
    action_safe = [row for row in ranked_records if row["safety"]["survivor_eligible"]]
    summary = {
        "status": "COMPLETED__DISCOVERY_STATISTICS_ONLY__NO_VALIDATION_NO_HOLDOUT",
        "run_id": args.run_id,
        "mode": args.mode,
        "factor_count": factor_count,
        "factor_safety_counts": {
            tier: sum(row["safety"]["tier"] == tier for row in records)
            for tier in sorted({row["safety"]["tier"] for row in records})
        },
        "primary_horizon_sessions": primary,
        "by_fdr_0_10_count": sum(
            float(row["horizons"][str(primary)]["q_value"]) <= 0.10 for row in records
        ),
        "action_safe_by_fdr_0_10_count": sum(
            float(row["horizons"][str(primary)]["q_value"]) <= 0.10 for row in action_safe
        ),
        "top_overall": [
            {
                "candidate_id": row["candidate_id"],
                "formula": row["formula"],
                "safety_tier": row["safety"]["tier"],
                "metrics": row["horizons"][str(primary)],
                "primary_orientation": row["primary_orientation"],
                "oriented_cost_adjusted_top_excess_bps": row["oriented_cost_adjusted_top_excess_bps"],
            }
            for row in ranked_records[:30]
        ],
        "top_action_safe": [
            {
                "candidate_id": row["candidate_id"],
                "formula": row["formula"],
                "metrics": row["horizons"][str(primary)],
                "primary_orientation": row["primary_orientation"],
                "oriented_cost_adjusted_top_excess_bps": row["oriented_cost_adjusted_top_excess_bps"],
            }
            for row in action_safe[:30]
        ],
        "panel": {
            "shape": list(panel.shape),
            "date_start": panel.dates[0],
            "date_end": panel.dates[-1],
            "loader": panel.metadata["loader"],
            "content_hash": panel.metadata["content_hash"],
        },
        "timing": {
            "load_and_labels_seconds": load_seconds,
            "factor_loop_seconds": time.perf_counter() - factor_started,
            "total_seconds": time.perf_counter() - t0,
        },
        "validation_consumed": False,
        "holdout_opened": False,
        "interpretation_ceiling": "DISCOVERY_CANDIDATE_RANKING_ONLY__NOT_TRADABLE_CONCLUSION",
    }
    _atomic_json(stage / "summary.json", summary)
    finished_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": 1,
        "run_id": args.run_id,
        "status": summary["status"],
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "mode": args.mode,
        "dataset": config["dataset"],
        "config_path": str(CONFIG.relative_to(ROOT)).replace("\\", "/"),
        "config_sha256": _sha256(CONFIG),
        "catalog_path": str(CATALOG.relative_to(ROOT)).replace("\\", "/"),
        "catalog_sha256": _sha256(CATALOG),
        "factor_count": factor_count,
        "horizons": list(horizons),
        "permutations": int(args.permutations),
        "runtime": runtime,
        "code_sha256": {
            "runner": _sha256(Path(__file__)),
            "evaluation": _sha256(PROJECT / "factor_factory/evaluation.py"),
            "expressions": _sha256(PROJECT / "factor_factory/expressions.py"),
            "accelerator": _sha256(PROJECT / "factor_factory/accelerator.py"),
            "dense_loader": _sha256(PROJECT / "factor_factory/r0_dense_panel.py"),
            "factor_safety": _sha256(PROJECT / "factor_factory/factor_safety.py"),
        },
        "artifacts": {
            "factors.jsonl": _sha256(records_path),
            "summary.json": _sha256(stage / "summary.json"),
        },
        "validation_consumed": False,
        "holdout_opened": False,
        "automatic_trading": False,
    }
    _atomic_json(stage / "manifest.json", manifest)
    _atomic_json(stage / "COMMITTED.json", {
        "status": "COMMITTED",
        "run_id": args.run_id,
        "manifest_sha256": _sha256(stage / "manifest.json"),
        "committed_at_utc": finished_at,
    })
    (stage / "RUNNING.json").unlink(missing_ok=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage, final)
    print(json.dumps({"status": "COMMITTED", "run_id": args.run_id, "path": str(final), "factor_count": factor_count, "total_seconds": summary["timing"]["total_seconds"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
