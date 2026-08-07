from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from openpyxl import Workbook


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "control" / "tools" / "run_bounded_official_source_probe.py"
SPEC = importlib.util.spec_from_file_location("bounded_official_source_probe", SCRIPT)
assert SPEC and SPEC.loader
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def write_workbook(path: Path, rows: list[list[object]], *, force_a1_dimension: bool) -> None:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()
    if not force_a1_dimension:
        return

    rewritten = path.with_suffix(".rewritten.xlsx")
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(rewritten, "w") as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                text = payload.decode("utf-8")
                start = text.index("<dimension ")
                end = text.index("/>", start) + 2
                text = text[:start] + '<dimension ref="A1"/>' + text[end:]
                payload = text.encode("utf-8")
            target.writestr(item, payload)
    rewritten.replace(path)


def base_result() -> dict[str, object]:
    return {
        "probe_id": PROBE.PROBE_ORDER[1],
        "status": "PASS_FOR_BOUNDED_CAPTURE",
        "capture_validation_passed": True,
    }


class BoundedOfficialSourceProbeTests(unittest.TestCase):
    def test_incorrect_a1_dimension_with_real_rows_passes_after_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw_root = Path(directory)
            path = raw_root / "business" / "szse-a-share-current.xlsx"
            path.parent.mkdir(parents=True)
            write_workbook(
                path,
                [
                    ["A股代码", "A股简称", "A股上市日期", "公司全称"],
                    ["000001", "平安银行", "1991-04-03", "平安银行股份有限公司"],
                ],
                force_a1_dimension=True,
            )
            result = base_result()
            PROBE.enrich_result(result, raw_root)

            self.assertEqual(result["declared_worksheet_dimension"], "A1:A1")
            self.assertTrue(result["worksheet_dimensions_reset"])
            self.assertEqual(result["business_rows"], 1)
            self.assertTrue(result["required_business_fields_present"])
            self.assertEqual(result["status"], "PASS_FOR_BOUNDED_CAPTURE")

    def test_true_a1_only_workbook_fails_closed_for_zero_business_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw_root = Path(directory)
            path = raw_root / "business" / "szse-a-share-current.xlsx"
            path.parent.mkdir(parents=True)
            write_workbook(path, [], force_a1_dimension=False)
            result = base_result()
            PROBE.enrich_result(result, raw_root)

            self.assertEqual(result["business_rows"], 0)
            self.assertFalse(result["post_capture_content_validation_passed"])
            self.assertFalse(result["capture_validation_passed"])
            self.assertEqual(result["status"], "FAIL_CLOSED")

    def test_missing_required_field_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw_root = Path(directory)
            path = raw_root / "business" / "szse-a-share-current.xlsx"
            path.parent.mkdir(parents=True)
            write_workbook(
                path,
                [
                    ["A股代码", "A股简称"],
                    ["000001", "平安银行"],
                ],
                force_a1_dimension=True,
            )
            result = base_result()
            PROBE.enrich_result(result, raw_root)

            self.assertEqual(result["business_rows"], 1)
            self.assertIn("A股上市日期", result["missing_required_business_fields"])
            self.assertFalse(result["required_business_fields_present"])
            self.assertEqual(result["status"], "FAIL_CLOSED")

    def test_validate_plan_is_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            runner = workspace / "control" / "tools" / SCRIPT.name
            runner.parent.mkdir(parents=True)
            shutil.copyfile(SCRIPT, runner)
            for relative in PROBE.REQUIRED_UNCHANGED:
                path = workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")

            output = workspace / "reports" / "data-governance" / "test-probe"
            output.mkdir(parents=True)
            plan_path = output / "EXECUTION_PLAN.json"
            bound_paths = sorted(PROBE.REQUIRED_UNCHANGED | {"control/tools/" + SCRIPT.name})
            plan = {
                "schema_version": 1,
                "plan_id": "test-local-plan",
                "run_id": "test-local-run",
                "status": "ACTIVE__BOUNDED_INSPECTION_ONLY",
                "purpose_ceiling": "INSPECTION_ONLY",
                "output_directory": "reports/data-governance/test-probe",
                "execution_authorization_active": True,
                "formal_source_admission": False,
                "catalog_write_authorized": False,
                "quality_promotion_authorized": False,
                "bulk_backfill_authorized": False,
                "paid_or_credentialed_access_authorized": False,
                "network_policy": {
                    "max_calls_per_probe": 1,
                    "retry_count": 0,
                    "credentials_forbidden": True,
                    "pdf_body_downloads": 0,
                },
                "probes": [
                    {"probe_id": probe_id, "max_calls": 1}
                    for probe_id in PROBE.PROBE_ORDER
                ],
                "fail_closed_sources": [
                    {"source_id": source_id, "business_calls": 0}
                    for source_id in ("BSE_OFFICIAL", "CCER_CSMAR_INSTITUTIONAL_API")
                ],
                "authority_bindings": [
                    {
                        "path": relative,
                        "sha256": PROBE.sha256_file(workspace / relative),
                    }
                    for relative in bound_paths
                ],
            }
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            with mock.patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
                bindings = PROBE.validate_plan(plan, workspace, plan_path)
            self.assertEqual(set(bindings), set(bound_paths))


if __name__ == "__main__":
    unittest.main()
