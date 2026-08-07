from __future__ import annotations

import codecs
import csv
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = (
    WORKSPACE_ROOT / "control" / "tools" / "parse_csmar_audit_text_candidate.py"
)
SPEC = importlib.util.spec_from_file_location(
    "parse_csmar_audit_text_candidate", TOOL_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load audit text parser: {TOOL_PATH}")
PARSER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PARSER
SPEC.loader.exec_module(PARSER)


def raw_quoted_record(values: list[str], separator: str = "\r\n") -> str:
    return '"' + '","'.join(values) + '"' + separator


class CsmarAuditTextCandidateParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qdata-audit-text-")
        self.root = Path(self.temp.name)
        self.quarantine = (
            self.root
            / "data"
            / "market"
            / "quarantine"
            / "fixture"
            / "raw"
        )
        self.quarantine.mkdir(parents=True)
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

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_zip(self, name: str, member: str, csv_text: str) -> Path:
        path = self.quarantine / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(member, codecs.BOM_UTF8 + csv_text.encode("utf-8"))
            archive.writestr("descriptor.txt", "fixture")
        return path

    def audit_fixture(self) -> Path:
        columns = list(PARSER.AUDIT_COLUMNS)
        clean = [
            "000001",
            "平安银行",
            "2024-12-31",
            "2025-03-01",
            "标准无保留意见",
            "甲",
            "事务所",
            "",
            "10.00",
            "CNY",
            "",
            "",
            "",
            "",
            "10.00",
            "CNY",
            '合法转义""引号""',
            "",
            "100001",
            "",
        ]
        unescaped = [
            "000002",
            "万科A",
            "2024-12-31",
            "",
            "",
            "乙",
            "事务所",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            '第一行 "贵公司"\n第二行',
            "",
            "100002",
            "",
        ]
        ambiguous = [
            "200057",
            "*ST 大洋B",
            "1998-12-31",
            "1999-04-23",
            "拒绝发表意见",
            "丙",
            "事务所",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            '以公司的"金鹏","金路"两条船作为抵押',
            "",
            "106941",
            "106941",
        ]
        text = (
            ",".join(columns)
            + "\r\n"
            + raw_quoted_record(clean)
            + raw_quoted_record(unescaped, "\n")
            + raw_quoted_record(ambiguous)
        )
        return self.write_zip("audit.zip", "FIN_Audit.csv", text)

    def detail_fixture(self) -> Path:
        columns = list(PARSER.AUDIT_DETAIL_COLUMNS)
        clean = [
            "000001",
            "2024-12-31",
            "平安银行",
            "1",
            "减值",
            "描述",
            "方法",
            "",
        ]
        repaired = [
            "000001",
            "2024-12-31",
            "平安银行",
            "2",
            "收入",
            "第一行\n第二行",
            "核验",
            '附注“七"所示',
        ]
        text = (
            ",".join(columns)
            + "\r\n"
            + raw_quoted_record(clean)
            + raw_quoted_record(repaired)
        )
        return self.write_zip("detail.zip", "FIN_AuditDetail.csv", text)

    def test_default_inspection_is_write_free_and_accounts_every_audit_record(
        self,
    ) -> None:
        source = self.audit_fixture()
        result = PARSER.run(
            workspace_root=self.root,
            zip_path=source,
            table="FIN_Audit",
            run_id="fixture-inspect-v1",
            output_root=None,
            execute=False,
        )
        target = (
            self.root
            / "tmp"
            / "csmar-audit-text-candidate"
            / "csmar-fin-audit"
            / "fixture-inspect-v1"
        )
        self.assertEqual(result["status"], "INSPECTION_COMPLETE")
        self.assertFalse(result["write_performed"])
        self.assertFalse(target.exists())
        self.assertEqual(result["metrics"]["anchored_record_count"], 3)
        self.assertEqual(result["metrics"]["accounted_record_count"], 3)
        self.assertEqual(result["metrics"]["output_record_count"], 3)
        self.assertEqual(result["metrics"]["strict_valid_record_count"], 1)
        self.assertEqual(result["metrics"]["repaired_record_count"], 2)
        self.assertEqual(result["metrics"]["semantic_review_record_count"], 1)
        self.assertEqual(result["metrics"]["unescaped_quote_character_count"], 6)

    def test_execute_writes_reversible_csv_and_one_sidecar_row_per_source_record(
        self,
    ) -> None:
        source = self.audit_fixture()
        before = PARSER.sha256_file(
            self.root / "data" / "catalog" / "datasets.json"
        )
        result = PARSER.run(
            workspace_root=self.root,
            zip_path=source,
            table="FIN_Audit",
            run_id="fixture-execute-v1",
            output_root=None,
            execute=True,
        )
        target = Path(result["candidate_path"])
        repaired = target / "repaired" / "FIN_Audit.csv"
        sidecar_path = target / "repair" / "records.jsonl"
        self.assertTrue(repaired.is_file())
        self.assertTrue(sidecar_path.is_file())
        self.assertEqual(
            PARSER.sha256_file(self.root / "data" / "catalog" / "datasets.json"),
            before,
        )

        with repaired.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream, strict=True))
        self.assertEqual(rows[0], list(PARSER.AUDIT_COLUMNS))
        self.assertEqual(len(rows) - 1, 3)
        self.assertEqual(rows[2][16], '第一行 "贵公司"\n第二行')
        self.assertEqual(rows[2][17], "")
        self.assertEqual(rows[3][16], '以公司的"金鹏","金路"两条船作为抵押')
        self.assertEqual(rows[3][17], "")

        sidecars = [
            json.loads(line)
            for line in sidecar_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(sidecars), 3)
        self.assertEqual(sidecars[0]["parse_status"], "CLEAN_STRICT")
        self.assertEqual(
            sidecars[1]["parse_status"], "REPAIRED_UNESCAPED_QUOTES"
        )
        self.assertTrue(sidecars[2]["semantic_review_required"])
        self.assertTrue(sidecars[1]["actions"])
        quality = json.loads(
            (target / "quality" / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(quality["admission_decision"], "NOT_ADMITTED")
        self.assertTrue(quality["semantic_review"]["required"])
        self.assertEqual(quality["semantic_review"]["record_count"], 1)
        self.assertEqual(
            quality["semantic_review"]["records"][0]["record_ordinal"], 3
        )
        self.assertEqual(
            quality["semantic_review"]["records"][0]["key"],
            {"Stkcd": "200057", "Accper": "1998-12-31"},
        )

        manifest = json.loads(
            (target / "manifest.json").read_text(encoding="utf-8")
        )
        package = target / manifest["outputs"]["provenance_zip"]["relative_path"]
        self.assertTrue(package.is_file())
        self.assertEqual(
            PARSER.sha256_file(package),
            manifest["outputs"]["provenance_zip"]["sha256"],
        )
        with zipfile.ZipFile(package) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(
                set(archive.namelist()),
                {
                    "parser-manifest.json",
                    "quality/summary.json",
                    "repair/records.jsonl",
                    "repaired/FIN_Audit.csv",
                    "upstream_provenance_receipt.json",
                },
            )
            receipt = json.loads(
                archive.read("upstream_provenance_receipt.json").decode("utf-8")
            )
            internal_manifest_bytes = archive.read("parser-manifest.json")
            packaged_quality_bytes = archive.read("quality/summary.json")
        self.assertEqual(
            receipt["artifacts"]["parser_manifest"]["sha256"],
            PARSER.sha256_bytes(internal_manifest_bytes),
        )
        self.assertEqual(
            receipt["artifacts"]["repair_sidecar"]["sha256"],
            PARSER.sha256_file(sidecar_path),
        )
        self.assertEqual(receipt["artifacts"]["repair_sidecar"]["row_count"], 3)
        self.assertEqual(
            receipt["artifacts"]["repaired_csv"]["sha256"],
            PARSER.sha256_file(repaired),
        )
        self.assertEqual(
            receipt["parser"]["script_sha256"], PARSER.sha256_file(TOOL_PATH)
        )
        self.assertEqual(receipt["semantic_review"], quality["semantic_review"])
        self.assertEqual(
            packaged_quality_bytes,
            (target / "quality" / "summary.json").read_bytes(),
        )

        request = json.loads(
            (target / "downstream-build.request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            request["include_patterns"],
            [manifest["outputs"]["provenance_zip"]["relative_path"]],
        )
        self.assertEqual(
            request["zip_member_patterns"], ["repaired/FIN_Audit.csv"]
        )
        self.assertIn(
            manifest["outputs"]["provenance_zip"]["sha256"],
            request["source_access_note"],
        )
        self.assertIn("ordinals=3", request["source_access_note"])
        self.assertEqual(request["table"]["key_columns"], ["Stkcd", "Accper"])

    def test_provenance_zip_bytes_are_deterministic_for_identical_members(
        self,
    ) -> None:
        source = self.audit_fixture()
        result = PARSER.run(
            workspace_root=self.root,
            zip_path=source,
            table="FIN_Audit",
            run_id="deterministic-v1",
            output_root=None,
            execute=True,
        )
        target = Path(result["candidate_path"])
        package = target / result["outputs"]["provenance_zip"]["relative_path"]
        with zipfile.ZipFile(package) as archive:
            members = [
                (name, archive.read(name))
                for name in reversed(archive.namelist())
            ]
        replica = self.root / "tmp" / "deterministic-replica.zip"
        replica_inventory = PARSER.write_deterministic_zip(replica, members)
        self.assertEqual(PARSER.sha256_file(replica), PARSER.sha256_file(package))
        self.assertEqual(
            replica_inventory,
            result["outputs"]["provenance_zip"]["members"],
        )

    def test_detail_multiline_and_unescaped_quote_are_repaired_without_loss(
        self,
    ) -> None:
        source = self.detail_fixture()
        result = PARSER.run(
            workspace_root=self.root,
            zip_path=source,
            table="FIN_AuditDetail",
            run_id="detail-execute-v1",
            output_root=None,
            execute=True,
        )
        self.assertEqual(result["metrics"]["anchored_record_count"], 2)
        self.assertEqual(result["metrics"]["accounted_record_count"], 2)
        self.assertEqual(result["metrics"]["repaired_record_count"], 1)
        self.assertEqual(result["metrics"]["unescaped_quote_character_count"], 1)
        repaired = (
            Path(result["candidate_path"])
            / "repaired"
            / "FIN_AuditDetail.csv"
        )
        with repaired.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream, strict=True))
        self.assertEqual(len(rows) - 1, 2)
        self.assertEqual(rows[2][5], "第一行\n第二行")
        self.assertEqual(rows[2][7], '附注“七"所示')

    def test_source_outside_quarantine_is_rejected(self) -> None:
        source = self.audit_fixture()
        outside = self.root / "outside.zip"
        outside.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(
            PARSER.AuditTextParseError,
            "below data/market/quarantine",
        ):
            PARSER.run(
                workspace_root=self.root,
                zip_path=outside,
                table="FIN_Audit",
                run_id="outside-v1",
                output_root=None,
                execute=False,
            )

    def test_output_outside_tmp_is_rejected_even_in_inspect_mode(self) -> None:
        source = self.audit_fixture()
        with self.assertRaisesRegex(PARSER.AuditTextParseError, "below.*tmp"):
            PARSER.run(
                workspace_root=self.root,
                zip_path=source,
                table="FIN_Audit",
                run_id="unsafe-output-v1",
                output_root=self.root / "data" / "market",
                execute=False,
            )

    def test_unexplained_structural_width_fails_closed(self) -> None:
        columns = list(PARSER.AUDIT_DETAIL_COLUMNS)
        broken = [
            "000001",
            "2024-12-31",
            "平安银行",
            "1",
            "分类",
            "描述",
            # Method and Location are both missing.
        ]
        source = self.write_zip(
            "broken.zip",
            "FIN_AuditDetail.csv",
            ",".join(columns) + "\r\n" + raw_quoted_record(broken),
        )
        with self.assertRaisesRegex(
            PARSER.AuditTextParseError, "structural width"
        ):
            PARSER.run(
                workspace_root=self.root,
                zip_path=source,
                table="FIN_AuditDetail",
                run_id="broken-v1",
                output_root=None,
                execute=False,
            )


if __name__ == "__main__":
    unittest.main()
