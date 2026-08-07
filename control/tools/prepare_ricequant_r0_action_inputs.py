from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_BASE = (
    ROOT / "data" / "market" / "quarantine" / "ricequant-r0-core04-core05"
)
DEFAULT_OUTPUT = CANDIDATE_BASE / "candidate-20260804-v1"
EVENT_INPUT = ROOT / (
    "data/market/quarantine/r0-core04-core05-split/candidate-20260801-v1/"
    "CORE04/corporate_action_event/source_observations.parquet"
)
FACTOR_INPUT = ROOT / (
    "data/market/quarantine/r0-core04-core05-split/candidate-20260801-v1/"
    "CORE04/corporate_action_adjustment_event/vendor_factor_observations.parquet"
)


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
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def code_to_order_book_id(code: str) -> str | None:
    code = str(code).strip()
    if len(code) != 6 or not code.isdigit():
        return None
    if code.startswith(("600", "601", "603", "605", "606", "688", "689")):
        return f"{code}.XSHG"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return f"{code}.XSHE"
    return None


def validate_output_root(path: Path) -> Path:
    base = Path(os.path.abspath(CANDIDATE_BASE))
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise RuntimeError("output root must stay below the Ricequant quarantine root") from exc
    if len(relative.parts) != 1 or not relative.name.startswith("candidate-"):
        raise RuntimeError("output root must be one explicit candidate directory")
    return target


def build_universe(event_input: Path, factor_input: Path) -> dict[str, Any]:
    import duckdb

    if not event_input.is_file() or not factor_input.is_file():
        raise RuntimeError("explicit CSMAR comparison inputs are missing")
    connection = duckdb.connect()
    try:
        event_codes = connection.execute(
            "SELECT DISTINCT local_security_code FROM read_parquet(?) "
            "WHERE source_id = 'CSMAR_MANUAL_EXPORT'",
            [event_input.as_posix()],
        ).fetchall()
        factor_codes = connection.execute(
            "SELECT DISTINCT local_security_code FROM read_parquet(?)",
            [factor_input.as_posix()],
        ).fetchall()
    finally:
        connection.close()
    raw_codes = {str(item[0]) for item in event_codes + factor_codes if item[0] is not None}
    order_book_ids = sorted(
        item for code in raw_codes if (item := code_to_order_book_id(code)) is not None
    )
    exchange_counts = {
        "XSHG": sum(item.endswith(".XSHG") for item in order_book_ids),
        "XSHE": sum(item.endswith(".XSHE") for item in order_book_ids),
    }
    return {
        "schema_version": 1,
        "contract_id": "ricequant-r0-core04-core05-candidate-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose_ceiling": "INSPECTION_ONLY",
        "universe_role": "CSMAR_CORE04_CORE05_EXPLICIT_SECURITY_UNION__NOT_FORMAL_MASTER",
        "order_book_ids": order_book_ids,
        "order_book_id_count": len(order_book_ids),
        "exchange_counts": exchange_counts,
        "order_book_ids_sha256": hashlib.sha256(
            ("\n".join(order_book_ids) + "\n").encode("ascii")
        ).hexdigest(),
        "input_bindings": {
            "csmar_action_event": {
                "path": event_input.relative_to(ROOT).as_posix(),
                "bytes": event_input.stat().st_size,
                "sha256": sha256_file(event_input),
            },
            "csmar_vendor_factor": {
                "path": factor_input.relative_to(ROOT).as_posix(),
                "bytes": factor_input.stat().st_size,
                "sha256": sha256_file(factor_input),
            },
        },
        "formal_catalog_write": False,
        "quality_promotion": False,
        "research_eligible": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output_root = validate_output_root(args.output_root)
    output_path = output_root / "INPUT_UNIVERSE.json"
    if output_path.exists():
        raise RuntimeError("INPUT_UNIVERSE.json already exists; overwrite is forbidden")
    payload = build_universe(EVENT_INPUT, FACTOR_INPUT)
    write_json_atomic(output_path, payload)
    print(
        canonical_json(
            {
                "status": "INPUT_UNIVERSE_PREPARED_INSPECTION_ONLY",
                "order_book_id_count": payload["order_book_id_count"],
                "exchange_counts": payload["exchange_counts"],
                "output": output_path.relative_to(ROOT).as_posix(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
