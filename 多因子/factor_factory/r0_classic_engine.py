"""Causal, memory-bounded execution of the registered R0 classic factors.

The engine consumes a :class:`PanelData` plus the ephemeral fields produced by
``build_total_return_signal_fields``.  It deliberately computes one factor at
a time, returns ``float32`` matrices, and keeps only a bounded LRU of reusable
rolling intermediates.  Missing observations are hard episode boundaries:
no rolling statistic is allowed to bridge them.

All values are close-time signals.  The caller is responsible for enforcing
the registry contract that their first possible use is the next session.
"""

from __future__ import annotations

import ast
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

import numpy as np

from .accelerator import rolling_corr_gpu, rolling_gpu
from .expressions import _rolling
from .schema import PanelData
from .supplier_formula_engine import SupplierFormulaEvaluator
from .total_return_projection import TotalReturnProjection, build_total_return_signal_fields


EPS = 1e-12
SUPPORTED_KINDS = frozenset(
    {
        "total_return",
        "return_reversal",
        "return_volatility",
        "downside_volatility",
        "upside_volatility",
        "realized_skewness",
        "realized_kurtosis",
        "tail_loss",
        "maximum_drawdown",
        "moving_average_ratio",
        "ema_ratio",
        "linear_trend",
        "breakout_position",
        "price_location",
        "intraday_return",
        "overnight_return",
        "range_ratio",
        "shadow_ratio",
        "true_range",
        "atr",
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
        "macd",
        "ppo",
        "bollinger_z",
        "bollinger_width",
        "stochastic",
        "williams_r",
        "cci",
        "roc",
        "beta",
        "downside_beta",
        "market_corr",
        "residual_volatility",
        "residual_momentum",
        "supplier_dsl",
    }
)


class R0ClassicFactorError(ValueError):
    """Raised when a classic-factor specification cannot be evaluated safely."""


@dataclass(frozen=True)
class ClassicFactorResult:
    """One materialized factor plus small execution diagnostics."""

    factor_id: str
    kind: str
    values: np.ndarray
    finite_count: int
    finite_fraction: float
    accelerator: str


class _BoundedArrayCache:
    def __init__(self, max_bytes: int) -> None:
        if isinstance(max_bytes, bool) or int(max_bytes) < 0:
            raise ValueError("max_cache_bytes must be a non-negative integer")
        self.max_bytes = int(max_bytes)
        self.bytes = 0
        self.data: OrderedDict[str, np.ndarray] = OrderedDict()

    def get(self, key: str) -> np.ndarray | None:
        value = self.data.get(key)
        if value is not None:
            self.data.move_to_end(key)
        return value

    def put(self, key: str, value: np.ndarray) -> np.ndarray:
        array = np.asarray(value)
        if self.max_bytes == 0 or array.nbytes > self.max_bytes:
            return array
        old = self.data.pop(key, None)
        if old is not None:
            self.bytes -= old.nbytes
        while self.data and self.bytes + array.nbytes > self.max_bytes:
            _, evicted = self.data.popitem(last=False)
            self.bytes -= evicted.nbytes
        self.data[key] = array
        self.bytes += array.nbytes
        return array

    def clear(self) -> None:
        self.data.clear()
        self.bytes = 0


