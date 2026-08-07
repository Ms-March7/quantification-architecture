"""Deterministic, transparent R0-compatible classic factor specifications.

This module builds metadata only.  It does not read market payloads, evaluate
factor values, run statistics, consume Validation, or open Holdout.  Supplier
names are provenance hints only unless a public formula passes the local
whitelist; Ricequant names are always mapped to explicit local rewrites and
are never represented as the vendor's proprietary definition.
"""

from __future__ import annotations

import ast
from collections import Counter
import csv
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .supplier_formula_engine import (
    SUPPORTED_FIELDS,
    SUPPORTED_FUNCTIONS,
    SupplierFormulaEvaluator,
)


ALLOWED_INPUTS = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "amount",
        "volume",
        "realized_total_return",
    }
)
CAUSAL_TIMING = "TRADE_DATE_D_CLOSE__FIRST_USE_NEXT_SESSION"
STATUS = "SPECIFICATIONS_BUILT__NOT_EVALUATED__NO_RESEARCH_RUN"

COMPANY_ACTION_SENSITIVITIES = frozenset(
    {
        "ACTION_SAFE__REALIZED_TOTAL_RETURN",
        "SAME_DAY_PRICE_RATIO__LOW_SENSITIVITY",
        "RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
        "VOLUME_CROSS_PERIOD__SPLIT_SENSITIVE",
        "RAW_PRICE_AND_VOLUME_CROSS_PERIOD__SPLIT_SENSITIVE",
        "AMOUNT_LIQUIDITY__ACTION_EFFECT_INDIRECT",
        "TOTAL_RETURN_WITH_AMOUNT__PRICE_ACTION_SAFE_AMOUNT_INDIRECT",
        "TOTAL_RETURN_WITH_VOLUME__VOLUME_SPLIT_SENSITIVE",
        "MIXED_PUBLIC_FORMULA__FIELD_DEPENDENT",
        "DIAGNOSTIC_ONLY__ARBITRARY_PER_SECURITY_REBASE_SENSITIVE",
    }
)

RECORD_KEYS = frozenset(
    {
        "factor_id",
        "family",
        "kind",
        "params",
        "formula",
        "inputs",
        "source_refs",
        "source_formula",
        "source_mapping",
        "company_action_sensitivity",
        "causal_timing",
        "definition_sha256",
        "legacy_512_overlap",
    }
)

KIND_PARAM_KEYS: dict[str, frozenset[str]] = {
    name: frozenset({"window"})
    for name in (
        "total_return",
        "return_reversal",
        "return_volatility",
        "downside_volatility",
        "upside_volatility",
        "realized_skewness",
        "realized_kurtosis",
        "maximum_drawdown",
        "moving_average_ratio",
        "ema_ratio",
        "breakout_position",
        "amount_mean",
        "amount_ratio",
        "amount_volatility",
        "volume_mean",
        "volume_ratio",
        "volume_volatility",
        "amihud_illiquidity",
        "zero_return_ratio",
        "return_amount_corr",
        "return_volume_corr",
        "price_volume_corr",
        "obv",
        "money_flow_volume",
        "rsi",
        "atr",
        "roc",
        "beta",
        "downside_beta",
        "market_corr",
        "residual_volatility",
        "williams_r",
    )
}
KIND_PARAM_KEYS.update(
    {
        "tail_loss": frozenset({"window", "quantile"}),
        "linear_trend": frozenset({"window", "metric"}),
        "price_location": frozenset({"window", "field"}),
        "intraday_return": frozenset({"window", "stat"}),
        "overnight_return": frozenset({"window", "stat"}),
        "range_ratio": frozenset({"window", "stat"}),
        "true_range": frozenset({"window", "stat"}),
        "shadow_ratio": frozenset({"window", "side", "stat"}),
        "macd": frozenset({"fast", "slow", "signal", "component"}),
        "ppo": frozenset({"fast", "slow", "signal", "component"}),
        "bollinger_z": frozenset({"window", "num_std"}),
        "bollinger_width": frozenset({"window", "num_std"}),
        "stochastic": frozenset({"window", "smooth", "component"}),
        "cci": frozenset({"window", "constant"}),
        "residual_momentum": frozenset({"formation_window", "beta_window"}),
        "supplier_dsl": frozenset({"vwap"}),
    }
)

ACADEMIC_REFS: dict[str, dict[str, str]] = {
    "momentum": {
        "source": "academic",
        "citation": "Jegadeesh and Titman (1993), Returns to Buying Winners and Selling Losers",
        "relation": "CLASSIC_CONCEPT__LOCAL_TRANSPARENT_SPECIFICATION",
    },
    "reversal": {
        "source": "academic",
        "citation": "Jegadeesh (1990), Evidence of Predictable Behavior of Security Returns",
        "relation": "CLASSIC_CONCEPT__LOCAL_TRANSPARENT_SPECIFICATION",
    },
    "illiquidity": {
        "source": "academic",
        "citation": "Amihud (2002), Illiquidity and Stock Returns",
        "relation": "CLASSIC_CONCEPT__LOCAL_TRANSPARENT_SPECIFICATION",
    },
    "volatility": {
        "source": "academic",
        "citation": "Realized return-distribution diagnostics; local transparent implementation",
        "relation": "CLASSIC_CONCEPT__LOCAL_TRANSPARENT_SPECIFICATION",
    },
    "beta": {
        "source": "academic",
        "citation": "Market-model beta and residual-return diagnostics",
        "relation": "CLASSIC_CONCEPT__LOCAL_TRANSPARENT_SPECIFICATION",
    },
    "technical": {
        "source": "technical_classic",
        "citation": "Wilder and standard public technical-indicator definitions",
        "relation": "CLASSIC_CONCEPT__LOCAL_TRANSPARENT_SPECIFICATION",
    },
    "local": {
        "source": "local",
        "citation": "R0 transparent price-volume research design",
        "relation": "LOCAL_TRANSPARENT_SPECIFICATION",
    },
}

