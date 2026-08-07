#!/usr/bin/env python3
"""Compute ten full-market factor arrays and persist metrics only.

This is an engineering smoke.  It does not create labels, IC, portfolios,
returns, a backtest, Validation/Holdout state, or a reusable factor-value file.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_factory.expressions import Expression, evaluate_expression  # noqa: E402
from factor_factory.r0_shsz_panel_input import R0SHSZPVPanelAdapter  # noqa: E402


def f(name: str) -> Expression:
    return Expression.terminal(name)


def u(op: str, child: Expression, window: int | None = None) -> Expression:
    return Expression(op, (child,), window=window)


def b(op: str, left: Expression, right: Expression) -> Expression:
    return Expression(op, (left, right))


def expressions() -> dict[str, Expression]:
    close, open_, high, low, volume = f("close"), f("open"), f("high"), f("low"), f("volume")
    delay_close_1 = u("delay", close, 1)
    overnight = b("safe_div", b("sub", open_, delay_close_1), delay_close_1)
    return {
        "local_intraday_return": b("safe_div", b("sub", close, open_), open_),
        "rq_intraday_location": b("safe_div", b("sub", close, open_), b("sub", high, low)),
        "local_overnight_gap": overnight,
        "jq_alpha191_015": overnight,
        "local_close_momentum_5": b("safe_div", u("delta", close, 5), u("delay", close, 5)),
        "jq_alpha191_018": b("safe_div", close, u("delay", close, 5)),
        "local_close_volatility_20": u(
            "ts_std", b("safe_div", u("delta", close, 1), u("delay", close, 1)), 20
        ),
        "local_volume_ratio_5_20": b("safe_div", u("ts_mean", volume, 5), u("ts_mean", volume, 20)),
        "local_price_position_20": b(
            "safe_div",
            b("sub", close, u("ts_min", close, 20)),
            b("sub", u("ts_max", close, 20), u("ts_min", close, 20)),
        ),
        "jq_alpha101_006": u("neg", Expression("ts_corr", (open_, volume), window=10)),
    }


def strict_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("config must be one JSON object")
    return value


def write_new_json(path: Path, value: dict[str, object]) -> None:
    if path.exists():
        raise FileExistsError(f"immutable receipt already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, default=PROJECT_ROOT.parent)
    args = parser.parse_args()

    config = strict_json(args.config)
    configured = {str(row["factor_id"]): str(row["formula"]) for row in config["factors"]}
    trees = expressions()
    canonical = {name: tree.canonical() for name, tree in trees.items()}
    if configured != canonical:
        raise ValueError("config formulas do not exactly match executable expression trees")

    started = time.perf_counter()
    panel = R0SHSZPVPanelAdapter(workspace_root=args.workspace_root).load(
        start_date=config["date_range"][0],
        end_date=config["date_range"][1],
        symbols=None,
        max_panel_cells=int(config["max_panel_cells"]),
    )
    loaded = time.perf_counter()
    metrics: list[dict[str, object]] = []
    arrays: dict[str, np.ndarray] = {}
    shared_cache: dict[str, np.ndarray] = {}
    for name, tree in trees.items():
        factor_started = time.perf_counter()
        values = evaluate_expression(tree, panel.fields, cache=shared_cache, accelerator="cpu")
        arrays[name] = values
        finite = np.isfinite(values)
        latest_finite = finite[-1]
        metrics.append(
            {
                "factor_id": name,
                "formula": tree.canonical(),
                "shape": list(values.shape),
                "finite_cells": int(np.count_nonzero(finite)),
                "finite_rate": float(np.mean(finite)),
                "latest_finite_symbols": int(np.count_nonzero(latest_finite)),
                "latest_finite_rate": float(np.mean(latest_finite)),
                "elapsed_seconds": round(time.perf_counter() - factor_started, 6),
            }
        )

    equivalence: list[dict[str, object]] = []
    for left, right in config["exact_equivalence_checks"]:
        a, b_value = arrays[left], arrays[right]
        both = np.isfinite(a) & np.isfinite(b_value)
        max_error = float(np.max(np.abs(a[both] - b_value[both]))) if np.any(both) else None
        equivalence.append(
            {
                "left": left,
                "right": right,
                "finite_overlap_cells": int(np.count_nonzero(both)),
                "max_absolute_error": max_error,
                "exact_equal_with_nan": bool(np.array_equal(a, b_value, equal_nan=True)),
            }
        )

    config_sha256 = hashlib.sha256(args.config.read_bytes()).hexdigest()
    receipt: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": "r0-full-market-factor-engineering-smoke-10-20260805-v1",
        "status": "PASS__TEN_FACTOR_FULL_MARKET_ENGINEERING_SMOKE__NO_RESEARCH_RUN",
        "executed_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "config": str(args.config.resolve(strict=True)),
        "config_sha256": config_sha256,
        "research_input": f"{panel.metadata['dataset_id']}@{panel.metadata['snapshot_id']}",
        "content_hash": panel.metadata["content_hash"],
        "panel_shape": list(panel.shape),
        "date_start": panel.dates[0],
        "date_end": panel.dates[-1],
        "date_count": len(panel.dates),
        "symbol_count": len(panel.symbols),
        "fields": list(panel.fields),
        "load_seconds": round(loaded - started, 6),
        "compute_seconds": round(time.perf_counter() - loaded, 6),
        "factors": metrics,
        "equivalence_checks": equivalence,
        "factor_value_rows_persisted": 0,
        "factor_value_arrays_persisted": 0,
        "factor_runs": 10,
        "ic_runs": 0,
        "group_return_runs": 0,
        "backtest_runs": 0,
        "validation_consumed": False,
        "holdout_opened": False,
        "limitations": [
            "UNADJUSTED_PRICE_ENGINEERING_ONLY__CORPORATE_ACTION_FEATURES_FORBIDDEN",
            "NO_LABELS_OR_ECONOMIC_EVALUATION",
            "EXECUTION_MASKS_NOT_USED_AS_SIGNAL_FIELDS",
            "NO_FACTOR_VALUES_PERSISTED",
        ],
    }
    write_new_json(args.receipt.resolve(strict=False), receipt)
    print(json.dumps({k: receipt[k] for k in ("status", "panel_shape", "factor_runs", "ic_runs", "holdout_opened")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
