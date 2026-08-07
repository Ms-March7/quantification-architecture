from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_BASE = ROOT / "data/market/quarantine/ricequant-r0-core04-core05"
DEFAULT_OUTPUT = CANDIDATE_BASE / "candidate-20260804-v1"
CSMAR_EVENT = ROOT / (
    "data/market/quarantine/r0-core04-core05-split/candidate-20260801-v1/"
    "CORE04/corporate_action_event/source_observations.parquet"
)
CSMAR_TERM = ROOT / (
    "data/market/quarantine/r0-core04-core05-split/candidate-20260801-v1/"
    "CORE04/corporate_action_terms/source_observations.parquet"
)
CSMAR_FACTOR = ROOT / (
    "data/market/quarantine/r0-core04-core05-split/candidate-20260801-v1/"
    "CORE04/corporate_action_adjustment_event/vendor_factor_observations.parquet"
)
SEGMENTS = {
    "BASE_2005_2024": ("2005-01-01", "2024-12-31"),
    "TAIL_2025_20260803": ("2025-01-01", "2026-08-03"),
    "TOTAL_2005_20260803": ("2005-01-01", "2026-08-03"),
}


class ReconcileError(RuntimeError):
    pass


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


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_bytes_atomic(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
    )


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    payload = b"".join((canonical_json(row) + "\n").encode("utf-8") for row in rows)
    write_bytes_atomic(path, payload)


def validate_output_root(path: Path) -> Path:
    base = Path(os.path.abspath(CANDIDATE_BASE))
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise ReconcileError("output root must stay below the Ricequant quarantine root") from exc
    if len(relative.parts) != 1 or not relative.name.startswith("candidate-"):
        raise ReconcileError("output root must be one explicit candidate directory")
    return target