RQ_NAME_MAP: dict[str, tuple[str, ...]] = {
    "ema_ratio": ("EMA10", "EMA20", "EMA60"),
    "maximum_drawdown": ("MDD20", "MDD60"),
    "obv": ("OBV",),
    "rsi": ("RSI6",),
    "atr": ("ATR",),
    "macd": ("MACD_DIFF", "MACD_DEA"),
    "bollinger_z": ("BOLL",),
    "bollinger_width": ("BOLL_UP", "BOLL_DOWN"),
    "stochastic": ("KDJ_K", "KDJ_D"),
    "williams_r": ("LWR1", "LWR2"),
    "cci": ("CCI",),
    "roc": ("ROC",),
}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    for ancestor in resolved.parents:
        if (ancestor / "AGENTS.md").is_file() and (ancestor / "data/catalog").is_dir():
            return resolved.relative_to(ancestor).as_posix()
    return resolved.as_posix()


def _formula_exact_fingerprint(formula: str) -> str:
    normalized = re.sub(r"\s+", "", formula).casefold()
    return sha256_text(normalized)


def _formula_semantic_fingerprint(formula: str) -> str:
    normalized = re.sub(r"\s+", "", formula).casefold().replace("$", "")
    aliases = {
        "ts_std(": "stddev(",
        "ts_mean(": "mean(",
        "cs_rank(": "rank(",
        "ts_corr(": "correlation(",
        "safe_div(": "divide(",
    }
    for old, new in aliases.items():
        normalized = normalized.replace(old, new)
    normalized = normalized.replace("std(", "stddev(")
    normalized = normalized.replace("+-", "-")
    return sha256_text(normalized)


def _spec_semantic_key(kind: str, params: Mapping[str, Any]) -> str:
    return canonical_json({"kind": kind, "params": dict(params)})


