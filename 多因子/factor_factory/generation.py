"""Deterministic grammar search, mutation, and factor lineage."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Iterable, Sequence

from .expressions import (
    ALLOWED_WINDOWS,
    Expression,
    expression_price_scale_exponent,
    expression_unit,
    validate_expression,
)


WINDOWS = tuple(value for value in ALLOWED_WINDOWS if value != 1)
UNARY_OPS = ("neg", "abs", "signed_log1p", "cs_rank", "cs_zscore")
BINARY_OPS = ("add", "sub", "mul", "safe_div")
ROLLING_OPS = ("delay", "delta", "ts_mean", "ts_std", "ts_min", "ts_max", "ts_rank")


@dataclass(frozen=True)
class FactorCandidate:
    candidate_id: str
    expression: Expression
    generation: int
    parents: tuple[str, ...] = ()
    origin: str = "grammar"

    @property
    def formula(self) -> str:
        return self.expression.canonical()


def make_candidate(
    expression: Expression,
    generation: int = 0,
    parents: Sequence[str] = (),
    origin: str = "grammar",
) -> FactorCandidate:
    formula = expression.canonical()
    identifier = hashlib.sha256(formula.encode("utf-8")).hexdigest()[:16]
    return FactorCandidate(identifier, expression, generation, tuple(parents), origin)


def _paths(expression: Expression, prefix: tuple[int, ...] = ()) -> list[tuple[int, ...]]:
    result = [prefix]
    for index, child in enumerate(expression.children):
        result.extend(_paths(child, prefix + (index,)))
    return result


def _at_path(expression: Expression, path: tuple[int, ...]) -> Expression:
    value = expression
    for index in path:
        value = value.children[index]
    return value


def _replace_path(
    expression: Expression, path: tuple[int, ...], replacement: Expression
) -> Expression:
    if not path:
        return replacement
    index = path[0]
    children = list(expression.children)
    children[index] = _replace_path(children[index], path[1:], replacement)
    return Expression(expression.op, tuple(children), expression.field, expression.window)


def seed_expressions(available_fields: Iterable[str]) -> list[Expression]:
    fields = set(available_fields)
    close = Expression.terminal("close")
    open_price = Expression.terminal("open")
    volume = Expression.terminal("volume")
    returns_1 = Expression("safe_div", (Expression("delta", (close,), window=1), Expression("delay", (close,), window=1)))
    overnight = Expression(
        "safe_div",
        (
            Expression("sub", (open_price, Expression("delay", (close,), window=1))),
            Expression("delay", (close,), window=1),
        ),
    )
    expressions: list[Expression] = [
        Expression("safe_div", (Expression("sub", (close, open_price)), open_price)),
        Expression("cs_rank", (returns_1,)),
        overnight,
        Expression("neg", (overnight,)),
    ]
    for window in (3, 5, 10, 20, 40, 60):
        momentum = Expression(
            "safe_div",
            (Expression("delta", (close,), window=window), Expression("delay", (close,), window=window)),
        )
        expressions.extend([
            momentum,
            Expression("neg", (momentum,)),
            Expression("ts_std", (returns_1,), window=window),
            Expression("safe_div", (volume, Expression("ts_mean", (volume,), window=window))),
            Expression("ts_corr", (returns_1, volume), window=window),
            Expression(
                "sub",
                (
                    Expression("cs_rank", (momentum,)),
                    Expression("cs_rank", (Expression("ts_std", (returns_1,), window=window),)),
                ),
            ),
        ])
    if {"high", "low"}.issubset(fields):
        high = Expression.terminal("high")
        low = Expression.terminal("low")
        expressions.append(Expression("safe_div", (Expression("sub", (high, low)), close)))
        for window in (5, 10, 20, 60):
            expressions.append(Expression(
                "safe_div",
                (
                    Expression("sub", (close, Expression("ts_min", (low,), window=window))),
                    Expression(
                        "sub",
                        (
                            Expression("ts_max", (high,), window=window),
                            Expression("ts_min", (low,), window=window),
                        ),
                    ),
                ),
            ))
    if "amount" in fields:
        amount = Expression.terminal("amount")
        for window in (5, 20, 60):
            amount_ratio = Expression(
                "safe_div", (amount, Expression("ts_mean", (amount,), window=window))
            )
            expressions.extend([
                amount_ratio,
                Expression("ts_rank", (amount_ratio,), window=window),
                Expression("ts_corr", (returns_1, amount_ratio), window=window),
            ])
    return expressions


class FactorGenerator:
    def __init__(
        self,
        fields: Sequence[str],
        seed: int = 20260715,
        max_depth: int = 3,
        required_output_unit: tuple[int, int] = (0, 0),
    ) -> None:
        self.fields = tuple(sorted(set(fields)))
        self.random = random.Random(seed)
        self.max_depth = max(1, int(max_depth))
        self.required_output_unit = required_output_unit

    def _random_expression_unchecked(self, remaining: int) -> Expression:
        if remaining <= 0 or self.random.random() < 0.24:
            return Expression.terminal(self.random.choice(self.fields))
        choice = self.random.random()
        if choice < 0.25:
            return Expression(self.random.choice(UNARY_OPS), (self._random_expression_unchecked(remaining - 1),))
        if choice < 0.67:
            return Expression(
                self.random.choice(ROLLING_OPS),
                (self._random_expression_unchecked(remaining - 1),),
                window=self.random.choice(WINDOWS),
            )
        if choice < 0.92:
            return Expression(
                self.random.choice(BINARY_OPS),
                (self._random_expression_unchecked(remaining - 1), self._random_expression_unchecked(remaining - 1)),
            )
        return Expression(
            "ts_corr",
            (self._random_expression_unchecked(remaining - 1), self._random_expression_unchecked(remaining - 1)),
            window=self.random.choice(WINDOWS),
        )

    def random_expression(self, depth: int | None = None) -> Expression:
        remaining = self.max_depth if depth is None else depth
        for _ in range(100):
            candidate = self._random_expression_unchecked(remaining)
            if (
                expression_unit(candidate) == self.required_output_unit
                and expression_price_scale_exponent(candidate) == 0
            ):
                return candidate
        return Expression("ts_rank", (Expression.terminal("close"),), window=5)

    def initial_population(self, size: int) -> list[FactorCandidate]:
        if size < 1:
            raise ValueError("population size must be positive")
        candidates = [
            make_candidate(expr, origin="seed")
            for expr in seed_expressions(self.fields)
            if (
                expr.depth <= self.max_depth
                and expression_unit(expr) == self.required_output_unit
                and expression_price_scale_exponent(expr) == 0
            )
        ]
        seen = {candidate.candidate_id for candidate in candidates}
        attempts = 0
        while len(candidates) < size and attempts < size * 30:
            attempts += 1
            candidate = make_candidate(self.random_expression(), origin="grammar")
            if candidate.candidate_id not in seen:
                seen.add(candidate.candidate_id)
                candidates.append(candidate)
        if len(candidates) < size:
            raise RuntimeError(
                f"factor grammar produced only {len(candidates)} unique candidates; requested {size}"
            )
        return candidates[:size]

    def offspring(
        self,
        parents: Sequence[FactorCandidate],
        size: int,
        generation: int,
        seen_ids: set[str] | None = None,
    ) -> list[FactorCandidate]:
        if not parents:
            return []
        result: list[FactorCandidate] = []
        seen = set(seen_ids or ()) | {parent.candidate_id for parent in parents}
        attempts = 0
        while len(result) < size and attempts < size * 40:
            attempts += 1
            left = self.random.choice(parents)
            if len(parents) > 1 and self.random.random() < 0.35:
                right = self.random.choice(parents)
                left_path = self.random.choice(_paths(left.expression))
                right_path = self.random.choice(_paths(right.expression))
                expression = _replace_path(
                    left.expression, left_path, _at_path(right.expression, right_path)
                )
                parent_ids = (left.candidate_id, right.candidate_id)
                origin = "crossover"
            else:
                path = self.random.choice(_paths(left.expression))
                remaining = max(0, self.max_depth - len(path))
                replacement = self.random_expression(remaining)
                expression = _replace_path(
                    left.expression, path, replacement
                )
                parent_ids = (left.candidate_id,)
                origin = "mutation"
            candidate = make_candidate(expression, generation, parent_ids, origin)
            try:
                validate_expression(expression, available_fields=set(self.fields), max_depth=self.max_depth)
            except (KeyError, ValueError):
                continue
            if (
                expression_unit(expression) == self.required_output_unit
                and expression_price_scale_exponent(expression) == 0
                and candidate.candidate_id not in seen
                and len(candidate.formula) <= 900
            ):
                seen.add(candidate.candidate_id)
                result.append(candidate)
        if len(result) < size:
            raise RuntimeError(
                f"factor offspring search produced only {len(result)} unique candidates; requested {size}"
            )
        return result
