#!/usr/bin/env python3
"""Run the external/classic R0 factor catalog in Discovery only.

The runner is deliberately unable to open Validation or Holdout.  It reads the
exact admitted R0 narrow snapshot only through the formal dense adapter, keeps
factor failures isolated, and commits an immutable run after family-wide BY
adjustment and redundancy clustering.
"""

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
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from factor_factory.accelerator import (  # noqa: E402
    AcceleratorError,
    accelerator_signature,
    cross_sectional_factor_metrics_gpu,
)
from factor_factory.evaluation import (  # noqa: E402
    FactorMetrics,
    benjamini_yekutieli,
    evaluate_factor,
    forward_total_returns,
    minimum_trading_age_mask,
    winsorized_cross_sectional_rank,
)
from factor_factory.expressions import _rolling  # noqa: E402
from factor_factory.r0_dense_panel import load_r0_shsz_panel_dense  # noqa: E402
from factor_factory.total_return_projection import build_total_return_signal_fields  # noqa: E402
from factor_factory.r0_classic_engine import R0ClassicFactorError  # noqa: E402


CONFIG = PROJECT / "configs/r0_shsz_pv_classic_discovery_20260806.json"
CATALOG = PROJECT / "configs/r0_shsz_pv_classic_factor_catalog_v1.json"
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,95}$")
EXPECTED_DATASET = "cn-a-share-r0-shsz-pv-panel"
EXPECTED_SNAPSHOT = "20050104-20260803-v1"
EXPECTED_CONTENT_HASH = "26309d36b7c08096fff55df100f1d27df8412b106ea48f282ce0f7caad37084b"
EXPECTED_CONFIG_SHA256 = "9f1e789b9e13a17d2998ff385f86b6ec9a2535468e5a6f9eac67f284cbc1e9ba"
REQUIRED_RECORD_KEYS = {
    "factor_id",
    "family",
    "kind",
    "params",
    "formula",
    "inputs",
    "source_refs",
    "source_formula",
    "source_mapping",
    "company_action_sensitivity",
    "causal_timing",
    "definition_sha256",
    "legacy_512_overlap",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (text + "\n").encode("utf-8")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, _json_bytes(value))


