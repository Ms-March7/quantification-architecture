#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / "data/market/quarantine/r0-official-calendar-rules/normalized-stagea-20260801-v2"
REPORT = ROOT / "reports/data-governance/r0-official-calendar-rules-normalized-stagea-20260801-v2"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class NormalizedStageATest(unittest.TestCase):
    def test_boundary(self):
        summary = load(CANDIDATE / "NORMALIZATION_SUMMARY.json")
        self.assertEqual(summary["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertEqual(summary["quality_status"], "NOT_ADMITTED")
        self.assertFalse(summary["formal_admission"])
        self.assertFalse(summary["research_usable"])
        self.assertFalse(summary["statutory_holiday_inference"])
        self.assertFalse(summary["weekday_open_day_inference"])

    def test_closure_rows_are_explicit_and_unique(self):
        rows = jsonl(CANDIDATE / "VENUE_DATE_CLOSURE_CANDIDATE.jsonl")
        keys = [(row["venue"], row["calendar_date"]) for row in rows]
        self.assertGreater(len(rows), 500)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual({row["venue"] for row in rows}, {"SSE", "SZSE", "BSE"})
        self.assertTrue(all(row["is_trading_day"] is False for row in rows))
        self.assertTrue(all(row["closure_evidence_only"] is True for row in rows))
        self.assertTrue(all(row["evidence"] for row in rows))

    def test_each_evidence_has_raw_hash(self):
        rows = jsonl(CANDIDATE / "VENUE_DATE_CLOSURE_CANDIDATE.jsonl")
        for row in rows:
            for evidence in row["evidence"]:
                raw = ROOT / evidence["source_raw"]["path"]
                self.assertTrue(raw.is_file())
                self.assertEqual(sha(raw), evidence["source_raw"]["sha256"])
                self.assertIsNone(evidence["available_at"])
                self.assertTrue(evidence["pit_status"].startswith("UNRESOLVED"))

    def test_rule_ledger_is_presence_only(self):
        rows = jsonl(CANDIDATE / "RULE_VERSION_CANDIDATE.jsonl")
        self.assertEqual(len(rows), 13)
        self.assertTrue(all(row["complete_version_chain"] is False for row in rows))
        self.assertTrue(all(row["effective_from"] is None for row in rows))

    def test_session_import_bindings(self):
        rows = jsonl(CANDIDATE / "SESSION_INTERVAL_CANDIDATE.jsonl")
        self.assertEqual(len(rows), 28)
        self.assertEqual({row["venue"] for row in rows}, {"SSE", "SZSE", "BSE"})
        for row in rows:
            raw = ROOT / row["raw_path"]
            notice = ROOT / row["notice_raw_path"]
            self.assertEqual(sha(raw), row["raw_sha256"])
            self.assertEqual(sha(notice), row["notice_raw_sha256"])

    def test_gate_is_fail_closed(self):
        gate = load(REPORT / "CORE02_STAGE_A_RECOMPUTE.json")
        self.assertEqual(
            {key: value["status"] for key, value in gate["gates"].items()},
            {"CORE02-Q1": "FAIL", "CORE02-Q2": "FAIL", "CORE02-Q3": "UNKNOWN", "CORE02-Q4": "PASS"},
        )
        self.assertFalse(gate["formal_admission"])
        self.assertIn("NOT_ADMITTED", gate["decision"])

    def test_bounded_search_does_not_claim_missing_evidence(self):
        receipt = load(REPORT / "BOUNDED_SEARCH_RECEIPTS.json")
        self.assertEqual(receipt["sse_2005_2008"]["annual_master_found"], [])
        self.assertEqual(receipt["bse_2021_2024"]["annual_calendar_evidence_ingested"], [
            "bse-calendar-2022", "bse-calendar-2023", "bse-calendar-2024"
        ])
        self.assertEqual(receipt["bse_2021_2024"]["temporary_closure_evidence_ingested"], [])

    def test_protection_check(self):
        protection = load(REPORT / "PROTECTION_CHECK.json")
        self.assertTrue(protection["pass"])
        self.assertEqual(protection["before"], protection["after"])

    def test_manifests_bind_exact_files(self):
        for root, name in ((CANDIDATE, "CANDIDATE_MANIFEST.json"), (REPORT, "REPORT_MANIFEST.json")):
            manifest = load(root / name)
            expected = {item["path"] for item in manifest["files"]}
            actual = {
                path.resolve().relative_to(ROOT.resolve()).as_posix()
                for path in root.rglob("*") if path.is_file() and path.name != name
            }
            self.assertEqual(expected, actual)
            for item in manifest["files"]:
                path = ROOT / item["path"]
                self.assertEqual(path.stat().st_size, item["bytes"])
                self.assertEqual(sha(path), item["sha256"])


if __name__ == "__main__":
    unittest.main()
