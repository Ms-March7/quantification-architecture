from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = WORKSPACE_ROOT / "control" / "tools" / "build_downloaded_dataset.py"
SPEC = importlib.util.spec_from_file_location("build_downloaded_dataset", TOOL_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load local dataset builder: {TOOL_PATH}")
BUILDER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUILDER
SPEC.loader.exec_module(BUILDER)

try:
    import duckdb  # type: ignore
except ImportError:
    duckdb = None


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class DownloadedDatasetBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qdata-downloaded-builder-")
        self.root = Path(self.temp.name)
        self.catalog = self.root / "data" / "catalog" / "datasets.json"
        write_json(
            self.catalog,
            {
                "schema_version": 1,
                "workspace_root": self.root.as_posix(),
                "stores": [],
                "datasets": [],
            },
        )
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        (self.inbox / "part-a.csv").write_text(
            "Stkcd,Trddt,Clsprc\n"
            "000001,2026-01-02,10.5\n"
            "600000,2026-02-03,20.25\n",
            encoding="utf-8",
            newline="\n",
        )
        with zipfile.ZipFile(self.inbox / "part-b.zip", "w") as archive:
            archive.writestr(
                "nested/part-b.csv",
                "Stkcd,Trddt,Clsprc\n300001,2026-02-04,30.75\n",
            )
        self.request_value = {
            "schema_version": 1,
            "mode": "local_download_candidate",
            "dataset_id": "test-daily",
            "run_id": "fixture-v1",
            "source_id": "TEST_MANUAL_EXPORT",
            "dataset_schema_version": "candidate-v1",
            "authorized_local_research_download": True,
            "source_access_note": "test fixture authorization",
            "include_patterns": ["*.csv", "**/*.csv", "*.zip", "**/*.zip"],
            "zip_member_patterns": ["*.csv", "**/*.csv"],
            "table": {
                "table_name": "daily_candidate",
                "expected_columns": ["Stkcd", "Trddt", "Clsprc"],
                "column_types": {
                    "Stkcd": "VARCHAR",
                    "Trddt": "DATE",
                    "Clsprc": "DOUBLE",
                },
                "date_column": "Trddt",
                "key_columns": ["Stkcd", "Trddt"],
                "required_columns": ["Stkcd", "Trddt", "Clsprc"],
                "delimiter": ",",
                "encoding": "utf-8",
                "allow_extra_columns": False,
                "partition_mode": "year_month",
            },
            "limits": {
                "max_packages": 10,
                "max_csv_sources": 10,
                "max_archive_members": 10,
                "max_total_input_bytes": 10_000_000,
                "max_total_unpacked_bytes": 10_000_000,
                "max_compression_ratio": 100,
                "max_rows": 100,
            },
        }
        self.request = self.root / "request.json"
        write_json(self.request, self.request_value)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_builder(self, *, execute: bool) -> dict:
        return BUILDER.run(
            workspace_root=self.root,
            request_path=self.request,
            inbox=self.inbox,
            output_root=None,
            execute=execute,
        )

    def test_default_plan_is_write_free_and_discovers_csv_inside_zip(self) -> None:
        before = BUILDER.sha256_file(self.catalog)
        result = self.run_builder(execute=False)
        target = (
            self.root
            / "tmp"
            / "local-dataset-build"
            / "test-daily"
            / "fixture-v1"
        )
        self.assertEqual(result["status"], "PLAN_READY")
        self.assertFalse(result["write_performed"])
        self.assertEqual(result["package_count"], 2)
        self.assertEqual(result["csv_source_count"], 2)
        self.assertFalse(target.exists())
        self.assertEqual(BUILDER.sha256_file(self.catalog), before)

    @unittest.skipIf(duckdb is None, "DuckDB is required for the execute test")
    def test_execute_writes_partitioned_candidate_without_catalog_change(self) -> None:
        before = BUILDER.sha256_file(self.catalog)
        result = self.run_builder(execute=True)
        target = Path(result["candidate_path"])
        self.assertEqual(result["status"], "CANDIDATE_BUILT")
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["partition_count"], 2)
        self.assertEqual(BUILDER.sha256_file(self.catalog), before)
        self.assertEqual(result["catalog_sha256_after"], before)
        self.assertTrue(
            (
                target
                / "curated"
                / "schema=candidate-v1"
                / "year=2026"
                / "month=01"
                / "part-000.parquet"
            ).is_file()
        )
        self.assertTrue(
            (
                target
                / "curated"
                / "schema=candidate-v1"
                / "year=2026"
                / "month=02"
                / "part-000.parquet"
            ).is_file()
        )
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        quality = json.loads(
            (target / "quality" / "summary.json").read_text(encoding="utf-8")
        )
        report = (target / "REPORT.md").read_text(encoding="utf-8")
        self.assertFalse(manifest["catalog_registered"])
        self.assertFalse(manifest["source_admission"])
        self.assertFalse(manifest["research_ready"])
        self.assertEqual(manifest["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertEqual(quality["mechanical_status"], "PASS")
        self.assertEqual(quality["admission_decision"], "NOT_ADMITTED")
        self.assertTrue(report.isascii())
        parquet_glob = (
            target
            / "curated"
            / "schema=candidate-v1"
            / "year=*"
            / "month=*"
            / "*.parquet"
        ).as_posix()
        count = duckdb.connect(":memory:").execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [parquet_glob]
        ).fetchone()[0]
        self.assertEqual(count, 3)

    @unittest.skipIf(duckdb is None, "DuckDB is required for the execute test")
    def test_duplicate_business_key_fails_without_final_candidate(self) -> None:
        (self.inbox / "part-a.csv").write_text(
            "Stkcd,Trddt,Clsprc\n"
            "000001,2026-01-02,10.5\n"
            "000001,2026-01-02,11.0\n",
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(
            BUILDER.CandidateBuildError, "duplicate business keys"
        ):
            self.run_builder(execute=True)
        target = (
            self.root
            / "tmp"
            / "local-dataset-build"
            / "test-daily"
            / "fixture-v1"
        )
        self.assertFalse(target.exists())
        self.assertEqual(
            list(target.parent.glob(".fixture-v1.partial-*")),
            [],
        )

    def test_unsafe_zip_member_is_rejected_during_plan(self) -> None:
        with zipfile.ZipFile(self.inbox / "unsafe.zip", "w") as archive:
            archive.writestr("../escape.csv", "Stkcd,Trddt,Clsprc\n1,2026-01-01,1\n")
        with self.assertRaisesRegex(BUILDER.CandidateBuildError, "unsafe ZIP"):
            self.run_builder(execute=False)

    def test_execute_requires_explicit_local_research_authorization(self) -> None:
        self.request_value["authorized_local_research_download"] = False
        write_json(self.request, self.request_value)
        with self.assertRaisesRegex(
            BUILDER.CandidateBuildError,
            "authorized_local_research_download=true",
        ):
            self.run_builder(execute=True)

    def test_checked_in_csmar_templates_and_intake_manifest_validate(self) -> None:
        contract = (
            WORKSPACE_ROOT
            / "data"
            / "catalog"
            / "contracts"
            / "local_download_build_v1"
        )
        for name in (
            "csmar_trd_co.request.template.json",
            "csmar_trd_dalyr.request.template.json",
            "csmar_trd_cptl.request.template.json",
            "csmar_trd_capchg.request.template.json",
            "csmar_trd_nolimit.request.template.json",
            "csmar_trd_co.current-pilot.request.json",
            "csmar_trd_dalyr.current-pilot.request.json",
        ):
            value = json.loads((contract / name).read_text(encoding="utf-8"))
            config = BUILDER.validate_request(value, for_execute=False)
            self.assertIn(config["table"]["partition_mode"], {"single", "year_month"})
        locked_action_templates = {
            "csmar_trd_cptl.request.template.json": [
                "Stkcd",
                "Disttyp",
                "Annodt",
                "Exdistdt",
                "Paydt",
                "Amount",
                "Roprc",
                "Markettype",
            ],
            "csmar_trd_capchg.request.template.json": [
                "Stkcd",
                "Shrchgdt",
                "Shrtyp",
                "Nshrttl",
                "Nshrstt",
                "Nshrlpd",
                "Nshrlpf",
                "Nshrlpn",
                "Nshremp",
                "Nshrmf",
                "Nshrrot",
                "Nshrprf",
                "Nshra",
                "Nshrb",
                "Nshrh",
                "Nshrunl",
                "Nshrsms",
                "Nshrglea",
                "Nshrsina",
                "Nshrlpo",
                "Nshroft",
            ],
            "csmar_trd_nolimit.request.template.json": [
                "TradingDate",
                "Symbol",
                "Reason",
            ],
        }
        for name, expected_columns in locked_action_templates.items():
            value = json.loads((contract / name).read_text(encoding="utf-8"))
            config = BUILDER.validate_request(value, for_execute=False)
            self.assertFalse(value["authorized_local_research_download"])
            self.assertEqual(value["table"]["expected_columns"], expected_columns)
            exact_source = BUILDER.CsvSource(
                package_relative_path="fixture.zip",
                member_name="fixture.csv",
                source_ref="zip:fixture.zip!fixture.csv",
                uncompressed_bytes=1,
                compressed_bytes=1,
                encoding="utf-8-sig",
                delimiter=",",
                header=list(expected_columns),
            )
            self.assertEqual(
                BUILDER.validate_headers([exact_source], config),
                expected_columns,
            )
            reordered_source = BUILDER.CsvSource(
                package_relative_path="fixture.zip",
                member_name="fixture.csv",
                source_ref="zip:fixture.zip!fixture.csv",
                uncompressed_bytes=1,
                compressed_bytes=1,
                encoding="utf-8-sig",
                delimiter=",",
                header=expected_columns[1:] + expected_columns[:1],
            )
            with self.assertRaisesRegex(
                BUILDER.CandidateBuildError,
                "does not exactly match",
            ):
                BUILDER.validate_headers([reordered_source], config)
            with self.assertRaisesRegex(
                BUILDER.CandidateBuildError,
                "authorized_local_research_download=true",
            ):
                BUILDER.validate_request(value, for_execute=True)
        intake = json.loads(
            (contract / "csmar.intake-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["namePhy"] for item in intake["tables"]],
            ["TRD_Co", "TRD_Dalyr"],
        )
        self.assertFalse(intake["authorization"]["secrets_present"])
        self.assertFalse(intake["execution"]["network_access"])

    def test_single_partition_allows_null_date_column(self) -> None:
        self.request_value["table"]["partition_mode"] = "single"
        self.request_value["table"]["date_column"] = None
        self.request_value["table"]["column_types"]["Trddt"] = "VARCHAR"
        config = BUILDER.validate_request(self.request_value, for_execute=False)
        self.assertIsNone(config["table"]["date_column"])

    def test_year_month_partition_rejects_null_date_column(self) -> None:
        self.request_value["table"]["date_column"] = None
        with self.assertRaisesRegex(
            BUILDER.CandidateBuildError,
            "year_month partitioning requires",
        ):
            BUILDER.validate_request(self.request_value, for_execute=False)

    @unittest.skipIf(duckdb is None, "DuckDB is required for the execute test")
    def test_single_partition_mode_supports_reference_table(self) -> None:
        self.request_value["run_id"] = "fixture-single-v1"
        self.request_value["table"]["partition_mode"] = "single"
        write_json(self.request, self.request_value)
        result = self.run_builder(execute=True)
        target = Path(result["candidate_path"])
        self.assertEqual(result["partition_count"], 1)
        self.assertTrue(
            (
                target
                / "curated"
                / "schema=candidate-v1"
                / "part-000.parquet"
            ).is_file()
        )
        partitions = json.loads(
            (target / "partition-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(partitions["partition_scheme"], ["single"])


if __name__ == "__main__":
    unittest.main()
