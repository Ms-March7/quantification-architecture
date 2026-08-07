from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np

from factor_factory.qdata_input import ResolvedResearchInput
from factor_factory.r0_dense_panel import (
    _select_curated_payloads,
    load_r0_shsz_panel_dense,
)
from factor_factory.r0_shsz_panel_input import (
    AUTHORITY_RELATIVE_PATHS,
    CONTRACT_ID,
    DATASET_ID,
    EXPECTED_RESEARCH_INPUT_CONTRACT,
    HASH_ALGORITHM,
    PAYLOAD_HASH_ALGORITHM,
    SNAPSHOT_ID,
    AvailabilityRecord,
    PayloadSlice,
    R0SHSZPVPanelAdapter,
    R0SHSZPanelInputError,
    enforce_signal_field_isolation,
)


TZ = ZoneInfo("Asia/Shanghai")


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class StubResolver:
    def __init__(self, descriptor: ResolvedResearchInput) -> None:
        self.descriptor = descriptor
        self.calls: list[tuple[str, str]] = []

    def resolve(self, dataset_id: str, snapshot_id: str) -> ResolvedResearchInput:
        self.calls.append((dataset_id, snapshot_id))
        return self.descriptor


class StubReader:
    def __init__(self, payload: PayloadSlice) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def read_slice(
        self,
        parquet_paths: Sequence[Path],
        *,
        start_date: date,
        end_date: date,
        symbols: tuple[str, ...] | None,
        max_panel_cells: int,
    ) -> PayloadSlice:
        self.calls.append(
            {
                "paths": tuple(parquet_paths),
                "start_date": start_date,
                "end_date": end_date,
                "symbols": symbols,
                "max_panel_cells": max_panel_cells,
            }
        )
        return self.payload


