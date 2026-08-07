from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_BASE = ROOT / "data/market/quarantine/ricequant-r0-core04-core05"
DEFAULT_OUTPUT = CANDIDATE_BASE / "candidate-20260804-v1"
TARGET_DATES = ("2026-07-29", "2026-07-30", "2026-07-31", "2026-08-03")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RuntimeError(f"missing aggregate input: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def build_payload(
    factors: Iterable[dict[str, Any]], actions: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    selected_factors = sorted(
        (
            {
                "event_date": row["event_date"],
                "security_id": row["security_id"],
                "ex_factor": float(row["ex_factor"]),
                "ex_cum_factor": (
                    None if row.get("ex_cum_factor") is None else float(row["ex_cum_factor"])
                ),
                "registration_date": row.get("registration_date"),
                "source_row_sha256": row["source_row_sha256"],
            }
            for row in factors
            if row.get("event_date") in TARGET_DATES
        ),
        key=lambda row: (row["event_date"], row["security_id"]),
    )
    selected_actions = [row for row in actions if row.get("event_date") in TARGET_DATES]
    dates: dict[str, Any] = {}
    for target in TARGET_DATES:
        factor_rows = [row for row in selected_factors if row["event_date"] == target]
        action_rows = [row for row in selected_actions if row.get("event_date") == target]
        action_counts = Counter(str(row.get("event_type")) for row in action_rows)
        factors_numeric = [row["ex_factor"] for row in factor_rows]
        dates[target] = {
            "factor_row_count": len(factor_rows),
            "security_count": len({row["security_id"] for row in factor_rows}),
            "action_type_counts": dict(sorted(action_counts.items())),
            "min_ex_factor": min(factors_numeric) if factors_numeric else None,
            "max_ex_factor": max(factors_numeric) if factors_numeric else None,
            "close_prev_close_differs_from_action_adjusted_return": bool(factor_rows),
        }
    if any(not math.isfinite(row["ex_factor"]) or row["ex_factor"] <= 0 for row in selected_factors):
        raise RuntimeError("tail calibration contains an invalid ex_factor")
    return {
        "schema_version": 1,
        "receipt_id": "ricequant-tail-return-calibration-20260804-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose_ceiling": "INSPECTION_ONLY",
        "target_dates": list(TARGET_DATES),
        "dates": dates,
        "factor_rows": selected_factors,
        "factor_row_count": len(selected_factors),
        "formula_candidate": "action_adjusted_return = close_t * ex_factor_t / prev_close_t - 1",
        "formula_role": "RQDATA_VENDOR_CALIBRATION_ONLY__NOT_FORMAL_CORE05",
        "security_603986_has_factor_on_target_dates": any(
            row["security_id"] == "603986.XSHG" for row in selected_factors
        ),
        "scope_limit": "COMMON_STOCK_XSHG_XSHE_ONLY__ETF_AND_REIT_NOT_COVERED",
        "raw_api_payload_persisted": False,
        "credential_value_captured": False,
        "formal_effect": "NONE",
        "research_eligible": False,
    }


def run(output_root: Path) -> dict[str, Any]:
    output_root = Path(os.path.abspath(output_root))
    base = Path(os.path.abspath(CANDIDATE_BASE))
    try:
        relative = output_root.relative_to(base)
    except ValueError as exc:
        raise RuntimeError("output root escapes the Ricequant quarantine root") from exc
    if len(relative.parts) != 1:
        raise RuntimeError("output root must be one explicit candidate directory")
    factor_path = output_root / "aggregate/EX_FACTORS_TAIL_2025_20260803.jsonl"
    action_path = output_root / "aggregate/ACTION_EVENTS_TAIL_2025_20260803.jsonl"
    payload = build_payload(load_jsonl(factor_path), load_jsonl(action_path))
    payload["input_bindings"] = {
        factor_path.relative_to(ROOT).as_posix(): {
            "bytes": factor_path.stat().st_size,
            "sha256": sha256_file(factor_path),
        },
        action_path.relative_to(ROOT).as_posix(): {
            "bytes": action_path.stat().st_size,
            "sha256": sha256_file(action_path),
        },
    }
    result_path = output_root / "TAIL_RETURN_CALIBRATION.json"
    write_json_atomic(result_path, payload)
    manifest = {
        "schema_version": 1,
        "purpose_ceiling": "INSPECTION_ONLY",
        "entry": {
            "path": result_path.relative_to(ROOT).as_posix(),
            "bytes": result_path.stat().st_size,
            "sha256": sha256_file(result_path),
        },
        "formal_effect": "NONE",
        "research_eligible": False,
    }
    write_json_atomic(output_root / "TAIL_RETURN_CALIBRATION_MANIFEST.json", manifest)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = run(args.output_root)
    print(
        canonical_json(
            {
                "status": "TAIL_RETURN_CALIBRATION_PASS_INSPECTION_ONLY",
                "factor_row_count": payload["factor_row_count"],
                "dates": payload["dates"],
                "security_603986_has_factor_on_target_dates": payload[
                    "security_603986_has_factor_on_target_dates"
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
