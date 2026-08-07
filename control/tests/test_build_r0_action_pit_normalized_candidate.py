import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "tools" / "build_r0_action_pit_normalized_candidate.py"
SPEC = importlib.util.spec_from_file_location("r0_action", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)


class R0ActionPitCandidateTests(unittest.TestCase):
    def sample(self, title="实施公告"):
        return {
            "announcement_id": "123",
            "security_code": "000001",
            "security_name": "样本",
            "organization_id": "org",
            "announcement_title": title,
            "published_date_candidate": "2020-01-02",
            "published_at_candidate_asia_shanghai": "2020-01-02T00:00:00+08:00",
            "associate_announcement_source_value": "456",
            "lifecycle_query_matches": [{"lifecycle_role": "IMPLEMENTATION"}],
            "raw_record_sha256": "a" * 64,
            "source_url": "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            "adjunct_url_source_value": "finalpage/123.PDF",
        }

    def test_unknown_pit_fields_remain_null(self):
        row = MOD.normalize_row(self.sample())
        for field in ("permanent_security_id", "available_at", "effective_at", "supersedes", "economic_terms"):
            self.assertIsNone(row[field])

    def test_related_id_is_not_promoted_to_supersedes(self):
        row = MOD.normalize_row(self.sample())
        self.assertEqual(row["related_source_ids_observed"], ["456"])
        self.assertIsNone(row["supersedes"])

    def test_title_is_diagnostic_only(self):
        row = MOD.normalize_row(self.sample("更正公告（已取消）"))
        self.assertEqual(row["withdrawal_state"], "UNKNOWN")
        self.assertEqual(row["lifecycle_title_hint"], "WITHDRAWAL_OR_CANCELLATION_TITLE_HINT_ONLY")

    def test_midnight_candidate_is_not_available_at(self):
        row = MOD.normalize_row(self.sample())
        self.assertEqual(row["published_at_precision"], "DATE_ONLY_OR_MIDNIGHT_RISK")
        self.assertIsNone(row["available_at"])

    def test_roles_are_deduplicated_and_sorted(self):
        sample = self.sample()
        sample["lifecycle_query_matches"] = [
            {"lifecycle_role": "REVISION"},
            {"lifecycle_role": "IMPLEMENTATION"},
            {"lifecycle_role": "REVISION"},
        ]
        row = MOD.normalize_row(sample)
        self.assertEqual(row["lifecycle_query_roles"], ["IMPLEMENTATION", "REVISION"])

    def test_required_identifiers_fail_closed(self):
        sample = self.sample()
        sample["security_code"] = ""
        with self.assertRaises(ValueError):
            MOD.normalize_row(sample)

    def test_write_jsonl_is_deterministic_utf8(self):
        row = MOD.normalize_row(self.sample())
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.jsonl"
            MOD.write_jsonl(path, [row])
            parsed = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["security_name_observed"], "样本")


if __name__ == "__main__":
    unittest.main()
