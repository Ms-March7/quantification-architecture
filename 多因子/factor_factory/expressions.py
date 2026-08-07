"""A small, safe factor expression tree and causal NumPy operators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .accelerator import rolling_corr_gpu, rolling_gpu


EPS = 1e-12
Unit = tuple[int, int]


FIELD_UNITS: dict[str, Unit] = {
    "open": (1, 0),
    "high": (1, 0),
    "low": (1, 0),
    "close": (1, 0),
    "vwap": (1, 0),
    "volume": (0, 1),
    "amount": (1, 1),
    "realized_total_return": (0, 0),
}
PRICE_SCALE_EXPONENT: dict[str, int] = {
    "open": 1,
    "high": 1,
    "low": 1,
    "close": 1,
    "vwap": 1,
    "volume": 0,
    "amount": 0,
    "realized_total_return": 0,
}
ALLOWED_WINDOWS = (1, 2, 3, 5, 10, 20, 40, 60)
UNARY_OPS = {"neg", "abs", "signed_log1p", "cs_rank", "cs_zscore"}
BINARY_OPS = {"add", "sub", "mul", "safe_div"}
ROLLING_OPS = {"delay", "delta", "ts_mean", "ts_std", "ts_min", "ts_max", "ts_rank"}


@dataclass(frozen=True)
class Expression:
    op: str
    children: tuple["Expression", ...] = ()
    field: str | None = None
    window: int | None = None

    @staticmethod
    def terminal(name: str) -> "Expression":
        return Expression("field", field=name)

    def canonical(self) -> str:
        if self.op == "field":
            return f"${self.field}"
        if self.op == "neg" and self.children and self.children[0].op == "neg":
            return self.children[0].children[0].canonical()
        if self.op == "abs" and self.children and self.children[0].op == "abs":
            return self.children[0].canonical()
        if (
            self.op == "delay" and self.children and self.children[0].op == "delay"
            and self.window is not None and self.children[0].window is not None
        ):
            total = self.window + self.children[0].window
            return f"delay({self.children[0].children[0].canonical()},{total})"
        suffix = f",{self.window}" if self.window is not None else ""
        arguments_list = [child.canonical() for child in self.children]
        if self.op in {"add", "mul"}:
            arguments_list.sort()
        arguments = ",".join(arguments_list)
        return f"{self.op}({arguments}{suffix})"

    @property
    def complexity(self) -> int:
        return 1 + sum(child.complexity for child in self.children)

    @property
    def depth(self) -> int:
        return 0 if self.op == "field" else 1 + max((child.depth for child in self.children), default=0)


def expression_to_dict(expression: Expression) -> dict[str, Any]:
    return {
        "op": expression.op,
        "field": expression.field,
        "window": expression.window,
        "children": [expression_to_dict(child) for child in expression.children],
    }


def expression_from_dict(value: Mapping[str, Any]) -> Expression:
    if set(value) != {"op", "field", "window", "children"}:
        raise ValueError("invalid serialized expression keys")
    children = value["children"]
    if not isinstance(children, list):
        raise ValueError("serialized expression children must be a list")
    return Expression(
        op=str(value["op"]),
        children=tuple(expression_from_dict(child) for child in children),
        field=None if value["field"] is None else str(value["field"]),
        window=None if value["window"] is None else int(value["window"]),
    )


def expression_unit(expression: Expression) -> Unit | None:
    """Return a simple price/volume unit or ``None`` for invalid algebra."""

    if expression.op == "field":
        return FIELD_UNITS.get(str(expression.field))
    children = [expression_unit(child) for child in expression.children]
    if any(unit is None for unit in children):
        return None
    units = [unit for unit in children if unit is not None]
    if expression.op in {"neg", "abs", "delay", "delta", "ts_mean", "ts_std", "ts_min", "ts_max"}:
        return units[0]
    if expression.op in {"cs_rank", "cs_zscore"}:
        return (0, 0) if units[0] == (0, 0) else None
    if expression.op in {"ts_rank", "ts_corr"}:
        return (0, 0)
    if expression.op == "signed_log1p":
        return (0, 0) if units[0] == (0, 0) else None
    if expression.op in {"add", "sub"}:
        return units[0] if units[0] == units[1] else None
    if expression.op in {"mul", "safe_div"}:
        sign = 1 if expression.op == "mul" else -1
        result = (units[0][0] + sign * units[1][0], units[0][1] + sign * units[1][1])
        return result if abs(result[0]) + abs(result[1]) <= 2 else None
    return None


def expression_price_scale_exponent(expression: Expression) -> int | None:
    """Track sensitivity to Qlib's per-instrument adjusted-price scale."""

    if expression.op == "field":
        return PRICE_SCALE_EXPONENT.get(str(expression.field))
    children = [expression_price_scale_exponent(child) for child in expression.children]
    if any(value is None for value in children):
        return None
    exponents = [int(value) for value in children if value is not None]
    if expression.op in {"neg", "abs", "delay", "delta", "ts_mean", "ts_std", "ts_min", "ts_max"}:
        return exponents[0]
    if expression.op in {"ts_rank", "ts_corr"}:
        return 0
    if expression.op in {"cs_rank", "cs_zscore", "signed_log1p"}:
        return 0 if exponents[0] == 0 else None
    if expression.op in {"add", "sub"}:
        return exponents[0] if exponents[0] == exponents[1] else None
    if expression.op == "mul":
        return exponents[0] + exponents[1]
    if expression.op == "safe_div":
        return exponents[0] - exponents[1]
    return None


