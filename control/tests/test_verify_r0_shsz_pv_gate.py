from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "control" / "tools" / "verify_r0_shsz_pv_gate.py"
CONTRACT_PATH = (
    WORKSPACE
    / "data"
    / "catalog"
    / "contracts"
    / "r0_shsz_pv_v1"
    / "contract.json"
)
SPEC = importlib.util.spec_from_file_location("verify_r0_shsz_pv_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def protected_state() -> dict[str, bytes]:
    paths = {
        "datasets": WORKSPACE / "data" / "catalog" / "datasets.json",
        "roadmap": WORKSPACE / "control" / "ROADMAP.md",
        "registry": (
            WORKSPACE
            / "data"
            / "catalog"
            / "contracts"
            / "source_admission_v1"
            / "current_source_registry.json"
        ),
    }
    return {name: path.read_bytes() for name, path in paths.items()}


class VerifyR0ShszPvGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_contract_is_independent_and_scope_is_frozen(self):
        self.assertEqual(self.contract["gate_id"], "R0_SHSZ_PV")
        self.assertFalse(self.contract["independence"]["canonical_gate_modified"])
        self.assertFalse(self.contract["independence"]["canonical_gate_satisfied_by_this_contract"])
        self.assertEqual(self.contract["scope"]["venues"], ["XSHG", "XSHE"])
        self.assertEqual(self.contract["scope"]["window_start"], "2005-01-01")
        self.assertEqual(self.contract["scope"]["window_end"], "2024-12-31")
        self.assertEqual(self.contract["scope"]["excluded_conflict_key_count"], 375)
        self.assertFalse(self.contract["scope"]["bjse_in_scope"])

    def test_live_evidence_integrity_passes_but_gate_remains_fail_closed(self):
        before = protected_state()
        result = MODULE.verify_gate(WORKSPACE, self.contract)
        after = protected_state()
        self.assertEqual(before, after)
        self.assertEqual(result["evidence_integrity_verdict"], "PASS")
        self.assertEqual(result["research_gate_verdict"], "FAIL")
        self.assertEqual(result["overall_verdict"], "FAIL")
        self.assertEqual(result["current_gate_state"], "FAIL_CLOSED")
        self.assertEqual(MODULE.exit_code_for(result), 2)
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["factor_runs"], 0)

    def test_core04_core05_core06_are_never_reported_as_pass(self):
        result = MODULE.verify_gate(WORKSPACE, self.contract)
        for core in ("CORE-04", "CORE-05", "CORE-06"):
            self.assertEqual(
                result["per_core"][core]["research_gate_status"],
                "FAIL_CLOSED__NOT_ADMITTED",
            )
        self.assertEqual(result["gate_conditions"]["core04_complete_revision_chain"], "FAIL")
        self.assertEqual(result["gate_conditions"]["core04_strict_action_rows"], "FAIL")
        self.assertEqual(result["gate_conditions"]["core05_strict_projection_inputs"], "FAIL")
        self.assertEqual(result["gate_conditions"]["core06_open_execution"], "FAIL")

    def test_manifest_hash_drift_fails_integrity(self):
        changed = copy.deepcopy(self.contract)
        changed["evidence_manifests"]["price_volume"]["sha256"] = "0" * 64
        result = MODULE.verify_gate(WORKSPACE, changed)
        self.assertEqual(result["evidence_integrity_verdict"], "FAIL")
        self.assertEqual(result["research_gate_verdict"], "FAIL")
        self.assertEqual(MODULE.exit_code_for(result), 3)


if __name__ == "__main__":
    unittest.main()
