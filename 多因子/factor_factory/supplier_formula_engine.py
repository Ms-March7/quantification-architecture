"""Whitelisted evaluator for a bounded subset of documented supplier formulas.

This is intentionally separate from the generative factor DSL.  It accepts
only arithmetic plus a small, audited set of causal Alpha101 operators.  It
never executes arbitrary Python and never imports vendor factor values.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from .accelerator import rolling_corr_gpu, rolling_gpu
from .expressions import _cross_sectional, _delay, _rolling, _rolling_corr


SUPPORTED_FUNCTIONS = frozenset(
    {
        "abs",
        "correlation",
        "delay",
        "delta",
        "rank",
        "stddev",
        "sum",
        "ts_max",
        "ts_min",
        "ts_rank",
    }
)
SUPPORTED_FIELDS = frozenset(
    {"open", "high", "low", "close", "volume", "returns", "vwap"}
)


def _finite(values: Any) -> Any:
    if np.isscalar(values):
        return values
    array = np.asarray(values)
    return np.where(np.isfinite(array), array, np.nan).astype(np.float32, copy=False)


def _window(value: Any) -> int:
    if isinstance(value, np.ndarray) or isinstance(value, bool):
        raise ValueError("rolling window must be a scalar number")
    numeric = float(value)
    rounded = int(round(numeric))
    if not np.isfinite(numeric) or rounded < 1 or abs(numeric - rounded) > 1e-9:
        raise ValueError(f"rolling window must be a positive integer; got {value!r}")
    return rounded


def _rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    source = np.asarray(values, dtype=np.float64)
    result = np.full(source.shape, np.nan, dtype=np.float32)
    if window > len(source):
        return result
    valid = np.isfinite(source)
    minimum = max(2, int(np.ceil(window * 0.80)))
    zero_f = np.zeros((1, source.shape[1]), dtype=np.float64)
    zero_i = np.zeros((1, source.shape[1]), dtype=np.int32)
    cumulative = np.vstack((zero_f, np.cumsum(np.where(valid, source, 0.0), axis=0)))
    counts = np.vstack((zero_i, np.cumsum(valid, axis=0, dtype=np.int32)))
    rolled = cumulative[window:] - cumulative[:-window]
    rolled_counts = counts[window:] - counts[:-window]
    result[window - 1 :] = np.where(rolled_counts >= minimum, rolled, np.nan).astype(
        np.float32, copy=False
    )
    return result


@dataclass
class SupplierFormulaEvaluator:
    """Evaluate one documented formula against an explicit field mapping."""

    fields: Mapping[str, np.ndarray]
    eligible: np.ndarray | None = None
    accelerator: str = "cpu"
    gpu_column_chunk: int = 128
    cache: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.accelerator not in {"cpu", "gpu"}:
            raise ValueError("accelerator must be 'cpu' or 'gpu'")
        missing = {
            name
            for name in ("open", "high", "low", "close", "volume", "realized_total_return")
            if name not in self.fields
        }
        if missing:
            raise ValueError(f"supplier formula fields missing: {sorted(missing)}")
        shape = np.asarray(self.fields["close"]).shape
        if len(shape) != 2:
            raise ValueError("supplier formula fields must be date-by-asset matrices")
        if any(np.asarray(value).shape != shape for value in self.fields.values()):
            raise ValueError("supplier formula field shapes must match")
        if self.eligible is not None and np.asarray(self.eligible).shape != shape:
            raise ValueError("eligible mask shape mismatch")

    @staticmethod
    def parse(formula: str) -> ast.Expression:
        normalized = formula.strip().replace("^", "**")
        try:
            tree = ast.parse(normalized, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"unsupported supplier formula syntax: {formula}") from exc
        for node in ast.walk(tree):
            if isinstance(
                node,
                (
                    ast.Expression,
                    ast.Load,
                    ast.Constant,
                    ast.Name,
                    ast.Call,
                    ast.BinOp,
                    ast.UnaryOp,
                    ast.Add,
                    ast.Sub,
                    ast.Mult,
                    ast.Div,
                    ast.Pow,
                    ast.USub,
                    ast.UAdd,
                ),
            ):
                continue
            raise ValueError(f"unsupported supplier AST node: {type(node).__name__}")
        return tree

    def evaluate(self, formula: str) -> np.ndarray:
        tree = self.parse(formula)
        value = self._node(tree.body)
        if np.isscalar(value):
            value = np.full(np.asarray(self.fields["close"]).shape, value, dtype=np.float32)
        return _finite(value)

    def _node(self, node: ast.AST) -> Any:
        key = ast.dump(node, annotate_fields=True, include_attributes=False)
        if key in self.cache:
            return self.cache[key]
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("supplier constants must be numeric")
            value: Any = float(node.value)
        elif isinstance(node, ast.Name):
            name = node.id.lower()
            if name not in SUPPORTED_FIELDS:
                raise ValueError(f"unsupported supplier field: {node.id}")
            source_name = "realized_total_return" if name == "returns" else name
            if source_name not in self.fields:
                raise ValueError(f"supplier field unavailable: {source_name}")
            value = np.asarray(self.fields[source_name], dtype=np.float32)
        elif isinstance(node, ast.UnaryOp):
            child = self._node(node.operand)
            value = -child if isinstance(node.op, ast.USub) else child
        elif isinstance(node, ast.BinOp):
            left, right = self._node(node.left), self._node(node.right)
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                if isinstance(node.op, ast.Add):
                    value = left + right
                elif isinstance(node.op, ast.Sub):
                    value = left - right
                elif isinstance(node.op, ast.Mult):
                    value = left * right
                elif isinstance(node.op, ast.Div):
                    denominator = np.asarray(right)
                    floor = np.finfo(np.float64).eps * np.maximum(
                        np.abs(np.asarray(left)), np.abs(denominator)
                    )
                    value = np.where(np.abs(denominator) > floor, left / right, np.nan)
                elif isinstance(node.op, ast.Pow):
                    value = np.power(left, right)
                else:  # pragma: no cover - parse whitelist already rejects it
                    raise ValueError("unsupported supplier binary operator")
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise ValueError("supplier calls must be simple positional functions")
            name = node.func.id.lower()
            if name not in SUPPORTED_FUNCTIONS:
                raise ValueError(f"unsupported supplier function: {node.func.id}")
            args = [self._node(item) for item in node.args]
            value = self._call(name, args)
        else:  # pragma: no cover - parse whitelist already rejects it
            raise ValueError(f"unsupported supplier expression node: {type(node).__name__}")
        value = _finite(value)
        self.cache[key] = value
        return value

    def _call(self, name: str, args: list[Any]) -> np.ndarray:
        if name == "abs":
            if len(args) != 1:
                raise ValueError("abs expects one argument")
            return np.abs(args[0])
        if name == "rank":
            if len(args) != 1:
                raise ValueError("rank expects one argument")
            # Existing engine returns [-0.5, 0.5]; WorldQuant rank is [0, 1].
            ranked = _cross_sectional(np.asarray(args[0]), "rank", self.eligible)
            return np.where(np.isfinite(ranked), ranked + 0.5, np.nan)
        if name in {"delay", "delta"}:
            if len(args) != 2:
                raise ValueError(f"{name} expects two arguments")
            delayed = _delay(np.asarray(args[0]), _window(args[1]))
            return delayed if name == "delay" else np.asarray(args[0]) - delayed
        if name == "sum":
            if len(args) != 2:
                raise ValueError("sum expects two arguments")
            return _rolling_sum(np.asarray(args[0]), _window(args[1]))
        if name in {"stddev", "ts_min", "ts_max", "ts_rank"}:
            if len(args) != 2:
                raise ValueError(f"{name} expects two arguments")
            operation = {
                "stddev": "std",
                "ts_min": "min",
                "ts_max": "max",
                "ts_rank": "rank",
            }[name]
            window = _window(args[1])
            return (
                rolling_gpu(
                    np.asarray(args[0]),
                    window,
                    operation,
                    column_chunk=self.gpu_column_chunk,
                )
                if self.accelerator == "gpu"
                else _rolling(np.asarray(args[0]), window, operation)
            )
        if name == "correlation":
            if len(args) != 3:
                raise ValueError("correlation expects three arguments")
            window = _window(args[2])
            return (
                rolling_corr_gpu(
                    np.asarray(args[0]),
                    np.asarray(args[1]),
                    window,
                    column_chunk=self.gpu_column_chunk,
                )
                if self.accelerator == "gpu"
                else _rolling_corr(np.asarray(args[0]), np.asarray(args[1]), window)
            )
        raise ValueError(f"unsupported supplier function: {name}")
