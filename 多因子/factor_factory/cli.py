"""Command line entrypoints for repeatable factor-factory runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from .attempts import ResearchAttemptGovernance, canonical_attempt_path
from .backtest import CostModel
from .pipeline import FactorFactory, FactorFactoryConfig
from .qlib_store import QlibBinaryStore
from .registry import ResearchRegistry, ResearchRunLock, atomic_write_json, file_sha256
from .schema import panel_from_bars
from .selection import SelectionGates
from .validation import three_way_time_split


def _load_json(path: str | Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is forbidden: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key is forbidden: {key}")
            result[key] = value
        return result

    return json.loads(
        Path(path).read_text(encoding="utf-8-sig"),
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )


def _config_from_mapping(payload: Mapping[str, Any]) -> FactorFactoryConfig:
    values = dict(payload)
    schema_version = values.pop("schema_version", None)
    if schema_version != 1:
        raise ValueError("factor-factory config requires schema_version=1")
    if "selection_gates" in values:
        values["selection_gates"] = SelectionGates(**values["selection_gates"])
    if "cost_model" in values:
        values["cost_model"] = CostModel(**values["cost_model"])
    return FactorFactoryConfig(**values)


def _extract_bars(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, dict):
        for key in ("bars_by_symbol", "bars"):
            candidate = payload.get(key)
            if isinstance(candidate, dict):
                return candidate
        for key in ("data", "payload", "bundle"):
            nested = payload.get(key)
            if nested is not None:
                try:
                    return _extract_bars(nested)
                except ValueError:
                    pass
    raise ValueError("input JSON must contain a bars_by_symbol or bars mapping")


def _attempt_terminal_evidence(
    output: Path, manifest: Mapping[str, Any], terminal_state: str
) -> dict[str, Any]:
    ensemble = manifest.get("ensemble")
    if not isinstance(ensemble, dict):
        raise RuntimeError("committed research manifest has no ensemble result")
    if terminal_state == "RESULT_COMMITTED":
        consumption = ensemble.get("holdout_consumption")
        if not isinstance(consumption, dict):
            raise RuntimeError("committed holdout manifest has no consumption identity")
        return {
            "manifest_sha256": file_sha256(output / "manifest.json"),
            "source_research_manifest_sha256": manifest[
                "source_research_manifest_sha256"
            ],
            "freeze_hash": ensemble.get("freeze_hash"),
            "result_artifact_sha256": file_sha256(output / "holdout-result.json"),
            "qualified": bool(ensemble.get("qualified", False)),
            "holdout_run_id": consumption["run_id"],
        }
    selected = _load_json(output / "selected.json")
    if not isinstance(selected, list):
        raise RuntimeError("committed selected artifact is not a list")
    commit = _load_json(output / "COMMITTED.json")
    return {
        "manifest_sha256": file_sha256(output / "manifest.json"),
        "commit_manifest_sha256": commit["manifest_sha256"],
        "result_status": terminal_state,
        "freeze_hash": ensemble.get("freeze_hash"),
        "selected_count": len(selected),
        "qualified": bool(ensemble.get("qualified", False)),
    }


def _reconcile_committed_attempt(output: Path, manifest: Mapping[str, Any]) -> None:
    attempt = manifest.get("research_attempt")
    terminal_state = manifest.get("attempt_terminal_state")
    if attempt is None and terminal_state is None:
        return
    if not isinstance(attempt, dict) or terminal_state not in {
        "NO_FACTOR", "FROZEN_AWAITING_HOLDOUT", "RESULT_COMMITTED"
    }:
        raise RuntimeError("committed run has invalid research-attempt recovery metadata")
    governance = ResearchAttemptGovernance(Path(str(attempt["database"])))
    evidence = _attempt_terminal_evidence(output, manifest, str(terminal_state))
    predecessor = (
        "HOLDOUT_OPENED" if terminal_state == "RESULT_COMMITTED" else "VALIDATION_CONSUMED"
    )
    record = governance.require_state(
        str(attempt["protocol_id"]),
        str(attempt["protocol_hash"]),
        {predecessor, str(terminal_state)},
    )
    governance.transition(
        record.protocol_id, record.protocol_hash, str(terminal_state), evidence
    )


def _preflight_output(path: str | Path) -> None:
    output = Path(path)
    if (output / "COMMITTED.json").exists():
        manifest = ResearchRegistry(output).verify_completed()
        _reconcile_committed_attempt(output, manifest)
        raise FileExistsError(f"completed run is immutable: {output}")
    if (output / ".run.lock").exists():
        raise RuntimeError(f"run directory is locked; inspect before recovery: {output}")


def _verify_failed_lineage(
    lineage_directory: str | Path, expected_manifest_sha256: str
) -> dict[str, Any]:
    lineage = Path(lineage_directory)
    aborted_path = lineage / "ABORTED.json"
    if aborted_path.is_file() and not (lineage / "COMMITTED.json").exists():
        actual_hash = file_sha256(aborted_path)
        if actual_hash != expected_manifest_sha256:
            raise RuntimeError("technical-abort lineage SHA-256 differs from config")
        aborted = _load_json(aborted_path)
        if not isinstance(aborted, dict) or (
            aborted.get("state") != "ABORTED_TECHNICAL"
            or aborted.get("holdout_was_opened") is not False
            or aborted.get("result_committed") is not False
            or (lineage / "manifest.json").exists()
            or (lineage / "selected.json").exists()
            or (lineage / "freeze.json").exists()
        ):
            raise RuntimeError("technical-abort lineage is not sealed and result-free")
        governance = ResearchAttemptGovernance(Path(str(aborted["attempt_database"])))
        record = governance.require_state(
            str(aborted["protocol_id"]),
            str(aborted["protocol_hash"]),
            {"ABORTED_TECHNICAL"},
        )
        evidence = (record.state_evidence or {}).get("ABORTED_TECHNICAL", {})
        if evidence.get("abort_artifact_sha256") != actual_hash:
            raise RuntimeError("technical-abort lineage differs from governed evidence")
        return {
            "run_directory": str(lineage.resolve()),
            "manifest_sha256": actual_hash,
            "commit_sha256": None,
            "selected_sha256": None,
            "predecessor_state": "ABORTED_TECHNICAL",
            "qualified": False,
            "holdout_was_opened": False,
        }
    manifest = ResearchRegistry(lineage).verify_completed()
    actual_hash = file_sha256(lineage / "manifest.json")
    if actual_hash != expected_manifest_sha256:
        raise RuntimeError("V2 lineage manifest SHA-256 differs from V3 config")
    selected = _load_json(lineage / "selected.json")
    ensemble = manifest.get("ensemble")
    if not isinstance(selected, list) or selected:
        raise RuntimeError("V3 lineage must point to a committed run with no selected factor")
    if not isinstance(ensemble, dict):
        raise RuntimeError("V3 lineage manifest has no ensemble result")
    if (
        bool(ensemble.get("qualified"))
        or bool(ensemble.get("holdout_was_opened"))
        or ensemble.get("freeze_hash")
        or (lineage / "freeze.json").exists()
    ):
        raise RuntimeError("V3 lineage is not the declared failed, unopened V2 run")
    return {
        "run_directory": str(lineage.resolve()),
        "manifest_sha256": actual_hash,
        "commit_sha256": file_sha256(lineage / "COMMITTED.json"),
        "selected_sha256": file_sha256(lineage / "selected.json"),
        "qualified": False,
        "holdout_was_opened": False,
    }


def _calendar_split_manifest(
    dates: tuple[str, ...], splits: Any
) -> dict[str, Any]:
    def describe(indices: Any) -> dict[str, Any]:
        return {
            "start": dates[int(indices[0])],
            "end": dates[int(indices[-1])],
            "count": len(indices),
        }

    return {
        "discovery": describe(splits.discovery),
        "validation": describe(splits.validation),
        "holdout": describe(splits.holdout),
        "purge_dates": splits.purge,
    }


def _prepare_qlib_attempt(
    args: argparse.Namespace,
    config: FactorFactoryConfig,
    progress: Any,
) -> dict[str, Any]:
    if not args.config:
        raise ValueError("real Qlib research requires an explicit preregistered V3 config")
    if (
        config.protocol_version < 3
        or config.protocol_id == "UNREGISTERED"
        or not config.lineage_manifest_sha256
    ):
        raise ValueError(
            "real Qlib research requires protocol_version>=3, protocol_id, and lineage hash"
        )
    if config.cost_model.lot_size != 1:
        raise ValueError("Qlib normalized prices require cost_model.lot_size=1 before mining starts")
    store = QlibBinaryStore(args.root)
    end_date = args.end_date or store.calendar[-1]
    panel_start = store.calendar.index(args.start_date)
    panel_end = store.calendar.index(end_date)
    dates = tuple(store.calendar[panel_start:panel_end + 1])
    splits = three_way_time_split(
        len(dates),
        purge=config.horizon + 1,
        discovery_fraction=config.discovery_fraction,
        validation_fraction=config.validation_fraction,
    )
    seal_local_index = int(splits.validation[-1]) + config.horizon + 1
    if seal_local_index >= int(splits.holdout[0]):
        raise RuntimeError("research content seal would overlap the holdout")
    seal_after_date = dates[seal_local_index]
    exchanges = tuple(sorted({
        item.strip().upper() for item in args.exchanges.split(",") if item.strip()
    }))
    if not exchanges:
        raise ValueError("--exchanges must contain at least one exchange")
    split_manifest = _calendar_split_manifest(dates, splits)
    lineage = _verify_failed_lineage(
        args.lineage_research, config.lineage_manifest_sha256
    )
    progress("hashing registered Qlib tree without parsing feature values")
    snapshot_identity = store.research_snapshot_identity(
        start_date=args.start_date,
        end_date=end_date,
        universe=args.universe,
        exchanges=exchanges,
    )
    factory = FactorFactory(config, progress=progress)
    config_file_sha256 = file_sha256(Path(args.config))
    payload = {
        "schema_version": 1,
        "protocol_version": config.protocol_version,
        "protocol_id": config.protocol_id,
        "validation_start": split_manifest["validation"]["start"],
        "validation_end": split_manifest["validation"]["end"],
        "holdout_start": split_manifest["holdout"]["start"],
        "holdout_end": split_manifest["holdout"]["end"],
        "run_directory": str(Path(args.output).resolve()),
        "config": asdict(config),
        "config_file_sha256": config_file_sha256,
        "engine_fingerprint": factory._engine_fingerprint(),
        "runtime_signature": factory._runtime_signature(config.accelerator),
        "lineage_manifest_sha256": config.lineage_manifest_sha256,
        "lineage": lineage,
        "snapshot_identity": snapshot_identity,
        "panel_request": {
            "start_date": args.start_date,
            "end_date": end_date,
            "universe": args.universe,
            "exchanges": list(exchanges),
            "minimum_history_dates": args.minimum_history_dates,
        },
        "splits": split_manifest,
        "content_seal": {
            "loaded_through": seal_after_date,
            "holdout_content_start": split_manifest["holdout"]["start"],
        },
    }
    protocol_hash = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return {
        "store": store,
        "payload": payload,
        "protocol_hash": protocol_hash,
        "config_file_sha256": config_file_sha256,
        "snapshot_identity": snapshot_identity,
        "seal_after_date": seal_after_date,
        "end_date": end_date,
        "exchanges": exchanges,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auditable automatic factor discovery")
    parser.add_argument("--config", help="optional FactorFactoryConfig JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    qlib = subparsers.add_parser("from-qlib", help="mine a real Qlib A-share research snapshot")
    qlib.add_argument("--root", required=True, help="directory containing calendars/features/instruments")
    qlib.add_argument("--start-date", required=True)
    qlib.add_argument("--end-date", help="exact trading date; defaults to the store's last date")
    qlib.add_argument("--universe", default="all")
    qlib.add_argument("--exchanges", default="SH,SZ")
    qlib.add_argument("--output", default=".tmp/factor-factory/qlib-run")
    qlib.add_argument("--minimum-history-dates", type=int, default=60)
    qlib.add_argument(
        "--lineage-research",
        required=True,
        help="committed failed V2 research directory named by lineage_manifest_sha256",
    )

    preflight = subparsers.add_parser(
        "preflight-qlib",
        help="hash and verify a V3 Qlib attempt without registering or consuming validation",
    )
    preflight.add_argument("--root", required=True)
    preflight.add_argument("--start-date", required=True)
    preflight.add_argument("--end-date")
    preflight.add_argument("--universe", default="all")
    preflight.add_argument("--exchanges", default="SH,SZ")
    preflight.add_argument("--output", required=True, help="intended future research directory")
    preflight.add_argument("--minimum-history-dates", type=int, default=60)
    preflight.add_argument("--lineage-research", required=True)
    preflight.add_argument("--report", help="optional retained preflight JSON artifact")

    run_json = subparsers.add_parser("from-json", help="mine factors from aligned OHLCV JSON")
    run_json.add_argument("--input", required=True)
    run_json.add_argument("--output", default=".tmp/factor-factory/json-run")
    run_json.add_argument("--minimum-dates", type=int, default=120)

    holdout = subparsers.add_parser(
        "open-holdout",
        help="open the one governed holdout for an explicitly confirmed frozen Qlib run",
    )
    holdout.add_argument("--root", required=True, help="same immutable Qlib snapshot used for research")
    holdout.add_argument("--research", required=True, help="committed FROZEN_AWAITING_HOLDOUT directory")
    holdout.add_argument("--confirm-freeze-hash", required=True, help="exact 64-character freeze hash")
    holdout.add_argument("--output", required=True, help="new directory, separate from the research run")

    recover = subparsers.add_parser(
        "recover-lock", help="archive a dead local-process run lock after exact hash confirmation"
    )
    recover.add_argument("--output", required=True)
    recover.add_argument("--confirm-lock-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "recover-lock":
        if args.config:
            raise ValueError("recover-lock does not accept --config")
        result = ResearchRunLock.recover_stale(
            args.output, confirm_lock_sha256=args.confirm_lock_sha256
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    _preflight_output(args.output)
    progress = lambda message: print(message, file=sys.stderr, flush=True)
    if args.command == "open-holdout":
        if args.config:
            raise ValueError("open-holdout derives its immutable config from freeze.json; --config is forbidden")
        if len(args.confirm_freeze_hash) != 64:
            raise ValueError("--confirm-freeze-hash must contain exactly 64 hexadecimal characters")
        try:
            int(args.confirm_freeze_hash, 16)
        except ValueError as exc:
            raise ValueError("--confirm-freeze-hash must be hexadecimal") from exc
        result = FactorFactory.open_frozen_qlib_holdout(
            args.root,
            args.research,
            args.output,
            confirm_freeze_hash=args.confirm_freeze_hash,
            progress=progress,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2, allow_nan=False))
        return 0

    config = _config_from_mapping(_load_json(args.config)) if args.config else FactorFactoryConfig()
    if args.command in {"from-qlib", "preflight-qlib"}:
        prepared = _prepare_qlib_attempt(args, config, progress)
        payload = prepared["payload"]
        if args.command == "preflight-qlib":
            report = {
                "status": "PREFLIGHT_PASSED_NOT_REGISTERED",
                "validation_consumed": False,
                "attempt_database": str(canonical_attempt_path().resolve()),
                "predicted_protocol_hash": prepared["protocol_hash"],
                "config_file_sha256": prepared["config_file_sha256"],
                "engine_fingerprint": payload["engine_fingerprint"],
                "runtime_signature": payload["runtime_signature"],
                "lineage": payload["lineage"],
                "snapshot_identity": prepared["snapshot_identity"],
                "splits": payload["splits"],
                "content_seal": payload["content_seal"],
                "intended_run_directory": payload["run_directory"],
            }
            if args.report:
                atomic_write_json(Path(args.report), report)
                report["report"] = str(Path(args.report).resolve())
            print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        attempt_path = canonical_attempt_path()
        attempt_governance = ResearchAttemptGovernance(attempt_path)
        with ResearchRunLock(args.output):
            registered = attempt_governance.register(payload)
            validation_evidence = {
                "protocol_hash": registered.protocol_hash,
                "config_file_sha256": prepared["config_file_sha256"],
                "engine_fingerprint": payload["engine_fingerprint"],
                "runtime_signature": payload["runtime_signature"],
                "lineage_manifest_sha256": config.lineage_manifest_sha256,
                "snapshot_sha256": prepared["snapshot_identity"]["snapshot_sha256"],
                "raw_tree_fingerprint": prepared["snapshot_identity"]["raw_tree_fingerprint"],
                "loaded_through": prepared["seal_after_date"],
            }
            consumed = attempt_governance.transition(
                config.protocol_id,
                registered.protocol_hash,
                "VALIDATION_CONSUMED",
                validation_evidence,
            )
            panel = prepared["store"].load_panel(
                start_date=args.start_date,
                end_date=prepared["end_date"],
                universe=args.universe,
                exchanges=prepared["exchanges"],
                minimum_history_dates=args.minimum_history_dates,
                seal_after_date=prepared["seal_after_date"],
            )
            panel.metadata["research_attempt"] = {
                "database": str(attempt_path.resolve()),
                "protocol_id": consumed.protocol_id,
                "protocol_hash": consumed.protocol_hash,
                "validation_evidence": validation_evidence,
            }
            factory = FactorFactory(
                config,
                progress=progress,
                attempt_governance_path=attempt_path,
            )
            result = factory._run_locked(panel, Path(args.output))
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    else:
        payload = _load_json(args.input)
        bars = _extract_bars(payload)
        panel = panel_from_bars(
            bars,
            minimum_dates=args.minimum_dates,
            metadata={
                "source_file": str(Path(args.input).resolve()),
                "mode": "json_smoke_adapter",
                "promotion_ceiling": "ENGINEERING_ONLY",
                "promotion_blockers": ["common-date-intersection adapter is not a point-in-time research universe"],
            },
        )
    result = FactorFactory(config, progress=progress).run(panel, args.output)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
