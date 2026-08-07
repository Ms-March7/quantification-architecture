"""Conservative dependency classification for the unified factor registry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re


R0_FIELDS = ("open", "high", "low", "close", "volume", "amount", "vwap", "returns")

_FINANCIAL_PATTERNS = (
    r"\bassets?\b", r"\bliabilit", r"\bequity\b", r"\brevenue\b",
    r"\bprofit\b", r"\bnet_income\b", r"\bcash[_ ]?flow\b", r"\bebit",
    r"\bbook[_ ]?value\b", r"\broe\b", r"\broa\b", r"\bpe\b", r"\bpb\b",
    r"\bps\b", r"\bdividend\b", r"\bearnings?\b",
)
_MARKET_REFERENCE_PATTERNS = (
    r"indneutralize", r"industry", r"sector", r"subindustry",
    r"market[_ ]?cap", r"float[_ ]?cap", r"shares?", r"turnover[_ ]?rate",
)
_EXPECTATION_PATTERNS = (r"consensus", r"forecast", r"analyst", r"expectation")
_SENTIMENT_PATTERNS = (r"sentiment", r"news", r"social", r"attention")


@dataclass(frozen=True)
class DependencyAssessment:
    dependency_level: str
    dependency_fields: tuple[str, ...]
    local_compute_status: str
    classification_basis: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def extract_r0_fields(text: str) -> tuple[str, ...]:
    lowered = text.casefold()
    found = []
    for field in R0_FIELDS:
        if re.search(rf"(?<![a-z0-9_]){re.escape(field)}(?![a-z0-9_])", lowered):
            found.append(field)
    if "money" in lowered and "amount" not in found:
        found.append("amount")
    return tuple(sorted(set(found)))


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(re.search(pattern, lowered) for pattern in patterns)


def classify_local_expression(formula: str) -> DependencyAssessment:
    fields = extract_r0_fields(formula.replace("$", ""))
    return DependencyAssessment(
        dependency_level="R0_SHSZ_PV",
        dependency_fields=fields,
        local_compute_status="LOCAL_EXPRESSION_EXECUTABLE",
        classification_basis="EXACT_LOCAL_WHITELISTED_EXPRESSION",
    )


def classify_joinquant_definition(
    namespace: str,
    formula_or_definition: str,
    implemented_status: str,
) -> DependencyAssessment:
    text = formula_or_definition.strip()
    fields = extract_r0_fields(text)
    if implemented_status == "NOT_IMPLEMENTED":
        return DependencyAssessment(
            "UNIMPLEMENTED", fields, "BLOCKED", "JOINQUANT_DOCUMENTED_NOT_IMPLEMENTED"
        )
    if not text:
        return DependencyAssessment(
            "UNKNOWN", (), "BLOCKED", "EMPTY_DEFINITION"
        )
    if _matches_any(text, _EXPECTATION_PATTERNS):
        return DependencyAssessment(
            "R1_EXPECTATION_PIT", fields, "BLOCKED_BY_R1", "KEYWORD_HEURISTIC_EXPECTATION"
        )
    if _matches_any(text, _SENTIMENT_PATTERNS):
        return DependencyAssessment(
            "R1_SENTIMENT_TIMESTAMP", fields, "BLOCKED_BY_R1", "KEYWORD_HEURISTIC_SENTIMENT"
        )
    if _matches_any(text, _FINANCIAL_PATTERNS):
        return DependencyAssessment(
            "R1_FINANCIAL_PIT", fields, "BLOCKED_BY_R1", "KEYWORD_HEURISTIC_FINANCIAL"
        )
    if _matches_any(text, _MARKET_REFERENCE_PATTERNS):
        return DependencyAssessment(
            "R1_MARKET_REFERENCE", fields, "BLOCKED_BY_R1", "KEYWORD_HEURISTIC_MARKET_REFERENCE"
        )
    if namespace in {"alpha101", "alpha191"}:
        return DependencyAssessment(
            "R0_FORMULA_CANDIDATE",
            fields,
            "PARSER_IMPLEMENTATION_REQUIRED",
            "FORMULA_PRESENT_NO_R1_KEYWORD__REVIEW_REQUIRED",
        )
    if namespace == "technical_analysis":
        return DependencyAssessment(
            "R0_TECHNICAL_CANDIDATE",
            fields,
            "VENDOR_FUNCTION_REIMPLEMENTATION_REQUIRED",
            "JOINQUANT_TECHNICAL_DOCUMENTATION",
        )
    return DependencyAssessment("UNKNOWN", fields, "REVIEW_REQUIRED", "NO_CLASSIFICATION_RULE")


def classify_ricequant_type(factor_type: str) -> DependencyAssessment:
    if factor_type in {"alpha101", "moving_average_indicator", "obos_indicator", "energy_indicator"}:
        return DependencyAssessment(
            "R0_VENDOR_FACTOR_VALUE",
            (),
            "VENDOR_VALUE_PROBE__LOCAL_FORMULA_REQUIRED",
            f"RQDATA_TYPE_{factor_type}",
        )
    if factor_type == "eod_indicator":
        return DependencyAssessment(
            "R1_VALUATION_AND_MARKET_REFERENCE",
            (),
            "VENDOR_VALUE_PROBE__PIT_REVIEW_REQUIRED",
            "RQDATA_TYPE_eod_indicator",
        )
    if factor_type in {
        "income_statement", "balance_sheet", "cash_flow_statement",
        "operational_indicator", "cash_flow_indicator", "financial_indicator",
        "growth_indicator",
    }:
        return DependencyAssessment(
            "R1_FINANCIAL_PIT",
            (),
            "VENDOR_VALUE_PROBE__PIT_REVIEW_REQUIRED",
            f"RQDATA_TYPE_{factor_type}",
        )
    return DependencyAssessment(
        "UNKNOWN_VENDOR_OTHER",
        (),
        "METADATA_VISIBLE__DEPENDENCY_REVIEW_REQUIRED",
        f"RQDATA_TYPE_{factor_type}",
    )