def _empty_like(values: np.ndarray) -> np.ndarray:
    dtype = np.float32 if values.dtype.itemsize <= 4 else np.float64
    return np.full(values.shape, np.nan, dtype=dtype)


def _delay(values: np.ndarray, window: int) -> np.ndarray:
    result = _empty_like(values)
    if window < len(values):
        result[window:] = values[:-window]
    return result


def _rolling(values: np.ndarray, window: int, operation: str) -> np.ndarray:
    result = _empty_like(values)
    minimum = max(2, int(np.ceil(window * 0.80)))
    valid_all = np.isfinite(values)
    if operation in {"mean", "std"}:
        any_valid = np.any(valid_all, axis=0)
        first_valid = np.argmax(valid_all, axis=0)
        reference = np.zeros(values.shape[1], dtype=np.float64)
        columns = np.flatnonzero(any_valid)
        reference[columns] = values[first_valid[columns], columns]
        filled = np.where(valid_all, values - reference[None, :], 0.0).astype(np.float64, copy=False)
        count_cumulative = np.vstack([
            np.zeros((1, values.shape[1]), dtype=np.int32),
            np.cumsum(valid_all, axis=0, dtype=np.int32),
        ])
        sum_cumulative = np.vstack([
            np.zeros((1, values.shape[1]), dtype=np.float64),
            np.cumsum(filled, axis=0, dtype=np.float64),
        ])
        counts = count_cumulative[window:] - count_cumulative[:-window]
        sums = sum_cumulative[window:] - sum_cumulative[:-window]
        eligible = counts >= minimum
        if operation == "mean":
            rolled = np.where(
                eligible, sums / np.maximum(counts, 1) + reference[None, :], np.nan
            )
        else:
            square_cumulative = np.vstack([
                np.zeros((1, values.shape[1]), dtype=np.float64),
                np.cumsum(filled * filled, axis=0, dtype=np.float64),
            ])
            square_sums = square_cumulative[window:] - square_cumulative[:-window]
            variance = (square_sums - sums * sums / np.maximum(counts, 1)) / np.maximum(counts - 1, 1)
            rolled = np.where(eligible, np.sqrt(np.maximum(variance, 0.0)), np.nan)
        result[window - 1:] = rolled.astype(result.dtype, copy=False)
        return result
    for end in range(window - 1, len(values)):
        sample = values[end - window + 1:end + 1]
        valid = np.isfinite(sample)
        counts = valid.sum(axis=0)
        eligible = counts >= minimum
        if operation == "min":
            safe = np.where(valid, sample, np.inf)
            result[end, eligible] = np.min(safe, axis=0)[eligible]
        elif operation == "max":
            safe = np.where(valid, sample, -np.inf)
            result[end, eligible] = np.max(safe, axis=0)[eligible]
        elif operation == "rank":
            current = sample[-1]
            finite_current = np.isfinite(current)
            below = np.sum(valid & (sample < current), axis=0)
            equal = np.sum(valid & (sample == current), axis=0)
            different = np.any(valid & (sample != current), axis=0)
            eligible &= finite_current & different
            ranks = (below + (equal - 1) / 2.0) / np.maximum(counts - 1, 1)
            result[end, eligible] = ranks[eligible]
    return result


