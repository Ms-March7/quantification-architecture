#!/usr/bin/env python3
"""Build the deterministic 512-candidate R0_SHSZ_PV factor catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from factor_factory.generation import FactorGenerator  # noqa: E402


CONFIG = PROJECT / "configs/r0_shsz_pv_single_factor_screen_v1.json"
DEFAULT_OUTPUT = PROJECT / "configs/r0_shsz_pv_factor_catalog_512_v1.json"


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def family(formula: str) -> str:
    if "ts_std" in formula:
        return "volatility"
    if "ts_corr" in formula:
        return "price_volume_correlation"
    if "$amount" in formula:
        return "turnover_amount"
    if "$volume" in formula:
        return "volume"
    if "ts_min" in formula or "ts_max" in formula:
        return "price_position"
    if "delta($close" in formula:
        return "momentum_or_reversal"
    if "$open" in formula:
        return "intraday_or_overnight"
    return "composite_price_volume"


def build() -> dict[str, object]:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    specification = config["factor_catalog"]
    generator = FactorGenerator(
        specification["fields"], seed=int(specification["seed"]),
        max_depth=int(specification["max_depth"]), required_output_unit=(0, 0),
    )
    candidates = generator.initial_population(int(specification["candidate_count"]))
    rows = [
        {
            "candidate_id": item.candidate_id,
            "formula": item.formula,
            "origin": item.origin,
            "generation": item.generation,
            "family": family(item.formula),
            "causal_contract": "TRADE_DATE_D_CLOSE_KNOWN_AT_NEXT_SESSION_OPEN",
        }
        for item in candidates
    ]
    if len({item["candidate_id"] for item in rows}) != len(rows):
        raise RuntimeError("factor catalog contains duplicate candidate ids")
    return {
        "schema_version": 1,
        "catalog_id": specification["catalog_id"],
        "status": "FROZEN_CATALOG__NOT_EVALUATED",
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_id": config["protocol_id"],
        "protocol_sha256": sha256_file(CONFIG),
        "engine_sha256": sha256_file(PROJECT / "factor_factory/generation.py"),
        "candidate_count": len(rows),
        "seed_count": sum(item["origin"] == "seed" for item in rows),
        "grammar_count": sum(item["origin"] == "grammar" for item in rows),
        "seed": specification["seed"],
        "max_depth": specification["max_depth"],
        "fields": specification["fields"],
        "catalog_content_hash": hashlib.sha256(canonical_bytes(rows)).hexdigest(),
        "candidates": rows,
        "factor_runs": 0,
        "validation_consumed": False,
        "holdout_opened": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build()
    atomic_write(args.output, result)
    print(json.dumps({key: result[key] for key in ("status", "catalog_id", "candidate_count", "seed_count", "grammar_count", "catalog_content_hash")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
