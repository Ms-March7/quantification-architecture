#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / "data/market/quarantine/r0-official-calendar-rules/minimum-gap-closure-20260801-v1"
REPORT = ROOT / "reports/data-governance/r0-official-calendar-rules-minimum-gap-closure-20260801-v1"


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


class MinimumGapEvidenceTest(unittest.TestCase):
    def test_boundary(self):
        summary = load(CANDIDATE / "SUMMARY.json")
        self.assertEqual(summary["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertEqual(summary["quality_status"], "NOT_ADMITTED")
        self.assertFalse(summary["formal_admission"])
        self.assertFalse(summary["research_usable"])
        self.assertTrue(summary["official_sources_only"])
        self.assertFalse(summary["statutory_holiday_inference"])
        self.assertFalse(summary["weekday_open_day_inference"])

    def test_network_receipts_bind_raw(self):
        receipts = load(REPORT / "NETWORK_REQUEST_RECEIPTS.json")
        self.assertEqual(len(receipts), 17)
        self.assertGreaterEqual(sum(r["success"] for r in receipts), 16)
        for receipt in receipts:
            raw = ROOT / receipt["raw"]["path"]
            self.assertTrue(raw.is_file())
            self.assertEqual(raw.stat().st_size, receipt["raw"]["bytes"])
            self.assertEqual(sha(raw), receipt["raw"]["sha256"])

    def test_sse_archive_scan_is_bounded(self):
        scan = load(REPORT / "SSE_2005_2008_ARCHIVE_SCAN_RECEIPT.json")
        self.assertEqual(scan["archive_page_count_fetched"], 6)
        self.assertEqual(scan["archive_entry_count"], 83)
        self.assertEqual(scan["earliest_publication_date_visible"], "2013-09-11")
        self.assertEqual(scan["target_annual_master_matches"], {str(y): [] for y in range(2005, 2009)})
        self.assertFalse(scan["nonexistence_claimed"])

    def test_explicit_temporary_events(self):
        rows = jsonl(CANDIDATE / "TEMPORARY_CLOSURE_EVIDENCE.jsonl")
        self.assertEqual({(r["venue"], r["event_date"]) for r in rows}, {
            ("SSE", "2008-05-19"), ("SSE", "2022-12-06"), ("SZSE", "2022-12-06")
        })
        self.assertTrue(all(r["semantic_tokens_all_present"] for r in rows))
        coverage = load(REPORT / "TEMPORARY_EMERGENCY_COVERAGE.json")
        self.assertEqual(coverage["bse_actual_full_market_event_evidence"], [])
        self.assertFalse(coverage["complete_three_venue_event_archive"])

    def test_historical_rules_hash_verified_but_chain_open(self):
        rows = jsonl(CANDIDATE / "HISTORICAL_SESSION_RULE_ENTRANCES.jsonl")
        self.assertEqual(len(rows), 8)
        self.assertEqual({r["venue"] for r in rows}, {"SSE", "SZSE", "BSE"})
        self.assertTrue(all(r["upstream_hash_verified"] for r in rows))
        self.assertTrue(all(r["complete_version_chain"] is False for r in rows))

    def test_bse_2021_remains_partial(self):
        row = load(REPORT / "BSE_2021_PARTIAL_WINDOW_EVIDENCE.json")
        self.assertEqual(row["market_effective_date"], "2021-11-15")
        self.assertFalse(row["annual_closure_master_found"])
        self.assertFalse(row["complete_daily_open_closed_calendar"])

    def test_gate_remains_fail_closed(self):
        gate = load(REPORT / "CORE02_GATE_RECOMPUTE.json")
        self.assertEqual({k: v["status"] for k, v in gate["gates"].items()}, {
            "CORE02-Q1": "FAIL", "CORE02-Q2": "FAIL", "CORE02-Q3": "UNKNOWN", "CORE02-Q4": "PASS"
        })
        self.assertFalse(gate["formal_admission"])

    def test_protection_and_manifests(self):
        protection = load(REPORT / "PROTECTION_CHECK.json")
        self.assertTrue(protection["pass"])
        self.assertEqual(protection["before"], protection["after"])
        for root, name in ((CANDIDATE, "CANDIDATE_MANIFEST.json"), (REPORT, "REPORT_MANIFEST.json")):
            manifest = load(root / name)
            actual = {p.resolve().relative_to(ROOT.resolve()).as_posix() for p in root.rglob("*") if p.is_file() and p.name != name}
            self.assertEqual({x["path"] for x in manifest["files"]}, actual)
            for item in manifest["files"]:
                self.assertEqual(sha(ROOT / item["path"]), item["sha256"])


if __name__ == "__main__":
    unittest.main()