def atomic_npy(path: Path, values: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        np.save(handle, np.asarray(values, dtype=np.float32), allow_pickle=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    if not cleaned:
        raise ValueError("factor id has no safe path representation")
    return cleaned[:120]


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return value


def validate_contract(config: Mapping[str, Any], catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    dataset = config.get("dataset", {})
    exact = (
        dataset.get("dataset_id"),
        dataset.get("snapshot_id"),
        dataset.get("content_hash"),
    )
    if exact != (EXPECTED_DATASET, EXPECTED_SNAPSHOT, EXPECTED_CONTENT_HASH):
        raise RuntimeError("execution config does not pin the exact admitted R0 snapshot")
    window = config.get("discovery_window", {})
    holdout = window.get("protocol_holdout")
    if (
        window.get("decision_start") != "2005-07-01"
        or window.get("decision_end") != "2021-12-31"
        or window.get("hard_data_cutoff") != "2022-01-31"
        or holdout != ["2022-02-01", "2026-08-03"]
        or window.get("validation_consumed") is not False
        or window.get("holdout_opened") is not False
    ):
        raise RuntimeError("Discovery/Holdout boundary differs from the frozen contract")
    hard = config.get("hard_boundaries", {})
    required_false = (
        "validation_consumed",
        "holdout_opened",
        "automatic_trading",
        "legacy_512_run",
        "data_published_write",
    )
    if any(hard.get(name) is not False for name in required_false):
        raise RuntimeError("execution config crosses a hard research boundary")
    horizons = config.get("labels", {}).get("horizons_sessions")
    if horizons != [5, 10, 20, 60]:
        raise RuntimeError("required 5/10/20/60-session labels are not frozen")
    if config.get("universe", {}).get("minimum_observed_trading_sessions") != 120:
        raise RuntimeError("minimum observed trading age must be 120 sessions")

    records = catalog.get("records")
    if not isinstance(records, list):
        raise RuntimeError("classic factor catalog has no records list")
    count = len(records)
    gate = config.get("factor_catalog", {})
    if not int(gate.get("minimum_records", 0)) <= count <= int(gate.get("maximum_records", -1)):
        raise RuntimeError(f"classic factor catalog count outside frozen gate: {count}")
    if catalog.get("record_count") != count:
        raise RuntimeError("classic factor catalog record_count mismatch")
    if gate.get("record_count") != count:
        raise RuntimeError("execution config does not pin the exact catalog record count")
    if gate.get("content_sha256") != catalog.get("catalog_content_sha256"):
        raise RuntimeError("execution config does not pin the exact catalog content hash")
    if catalog.get("catalog_id") != gate.get("catalog_id"):
        raise RuntimeError("classic factor catalog id mismatch")
    if catalog.get("status") != gate.get("required_status"):
        raise RuntimeError("classic factor catalog status mismatch")
    if catalog.get("factor_runs") != 0:
        raise RuntimeError("factor definition catalog unexpectedly records factor runs")
    if catalog.get("validation_consumed") is not False or catalog.get("holdout_opened") is not False:
        raise RuntimeError("factor definition catalog crossed Validation/Holdout")
    identifiers: list[str] = []
    hashes: list[str] = []
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(records):
        if not isinstance(raw, dict) or set(raw) != REQUIRED_RECORD_KEYS:
            raise RuntimeError(f"invalid classic factor record schema at index {index}")
        factor_id = str(raw["factor_id"])
        definition_hash = str(raw["definition_sha256"])
        if not re.fullmatch(r"[0-9a-f]{64}", definition_hash):
            raise RuntimeError(f"invalid definition hash for {factor_id}")
        overlap = raw.get("legacy_512_overlap")
        if overlap != {"exact": False, "semantic": False}:
            raise RuntimeError(f"legacy 512 factor is forbidden in this run: {factor_id}")
        identifiers.append(factor_id)
        hashes.append(definition_hash)
        definition_payload = {
            "kind": raw["kind"],
            "params": raw["params"],
            "formula": " ".join(str(raw["formula"]).split()),
            "inputs": sorted(set(str(value) for value in raw["inputs"])),
            "company_action_sensitivity": raw["company_action_sensitivity"],
            "causal_timing": raw["causal_timing"],
        }
        calculated_definition = hashlib.sha256(
            json.dumps(
                definition_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if calculated_definition != definition_hash:
            raise RuntimeError(f"definition hash mismatch for {factor_id}")
        normalized.append(dict(raw))
    if len(set(identifiers)) != count:
        raise RuntimeError("duplicate factor_id in classic catalog")
    if len(set(hashes)) != count:
        raise RuntimeError("duplicate factor definition in classic catalog")
    calculated_catalog = hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if calculated_catalog != catalog.get("catalog_content_sha256"):
        raise RuntimeError("classic factor catalog content hash mismatch")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--factor-limit", type=int)
    parser.add_argument("--permutations", type=int)
    parser.add_argument("--accelerator", choices=("auto", "cpu", "gpu"), default="auto")
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="allow a partial engineering run; no FDR or cost-survivor candidate is emitted",
    )
    parser.add_argument(
        "--contract-only",
        action="store_true",
        help="validate frozen config/catalog and print the intended run without reading data",
    )
    return parser


class CheckpointStore:
    """Atomic, exact-prefix factor checkpoints with immutable resume identity."""

    def __init__(self, stage: Path, factor_ids: Sequence[str], fingerprint: Mapping[str, Any]):
        self.stage = stage
        self.factor_ids = tuple(factor_ids)
        self.fingerprint = dict(fingerprint)
        self.checkpoints = stage / "checkpoints"
        self.sketches = stage / "correlation_sketches"

    def initialize(self) -> dict[str, Any]:
        self.stage.mkdir(parents=True, exist_ok=True)
        running = self.stage / "RUNNING.json"
        if running.exists():
            existing = _load_json(running)
            if existing.get("resume_fingerprint") != self.fingerprint:
                raise RuntimeError("checkpoint resume fingerprint mismatch")
            return existing
        if (self.stage / "COMMITTED.json").exists():
            raise RuntimeError("committed stage must be verified and published, not reinitialized")
        if any(self.stage.iterdir()):
            raise RuntimeError("non-empty checkpoint stage has no valid RUNNING or COMMITTED marker")
        value = {
            "status": "RUNNING__DISCOVERY_ONLY",
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "resume_fingerprint": self.fingerprint,
            "factor_ids": list(self.factor_ids),
            "validation_consumed": False,
            "holdout_opened": False,
        }
        atomic_json(running, value)
        return value

    def load_prefix(self) -> list[dict[str, Any]]:
        if not self.checkpoints.exists():
            return []
        paths = sorted(self.checkpoints.glob("*.json"))
        records: list[dict[str, Any]] = []
        for expected, path in enumerate(paths):
            row = _load_json(path)
            if row.get("factor_index") != expected:
                raise RuntimeError("checkpoint indexes are not a contiguous prefix")
            if expected >= len(self.factor_ids) or row.get("factor_id") != self.factor_ids[expected]:
                raise RuntimeError("checkpoint factor order differs from frozen catalog")
            records.append(row)
        return records

    def commit_factor(self, index: int, record: Mapping[str, Any], sketch: np.ndarray | None) -> None:
        if index < 0 or index >= len(self.factor_ids):
            raise ValueError("checkpoint factor index outside catalog")
        if record.get("factor_index") != index or record.get("factor_id") != self.factor_ids[index]:
            raise ValueError("checkpoint record identity mismatch")
        prefix = f"{index:04d}-{_safe_component(self.factor_ids[index])}"
        if sketch is not None:
            sketch_path = self.sketches / f"{prefix}.npy"
            atomic_npy(sketch_path, sketch)
            enriched = dict(record)
            enriched["correlation_sketch"] = {
                "path": str(sketch_path.relative_to(self.stage)).replace("\\", "/"),
                "sha256": sha256_file(sketch_path),
                "finite": int(np.isfinite(sketch).sum()),
                "size": int(sketch.size),
            }
        else:
            enriched = dict(record)
            enriched["correlation_sketch"] = None
        atomic_json(self.checkpoints / f"{prefix}.json", enriched)


class RunLock:
    """One-process lock for a run id; the OS releases it after a hard crash."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Any | None = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - Windows is the production runtime
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            self.handle = None
            raise RuntimeError(f"run id is already active: {self.path.stem}") from error
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def resolve_accelerator(requested: str) -> tuple[str, dict[str, Any]]:
    if requested == "cpu":
        return "cpu", {"selection": "EXPLICIT_CPU", **accelerator_signature("cpu")}
    if requested == "gpu":
        return "gpu", {"selection": "EXPLICIT_GPU_FAIL_CLOSED", **accelerator_signature("gpu")}
    try:
        return "gpu", {"selection": "AUTO_GPU", **accelerator_signature("gpu")}
    except Exception as error:  # fallback is legal only before the first factor
        return "cpu", {
            "selection": "AUTO_CPU_FALLBACK_BEFORE_ANY_FACTOR",
            "fallback_error_type": type(error).__name__,
            "fallback_error": str(error)[:500],
            **accelerator_signature("cpu"),
        }


def effective_decision_indices(
    dates: Sequence[str], *, start: str, end: str, horizon: int
) -> np.ndarray:
    """Return decision dates whose t+1..t+h+1 label stays inside loaded data."""

    maximum = len(dates) - int(horizon) - 2
    return np.asarray(
        [index for index, value in enumerate(dates) if start <= value <= end and index <= maximum],
        dtype=np.int64,
    )


def build_walk_forward_splits(
    dates: Sequence[str],
    decision_indices: np.ndarray,
    *,
    train_years: int | None,
    first_test_year: int,
    last_test_year: int,
    purge_sessions: int,
    gap_sessions: int,
    minimum_train_sessions: int,
    minimum_test_sessions: int,
) -> list[dict[str, Any]]:
    decisions = np.asarray(decision_indices, dtype=np.int64)
    offset = {int(value): index for index, value in enumerate(decisions)}
    folds: list[dict[str, Any]] = []
    for year in range(first_test_year, last_test_year + 1):
        year_values = [int(value) for value in decisions if str(dates[int(value)]).startswith(f"{year:04d}-")]
        if len(year_values) <= gap_sessions:
            continue
        test = np.asarray(year_values[gap_sessions:], dtype=np.int64)
        year_start_offset = offset[int(year_values[0])]
        first_test_offset = offset[int(test[0])]
        # Purge removes the tail of the pre-test training sample; gap removes
        # the head of the test year.  They are independent embargoes.
        train_stop = year_start_offset - purge_sessions
        if train_stop <= 0:
            continue
        train = decisions[:train_stop]
        if train_years is not None:
            lower = f"{year - train_years:04d}-01-01"
            train = np.asarray([int(value) for value in train if dates[int(value)] >= lower], dtype=np.int64)
        if len(train) < minimum_train_sessions or len(test) < minimum_test_sessions:
            continue
        if (
            offset[int(test[0])] - offset[int(train[-1])]
            <= purge_sessions + gap_sessions
        ):
            raise RuntimeError("walk-forward purge invariant failed")
        folds.append(
            {
                "test_year": year,
                "train": train,
                "test": test,
                "train_start": dates[int(train[0])],
                "train_end": dates[int(train[-1])],
                "test_start": dates[int(test[0])],
                "test_end": dates[int(test[-1])],
                "purge_sessions": purge_sessions,
                "gap_sessions": gap_sessions,
            }
        )
    return folds


def _rank_1d(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    starts = np.flatnonzero(np.r_[True, sorted_values[1:] != sorted_values[:-1]])
    ends = np.r_[starts[1:], len(values)]
    ranks = np.empty(len(values), dtype=np.float64)
    ranks[order] = np.repeat((starts + ends - 1) / 2.0, ends - starts)
    return ranks


def _tail_weights_from_rank(values: np.ndarray, size: int, *, top: bool) -> np.ndarray:
    weights = np.zeros(len(values), dtype=np.float64)
    levels = np.unique(values)
    if top:
        levels = levels[::-1]
    remaining = float(size)
    for level in levels:
        members = np.flatnonzero(values == level)
        allocation = min(1.0, remaining / max(1, len(members)))
        weights[members] = allocation
        remaining -= allocation * len(members)
        if remaining <= 1e-12:
            break
    weights /= max(weights.sum(), 1e-12)
    return weights


def _daily_factor_series_cpu(
    ranked: np.ndarray,
    target: np.ndarray,
    indices: np.ndarray,
    *,
    minimum_assets: int,
    quantile_fraction: float,
) -> dict[str, np.ndarray]:
    requested = np.asarray(indices, dtype=np.int64)
    daily_ic = np.full(len(requested), np.nan, dtype=np.float64)
    top_excess = np.full(len(requested), np.nan, dtype=np.float64)
    bottom_excess = np.full(len(requested), np.nan, dtype=np.float64)
    spread = np.full(len(requested), np.nan, dtype=np.float64)
    for position, date in enumerate(requested):
        x = np.asarray(ranked[int(date)], dtype=np.float64)
        y = np.asarray(target[int(date)], dtype=np.float64)
        decision_valid = np.isfinite(x)
        decision_count = int(decision_valid.sum())
        if decision_count < minimum_assets:
            continue
        clean_x = x[decision_valid]
        clean_y = y[decision_valid]
        quantile_size = max(1, int(np.floor(decision_count * quantile_fraction)))
        top = _tail_weights_from_rank(clean_x, quantile_size, top=True)
        bottom = _tail_weights_from_rank(clean_x, quantile_size, top=False)
        outcome_known = np.isfinite(clean_y)
        paired_count = int(outcome_known.sum())
        if paired_count >= minimum_assets:
            xr = _rank_1d(clean_x[outcome_known])
            yr = _rank_1d(clean_y[outcome_known])
            if np.std(xr) > 1e-12 and np.std(yr) > 1e-12:
                daily_ic[position] = float(np.corrcoef(xr, yr)[0, 1])
        if paired_count < 1:
            continue
        benchmark = float(np.mean(clean_y[outcome_known]))
        top_known = outcome_known & (top > 0.0)
        bottom_known = outcome_known & (bottom > 0.0)
        top_return = np.nan
        bottom_return = np.nan
        if np.any(top_known):
            top_return = float(np.sum(top[top_known] * clean_y[top_known]) / np.sum(top[top_known]))
            top_excess[position] = top_return - benchmark
        if np.any(bottom_known):
            bottom_return = float(
                np.sum(bottom[bottom_known] * clean_y[bottom_known]) / np.sum(bottom[bottom_known])
            )
            bottom_excess[position] = bottom_return - benchmark
        if np.isfinite(top_return) and np.isfinite(bottom_return):
            spread[position] = top_return - bottom_return
    return {
        "indices": requested,
        "ic": daily_ic,
        "top_excess": top_excess,
        "bottom_excess": bottom_excess,
        "spread": spread,
    }


def daily_factor_series(
    ranked: np.ndarray,
    target: np.ndarray,
    indices: np.ndarray,
    *,
    minimum_assets: int,
    quantile_fraction: float,
    accelerator: str,
) -> dict[str, np.ndarray]:
    if accelerator == "gpu":
        raw = cross_sectional_factor_metrics_gpu(
            ranked,
            target,
            indices,
            minimum_assets=minimum_assets,
            quantile_fraction=quantile_fraction,
        )
        requested = np.asarray(indices, dtype=np.int64)
        daily_ic = np.full(len(requested), np.nan, dtype=np.float64)
        top_excess = np.full(len(requested), np.nan, dtype=np.float64)
        bottom_excess = np.full(len(requested), np.nan, dtype=np.float64)
        spread = np.full(len(requested), np.nan, dtype=np.float64)
        daily_ic[np.asarray(raw["daily_positions"], dtype=np.int64)] = np.asarray(
            raw["daily_ic"], dtype=np.float64
        )
        top_excess[np.asarray(raw["daily_top_positions"], dtype=np.int64)] = np.asarray(
            raw["daily_top_excess"], dtype=np.float64
        )
        bottom_excess[np.asarray(raw["daily_bottom_positions"], dtype=np.int64)] = np.asarray(
            raw["daily_bottom_excess"], dtype=np.float64
        )
        spread[np.asarray(raw["daily_spread_positions"], dtype=np.int64)] = np.asarray(
            raw["daily_spread"], dtype=np.float64
        )
        return {
            "indices": requested,
            "ic": daily_ic,
            "top_excess": top_excess,
            "bottom_excess": bottom_excess,
            "spread": spread,
        }
    return _daily_factor_series_cpu(
        ranked,
        target,
        indices,
        minimum_assets=minimum_assets,
        quantile_fraction=quantile_fraction,
    )


def build_market_states(panel: Any, age: np.ndarray, config: Mapping[str, Any]) -> np.ndarray:
    returns = np.asarray(panel.fields["realized_total_return"], dtype=np.float64)
    known = np.asarray(age, dtype=bool) & np.isfinite(returns)
    count = known.sum(axis=1)
    market = np.divide(
        np.where(known, returns, 0.0).sum(axis=1),
        count,
        out=np.full(len(returns), np.nan, dtype=np.float64),
        where=count > 0,
    )
    settings = config["statistics"]["market_state_stability"]
    trend_window = int(settings["trend_window_sessions"])
    vol_window = int(settings["volatility_window_sessions"])
    trend = np.full(len(market), np.nan, dtype=np.float64)
    volatility = np.full(len(market), np.nan, dtype=np.float64)
    for end in range(len(market)):
        if end + 1 >= trend_window:
            sample = market[end - trend_window + 1 : end + 1]
            if np.isfinite(sample).sum() >= int(np.ceil(0.8 * trend_window)):
                trend[end] = float(np.expm1(np.nansum(np.log1p(sample))))
        if end + 1 >= vol_window:
            sample = market[end - vol_window + 1 : end + 1]
            if np.isfinite(sample).sum() >= int(np.ceil(0.8 * vol_window)):
                volatility[end] = float(np.nanstd(sample, ddof=1))
    states = np.full(len(market), "UNKNOWN", dtype="<U20")
    historical_vols: list[float] = []
    for index in range(len(market)):
        if np.isfinite(volatility[index]):
            historical_vols.append(float(volatility[index]))
        if not np.isfinite(trend[index]) or len(historical_vols) < 60:
            continue
        if trend[index] > float(settings["bull_threshold"]):
            direction = "BULL"
        elif trend[index] < float(settings["bear_threshold"]):
            direction = "BEAR"
        else:
            direction = "NEUTRAL"
        threshold = float(np.median(historical_vols))
        regime = "HIGHVOL" if volatility[index] > threshold else "LOWVOL"
        states[index] = f"{direction}_{regime}"
    return states


def summarize_market_states(daily: Mapping[str, np.ndarray], states: np.ndarray) -> dict[str, Any]:
    values: dict[str, Any] = {}
    indices = np.asarray(daily["indices"], dtype=np.int64)
    ic = np.asarray(daily["ic"], dtype=np.float64)
    top = np.asarray(daily["top_excess"], dtype=np.float64)
    for state in sorted(set(states[indices])):
        if state == "UNKNOWN":
            continue
        selected = states[indices] == state
        ic_selected = selected & np.isfinite(ic)
        top_selected = selected & np.isfinite(top)
        if int(ic_selected.sum()) < 20:
            continue
        values[str(state)] = {
            "state_dates": int(selected.sum()),
            "ic_evaluable_dates": int(ic_selected.sum()),
            "return_evaluable_dates": int(top_selected.sum()),
            "mean_ic_raw": float(np.mean(ic[ic_selected])),
            "positive_ic_ratio_raw": float(np.mean(ic[ic_selected] > 0.0)),
            "mean_top_excess_raw": float(np.mean(top[top_selected])) if np.any(top_selected) else None,
        }
    return {
        "status": "DESCRIPTIVE_DISCOVERY_STATE_STABILITY_ONLY",
        "states": values,
        "state_count": len(values),
        "minimum_mean_ic_raw": min((row["mean_ic_raw"] for row in values.values()), default=None),
    }


def runtime_action_safety(spec: Mapping[str, Any]) -> dict[str, Any]:
    declared = str(spec.get("company_action_sensitivity", "UNKNOWN"))
    kind = str(spec.get("kind", ""))
    volume_sensitive = "VOLUME" in declared and "AMOUNT" not in declared
    arbitrary_rebase = "DIAGNOSTIC_ONLY__ARBITRARY_PER_SECURITY_REBASE" in declared
    absolute_projected_level = kind == "macd"
    survivor_eligible = not (volume_sensitive or arbitrary_rebase or absolute_projected_level)
    if volume_sensitive:
        status = "DIAGNOSTIC_ONLY__SHARE_VOLUME_SPLIT_SENSITIVE"
    elif arbitrary_rebase or absolute_projected_level:
        status = "DIAGNOSTIC_ONLY__ARBITRARY_PER_SECURITY_PRICE_REBASE"
    else:
        status = "RUNTIME_PRICE_ACTION_HANDLED__SCALE_INVARIANT"
    return {
        "declared_definition_sensitivity": declared,
        "price_handling": "CAUSAL_CHAINED_TOTAL_RETURN_SIGNAL_PROJECTION",
        "projection_absolute_level_allowed": False,
        "share_volume_handling": "RAW_SHARES__NO_FORMAL_SPLIT_ADJUSTMENT",
        "amount_handling": "RAW_TURNOVER_CNY",
        "survivor_eligible": survivor_eligible,
        "status": status,
    }


def summarize_walk_forward(
    daily: Mapping[str, np.ndarray],
    schemes: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    turnover_proxy: float,
    costs: Sequence[float],
    minimum_train_sessions: int,
    minimum_test_sessions: int,
) -> dict[str, Any]:
    indices = np.asarray(daily["indices"], dtype=np.int64)
    by_index = {int(value): position for position, value in enumerate(indices)}
    ic = np.asarray(daily["ic"], dtype=np.float64)
    top = np.asarray(daily["top_excess"], dtype=np.float64)
    bottom = np.asarray(daily["bottom_excess"], dtype=np.float64)
    spread = np.asarray(daily["spread"], dtype=np.float64)
    result: dict[str, Any] = {}
    for scheme, splits in schemes.items():
        folds: list[dict[str, Any]] = []
        for split in splits:
            train_positions = [by_index[int(value)] for value in split["train"] if int(value) in by_index]
            test_positions = [by_index[int(value)] for value in split["test"] if int(value) in by_index]
            train_ic = ic[train_positions]
            test_ic_raw = ic[test_positions]
            if (
                int(np.isfinite(train_ic).sum()) < minimum_train_sessions
                or int(np.isfinite(test_ic_raw).sum()) < minimum_test_sessions
            ):
                continue
            train_mean_ic = float(np.nanmean(train_ic))
            orientation = 1 if train_mean_ic >= 0.0 else -1
            test_ic = orientation * test_ic_raw
            oriented_top = top[test_positions] if orientation > 0 else bottom[test_positions]
            finite_test_ic = test_ic[np.isfinite(test_ic)]
            finite_oriented_top = oriented_top[np.isfinite(oriented_top)]
            if len(finite_oriented_top) < 1:
                continue
            mean_oriented_top = float(np.mean(finite_oriented_top))
            folds.append(
                {
                    "test_year": int(split["test_year"]),
                    "train_start": split["train_start"],
                    "train_end": split["train_end"],
                    "test_start": split["test_start"],
                    "test_end": split["test_end"],
                    "orientation": orientation,
                    "train_mean_ic_raw": train_mean_ic,
                    "test_mean_ic_oriented": float(np.mean(finite_test_ic)),
                    "test_positive_ic_ratio_oriented": float(np.mean(finite_test_ic > 0.0)),
                    "test_top_excess_oriented": mean_oriented_top,
                    "test_ic_evaluable_dates": int(len(finite_test_ic)),
                    "test_return_evaluable_dates": int(len(finite_oriented_top)),
                    "cost_adjusted_top_excess_proxy": {
                        str(int(cost)): mean_oriented_top - cost / 10_000.0 * turnover_proxy
                        for cost in costs
                    },
                }
            )
        result[scheme] = {
            "folds": folds,
            "summary": {
                "fold_count": len(folds),
                "mean_test_ic_oriented": float(np.mean([row["test_mean_ic_oriented"] for row in folds])) if folds else None,
                "positive_fold_ic_ratio": float(np.mean([row["test_mean_ic_oriented"] > 0.0 for row in folds])) if folds else None,
                "worst_fold_ic": float(min((row["test_mean_ic_oriented"] for row in folds), default=np.nan)) if folds else None,
            },
        }
    return {
        "status": "DISCOVERY_WALK_FORWARD_ORIENTATION_ONLY",
        "schemes": result,
        "cost_note": (
            "fold cost uses orientation-matched full-Discovery turnover as an explicit proxy; "
            "opened-limit-board event stress is applied only in the separate full-Discovery cost diagnostic; "
            "not a tradable backtest"
        ),
    }


def _tail_membership(
    ranked: np.ndarray,
    indices: np.ndarray,
    fraction: float,
    *,
    top: bool,
) -> np.ndarray:
    return _tail_weights_matrix(ranked, indices, fraction, top=top) > 0.0


def _tail_weights_matrix(
    ranked: np.ndarray,
    indices: np.ndarray,
    fraction: float,
    *,
    top: bool,
) -> np.ndarray:
    selected = np.zeros((len(indices), ranked.shape[1]), dtype=np.float32)
    for position, date in enumerate(np.asarray(indices, dtype=np.int64)):
        row = np.asarray(ranked[int(date)], dtype=np.float64)
        valid = np.isfinite(row)
        count = int(valid.sum())
        if count < 2:
            continue
        size = max(1, int(np.floor(count * fraction)))
        clean = row[valid]
        if top:
            cutoff = np.partition(clean, count - size)[count - size]
            strict = valid & (row > cutoff)
            tied = valid & (row == cutoff)
        else:
            cutoff = np.partition(clean, size - 1)[size - 1]
            strict = valid & (row < cutoff)
            tied = valid & (row == cutoff)
        selected[position, strict] = 1.0
        tied_count = int(tied.sum())
        remaining = size - int(strict.sum())
        if tied_count and remaining > 0:
            selected[position, tied] = float(remaining) / float(tied_count)
    return selected


def execution_feasibility(
    panel: Any,
    ranked: np.ndarray,
    indices: np.ndarray,
    *,
    horizon: int,
    fraction: float,
    top: bool,
) -> dict[str, Any]:
    membership = _tail_weights_matrix(ranked, indices, fraction, top=top)
    entry = np.asarray(indices, dtype=np.int64) + 1
    exit_dates = entry + int(horizon)
    selected_mass = float(membership.sum())
    entry_known = np.isfinite(np.asarray(panel.fields["open"])[entry])
    exit_known = np.isfinite(np.asarray(panel.fields["open"])[exit_dates])
    buyable = np.asarray(panel.buyable, dtype=bool)[entry]
    sellable = np.asarray(panel.sellable, dtype=bool)[exit_dates]
    known_entry_mass = float((membership * entry_known).sum())
    known_exit_mass = float((membership * exit_known).sum())
    execution_masks = panel.metadata.get("execution_masks", {})
    reopened = np.asarray(
        execution_masks.get("reopened_limit_board", np.zeros(panel.shape)), dtype=bool
    )
    if reopened.shape != panel.shape:
        raise RuntimeError("reopened-limit-board execution mask shape mismatch")
    reopened_entry_rate = float((membership * reopened[entry]).sum() / max(1.0, selected_mass))
    reopened_exit_rate = float((membership * reopened[exit_dates]).sum() / max(1.0, selected_mass))
    return {
        "status": "SEPARATE_MECHANICAL_FEASIBILITY__NOT_LABEL_FILTER__NOT_BACKTEST",
        "selected_asset_date_mass": selected_mass,
        "entry_known_rate": known_entry_mass / max(1.0, selected_mass),
        "known_entry_buyable_rate": float((membership * entry_known * buyable).sum() / max(1.0, known_entry_mass)),
        "exit_known_rate": known_exit_mass / max(1.0, selected_mass),
        "known_exit_sellable_rate": float((membership * exit_known * sellable).sum() / max(1.0, known_exit_mass)),
        "reopened_limit_board_entry_rate": reopened_entry_rate,
        "reopened_limit_board_exit_rate": reopened_exit_rate,
        "reopened_limit_board_round_trip_event_rate": reopened_entry_rate + reopened_exit_rate,
        "opened_limit_board_policy": "ALLOWED_BY_FORMAL_LOADER",
        "one_price_board_policy": "SIDE_BLOCKED_BY_FORMAL_LOADER",
        "selected_tail": "HIGH_FACTOR" if top else "LOW_FACTOR",
    }


def capacity_proxy(
    ranked: np.ndarray,
    indices: np.ndarray,
    trailing_amount: np.ndarray,
    *,
    fraction: float,
    participation_rate: float,
    top: bool,
) -> dict[str, Any]:
    membership = _tail_weights_matrix(ranked, indices, fraction, top=top)
    values: list[float] = []
    unknown_dates = 0
    for position, date in enumerate(np.asarray(indices, dtype=np.int64)):
        weights = np.asarray(membership[position], dtype=np.float64)
        chosen = weights > 0.0
        mass = float(weights.sum())
        if mass <= 0.0:
            continue
        adv = np.asarray(trailing_amount[int(date), chosen], dtype=np.float64)
        if not np.isfinite(adv).all() or np.any(adv <= 0.0):
            unknown_dates += 1
            continue
        portfolio_weights = weights[chosen] / mass
        values.append(float(np.min(participation_rate * adv / portfolio_weights)))
    array = np.asarray(values, dtype=np.float64)
    return {
        "status": "DISCOVERY_LIQUIDITY_PROXY__NOT_CAPACITY_GUARANTEE",
        "evaluable_dates": int(len(array)),
        "unknown_dates": int(unknown_dates),
        "median_portfolio_cny": float(np.median(array)) if len(array) else None,
        "p10_portfolio_cny": float(np.quantile(array, 0.10)) if len(array) else None,
        "participation_rate": participation_rate,
        "selected_tail": "HIGH_FACTOR" if top else "LOW_FACTOR",
    }


def _correlation_sketch(
    ranked: np.ndarray, decision_indices: np.ndarray, *, date_count: int, symbol_count: int
) -> np.ndarray:
    dates = np.asarray(decision_indices, dtype=np.int64)
    date_positions = np.unique(np.linspace(0, len(dates) - 1, min(date_count, len(dates)), dtype=np.int64))
    symbol_positions = np.unique(
        np.linspace(0, ranked.shape[1] - 1, min(symbol_count, ranked.shape[1]), dtype=np.int64)
    )
    return np.asarray(ranked[np.ix_(dates[date_positions], symbol_positions)], dtype=np.float32).ravel()


def correlation_clusters(
    records: Sequence[Mapping[str, Any]], stage: Path, *, threshold: float, minimum_pairs: int
) -> dict[str, Any]:
    successful = [row for row in records if row.get("mechanical", {}).get("completed") is True]
    if not successful:
        return {"status": "NO_SUCCESSFUL_FACTORS", "clusters": []}
    sketches: list[np.ndarray] = []
    ids: list[str] = []
    for row in successful:
        pointer = row.get("correlation_sketch")
        if not isinstance(pointer, dict):
            continue
        path = stage / str(pointer["path"])
        if sha256_file(path) != pointer.get("sha256"):
            raise RuntimeError(f"correlation sketch hash mismatch: {row['factor_id']}")
        sketches.append(np.load(path, allow_pickle=False).astype(np.float64, copy=False))
        ids.append(str(row["factor_id"]))
    if not sketches:
        return {"status": "NO_VALID_SKETCHES", "clusters": []}
    matrix = np.vstack(sketches)
    valid = np.isfinite(matrix).astype(np.float64)
    clean = np.where(np.isfinite(matrix), matrix, 0.0)
    counts = valid @ valid.T
    sum_x = clean @ valid.T
    sum_x2 = (clean * clean) @ valid.T
    sum_xy = clean @ clean.T
    safe_n = np.maximum(counts, 1.0)
    covariance = sum_xy - sum_x * sum_x.T / safe_n
    variance_left = np.maximum(sum_x2 - sum_x * sum_x / safe_n, 0.0)
    variance_right = variance_left.T
    denominator = np.sqrt(variance_left * variance_right)
    correlation = np.divide(
        covariance,
        denominator,
        out=np.full_like(covariance, np.nan),
        where=(counts >= minimum_pairs) & (denominator > 1e-12),
    )
    parent = list(range(len(ids)))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    rows, columns = np.where(np.triu(np.abs(correlation) >= threshold, k=1))
    for left, right in zip(rows.tolist(), columns.tolist()):
        union(int(left), int(right))
    groups: dict[int, list[str]] = {}
    for index, factor_id in enumerate(ids):
        groups.setdefault(find(index), []).append(factor_id)
    records_by_id = {str(row["factor_id"]): row for row in successful}
    def representative_key(factor_id: str) -> tuple[float, float, str]:
        row = records_by_id[factor_id]
        primary_horizon = str(row.get("primary_horizon_sessions", ""))
        metrics = row.get("horizons", {}).get(primary_horizon, {})
        return (
            float(metrics.get("q_value", 1.0)),
            -abs(float(metrics.get("mean_ic", 0.0))),
            factor_id,
        )

    clusters = []
    for sequence, members in enumerate(sorted(groups.values(), key=lambda value: (-len(value), value))):
        ordered = sorted(members)
        representative = min(ordered, key=representative_key)
        clusters.append(
            {
                "cluster_id": f"cluster-{sequence:04d}",
                "factor_ids": ordered,
                "size": len(ordered),
                "representative_factor_id": representative,
            }
        )
    return {
        "status": "COMPLETED__PAIRWISE_COMPLETE_RANK_SKETCH",
        "threshold_absolute_rank_correlation": threshold,
        "minimum_pair_observations": minimum_pairs,
        "factor_count": len(ids),
        "cluster_count": len(clusters),
        "clusters": clusters,
    }


def apply_family_by(records: list[dict[str, Any]], horizons: Sequence[int]) -> None:
    for horizon in horizons:
        eligible = [
            row
            for row in records
            if row.get("mechanical", {}).get("completed") is True
            and str(horizon) in row.get("horizons", {})
        ]
        metrics = [FactorMetrics(**row["horizons"][str(horizon)]) for row in eligible]
        # Mechanical failures remain members of the predeclared test family.
        # Treating their unavailable p-values as one is conservative and keeps
        # the family size fixed instead of rewarding an engine failure.
        padded = metrics + (
            [FactorMetrics(**{**metrics[0].to_dict(), "p_value": 1.0})] * (len(records) - len(metrics))
            if metrics
            else []
        )
        adjusted = benjamini_yekutieli(padded)[: len(metrics)]
        for row, metrics in zip(eligible, adjusted):
            row["horizons"][str(horizon)] = metrics.to_dict()
    for row in records:
        if row.get("mechanical", {}).get("completed") is not True:
            continue
        primary = str(row["primary_horizon_sessions"])
        metrics = row["horizons"][primary]
        orientation = 1 if float(metrics["mean_ic"]) >= 0.0 else -1
        row["statistical"] = {
            "status": "DISCOVERY_BY_FDR_CANDIDATE_ONLY",
            "primary_orientation": orientation,
            "by_fdr_0_10": float(metrics["q_value"]) <= 0.10,
            "validation_consumed": False,
            "holdout_opened": False,
        }
        gross_excess = float(
            metrics["top_excess_mean"] if orientation > 0 else metrics["bottom_excess_mean"]
        )
        oriented_turnover = float(
            metrics["top_tail_turnover"] if orientation > 0 else metrics["bottom_tail_turnover"]
        )
        reopened_event_rate = float(
            row.get("execution_feasibility", {}).get(
                "reopened_limit_board_round_trip_event_rate", 0.0
            )
        )
        opened_extra_bps = float(row["cost"].get("opened_limit_board_extra_slippage_bps", 0.0))
        opened_extra_return = opened_extra_bps / 10_000.0 * reopened_event_rate
        row["cost"]["orientation_tail"] = "HIGH_FACTOR" if orientation > 0 else "LOW_FACTOR"
        row["cost"]["orientation_tail_turnover"] = oriented_turnover
        row["cost"]["reopened_limit_board_round_trip_event_rate"] = reopened_event_rate
        row["cost"]["reopened_limit_board_extra_slippage_return"] = opened_extra_return
        row["cost"]["oriented_top_excess_after_cost"] = {
            key: gross_excess
            - float(key) / 10_000.0 * oriented_turnover
            - opened_extra_return
            for key in row["cost"]["round_trip_cost_bps"]
        }
        row["cost"]["survives_30bps_descriptive"] = (
            row["cost"]["oriented_top_excess_after_cost"].get("30", -1.0) > 0.0
        )


def _tree_hash(paths: Iterable[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: str(value.relative_to(root)).replace("\\", "/")):
        relative = str(path.relative_to(root)).replace("\\", "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def verify_committed_stage(
    stage: Path, fingerprint: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify a crash-complete stage before the sole allowed recovery move."""

    commit_path = stage / "COMMITTED.json"
    manifest_path = stage / "manifest.json"
    summary_path = stage / "summary.json"
    if not (commit_path.is_file() and manifest_path.is_file() and summary_path.is_file()):
        raise RuntimeError("committed recovery stage is incomplete")
    commit = _load_json(commit_path)
    manifest = _load_json(manifest_path)
    summary = _load_json(summary_path)
    if commit.get("status") != "COMMITTED" or commit.get("manifest_sha256") != sha256_file(manifest_path):
        raise RuntimeError("committed recovery marker or manifest hash mismatch")
    expected = {
        "run_id": fingerprint["run_id"],
        "factor_count": fingerprint["factor_count"],
        "run_kind": fingerprint["run_kind"],
        "permutations": fingerprint["permutations"],
        "config_sha256": fingerprint["config_sha256"],
        "catalog_sha256": fingerprint["catalog_sha256"],
        "code_sha256": fingerprint["code_sha256"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(f"committed recovery fingerprint mismatch: {key}")
    for name, digest in manifest.get("artifacts", {}).items():
        path = stage / str(name)
        if not path.is_file() or sha256_file(path) != digest:
            raise RuntimeError(f"committed recovery artifact mismatch: {name}")
    if manifest.get("checkpoint_tree_sha256") != _tree_hash(
        (stage / "checkpoints").glob("*.json"), stage
    ):
        raise RuntimeError("committed recovery checkpoint tree mismatch")
    if manifest.get("correlation_sketch_tree_sha256") != _tree_hash(
        (stage / "correlation_sketches").glob("*.npy"), stage
    ):
        raise RuntimeError("committed recovery sketch tree mismatch")
    if summary.get("run_id") != fingerprint["run_id"]:
        raise RuntimeError("committed recovery summary identity mismatch")
    return manifest, summary


def _main_unlocked(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not RUN_ID.fullmatch(args.run_id):
        raise SystemExit("invalid run id")
    if args.permutations is not None and args.permutations < 0:
        raise SystemExit("permutations must be non-negative")
    if sha256_file(CONFIG) != EXPECTED_CONFIG_SHA256:
        raise RuntimeError("execution config differs from the pre-frozen file hash")
    config = _load_json(CONFIG)
    catalog = _load_json(CATALOG)
    records = validate_contract(config, catalog)
    catalog_record_count = len(records)
    if args.factor_limit is not None:
        if args.factor_limit < 1 or args.factor_limit > len(records):
            raise SystemExit(f"factor-limit must be within 1..{len(records)}")
        records = records[: args.factor_limit]
    partial_catalog_run = len(records) != catalog_record_count
    if partial_catalog_run and not (args.smoke_only or args.contract_only):
        raise SystemExit("partial factor-limit requires --smoke-only outside contract-only mode")
    if args.smoke_only and not partial_catalog_run:
        raise SystemExit("--smoke-only requires a factor-limit smaller than the frozen catalog")
    frozen_permutations = int(config["statistics"]["permutations"])
    if (
        args.permutations is not None
        and int(args.permutations) != frozen_permutations
        and not (args.smoke_only or args.contract_only)
    ):
        raise SystemExit("permutation override requires --smoke-only outside contract-only mode")
    permutations = int(frozen_permutations if args.permutations is None else args.permutations)
    intended = {
        "run_id": args.run_id,
        "factor_count": len(records),
        "catalog_record_count": int(catalog["record_count"]),
        "run_kind": "ENGINEERING_SMOKE_ONLY" if args.smoke_only else "FULL_CATALOG_DISCOVERY",
        "partial_catalog_run": partial_catalog_run,
        "permutations": permutations,
        "accelerator_requested": args.accelerator,
        "dataset": config["dataset"],
        "decision_window": config["discovery_window"],
        "validation_consumed": False,
        "holdout_opened": False,
    }
    if args.contract_only:
        print(json.dumps({"status": "CONTRACT_PASS__NO_DATA_READ", **intended}, ensure_ascii=False, indent=2))
        return 0

    final_root = (
        PROJECT / "engineering_runs/factor_factory"
        if args.smoke_only
        else PROJECT / "research_runs/factor_factory"
    )
    final = final_root / args.run_id
    stage = PROJECT / ".tmp/r0-classic-discovery" / args.run_id
    if final.exists():
        raise SystemExit(f"immutable final run already exists: {final}")
    accelerator, runtime_accelerator = resolve_accelerator(args.accelerator)
    config_sha = sha256_file(CONFIG)
    catalog_sha = sha256_file(CATALOG)
    runner_sha = sha256_file(Path(__file__))
    code_sha256 = {
        "runner": runner_sha,
        "engine": sha256_file(PROJECT / "factor_factory/r0_classic_engine.py"),
        "evaluation": sha256_file(PROJECT / "factor_factory/evaluation.py"),
        "dense_loader": sha256_file(PROJECT / "factor_factory/r0_dense_panel.py"),
        "total_return_projection": sha256_file(PROJECT / "factor_factory/total_return_projection.py"),
        "accelerator": sha256_file(PROJECT / "factor_factory/accelerator.py"),
        "expressions": sha256_file(PROJECT / "factor_factory/expressions.py"),
        "schema": sha256_file(PROJECT / "factor_factory/schema.py"),
        "supplier_formula_engine": sha256_file(PROJECT / "factor_factory/supplier_formula_engine.py"),
        "r0_shsz_panel_input": sha256_file(PROJECT / "factor_factory/r0_shsz_panel_input.py"),
        "validation": sha256_file(PROJECT / "factor_factory/validation.py"),
    }
    factor_ids = [str(row["factor_id"]) for row in records]
    fingerprint = {
        **intended,
        "accelerator_resolved": accelerator,
        "config_sha256": config_sha,
        "catalog_sha256": catalog_sha,
        "runner_sha256": runner_sha,
        "code_sha256": code_sha256,
        "factor_definition_sha256": [str(row["definition_sha256"]) for row in records],
    }
    store = CheckpointStore(stage, factor_ids, fingerprint)
    if (stage / "COMMITTED.json").exists():
        _, recovered_summary = verify_committed_stage(stage, fingerprint)
        (stage / "RUNNING.json").unlink(missing_ok=True)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, final)
        print(
            json.dumps(
                {
                    "status": "RECOVERED_COMMITTED_STAGE__NO_RECOMPUTE",
                    "run_id": args.run_id,
                    "path": str(final),
                    "mechanical_completed": recovered_summary.get("mechanical_completed_count"),
                    "mechanical_failed": recovered_summary.get("mechanical_failed_count"),
                    "tradable_conclusions": 0,
                    "validation_consumed": False,
                    "holdout_opened": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    running = store.initialize()
    checkpoints = store.load_prefix()

    # Import lazily so contract tests do not initialize the heavy engine.
    from factor_factory.r0_classic_engine import R0ClassicFactorEngine  # noqa: E402

    t0 = time.perf_counter()
    window = config["discovery_window"]
    panel = load_r0_shsz_panel_dense(
        start_date=window["load_start"],
        end_date=window["hard_data_cutoff"],
        max_panel_cells=30_000_000,
    )
    panel_identity = (
        panel.metadata.get("dataset_id"),
        panel.metadata.get("snapshot_id"),
        panel.metadata.get("content_hash"),
    )
    if panel_identity != (EXPECTED_DATASET, EXPECTED_SNAPSHOT, EXPECTED_CONTENT_HASH):
        raise RuntimeError("formal dense loader returned a different snapshot")
    if panel.dates[-1] > window["hard_data_cutoff"] or panel.dates[-1] >= window["protocol_holdout"][0]:
        raise RuntimeError("Holdout content entered the Discovery panel")
    age = minimum_trading_age_mask(panel, int(config["universe"]["minimum_observed_trading_sessions"]))
    projection = build_total_return_signal_fields(panel)
    engine = R0ClassicFactorEngine(
        panel,
        projection,
        research_eligible=age,
        accelerator=accelerator,
        gpu_column_chunk=int(config["accelerator"]["gpu_column_chunk"]),
    )
    horizons = [int(value) for value in config["labels"]["horizons_sessions"]]
    targets: dict[int, np.ndarray] = {}
    decisions: dict[int, np.ndarray] = {}
    for horizon in horizons:
        decisions[horizon] = effective_decision_indices(
            panel.dates,
            start=window["decision_start"],
            end=window["decision_end"],
            horizon=horizon,
        )
        targets[horizon] = forward_total_returns(
            panel,
            horizon,
            decision_start=int(decisions[horizon][0]),
            decision_end=int(decisions[horizon][-1]),
            decision_eligible=age,
        )
    primary = int(config["labels"]["primary_horizon_sessions"])
    state_vector = build_market_states(panel, age, config)
    capacity = config["capacity_proxy"]
    capacity_window = int(capacity["trailing_amount_window_sessions"])
    amount_values = np.asarray(panel.fields["amount"], dtype=np.float32)
    trailing_amount = _rolling(
        amount_values,
        capacity_window,
        "mean",
    )
    amount_valid = np.isfinite(amount_values) & np.asarray(panel.eligible, dtype=bool)
    amount_counts = np.vstack(
        [
            np.zeros((1, amount_valid.shape[1]), dtype=np.int32),
            np.cumsum(amount_valid, axis=0, dtype=np.int32),
        ]
    )
    full_capacity_window = np.zeros_like(amount_valid)
    full_capacity_window[capacity_window - 1 :] = (
        amount_counts[capacity_window:] - amount_counts[:-capacity_window]
    ) == capacity_window
    trailing_amount = np.where(full_capacity_window, trailing_amount, np.nan).astype(
        np.float32, copy=False
    )
    schemes: dict[str, list[dict[str, Any]]] = {}
    samples = config["sample_schemes"]
    for profile in samples["separation_profiles"]:
        for years in samples["rolling_train_years"]:
            key = f"rolling-{int(years)}y__{profile['profile_id']}"
            schemes[key] = build_walk_forward_splits(
                panel.dates,
                decisions[primary],
                train_years=int(years),
                first_test_year=int(samples["first_test_year"]),
                last_test_year=int(samples["last_test_year"]),
                purge_sessions=int(profile["purge_sessions"]),
                gap_sessions=int(profile["gap_sessions"]),
                minimum_train_sessions=int(samples["minimum_train_sessions"]),
                minimum_test_sessions=int(samples["minimum_test_sessions"]),
            )
        key = f"expanding__{profile['profile_id']}"
        schemes[key] = build_walk_forward_splits(
            panel.dates,
            decisions[primary],
            train_years=None,
            first_test_year=int(samples["first_test_year"]),
            last_test_year=int(samples["last_test_year"]),
            purge_sessions=int(profile["purge_sessions"]),
            gap_sessions=int(profile["gap_sessions"]),
            minimum_train_sessions=int(samples["minimum_train_sessions"]),
            minimum_test_sessions=int(samples["minimum_test_sessions"]),
        )
    load_seconds = time.perf_counter() - t0

    costs = [float(value) for value in config["execution_and_cost"]["round_trip_cost_bps"]]
    minimum_assets = int(config["universe"]["minimum_assets_per_date"])
    quantile_fraction = float(config["statistics"]["quantile_fraction"])
    sketch_config = config["statistics"]["correlation_clustering"]
    completed = len(checkpoints)
    factor_t0 = time.perf_counter()
    for index, spec in enumerate(records):
        if index < completed:
            continue
        item_started = time.perf_counter()
        factor_id = str(spec["factor_id"])
        try:
            values = engine.evaluate(spec)
            values = np.asarray(values, dtype=np.float32)
            if values.shape != panel.shape:
                raise RuntimeError(f"factor engine returned shape {values.shape}, expected {panel.shape}")
            values = np.where(age, values, np.nan)
            ranked = winsorized_cross_sectional_rank(
                values,
                age,
                minimum_assets=minimum_assets,
            )
            metrics: dict[str, Any] = {}
            for horizon in horizons:
                result = evaluate_factor(
                    ranked,
                    targets[horizon],
                    decisions[horizon],
                    minimum_assets=minimum_assets,
                    quantile_fraction=quantile_fraction,
                    round_trip_cost_bps=15.0,
                    fold_count=int(config["statistics"]["fold_count"]),
                    permutations=permutations,
                    permutation_block_length=max(
                        int(config["statistics"]["permutation_minimum_block_sessions"]),
                        int(horizon) + 1,
                    ),
                    seed=20260806 + index * 1009 + horizon,
                    accelerator=accelerator,
                )
                metrics[str(horizon)] = result.to_dict()
            daily = daily_factor_series(
                ranked,
                targets[primary],
                decisions[primary],
                minimum_assets=minimum_assets,
                quantile_fraction=quantile_fraction,
                accelerator=accelerator,
            )
            primary_metrics = metrics[str(primary)]
            primary_orientation = 1 if float(primary_metrics["mean_ic"]) >= 0.0 else -1
            oriented_turnover = float(
                primary_metrics[
                    "top_tail_turnover" if primary_orientation > 0 else "bottom_tail_turnover"
                ]
            )
            feasibility = execution_feasibility(
                panel,
                ranked,
                decisions[primary],
                horizon=primary,
                fraction=quantile_fraction,
                top=primary_orientation > 0,
            )
            record: dict[str, Any] = {
                "factor_index": index,
                "factor_id": factor_id,
                "family": spec["family"],
                "kind": spec["kind"],
                "formula": spec["formula"],
                "definition_sha256": spec["definition_sha256"],
                "source_refs": spec["source_refs"],
                "company_action_sensitivity": spec["company_action_sensitivity"],
                "runtime_company_action_handling": runtime_action_safety(spec),
                "causal_timing": spec["causal_timing"],
                "mechanical": {
                    "completed": True,
                    "status": "FACTOR_VALUES_COMPUTED_EPHEMERALLY__NOT_PERSISTED",
                    "finite_value_count": int(np.isfinite(values).sum()),
                    "finite_rank_count": int(np.isfinite(ranked).sum()),
                },
                "horizons": metrics,
                "primary_horizon_sessions": primary,
                "factor_decay": {
                    "status": "MULTI_HORIZON_DISCOVERY_DIAGNOSTIC",
                    "mean_ic_raw_by_horizon": {
                        str(horizon): float(metrics[str(horizon)]["mean_ic"])
                        for horizon in horizons
                    },
                    "oriented_mean_ic_by_horizon": {
                        str(horizon): (
                            1 if float(metrics[str(primary)]["mean_ic"]) >= 0.0 else -1
                        )
                        * float(metrics[str(horizon)]["mean_ic"])
                        for horizon in horizons
                    },
                },
                "walk_forward": summarize_walk_forward(
                    daily,
                    schemes,
                    turnover_proxy=oriented_turnover,
                    costs=costs,
                    minimum_train_sessions=int(samples["minimum_train_sessions"]),
                    minimum_test_sessions=int(samples["minimum_test_sessions"]),
                ),
                "market_state_stability": summarize_market_states(daily, state_vector),
                "execution_feasibility": feasibility,
                "capacity_proxy": capacity_proxy(
                    ranked,
                    decisions[primary],
                    trailing_amount,
                    fraction=quantile_fraction,
                    participation_rate=float(capacity["participation_rate"]),
                    top=primary_orientation > 0,
                ),
                "cost": {
                    "status": "DISCOVERY_COST_STRESS_ONLY__NOT_BACKTEST",
                    "round_trip_cost_bps": [str(int(value)) for value in costs],
                    "opened_limit_board_extra_slippage_bps": float(
                        config["execution_and_cost"]["opened_limit_board_extra_slippage_bps"]
                    ),
                },
                "statistical": {"status": "PENDING_FAMILY_WIDE_BY_FDR"},
                "tradable": {
                    "value": False,
                    "status": "NOT_ESTABLISHED__NO_VALIDATION_NO_HOLDOUT_NO_PORTFOLIO_BACKTEST",
                },
                "seconds": time.perf_counter() - item_started,
                "validation_consumed": False,
                "holdout_opened": False,
            }
            sketch = _correlation_sketch(
                ranked,
                decisions[primary],
                date_count=int(sketch_config["sketch_date_count"]),
                symbol_count=int(sketch_config["sketch_symbol_count"]),
            )
            store.commit_factor(index, record, sketch)
            del values, ranked, daily, sketch
        except R0ClassicFactorError as error:
            failure = {
                "factor_index": index,
                "factor_id": factor_id,
                "family": spec["family"],
                "kind": spec["kind"],
                "formula": spec["formula"],
                "definition_sha256": spec["definition_sha256"],
                "mechanical": {
                    "completed": False,
                    "status": "FAILED_ISOLATED",
                    "error_type": type(error).__name__,
                    "error": str(error)[:2000],
                    "traceback": traceback.format_exc(limit=12),
                },
                "statistical": {"status": "NOT_RUN_MECHANICAL_FAILURE"},
                "cost": {"status": "NOT_RUN_MECHANICAL_FAILURE"},
                "tradable": {"value": False, "status": "NOT_ESTABLISHED"},
                "seconds": time.perf_counter() - item_started,
                "validation_consumed": False,
                "holdout_opened": False,
            }
            store.commit_factor(index, failure, None)
        except (AcceleratorError, MemoryError, SystemError):
            # Explicit GPU execution is fail-closed.  Resource/runtime failures
            # are stage-level and resumable; recording them as one bad factor
            # could otherwise commit a mostly failed formal run.
            raise
        gc.collect()
        done = index + 1
        elapsed = time.perf_counter() - factor_t0
        eta = elapsed / max(1, done - completed) * (len(records) - done)
        atomic_json(
            stage / "progress.json",
            {
                "status": "RUNNING__DISCOVERY_ONLY",
                "completed": done,
                "total": len(records),
                "percent": round(100.0 * done / len(records), 2),
                "elapsed_seconds": elapsed,
                "eta_seconds": max(0.0, eta),
                "last_factor_id": factor_id,
                "validation_consumed": False,
                "holdout_opened": False,
            },
        )
        print(
            json.dumps(
                {"progress": f"{done}/{len(records)}", "last": factor_id, "eta_seconds": round(max(0.0, eta), 1)},
                ensure_ascii=False,
            ),
            flush=True,
        )

    final_records = store.load_prefix()
    if len(final_records) != len(records):
        raise RuntimeError("factor loop ended without a complete checkpoint prefix")
    if args.smoke_only:
        for row in final_records:
            if row.get("mechanical", {}).get("completed") is True:
                row["statistical"] = {
                    "status": "SMOKE_ONLY__NO_FAMILY_WIDE_FDR_CANDIDATE",
                    "by_fdr_0_10": False,
                    "validation_consumed": False,
                    "holdout_opened": False,
                }
                row["cost"]["survives_30bps_descriptive"] = False
    else:
        apply_family_by(final_records, horizons)
    cluster = correlation_clusters(
        final_records,
        stage,
        threshold=float(sketch_config["rank_correlation_threshold"]),
        minimum_pairs=int(sketch_config["minimum_pair_observations"]),
    )
    atomic_json(stage / "correlation_clusters.json", cluster)
    factors_payload = b"".join(_json_bytes(row, pretty=False) for row in final_records)
    atomic_bytes(stage / "factors.jsonl", factors_payload)
    successful = [row for row in final_records if row.get("mechanical", {}).get("completed") is True]
    fdr = [row for row in successful if row.get("statistical", {}).get("by_fdr_0_10") is True]
    action_safe_fdr = [
        row
        for row in fdr
        if row.get("runtime_company_action_handling", {}).get("survivor_eligible") is True
    ]
    cost_survivors = [row for row in fdr if row.get("cost", {}).get("survives_30bps_descriptive") is True]
    action_safe_cost_survivors = [
        row
        for row in action_safe_fdr
        if row.get("cost", {}).get("survives_30bps_descriptive") is True
    ]
    primary_key = str(primary)
    ranked_success = sorted(
        successful,
        key=lambda row: (
            float(row["horizons"][primary_key]["q_value"]),
            -abs(float(row["horizons"][primary_key]["mean_ic"])),
            row["factor_id"],
        ),
    )
    def compact(rows: Sequence[Mapping[str, Any]], classification: str) -> list[dict[str, Any]]:
        ordered = sorted(
            rows,
            key=lambda row: (
                float(row["horizons"][primary_key]["q_value"]),
                -abs(float(row["horizons"][primary_key]["mean_ic"])),
                row["factor_id"],
            ),
        )
        return [
            {
                "classification": classification,
                "factor_id": row["factor_id"],
                "family": row["family"],
                "formula": row["formula"],
                "metrics": row["horizons"][primary_key],
                "cost": row["cost"],
                "runtime_company_action_handling": row["runtime_company_action_handling"],
                "tradable": row["tradable"],
            }
            for row in ordered[:30]
        ]
    total_seconds = time.perf_counter() - t0
    summary = {
        "status": (
            "COMPLETED__ENGINEERING_SMOKE_ONLY__NO_FDR_NO_VALIDATION_NO_HOLDOUT"
            if args.smoke_only
            else "COMPLETED__DISCOVERY_ONLY__NO_VALIDATION_NO_HOLDOUT"
        ),
        "run_id": args.run_id,
        "catalog_factor_count": int(catalog["record_count"]),
        "executed_factor_count": len(records),
        "partial_catalog_run": partial_catalog_run,
        "mechanical_completed_count": len(successful),
        "mechanical_failed_count": len(records) - len(successful),
        "statistical_by_fdr_0_10_count": len(fdr),
        "action_safe_statistical_by_fdr_0_10_count": len(action_safe_fdr),
        "cost_30bps_descriptive_survivor_count": len(cost_survivors),
        "action_safe_cost_30bps_descriptive_survivor_count": len(action_safe_cost_survivors),
        "tradable_conclusion_count": 0,
        "top_mechanical_descriptive_only": [] if args.smoke_only else compact(
            ranked_success, "MECHANICAL_DESCRIPTIVE_ONLY__NOT_STATISTICAL_CANDIDATE"
        ),
        "statistical_candidates": [] if args.smoke_only else compact(
            fdr, "DISCOVERY_BY_FDR_CANDIDATE_ONLY"
        ),
        "action_safe_statistical_candidates": [] if args.smoke_only else compact(
            action_safe_fdr, "ACTION_SAFE_DISCOVERY_BY_FDR_CANDIDATE_ONLY"
        ),
        "cost_survivors": [] if args.smoke_only else compact(
            cost_survivors, "DISCOVERY_BY_FDR_AND_30BPS_DESCRIPTIVE_COST_SURVIVOR"
        ),
        "action_safe_cost_survivors": [] if args.smoke_only else compact(
            action_safe_cost_survivors,
            "ACTION_SAFE_DISCOVERY_BY_FDR_AND_30BPS_DESCRIPTIVE_COST_SURVIVOR",
        ),
        "panel": {
            "shape": list(panel.shape),
            "date_start": panel.dates[0],
            "date_end": panel.dates[-1],
            "dataset_id": panel.metadata["dataset_id"],
            "snapshot_id": panel.metadata["snapshot_id"],
            "content_hash": panel.metadata["content_hash"],
            "holdout_content_loaded": False,
            "physical_payload_isolation": {
                "selected_payload_count": panel.metadata.get("bounded_slice", {}).get(
                    "selected_payload_count"
                ),
                "selected_partition_end_exclusive_max": max(
                    (
                        item.get("partition_end_exclusive")
                        for item in panel.metadata.get("bounded_slice", {}).get(
                            "selected_payloads", []
                        )
                    ),
                    default=None,
                ),
                "policy": "MANIFEST_MONTH_FILTER_BEFORE_DUCKDB__ONLY_SELECTED_PAYLOADS_HASHED_AND_OPENED",
            },
            "terminal_event_mask_status": panel.metadata.get("terminal_event_mask_status"),
            "forward_total_return_diagnostics": panel.metadata.get(
                "forward_total_return_diagnostics", {}
            ),
            "factor_signal_projection": projection.diagnostics,
        },
        "horizon_decision_date_counts": {str(key): int(len(value)) for key, value in decisions.items()},
        "boundaries": {
            "mechanical": "ENGINE_AND_DATA_COMPLETION_ONLY",
            "statistical": "DISCOVERY_CANDIDATE_ONLY",
            "cost": "DISCOVERY_STRESS_SURVIVOR_ONLY",
            "tradable": "FALSE__REQUIRES_VALIDATION_HOLDOUT_PORTFOLIO_BACKTEST",
        },
        "known_limitations": [
            "R0_HISTORICAL_ST_AND_SUSPENSION_SEMANTICS_ARE_NOT_COMPLETE",
            "WALK_FORWARD_COST_USES_EXPLICIT_FULL_DISCOVERY_TURNOVER_PROXY",
            "CAPACITY_IS_A_TRAILING_AMOUNT_PROXY_NOT_A_GUARANTEE",
            "CORRELATION_CLUSTERING_USES_A_DETERMINISTIC_RANK_SKETCH",
            "NO_INDUSTRY_NEUTRALIZATION_IN_R0",
            "TERMINAL_DELIST_INVALIDATION_MASK_UNAVAILABLE__MISSING_OUTCOMES_NOT_IMPUTED",
        ],
        "timing": {
            "load_labels_shared_inputs_seconds": load_seconds,
            "factor_loop_seconds": time.perf_counter() - factor_t0,
            "total_seconds": total_seconds,
        },
        "validation_consumed": False,
        "holdout_opened": False,
        "automatic_trading": False,
    }
    atomic_json(stage / "summary.json", summary)
    finished_at = datetime.now(timezone.utc).isoformat()
    artifact_names = ("factors.jsonl", "summary.json", "correlation_clusters.json")
    manifest = {
        "schema_version": 1,
        "run_id": args.run_id,
        "status": summary["status"],
        "started_at_utc": running["started_at_utc"],
        "finished_at_utc": finished_at,
        "dataset": config["dataset"],
        "config_path": str(CONFIG.relative_to(ROOT)).replace("\\", "/"),
        "config_sha256": config_sha,
        "catalog_path": str(CATALOG.relative_to(ROOT)).replace("\\", "/"),
        "catalog_sha256": catalog_sha,
        "catalog_content_sha256": catalog.get("catalog_content_sha256"),
        "factor_count": len(records),
        "run_kind": intended["run_kind"],
        "permutations": permutations,
        "accelerator": runtime_accelerator,
        "runtime": {
            "python": sys.version,
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "code_sha256": code_sha256,
        "artifacts": {name: sha256_file(stage / name) for name in artifact_names},
        "checkpoint_tree_sha256": _tree_hash(store.checkpoints.glob("*.json"), stage),
        "correlation_sketch_tree_sha256": _tree_hash(store.sketches.glob("*.npy"), stage),
        "validation_consumed": False,
        "holdout_opened": False,
        "automatic_trading": False,
        "tradable_conclusion": False,
    }
    atomic_json(stage / "manifest.json", manifest)
    atomic_json(
        stage / "COMMITTED.json",
        {
            "status": "COMMITTED",
            "run_id": args.run_id,
            "manifest_sha256": sha256_file(stage / "manifest.json"),
            "committed_at_utc": finished_at,
            "validation_consumed": False,
            "holdout_opened": False,
        },
    )
    (stage / "RUNNING.json").unlink(missing_ok=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage, final)
    print(
        json.dumps(
            {
                "status": (
                    "COMMITTED__ENGINEERING_SMOKE_ONLY"
                    if args.smoke_only
                    else "COMMITTED__DISCOVERY_ONLY"
                ),
                "run_id": args.run_id,
                "path": str(final),
                "mechanical_completed": len(successful),
                "mechanical_failed": len(records) - len(successful),
                "statistical_by_fdr_0_10": len(fdr),
                "cost_30bps_descriptive_survivors": len(cost_survivors),
                "action_safe_cost_30bps_descriptive_survivors": len(action_safe_cost_survivors),
                "tradable_conclusions": 0,
                "total_seconds": total_seconds,
                "validation_consumed": False,
                "holdout_opened": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    preview = build_parser().parse_args(argv)
    if preview.contract_only:
        return _main_unlocked(argv)
    if not RUN_ID.fullmatch(preview.run_id):
        raise SystemExit("invalid run id")
    lock_path = PROJECT / ".tmp/r0-classic-discovery/.locks" / f"{preview.run_id}.lock"
    with RunLock(lock_path):
        return _main_unlocked(argv)


if __name__ == "__main__":
    raise SystemExit(main())