def _rolling_corr(left: np.ndarray, right: np.ndarray, window: int) -> np.ndarray:
    result = _empty_like(left)
    minimum = max(3, int(np.ceil(window * 0.80)))
    for end in range(window - 1, len(left)):
        x = left[end - window + 1:end + 1]
        y = right[end - window + 1:end + 1]
        valid = np.isfinite(x) & np.isfinite(y)
        count = valid.sum(axis=0)
        safe_x = np.where(valid, x, 0.0)
        safe_y = np.where(valid, y, 0.0)
        mean_x = safe_x.sum(axis=0) / np.maximum(count, 1)
        mean_y = safe_y.sum(axis=0) / np.maximum(count, 1)
        centered_x = np.where(valid, x - mean_x, 0.0)
        centered_y = np.where(valid, y - mean_y, 0.0)
        covariance = np.sum(centered_x * centered_y, axis=0)
        variance_x = np.sum(centered_x * centered_x, axis=0)
        variance_y = np.sum(centered_y * centered_y, axis=0)
        denominator = np.sqrt(variance_x * variance_y)
        eligible = (
            (count >= minimum)
            & np.isfinite(left[end])
            & np.isfinite(right[end])
            & (denominator > EPS)
        )
        result[end, eligible] = covariance[eligible] / denominator[eligible]
    return np.where(
        np.isfinite(result), np.rint(result.astype(np.float64) * 1.0e5) / 1.0e5, np.nan
    ).astype(result.dtype, copy=False)


def _cross_sectional(values: np.ndarray, operation: str, eligible: np.ndarray | None = None) -> np.ndarray:
    result = _empty_like(values)
    for date in range(len(values)):
        row = values[date]
        valid = np.isfinite(row)
        if eligible is not None:
            valid &= eligible[date]
        count = int(valid.sum())
        if count < 2:
            continue
        clean = row[valid]
        if operation == "rank":
            order = np.argsort(clean, kind="stable")
            ranks = np.empty(count, dtype=np.float64)
            sorted_values = clean[order]
            starts = np.flatnonzero(np.r_[True, sorted_values[1:] != sorted_values[:-1]])
            ends = np.r_[starts[1:], count]
            averages = (starts + ends - 1) / 2.0
            ranks[order] = np.repeat(averages, ends - starts)
            if len(np.unique(clean)) < 2:
                continue
            result[date, valid] = ranks / (count - 1.0) - 0.5
        else:
            scale = float(np.std(clean, ddof=1))
            result[date, valid] = 0.0 if scale <= EPS else (clean - np.mean(clean)) / scale
    return result