class R0SHSZPanelInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.snapshot_root = (
            self.workspace
            / "data"
            / "market"
            / "snapshots"
            / DATASET_ID
            / SNAPSHOT_ID
        )
        self.snapshot_root.mkdir(parents=True)
        self.curated_root = (
            self.workspace
            / "data"
            / "market"
            / "curated"
            / DATASET_ID
            / SNAPSHOT_ID
        )
        self.raw_root = (
            self.workspace
            / "data"
            / "market"
            / "raw"
            / DATASET_ID
            / SNAPSHOT_ID
        )
        self.raw_root.mkdir(parents=True)
        self.payload_path = (
            self.curated_root
            / "schema=r0-shsz-pv-panel-v2"
            / "year=2026"
            / "month=8"
            / "part-test.parquet"
        )
        self.payload_path.parent.mkdir(parents=True)
        self.payload_path.write_bytes(b"sealed parquet fixture bytes")
        self._write_workspace_contract()
        self.documents = self._base_documents()
        self.descriptor = self._write_authorities(self.documents)
        self.resolver = StubResolver(self.descriptor)
        self.reader = StubReader(self._valid_payload())

    def _write_workspace_contract(self) -> None:
        path = (
            self.workspace
            / "data"
            / "catalog"
            / "contracts"
            / CONTRACT_ID
            / "contract.json"
        )
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "contract_id": CONTRACT_ID,
                    "gate_id": "R0_SHSZ_PV",
                    "scope": {
                        "venues": ["XSHG", "XSHE"],
                        "instrument_scope": "ORDINARY_A_SHARES_ONLY",
                        "coverage_start_date": "2005-01-04",
                        "coverage_end_date": "2026-08-03",
                    },
                    "research_semantics": {
                        "timezone": "Asia/Shanghai",
                        "decision_time": "TRADE_DATE_CLOSE",
                        "bar_available_at": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
                        "earliest_execution_time": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
                        "signal_lag_sessions_minimum": 1,
                        "raw_price_convention": "UNADJUSTED",
                        "return_convention": (
                            "ONE_PERIOD_REALIZED_TOTAL_RETURN_AVAILABLE_AFTER_PERIOD_CLOSE"
                        ),
                        "adjusted_price_levels_allowed": False,
                        "latest_forward_adjusted_history_allowed": False,
                        "corporate_action_features_allowed": False,
                        "missing_value_policy": "NULL_NEVER_ZERO_FILL",
                        "execution_mask_role": (
                            "SIMULATION_ONLY__NEVER_SIGNAL_RANK_INPUT"
                        ),
                    },
                    "formal_target": {
                        "dataset_id": DATASET_ID,
                        "snapshot_id": SNAPSHOT_ID,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _base_documents(self) -> dict[str, dict[str, Any]]:
        workspace_relative = self.payload_path.relative_to(self.workspace).as_posix()
        relative = self.payload_path.relative_to(self.curated_root).as_posix()
        file_record = {
            "layer": "curated",
            "relative_path": relative,
            "workspace_relative_path": workspace_relative,
            "bytes": self.payload_path.stat().st_size,
            "sha256": sha(self.payload_path.read_bytes()),
        }
        canonical_record = {
            key: file_record[key]
            for key in ("layer", "relative_path", "bytes", "sha256")
        }
        payload_hash = sha(canonical([canonical_record]))
        request = {
            "schema_version": 1,
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "store_id": "workspace-market-snapshots",
            "owner_project": "multi-factor",
            "purpose_ceiling": "RESEARCH_ONLY",
            "license_status": "DECLARED",
            "quality_status": "OK",
            "consumers": ["multi-factor"],
        }
        manifest = {
            "schema_version": 1,
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "root_path": str(self.snapshot_root).replace("\\", "/"),
            "hash_algorithm": HASH_ALGORITHM,
            "payload_hash": payload_hash,
            "payload_hash_algorithm": PAYLOAD_HASH_ALGORITHM,
            "contract_id": CONTRACT_ID,
            "dataset_schema_version": "r0-shsz-pv-panel-v2",
            "available_at": "2026-08-04T09:30:00+08:00",
            "raw_root": self.raw_root.relative_to(self.workspace).as_posix(),
            "curated_root": self.curated_root.relative_to(self.workspace).as_posix(),
            "status": "SEALED_SNAPSHOT",
            "formal_workspace_execution": True,
            "research_input_contract": dict(EXPECTED_RESEARCH_INPUT_CONTRACT),
        }
        provenance = {
            "schema_version": 1,
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "mode": "FORMAL_ATOMIC_WORKSPACE_PUBLICATION",
            "source_id": "fixture-source",
            "contract_id": CONTRACT_ID,
            "request_contract_hash": sha(canonical(request)),
            "payload_hash": payload_hash,
            "available_at": "2026-08-04T09:30:00+08:00",
        }
        partitions = {
            "schema_version": 1,
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "payload_hash": payload_hash,
            "files": [file_record],
        }
        quality = {
            "schema_version": 1,
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "overall": "PASS",
            "catalog_quality_status": "OK",
            "purpose_ceiling": "RESEARCH_ONLY",
            "formal_workspace_execution": True,
        }
        return {
            "request.json": request,
            "manifest.json": manifest,
            "provenance.json": provenance,
            "partition-manifest.json": partitions,
            "quality/summary.json": quality,
        }

    def _write_authorities(
        self,
        documents: Mapping[str, Mapping[str, Any]],
        *,
        lifecycle: str = "frozen",
        license_status: str = "DECLARED",
    ) -> ResolvedResearchInput:
        writable = {name: dict(value) for name, value in documents.items()}
        manifest = writable["manifest.json"]
        manifest.pop("content_hash", None)
        content_hash = sha(
            canonical(
                {
                    "hash_algorithm": HASH_ALGORITHM,
                    "request": writable["request.json"],
                    "manifest": manifest,
                    "provenance": writable["provenance.json"],
                    "partition_manifest": writable["partition-manifest.json"],
                    "quality": writable["quality/summary.json"],
                }
            )
        )
        manifest["content_hash"] = content_hash
        authority_paths: list[Path] = []
        for relative, value in writable.items():
            path = self.snapshot_root.joinpath(*Path(relative).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            authority_paths.append(path)
        self.documents = writable
        return ResolvedResearchInput(
            dataset_id=DATASET_ID,
            snapshot_id=SNAPSHOT_ID,
            resolved_path=self.snapshot_root,
            purpose_ceiling="RESEARCH_ONLY",
            lifecycle=lifecycle,
            quality_status="OK",
            license_status=license_status,
            available_at=datetime.fromisoformat("2026-08-04T09:30:00+08:00"),
            content_hash=content_hash,
            hash_algorithm=HASH_ALGORITHM,
            authority_paths=tuple(authority_paths),
            known_limits=(),
        )

    @staticmethod
    def _availability(
        *,
        middle: datetime | None = None,
        terminal: datetime | None = None,
    ) -> tuple[AvailabilityRecord, ...]:
        first_next = datetime(2026, 7, 31, 9, 30, tzinfo=TZ)
        middle_value = middle or datetime(2026, 8, 3, 9, 30, tzinfo=TZ)
        terminal_value = terminal or datetime(2026, 8, 4, 9, 30, tzinfo=TZ)
        return (
            AvailabilityRecord(date(2005, 1, 4), first_next, first_next, 1, 1),
            AvailabilityRecord(date(2026, 7, 31), middle_value, middle_value, 2, 2),
            AvailabilityRecord(date(2026, 8, 3), terminal_value, terminal_value, 1, 1),
        )

    @staticmethod
    def _row(
        symbol: str,
        trade_date: date,
        *,
        previous_close: float,
        close: float,
        realized_total_return: float,
        had_trade: bool = True,
        open_execution_eligible: bool = True,
        available_at: datetime,
    ) -> dict[str, Any]:
        open_price = close + 0.5
        return {
            "order_book_id": symbol,
            "exchange_mic": symbol.split(".")[1],
            "local_code": symbol.split(".")[0],
            "trade_date": trade_date,
            "previous_close": previous_close,
            "open": open_price,
            "high": max(open_price, close) + 1.0,
            "low": min(open_price, close) - 1.0,
            "close": close,
            "volume_shares": 1000 if had_trade else 0,
            "turnover_cny": 100000.0 if had_trade else 0.0,
            "realized_total_return": realized_total_return,
            "trading_status_code": "1",
            "upper_limit_price": None,
            "lower_limit_price": None,
            "available_at": available_at,
            "listed_episode_eligible": True,
            "had_trade": had_trade,
            "open_execution_eligible": open_execution_eligible,
            "source_id": "fixture",
            "source_snapshot_id": "fixture-v1",
        }

    def _valid_payload(self) -> PayloadSlice:
        july_available = datetime(2026, 8, 3, 9, 30, tzinfo=TZ)
        august_available = datetime(2026, 8, 4, 9, 30, tzinfo=TZ)
        return PayloadSlice(
            rows=(
                # The -1% raw price move plus a cash action has +1% total return.
                self._row(
                    "603986.XSHG",
                    date(2026, 7, 31),
                    previous_close=100.0,
                    close=99.0,
                    realized_total_return=0.01,
                    available_at=july_available,
                ),
                self._row(
                    "000001.XSHE",
                    date(2026, 7, 31),
                    previous_close=10.0,
                    close=10.0,
                    realized_total_return=0.0,
                    had_trade=False,
                    open_execution_eligible=False,
                    available_at=july_available,
                ),
                self._row(
                    "603986.XSHG",
                    date(2026, 8, 3),
                    previous_close=99.0,
                    close=98.0,
                    realized_total_return=-0.0101010101,
                    open_execution_eligible=False,
                    available_at=august_available,
                ),
            ),
            availability=self._availability(),
        )

    def _adapter(self) -> R0SHSZPVPanelAdapter:
        return R0SHSZPVPanelAdapter(
            workspace_root=self.workspace,
            resolver=self.resolver,  # type: ignore[arg-type]
            parquet_reader=self.reader,
            now_provider=lambda: datetime(2026, 8, 4, 9, 30, tzinfo=TZ),
        )

    def test_research_load_fails_closed_before_terminal_available_at(self) -> None:
        adapter = R0SHSZPVPanelAdapter(
            workspace_root=self.workspace,
            resolver=self.resolver,  # type: ignore[arg-type]
            parquet_reader=self.reader,
            now_provider=lambda: datetime(2026, 8, 4, 2, 0, tzinfo=TZ),
        )
        with self.assertRaisesRegex(R0SHSZPanelInputError, "not yet available"):
            adapter.load(
                start_date="2026-07-31",
                end_date="2026-08-03",
                symbols=("603986.XSHG",),
            )
        self.assertEqual(self.reader.calls, [])

    def test_stub_cannot_bypass_qdata_with_invalid_available_at(self) -> None:
        for value, message in (
            (datetime(2026, 8, 4, 9, 30), "explicit UTC offset"),
            (None, "timezone-aware timestamp"),
        ):
            with self.subTest(available_at=value):
                self.reader.calls.clear()
                self.resolver = StubResolver(
                    replace(self.descriptor, available_at=value)  # type: ignore[arg-type]
                )
                with self.assertRaisesRegex(R0SHSZPanelInputError, message):
                    self._adapter().load(
                        start_date="2026-07-31",
                        end_date="2026-08-03",
                        symbols=("603986.XSHG",),
                    )
                self.assertEqual(self.reader.calls, [])

    def test_exact_formal_snapshot_builds_bounded_panel_without_mask_features(self) -> None:
        panel = self._adapter().load(
            start_date="2026-07-31",
            end_date="2026-08-03",
            symbols=("603986.XSHG", "000001.XSHE"),
        )

        self.assertEqual(panel.dates, ("2026-07-31", "2026-08-03"))
        self.assertEqual(panel.symbols, ("000001.XSHE", "603986.XSHG"))
        self.assertEqual(
            set(panel.fields),
            {"open", "close", "high", "low", "volume", "amount", "realized_total_return"},
        )
        self.assertNotIn("open_execution_eligible", panel.fields)
        self.assertNotIn("eligible", panel.fields)
        stock = panel.symbols.index("603986.XSHG")
        inactive = panel.symbols.index("000001.XSHE")
        self.assertTrue(panel.eligible[0, stock])
        self.assertTrue(panel.buyable[0, stock])
        self.assertFalse(panel.buyable[1, stock])
        self.assertFalse(panel.sellable[1, stock])
        self.assertFalse(panel.eligible[0, inactive])
        self.assertEqual(panel.fields["volume"][0, inactive], 0.0)
        # Missing symbol-date cells are NaN, never silently zero-filled.
        self.assertTrue(np.isnan(panel.fields["close"][1, inactive]))
        self.assertTrue(np.isnan(panel.fields["volume"][1, inactive]))
        self.assertFalse(panel.eligible[1, inactive])
        self.assertEqual(panel.metadata["signal_lag_sessions_minimum"], 1)
        self.assertEqual(panel.metadata["factor_runs"], 0)
        self.assertEqual(len(self.resolver.calls), 1)
        self.assertEqual(len(self.reader.calls), 1)

    def test_ex_post_execution_mask_cannot_enter_signal_fields(self) -> None:
        with self.assertRaisesRegex(
            R0SHSZPanelInputError,
            "ex-post execution/eligibility masks cannot be signal fields",
        ):
            enforce_signal_field_isolation(
                ("open", "close", "open_execution_eligible"),
                ("open", "close", "open_execution_eligible"),
            )

    def test_action_aware_total_return_is_preserved_not_recomputed_from_prices(self) -> None:
        panel = self._adapter().load(
            start_date="2026-07-31",
            end_date="2026-08-03",
            symbols=("603986.XSHG",),
        )
        stock = panel.symbols.index("603986.XSHG")
        self.assertAlmostEqual(panel.fields["realized_total_return"][0, stock], 0.01)
        raw_price_return = 99.0 / 100.0 - 1.0
        self.assertNotAlmostEqual(
            panel.fields["realized_total_return"][0, stock], raw_price_return
        )

    def test_one_price_limit_blocks_only_constrained_side_and_reopened_board_is_allowed(self) -> None:
        rows = [dict(row) for row in self.reader.payload.rows]
        first = rows[0]
        first.update({
            "open": 110.0,
            "high": 110.0,
            "low": 110.0,
            "close": 110.0,
            "upper_limit_price": 110.0,
            "lower_limit_price": 90.0,
            "open_execution_eligible": False,
        })
        last = rows[-1]
        last.update({
            "open": 110.0,
            "high": 110.0,
            "low": 108.0,
            "close": 109.0,
            "upper_limit_price": 110.0,
            "lower_limit_price": 90.0,
            "open_execution_eligible": False,
        })
        self.reader.payload = PayloadSlice(
            rows=tuple(rows), availability=self.reader.payload.availability
        )
        panel = self._adapter().load(
            start_date="2026-07-31",
            end_date="2026-08-03",
            symbols=("603986.XSHG",),
        )
        stock = panel.symbols.index("603986.XSHG")
        self.assertFalse(panel.buyable[0, stock])
        self.assertTrue(panel.sellable[0, stock])
        self.assertTrue(panel.buyable[1, stock])
        self.assertTrue(panel.sellable[1, stock])
        self.assertEqual(
            panel.metadata["execution_mask_counts"]["reopened_limit_board"], 1
        )

    def test_available_at_same_day_is_rejected_as_leakage(self) -> None:
        same_day = datetime(2026, 7, 31, 9, 30, tzinfo=TZ)
        self.reader.payload = PayloadSlice(
            rows=self.reader.payload.rows,
            availability=self._availability(middle=same_day),
        )
        with self.assertRaisesRegex(R0SHSZPanelInputError, "next observed session 09:30"):
            self._adapter().load(
                start_date="2026-07-31",
                end_date="2026-08-03",
                symbols=("603986.XSHG",),
            )

    def test_terminal_august_3_must_bind_to_august_4_open(self) -> None:
        wrong_tail = datetime(2026, 8, 5, 9, 30, tzinfo=TZ)
        self.reader.payload = PayloadSlice(
            rows=self.reader.payload.rows,
            availability=self._availability(terminal=wrong_tail),
        )
        with self.assertRaisesRegex(R0SHSZPanelInputError, "expected 2026-08-04T09:30:00"):
            self._adapter().load(
                start_date="2026-07-31",
                end_date="2026-08-03",
                symbols=("603986.XSHG",),
            )

    def test_exact_identity_rejects_latest_and_direct_path_before_qdata(self) -> None:
        for dataset_id, snapshot_id in (
            (DATASET_ID, "latest"),
            (str(self.snapshot_root), SNAPSHOT_ID),
        ):
            with self.subTest(dataset_id=dataset_id, snapshot_id=snapshot_id):
                self.resolver.calls.clear()
                with self.assertRaisesRegex(R0SHSZPanelInputError, "only exact"):
                    self._adapter().load(
                        dataset_id=dataset_id,
                        snapshot_id=snapshot_id,
                        start_date="2026-07-31",
                        end_date="2026-08-03",
                        symbols=("603986.XSHG",),
                    )
                self.assertEqual(self.resolver.calls, [])

    def test_active_or_review_like_resolution_is_not_a_formal_input(self) -> None:
        active = self._write_authorities(self.documents, lifecycle="active")
        self.resolver = StubResolver(active)
        with self.assertRaisesRegex(R0SHSZPanelInputError, "lifecycle=frozen"):
            self._adapter().load(
                start_date="2026-07-31",
                end_date="2026-08-03",
                symbols=("603986.XSHG",),
            )

        self.documents["provenance.json"]["mode"] = "REVIEW_CANDIDATE"
        reviewed = self._write_authorities(self.documents)
        self.resolver = StubResolver(reviewed)
        with self.assertRaisesRegex(R0SHSZPanelInputError, "candidate/review/synthetic"):
            self._adapter().load(
                start_date="2026-07-31",
                end_date="2026-08-03",
                symbols=("603986.XSHG",),
            )

    def test_non_declared_license_is_rejected_even_if_a_stub_bypasses_qdata(self) -> None:
        descriptor = self._write_authorities(
            self.documents,
            license_status="NOT_APPLICABLE",
        )
        self.resolver = StubResolver(descriptor)
        with self.assertRaisesRegex(R0SHSZPanelInputError, "DECLARED or VERIFIED"):
            self._adapter().load(
                start_date="2026-07-31",
                end_date="2026-08-03",
                symbols=("603986.XSHG",),
            )

    def test_exact_research_contract_map_is_required(self) -> None:
        research_contract = dict(
            self.documents["manifest.json"]["research_input_contract"]
        )
        research_contract["signal_lag_sessions_minimum"] = 0
        self.documents["manifest.json"]["research_input_contract"] = research_contract
        descriptor = self._write_authorities(self.documents)
        self.resolver = StubResolver(descriptor)
        with self.assertRaisesRegex(R0SHSZPanelInputError, "exact R0_SHSZ_PV map"):
            self._adapter().load(
                start_date="2026-07-31",
                end_date="2026-08-03",
                symbols=("603986.XSHG",),
            )

    def test_payload_hash_mismatch_fails_before_parquet_reader(self) -> None:
        self.payload_path.write_bytes(b"tampered")
        with self.assertRaisesRegex(R0SHSZPanelInputError, "byte size mismatch|hash mismatch"):
            self._adapter().load(
                start_date="2026-07-31",
                end_date="2026-08-03",
                symbols=("603986.XSHG",),
            )
        self.assertEqual(self.reader.calls, [])


class R0DensePanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.curated_root = (
            self.workspace
            / "data"
            / "market"
            / "curated"
            / DATASET_ID
            / SNAPSHOT_ID
        )

    def _entry(self, path: Path) -> dict[str, Any]:
        return {
            "layer": "curated",
            "relative_path": path.relative_to(self.curated_root).as_posix(),
            "workspace_relative_path": path.relative_to(self.workspace).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha(path.read_bytes()),
        }

    def test_manifest_month_selection_hashes_only_intersecting_payload(self) -> None:
        january = (
            self.curated_root
            / "schema=r0-shsz-pv-panel-v2"
            / "year=2022"
            / "month=1"
            / "part-test.parquet"
        )
        february = january.parent.parent / "month=2" / "part-test.parquet"
        january.parent.mkdir(parents=True)
        february.parent.mkdir(parents=True)
        january.write_bytes(b"january")
        february.write_bytes(b"future footer must never be opened")
        january_entry = self._entry(january)
        february_entry = self._entry(february)
        february_entry["sha256"] = "0" * 64
        adapter = SimpleNamespace(workspace_root=self.workspace)
        selected, audit = _select_curated_payloads(
            adapter=adapter,
            documents={
                "partition-manifest.json": {
                    "files": [january_entry, february_entry],
                }
            },
            validated_paths=(january, february),
            start=date(2022, 1, 1),
            end=date(2022, 1, 31),
        )
        self.assertEqual(selected, (january,))
        self.assertEqual(len(audit), 1)
        self.assertIn("year=2022/month=1/", audit[0]["relative_path"])

    def test_dense_loader_never_passes_future_invalid_parquet_to_duckdb(self) -> None:
        try:
            import duckdb
        except ImportError:
            self.skipTest("DuckDB is not installed")
        january = (
            self.curated_root
            / "schema=r0-shsz-pv-panel-v2"
            / "year=2022"
            / "month=1"
            / "part-test.parquet"
        )
        february = january.parent.parent / "month=2" / "part-invalid.parquet"
        january.parent.mkdir(parents=True)
        february.parent.mkdir(parents=True)
        connection = duckdb.connect(database=":memory:")
        try:
            connection.execute(
                """
                CREATE TABLE payload (
                    order_book_id VARCHAR,
                    exchange_mic VARCHAR,
                    local_code VARCHAR,
                    trade_date DATE,
                    previous_close DOUBLE,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE,
                    volume_shares BIGINT,
                    turnover_cny DOUBLE,
                    realized_total_return DOUBLE,
                    trading_status_code VARCHAR,
                    upper_limit_price DOUBLE,
                    lower_limit_price DOUBLE,
                    available_at TIMESTAMPTZ,
                    listed_episode_eligible BOOLEAN,
                    had_trade BOOLEAN,
                    open_execution_eligible BOOLEAN,
                    source_id VARCHAR,
                    source_snapshot_id VARCHAR
                )
                """
            )
            connection.execute(
                """
                INSERT INTO payload VALUES (
                    '600000.XSHG', 'XSHG', '600000', DATE '2022-01-04',
                    10.0, 11.0, 11.0, 10.5, 10.8, 1000, 10800.0, 0.08,
                    'TRADING', 11.0, 9.0,
                    TIMESTAMPTZ '2022-01-05 09:30:00+08:00',
                    TRUE, TRUE, TRUE, 'fixture', 'fixture-v1'
                )
                """
            )
            destination = str(january).replace("'", "''").replace("\\", "/")
            connection.execute(f"COPY payload TO '{destination}' (FORMAT PARQUET)")
        finally:
            connection.close()
        february.write_bytes(b"not a parquet file")
        documents = {
            "partition-manifest.json": {
                "files": [self._entry(january), self._entry(february)],
            }
        }

        class DenseAdapterStub:
            def __init__(stub, workspace_root: str | Path | None = None) -> None:
                stub.workspace_root = Path(workspace_root or self.workspace)
                stub.resolver = stub

            def resolve(stub, dataset_id: str, snapshot_id: str) -> Any:
                return SimpleNamespace(content_hash="f" * 64)

            def _exact_identity(stub, dataset_id: str, snapshot_id: str) -> None:
                return None

            def _symbols(stub, symbols: Sequence[str] | None) -> tuple[str, ...] | None:
                return tuple(symbols) if symbols is not None else None

            def _validate_workspace_contract(stub) -> None:
                return None

            def _validate_formal_resolution(stub, resolved: Any) -> Any:
                return documents

            def _payload_paths(
                stub,
                supplied_documents: Any,
                *,
                verify_payload_hashes: bool,
            ) -> tuple[Path, ...]:
                self.assertFalse(verify_payload_hashes)
                return january, february

        with patch(
            "factor_factory.r0_dense_panel.R0SHSZPVPanelAdapter",
            DenseAdapterStub,
        ):
            panel = load_r0_shsz_panel_dense(
                start_date="2022-01-04",
                end_date="2022-01-04",
                workspace_root=self.workspace,
            )
        self.assertEqual(panel.shape, (1, 1))
        selected = panel.metadata["bounded_slice"]["selected_payloads"]
        self.assertEqual(len(selected), 1)
        self.assertIn("year=2022/month=1/", selected[0]["relative_path"])
        self.assertNotIn("reopened_limit_board", panel.fields)
        reopened = panel.metadata["execution_masks"]["reopened_limit_board"]
        self.assertTrue(bool(reopened[0, 0]))
        self.assertFalse(reopened.flags.writeable)
        self.assertEqual(panel.metadata["opened_limit_board_extra_stress_bps"], 20.0)
        self.assertTrue(panel.metadata["terminal_event_mask_status"].startswith("UNAVAILABLE"))


if __name__ == "__main__":
    unittest.main()
