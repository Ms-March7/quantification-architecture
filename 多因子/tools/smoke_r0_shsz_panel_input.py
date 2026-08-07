#!/usr/bin/env python3
"""Read-only bounded smoke for the exact formal R0_SHSZ_PV snapshot."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_factory.r0_shsz_panel_input import (  # noqa: E402
    R0SHSZPVPanelAdapter,
    R0SHSZPanelInputError,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--start-date", required=True)
    value.add_argument("--end-date", required=True)
    value.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated exact RQData order_book_ids, e.g. 603986.XSHG,000001.XSHE",
    )
    value.add_argument("--max-panel-cells", type=int, default=100_000)
    value.add_argument(
        "--workspace-root",
        type=Path,
        default=PROJECT_ROOT.parent,
    )
    value.add_argument(
        "--receipt",
        type=Path,
        help="Optional new JSON receipt path; it must be under <workspace>/reports and cannot overwrite",
    )
    return value


def write_receipt(path: Path, workspace_root: Path, value: dict[str, object]) -> None:
    reports = (workspace_root / "reports").resolve(strict=False)
    target = path if path.is_absolute() else workspace_root / path
    target = target.resolve(strict=False)
    try:
        if Path(os.path.commonpath((str(reports), str(target)))) != reports:
            raise ValueError
    except ValueError as error:
        raise R0SHSZPanelInputError("receipt path must remain under workspace reports/") from error
    if target.exists():
        raise R0SHSZPanelInputError("receipt path already exists; immutable smoke receipts cannot overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    args = parser().parse_args()
    symbols = tuple(value.strip() for value in args.symbols.split(",") if value.strip())
    try:
        panel = R0SHSZPVPanelAdapter(
            workspace_root=args.workspace_root
        ).load(
            start_date=args.start_date,
            end_date=args.end_date,
            symbols=symbols,
            max_panel_cells=args.max_panel_cells,
        )
    except R0SHSZPanelInputError as error:
        print(
            json.dumps(
                {
                    "verdict": "FAIL_CLOSED",
                    "error": str(error),
                    "factor_runs": 0,
                    "ic_runs": 0,
                    "backtest_runs": 0,
                    "validation_consumed": False,
                    "holdout_opened": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    missing = {
        name: int(np.count_nonzero(~np.isfinite(values)))
        for name, values in panel.fields.items()
    }
    result: dict[str, object] = {
        "schema_version": 1,
        "verdict": "PASS__BOUNDED_PANELDATA_SMOKE_ONLY",
        "identity": (
            f"{panel.metadata['dataset_id']}@{panel.metadata['snapshot_id']}"
        ),
        "content_hash": panel.metadata["content_hash"],
        "executed_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "dates": list(panel.dates),
        "symbols": list(panel.symbols),
        "shape": list(panel.shape),
        "fields": list(panel.fields),
        "missing_cells_by_field": missing,
        "eligible_cells": int(np.count_nonzero(panel.eligible)),
        "buyable_open_cells": int(np.count_nonzero(panel.buyable)),
        "sellable_open_cells": int(np.count_nonzero(panel.sellable)),
        "available_at_by_trade_date": panel.metadata[
            "available_at_by_trade_date"
        ],
        "signal_lag_sessions_minimum": panel.metadata[
            "signal_lag_sessions_minimum"
        ],
        "execution_mask_role": "EX_POST_SIMULATION_ONLY__NEVER_SIGNAL_FIELD",
        "factor_runs": 0,
        "ic_runs": 0,
        "backtest_runs": 0,
        "validation_consumed": False,
        "holdout_opened": False,
        "writes_performed": 1 if args.receipt is not None else 0,
    }
    if args.receipt is not None:
        try:
            write_receipt(args.receipt, args.workspace_root.resolve(strict=False), result)
        except R0SHSZPanelInputError as error:
            print(json.dumps({"verdict": "FAIL_CLOSED", "error": str(error)}, ensure_ascii=False, indent=2))
            return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