def evaluate_expression(
    expression: Expression,
    fields: Mapping[str, np.ndarray],
    cache: dict[str, np.ndarray] | None = None,
    eligible: np.ndarray | None = None,
    *,
    accelerator: str = "cpu",
    gpu_column_chunk: int = 256,
) -> np.ndarray:
    """Evaluate a whitelisted expression; arbitrary Python is never executed."""

    if accelerator not in {"cpu", "gpu"}:
        raise ValueError("accelerator must be 'cpu' or 'gpu'")
    if isinstance(gpu_column_chunk, bool) or not isinstance(gpu_column_chunk, int) or gpu_column_chunk < 1:
        raise ValueError("gpu_column_chunk must be a positive integer")
    validate_expression(expression, available_fields=set(fields))
    memo = cache if cache is not None else {}
    canonical = expression.canonical()
    key = canonical if accelerator == "cpu" else f"gpu:{gpu_column_chunk}:{canonical}"
    if key in memo:
        return memo[key]
    if expression.op == "field":
        if expression.field not in fields:
            raise KeyError(f"field {expression.field!r} is unavailable")
        value = np.array(fields[expression.field], copy=True)
    else:
        children = [
            evaluate_expression(
                child,
                fields,
                memo,
                eligible,
                accelerator=accelerator,
                gpu_column_chunk=gpu_column_chunk,
            )
            for child in expression.children
        ]
        op = expression.op
        window = expression.window
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            if op == "neg":
                value = -children[0]
            elif op == "abs":
                value = np.abs(children[0])
            elif op == "signed_log1p":
                value = np.sign(children[0]) * np.log1p(np.abs(children[0]))
            elif op == "add":
                value = children[0] + children[1]
            elif op == "sub":
                value = children[0] - children[1]
            elif op == "mul":
                value = children[0] * children[1]
            elif op == "safe_div":
                numerator = children[0]
                denominator = children[1]
                relative_floor = np.finfo(np.float64).eps * np.maximum(
                    np.abs(numerator), np.abs(denominator)
                )
                value = np.where(
                    np.abs(denominator) > relative_floor, numerator / denominator, np.nan
                )
            elif op == "delay":
                value = _delay(children[0], int(window or 1))
            elif op == "delta":
                value = children[0] - _delay(children[0], int(window or 1))
            elif op in {"ts_mean", "ts_std", "ts_min", "ts_max", "ts_rank"}:
                operation = op.removeprefix("ts_")
                value = (
                    rolling_gpu(
                        children[0],
                        int(window or 2),
                        operation,
                        column_chunk=gpu_column_chunk,
                    )
                    if accelerator == "gpu"
                    else _rolling(children[0], int(window or 2), operation)
                )
            elif op == "ts_corr":
                value = (
                    rolling_corr_gpu(
                        children[0],
                        children[1],
                        int(window or 5),
                        column_chunk=gpu_column_chunk,
                    )
                    if accelerator == "gpu"
                    else _rolling_corr(children[0], children[1], int(window or 5))
                )
            elif op == "cs_rank":
                value = _cross_sectional(children[0], "rank", eligible)
            elif op == "cs_zscore":
                value = _cross_sectional(children[0], "zscore", eligible)
            else:
                raise ValueError(f"unsupported factor operation: {op}")
    value = np.where(np.isfinite(value), value, np.nan)
    memo[key] = value
    return value


def validate_expression(
    expression: Expression,
    *,
    available_fields: set[str] | None = None,
    max_depth: int | None = None,
) -> None:
    """Fail fast on malformed arity, fields, windows, or depth."""

    if max_depth is not None and expression.depth > max_depth:
        raise ValueError(f"expression depth {expression.depth} exceeds {max_depth}")
    if expression.op == "field":
        if expression.children or expression.window is not None or not expression.field:
            raise ValueError("field terminals cannot have children or windows")
        if available_fields is not None and expression.field not in available_fields:
            raise KeyError(f"field {expression.field!r} is unavailable")
        return
    if expression.field is not None:
        raise ValueError("non-terminal operations cannot carry a field")
    if expression.op in UNARY_OPS:
        expected = 1
        if expression.window is not None:
            raise ValueError(f"{expression.op} does not accept a window")
    elif expression.op in BINARY_OPS:
        expected = 2
        if expression.window is not None:
            raise ValueError(f"{expression.op} does not accept a window")
    elif expression.op in ROLLING_OPS:
        expected = 1
        if expression.window not in ALLOWED_WINDOWS:
            raise ValueError(f"invalid {expression.op} window: {expression.window}")
    elif expression.op == "ts_corr":
        expected = 2
        if expression.window not in ALLOWED_WINDOWS:
            raise ValueError(f"invalid ts_corr window: {expression.window}")
    else:
        raise ValueError(f"unsupported factor operation: {expression.op}")
    if len(expression.children) != expected:
        raise ValueError(f"{expression.op} expects {expected} children")
    for child in expression.children:
        validate_expression(child, available_fields=available_fields)
