from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools/build_ricequant_r1_index_market_candidate.py"
SPEC = importlib.util.spec_from_file_location("rq_r1_index_market", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RicequantIndexMarketCandidateTests(unittest.TestCase):
    def test_contract_is_inspection_only_and_bounded(self) -> None:
        import json

        contract = json.loads(MODULE.CONTRACT.read_text(encoding="utf-8"))
        self.assertFalse(contract["output"]["formal_admission"])
        self.assertFalse(contract["output"]["research_use"])
        self.assertLessEqual(contract["limits"]["maximum_business_api_calls_including_quota"], 5)

    def test_canonical_rows_hash_is_order_sensitive_after_normalization(self) -> None:
        left = [{"a": 1}, {"a": 2}]
        right = list(reversed(left))
        self.assertNotEqual(MODULE.canonical_rows_hash(left), MODULE.canonical_rows_hash(right))

    def test_clean_scalar_converts_missing_to_empty(self) -> None:
        import numpy as np

        self.assertEqual(MODULE.clean_scalar(np.nan), "")


if __name__ == "__main__":
    unittest.main()
