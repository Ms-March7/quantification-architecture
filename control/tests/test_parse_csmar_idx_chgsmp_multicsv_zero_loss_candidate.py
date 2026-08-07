from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = (
    WORKSPACE_ROOT
    / "control"
    / "tools"
    / "parse_csmar_idx_chgsmp_multicsv_zero_loss_candidate.py"
)
SPEC = importlib.util.spec_from_file_location(
    "parse_csmar_idx_chgsmp_multicsv_zero_loss_candidate",
    TOOL_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load multi-CSV IDX_Chgsmp parser: {TOOL_PATH}")
PARSER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PARSER
SPEC.loader.exec_module(PARSER)


class CsmarIdxChgsmpMultiCsvZeroLossCandidateTests(unittest.TestCase):
    def test_reviewed_live_package_plans_are_bound_and_write_free(self) -> None:
        downloads = Path.home() / "Downloads"
        for package_id, binding in PARSER.PACKAGE_BINDINGS.items():
            with self.subTest(package_id=package_id):
                source = downloads / binding.zip_name
                if not source.is_file():
                    self.skipTest(
                        f"reviewed local export is unavailable: {source}"
                    )
                catalog_before = PARSER.catalog_hash(WORKSPACE_ROOT)
                result = PARSER.run(
                    workspace_root=WORKSPACE_ROOT,
                    package_id=package_id,
                    zip_path=source,
                    input_root=downloads,
                    run_id=f"unit-plan-{package_id}",
                    output_root=None,
                    execute=False,
                )
                self.assertEqual(
                    result["status"],
                    "BOUND_PLAN_COMPLETE__NO_PARSE__NO_WRITES",
                )
                self.assertFalse(result["write_performed"])
                self.assertEqual(
                    result["source"]["zip_sha256"],
                    binding.zip_sha256,
                )
                self.assertEqual(
                    sum(
                        int(member.row_count or 0)
                        for member in binding.csv_members
                    ),
                    binding.expected_rows,
                )
                self.assertEqual(
                    PARSER.catalog_hash(WORKSPACE_ROOT),
                    catalog_before,
                )
                self.assertFalse(Path(result["candidate_path"]).exists())

    def test_exact_013_duplicate_filename_is_rejected(self) -> None:
        downloads = Path.home() / "Downloads"
        binding = PARSER.PACKAGE_BINDINGS["013710529"]
        duplicates = sorted(downloads.glob("*013710529* (1).zip"))
        if not duplicates:
            self.skipTest("reviewed exact duplicate is unavailable")
        with self.assertRaises(PARSER.IdxChgsmpMultiCsvError):
            PARSER.require_input_location(
                duplicates[0],
                downloads,
                binding,
            )

    def test_stream_parser_repairs_only_quote_defects_reversibly(self) -> None:
        header = (
            "\ufeff"
            '"Indexcd","Chgsmp01","Chgsmp02","Chgsmp03",'
            '"Chgsmp04","Chgsmp05","Chgsmp06","Chgsmp07"\r\n'
        )
        clean = (
            '"000001","2021-01-04","600000","浦发银行",'
            '"1","1","2021-01-01","SSE"\r\n'
        )
        repaired = (
            '"000001","2021-01-05","Z1","产品"龙鑫"固收",'
            '"1","7","2021-01-02",""\r\n'
        )
        raw = (header + clean + repaired).encode("utf-8")
        member = PARSER.MemberBinding(
            name="IDX_Chgsmp.csv",
            bytes=len(raw),
            compressed_bytes=0,
            crc32="00000000",
            sha256=hashlib.sha256(raw).hexdigest(),
            row_count=2,
            repaired_record_count=1,
            quote_repair_count=2,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "source.zip"
            with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr(member.name, raw)
            canonical = root / "canonical.csv"
            sidecar = io.StringIO()
            accumulator = PARSER.PackageAccumulator()
            with zipfile.ZipFile(archive_path) as archive:
                metrics = PARSER.parse_member_to_canonical(
                    archive=archive,
                    member=member,
                    canonical_path=canonical,
                    repair_sidecar=sidecar,
                    accumulator=accumulator,
                    package_ordinal_start=0,
                )
            with canonical.open(
                "r", encoding="utf-8-sig", newline=""
            ) as stream:
                rows = list(csv.reader(stream, strict=True))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[2][3], '产品"龙鑫"固收')
            self.assertEqual(rows[2][7], "")
            self.assertEqual(metrics["unaccounted_source_bytes"], 0)
            self.assertEqual(metrics["repaired_source_record_count"], 1)
            self.assertEqual(metrics["quote_repair_count"], 2)
            self.assertEqual(accumulator.repair_sidecar_rows, 1)
            self.assertIn("INSERT_QUOTE", sidecar.getvalue())

    def test_output_root_cannot_escape_dedicated_tmp_tree(self) -> None:
        with self.assertRaises(PARSER.IdxChgsmpMultiCsvError):
            PARSER.require_output_location(
                WORKSPACE_ROOT,
                WORKSPACE_ROOT / "tmp" / "another-candidate",
                "unsafe-target",
            )


if __name__ == "__main__":
    unittest.main()