def _legacy_semantic_spec_keys(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Recognize audited equivalences between the old DSL and classic kinds.

    This is intentionally narrow and fail-closed.  An unrecognized old
    expression remains only in the formula-fingerprint comparison; it is never
    guessed into a semantic class.
    """

    keys: set[str] = set()
    patterns: tuple[tuple[re.Pattern[str], str, Any], ...] = (
        (
            re.compile(r"^safe_div\(delta\(\$close,(\d+)\),delay\(\$close,\1\)\)$"),
            "roc",
            lambda match: {"window": int(match.group(1))},
        ),
        (
            re.compile(r"^safe_div\(\$volume,ts_mean\(\$volume,(\d+)\)\)$"),
            "volume_ratio",
            lambda match: {"window": int(match.group(1))},
        ),
        (
            re.compile(r"^safe_div\(\$amount,ts_mean\(\$amount,(\d+)\)\)$"),
            "amount_ratio",
            lambda match: {"window": int(match.group(1))},
        ),
        (
            re.compile(r"^safe_div\(sub\(\$close,\$open\),\$open\)$"),
            "intraday_return",
            lambda match: {"window": 1, "stat": "mean"},
        ),
        (
            re.compile(r"^safe_div\(sub\(\$high,\$low\),\$close\)$"),
            "range_ratio",
            lambda match: {"window": 1, "stat": "mean"},
        ),
        (
            re.compile(
                r"^safe_div\(sub\(\$close,ts_min\(\$low,(\d+)\)\),"
                r"sub\(ts_max\(\$high,\1\),ts_min\(\$low,\1\)\)\)$"
            ),
            "breakout_position",
            lambda match: {"window": int(match.group(1))},
        ),
    )
    for row in rows:
        formula = re.sub(r"\s+", "", str(row["formula"])).casefold()
        for pattern, kind, params_builder in patterns:
            match = pattern.fullmatch(formula)
            if match:
                keys.add(_spec_semantic_key(kind, params_builder(match)))
                break
    return keys


def _definition_hash(
    *,
    kind: str,
    params: Mapping[str, Any],
    formula: str,
    inputs: Iterable[str],
    company_action_sensitivity: str,
) -> str:
    payload = {
        "kind": kind,
        "params": dict(params),
        "formula": re.sub(r"\s+", " ", formula.strip()),
        "inputs": sorted(set(inputs)),
        "company_action_sensitivity": company_action_sensitivity,
        "causal_timing": CAUSAL_TIMING,
    }
    return sha256_text(canonical_json(payload))


def _safe_prefix(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")[:34]


def _source_refs(kind: str, academic_key: str) -> list[dict[str, str]]:
    refs = [dict(ACADEMIC_REFS[academic_key])]
    if kind in RQ_NAME_MAP:
        for name in RQ_NAME_MAP[kind]:
            refs.append(
                {
                    "source": "ricequant",
                    "namespace": "public_catalog_name_observation",
                    "source_key": name,
                    "artifact": "reports/data-governance/factor-ab-capability-20260805-v1/RQ_FACTOR_METADATA.json",
                    "relation": "NAME_ONLY__LOCAL_REWRITE__NOT_VENDOR_FORMULA_EQUIVALENCE",
                }
            )
    return refs


class _Accumulator:
    def __init__(self, legacy_rows: Iterable[Mapping[str, Any]]) -> None:
        legacy_rows = list(legacy_rows)
        self.records: list[dict[str, Any]] = []
        self.by_definition: dict[str, dict[str, Any]] = {}
        self.legacy_exact = {
            _formula_exact_fingerprint(str(row["formula"])) for row in legacy_rows
        }
        self.legacy_semantic = {
            _formula_semantic_fingerprint(str(row["formula"])) for row in legacy_rows
        }
        self.legacy_semantic_specs = _legacy_semantic_spec_keys(legacy_rows)
        self.filtered_legacy_exact = 0
        self.filtered_legacy_semantic_formula = 0
        self.filtered_legacy_semantic_spec = 0

    def add(
        self,
        *,
        family: str,
        kind: str,
        params: Mapping[str, Any],
        formula: str,
        inputs: Iterable[str],
        source_refs: list[dict[str, str]],
        company_action_sensitivity: str,
        source_formula: str | None = None,
        source_mapping: str = "LOCAL_TRANSPARENT_DEFINITION",
    ) -> None:
        if kind not in KIND_PARAM_KEYS:
            raise ValueError(f"unsupported classic factor kind: {kind}")
        if set(params) != KIND_PARAM_KEYS[kind]:
            raise ValueError(
                f"invalid params for {kind}: expected {sorted(KIND_PARAM_KEYS[kind])}, "
                f"got {sorted(params)}"
            )
        normalized_inputs = sorted(set(inputs))
        if not normalized_inputs or not set(normalized_inputs) <= ALLOWED_INPUTS:
            raise ValueError(f"invalid R0 inputs for {kind}: {normalized_inputs}")
        if company_action_sensitivity not in COMPANY_ACTION_SENSITIVITIES:
            raise ValueError(f"unknown company-action sensitivity: {company_action_sensitivity}")
        if source_mapping == "LOCAL_TRANSPARENT_DEFINITION" and any(
            ref.get("source") == "ricequant" for ref in source_refs
        ):
            source_mapping = (
                "STANDARD_NAME__LOCAL_TRANSPARENT_REWRITE__"
                "NOT_VENDOR_FORMULA_EQUIVALENCE"
            )
        exact = _formula_exact_fingerprint(formula)
        semantic = _formula_semantic_fingerprint(formula)
        if exact in self.legacy_exact:
            self.filtered_legacy_exact += 1
            return
        if semantic in self.legacy_semantic:
            self.filtered_legacy_semantic_formula += 1
            return
        if _spec_semantic_key(kind, params) in self.legacy_semantic_specs:
            self.filtered_legacy_semantic_spec += 1
            return
        definition = _definition_hash(
            kind=kind,
            params=params,
            formula=formula,
            inputs=normalized_inputs,
            company_action_sensitivity=company_action_sensitivity,
        )
        if definition in self.by_definition:
            existing = self.by_definition[definition]
            known = {canonical_json(item) for item in existing["source_refs"]}
            for item in source_refs:
                if canonical_json(item) not in known:
                    existing["source_refs"].append(item)
                    known.add(canonical_json(item))
            existing["source_refs"].sort(key=canonical_json)
            return
        record = {
            "factor_id": f"r0c:{_safe_prefix(family)}:{_safe_prefix(kind)}:{definition[:16]}",
            "family": family,
            "kind": kind,
            "params": dict(params),
            "formula": formula,
            "inputs": normalized_inputs,
            "source_refs": sorted(source_refs, key=canonical_json),
            "source_formula": source_formula,
            "source_mapping": source_mapping,
            "company_action_sensitivity": company_action_sensitivity,
            "causal_timing": CAUSAL_TIMING,
            "definition_sha256": definition,
            "legacy_512_overlap": {"exact": False, "semantic": False},
        }
        self.records.append(record)
        self.by_definition[definition] = record


def _add_parametric_specs(acc: _Accumulator) -> None:
    total_windows = (1, 2, 3, 5, 10, 20, 40, 60, 90, 120, 180, 250)
    for window in total_windows:
        formula = f"prod(1 + realized_total_return, {window}) - 1"
        acc.add(
            family="momentum_trend",
            kind="total_return",
            params={"window": window},
            formula=formula,
            inputs=("realized_total_return",),
            source_refs=_source_refs("total_return", "momentum"),
            company_action_sensitivity="ACTION_SAFE__REALIZED_TOTAL_RETURN",
        )
    for window in (1, 2, 3, 5, 10, 20, 40):
        acc.add(
            family="short_term_reversal",
            kind="return_reversal",
            params={"window": window},
            formula=f"-(prod(1 + realized_total_return, {window}) - 1)",
            inputs=("realized_total_return",),
            source_refs=_source_refs("return_reversal", "reversal"),
            company_action_sensitivity="ACTION_SAFE__REALIZED_TOTAL_RETURN",
        )

    vol_windows = (5, 10, 20, 40, 60, 120, 250)
    vol_formulas = {
        "return_volatility": "rolling_std(realized_total_return, {w}, ddof=1)",
        "downside_volatility": "sqrt(mean(min(realized_total_return, 0)^2, {w}))",
        "upside_volatility": "sqrt(mean(max(realized_total_return, 0)^2, {w}))",
    }
    for kind, template in vol_formulas.items():
        for window in vol_windows:
            acc.add(
                family="volatility_tail_risk",
                kind=kind,
                params={"window": window},
                formula=template.format(w=window),
                inputs=("realized_total_return",),
                source_refs=_source_refs(kind, "volatility"),
                company_action_sensitivity="ACTION_SAFE__REALIZED_TOTAL_RETURN",
            )
    for kind, template in (
        ("realized_skewness", "rolling_skew(realized_total_return, {w}, bias=false)"),
        ("realized_kurtosis", "rolling_excess_kurtosis(realized_total_return, {w}, bias=false)"),
    ):
        for window in (10, 20, 40, 60, 120, 250):
            acc.add(
                family="volatility_tail_risk",
                kind=kind,
                params={"window": window},
                formula=template.format(w=window),
                inputs=("realized_total_return",),
                source_refs=_source_refs(kind, "volatility"),
                company_action_sensitivity="ACTION_SAFE__REALIZED_TOTAL_RETURN",
            )
    for window in (10, 20, 40, 60, 120, 250):
        acc.add(
            family="volatility_tail_risk",
            kind="maximum_drawdown",
            params={"window": window},
            formula=f"minimum_drawdown(compound_path(realized_total_return, {window}))",
            inputs=("realized_total_return",),
            source_refs=_source_refs("maximum_drawdown", "volatility"),
            company_action_sensitivity="ACTION_SAFE__REALIZED_TOTAL_RETURN",
        )
    for window in (20, 40, 60, 120, 250):
        for quantile in (0.05, 0.10):
            acc.add(
                family="volatility_tail_risk",
                kind="tail_loss",
                params={"window": window, "quantile": quantile},
                formula=(
                    f"-conditional_mean(realized_total_return, realized_total_return <= "
                    f"rolling_quantile(realized_total_return, {quantile}, {window}), {window})"
                ),
                inputs=("realized_total_return",),
                source_refs=_source_refs("tail_loss", "volatility"),
                company_action_sensitivity="ACTION_SAFE__REALIZED_TOTAL_RETURN",
            )

    trend_windows = (3, 5, 10, 20, 40, 60, 90, 120, 180, 250)
    for kind, operator in (
        ("moving_average_ratio", "sma"),
        ("ema_ratio", "ema"),
    ):
        for window in trend_windows:
            acc.add(
                family="trend_price_position",
                kind=kind,
                params={"window": window},
                formula=f"close / {operator}(close, {window}) - 1",
                inputs=("close",),
                source_refs=_source_refs(kind, "technical"),
                company_action_sensitivity="RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
            )
    for window in (5, 10, 20, 40, 60, 120, 250):
        for metric in ("slope", "r2"):
            acc.add(
                family="trend_price_position",
                kind="linear_trend",
                params={"window": window, "metric": metric},
                formula=f"ols_{metric}(log(close), time_index, {window})",
                inputs=("close",),
                source_refs=_source_refs("linear_trend", "local"),
                company_action_sensitivity="RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
            )
    for kind, formula_template, inputs in (
        (
            "breakout_position",
            "(close - rolling_min(low, {w})) / (rolling_max(high, {w}) - rolling_min(low, {w}))",
            ("close", "high", "low"),
        ),
        (
            "price_location",
            "(close - rolling_min(close, {w})) / (rolling_max(close, {w}) - rolling_min(close, {w}))",
            ("close",),
        ),
    ):
        for window in (5, 10, 20, 40, 60, 90, 120, 250):
            params = {"window": window}
            if kind == "price_location":
                params["field"] = "close"
            acc.add(
                family="trend_price_position",
                kind=kind,
                params=params,
                formula=formula_template.format(w=window),
                inputs=inputs,
                source_refs=_source_refs(kind, "local"),
                company_action_sensitivity="RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
            )

    range_windows = (1, 3, 5, 10, 20, 40, 60)
    intraday_specs = {
        "intraday_return": ("close / open - 1", ("open", "close"), "SAME_DAY_PRICE_RATIO__LOW_SENSITIVITY"),
        "overnight_return": ("open / delay(close, 1) - 1", ("open", "close"), "RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE"),
        "range_ratio": ("(high - low) / close", ("high", "low", "close"), "SAME_DAY_PRICE_RATIO__LOW_SENSITIVITY"),
        "true_range": (
            "max(high-low, abs(high-delay(close,1)), abs(low-delay(close,1))) / delay(close,1)",
            ("high", "low", "close"),
            "RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
        ),
    }
    for kind, (base, inputs, sensitivity) in intraday_specs.items():
        for window in range_windows:
            for stat in ("mean", "std"):
                # A one-observation standard deviation is intrinsically
                # constant/undefined and must not enter a research catalog.
                if window == 1 and stat == "std":
                    continue
                acc.add(
                    family="intraday_overnight_range",
                    kind=kind,
                    params={"window": window, "stat": stat},
                    formula=f"rolling_{stat}({base}, {window})",
                    inputs=inputs,
                    source_refs=_source_refs(kind, "local"),
                    company_action_sensitivity=sensitivity,
                )
    for window in range_windows:
        for side, base in (
            ("upper", "(high - max(open, close)) / (high - low)"),
            ("lower", "(min(open, close) - low) / (high - low)"),
        ):
            acc.add(
                family="intraday_overnight_range",
                kind="shadow_ratio",
                params={"window": window, "side": side, "stat": "mean"},
                formula=f"rolling_mean({base}, {window})",
                inputs=("open", "high", "low", "close"),
                source_refs=_source_refs("shadow_ratio", "local"),
                company_action_sensitivity="SAME_DAY_PRICE_RATIO__LOW_SENSITIVITY",
            )
    for window in range_windows:
        acc.add(
            family="intraday_overnight_range",
            kind="atr",
            params={"window": window},
            formula=(
                f"rolling_mean(max(high-low, abs(high-delay(close,1)), "
                f"abs(low-delay(close,1))), {window}) / close"
            ),
            inputs=("high", "low", "close"),
            source_refs=_source_refs("atr", "technical"),
            company_action_sensitivity="RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
        )

    liquidity_windows = (3, 5, 10, 20, 40, 60, 120, 250)
    liquidity_specs = {
        "amount_mean": ("log1p(mean(amount, {w}))", ("amount",), "AMOUNT_LIQUIDITY__ACTION_EFFECT_INDIRECT"),
        "amount_ratio": ("amount / mean(amount, {w})", ("amount",), "AMOUNT_LIQUIDITY__ACTION_EFFECT_INDIRECT"),
        "amount_volatility": ("rolling_std(log1p(amount), {w}, ddof=1)", ("amount",), "AMOUNT_LIQUIDITY__ACTION_EFFECT_INDIRECT"),
        "volume_mean": ("log1p(mean(volume, {w}))", ("volume",), "VOLUME_CROSS_PERIOD__SPLIT_SENSITIVE"),
        "volume_ratio": ("volume / mean(volume, {w})", ("volume",), "VOLUME_CROSS_PERIOD__SPLIT_SENSITIVE"),
        "volume_volatility": ("rolling_std(log1p(volume), {w}, ddof=1)", ("volume",), "VOLUME_CROSS_PERIOD__SPLIT_SENSITIVE"),
    }
    for kind, (template, inputs, sensitivity) in liquidity_specs.items():
        for window in liquidity_windows:
            acc.add(
                family="liquidity_price_volume",
                kind=kind,
                params={"window": window},
                formula=template.format(w=window),
                inputs=inputs,
                source_refs=_source_refs(kind, "local"),
                company_action_sensitivity=sensitivity,
            )
    for window in (5, 10, 20, 40, 60, 120, 250):
        for kind, formula, inputs, sensitivity, ref in (
            (
                "amihud_illiquidity",
                f"mean(abs(realized_total_return) / amount, {window}) * 1e8",
                ("realized_total_return", "amount"),
                "TOTAL_RETURN_WITH_AMOUNT__PRICE_ACTION_SAFE_AMOUNT_INDIRECT",
                "illiquidity",
            ),
            (
                "zero_return_ratio",
                f"mean(abs(realized_total_return) <= 1e-12, {window})",
                ("realized_total_return",),
                "ACTION_SAFE__REALIZED_TOTAL_RETURN",
                "illiquidity",
            ),
            (
                "return_amount_corr",
                f"rolling_corr(realized_total_return, log1p(amount), {window})",
                ("realized_total_return", "amount"),
                "TOTAL_RETURN_WITH_AMOUNT__PRICE_ACTION_SAFE_AMOUNT_INDIRECT",
                "local",
            ),
            (
                "return_volume_corr",
                f"rolling_corr(realized_total_return, log1p(volume), {window})",
                ("realized_total_return", "volume"),
                "TOTAL_RETURN_WITH_VOLUME__VOLUME_SPLIT_SENSITIVE",
                "local",
            ),
            (
                "price_volume_corr",
                f"rolling_corr(close, log1p(volume), {window})",
                ("close", "volume"),
                "RAW_PRICE_AND_VOLUME_CROSS_PERIOD__SPLIT_SENSITIVE",
                "local",
            ),
            (
                "obv",
                f"sum(sign(realized_total_return) * volume, {window}) / sum(volume, {window})",
                ("realized_total_return", "volume"),
                "TOTAL_RETURN_WITH_VOLUME__VOLUME_SPLIT_SENSITIVE",
                "technical",
            ),
            (
                "money_flow_volume",
                f"sum(((2*close-high-low)/(high-low))*volume, {window}) / sum(volume, {window})",
                ("high", "low", "close", "volume"),
                "VOLUME_CROSS_PERIOD__SPLIT_SENSITIVE",
                "technical",
            ),
        ):
            acc.add(
                family="liquidity_price_volume",
                kind=kind,
                params={"window": window},
                formula=formula,
                inputs=inputs,
                source_refs=_source_refs(kind, ref),
                company_action_sensitivity=sensitivity,
            )

    for window in (3, 5, 6, 10, 12, 14, 20, 24):
        acc.add(
            family="technical_indicators",
            kind="rsi",
            params={"window": window},
            formula=(
                f"100 - 100 / (1 + mean(max(realized_total_return,0),{window}) / "
                f"mean(max(-realized_total_return,0),{window}))"
            ),
            inputs=("realized_total_return",),
            source_refs=_source_refs("rsi", "technical"),
            company_action_sensitivity="ACTION_SAFE__REALIZED_TOTAL_RETURN",
            source_mapping="STANDARD_NAME__RETURN_BASED_LOCAL_REWRITE__NOT_VENDOR_EQUIVALENCE",
        )
    for window in (1, 3, 5, 10, 12, 20, 40, 60, 120, 250):
        acc.add(
            family="technical_indicators",
            kind="roc",
            params={"window": window},
            formula=f"close / delay(close, {window}) - 1",
            inputs=("close",),
            source_refs=_source_refs("roc", "technical"),
            company_action_sensitivity="RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
            source_mapping="STANDARD_NAME__LOCAL_REWRITE__NOT_VENDOR_EQUIVALENCE",
        )
    macd_sets = ((5, 20, 5), (6, 13, 5), (8, 17, 9), (12, 26, 9), (20, 40, 10))
    for kind in ("macd", "ppo"):
        for fast, slow, signal in macd_sets:
            for component in ("line", "signal", "hist"):
                if kind == "macd":
                    line = f"ema(close,{fast}) - ema(close,{slow})"
                else:
                    line = f"(ema(close,{fast}) - ema(close,{slow})) / ema(close,{slow})"
                component_formula = {
                    "line": line,
                    "signal": f"ema({line},{signal})",
                    "hist": f"({line}) - ema({line},{signal})",
                }[component]
                acc.add(
                    family="technical_indicators",
                    kind=kind,
                    params={"fast": fast, "slow": slow, "signal": signal, "component": component},
                    formula=component_formula,
                    inputs=("close",),
                    source_refs=_source_refs(kind, "technical"),
                    company_action_sensitivity="RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
                    source_mapping="STANDARD_NAME__LOCAL_REWRITE__NOT_VENDOR_EQUIVALENCE",
                )
    for kind, template in (
        ("bollinger_z", "(close - mean(close,{w})) / std(close,{w})"),
        ("bollinger_width", "(2*{k}*std(close,{w})) / mean(close,{w})"),
    ):
        for window in (5, 10, 20, 40, 60, 120):
            acc.add(
                family="technical_indicators",
                kind=kind,
                params={"window": window, "num_std": 2.0},
                formula=template.format(w=window, k=2.0),
                inputs=("close",),
                source_refs=_source_refs(kind, "technical"),
                company_action_sensitivity="RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
                source_mapping="STANDARD_NAME__LOCAL_REWRITE__NOT_VENDOR_EQUIVALENCE",
            )
    for window in (5, 9, 14, 20, 40):
        for component in ("k", "d"):
            smooth = 3
            raw = f"100*(close-min(low,{window}))/(max(high,{window})-min(low,{window}))"
            formula = raw if component == "k" else f"mean({raw},{smooth})"
            acc.add(
                family="technical_indicators",
                kind="stochastic",
                params={"window": window, "smooth": smooth, "component": component},
                formula=formula,
                inputs=("high", "low", "close"),
                source_refs=_source_refs("stochastic", "technical"),
                company_action_sensitivity="RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
                source_mapping="STANDARD_NAME__LOCAL_REWRITE__NOT_VENDOR_EQUIVALENCE",
            )
    for kind, template in (
        ("williams_r", "-100*(max(high,{w})-close)/(max(high,{w})-min(low,{w}))"),
        (
            "cci",
            "(typical_price-mean(typical_price,{w}))/(0.015*mean_abs_deviation(typical_price,{w}))",
        ),
    ):
        for window in (5, 10, 14, 20, 40, 60):
            params: dict[str, Any] = {"window": window}
            if kind == "cci":
                params["constant"] = 0.015
            acc.add(
                family="technical_indicators",
                kind=kind,
                params=params,
                formula=template.format(w=window),
                inputs=("high", "low", "close"),
                source_refs=_source_refs(kind, "technical"),
                company_action_sensitivity="RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE",
                source_mapping="STANDARD_NAME__LOCAL_REWRITE__NOT_VENDOR_EQUIVALENCE",
            )

    market_windows = (20, 40, 60, 90, 120, 250)
    market_formulas = {
        "beta": "cov(realized_total_return, equal_weight_market_return, {w}) / var(equal_weight_market_return, {w})",
        "downside_beta": "downside_cov(realized_total_return, equal_weight_market_return, {w}) / downside_var(equal_weight_market_return, {w})",
        "market_corr": "corr(realized_total_return, equal_weight_market_return, {w})",
        "residual_volatility": "std(market_model_residual(realized_total_return, equal_weight_market_return, {w}), {w})",
    }
    for kind, template in market_formulas.items():
        for window in market_windows:
            acc.add(
                family="market_beta_residual",
                kind=kind,
                params={"window": window},
                formula=template.format(w=window),
                inputs=("realized_total_return",),
                source_refs=_source_refs(kind, "beta"),
                company_action_sensitivity="ACTION_SAFE__REALIZED_TOTAL_RETURN",
                source_mapping="LOCAL_EQUAL_WEIGHT_DYNAMIC_ELIGIBLE_MARKET_RETURN",
            )
    for formation_window, beta_window in (
        (20, 60),
        (40, 60),
        (60, 120),
        (90, 120),
        (120, 250),
        (250, 250),
    ):
        acc.add(
            family="market_beta_residual",
            kind="residual_momentum",
            params={"formation_window": formation_window, "beta_window": beta_window},
            formula=(
                f"compound(market_model_residual(realized_total_return, "
                f"equal_weight_market_return, {beta_window}), {formation_window})"
            ),
            inputs=("realized_total_return",),
            source_refs=_source_refs("residual_momentum", "beta"),
            company_action_sensitivity="ACTION_SAFE__REALIZED_TOTAL_RETURN",
            source_mapping="LOCAL_EQUAL_WEIGHT_DYNAMIC_ELIGIBLE_MARKET_RETURN",
        )


def _strict_supplier_formula(formula: str) -> tuple[ast.Expression, set[str]]:
    tree = SupplierFormulaEvaluator.parse(formula)
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise ValueError("supplier call is not a simple positional whitelist call")
            if node.func.id.casefold() not in SUPPORTED_FUNCTIONS:
                raise ValueError(f"unsupported supplier function: {node.func.id}")
        elif isinstance(node, ast.Name):
            name = node.id.casefold()
            if name not in SUPPORTED_FUNCTIONS:
                if name not in SUPPORTED_FIELDS:
                    raise ValueError(f"unsupported supplier field: {node.id}")
                fields.add(name)
    return tree, fields


@dataclass(frozen=True)
class SupplierPriceScaleAssessment:
    """Proof result for arbitrary positive per-security price rebasing."""

    invariant: bool
    has_price: bool
    final_degree: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _ScaleState:
    valid: bool
    degree: Fraction | None
    has_price: bool
    reasons: tuple[str, ...] = ()


def _numeric_literal(node: ast.AST) -> Fraction | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Fraction(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _numeric_literal(node.operand)
        if value is not None:
            return -value if isinstance(node.op, ast.USub) else value
    return None


def _invalid_scale(*states: _ScaleState, reason: str) -> _ScaleState:
    reasons = {reason}
    has_price = False
    for state in states:
        has_price = has_price or state.has_price
        reasons.update(state.reasons)
    return _ScaleState(False, None, has_price, tuple(sorted(reasons)))


def _price_scale_state(node: ast.AST) -> _ScaleState:
    if isinstance(node, ast.Constant):
        return _ScaleState(True, Fraction(0), False)
    if isinstance(node, ast.Name):
        name = node.id.casefold()
        if name in {"open", "high", "low", "close", "vwap"}:
            return _ScaleState(True, Fraction(1), True)
        if name in {"returns", "volume"}:
            return _ScaleState(True, Fraction(0), False)
        return _ScaleState(False, None, False, (f"UNSUPPORTED_NAME:{name}",))
    if isinstance(node, ast.UnaryOp):
        return _price_scale_state(node.operand)
    if isinstance(node, ast.BinOp):
        left = _price_scale_state(node.left)
        right = _price_scale_state(node.right)
        if not left.valid or not right.valid or left.degree is None or right.degree is None:
            return _invalid_scale(left, right, reason="INVALID_CHILD")
        has_price = left.has_price or right.has_price
        if isinstance(node.op, (ast.Add, ast.Sub)):
            if left.degree != right.degree:
                # Adding/subtracting an exact zero preserves homogeneity.
                left_literal = _numeric_literal(node.left)
                right_literal = _numeric_literal(node.right)
                if left_literal == 0:
                    return _ScaleState(True, right.degree, has_price, right.reasons)
                if right_literal == 0:
                    return _ScaleState(True, left.degree, has_price, left.reasons)
                return _invalid_scale(left, right, reason="MIXED_PRICE_SCALE_DEGREES")
            return _ScaleState(True, left.degree, has_price)
        if isinstance(node.op, ast.Mult):
            return _ScaleState(True, left.degree + right.degree, has_price)
        if isinstance(node.op, ast.Div):
            return _ScaleState(True, left.degree - right.degree, has_price)
        if isinstance(node.op, ast.Pow):
            exponent = _numeric_literal(node.right)
            if exponent is None or right.has_price:
                return _invalid_scale(left, right, reason="NON_LITERAL_OR_PRICE_SCALED_EXPONENT")
            return _ScaleState(True, left.degree * exponent, has_price)
        return _invalid_scale(left, right, reason="UNSUPPORTED_BINARY_OPERATOR")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            return _ScaleState(False, None, False, ("NON_SIMPLE_CALL",))
        name = node.func.id.casefold()
        data_args = node.args[:1]
        if name == "correlation":
            data_args = node.args[:2]
        states = tuple(_price_scale_state(arg) for arg in data_args)
        if not states or any(not state.valid for state in states):
            return _invalid_scale(*states, reason=f"INVALID_{name.upper()}_INPUT")
        has_price = any(state.has_price for state in states)
        if name == "rank":
            child = states[0]
            if child.degree != 0:
                return _invalid_scale(child, reason="CROSS_SECTIONAL_RANK_OF_PRICE_SCALED_VALUE")
            return _ScaleState(True, Fraction(0), has_price)
        if name in {"ts_rank", "correlation"}:
            return _ScaleState(True, Fraction(0), has_price)
        if name in {"abs", "delay", "delta", "sum", "stddev", "ts_min", "ts_max"}:
            return _ScaleState(True, states[0].degree, has_price)
        return _invalid_scale(*states, reason=f"UNSUPPORTED_SCALE_FUNCTION:{name}")
    return _ScaleState(False, None, False, (f"UNSUPPORTED_AST:{type(node).__name__}",))


def supplier_price_scale_assessment(formula: str) -> SupplierPriceScaleAssessment:
    """Conservatively prove invariance to each security's arbitrary price scale.

    The proof treats cross-sectional ``rank(price_level)`` as unsafe because
    each security may have a different positive rebase constant.  Time-series
    rank and correlation are invariant when their inputs are homogeneous.
    """

    tree, _ = _strict_supplier_formula(formula)
    state = _price_scale_state(tree.body)
    invariant = bool(state.valid and state.degree == 0)
    reasons = set(state.reasons)
    if state.valid and state.degree != 0:
        reasons.add(f"FINAL_PRICE_SCALE_DEGREE:{state.degree}")
    return SupplierPriceScaleAssessment(
        invariant=invariant,
        has_price=state.has_price,
        final_degree=None if state.degree is None else str(state.degree),
        reasons=tuple(sorted(reasons)),
    )


def _supplier_sensitivity(fields: set[str], formula: str) -> str:
    scale = supplier_price_scale_assessment(formula)
    lowered = formula.casefold()
    raw_price_fields = fields & {"open", "high", "low", "close", "vwap"}
    if raw_price_fields and not scale.invariant:
        return "DIAGNOSTIC_ONLY__ARBITRARY_PER_SECURITY_REBASE_SENSITIVE"
    has_price_lag = bool(raw_price_fields) and any(
        token in lowered
        for token in ("delay(", "delta(", "sum(", "stddev(", "ts_min(", "ts_max(", "ts_rank(", "correlation(")
    )
    has_volume = "volume" in fields
    if has_price_lag and has_volume:
        return "RAW_PRICE_AND_VOLUME_CROSS_PERIOD__SPLIT_SENSITIVE"
    if has_price_lag:
        return "RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE"
    if has_volume:
        return "VOLUME_CROSS_PERIOD__SPLIT_SENSITIVE"
    if raw_price_fields:
        return "SAME_DAY_PRICE_RATIO__LOW_SENSITIVITY"
    return "MIXED_PUBLIC_FORMULA__FIELD_DEPENDENT"


def _add_joinquant_public_specs(acc: _Accumulator, path: Path) -> dict[str, int]:
    accepted = Counter()
    rejected = Counter()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in sorted(rows, key=lambda item: (item["namespace"], item["factor_name"])):
        namespace = row["namespace"].strip().casefold()
        if namespace not in {"alpha101", "alpha191"}:
            continue
        formula = row["formula_or_definition"].strip()
        if not formula:
            rejected["empty_formula"] += 1
            continue
        try:
            _, fields = _strict_supplier_formula(formula)
        except ValueError:
            rejected["outside_local_whitelist"] += 1
            continue
        inputs: set[str] = set()
        for field in fields:
            if field == "returns":
                inputs.add("realized_total_return")
            elif field == "vwap":
                inputs.update(("amount", "volume"))
            else:
                inputs.add(field)
        if not inputs or not inputs <= ALLOWED_INPUTS:
            rejected["unavailable_input"] += 1
            continue
        scale = supplier_price_scale_assessment(formula)
        sensitivity = _supplier_sensitivity(fields, formula)
        scale_mapping = (
            "PRICE_SCALE_INVARIANCE_AST_PROVED"
            if scale.invariant
            else "DIAGNOSTIC_ONLY_ARBITRARY_PER_SECURITY_REBASE"
        )
        before = len(acc.records)
        acc.add(
            family="supplier_public_price_volume",
            kind="supplier_dsl",
            params={"vwap": "amount_div_volume"},
            formula=formula,
            inputs=inputs,
            source_refs=[
                {
                    "source": "joinquant",
                    "namespace": namespace,
                    "source_key": row["factor_name"],
                    "source_url": row["source_url"],
                    "artifact": _portable_path(path),
                    "relation": "PUBLIC_FORMULA__LOCAL_WHITELIST_RECOMPUTE",
                }
            ],
            source_formula=formula,
            source_mapping=(
                "PUBLIC_FORMULA_LOCAL_RECOMPUTE__returns=realized_total_return__"
                "vwap=amount_div_volume__NOT_VENDOR_VALUE_EQUIVALENCE__"
                f"{scale_mapping}"
            ),
            company_action_sensitivity=sensitivity,
        )
        added = int(len(acc.records) > before)
        accepted[namespace] += added
        if added:
            accepted[
                "price_scale_invariant"
                if scale.invariant
                else "diagnostic_arbitrary_rebase"
            ] += 1
    return {
        **{f"accepted_{key}": value for key, value in sorted(accepted.items())},
        **{f"rejected_{key}": value for key, value in sorted(rejected.items())},
    }


def validate_catalog(catalog: Mapping[str, Any]) -> None:
    records = catalog.get("records")
    if not isinstance(records, list):
        raise ValueError("catalog records must be a list")
    if not 300 <= len(records) <= 600:
        raise ValueError(f"classic R0 catalog must contain 300-600 records; got {len(records)}")
    factor_ids: set[str] = set()
    definition_hashes: set[str] = set()
    for record in records:
        if set(record) != RECORD_KEYS:
            raise ValueError(f"invalid record keys for {record.get('factor_id')}")
        factor_id = str(record["factor_id"])
        if factor_id in factor_ids:
            raise ValueError(f"duplicate factor_id: {factor_id}")
        factor_ids.add(factor_id)
        definition = str(record["definition_sha256"])
        if definition in definition_hashes:
            raise ValueError(f"duplicate factor definition: {definition}")
        definition_hashes.add(definition)
        if record["kind"] not in KIND_PARAM_KEYS:
            raise ValueError(f"unsupported kind: {record['kind']}")
        if set(record["params"]) != KIND_PARAM_KEYS[record["kind"]]:
            raise ValueError(f"invalid params for: {factor_id}")
        if not set(record["inputs"]) <= ALLOWED_INPUTS:
            raise ValueError(f"non-R0 input in: {factor_id}")
        if not record["inputs"] or not record["formula"] or not record["source_refs"]:
            raise ValueError(f"incomplete transparent definition: {factor_id}")
        if record["company_action_sensitivity"] not in COMPANY_ACTION_SENSITIVITIES:
            raise ValueError(f"invalid company-action sensitivity: {factor_id}")
        if record["causal_timing"] != CAUSAL_TIMING:
            raise ValueError(f"invalid causal timing: {factor_id}")
        expected = _definition_hash(
            kind=record["kind"],
            params=record["params"],
            formula=record["formula"],
            inputs=record["inputs"],
            company_action_sensitivity=record["company_action_sensitivity"],
        )
        if expected != definition:
            raise ValueError(f"definition hash mismatch: {factor_id}")
        if record["legacy_512_overlap"] != {"exact": False, "semantic": False}:
            raise ValueError(f"legacy 512 overlap was not filtered: {factor_id}")
        if record["kind"] == "supplier_dsl":
            _strict_supplier_formula(str(record["formula"]))
            if record["source_formula"] != record["formula"]:
                raise ValueError(f"supplier public formula provenance mismatch: {factor_id}")
            scale = supplier_price_scale_assessment(str(record["formula"]))
            diagnostic = (
                record["company_action_sensitivity"]
                == "DIAGNOSTIC_ONLY__ARBITRARY_PER_SECURITY_REBASE_SENSITIVE"
            )
            if scale.has_price and not scale.invariant:
                if not diagnostic or "DIAGNOSTIC_ONLY_ARBITRARY" not in str(record["source_mapping"]):
                    raise ValueError(f"unsafe supplier price scale was not diagnostic-only: {factor_id}")
            elif diagnostic:
                raise ValueError(f"price-scale-invariant supplier formula mislabeled diagnostic: {factor_id}")
        elif any(ref.get("source") == "ricequant" for ref in record["source_refs"]):
            if "NOT_VENDOR" not in str(record["source_mapping"]):
                raise ValueError(f"Ricequant name mapping lacks non-equivalence warning: {factor_id}")
    if int(catalog.get("record_count", -1)) != len(records):
        raise ValueError("record_count mismatch")
    expected_content = sha256_text(canonical_json(records))
    if catalog.get("catalog_content_sha256") != expected_content:
        raise ValueError("catalog content hash mismatch")
    if catalog.get("factor_runs") != 0:
        raise ValueError("catalog builder must not run factors")
    if catalog.get("validation_consumed") is not False or catalog.get("holdout_opened") is not False:
        raise ValueError("catalog builder crossed research-stage boundaries")


def build_catalog(
    *,
    legacy_catalog_path: Path,
    joinquant_definitions_path: Path,
    ricequant_metadata_path: Path,
) -> dict[str, Any]:
    for path in (legacy_catalog_path, joinquant_definitions_path, ricequant_metadata_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    legacy = json.loads(legacy_catalog_path.read_text(encoding="utf-8"))
    legacy_rows = legacy.get("candidates")
    if not isinstance(legacy_rows, list) or len(legacy_rows) != 512:
        raise ValueError("legacy comparison catalog is not the frozen 512-candidate artifact")
    rq_metadata = json.loads(ricequant_metadata_path.read_text(encoding="utf-8"))
    factor_types = rq_metadata.get("factor_types")
    if not isinstance(factor_types, dict):
        raise ValueError("Ricequant metadata lacks factor_types")
    observed_rq_names = {
        str(name)
        for category in factor_types.values()
        if isinstance(category, dict)
        for name in category.get("names", [])
    }
    referenced_rq_names = {name for names in RQ_NAME_MAP.values() for name in names}
    # Names differ by endpoint/version.  At least the stable core must have been
    # observed; absent aliases remain documented local rewrites, not vendor facts.
    stable_core = {"ATR", "CCI", "OBV", "MDD20", "MDD60", "KDJ_K", "KDJ_D"}
    if not stable_core <= observed_rq_names:
        raise ValueError(
            f"Ricequant metadata is missing stable observed technical names: "
            f"{sorted(stable_core - observed_rq_names)}"
        )
    if not referenced_rq_names <= observed_rq_names:
        raise ValueError(
            "Ricequant name mappings include names absent from the observed metadata: "
            f"{sorted(referenced_rq_names - observed_rq_names)}"
        )

    accumulator = _Accumulator(legacy_rows)
    _add_parametric_specs(accumulator)
    supplier_summary = _add_joinquant_public_specs(accumulator, joinquant_definitions_path)
    records = sorted(accumulator.records, key=lambda item: item["factor_id"])
    family_counts = Counter(str(item["family"]) for item in records)
    kind_counts = Counter(str(item["kind"]) for item in records)
    catalog: dict[str, Any] = {
        "schema_version": 1,
        "catalog_id": "r0-shsz-pv-classic-factor-catalog-20260806-v1",
        "status": STATUS,
        "protocol_id": "full-market-layered-factor-research-c-v1.1-20260805",
        "allowed_inputs": sorted(ALLOWED_INPUTS),
        "source_artifacts": [
            {
                "role": "LEGACY_EXACT_AND_SEMANTIC_FINGERPRINT_COMPARISON_ONLY",
                "path": _portable_path(legacy_catalog_path),
                "sha256": sha256_file(legacy_catalog_path),
            },
            {
                "role": "JOINQUANT_PUBLIC_FORMULA_SOURCE",
                "path": _portable_path(joinquant_definitions_path),
                "sha256": sha256_file(joinquant_definitions_path),
            },
            {
                "role": "RICEQUANT_NAME_OBSERVATION_ONLY",
                "path": _portable_path(ricequant_metadata_path),
                "sha256": sha256_file(ricequant_metadata_path),
            },
        ],
        "record_count": len(records),
        "family_counts": dict(sorted(family_counts.items())),
        "kind_counts": dict(sorted(kind_counts.items())),
        "supplier_formula_summary": supplier_summary,
        "ricequant_name_mapping_summary": {
            "referenced_name_count": len(referenced_rq_names),
            "observed_referenced_name_count": len(referenced_rq_names & observed_rq_names),
            "mapping_contract": "NAME_ONLY__LOCAL_REWRITE__NOT_VENDOR_FORMULA_EQUIVALENCE",
        },
        "legacy_512_comparison": {
            "legacy_record_count": 512,
            "role": "FINGERPRINT_COMPARISON_ONLY__NOT_COPIED__NOT_RUN",
            "filtered_exact_count": accumulator.filtered_legacy_exact,
            "filtered_semantic_count": (
                accumulator.filtered_legacy_semantic_formula
                + accumulator.filtered_legacy_semantic_spec
            ),
            "filtered_semantic_formula_count": accumulator.filtered_legacy_semantic_formula,
            "filtered_semantic_spec_count": accumulator.filtered_legacy_semantic_spec,
            "retained_overlap_count": 0,
        },
        "catalog_content_sha256": sha256_text(canonical_json(records)),
        "factor_runs": 0,
        "validation_consumed": False,
        "holdout_opened": False,
        "records": records,
    }
    validate_catalog(catalog)
    return catalog


__all__ = [
    "ALLOWED_INPUTS",
    "CAUSAL_TIMING",
    "KIND_PARAM_KEYS",
    "STATUS",
    "SupplierPriceScaleAssessment",
    "build_catalog",
    "canonical_json",
    "sha256_file",
    "supplier_price_scale_assessment",
    "validate_catalog",
]
