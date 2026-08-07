import csv
import importlib.util
import io
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "capture_stage_a_core02_iso_mic_evidence.py"
)
SPEC = importlib.util.spec_from_file_location(
    "capture_stage_a_core02_iso_mic_evidence",
    SCRIPT,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def csv_payload(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(MODULE.REQUIRED_COLUMNS))
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def row(mic, name, *, status="ACTIVE"):
    return {
        "MIC": mic,
        "OPERATING MIC": mic,
        "OPRT/SGMT": "OPRT",
        "MARKET NAME-INSTITUTION DESCRIPTION": name,
        "ISO COUNTRY CODE (ISO 3166)": "CN",
        "STATUS": status,
        "CREATION DATE": "20000101",
        "LAST UPDATE DATE": "20260713",
        "LAST VALIDATION DATE": "20260713",
        "EXPIRY DATE": "",
    }


class MicParserTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row("XSHG", "SHANGHAI STOCK EXCHANGE"),
            row("XSHE", "SHENZHEN STOCK EXCHANGE"),
            row("BJSE", "BEIJING STOCK EXCHANGE"),
        ]

    def test_exact_three_mappings_pass(self):
        parsed = MODULE.parse_target_rows(csv_payload(self.rows))
        self.assertEqual([item["mic"] for item in parsed], [
            "XSHG",
            "XSHE",
            "BJSE",
        ])

    def test_missing_bjse_fails(self):
        with self.assertRaises(ValueError):
            MODULE.parse_target_rows(csv_payload(self.rows[:2]))

    def test_duplicate_target_fails(self):
        with self.assertRaises(ValueError):
            MODULE.parse_target_rows(
                csv_payload(self.rows + [self.rows[0]])
            )

    def test_inactive_target_fails(self):
        rows = list(self.rows)
        rows[-1] = row(
            "BJSE",
            "BEIJING STOCK EXCHANGE",
            status="EXPIRED",
        )
        with self.assertRaises(ValueError):
            MODULE.parse_target_rows(csv_payload(rows))


class PageMarkerTests(unittest.TestCase):
    def test_current_release_markers_pass(self):
        page = b"""
        <h1>Market identifier codes</h1>
        <div>13 July 2026</div>
        <div>27 July 2026</div>
        <div>10 August 2026</div>
        """
        markers = MODULE.page_release_markers(page)
        self.assertEqual(markers["publication_date"], "13 July 2026")


if __name__ == "__main__":
    unittest.main()