def load_jsonl(paths: Iterable[Path]):
    import pandas as pd

    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            raise ReconcileError(f"missing normalized RQData input: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                rows.append(json.loads(line))
    return pd.DataFrame(rows)


def load_csmar_inputs():
    import duckdb

    for path in (CSMAR_EVENT, CSMAR_TERM, CSMAR_FACTOR):
        if not path.is_file():
            raise ReconcileError(f"missing explicit CSMAR input: {path}")
    connection = duckdb.connect()
    try:
        events = connection.execute(
            """
            SELECT
              e.local_security_code,
              CAST(e.event_date_candidate AS VARCHAR) AS event_date,
              e.action_type_candidate,
              CAST(t.cash_gross_cny_per_source_share AS DOUBLE) AS csmar_cash_per_share,
              CAST(t.bonus_shares_per_source_share AS DOUBLE) AS csmar_bonus_shares,
              CAST(t.split_increment_shares_per_source_share AS DOUBLE) AS csmar_split_increment,
              CAST(t.rights_shares_per_source_share AS DOUBLE) AS csmar_rights_proportion,
              CAST(t.rights_price_cny_per_share AS DOUBLE) AS csmar_rights_price
            FROM read_parquet(?) e
            LEFT JOIN read_parquet(?) t
              ON e.source_observation_id = t.event_source_observation_id
            WHERE e.source_id = 'CSMAR_MANUAL_EXPORT'
              AND e.event_date_candidate BETWEEN DATE '2005-01-01' AND DATE '2026-08-03'
            """,
            [CSMAR_EVENT.as_posix(), CSMAR_TERM.as_posix()],
        ).fetchdf()
        factors = connection.execute(
            """
            SELECT
              local_security_code,
              CAST(ex_date_candidate AS VARCHAR) AS event_date,
              CAST(vendor_forward_event_price_multiplier AS DOUBLE) AS csmar_forward_factor,
              CAST(vendor_backward_event_price_multiplier AS DOUBLE) AS csmar_backward_factor
            FROM read_parquet(?)
            WHERE ex_date_candidate BETWEEN DATE '2005-01-01' AND DATE '2026-08-03'
            """,
            [CSMAR_FACTOR.as_posix()],
        ).fetchdf()
    finally:
        connection.close()
    return events, factors


def _mapped_csmar_type(value: Any) -> str | None:
    mapping = {
        "CASH_DIVIDEND": "DIVIDEND",
        "BONUS_SHARE": "SPLIT",
        "SPLIT_OR_SHARE_DISTRIBUTION_CANDIDATE": "SPLIT",
        "RIGHTS": "ALLOTMENT",
    }
    return mapping.get(str(value))


def _key_set(frame: Any, fields: list[str]) -> set[tuple[str, ...]]:
    if frame.empty:
        return set()
    return {
        tuple(str(row[field]) for field in fields)
        for _, row in frame[fields].drop_duplicates().iterrows()
    }


def _sample_keys(keys: Iterable[tuple[str, ...]], label: str, segment: str) -> list[dict[str, Any]]:
    return [
        {
            "segment": segment,
            "sample_class": label,
            "local_security_code": key[0],
            "event_date": key[1],
            **({"event_type": key[2]} if len(key) > 2 else {}),
        }
        for key in sorted(keys)[:25]
    ]


def _safe_sum(series: Any) -> float | None:
    values = series.dropna()
    if values.empty:
        return None
    result = float(values.sum())
    return result if math.isfinite(result) else None


def reconcile_actions(rq_actions: Any, cs_actions: Any, segment: str, start: str, end: str):
    import pandas as pd

    rq = rq_actions.loc[
        rq_actions["event_date"].astype(str).between(start, end)
    ].copy()
    cs = cs_actions.loc[cs_actions["event_date"].astype(str).between(start, end)].copy()
    cs["event_type"] = cs["action_type_candidate"].map(_mapped_csmar_type)
    cs = cs.loc[cs["event_type"].notna()].copy()
    broad_fields = ["local_security_code", "event_date"]
    exact_fields = broad_fields + ["event_type"]
    rq_broad = _key_set(rq, broad_fields)
    cs_broad = _key_set(cs, broad_fields)
    rq_exact = _key_set(rq, exact_fields)
    cs_exact = _key_set(cs, exact_fields)

    rq_types = (
        rq.groupby(broad_fields)["event_type"]
        .agg(lambda values: "|".join(sorted(set(map(str, values)))))
        .rename("rq_types")
        .reset_index()
    )
    cs_types = (
        cs.groupby(broad_fields)["event_type"]
        .agg(lambda values: "|".join(sorted(set(map(str, values)))))
        .rename("csmar_types")
        .reset_index()
    )
    type_join = rq_types.merge(cs_types, on=broad_fields, how="inner")
    type_conflicts = int((type_join["rq_types"] != type_join["csmar_types"]).sum())

    term_fields = {
        "DIVIDEND": ("cash_per_share", "csmar_cash_per_share", 1e-6),
        "SPLIT": ("split_increment", "csmar_split_total", 1e-6),
        # CSMAR's normalized rights ratio aligns with the actually allotted
        # proportion rather than RQData's initially announced proportion.
        "ALLOTMENT": ("allotted_proportion", "csmar_rights_proportion", 1e-5),
    }
    cs["csmar_split_total"] = cs.apply(
        lambda row: sum(
            value
            for value in (
                row.get("csmar_bonus_shares"),
                row.get("csmar_split_increment"),
            )
            if pd.notna(value)
        )
        if pd.notna(row.get("csmar_bonus_shares"))
        or pd.notna(row.get("csmar_split_increment"))
        else None,
        axis=1,
    )
    term_summary: dict[str, Any] = {}
    for event_type, (rq_field, cs_field, tolerance) in term_fields.items():
        rq_part = rq.loc[rq["event_type"] == event_type]
        cs_part = cs.loc[cs["event_type"] == event_type]
        rq_agg = (
            rq_part.groupby(exact_fields, dropna=False)[rq_field]
            .agg(_safe_sum)
            .rename("rq_value")
            .reset_index()
        )
        cs_agg = (
            cs_part.groupby(exact_fields, dropna=False)[cs_field]
            .agg(_safe_sum)
            .rename("csmar_value")
            .reset_index()
        )
        joined = rq_agg.merge(cs_agg, on=exact_fields, how="inner")
        comparable = joined.loc[joined["rq_value"].notna() & joined["csmar_value"].notna()].copy()
        if comparable.empty:
            matched = conflicts = 0
            max_abs_delta = None
        else:
            deltas = (comparable["rq_value"] - comparable["csmar_value"]).abs()
            matched = int((deltas <= tolerance).sum())
            conflicts = int((deltas > tolerance).sum())
            max_abs_delta = float(deltas.max())
        term_summary[event_type] = {
            "matched_exact_type_keys": int(len(joined)),
            "comparable_numeric_keys": int(len(comparable)),
            "numeric_matches": matched,
            "numeric_conflicts": conflicts,
            "missing_or_noncomparable_terms": int(len(joined) - len(comparable)),
            "absolute_tolerance": tolerance,
            "max_absolute_delta": max_abs_delta,
        }
    result = {
        "rqdata_rows": int(len(rq)),
        "csmar_rows_in_comparable_types": int(len(cs)),
        "rqdata_security_event_keys": len(rq_broad),
        "csmar_security_event_keys": len(cs_broad),
        "matched_security_event_keys": len(rq_broad & cs_broad),
        "rqdata_only_security_event_keys": len(rq_broad - cs_broad),
        "csmar_only_security_event_keys": len(cs_broad - rq_broad),
        "matched_security_event_type_keys": len(rq_exact & cs_exact),
        "rqdata_only_security_event_type_keys": len(rq_exact - cs_exact),
        "csmar_only_security_event_type_keys": len(cs_exact - rq_exact),
        "type_set_conflicts_on_matched_keys": type_conflicts,
        "rqdata_duplicate_exact_type_rows": int(rq.duplicated(exact_fields, keep=False).sum()),
        "csmar_duplicate_exact_type_rows": int(cs.duplicated(exact_fields, keep=False).sum()),
        "term_reconciliation": term_summary,
    }
    samples = []
    samples += _sample_keys(rq_broad - cs_broad, "RQDATA_ONLY_ACTION_KEY", segment)
    samples += _sample_keys(cs_broad - rq_broad, "CSMAR_ONLY_ACTION_KEY", segment)
    return result, samples


def reconcile_factors(rq_factors: Any, cs_factors: Any, segment: str, start: str, end: str):
    key_fields = ["local_security_code", "event_date"]
    rq = rq_factors.loc[rq_factors["event_date"].astype(str).between(start, end)].copy()
    cs = cs_factors.loc[cs_factors["event_date"].astype(str).between(start, end)].copy()
    rq_keys = _key_set(rq, key_fields)
    cs_keys = _key_set(cs, key_fields)
    rq_counts = rq.groupby(key_fields).size().rename("rq_count").reset_index()
    cs_counts = cs.groupby(key_fields).size().rename("csmar_count").reset_index()
    rq_unique = rq.merge(rq_counts.loc[rq_counts["rq_count"] == 1, key_fields], on=key_fields)
    cs_unique = cs.merge(cs_counts.loc[cs_counts["csmar_count"] == 1, key_fields], on=key_fields)
    joined = rq_unique[key_fields + ["ex_factor"]].merge(
        cs_unique[key_fields + ["csmar_backward_factor"]], on=key_fields, how="inner"
    )
    comparable = joined.loc[
        joined["ex_factor"].notna() & joined["csmar_backward_factor"].notna()
    ].copy()
    # RQData's event ex_factor is the >1 backward event multiplier in its own
    # documented examples.  CSMAR's BwardFactor is the reciprocal of its
    # FwardFactor and is stored at six decimal places, hence a 1e-5 tolerance.
    tolerance = 1e-5
    if comparable.empty:
        matches = conflicts = 0
        max_delta = None
        delta_quantiles = {}
    else:
        deltas = (
            comparable["ex_factor"] - comparable["csmar_backward_factor"]
        ).abs()
        matches = int((deltas <= tolerance).sum())
        conflicts = int((deltas > tolerance).sum())
        max_delta = float(deltas.max())
        delta_quantiles = {
            "p50": float(deltas.quantile(0.50)),
            "p90": float(deltas.quantile(0.90)),
            "p99": float(deltas.quantile(0.99)),
        }
    result = {
        "rqdata_rows": int(len(rq)),
        "csmar_rows": int(len(cs)),
        "rqdata_security_event_keys": len(rq_keys),
        "csmar_security_event_keys": len(cs_keys),
        "matched_security_event_keys": len(rq_keys & cs_keys),
        "rqdata_only_security_event_keys": len(rq_keys - cs_keys),
        "csmar_only_security_event_keys": len(cs_keys - rq_keys),
        "rqdata_duplicate_key_rows": int(rq.duplicated(key_fields, keep=False).sum()),
        "csmar_duplicate_key_rows": int(cs.duplicated(key_fields, keep=False).sum()),
        "comparable_unique_keys": int(len(comparable)),
        "numeric_matches": matches,
        "numeric_conflicts": conflicts,
        "absolute_tolerance": tolerance,
        "max_absolute_delta": max_delta,
        "absolute_delta_quantiles": delta_quantiles,
        "comparison_semantics": "RQDATA_EX_FACTOR_VS_CSMAR_BACKWARD_EVENT_PRICE_MULTIPLIER",
    }
    samples = []
    samples += _sample_keys(rq_keys - cs_keys, "RQDATA_ONLY_FACTOR_KEY", segment)
    samples += _sample_keys(cs_keys - rq_keys, "CSMAR_ONLY_FACTOR_KEY", segment)
    return result, samples


def run(output_root: Path) -> dict[str, Any]:
    output_root = validate_output_root(output_root)
    aggregate = output_root / "aggregate"
    rq_actions = load_jsonl(
        [
            aggregate / "ACTION_EVENTS_BASE_2005_2024.jsonl",
            aggregate / "ACTION_EVENTS_TAIL_2025_20260803.jsonl",
        ]
    )
    rq_factors = load_jsonl(
        [
            aggregate / "EX_FACTORS_BASE_2005_2024.jsonl",
            aggregate / "EX_FACTORS_TAIL_2025_20260803.jsonl",
        ]
    )
    required_action = {
        "local_security_code",
        "event_date",
        "event_type",
        "cash_per_share",
        "split_increment",
        "allotment_proportion",
        "allotted_proportion",
    }
    required_factor = {"local_security_code", "event_date", "ex_factor"}
    if not required_action.issubset(rq_actions.columns):
        raise ReconcileError("RQData action aggregate is missing normalized fields")
    if not required_factor.issubset(rq_factors.columns):
        raise ReconcileError("RQData factor aggregate is missing normalized fields")
    cs_actions, cs_factors = load_csmar_inputs()
    result: dict[str, Any] = {
        "schema_version": 1,
        "contract_id": "ricequant-r0-core04-core05-candidate-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "RECONCILED_WITH_CONFLICTS_AND_UNMATCHED__INSPECTION_ONLY",
        "purpose_ceiling": "INSPECTION_ONLY",
        "segments": {},
        "canonical_source_selected": False,
        "silent_conflict_overwrite": False,
        "formal_catalog_write": False,
        "quality_promotion": False,
        "research_eligible": False,
        "formal_effect": "NONE",
        "input_bindings": {},
    }
    input_paths = [
        aggregate / "ACTION_EVENTS_BASE_2005_2024.jsonl",
        aggregate / "ACTION_EVENTS_TAIL_2025_20260803.jsonl",
        aggregate / "EX_FACTORS_BASE_2005_2024.jsonl",
        aggregate / "EX_FACTORS_TAIL_2025_20260803.jsonl",
        CSMAR_EVENT,
        CSMAR_TERM,
        CSMAR_FACTOR,
    ]
    for path in input_paths:
        result["input_bindings"][path.relative_to(ROOT).as_posix()] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    samples: list[dict[str, Any]] = []
    for segment, (start, end) in SEGMENTS.items():
        action_result, action_samples = reconcile_actions(
            rq_actions, cs_actions, segment, start, end
        )
        factor_result, factor_samples = reconcile_factors(
            rq_factors, cs_factors, segment, start, end
        )
        result["segments"][segment] = {
            "start_date": start,
            "end_date": end,
            "actions": action_result,
            "factors": factor_result,
        }
        if segment != "TOTAL_2005_20260803":
            samples.extend(action_samples)
            samples.extend(factor_samples)
    result_path = output_root / "RECONCILIATION.json"
    sample_path = output_root / "RECONCILIATION_SAMPLES.jsonl"
    write_json_atomic(result_path, result)
    write_jsonl_atomic(sample_path, samples)
    manifest = {
        "schema_version": 1,
        "contract_id": result["contract_id"],
        "purpose_ceiling": "INSPECTION_ONLY",
        "entries": [
            {
                "path": path.relative_to(ROOT).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in (result_path, sample_path)
        ],
        "formal_effect": "NONE",
        "research_eligible": False,
    }
    write_json_atomic(output_root / "RECONCILIATION_MANIFEST.json", manifest)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run(args.output_root)
    total = result["segments"]["TOTAL_2005_20260803"]
    print(
        canonical_json(
            {
                "status": result["status"],
                "action_matched_keys": total["actions"]["matched_security_event_keys"],
                "action_rqdata_only_keys": total["actions"]["rqdata_only_security_event_keys"],
                "action_csmar_only_keys": total["actions"]["csmar_only_security_event_keys"],
                "factor_matched_keys": total["factors"]["matched_security_event_keys"],
                "factor_numeric_conflicts": total["factors"]["numeric_conflicts"],
                "formal_effect": "NONE",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
