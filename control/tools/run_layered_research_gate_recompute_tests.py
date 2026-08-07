#!/usr/bin/env python3
"""Regression tests for recompute_layered_research_gates.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import recompute_layered_research_gates as gate


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class LayeredResearchGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.planned_path = self.root / "planned.json"
        self.catalog_path = self.root / "catalog.json"
        self.selection_path = self.root / "selection.json"
        self.registry_path = self.root / "l2-registry.json"
        self.planned_rows = []
        for index in range(1, 21):
            self.planned_rows.append(
                {
                    "plan_id": f"CORE-{index:02d}",
                    "dataset_id": f"core-{index:02d}-dataset",
                    "gate": "G_DATA_CORE",
                }
            )
        for index in range(1, 9):
            self.planned_rows.append(
                {
                    "plan_id": f"GCTX-{index:02d}",
                    "dataset_id": f"gctx-{index:02d}-dataset",
                    "gate": "G_DATA_GLOBAL_CONTEXT",
                }
            )
        write_json(
            self.planned_path,
            {"schema_version": 1, "datasets": self.planned_rows},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_formal_catalog(self, *, overrides: dict[str, dict[str, object]] | None = None) -> dict:
        overrides = overrides or {}
        datasets = []
        for row in self.planned_rows:
            plan_id = row["plan_id"]
            payload = self.root / "payloads" / f"{plan_id}.bin"
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(f"payload:{plan_id}".encode("utf-8"))
            item = {
                "dataset_id": row["dataset_id"],
                "snapshot_id": "2026-08-01-v1",
                "store_id": "formal",
                "owner_project": "workspace",
                "physical_path": str(payload),
                "lifecycle": "frozen",
                "mutability": "immutable",
                "purpose_ceiling": "RESEARCH_ONLY",
                "license_status": "VERIFIED",
                "quality_status": "OK",
                "authority_paths": [str(payload)],
                "content_hash": gate.sha256_file(payload),
                "hash_algorithm": "sha256-file-v1",
                "consumers": ["multi-factor"],
            }
            item.update(overrides.get(plan_id, {}))
            datasets.append(item)
        return {
            "schema_version": 1,
            "updated_at": "2026-08-01T00:00:00+08:00",
            "workspace_root": str(self.root),
            "catalog_mode": "federated_logical_catalog",
            "physical_migration": False,
            "authority_precedence": ["manifest"],
            "owners": ["workspace", "multi-factor"],
            "stores": [],
            "datasets": datasets,
            "retired_datasets": [],
            "publication_contracts": [],
        }

    def make_selection(self, *, include_all: bool = True) -> dict:
        selected = {}
        if include_all:
            for row in self.planned_rows:
                selected[row["plan_id"]] = {
                    "dataset_id": row["dataset_id"],
                    "snapshot_id": "2026-08-01-v1",
                }
        return {"schema_version": 1, "selection_id": "fixture", "selected_snapshots": selected}

    def make_l2_receipts(self, catalog: dict, selection: dict) -> dict:
        write_json(self.catalog_path, catalog)
        catalog_hash = gate.sha256_file(self.catalog_path)
        by_plan = gate.validate_planned_catalog(
            json.loads(self.planned_path.read_text(encoding="utf-8"))
        )
        tier_plans = gate.tier_plan_ids(by_plan)
        registry = {"schema_version": 1, "registry_id": "fixture", "receipts": {}}
        for tier in gate.TIER_ORDER:
            bindings = gate.selected_bindings(tier_plans[tier], selection["selected_snapshots"])
            receipt = {
                "schema_version": 1,
                "receipt_type": "L2_RESEARCH_TIER_GATE",
                "research_tier": tier,
                "gate_status": "PASS",
                "catalog_sha256": catalog_hash,
                "selected_inputs_sha256": gate.canonical_sha256(bindings),
                "dataset_bindings": bindings,
                "checks": {name: "PASS" for name in gate.L2_REQUIRED_CHECKS[tier]},
                "candidate_payload_read": False,
                "uses_latest": False,
            }
            path = self.root / "receipts" / f"{tier}.json"
            write_json(path, receipt)
            registry["receipts"][tier] = {"path": str(path), "sha256": gate.sha256_file(path)}
        return registry

    def run_gate(self) -> dict:
        return gate.recompute(
            catalog_path=self.catalog_path,
            planned_path=self.planned_path,
            selection_path=self.selection_path,
            l2_registry_path=self.registry_path,
            consumer="multi-factor",
            generated_at="2026-08-01T00:00:00+08:00",
        )

    def test_all_exact_formal_inputs_and_l2_receipts_pass(self) -> None:
        catalog = self.make_formal_catalog()
        selection = self.make_selection()
        registry = self.make_l2_receipts(catalog, selection)
        write_json(self.selection_path, selection)
        write_json(self.registry_path, registry)
        result = self.run_gate()
        self.assertEqual(result["highest_passed_research_tier"], "R2_A_SHARE_GLOBAL")
        self.assertTrue(result["research_run_allowed"])
        self.assertEqual(result["layers"]["R0_PV"]["formal_dataset_pass_count"], 6)
        self.assertEqual(result["layers"]["R1_CLASSIC_CN"]["formal_dataset_pass_count"], 20)
        self.assertEqual(result["layers"]["R2_A_SHARE_GLOBAL"]["formal_dataset_pass_count"], 28)
        self.assertEqual(
            result["layers"]["R2_A_SHARE_GLOBAL"]["formal_dataset_group_counts"],
            {
                "G_DATA_CORE": {"required": 20, "passed": 20},
                "G_DATA_GLOBAL_CONTEXT": {"required": 8, "passed": 8},
            },
        )
        self.assertFalse(result["layers"]["L0_INSPECTION"]["research_allowed"])

    def test_missing_selection_and_receipts_fail_closed(self) -> None:
        write_json(self.catalog_path, self.make_formal_catalog())
        write_json(self.selection_path, self.make_selection(include_all=False))
        write_json(self.registry_path, {"schema_version": 1, "receipts": {}})
        result = self.run_gate()
        self.assertIsNone(result["highest_passed_research_tier"])
        self.assertFalse(result["research_run_allowed"])
        self.assertEqual(result["layers"]["R0_PV"]["formal_dataset_pass_count"], 0)
        self.assertIn(
            "EXACT_SNAPSHOT_SELECTION_MISSING",
            result["layers"]["R0_PV"]["dataset_evaluations"][0]["reasons"],
        )
        self.assertEqual(result["layers"]["R0_PV"]["l2_gate"]["reasons"], ["L2_RECEIPT_MISSING"])

    def test_candidate_cannot_masquerade_as_formal(self) -> None:
        catalog = self.make_formal_catalog(
            overrides={
                "CORE-01": {
                    "lifecycle": "review",
                    "purpose_ceiling": "SMOKE_ONLY",
                    "license_status": "MIXED",
                    "quality_status": "REVIEW",
                }
            }
        )
        selection = self.make_selection()
        registry = self.make_l2_receipts(catalog, selection)
        write_json(self.selection_path, selection)
        write_json(self.registry_path, registry)
        result = self.run_gate()
        core01 = result["layers"]["R0_PV"]["dataset_evaluations"][0]
        self.assertEqual(core01["status"], "FAIL")
        self.assertTrue(any(reason.startswith("LIFECYCLE_NOT_FORMAL") for reason in core01["reasons"]))
        self.assertTrue(any(reason.startswith("PURPOSE_BELOW_RESEARCH") for reason in core01["reasons"]))
        self.assertTrue(any(reason.startswith("LICENSE_NOT_FORMAL") for reason in core01["reasons"]))
        self.assertTrue(any(reason.startswith("QUALITY_NOT_OK") for reason in core01["reasons"]))
        self.assertEqual(result["layers"]["R0_PV"]["status"], "NOT_MET")

    def test_latest_snapshot_is_rejected_as_invalid_input(self) -> None:
        selection = self.make_selection()
        selection["selected_snapshots"]["CORE-01"]["snapshot_id"] = "latest"
        by_plan = gate.validate_planned_catalog(
            json.loads(self.planned_path.read_text(encoding="utf-8"))
        )
        with self.assertRaisesRegex(gate.GateError, "forbidden latest"):
            gate.validate_selection(selection, by_plan)

    def test_dataset_identity_mismatch_is_rejected(self) -> None:
        selection = self.make_selection()
        selection["selected_snapshots"]["CORE-01"]["dataset_id"] = "wrong-dataset"
        by_plan = gate.validate_planned_catalog(
            json.loads(self.planned_path.read_text(encoding="utf-8"))
        )
        with self.assertRaisesRegex(gate.GateError, "dataset_id mismatch"):
            gate.validate_selection(selection, by_plan)

    def test_l2_receipt_catalog_binding_mismatch_fails(self) -> None:
        catalog = self.make_formal_catalog()
        selection = self.make_selection()
        registry = self.make_l2_receipts(catalog, selection)
        receipt_path = Path(registry["receipts"]["R0_PV"]["path"])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["catalog_sha256"] = "0" * 64
        write_json(receipt_path, receipt)
        registry["receipts"]["R0_PV"]["sha256"] = gate.sha256_file(receipt_path)
        write_json(self.selection_path, selection)
        write_json(self.registry_path, registry)
        result = self.run_gate()
        self.assertEqual(result["layers"]["R0_PV"]["formal_dataset_gate"], "PASS")
        self.assertEqual(result["layers"]["R0_PV"]["l2_gate"]["status"], "FAIL")
        self.assertIn("L2_CATALOG_BINDING_MISMATCH", result["layers"]["R0_PV"]["l2_gate"]["reasons"])
        self.assertEqual(result["layers"]["R0_PV"]["status"], "NOT_MET")

    def test_registered_formal_identity_is_not_auto_selected(self) -> None:
        write_json(self.catalog_path, self.make_formal_catalog())
        write_json(self.selection_path, self.make_selection(include_all=False))
        write_json(self.registry_path, {"schema_version": 1, "receipts": {}})
        result = self.run_gate()
        self.assertEqual(result["catalog_plan_intersection"]["matched_plan_identities"], 28)
        self.assertEqual(result["layers"]["R0_PV"]["formal_dataset_pass_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
