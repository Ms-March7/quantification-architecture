import unittest

from factor_factory.factor_dependencies import (
    classify_joinquant_definition,
    classify_local_expression,
    classify_ricequant_type,
)


class FactorDependencyTests(unittest.TestCase):
    def test_local_expression_is_exact_r0(self):
        result = classify_local_expression("safe_div(delta($close,5),delay($close,5))")
        self.assertEqual(result.dependency_level, "R0_SHSZ_PV")
        self.assertEqual(result.dependency_fields, ("close",))

    def test_joinquant_market_reference_is_blocked(self):
        result = classify_joinquant_definition(
            "alpha101", "rank(indneutralize(close, industry))", "DOCUMENTED_CALLABLE_CANDIDATE"
        )
        self.assertEqual(result.dependency_level, "R1_MARKET_REFERENCE")

    def test_joinquant_price_formula_is_candidate(self):
        result = classify_joinquant_definition(
            "alpha101", "(-1*correlation(open, volume, 10))", "DOCUMENTED_CALLABLE_CANDIDATE"
        )
        self.assertEqual(result.dependency_level, "R0_FORMULA_CANDIDATE")
        self.assertEqual(result.dependency_fields, ("open", "volume"))

    def test_empty_and_unimplemented_fail_closed(self):
        empty = classify_joinquant_definition("alpha191", "", "DOCUMENTED_CALLABLE_CANDIDATE")
        unavailable = classify_joinquant_definition("alpha101", "close", "NOT_IMPLEMENTED")
        self.assertEqual(empty.local_compute_status, "BLOCKED")
        self.assertEqual(unavailable.dependency_level, "UNIMPLEMENTED")

    def test_ricequant_types_are_conservative(self):
        self.assertEqual(classify_ricequant_type("alpha101").dependency_level, "R0_VENDOR_FACTOR_VALUE")
        self.assertEqual(classify_ricequant_type("income_statement").dependency_level, "R1_FINANCIAL_PIT")
        self.assertEqual(classify_ricequant_type("other").dependency_level, "UNKNOWN_VENDOR_OTHER")


if __name__ == "__main__":
    unittest.main()