class _CausalSupplierFormulaEvaluator(SupplierFormulaEvaluator):
    """Supplier whitelist with strict full-window episode semantics.

    The original bounded evaluator intentionally permits 80% observations in
    rolling windows.  R0 classic research instead treats every missing derived
    or source value as a hard boundary.  This subclass only tightens those
    operators; the original AST and arbitrary-code protections remain intact.
    """

    def __init__(self, *args: Any, classic_engine: "R0ClassicFactorEngine", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.classic_engine = classic_engine

    @staticmethod
    def _window_value(value: Any) -> int:
        if isinstance(value, np.ndarray) or isinstance(value, bool):
            raise R0ClassicFactorError("supplier rolling window must be scalar")
        numeric = float(value)
        result = int(round(numeric))
        if not np.isfinite(numeric) or result < 1 or abs(numeric - result) > 1e-9:
            raise R0ClassicFactorError("supplier rolling window must be a positive integer")
        return result

    def _call(self, name: str, args: list[Any]) -> np.ndarray:
        if name == "sum":
            if len(args) != 2:
                raise R0ClassicFactorError("sum expects two arguments")
            values = np.asarray(args[0], dtype=np.float32)
            window = self._window_value(args[1])
            source = np.asarray(values, dtype=np.float64)
            valid = np.isfinite(source)
            prefix = np.vstack(
                (np.zeros((1, source.shape[1])), np.cumsum(np.where(valid, source, 0.0), axis=0))
            )
            counts = np.vstack(
                (np.zeros((1, source.shape[1]), dtype=np.int32), np.cumsum(valid, axis=0, dtype=np.int32))
            )
            result = np.full(source.shape, np.nan, dtype=np.float32)
            if window <= len(source):
                rolled = prefix[window:] - prefix[:-window]
                count = counts[window:] - counts[:-window]
                result[window - 1 :] = np.where(count == window, rolled, np.nan).astype(np.float32)
            return result
        if name in {"stddev", "ts_min", "ts_max", "ts_rank"}:
            if len(args) != 2:
                raise R0ClassicFactorError(f"{name} expects two arguments")
            values = np.asarray(args[0], dtype=np.float32)
            window = self._window_value(args[1])
            operation = {
                "stddev": "std",
                "ts_min": "min",
                "ts_max": "max",
                "ts_rank": "rank",
            }[name]
            rolled = (
                rolling_gpu(values, window, operation, column_chunk=self.gpu_column_chunk)
                if self.accelerator == "gpu"
                else _rolling(values, window, operation)
            )
            strict = self.classic_engine._strict_mask(values, window)
            return np.where(strict, rolled, np.nan).astype(np.float32)
        if name == "correlation":
            if len(args) != 3:
                raise R0ClassicFactorError("correlation expects three arguments")
            left = np.asarray(args[0], dtype=np.float32)
            right = np.asarray(args[1], dtype=np.float32)
            window = self._window_value(args[2])
            if self.accelerator == "gpu" and window >= 3:
                rolled = rolling_corr_gpu(
                    left, right, window, column_chunk=self.gpu_column_chunk
                )
            else:
                rolled = self.classic_engine._rolling_corr_cpu_centered(
                    left, right, window
                )
            strict = self.classic_engine._strict_mask(left, window) & self.classic_engine._strict_mask(right, window)
            rolled = np.where(
                np.isfinite(rolled), np.rint(np.asarray(rolled, dtype=np.float64) * 1.0e5) / 1.0e5, np.nan
            )
            return np.where(strict, rolled, np.nan).astype(np.float32)
        return super()._call(name, args)


def _positive_int(params: Mapping[str, Any], name: str, default: int | None = None) -> int:
    raw = params.get(name, default)
    if isinstance(raw, bool) or raw is None:
        raise R0ClassicFactorError(f"{name} must be a positive integer")
    try:
        numeric = float(raw)
    except (TypeError, ValueError) as exc:
        raise R0ClassicFactorError(f"{name} must be a positive integer") from exc
    value = int(round(numeric))
    if not np.isfinite(numeric) or value < 1 or abs(numeric - value) > 1e-9:
        raise R0ClassicFactorError(f"{name} must be a positive integer")
    return value


def _finite_float(params: Mapping[str, Any], name: str, default: float) -> float:
    try:
        value = float(params.get(name, default))
    except (TypeError, ValueError) as exc:
        raise R0ClassicFactorError(f"{name} must be finite") from exc
    if not np.isfinite(value):
        raise R0ClassicFactorError(f"{name} must be finite")
    return value


def _safe_div(numerator: np.ndarray, denominator: np.ndarray | float) -> np.ndarray:
    left = np.asarray(numerator, dtype=np.float64)
    right = np.asarray(denominator, dtype=np.float64)
    scale = np.maximum(np.abs(left), np.abs(right))
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        return np.where(np.abs(right) > np.finfo(np.float64).eps * scale, left / right, np.nan)


class R0ClassicFactorEngine:
    """Evaluate registered classic R0 factor specs against one dense panel."""

    def __init__(
        self,
        panel: PanelData,
        signal_fields: TotalReturnProjection | Mapping[str, np.ndarray] | None = None,
        *,
        research_eligible: np.ndarray | None = None,
        accelerator: str = "cpu",
        gpu_column_chunk: int = 64,
        cpu_column_chunk: int = 32,
        max_cache_bytes: int = 768 * 1024 * 1024,
        reject_constant: bool = True,
    ) -> None:
        if accelerator not in {"cpu", "gpu"}:
            raise R0ClassicFactorError("accelerator must be 'cpu' or 'gpu'")
        if gpu_column_chunk < 1 or cpu_column_chunk < 1:
            raise R0ClassicFactorError("column chunks must be positive")
        if signal_fields is None:
            projection = build_total_return_signal_fields(panel)
            supplied = projection.fields
        elif isinstance(signal_fields, TotalReturnProjection):
            supplied = signal_fields.fields
        else:
            supplied = signal_fields
        shape = panel.shape
        required = {"open", "high", "low", "close", "realized_total_return", "volume", "amount"}
        missing = sorted(required - set(supplied))
        if missing:
            raise R0ClassicFactorError(f"classic factor fields missing: {missing}")
        self.panel = panel
        self.eligible = np.asarray(panel.eligible, dtype=bool)
        if research_eligible is None:
            self.research_eligible = self.eligible
        else:
            research_mask = np.asarray(research_eligible, dtype=bool)
            if research_mask.shape != shape:
                raise R0ClassicFactorError(
                    f"research_eligible shape mismatch: {research_mask.shape} != {shape}"
                )
            if np.any(research_mask & ~self.eligible):
                raise R0ClassicFactorError(
                    "research_eligible must be a subset of panel.eligible"
                )
            self.research_eligible = research_mask
        fields: dict[str, np.ndarray] = {}
        for name, values in supplied.items():
            array = np.asarray(values, dtype=np.float32)
            if array.shape != shape:
                raise R0ClassicFactorError(f"field {name} shape mismatch: {array.shape} != {shape}")
            # A non-eligible row is a hard history boundary, even when a source
            # happens to carry stale OHLC values for a suspension.
            fields[name] = np.where(self.eligible, array, np.nan).astype(np.float32, copy=False)
        if "vwap" not in fields:
            fields["vwap"] = _safe_div(fields["amount"], fields["volume"]).astype(np.float32)
        self.fields = fields
        self.accelerator = accelerator
        self.gpu_column_chunk = int(gpu_column_chunk)
        self.cpu_column_chunk = int(cpu_column_chunk)
        self.reject_constant = bool(reject_constant)
        self.cache = _BoundedArrayCache(max_cache_bytes)

    def clear_cache(self) -> None:
        self.cache.clear()

    def evaluate(self, spec: Mapping[str, Any]) -> np.ndarray:
        """Return one factor matrix, rejecting unsupported or degenerate specs."""

        factor_id = str(spec.get("factor_id", "")).strip()
        kind = str(spec.get("kind", "")).strip()
        if not factor_id:
            raise R0ClassicFactorError("factor_id is required")
        if kind not in SUPPORTED_KINDS:
            raise R0ClassicFactorError(f"unsupported classic factor kind: {kind!r}")
        params = spec.get("params", {})
        if not isinstance(params, Mapping):
            raise R0ClassicFactorError("params must be a mapping")
        inputs = spec.get("inputs", [])
        if inputs is not None:
            if not isinstance(inputs, (list, tuple)):
                raise R0ClassicFactorError("inputs must be a list")
            unavailable = sorted(str(name) for name in inputs if str(name) not in self.fields)
            if unavailable:
                raise R0ClassicFactorError(f"factor inputs unavailable: {unavailable}")

        values = self._dispatch(kind, params, spec)
        result = np.asarray(values, dtype=np.float32)
        if result.shape != self.panel.shape:
            raise R0ClassicFactorError(
                f"factor {factor_id} returned shape {result.shape}; expected {self.panel.shape}"
            )
        result = np.where(
            self.research_eligible & np.isfinite(result), result, np.nan
        ).astype(
            np.float32, copy=False
        )
        finite = result[np.isfinite(result)]
        if finite.size < 2:
            raise R0ClassicFactorError(f"factor {factor_id} has fewer than two finite values")
        if self.reject_constant:
            scale = max(1.0, float(np.max(np.abs(finite))))
            if float(np.max(finite) - np.min(finite)) <= 1e-7 * scale:
                raise R0ClassicFactorError(f"factor {factor_id} is constant")
        return result

    def evaluate_result(self, spec: Mapping[str, Any]) -> ClassicFactorResult:
        values = self.evaluate(spec)
        finite_count = int(np.count_nonzero(np.isfinite(values)))
        return ClassicFactorResult(
            factor_id=str(spec["factor_id"]),
            kind=str(spec["kind"]),
            values=values,
            finite_count=finite_count,
            finite_fraction=finite_count / values.size,
            accelerator=self.accelerator,
        )

    def evaluate_many(
        self, specs: Iterable[Mapping[str, Any]]
    ) -> Iterator[ClassicFactorResult]:
        """Stream factors without retaining all output matrices in memory."""

        for spec in specs:
            yield self.evaluate_result(spec)

    def _field(self, name: str) -> np.ndarray:
        return self.fields[name]

    def _cache_value(self, key: str, builder: Any) -> np.ndarray:
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        return self.cache.put(key, np.asarray(builder()))

    @staticmethod
    def _delay(values: np.ndarray, periods: int = 1) -> np.ndarray:
        result = np.full(values.shape, np.nan, dtype=np.float32)
        if periods < len(values):
            result[periods:] = values[:-periods]
        return result

    def _rolling_count(self, values: np.ndarray, window: int) -> np.ndarray:
        # Do not key arbitrary arrays by ``id``: an intermediate can be freed
        # and its id reused later, which would make a cached mask incorrect.
        valid = np.isfinite(values)
        prefix = np.vstack(
            (np.zeros((1, values.shape[1]), dtype=np.int32), np.cumsum(valid, axis=0, dtype=np.int32))
        )
        out = np.zeros(values.shape, dtype=np.int32)
        if window <= len(values):
            out[window - 1 :] = prefix[window:] - prefix[:-window]
        return out

    def _strict_mask(self, values: np.ndarray, window: int) -> np.ndarray:
        return self._rolling_count(values, window) == window

    def _rolling_sum(self, values: np.ndarray, window: int, *, key: str) -> np.ndarray:
        cache_key = f"sum:{key}:{window}"

        def build() -> np.ndarray:
            source = np.asarray(values, dtype=np.float64)
            valid = np.isfinite(source)
            prefix = np.vstack(
                (np.zeros((1, source.shape[1]), dtype=np.float64), np.cumsum(np.where(valid, source, 0.0), axis=0))
            )
            count = np.vstack(
                (np.zeros((1, source.shape[1]), dtype=np.int32), np.cumsum(valid, axis=0, dtype=np.int32))
            )
            out = np.full(source.shape, np.nan, dtype=np.float32)
            if window <= len(source):
                sums = prefix[window:] - prefix[:-window]
                counts = count[window:] - count[:-window]
                out[window - 1 :] = np.where(counts == window, sums, np.nan).astype(np.float32)
            return out

        return self._cache_value(cache_key, build)

    def _rolling_mean(self, values: np.ndarray, window: int, *, key: str) -> np.ndarray:
        return self._cache_value(
            f"mean:{key}:{window}",
            lambda: self._rolling_sum(values, window, key=key) / np.float32(window),
        )

    def _rolling_std(self, values: np.ndarray, window: int, *, key: str) -> np.ndarray:
        cache_key = f"std:{key}:{window}:{self.accelerator}"

        def build() -> np.ndarray:
            if window == 1:
                return np.where(np.isfinite(values), 0.0, np.nan).astype(np.float32)
            if self.accelerator == "gpu":
                result = rolling_gpu(
                    values, window, "std", column_chunk=self.gpu_column_chunk
                )
            else:
                # The shared CPU kernel subtracts a per-column reference before
                # accumulating, avoiding catastrophic cancellation for amount.
                result = _rolling(values, window, "std")
            return np.where(self._strict_mask(values, window), result, np.nan).astype(np.float32)

        return self._cache_value(cache_key, build)

    def _rolling_extreme(
        self, values: np.ndarray, window: int, operation: str, *, key: str
    ) -> np.ndarray:
        cache_key = f"{operation}:{key}:{window}:{self.accelerator}"

        def build() -> np.ndarray:
            if self.accelerator == "gpu":
                result = rolling_gpu(
                    values, window, operation, column_chunk=self.gpu_column_chunk
                )
                return np.where(self._strict_mask(values, window), result, np.nan).astype(np.float32)
            result = np.full(values.shape, np.nan, dtype=np.float32)
            if window > len(values):
                return result
            for start in range(0, values.shape[1], self.cpu_column_chunk):
                stop = min(values.shape[1], start + self.cpu_column_chunk)
                block = np.asarray(values[:, start:stop], dtype=np.float32)
                samples = np.lib.stride_tricks.sliding_window_view(block, window, axis=0)
                valid = np.isfinite(samples).all(axis=-1)
                if operation == "min":
                    rolled = np.min(np.where(np.isfinite(samples), samples, np.inf), axis=-1)
                else:
                    rolled = np.max(np.where(np.isfinite(samples), samples, -np.inf), axis=-1)
                result[window - 1 :, start:stop] = np.where(valid, rolled, np.nan)
            return result

        return self._cache_value(cache_key, build)

    def _rolling_corr(
        self, left: np.ndarray, right: np.ndarray, window: int, *, key: str
    ) -> np.ndarray:
        cache_key = f"corr:{key}:{window}:{self.accelerator}"

        def build() -> np.ndarray:
            right_matrix = (
                np.broadcast_to(right[:, None], left.shape)
                if np.asarray(right).ndim == 1
                else np.asarray(right)
            )
            if self.accelerator == "gpu":
                result = rolling_corr_gpu(
                    left, right_matrix, window, column_chunk=self.gpu_column_chunk
                )
            else:
                result = self._rolling_corr_cpu_centered(left, right_matrix, window)
            strict = self._strict_mask(left, window) & self._strict_mask(right_matrix, window)
            result = np.where(
                np.isfinite(result), np.rint(np.asarray(result, dtype=np.float64) * 1.0e5) / 1.0e5, np.nan
            )
            return np.where(strict, result, np.nan).astype(np.float32)

        return self._cache_value(cache_key, build)

    def _rolling_corr_cpu_centered(
        self, left: np.ndarray, right: np.ndarray, window: int
    ) -> np.ndarray:
        """Two-pass CPU peer of :func:`rolling_corr_gpu`, chunked by asset."""

        x_source = np.asarray(left)
        y_source = np.asarray(right)
        result = np.full(x_source.shape, np.nan, dtype=np.float32)
        if window > len(x_source):
            return result
        minimum = min(window, max(3, int(np.ceil(window * 0.80))))
        chunk = max(1, min(self.cpu_column_chunk, 16))
        for start in range(0, x_source.shape[1], chunk):
            stop = min(x_source.shape[1], start + chunk)
            xw = np.lib.stride_tricks.sliding_window_view(
                x_source[:, start:stop], window, axis=0
            )
            yw = np.lib.stride_tricks.sliding_window_view(
                y_source[:, start:stop], window, axis=0
            )
            valid = np.isfinite(xw) & np.isfinite(yw)
            count = np.sum(valid, axis=-1)
            divisor = np.maximum(count, 1)
            mean_x = np.sum(np.where(valid, xw, 0.0), axis=-1, dtype=np.float64) / divisor
            mean_y = np.sum(np.where(valid, yw, 0.0), axis=-1, dtype=np.float64) / divisor
            centered_x = np.where(valid, xw - mean_x[..., None], 0.0).astype(
                np.float64, copy=False
            )
            centered_y = np.where(valid, yw - mean_y[..., None], 0.0).astype(
                np.float64, copy=False
            )
            covariance = np.sum(centered_x * centered_y, axis=-1, dtype=np.float64)
            variance_x = np.sum(centered_x * centered_x, axis=-1, dtype=np.float64)
            variance_y = np.sum(centered_y * centered_y, axis=-1, dtype=np.float64)
            denominator = np.sqrt(np.maximum(variance_x, 0.0) * np.maximum(variance_y, 0.0))
            current_x = xw[..., -1]
            current_y = yw[..., -1]
            eligible = (
                (count >= minimum)
                & np.isfinite(current_x)
                & np.isfinite(current_y)
                & (denominator > EPS)
            )
            rolled = np.where(eligible, _safe_div(covariance, denominator), np.nan)
            result[window - 1 :, start:stop] = np.where(
                np.isfinite(rolled), np.rint(rolled * 1.0e5) / 1.0e5, np.nan
            ).astype(np.float32)
        return result

    @staticmethod
    def _rolling_pair_stats(
        left: np.ndarray, right: np.ndarray, window: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        x = np.asarray(left, dtype=np.float64)
        y = np.asarray(right, dtype=np.float64)
        valid = np.isfinite(x) & np.isfinite(y)

        def window_sum(values: np.ndarray) -> np.ndarray:
            prefix = np.vstack((np.zeros((1, values.shape[1])), np.cumsum(values, axis=0)))
            out = np.full(values.shape, np.nan, dtype=np.float64)
            if window <= len(values):
                out[window - 1 :] = prefix[window:] - prefix[:-window]
            return out

        any_valid = np.any(valid, axis=0)
        first_valid = np.argmax(valid, axis=0)
        columns = np.arange(x.shape[1])
        reference_x = np.where(any_valid, x[first_valid, columns], 0.0)
        reference_y = np.where(any_valid, y[first_valid, columns], 0.0)
        safe_x = np.where(valid, x - reference_x[None, :], 0.0)
        safe_y = np.where(valid, y - reference_y[None, :], 0.0)
        count = window_sum(valid.astype(np.float64))
        sx, sy = window_sum(safe_x), window_sum(safe_y)
        sxx, syy, sxy = window_sum(safe_x * safe_x), window_sum(safe_y * safe_y), window_sum(safe_x * safe_y)
        mean_x = sx / window + reference_x[None, :]
        mean_y = sy / window + reference_y[None, :]
        denom = max(window - 1, 1)
        var_x = (sxx - sx * sx / window) / denom
        var_y = (syy - sy * sy / window) / denom
        cov = (sxy - sx * sy / window) / denom
        full = count == window
        arrays = (mean_x, mean_y, var_x, var_y, cov)
        return tuple(np.where(full, item, np.nan) for item in arrays)  # type: ignore[return-value]

    def _rolling_quantile(
        self, values: np.ndarray, window: int, quantile: float, *, key: str
    ) -> np.ndarray:
        cache_key = f"quantile:{key}:{window}:{quantile:.8g}"

        def build() -> np.ndarray:
            result = np.full(values.shape, np.nan, dtype=np.float32)
            if window > len(values):
                return result
            for start in range(0, values.shape[1], self.cpu_column_chunk):
                stop = min(values.shape[1], start + self.cpu_column_chunk)
                samples = np.lib.stride_tricks.sliding_window_view(
                    values[:, start:stop], window, axis=0
                )
                valid = np.isfinite(samples).all(axis=-1)
                rolled = np.quantile(samples, quantile, axis=-1)
                result[window - 1 :, start:stop] = np.where(valid, rolled, np.nan)
            return result

        return self._cache_value(cache_key, build)

    def _rolling_expected_shortfall(
        self, values: np.ndarray, window: int, quantile: float, *, key: str
    ) -> np.ndarray:
        """Positive loss from the conditional mean at/below the rolling quantile."""

        cache_key = f"expected_shortfall:{key}:{window}:{quantile:.8g}"

        def build() -> np.ndarray:
            result = np.full(values.shape, np.nan, dtype=np.float32)
            if window > len(values):
                return result
            for start in range(0, values.shape[1], self.cpu_column_chunk):
                stop = min(values.shape[1], start + self.cpu_column_chunk)
                samples = np.lib.stride_tricks.sliding_window_view(
                    values[:, start:stop], window, axis=0
                ).astype(np.float64, copy=False)
                valid = np.isfinite(samples).all(axis=-1)
                threshold = np.quantile(samples, quantile, axis=-1)
                tail = samples <= threshold[..., None]
                count = np.sum(tail, axis=-1)
                conditional_mean = np.sum(np.where(tail, samples, 0.0), axis=-1) / np.maximum(
                    count, 1
                )
                result[window - 1 :, start:stop] = np.where(
                    valid & (count > 0), -conditional_mean, np.nan
                )
            return result

        return self._cache_value(cache_key, build)

    def _rolling_mad(self, values: np.ndarray, window: int, *, key: str) -> np.ndarray:
        cache_key = f"mad:{key}:{window}"

        def build() -> np.ndarray:
            result = np.full(values.shape, np.nan, dtype=np.float32)
            if window > len(values):
                return result
            for start in range(0, values.shape[1], self.cpu_column_chunk):
                stop = min(values.shape[1], start + self.cpu_column_chunk)
                samples = np.lib.stride_tricks.sliding_window_view(
                    values[:, start:stop], window, axis=0
                ).astype(np.float64, copy=False)
                valid = np.isfinite(samples).all(axis=-1)
                means = np.mean(np.where(np.isfinite(samples), samples, 0.0), axis=-1)
                deviations = np.mean(np.abs(samples - means[..., None]), axis=-1)
                result[window - 1 :, start:stop] = np.where(valid, deviations, np.nan)
            return result

        return self._cache_value(cache_key, build)

    def _ema(self, values: np.ndarray, span: int, *, key: str) -> np.ndarray:
        cache_key = f"ema:{key}:{span}"

        def build() -> np.ndarray:
            source = np.asarray(values, dtype=np.float64)
            result = np.full(source.shape, np.nan, dtype=np.float32)
            state = np.zeros(source.shape[1], dtype=np.float64)
            streak = np.zeros(source.shape[1], dtype=np.int32)
            alpha = 2.0 / (span + 1.0)
            for row in range(len(source)):
                current = source[row]
                valid = np.isfinite(current)
                fresh = valid & (streak == 0)
                continuing = valid & ~fresh
                state[fresh] = current[fresh]
                state[continuing] = alpha * current[continuing] + (1.0 - alpha) * state[continuing]
                state[~valid] = 0.0
                streak[valid] += 1
                streak[~valid] = 0
                ready = valid & (streak >= span)
                result[row, ready] = state[ready].astype(np.float32)
            return result

        return self._cache_value(cache_key, build)

    def _daily_market_return(self) -> np.ndarray:
        cached = self.cache.get("market:return:equal_weight")
        if cached is not None:
            return cached
        returns = self._field("realized_total_return")
        valid = self.research_eligible & np.isfinite(returns)
        count = valid.sum(axis=1)
        market = np.where(
            count >= 2,
            np.where(valid, returns, 0.0).sum(axis=1, dtype=np.float64) / np.maximum(count, 1),
            np.nan,
        ).astype(np.float32)
        return self.cache.put("market:return:equal_weight", market)

    def _rolling_moments(self, values: np.ndarray, window: int, order: int, *, key: str) -> np.ndarray:
        powers = [
            self._rolling_sum(
                np.asarray(values, dtype=np.float64) ** power,
                window,
                key=f"{key}^{power}",
            ).astype(np.float64)
            / window
            for power in range(1, order + 1)
        ]
        mean = powers[0]
        variance = np.maximum(powers[1] - mean * mean, 0.0)
        if order == 3:
            central = powers[2] - 3.0 * mean * powers[1] + 2.0 * mean**3
            biased = _safe_div(central, variance ** 1.5)
            if window <= 2:
                return np.full(values.shape, np.nan, dtype=np.float32)
            correction = np.sqrt(window * (window - 1.0)) / (window - 2.0)
            return (correction * biased).astype(np.float32)
        central = powers[3] - 4.0 * mean * powers[2] + 6.0 * mean * mean * powers[1] - 3.0 * mean**4
        biased_excess = _safe_div(central, variance * variance) - 3.0
        if window <= 3:
            return np.full(values.shape, np.nan, dtype=np.float32)
        correction = (window - 1.0) / ((window - 2.0) * (window - 3.0))
        return (correction * ((window + 1.0) * biased_excess + 6.0)).astype(np.float32)

    def _stat_window(self, daily: np.ndarray, window: int, stat: str, *, key: str) -> np.ndarray:
        if stat == "mean":
            return self._rolling_mean(daily, window, key=key)
        if stat == "std":
            return self._rolling_std(daily, window, key=key)
        raise R0ClassicFactorError("stat must be 'mean' or 'std'")

    def _total_return(self, window: int) -> np.ndarray:
        returns = self._field("realized_total_return")
        if np.any(np.isfinite(returns) & (returns <= -1.0)):
            raise R0ClassicFactorError("realized_total_return contains a value <= -100%")
        logs = np.log1p(returns.astype(np.float64))
        summed = self._rolling_sum(logs, window, key="log1p_return")
        return np.expm1(summed.astype(np.float64)).astype(np.float32)

    def _maximum_drawdown(self, window: int) -> np.ndarray:
        returns = self._field("realized_total_return")
        cache_key = f"maximum_drawdown:{window}"

        def build() -> np.ndarray:
            result = np.full(returns.shape, np.nan, dtype=np.float32)
            if window > len(returns):
                return result
            chunk = max(4, min(self.cpu_column_chunk, 16))
            for start in range(0, returns.shape[1], chunk):
                stop = min(returns.shape[1], start + chunk)
                samples = np.lib.stride_tricks.sliding_window_view(
                    returns[:, start:stop], window, axis=0
                ).astype(np.float64, copy=False)
                valid = np.isfinite(samples).all(axis=-1) & (samples > -1.0).all(axis=-1)
                compounded = np.cumprod(1.0 + np.where(np.isfinite(samples), samples, 0.0), axis=-1)
                wealth = np.concatenate(
                    (np.ones((*compounded.shape[:-1], 1), dtype=np.float64), compounded),
                    axis=-1,
                )
                peaks = np.maximum.accumulate(wealth, axis=-1)
                drawdowns = _safe_div(wealth, peaks) - 1.0
                worst = np.min(drawdowns, axis=-1)
                result[window - 1 :, start:stop] = np.where(valid, worst, np.nan)
            return result

        return self._cache_value(cache_key, build)

    def _linear_trend(self, window: int, metric: str) -> np.ndarray:
        close = self._field("close")
        log_price = np.where(close > 0.0, np.log(close.astype(np.float64)), np.nan)
        valid = np.isfinite(log_price)
        index = np.arange(len(close), dtype=np.float64)[:, None]

        def rolling_sum(values: np.ndarray) -> np.ndarray:
            prefix = np.vstack((np.zeros((1, values.shape[1])), np.cumsum(values, axis=0)))
            out = np.full(values.shape, np.nan)
            if window <= len(values):
                out[window - 1 :] = prefix[window:] - prefix[:-window]
            return out

        safe = np.where(valid, log_price, 0.0)
        count = rolling_sum(valid.astype(np.float64))
        sy = rolling_sum(safe)
        syy = rolling_sum(safe * safe)
        siy = rolling_sum(safe * index)
        end = index[:, 0]
        start = end - window + 1.0
        sxy = siy - start[:, None] * sy
        sx = window * (window - 1.0) / 2.0
        sxx = window * (window - 1.0) * (2.0 * window - 1.0) / 6.0
        var_x = sxx - sx * sx / window
        cov = sxy - sx * sy / window
        slope = cov / max(var_x, EPS)
        if metric == "slope":
            result = slope
        elif metric == "r2":
            var_y = syy - sy * sy / window
            result = _safe_div(cov * cov, var_x * var_y)
        else:
            raise R0ClassicFactorError("linear_trend metric must be 'slope' or 'r2'")
        return np.where(count == window, result, np.nan).astype(np.float32)

    def _rsi(self, window: int) -> np.ndarray:
        returns = self._field("realized_total_return")
        gains = np.where(np.isfinite(returns), np.maximum(returns, 0.0), np.nan)
        losses = np.where(np.isfinite(returns), np.maximum(-returns, 0.0), np.nan)
        gain = self._rolling_mean(gains, window, key="rsi_gain")
        loss = self._rolling_mean(losses, window, key="rsi_loss")
        denominator = gain + loss
        return np.where(
            denominator > EPS,
            100.0 * gain / denominator,
            np.where((gain == 0.0) & (loss == 0.0), 50.0, np.nan),
        ).astype(np.float32)

    def _macd_component(
        self, fast: int, slow: int, signal: int, component: str, *, percentage: bool
    ) -> np.ndarray:
        if not fast < slow:
            raise R0ClassicFactorError("MACD/PPO requires fast < slow")
        close = self._field("close")
        fast_ema = self._ema(close, fast, key="close")
        slow_ema = self._ema(close, slow, key="close")
        line = (
            _safe_div(fast_ema - slow_ema, slow_ema)
            if percentage
            else fast_ema - slow_ema
        ).astype(np.float32)
        signal_line = self._ema(line, signal, key=f"{'ppo' if percentage else 'macd'}:{fast}:{slow}")
        if component == "line":
            return line
        if component == "signal":
            return signal_line
        if component == "hist":
            return (line - signal_line).astype(np.float32)
        raise R0ClassicFactorError("component must be 'line', 'signal', or 'hist'")

    def _market_regression(self, window: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        cached = [self.cache.get(f"market_regression:{window}:{name}") for name in ("beta", "alpha", "residvol", "corr")]
        if all(value is not None for value in cached):
            return tuple(cached)  # type: ignore[return-value]
        returns = self._field("realized_total_return")
        market = self._daily_market_return()
        market_matrix = np.broadcast_to(market[:, None], returns.shape)
        mean_y, mean_x, var_y, var_x, covariance = self._rolling_pair_stats(
            returns, market_matrix, window
        )
        beta = _safe_div(covariance, var_x)
        alpha = mean_y - beta * mean_x
        residual_variance = np.maximum(var_y - _safe_div(covariance * covariance, var_x), 0.0)
        corr = _safe_div(covariance, np.sqrt(var_y * var_x))
        results = (
            beta.astype(np.float32),
            alpha.astype(np.float32),
            np.sqrt(residual_variance).astype(np.float32),
            corr.astype(np.float32),
        )
        for name, value in zip(("beta", "alpha", "residvol", "corr"), results):
            self.cache.put(f"market_regression:{window}:{name}", value)
        return results

    def _downside_beta(self, window: int) -> np.ndarray:
        returns = self._field("realized_total_return").astype(np.float64)
        market = self._daily_market_return().astype(np.float64)
        market_matrix = np.broadcast_to(market[:, None], returns.shape)
        full = np.isfinite(returns) & np.isfinite(market_matrix)
        selected = full & (market_matrix < 0.0)

        def window_sum(values: np.ndarray) -> np.ndarray:
            prefix = np.vstack((np.zeros((1, values.shape[1])), np.cumsum(values, axis=0)))
            out = np.full(values.shape, np.nan)
            if window <= len(values):
                out[window - 1 :] = prefix[window:] - prefix[:-window]
            return out

        count_full = window_sum(full.astype(np.float64))
        count = window_sum(selected.astype(np.float64))
        x = np.where(selected, market_matrix, 0.0)
        y = np.where(selected, returns, 0.0)
        sx, sy = window_sum(x), window_sum(y)
        sxx, sxy = window_sum(x * x), window_sum(x * y)
        cov = sxy - _safe_div(sx * sy, count)
        var = sxx - _safe_div(sx * sx, count)
        minimum = max(3, int(np.ceil(window * 0.20)))
        result = _safe_div(cov, var)
        return np.where((count_full == window) & (count >= minimum), result, np.nan).astype(np.float32)

    def _supplier_lookback(self, formula: str) -> tuple[int, set[str]]:
        tree = SupplierFormulaEvaluator.parse(formula)

        def visit(node: ast.AST) -> tuple[int, set[str]]:
            if isinstance(node, ast.Name):
                return 1, {node.id.lower()}
            if isinstance(node, ast.Constant):
                return 1, set()
            if isinstance(node, ast.UnaryOp):
                return visit(node.operand)
            if isinstance(node, ast.BinOp):
                left, lf = visit(node.left)
                right, rf = visit(node.right)
                return max(left, right), lf | rf
            if isinstance(node, ast.Call):
                children = [visit(arg) for arg in node.args]
                fields = set().union(*(item[1] for item in children))
                name = node.func.id.lower() if isinstance(node.func, ast.Name) else ""
                base = max((item[0] for item in children), default=1)
                if name in {"delay", "delta"} and len(node.args) == 2:
                    period = int(round(float(ast.literal_eval(node.args[1]))))
                    return children[0][0] + period, fields
                if name in {"sum", "stddev", "ts_min", "ts_max", "ts_rank"} and len(node.args) == 2:
                    period = int(round(float(ast.literal_eval(node.args[1]))))
                    return children[0][0] + period - 1, fields
                if name == "correlation" and len(node.args) == 3:
                    period = int(round(float(ast.literal_eval(node.args[2]))))
                    return max(children[0][0], children[1][0]) + period - 1, fields
                return base, fields
            return 1, set()

        return visit(tree.body)

    def _supplier_dsl(self, formula: str) -> np.ndarray:
        if not formula.strip():
            raise R0ClassicFactorError("supplier_dsl requires formula")
        evaluator = _CausalSupplierFormulaEvaluator(
            self.fields,
            # Fields retain the full eligible history for causal time-series
            # operators, while contemporaneous cross-sectional rank excludes
            # securities that have not yet entered the research universe.
            eligible=self.research_eligible,
            accelerator=self.accelerator,
            gpu_column_chunk=self.gpu_column_chunk,
            classic_engine=self,
        )
        result = evaluator.evaluate(formula)
        lookback, names = self._supplier_lookback(formula)
        mask = np.ones(self.panel.shape, dtype=bool)
        for name in names:
            source = "realized_total_return" if name == "returns" else name
            if source in self.fields:
                mask &= self._strict_mask(self.fields[source], lookback)
        return np.where(mask, result, np.nan).astype(np.float32)

    def _dispatch(
        self, kind: str, params: Mapping[str, Any], spec: Mapping[str, Any]
    ) -> np.ndarray:
        if kind == "supplier_dsl":
            return self._supplier_dsl(str(spec.get("formula", "")))

        if kind in {"macd", "ppo"}:
            fast = _positive_int(params, "fast")
            slow = _positive_int(params, "slow")
            signal = _positive_int(params, "signal")
            component = str(params.get("component", "line"))
            return self._macd_component(fast, slow, signal, component, percentage=kind == "ppo")

        if kind == "residual_momentum":
            formation = _positive_int(params, "formation_window")
            beta_window = _positive_int(params, "beta_window")
            beta, alpha, _, _ = self._market_regression(beta_window)
            market = self._daily_market_return()[:, None]
            residual = self._field("realized_total_return") - alpha - beta * market
            residual_log = np.where(residual > -1.0, np.log1p(residual), np.nan)
            compounded = self._rolling_sum(
                residual_log, formation, key=f"residual_log:{beta_window}"
            )
            return np.expm1(compounded.astype(np.float64)).astype(np.float32)

        window = _positive_int(params, "window")
        returns = self._field("realized_total_return")
        close = self._field("close")
        high = self._field("high")
        low = self._field("low")
        open_ = self._field("open")
        amount = self._field("amount")
        volume = self._field("volume")

        if kind in {"total_return", "roc"}:
            return self._total_return(window)
        if kind == "return_reversal":
            return -self._total_return(window)
        if kind == "return_volatility":
            return self._rolling_std(returns, window, key="return")
        if kind in {"downside_volatility", "upside_volatility"}:
            sided = (
                np.minimum(returns, 0.0) if kind == "downside_volatility" else np.maximum(returns, 0.0)
            )
            square_mean = self._rolling_mean(
                sided * sided, window, key=f"{kind}:square"
            )
            return np.sqrt(np.maximum(square_mean, 0.0)).astype(np.float32)
        if kind == "realized_skewness":
            return self._rolling_moments(returns, window, 3, key="return")
        if kind == "realized_kurtosis":
            return self._rolling_moments(returns, window, 4, key="return")
        if kind == "tail_loss":
            quantile = _finite_float(params, "quantile", 0.05)
            if not 0.0 < quantile < 0.5:
                raise R0ClassicFactorError("tail_loss quantile must be between 0 and 0.5")
            return self._rolling_expected_shortfall(
                returns, window, quantile, key="return"
            )
        if kind == "maximum_drawdown":
            return self._maximum_drawdown(window)
        if kind == "moving_average_ratio":
            return (_safe_div(close, self._rolling_mean(close, window, key="close")) - 1.0).astype(np.float32)
        if kind == "ema_ratio":
            return (_safe_div(close, self._ema(close, window, key="close")) - 1.0).astype(np.float32)
        if kind == "linear_trend":
            return self._linear_trend(window, str(params.get("metric", "slope")))
        if kind == "breakout_position":
            minimum = self._rolling_extreme(low, window, "min", key="low")
            maximum = self._rolling_extreme(high, window, "max", key="high")
            return _safe_div(close - minimum, maximum - minimum).astype(np.float32)
        if kind == "price_location":
            field_name = str(params.get("field", "close"))
            if field_name not in self.fields:
                raise R0ClassicFactorError(f"price_location field unavailable: {field_name}")
            source = self._field(field_name)
            minimum = self._rolling_extreme(source, window, "min", key=field_name)
            maximum = self._rolling_extreme(source, window, "max", key=field_name)
            return _safe_div(source - minimum, maximum - minimum).astype(np.float32)

        previous_close = self._delay(close)
        if kind == "intraday_return":
            daily = _safe_div(close, open_) - 1.0
            return self._stat_window(daily, window, str(params.get("stat", "mean")), key="intraday_return")
        if kind == "overnight_return":
            daily = _safe_div(open_, previous_close) - 1.0
            return self._stat_window(daily, window, str(params.get("stat", "mean")), key="overnight_return")
        if kind == "range_ratio":
            daily = _safe_div(high - low, close)
            return self._stat_window(daily, window, str(params.get("stat", "mean")), key="range_ratio")
        if kind == "shadow_ratio":
            side = str(params.get("side", "upper"))
            spread = high - low
            if side == "upper":
                daily = _safe_div(high - np.maximum(open_, close), spread)
            elif side == "lower":
                daily = _safe_div(np.minimum(open_, close) - low, spread)
            else:
                raise R0ClassicFactorError("shadow_ratio side must be 'upper' or 'lower'")
            return self._stat_window(daily, window, str(params.get("stat", "mean")), key=f"shadow:{side}")
        true_range = np.maximum.reduce(
            (high - low, np.abs(high - previous_close), np.abs(low - previous_close))
        )
        normalized_true_range = _safe_div(true_range, previous_close)
        if kind == "true_range":
            return self._stat_window(
                normalized_true_range, window, str(params.get("stat", "mean")), key="true_range"
            )
        if kind == "atr":
            return _safe_div(
                self._rolling_mean(true_range, window, key="true_range_raw"), close
            ).astype(np.float32)

        if kind in {"amount_mean", "volume_mean"}:
            source, key = (amount, "amount") if kind.startswith("amount") else (volume, "volume")
            mean = self._rolling_mean(source, window, key=key)
            return np.where(mean >= 0.0, np.log1p(mean), np.nan).astype(np.float32)
        if kind in {"amount_ratio", "volume_ratio"}:
            source, key = (amount, "amount") if kind.startswith("amount") else (volume, "volume")
            return _safe_div(source, self._rolling_mean(source, window, key=key)).astype(np.float32)
        if kind in {"amount_volatility", "volume_volatility"}:
            source, key = (amount, "amount") if kind.startswith("amount") else (volume, "volume")
            logged = np.where(source >= 0.0, np.log1p(source), np.nan).astype(np.float32)
            return self._rolling_std(logged, window, key=f"log1p_{key}")
        if kind == "amihud_illiquidity":
            daily = _safe_div(np.abs(returns), amount)
            return (1e8 * self._rolling_mean(daily, window, key="amihud_daily")).astype(np.float32)
        if kind == "zero_return_ratio":
            daily = np.where(np.isfinite(returns), np.abs(returns) <= 1e-12, np.nan).astype(np.float32)
            return self._rolling_mean(daily, window, key="zero_return")
        if kind == "return_amount_corr":
            logged = np.where(amount >= 0.0, np.log1p(amount), np.nan).astype(np.float32)
            return self._rolling_corr(returns, logged, window, key="return:log1p_amount")
        if kind == "return_volume_corr":
            logged = np.where(volume >= 0.0, np.log1p(volume), np.nan).astype(np.float32)
            return self._rolling_corr(returns, logged, window, key="return:log1p_volume")
        if kind == "price_volume_corr":
            logged = np.where(volume >= 0.0, np.log1p(volume), np.nan).astype(np.float32)
            return self._rolling_corr(close, logged, window, key="close:log1p_volume")
        if kind == "obv":
            signed_volume = np.where(np.isfinite(returns) & np.isfinite(volume), np.sign(returns) * volume, np.nan)
            return _safe_div(
                self._rolling_sum(signed_volume, window, key="signed_volume"),
                self._rolling_sum(np.abs(volume), window, key="abs_volume"),
            ).astype(np.float32)
        if kind == "money_flow_volume":
            multiplier = _safe_div((close - low) - (high - close), high - low)
            flow = np.where(np.isfinite(multiplier) & np.isfinite(volume), multiplier * volume, np.nan)
            return _safe_div(
                self._rolling_sum(flow, window, key="money_flow_volume"),
                self._rolling_sum(np.abs(volume), window, key="abs_volume"),
            ).astype(np.float32)
        if kind == "rsi":
            return self._rsi(window)
        if kind in {"bollinger_z", "bollinger_width"}:
            num_std = _finite_float(params, "num_std", 2.0)
            mean = self._rolling_mean(close, window, key="close")
            std = self._rolling_std(close, window, key="close")
            if kind == "bollinger_z":
                return _safe_div(close - mean, std).astype(np.float32)
            return _safe_div(2.0 * num_std * std, mean).astype(np.float32)
        if kind == "stochastic":
            smooth = _positive_int(params, "smooth", 3)
            minimum = self._rolling_extreme(low, window, "min", key="low")
            maximum = self._rolling_extreme(high, window, "max", key="high")
            k = (100.0 * _safe_div(close - minimum, maximum - minimum)).astype(np.float32)
            component = str(params.get("component", "k"))
            if component == "k":
                return k
            if component == "d":
                return self._rolling_mean(k, smooth, key=f"stochastic_k:{window}")
            raise R0ClassicFactorError("stochastic component must be 'k' or 'd'")
        if kind == "williams_r":
            minimum = self._rolling_extreme(low, window, "min", key="low")
            maximum = self._rolling_extreme(high, window, "max", key="high")
            return (-100.0 * _safe_div(maximum - close, maximum - minimum)).astype(np.float32)
        if kind == "cci":
            constant = _finite_float(params, "constant", 0.015)
            if constant <= 0.0:
                raise R0ClassicFactorError("CCI constant must be positive")
            typical = ((high + low + close) / 3.0).astype(np.float32)
            mean = self._rolling_mean(typical, window, key="typical_price")
            mean_deviation = self._rolling_mad(typical, window, key="typical_price")
            return _safe_div(typical - mean, constant * mean_deviation).astype(np.float32)
        if kind in {"beta", "market_corr", "residual_volatility"}:
            beta, _, residual_volatility, corr = self._market_regression(window)
            return {"beta": beta, "market_corr": corr, "residual_volatility": residual_volatility}[kind]
        if kind == "downside_beta":
            return self._downside_beta(window)

        raise R0ClassicFactorError(f"classic factor kind not implemented: {kind}")


__all__ = [
    "ClassicFactorResult",
    "R0ClassicFactorEngine",
    "R0ClassicFactorError",
    "SUPPORTED_KINDS",
]
