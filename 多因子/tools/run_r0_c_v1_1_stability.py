#!/usr/bin/env python3
"""Discovery-only rolling stability review for action-safe R0 factors."""

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
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from factor_factory.accelerator import (  # noqa: E402
    accelerator_signature,
    cross_sectional_factor_metrics_gpu,
)
from factor_factory.evaluation import (  # noqa: E402
    evaluate_factor,
    forward_total_returns,
    minimum_trading_age_mask,
    winsorized_cross_sectional_rank,
)
from factor_factory.expressions import evaluate_expression  # noqa: E402
from factor_factory.generation import FactorGenerator  # noqa: E402
from factor_factory.r0_dense_panel import load_r0_shsz_panel_dense  # noqa: E402


CONFIG = PROJECT / "configs/r0_shsz_pv_c_v1_1_discovery_20260805.json"
CATALOG = PROJECT / "configs/r0_shsz_pv_factor_catalog_512_v1.json"
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,95}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("ab") as handle:
        handle.write((json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def build_splits(
    dates: tuple[str, ...],
    decision_indices: np.ndarray,
    *,
    train_years: int | None,
    first_test_year: int,
    last_test_year: int,
    purge_sessions: int,
    embargo_sessions: int,
) -> list[dict[str, Any]]:
    position = {int(index): offset for offset, index in enumerate(decision_indices)}
    splits: list[dict[str, Any]] = []
    for year in range(first_test_year, last_test_year + 1):
        year_indices = [int(index) for index in decision_indices if dates[int(index)].startswith(f"{year:04d}-")]
        if len(year_indices) <= embargo_sessions:
            continue
        test = np.asarray(year_indices[embargo_sessions:], dtype=np.int64)
        first_offset = position[int(test[0])]
        train_stop = first_offset - purge_sessions
        if train_stop <= 0:
            continue
        candidates = decision_indices[:train_stop]
        if train_years is not None:
            lower = f"{year - train_years:04d}-01-01"
            candidates = np.asarray(
                [int(index) for index in candidates if dates[int(index)] >= lower],
                dtype=np.int64,
            )
        if len(candidates) < 400 or len(test) < 150:
            continue
        splits.append({
            "test_year": year,
            "train": candidates,
            "test": test,
            "train_start": dates[int(candidates[0])],
            "train_end": dates[int(candidates[-1])],
            "test_start": dates[int(test[0])],
            "test_end": dates[int(test[-1])],
        })
    return splits


def summarize_folds(folds: list[dict[str, Any]], costs: list[float]) -> dict[str, Any]:
    if not folds:
        return {"folds": 0}
    ic = [float(row["test_mean_ic_oriented"]) for row in folds]
    result: dict[str, Any] = {
        "folds": len(folds),
        "test_years": [int(row["test_year"]) for row in folds],
        "mean_test_ic": float(np.mean(ic)),
        "median_test_ic": float(median(ic)),
        "worst_test_ic": float(min(ic)),
        "positive_test_ic_ratio": float(np.mean(np.asarray(ic) > 0.0)),
        "orientation_positive_ratio": float(np.mean([row["orientation"] > 0 for row in folds])),
        "minimum_evaluable_date_ratio": float(min(row["evaluable_date_ratio"] for row in folds)),
    }
    result["costs"] = {}
    for cost in costs:
        values = [float(row["cost_adjusted_top_excess"][str(int(cost))]) for row in folds]
        result["costs"][str(int(cost))] = {
            "mean": float(np.mean(values)),
            "median": float(median(values)),
            "worst": float(min(values)),
            "positive_fold_ratio": float(np.mean(np.asarray(values) > 0.0)),
        }
    return result


def load_candidates() -> dict[str, Any]:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    if catalog.get("catalog_content_hash") != "6216ca0af144998d97032de715467ba61cec46cdb877af9bbb419ed582e0cb64":
        raise RuntimeError("factor catalog hash mismatch")
    generator = FactorGenerator(
        ("open", "high", "low", "close", "volume", "amount"),
        seed=20260804,
        max_depth=4,
        required_output_unit=(0, 0),
    )
    candidates = generator.initial_population(512)
    result: dict[str, Any] = {}
    for candidate, frozen in zip(candidates, catalog["candidates"]):
        if candidate.candidate_id != frozen["candidate_id"] or candidate.formula != frozen["formula"]:
            raise RuntimeError("factor catalog regeneration mismatch")
        result[candidate.candidate_id] = candidate
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--factor-limit", type=int)
    args = parser.parse_args()
    if not RUN_ID.fullmatch(args.run_id) or not RUN_ID.fullmatch(args.source_run):
        raise SystemExit("invalid run id")

    source = PROJECT / "research_runs/factor_factory" / args.source_run
    final = PROJECT / "research_runs/factor_factory" / args.run_id
    stage = PROJECT / ".tmp/r0-c-v1-1-stability" / args.run_id
    if final.exists():
        raise SystemExit(f"immutable final run already exists: {final}")
    if not (source / "COMMITTED.json").is_file():
        raise SystemExit("source Discovery run is not committed")
    stage.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()

    source_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    source_commit = json.loads((source / "COMMITTED.json").read_text(encoding="utf-8"))
    if sha256_file(source / "manifest.json") != source_commit.get("manifest_sha256"):
        raise RuntimeError("source manifest hash mismatch")
    for name in ("factors.jsonl", "summary.json"):
        if sha256_file(source / name) != source_manifest.get("artifacts", {}).get(name):
            raise RuntimeError(f"source artifact hash mismatch: {name}")
    if source_manifest.get("validation_consumed") or source_manifest.get("holdout_opened"):
        raise RuntimeError("source run crossed Validation/Holdout boundary")
    source_rows = load_jsonl(source / "factors.jsonl")
    eligible = [
        row for row in source_rows
        if row["safety"]["survivor_eligible"]
        and float(row["horizons"]["10"]["q_value"]) <= 0.10
        and int(row["horizons"]["10"]["n_dates"]) > 0
    ]
    eligible.sort(key=lambda row: (
        float(row["horizons"]["10"]["q_value"]),
        -abs(float(row["horizons"]["10"]["mean_ic"])),
        row["candidate_id"],
    ))
    if args.factor_limit is not None:
        if args.factor_limit < 1:
            raise SystemExit("factor-limit must be positive")
        eligible = eligible[: args.factor_limit]
    if not eligible:
        raise RuntimeError("no action-safe FDR candidates available for stability review")
    candidates = load_candidates()
    if any(row["candidate_id"] not in candidates for row in eligible):
        raise RuntimeError("source candidate absent from frozen catalog")

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    window = config["discovery_window"]
    t0 = time.perf_counter()
    panel = load_r0_shsz_panel_dense(
        start_date=window["load_start"],
        end_date=window["hard_data_cutoff"],
        max_panel_cells=30_000_000,
    )
    age = minimum_trading_age_mask(panel, int(config["universe"]["minimum_observed_trading_sessions"]))
    target = forward_total_returns(panel, 10, decision_eligible=age)
    decisions = np.asarray([
        index for index, value in enumerate(panel.dates)
        if window["decision_start"] <= value <= window["decision_end"]
    ], dtype=np.int64)
    schemes: dict[str, list[dict[str, Any]]] = {}
    purge = int(config["sample_schemes"]["purge_sessions"])
    embargo = int(config["sample_schemes"]["embargo_sessions"])
    for years in config["sample_schemes"]["rolling_train_years"]:
        schemes[f"rolling_{years}y"] = build_splits(
            panel.dates, decisions, train_years=int(years),
            first_test_year=2009, last_test_year=2021,
            purge_sessions=purge, embargo_sessions=embargo,
        )
    schemes["expanding"] = build_splits(
        panel.dates, decisions, train_years=None,
        first_test_year=2009, last_test_year=2021,
        purge_sessions=purge, embargo_sessions=embargo,
    )

    records_path = stage / "stability.jsonl"
    records = load_jsonl(records_path)
    expected = [row["candidate_id"] for row in eligible]
    if [row["candidate_id"] for row in records] != expected[: len(records)]:
        raise RuntimeError("checkpoint prefix mismatch")
    completed = {row["candidate_id"] for row in records}
    costs = [float(value) for value in config["execution_and_cost"]["round_trip_cost_bps"]]
    atomic_json(stage / "RUNNING.json", {
        "status": "RUNNING__DISCOVERY_STABILITY_ONLY",
        "run_id": args.run_id,
        "source_run": args.source_run,
        "factor_count": len(eligible),
        "validation_consumed": False,
        "holdout_opened": False,
    })

    for sequence, source_row in enumerate(eligible):
        candidate_id = source_row["candidate_id"]
        if candidate_id in completed:
            continue
        item_started = time.perf_counter()
        candidate = candidates[candidate_id]
        values = evaluate_expression(
            candidate.expression, panel.fields, eligible=age,
            accelerator="gpu",
            gpu_column_chunk=int(config["accelerator"]["gpu_column_chunk"]),
        )
        values = np.where(age, values, np.nan)
        ranked = winsorized_cross_sectional_rank(
            values, age,
            minimum_assets=int(config["universe"]["minimum_assets_per_date"]),
        )
        daily = cross_sectional_factor_metrics_gpu(
            ranked, target, decisions,
            minimum_assets=int(config["universe"]["minimum_assets_per_date"]),
            quantile_fraction=float(config["statistics"]["quantile_fraction"]),
        )
        actual = decisions[np.asarray(daily["daily_positions"], dtype=np.int64)]
        daily_ic = {int(index): float(value) for index, value in zip(actual, daily["daily_ic"])}
        scheme_rows: dict[str, Any] = {}
        for scheme, splits in schemes.items():
            folds: list[dict[str, Any]] = []
            for split in splits:
                train_ic = [daily_ic[int(index)] for index in split["train"] if int(index) in daily_ic]
                if len(train_ic) < 300:
                    continue
                orientation = 1 if float(np.mean(train_ic)) >= 0.0 else -1
                metrics = evaluate_factor(
                    ranked if orientation > 0 else -ranked,
                    target,
                    split["test"],
                    minimum_assets=int(config["universe"]["minimum_assets_per_date"]),
                    quantile_fraction=float(config["statistics"]["quantile_fraction"]),
                    round_trip_cost_bps=0.0,
                    fold_count=1,
                    permutations=0,
                    accelerator="gpu",
                )
                folds.append({
                    "test_year": split["test_year"],
                    "train_start": split["train_start"],
                    "train_end": split["train_end"],
                    "test_start": split["test_start"],
                    "test_end": split["test_end"],
                    "orientation": orientation,
                    "train_mean_ic_raw": float(np.mean(train_ic)),
                    "test_mean_ic_oriented": metrics.mean_ic,
                    "test_top_excess": metrics.top_excess_mean,
                    "turnover": metrics.turnover,
                    "evaluable_date_ratio": metrics.evaluable_date_ratio,
                    "cost_adjusted_top_excess": {
                        str(int(cost)): metrics.top_excess_mean - cost / 10_000.0 * metrics.turnover
                        for cost in costs
                    },
                })
            scheme_rows[scheme] = {"folds": folds, "summary": summarize_folds(folds, costs)}
        summaries = {name: value["summary"] for name, value in scheme_rows.items()}
        record = {
            "candidate_id": candidate_id,
            "formula": source_row["formula"],
            "source_q_value": source_row["horizons"]["10"]["q_value"],
            "source_mean_ic_raw": source_row["horizons"]["10"]["mean_ic"],
            "schemes": scheme_rows,
            "all_scheme_positive_mean_ic": all(
                summary.get("mean_test_ic", -1.0) > 0.0 for summary in summaries.values()
            ),
            "all_scheme_positive_fold_ratio_ge_0_60": all(
                summary.get("positive_test_ic_ratio", 0.0) >= 0.60 for summary in summaries.values()
            ),
            "all_scheme_positive_30bps_mean": all(
                summary.get("costs", {}).get("30", {}).get("mean", -1.0) > 0.0
                for summary in summaries.values()
            ),
            "seconds": time.perf_counter() - item_started,
            "validation_consumed": False,
            "holdout_opened": False,
        }
        append_jsonl(records_path, record)
        records.append(record)
        del values, ranked
        gc.collect()
        done = len(records)
        elapsed = time.perf_counter() - t0
        eta = elapsed / max(1, done) * (len(eligible) - done)
        atomic_json(stage / "progress.json", {
            "status": "RUNNING__DISCOVERY_STABILITY_ONLY",
            "completed": done,
            "total": len(eligible),
            "percent": round(100.0 * done / max(1, len(eligible)), 2),
            "eta_seconds": max(0.0, eta),
            "last_candidate_id": candidate_id,
        })
        print(json.dumps({"progress": f"{done}/{len(eligible)}", "eta_seconds": round(max(0.0, eta), 1)}), flush=True)

    stable_ic = [row for row in records if row["all_scheme_positive_mean_ic"] and row["all_scheme_positive_fold_ratio_ge_0_60"]]
    stable_cost = [row for row in stable_ic if row["all_scheme_positive_30bps_mean"]]
    ranked_records = sorted(records, key=lambda row: (
        -sum(value["summary"].get("mean_test_ic", -1.0) for value in row["schemes"].values()),
        row["candidate_id"],
    ))
    summary = {
        "status": "COMPLETED__DISCOVERY_STABILITY_ONLY__NO_VALIDATION_NO_HOLDOUT",
        "run_id": args.run_id,
        "source_run": args.source_run,
        "factor_count": len(records),
        "stable_ic_count": len(stable_ic),
        "stable_ic_and_30bps_count": len(stable_cost),
        "top_descriptive": [
            {
                "candidate_id": row["candidate_id"],
                "formula": row["formula"],
                "all_scheme_positive_mean_ic": row["all_scheme_positive_mean_ic"],
                "all_scheme_positive_fold_ratio_ge_0_60": row["all_scheme_positive_fold_ratio_ge_0_60"],
                "all_scheme_positive_30bps_mean": row["all_scheme_positive_30bps_mean"],
                "scheme_summaries": {name: value["summary"] for name, value in row["schemes"].items()},
            }
            for row in ranked_records[:30]
        ],
        "panel": {
            "shape": list(panel.shape),
            "date_start": panel.dates[0],
            "date_end": panel.dates[-1],
            "decision_end": window["decision_end"],
            "hard_data_cutoff": window["hard_data_cutoff"],
            "content_hash": panel.metadata["content_hash"],
        },
        "split_policy": {
            "rolling_train_years": config["sample_schemes"]["rolling_train_years"],
            "expanding": True,
            "test_years": [2009, 2021],
            "purge_sessions": purge,
            "embargo_sessions_removed_from_each_test_start": embargo,
            "orientation_frozen_from_pre_test_training_only": True,
        },
        "timing_seconds": time.perf_counter() - t0,
        "interpretation_ceiling": "DISCOVERY_STABILITY_DESCRIPTION_ONLY__NOT_INDEPENDENT_VALIDATION_OR_TRADABLE_CONCLUSION",
        "validation_consumed": False,
        "holdout_opened": False,
    }
    atomic_json(stage / "summary.json", summary)
    finished = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": 1,
        "run_id": args.run_id,
        "status": summary["status"],
        "started_at_utc": started,
        "finished_at_utc": finished,
        "source_run": args.source_run,
        "source_manifest_sha256": sha256_file(source / "manifest.json"),
        "source_factors_sha256": sha256_file(source / "factors.jsonl"),
        "config_path": CONFIG.relative_to(ROOT).as_posix(),
        "config_sha256": sha256_file(CONFIG),
        "catalog_path": CATALOG.relative_to(ROOT).as_posix(),
        "catalog_sha256": sha256_file(CATALOG),
        "runner_sha256": sha256_file(Path(__file__)),
        "code_sha256": {
            "evaluation": sha256_file(PROJECT / "factor_factory/evaluation.py"),
            "expressions": sha256_file(PROJECT / "factor_factory/expressions.py"),
            "accelerator": sha256_file(PROJECT / "factor_factory/accelerator.py"),
            "dense_loader": sha256_file(PROJECT / "factor_factory/r0_dense_panel.py"),
        },
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
            "accelerator": accelerator_signature("gpu"),
        },
        "artifacts": {
            "stability.jsonl": sha256_file(records_path),
            "summary.json": sha256_file(stage / "summary.json"),
        },
        "validation_consumed": False,
        "holdout_opened": False,
        "automatic_trading": False,
    }
    atomic_json(stage / "manifest.json", manifest)
    atomic_json(stage / "COMMITTED.json", {
        "status": "COMMITTED",
        "run_id": args.run_id,
        "manifest_sha256": sha256_file(stage / "manifest.json"),
        "committed_at_utc": finished,
    })
    (stage / "RUNNING.json").unlink(missing_ok=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage, final)
    print(json.dumps({
        "status": "COMMITTED",
        "run_id": args.run_id,
        "factor_count": len(records),
        "stable_ic_count": len(stable_ic),
        "stable_ic_and_30bps_count": len(stable_cost),
        "total_seconds": summary["timing_seconds"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
