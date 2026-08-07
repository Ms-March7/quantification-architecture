from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools/build_r0_shsz_pv_panel_candidate.py"
)
SPEC = importlib.util.spec_from_file_location("build_r0_shsz_pv_panel_candidate", MODULE_PATH)
assert SPEC and SPEC.loader
BUILDER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUILDER
SPEC.loader.exec_module(BUILDER)


class R0ShszPvPanelCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.historical = self.root / "historical"
        self.local_tail = self.root / "local-tail"
        self.delta = self.root / "delta"
        self.master_calendar = self.root / "master-calendar"
        self.actions = self.root / "actions"
        self.contract = self.root / "contract.json"
        self.action_contract = self.root / "action-contract.json"
        self.output = self.root / "candidate-20260804-v1"
        self._write_inputs()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    @staticmethod
    def _copy_query(path: Path, query: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect()
        try:
            escaped = path.resolve().as_posix().replace("'", "''")
            connection.execute(
                f"COPY ({query}) TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            connection.close()

    def _write_inputs(self) -> None:
        self._write_json(
            self.contract,
            {
                "contract_id": "r0_shsz_pv_v2",
                "normalized_panel_required_fields": list(BUILDER.REQUIRED_FIELDS),
            },
        )
        self._write_json(self.action_contract, {"contract_id": "test-action-contract"})

        self._copy_query(
            self.historical / "curated/year=2005/month=01/part.parquet",
            """
            SELECT * FROM (VALUES
              ('600000', DATE '2005-01-04', 1, 'XSHG', 10.0, 10.1, 10.6, 9.9, 10.5,
               1000::BIGINT, 10500.0, 0.05, 1, 11.0, 9.0),
              ('600849', DATE '2005-01-04', 1, 'XSHG', 8.0, 8.1, 8.3, 7.9, 8.2,
               100::BIGINT, 820.0, 0.025, 1, 8.8, 7.2),
              ('689009', DATE '2005-01-04', 32, 'XSHG', 20.0, 20.1, 20.6, 19.9, 20.5,
               100::BIGINT, 2050.0, 0.025, 1, 22.0, 18.0)
            ) AS t(source_local_code,trade_date,market_type,observed_venue_mic,
              exchange_preclose_price,open_price,high_price,low_price,close_price,
              traded_shares,traded_value_cny,vendor_total_return_with_cash_reinvestment,
              vendor_trade_status,vendor_limit_up_price,vendor_limit_down_price)
            """,
        )
        self._copy_query(
            self.local_tail / "curated/year=2025/month=02/part.parquet",
            """
            SELECT * FROM (VALUES
              ('302132', DATE '2025-02-17', 16, 10.0, 10.1, 10.6, 9.9, 10.5,
               2000::BIGINT, 21000.0, 0.05, 1, 11.0, 9.0),
              ('689009', DATE '2025-02-17', 32, 20.0, 20.1, 20.6, 19.9, 20.5,
               100::BIGINT, 2050.0, 0.025, 1, 22.0, 18.0)
            ) AS t(Stkcd,Trddt,Markettype,PreClosePrice,Opnprc,Hiprc,Loprc,Clsprc,
              Dnshrtrd,Dnvaltrd,Dretwd,Trdsta,LimitUp,LimitDown)
            """,
        )
        delta_rows = ",\n".join(
            f"('600000.XSHG', DATE '{trade_date}', 10.1, 10.6, 9.9, 10.5, 10.0, "
            "1000.0, 10500.0, 11.0, 9.0, FALSE, FALSE, NULL::BOOLEAN, FALSE)"
            for trade_date in ("2026-07-29", "2026-07-30", "2026-07-31", "2026-08-03")
        )
        self._copy_query(
            self.delta / "curated/year=2026/month=07/part.parquet",
            f"""
            SELECT * FROM (VALUES {delta_rows}) AS t(
              order_book_id,trade_date,open,high,low,close,prev_close,volume,total_turnover,
              limit_up,limit_down,zero_activity_candidate,st_consensus_state_known,
              st_consensus_state,suspension_state_known)
            """,
        )

        master_rows = [
            {
                "order_book_id": "600000.XSHG",
                "mic": "XSHG",
                "local_code": "600000",
                "episode_id": "episode-600000",
                "listed_date": "1999-11-10",
                "de_listed_date": None,
            },
            {
                "order_book_id": "302132.XSHE",
                "mic": "XSHE",
                "local_code": "302132",
                "episode_id": "episode-302132",
                "listed_date": "2010-08-27",
                "de_listed_date": None,
            },
            {
                "order_book_id": "689009.XSHG",
                "mic": "XSHG",
                "local_code": "689009",
                "episode_id": "episode-689009",
                "listed_date": "2020-10-29",
                "de_listed_date": None,
            },
        ]
        self._write_jsonl(
            self.master_calendar / "normalized/rqdata_shsz_cs_master.jsonl", master_rows
        )
        calendar = [
            "2005-01-04",
            "2005-01-05",
            "2025-02-17",
            "2025-02-18",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
            "2026-08-03",
        ]
        self._write_jsonl(
            self.master_calendar / "normalized/rqdata_trading_dates.jsonl",
            [{"trading_date": value} for value in calendar],
        )
        self._write_jsonl(
            self.actions / "aggregate/ACTION_EVENTS_TAIL_2025_20260803.jsonl",
            [
                {
                    "security_id": "600000.XSHG",
                    "event_date": "2026-07-30",
                    "event_type": "DIVIDEND",
                }
            ],
        )
        self._write_jsonl(
            self.actions / "aggregate/EX_FACTORS_TAIL_2025_20260803.jsonl",
            [
                {
                    "security_id": "600000.XSHG",
                    "event_date": "2026-07-30",
                    "ex_factor": 1.1,
                }
            ],
        )

    def _inputs(self) -> object:
        return BUILDER.BuildInputs(
            contract_path=self.contract,
            historical_root=self.historical,
            local_tail_root=self.local_tail,
            delta_root=self.delta,
            master_calendar_root=self.master_calendar,
            action_root=self.actions,
            action_contract_path=self.action_contract,
        )

    def _expectations(self) -> object:
        return BUILDER.BuildExpectations(
            delta_rows_by_date={
                "2026-07-29": 1,
                "2026-07-30": 1,
                "2026-07-31": 1,
                "2026-08-03": 1,
            },
            action_keys_by_date={"2026-07-30": 1},
        )

    def _build(self) -> dict[str, object]:
        receipt = BUILDER.test_terminal_session_receipt(
            ["2026-08-03", "2026-08-04", "2026-08-05"]
        )
        return BUILDER.build_candidate(
            self.output,
            inputs=self._inputs(),
            expectations=self._expectations(),
            terminal_receipt=receipt,
            verify_authorities=False,
            allow_test_paths=True,
        )

    def test_builds_exact_panel_and_candidate_manifest(self) -> None:
        result = self._build()
        self.assertEqual(result["rows"], 6)
        self.assertEqual(result["date_min"], "2005-01-04")
        self.assertEqual(result["date_max"], "2026-08-03")

        parquet_glob = (self.output / "curated").resolve().as_posix() + "/**/*.parquet"
        connection = duckdb.connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM read_parquet('{parquet_glob}', hive_partitioning=true) "
                "ORDER BY trade_date, order_book_id"
            ).fetchdf()
        finally:
            connection.close()
        self.assertIn("302132.XSHE", set(rows["order_book_id"]))
        self.assertNotIn("689009.XSHG", set(rows["order_book_id"]))
        self.assertNotIn("600849.XSHG", set(rows["order_book_id"]))
        event = rows[
            (rows["order_book_id"] == "600000.XSHG")
            & (rows["trade_date"].astype(str) == "2026-07-30")
        ].iloc[0]
        non_event = rows[
            (rows["order_book_id"] == "600000.XSHG")
            & (rows["trade_date"].astype(str) == "2026-07-29")
        ].iloc[0]
        self.assertAlmostEqual(event["realized_total_return"], 10.5 * 1.1 / 10.0 - 1.0)
        self.assertEqual(event["total_return_event_factor"], 1.1)
        self.assertAlmostEqual(non_event["realized_total_return"], 10.5 / 10.0 - 1.0)
        self.assertEqual(non_event["total_return_event_factor"], 1.0)

        candidate = json.loads(
            (self.output / "CANDIDATE_MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(candidate),
            {
                "schema_version",
                "contract_id",
                "candidate_id",
                "dataset_id",
                "snapshot_id",
                "purpose_ceiling",
                "formal_source_admission",
                "formal_catalog_written",
                "research_ready",
                "artifacts",
            },
        )
        artifact_paths = [item["relative_path"].replace("\\", "/") for item in candidate["artifacts"]]
        self.assertTrue(any("/raw/" in "/" + item for item in artifact_paths))
        self.assertTrue(any("/curated/" in "/" + item for item in artifact_paths))
        self.assertFalse(any(item.endswith("CANDIDATE_MANIFEST.json") for item in artifact_paths))
        quality = json.loads(
            (self.output / "quality/summary.json").read_text(encoding="utf-8")
        )
        self.assertTrue(quality["all_checks_pass"])
        self.assertEqual(quality["payload_schema"], "r0-shsz-pv-panel-v2")
        self.assertEqual(quality["primary_key"], ["order_book_id", "trade_date"])

    def test_fails_closed_when_action_factor_key_is_missing(self) -> None:
        self._write_jsonl(
            self.actions / "aggregate/EX_FACTORS_TAIL_2025_20260803.jsonl",
            [
                {
                    "security_id": "600000.XSHG",
                    "event_date": "2026-07-29",
                    "ex_factor": 1.01,
                }
            ],
        )
        with self.assertRaisesRegex(BUILDER.BuildError, "corporate-action calibration failed"):
            self._build()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
