from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from factor_factory.r0_classic_specs import (
    ALLOWED_INPUTS,
    CAUSAL_TIMING,
    KIND_PARAM_KEYS,
    RQ_NAME_MAP,
    build_catalog,
    canonical_json,
    supplier_price_scale_assessment,
    validate_catalog,
)


class R0ClassicSpecsTests(unittest.TestCase):
    def fixture(
        self,
        root: Path,
        *,
        first_legacy_formula: str = "legacy_formula_000",
    ) -> tuple[Path, Path, Path]:
        legacy = root / "legacy.json"
        candidates = [
            {
                "candidate_id": f"legacy_{index:03d}",
                "formula": first_legacy_formula if index == 0 else f"legacy_formula_{index:03d}",
            }
            for index in range(512)
        ]
        legacy.write_text(
            json.dumps({"candidate_count": 512, "candidates": candidates}),
            encoding="utf-8",
        )

        jq = root / "jq.csv"
        with jq.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "namespace",
                    "factor_name",
                    "formula_or_definition",
                    "source_url",
                ),
            )
            writer.writeheader()
            writer.writerow(
                {
                    "namespace": "alpha101",
                    "factor_name": "alpha_101",
                    "formula_or_definition": "((close - open) / ((high - low) + .001))",
                    "source_url": "https://www.joinquant.com/data/dict/alpha101",
                }
            )
            writer.writerow(
                {
                    "namespace": "alpha101",
                    "factor_name": "unsafe",
                    "formula_or_definition": "__import__('os').system('whoami')",
                    "source_url": "https://www.joinquant.com/data/dict/alpha101",
                }
            )

        rq = root / "rq.json"
        rq.write_text(
            json.dumps(
                {
                    "factor_types": {
                        "technical": {
                            "names": sorted(
                                {name for names in RQ_NAME_MAP.values() for name in names}
                            )
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return legacy, jq, rq

    def build(self, root: Path, **kwargs: str) -> dict[str, object]:
        legacy, jq, rq = self.fixture(root, **kwargs)
        return build_catalog(
            legacy_catalog_path=legacy,
            joinquant_definitions_path=jq,
            ricequant_metadata_path=rq,
        )

    def test_build_is_deterministic_and_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.build(root)
            second = self.build(root)
        self.assertEqual(first, second)
        self.assertEqual(first["record_count"], 401)
        self.assertEqual(first["factor_runs"], 0)
        self.assertFalse(first["validation_consumed"])
        self.assertFalse(first["holdout_opened"])
        self.assertEqual(
            first["catalog_content_sha256"],
            hashlib.sha256(canonical_json(first["records"]).encode("utf-8")).hexdigest(),
        )

    def test_records_have_closed_schema_hashes_and_r0_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build(Path(tmp))
        validate_catalog(catalog)
        records = catalog["records"]
        self.assertEqual(len({row["factor_id"] for row in records}), len(records))
        self.assertEqual(len({row["definition_sha256"] for row in records}), len(records))
        for row in records:
            self.assertLessEqual(set(row["inputs"]), ALLOWED_INPUTS)
            self.assertEqual(set(row["params"]), KIND_PARAM_KEYS[row["kind"]])
            self.assertEqual(row["causal_timing"], CAUSAL_TIMING)
            self.assertEqual(row["legacy_512_overlap"], {"exact": False, "semantic": False})

    def test_catalog_excludes_intrinsically_constant_one_day_std(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build(Path(tmp))
        forbidden = [
            row
            for row in catalog["records"]
            if row["params"].get("window") == 1 and row["params"].get("stat") == "std"
        ]
        self.assertEqual(forbidden, [])

    def test_p0_transparent_formula_contract_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build(Path(tmp))
        records = catalog["records"]
        for row in records:
            kind = row["kind"]
            params = row["params"]
            window = params.get("window")
            formula = row["formula"]
            if kind == "tail_loss":
                quantile = params["quantile"]
                self.assertEqual(
                    formula,
                    f"-conditional_mean(realized_total_return, realized_total_return <= "
                    f"rolling_quantile(realized_total_return, {quantile}, {window}), {window})",
                )
            elif kind == "breakout_position":
                self.assertEqual(
                    formula,
                    f"(close - rolling_min(low, {window})) / "
                    f"(rolling_max(high, {window}) - rolling_min(low, {window}))",
                )
            elif kind == "price_location":
                self.assertEqual(
                    formula,
                    f"(close - rolling_min(close, {window})) / "
                    f"(rolling_max(close, {window}) - rolling_min(close, {window}))",
                )
            elif kind in {"amount_volatility", "volume_volatility"}:
                field = kind.removesuffix("_volatility")
                self.assertEqual(
                    formula, f"rolling_std(log1p({field}), {window}, ddof=1)"
                )
            elif kind == "return_amount_corr":
                self.assertEqual(
                    formula,
                    f"rolling_corr(realized_total_return, log1p(amount), {window})",
                )
            elif kind == "return_volume_corr":
                self.assertEqual(
                    formula,
                    f"rolling_corr(realized_total_return, log1p(volume), {window})",
                )
            elif kind == "price_volume_corr":
                self.assertEqual(
                    formula, f"rolling_corr(close, log1p(volume), {window})"
                )
            elif kind == "realized_skewness":
                self.assertEqual(
                    formula,
                    f"rolling_skew(realized_total_return, {window}, bias=false)",
                )
            elif kind == "realized_kurtosis":
                self.assertEqual(
                    formula,
                    f"rolling_excess_kurtosis(realized_total_return, {window}, bias=false)",
                )
            elif kind == "atr":
                self.assertEqual(
                    formula,
                    f"rolling_mean(max(high-low, abs(high-delay(close,1)), "
                    f"abs(low-delay(close,1))), {window}) / close",
                )
            elif kind == "macd":
                line = f"ema(close,{params['fast']}) - ema(close,{params['slow']})"
                expected = {
                    "line": line,
                    "signal": f"ema({line},{params['signal']})",
                    "hist": f"({line}) - ema({line},{params['signal']})",
                }[params["component"]]
                self.assertEqual(formula, expected)
                self.assertNotIn("/ close", formula)

    def test_unsafe_supplier_formula_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build(Path(tmp))
        summary = catalog["supplier_formula_summary"]
        self.assertEqual(summary["accepted_alpha101"], 1)
        self.assertEqual(summary["rejected_outside_local_whitelist"], 1)
        suppliers = [row for row in catalog["records"] if row["kind"] == "supplier_dsl"]
        self.assertEqual(len(suppliers), 1)
        self.assertNotIn("__import__", suppliers[0]["formula"])
        self.assertEqual(
            suppliers[0]["company_action_sensitivity"],
            "DIAGNOSTIC_ONLY__ARBITRARY_PER_SECURITY_REBASE_SENSITIVE",
        )
        self.assertIn("DIAGNOSTIC_ONLY_ARBITRARY", suppliers[0]["source_mapping"])

    def test_same_day_supplier_price_scale_proof_is_conservative(self) -> None:
        invariant = (
            "(close-open)/open",
            "(high-low)/close",
            "(close-low)/(high-low)",
            "(high-open)/(high-low)",
            "(open-low)/(high-low)",
            "rank((close-open)/open)",
            "abs((close-open)/open)",
            "((low-close)*(open^5))/((low-high)*(close^5))",
            "(close+high+low)/(open+high+low)",
        )
        diagnostic = (
            "(close+high+low)/3",
            "((high*low)^0.5)-vwap",
            "rank(vwap-close)/rank(vwap+close)",
            "(close-open)/((high-low)+.001)",
            "rank(open)",
            "close-open",
        )
        for formula in invariant:
            with self.subTest(formula=formula):
                assessment = supplier_price_scale_assessment(formula)
                self.assertTrue(assessment.has_price)
                self.assertTrue(assessment.invariant, assessment.reasons)
                self.assertEqual(assessment.final_degree, "0")
        for formula in diagnostic:
            with self.subTest(formula=formula):
                assessment = supplier_price_scale_assessment(formula)
                self.assertTrue(assessment.has_price)
                self.assertFalse(assessment.invariant)

    def test_company_action_sensitivity_is_definition_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build(Path(tmp))
        record = catalog["records"][0]
        record["company_action_sensitivity"] = (
            "DIAGNOSTIC_ONLY__ARBITRARY_PER_SECURITY_REBASE_SENSITIVE"
        )
        with self.assertRaisesRegex(ValueError, "definition hash mismatch"):
            validate_catalog(catalog)

    def test_ricequant_names_are_never_claimed_as_vendor_formulas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build(Path(tmp))
        mapped = [
            row
            for row in catalog["records"]
            if any(ref.get("source") == "ricequant" for ref in row["source_refs"])
        ]
        self.assertGreater(len(mapped), 0)
        for row in mapped:
            self.assertIsNone(row["source_formula"])
            self.assertIn("NOT_VENDOR", row["source_mapping"])

    def test_exact_legacy_512_formula_is_filtered(self) -> None:
        formula = "prod(1 + realized_total_return, 1) - 1"
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build(Path(tmp), first_legacy_formula=formula)
        comparison = catalog["legacy_512_comparison"]
        self.assertEqual(comparison["filtered_exact_count"], 1)
        self.assertEqual(comparison["retained_overlap_count"], 0)
        self.assertFalse(any(row["formula"] == formula for row in catalog["records"]))

    def test_semantic_legacy_512_formula_is_filtered(self) -> None:
        legacy_formula = "rolling_skew($realized_total_return,20,bias=false)"
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build(Path(tmp), first_legacy_formula=legacy_formula)
        comparison = catalog["legacy_512_comparison"]
        self.assertEqual(comparison["filtered_exact_count"], 0)
        self.assertEqual(comparison["filtered_semantic_count"], 1)
        self.assertFalse(
            any(
                row["kind"] == "realized_skewness" and row["params"] == {"window": 20}
                for row in catalog["records"]
            )
        )

    def test_old_dsl_equivalent_kind_and_params_are_filtered(self) -> None:
        legacy_formula = "safe_div(delta($close,20),delay($close,20))"
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build(Path(tmp), first_legacy_formula=legacy_formula)
        comparison = catalog["legacy_512_comparison"]
        self.assertEqual(comparison["filtered_semantic_spec_count"], 1)
        self.assertFalse(
            any(
                row["kind"] == "roc" and row["params"] == {"window": 20}
                for row in catalog["records"]
            )
        )

    def test_missing_required_ricequant_observations_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy, jq, rq = self.fixture(root)
            rq.write_text(json.dumps({"factor_types": {"technical": {"names": []}}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stable observed technical names"):
                build_catalog(
                    legacy_catalog_path=legacy,
                    joinquant_definitions_path=jq,
                    ricequant_metadata_path=rq,
                )

    def test_validation_rejects_boundary_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build(Path(tmp))
        catalog["factor_runs"] = 1
        with self.assertRaisesRegex(ValueError, "must not run factors"):
            validate_catalog(catalog)


if __name__ == "__main__":
    unittest.main()
