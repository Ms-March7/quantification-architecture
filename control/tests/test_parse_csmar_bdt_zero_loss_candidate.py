from __future__ import annotations

import codecs
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = (
    WORKSPACE_ROOT
    / "control"
    / "tools"
    / "parse_csmar_bdt_zero_loss_candidate.py"
)
SPEC = importlib.util.spec_from_file_location(
    "parse_csmar_bdt_zero_loss_candidate",
    TOOL_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load BDT zero-loss parser: {TOOL_PATH}")
PARSER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PARSER
SPEC.loader.exec_module(PARSER)


def raw_quoted_record(values: list[str], separator: str = "\r\n") -> str:
    return '"' + '","'.join(values) + '"' + separator


class CsmarBdtZeroLossCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qdata-bdt-zero-loss-")
        self.root = Path(self.temp.name)
        self.downloads = self.root / "Downloads"
        self.downloads.mkdir()
        catalog = self.root / "data" / "catalog" / "datasets.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace_root": self.root.as_posix(),
                    "stores": [],
                    "datasets": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.original_specs = dict(PARSER.TABLE_SPECS)
        PARSER.TABLE_SPECS["BDT_CO"] = replace(
            PARSER.TABLE_SPECS["BDT_CO"],
            expected_record_count=2,
        )
        PARSER.TABLE_SPECS["BDT_Trdchg"] = replace(
            PARSER.TABLE_SPECS["BDT_Trdchg"],
            expected_record_count=2,
        )

    def tearDown(self) -> None:
        PARSER.TABLE_SPECS.clear()
        PARSER.TABLE_SPECS.update(self.original_specs)
        self.temp.cleanup()

    def write_zip(
        self,
        filename: str,
        *,
        table: str,
        rows: list[list[str]],
    ) -> Path:
        spec = PARSER.TABLE_SPECS[table]
        text = (
            ",".join(spec.columns)
            + "\r\n"
            + "".join(raw_quoted_record(row) for row in rows)
        )
        path = self.downloads / filename
        with zipfile.ZipFile(
            path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                spec.member_name,
                codecs.BOM_UTF8 + text.encode("utf-8"),
            )
            archive.writestr(
                spec.descriptor_member_name,
                "fixture descriptor",
            )
            archive.writestr("license.pdf", b"%PDF-fixture")
        return path

    def co_rows(self) -> list[list[str]]:
        first = [
            "600018",
            "上港旧",
            "2000-07-19",
            "107599",
            "上海旧公司",
            "1998-11-19",
            "2006-10-20",
            "G58",
            "装卸搬运",
            "G58",
            "运输",
            "SSE",
            "",
            "",
            "",
            "",
            "",
            "0",
            "0",
            "100.00",
            "普通经营范围",
        ]
        second = [
            "600018",
            "上港新",
            "2006-10-26",
            "104873",
            "上海新公司",
            "1988-10-21",
            "",
            "G55",
            "水运",
            "G55",
            "水运",
            "SSE",
            "",
            "",
            "",
            "",
            "",
            "1",
            "1",
            "200.00",
            '制造"高端"设备',
        ]
        return [first, second]

    def trdchg_rows(self) -> list[list[str]]:
        first = [
            "000001",
            "AB",
            "01",
            "旧名",
            "ST 新名",
            "2024-01-01",
            "2024-01-02",
            "连续亏损",
            "普通公告正文",
            "",
            "1",
        ]
        second = [
            "000001",
            "DB",
            "09",
            "ST 新名",
            "新名",
            "2024-02-01",
            "2024-02-02",
            "恢复正常",
            '公告将"旧名","新名"并列说明',
            "标准意见",
            "0",
        ]
        return [first, second]

    def test_default_plan_is_write_free_and_accounts_every_source_byte(
        self,
    ) -> None:
        source = self.write_zip(
            "company.zip",
            table="BDT_CO",
            rows=self.co_rows(),
        )
        source_hash = PARSER.sha256_file(source)
        result = PARSER.run(
            workspace_root=self.root,
            zip_path=source,
            input_root=self.downloads,
            table="BDT_CO",
            run_id="fixture-plan-v1",
            output_root=None,
            execute=False,
        )
        self.assertEqual(result["status"], "PLAN_COMPLETE__NO_WRITES")
        self.assertFalse(result["write_performed"])
        self.assertEqual(result["metrics"]["anchored_record_count"], 2)
        self.assertEqual(result["metrics"]["accounted_record_count"], 2)
        self.assertEqual(result["metrics"]["unaccounted_source_bytes"], 0)
        self.assertEqual(
            result["metrics"]["accounted_source_bytes"],
            result["metrics"]["source_csv_bytes"],
        )
        self.assertEqual(result["metrics"]["duplicate_key_count"], 0)
        self.assertEqual(result["metrics"]["repaired_record_count"], 1)
        self.assertEqual(PARSER.sha256_file(source), source_hash)
        self.assertFalse(Path(result["candidate_path"]).exists())

    def test_execute_writes_sidecar_raw_copy_and_zero_diff_parquet(
        self,
    ) -> None:
        source = self.write_zip(
            "company.zip",
            table="BDT_CO",
            rows=self.co_rows(),
        )
        source_hash = PARSER.sha256_file(source)
        catalog = self.root / "data" / "catalog" / "datasets.json"
        catalog_hash = PARSER.sha256_file(catalog)
        result = PARSER.run(
            workspace_root=self.root,
            zip_path=source,
            input_root=self.downloads,
            table="BDT_CO",
            run_id="fixture-execute-v1",
            output_root=None,
            execute=True,
        )
        target = Path(result["candidate_path"])
        self.assertTrue((target / "manifest.json").is_file())
        self.assertEqual(PARSER.sha256_file(source), source_hash)
        self.assertEqual(PARSER.sha256_file(catalog), catalog_hash)
        raw_copy = target / result["outputs"]["raw_zip"]["relative_path"]
        self.assertEqual(PARSER.sha256_file(raw_copy), source_hash)

        sidecar_path = (
            target / result["outputs"]["record_sidecar"]["relative_path"]
        )
        sidecars = [
            json.loads(line)
            for line in sidecar_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(sidecars), 2)
        self.assertEqual(sidecars[0]["parse_status"], "CLEAN_STRICT")
        self.assertEqual(
            sidecars[1]["parse_status"],
            "REPAIRED_UNESCAPED_QUOTES",
        )
        self.assertTrue(sidecars[1]["actions"])

        repaired = target / result["outputs"]["repaired_csv"]["relative_path"]
        with repaired.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream, strict=True))
        self.assertEqual(rows[0], list(PARSER.BDT_CO_COLUMNS))
        self.assertEqual(len(rows) - 1, 2)
        self.assertEqual(rows[2][-1], '制造"高端"设备')

        roundtrip = result["parquet_roundtrip"]
        self.assertEqual(roundtrip["csv_row_count"], 2)
        self.assertEqual(roundtrip["parquet_row_count"], 2)
        self.assertEqual(roundtrip["csv_minus_parquet_rows"], 0)
        self.assertEqual(roundtrip["parquet_minus_csv_rows"], 0)
        self.assertEqual(roundtrip["duplicate_key_rows"], 0)
        self.assertEqual(
            result["parser_script"]["sha256"],
            PARSER.sha256_file(TOOL_PATH),
        )

    def test_trdchg_literal_content_separators_are_reversible_and_flagged(
        self,
    ) -> None:
        source = self.write_zip(
            "changes.zip",
            table="BDT_Trdchg",
            rows=self.trdchg_rows(),
        )
        result = PARSER.run(
            workspace_root=self.root,
            zip_path=source,
            input_root=self.downloads,
            table="BDT_Trdchg",
            run_id="trdchg-execute-v1",
            output_root=None,
            execute=True,
        )
        self.assertEqual(result["metrics"]["anchored_record_count"], 2)
        self.assertEqual(
            result["metrics"]["literal_separator_merge_record_count"],
            1,
        )
        self.assertEqual(result["semantic_review"]["record_count"], 1)
        self.assertEqual(result["metrics"]["duplicate_key_count"], 0)
        target = Path(result["candidate_path"])
        repaired = target / result["outputs"]["repaired_csv"]["relative_path"]
        with repaired.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream, strict=True))
        self.assertEqual(
            rows[2][8],
            '公告将"旧名","新名"并列说明',
        )
        sidecar_path = (
            target / result["outputs"]["record_sidecar"]["relative_path"]
        )
        sidecars = [
            json.loads(line)
            for line in sidecar_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(sidecars[1]["semantic_review_required"])
        self.assertIn(
            "MERGE_LITERAL_FIELD_SEPARATORS_IN_CONTENT",
            {action["action"] for action in sidecars[1]["actions"]},
        )
        self.assertEqual(
            result["parquet_roundtrip"]["status"],
            "PASS_ZERO_BIDIRECTIONAL_DIFFERENCE",
        )

    def test_source_outside_explicit_input_root_is_rejected(self) -> None:
        source = self.write_zip(
            "company.zip",
            table="BDT_CO",
            rows=self.co_rows(),
        )
        outside = self.root / "outside.zip"
        outside.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(
            PARSER.BdtZeroLossError,
            "below the explicit input root",
        ):
            PARSER.run(
                workspace_root=self.root,
                zip_path=outside,
                input_root=self.downloads,
                table="BDT_CO",
                run_id="outside-v1",
                output_root=None,
                execute=False,
            )

    def test_duplicate_business_key_and_unreviewed_row_count_fail_closed(
        self,
    ) -> None:
        duplicate_rows = self.co_rows()
        duplicate_rows[1][2] = duplicate_rows[0][2]
        duplicate_source = self.write_zip(
            "duplicate.zip",
            table="BDT_CO",
            rows=duplicate_rows,
        )
        with self.assertRaisesRegex(
            PARSER.BdtZeroLossError,
            "duplicate business keys",
        ):
            PARSER.run(
                workspace_root=self.root,
                zip_path=duplicate_source,
                input_root=self.downloads,
                table="BDT_CO",
                run_id="duplicate-v1",
                output_root=None,
                execute=False,
            )

        short_source = self.write_zip(
            "short.zip",
            table="BDT_Trdchg",
            rows=self.trdchg_rows()[:1],
        )
        with self.assertRaisesRegex(
            PARSER.BdtZeroLossError,
            "reviewed expectation",
        ):
            PARSER.run(
                workspace_root=self.root,
                zip_path=short_source,
                input_root=self.downloads,
                table="BDT_Trdchg",
                run_id="short-v1",
                output_root=None,
                execute=False,
            )


if __name__ == "__main__":
    unittest.main()
