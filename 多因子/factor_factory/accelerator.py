"""Optional, explicit CuPy kernels for the factor research engine.

The CPU implementation remains the default.  Selecting ``gpu`` is fail-closed:
missing CuPy, an unavailable CUDA device, OOM, or a non-finite kernel result is
reported instead of silently changing the research backend.
"""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path
from typing import Any

import numpy as np


class AcceleratorError(RuntimeError):
    """Raised when an explicitly requested accelerator cannot be used safely."""


_DLL_HANDLES: list[Any] = []
_GPU_SMOKE_TESTED = False
_CROSS_SECTIONAL_KERNEL: Any | None = None


def _register_cuda_wheel_dlls() -> None:
    """Expose NVIDIA component-wheel DLLs to Windows' secure loader."""

    if os.name != "nt" or not hasattr(os, "add_dll_directory") or _DLL_HANDLES:
        return
    purelib = Path(sysconfig.get_paths()["purelib"])
    added: list[str] = []
    for relative in ("nvidia/cuda_runtime/bin", "nvidia/cuda_nvrtc/bin"):
        directory = purelib / relative
        if directory.is_dir():
            _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
            added.append(str(directory))
    if added:
        os.environ["PATH"] = os.pathsep.join(added + [os.environ.get("PATH", "")])


def _cupy() -> Any:
    global _GPU_SMOKE_TESTED
    _register_cuda_wheel_dlls()
    try:
        import cupy as cp
    except ImportError as exc:
        raise AcceleratorError(
            "GPU mode requires cupy-cuda12x; install requirements-gpu.txt"
        ) from exc
    try:
        if int(cp.cuda.runtime.getDeviceCount()) < 1:
            raise AcceleratorError("GPU mode found no CUDA device")
        cp.cuda.Device(0).use()
        if not _GPU_SMOKE_TESTED:
            probe = cp.arange(16, dtype=cp.float32)
            if float(cp.sum(probe).get()) != 120.0:
                raise AcceleratorError("CUDA kernel smoke test returned an invalid result")
            _GPU_SMOKE_TESTED = True
    except AcceleratorError:
        raise
    except Exception as exc:
        raise AcceleratorError(f"CUDA device initialization failed: {exc}") from exc
    return cp


def accelerator_signature(accelerator: str) -> dict[str, Any]:
    """Return immutable runtime evidence for the selected numerical backend."""

    if accelerator == "cpu":
        return {"accelerator": "cpu", "array_library": "numpy", "numpy": np.__version__}
    if accelerator != "gpu":
        raise ValueError("accelerator must be 'cpu' or 'gpu'")
    cp = _cupy()
    device = cp.cuda.Device(0)
    properties = cp.cuda.runtime.getDeviceProperties(device.id)
    name = properties["name"]
    if isinstance(name, bytes):
        name = name.decode("utf-8", errors="replace")
    return {
        "accelerator": "gpu",
        "array_library": "cupy",
        "cupy": cp.__version__,
        "cuda_runtime": int(cp.cuda.runtime.runtimeGetVersion()),
        "cuda_driver": int(cp.cuda.runtime.driverGetVersion()),
        "device_id": int(device.id),
        "device_name": str(name),
        "compute_capability": list(device.compute_capability),
    }


def _gpu_result(values: Any, dtype: np.dtype[Any], cp: Any) -> np.ndarray:
    result = cp.asnumpy(values).astype(dtype, copy=False)
    return np.where(np.isfinite(result), result, np.nan)


def rolling_gpu(
    values: np.ndarray,
    window: int,
    operation: str,
    *,
    column_chunk: int = 256,
) -> np.ndarray:
    """GPU equivalent of the causal rolling operators, chunked by instrument."""

    cp = _cupy()
    source = np.asarray(values)
    if source.ndim != 2:
        raise ValueError("rolling GPU input must be a 2-D date-by-asset matrix")
    if operation not in {"mean", "std", "min", "max", "rank"}:
        raise ValueError(f"unsupported GPU rolling operation: {operation}")
    if window < 1 or column_chunk < 1:
        raise ValueError("window and GPU column chunk must be positive")
    dtype = np.dtype(np.float32 if source.dtype.itemsize <= 4 else np.float64)
    result = np.full(source.shape, np.nan, dtype=dtype)
    if window > len(source):
        return result
    minimum = max(2, int(np.ceil(window * 0.80)))
    # Correlation needs more simultaneous work arrays than single-input ops;
    # these kernels cap float64 chunks further to stay well inside 6 GB VRAM.
    effective_chunk = min(column_chunk, 128 if source.dtype.itemsize > 4 else column_chunk)
    try:
        for start in range(0, source.shape[1], effective_chunk):
            stop = min(source.shape[1], start + effective_chunk)
            x = cp.asarray(source[:, start:stop])
            valid_all = cp.isfinite(x)
            if operation in {"mean", "std"}:
                any_valid = cp.any(valid_all, axis=0)
                first_valid = cp.argmax(valid_all, axis=0)
                columns = cp.arange(x.shape[1])
                reference = cp.where(any_valid, x[first_valid, columns], 0.0).astype(cp.float64)
                filled = cp.where(valid_all, x - reference[None, :], 0.0).astype(cp.float64)
                zero_i = cp.zeros((1, x.shape[1]), dtype=cp.int32)
                zero_f = cp.zeros((1, x.shape[1]), dtype=cp.float64)
                count_cumulative = cp.concatenate(
                    (zero_i, cp.cumsum(valid_all, axis=0, dtype=cp.int32)), axis=0
                )
                sum_cumulative = cp.concatenate(
                    (zero_f, cp.cumsum(filled, axis=0, dtype=cp.float64)), axis=0
                )
                counts = count_cumulative[window:] - count_cumulative[:-window]
                sums = sum_cumulative[window:] - sum_cumulative[:-window]
                eligible = counts >= minimum
                if operation == "mean":
                    rolled = cp.where(
                        eligible,
                        sums / cp.maximum(counts, 1) + reference[None, :],
                        cp.nan,
                    )
                else:
                    square_cumulative = cp.concatenate(
                        (zero_f, cp.cumsum(filled * filled, axis=0, dtype=cp.float64)),
                        axis=0,
                    )
                    square_sums = square_cumulative[window:] - square_cumulative[:-window]
                    variance = (
                        square_sums - sums * sums / cp.maximum(counts, 1)
                    ) / cp.maximum(counts - 1, 1)
                    rolled = cp.where(eligible, cp.sqrt(cp.maximum(variance, 0.0)), cp.nan)
            else:
                samples = cp.lib.stride_tricks.sliding_window_view(x, window, axis=0)
                valid = cp.isfinite(samples)
                counts = cp.sum(valid, axis=-1)
                eligible = counts >= minimum
                if operation == "min":
                    rolled = cp.where(
                        eligible, cp.min(cp.where(valid, samples, cp.inf), axis=-1), cp.nan
                    )
                elif operation == "max":
                    rolled = cp.where(
                        eligible, cp.max(cp.where(valid, samples, -cp.inf), axis=-1), cp.nan
                    )
                else:
                    current = samples[..., -1]
                    finite_current = cp.isfinite(current)
                    below = cp.sum(valid & (samples < current[..., None]), axis=-1)
                    equal = cp.sum(valid & (samples == current[..., None]), axis=-1)
                    different = cp.any(valid & (samples != current[..., None]), axis=-1)
                    eligible &= finite_current & different
                    ranks = (below + (equal - 1) / 2.0) / cp.maximum(counts - 1, 1)
                    rolled = cp.where(eligible, ranks, cp.nan)
            result[window - 1 :, start:stop] = _gpu_result(rolled, dtype, cp)
            del x, valid_all, rolled
            cp.get_default_memory_pool().free_all_blocks()
        return result
    except cp.cuda.memory.OutOfMemoryError as exc:
        cp.get_default_memory_pool().free_all_blocks()
        raise AcceleratorError(
            f"GPU rolling OOM; reduce gpu_column_chunk below {effective_chunk}"
        ) from exc
    except AcceleratorError:
        raise
    except Exception as exc:
        raise AcceleratorError(f"GPU rolling kernel failed: {exc}") from exc


def rolling_corr_gpu(
    left: np.ndarray,
    right: np.ndarray,
    window: int,
    *,
    column_chunk: int = 128,
) -> np.ndarray:
    """GPU rolling correlation with pairwise finite observations.

    The covariance calculation is deliberately two-pass and mean-centred.
    Computing ``sum(x*y) - sum(x)*sum(y)/n`` is catastrophically unstable for
    price/volume levels with a large offset and small within-window movement;
    it previously made several supplier formulas backend-dependent.  CuPy
    reductions remain float64 and the column chunks bound the extra centred
    work arrays in VRAM.
    """

    cp = _cupy()
    x_source = np.asarray(left)
    y_source = np.asarray(right)
    if x_source.shape != y_source.shape or x_source.ndim != 2:
        raise ValueError("GPU rolling correlation inputs must be equal 2-D matrices")
    dtype = np.dtype(np.float32 if x_source.dtype.itemsize <= 4 else np.float64)
    result = np.full(x_source.shape, np.nan, dtype=dtype)
    if window > len(x_source):
        return result
    minimum = max(3, int(np.ceil(window * 0.80)))
    effective_chunk = min(column_chunk, 64 if x_source.dtype.itemsize <= 4 else 32)
    try:
        for start in range(0, x_source.shape[1], effective_chunk):
            stop = min(x_source.shape[1], start + effective_chunk)
            x = cp.asarray(x_source[:, start:stop])
            y = cp.asarray(y_source[:, start:stop])
            xw = cp.lib.stride_tricks.sliding_window_view(x, window, axis=0)
            yw = cp.lib.stride_tricks.sliding_window_view(y, window, axis=0)
            valid = cp.isfinite(xw) & cp.isfinite(yw)
            count = cp.sum(valid, axis=-1)
            divisor = cp.maximum(count, 1)
            sum_x = cp.sum(cp.where(valid, xw, 0.0), axis=-1, dtype=cp.float64)
            sum_y = cp.sum(cp.where(valid, yw, 0.0), axis=-1, dtype=cp.float64)
            mean_x = sum_x / divisor
            mean_y = sum_y / divisor
            centered_x = cp.where(valid, xw - mean_x[..., None], 0.0).astype(
                cp.float64, copy=False
            )
            centered_y = cp.where(valid, yw - mean_y[..., None], 0.0).astype(
                cp.float64, copy=False
            )
            covariance = cp.sum(centered_x * centered_y, axis=-1, dtype=cp.float64)
            variance_x = cp.sum(centered_x * centered_x, axis=-1, dtype=cp.float64)
            variance_y = cp.sum(centered_y * centered_y, axis=-1, dtype=cp.float64)
            denominator = cp.sqrt(cp.maximum(variance_x, 0.0) * cp.maximum(variance_y, 0.0))
            current_x = xw[..., -1]
            current_y = yw[..., -1]
            eligible = (
                (count >= minimum)
                & cp.isfinite(current_x)
                & cp.isfinite(current_y)
                & (denominator > 1e-12)
            )
            rolled = cp.where(eligible, covariance / denominator, cp.nan)
            # Cross-sectional ranks amplify backend-level last-bit noise around
            # theoretical ties.  Six decimals is far below the statistical
            # resolution of a rolling correlation and makes those ties stable.
            rolled = cp.where(cp.isfinite(rolled), cp.rint(rolled * 1.0e5) / 1.0e5, cp.nan)
            result[window - 1 :, start:stop] = _gpu_result(rolled, dtype, cp)
            del x, y, xw, yw, valid, centered_x, centered_y, rolled
            cp.get_default_memory_pool().free_all_blocks()
        return result
    except cp.cuda.memory.OutOfMemoryError as exc:
        cp.get_default_memory_pool().free_all_blocks()
        raise AcceleratorError(
            f"GPU correlation OOM; reduce gpu_column_chunk below {effective_chunk}"
        ) from exc
    except Exception as exc:
        raise AcceleratorError(f"GPU correlation kernel failed: {exc}") from exc


def permutation_extreme_count_gpu(
    full_ic: np.ndarray,
    observed_positions: np.ndarray,
    *,
    permutations: int,
    block_length: int,
    seed: int,
    batch_size: int = 4096,
) -> int:
    """Count sign-flip null means at least as extreme as the observed mean.

    NumPy generates the signs so CPU and GPU consume the identical random
    experiment.  Only the large float64 reduction is offloaded.
    """

    cp = _cupy()
    values = np.asarray(full_ic, dtype=np.float64)
    positions = np.asarray(observed_positions, dtype=np.int64)
    if permutations < 1 or len(positions) < 1:
        return 0
    rng = np.random.default_rng(seed)
    block_count = int(np.ceil(len(values) / block_length))
    # Generate the full block calendar in one NumPy call.  NumPy's ``choice``
    # may consume its bit stream differently across separate shaped calls, so
    # batching only the transfer is required for exact CPU/GPU resampling.
    block_signs = rng.choice((-1.0, 1.0), size=(permutations, block_count))
    threshold = abs(float(np.mean(values[positions])))
    tolerance = 64.0 * np.finfo(np.float64).eps * max(
        1.0, float(np.max(np.abs(values[positions])))
    )
    observed_gpu = cp.asarray(values[positions], dtype=cp.float64)
    positions_gpu = cp.asarray(positions, dtype=cp.int64)
    extreme = 0
    try:
        for start in range(0, permutations, batch_size):
            count = min(batch_size, permutations - start)
            signs = np.repeat(
                block_signs[start : start + count], block_length, axis=1
            )[:, : len(values)]
            signs_gpu = cp.asarray(signs, dtype=cp.float64)
            null_means = cp.abs(cp.mean(signs_gpu[:, positions_gpu] * observed_gpu[None, :], axis=1))
            extreme += int(cp.asnumpy(cp.sum(null_means >= threshold - tolerance)))
            del signs_gpu, null_means
        return extreme
    except cp.cuda.memory.OutOfMemoryError as exc:
        cp.get_default_memory_pool().free_all_blocks()
        raise AcceleratorError("GPU permutation OOM; reduce permutation batch size") from exc
    except Exception as exc:
        raise AcceleratorError(f"GPU permutation kernel failed: {exc}") from exc


def _cross_sectional_kernel(cp: Any) -> Any:
    global _CROSS_SECTIONAL_KERNEL
    if _CROSS_SECTIONAL_KERNEL is None:
        _CROSS_SECTIONAL_KERNEL = cp.RawKernel(
            r'''
            extern "C" __global__
            void rank_tail_rows(
                const double* x,
                const double* y,
                const long long* x_pair_order,
                const long long* y_pair_order,
                const long long* x_tail_order,
                const long long* pair_counts,
                const long long* tail_counts,
                const int rows,
                const int cols,
                const double quantile_fraction,
                const long long tail_count,
                double* x_rank,
                double* y_rank,
                double* top_weight,
                double* bottom_weight,
                unsigned char* ic_variable,
                unsigned char* tail_variable)
            {
                const int row = (int)blockIdx.x;
                if (row >= rows || threadIdx.x != 0) return;
                const long long base = (long long)row * cols;
                int x_groups = 0;
                const long long pair_n = pair_counts[row];
                long long start = 0;
                while (start < pair_n) {
                    const long long first_index = x_pair_order[base + start];
                    const double level = x[base + first_index];
                    long long end = start + 1;
                    while (end < pair_n && x[base + x_pair_order[base + end]] == level) ++end;
                    const double average = 0.5 * ((double)start + (double)(end - 1));
                    for (long long position = start; position < end; ++position) {
                        const long long original = x_pair_order[base + position];
                        x_rank[base + original] = average;
                    }
                    ++x_groups;
                    start = end;
                }
                int y_groups = 0;
                start = 0;
                while (start < pair_n) {
                    const long long first_index = y_pair_order[base + start];
                    const double level = y[base + first_index];
                    long long end = start + 1;
                    while (end < pair_n && y[base + y_pair_order[base + end]] == level) ++end;
                    const double average = 0.5 * ((double)start + (double)(end - 1));
                    for (long long position = start; position < end; ++position) {
                        const long long original = y_pair_order[base + position];
                        y_rank[base + original] = average;
                    }
                    ++y_groups;
                    start = end;
                }
                ic_variable[row] = (x_groups > 1 && y_groups > 1) ? 1 : 0;

                const long long tail_n = tail_counts[row];
                if (tail_n < 2) { tail_variable[row] = 0; return; }
                long long q = tail_count > 0 ? tail_count : (long long)(tail_n * quantile_fraction);
                if (q > tail_n) q = tail_n;
                if (q < 1) q = 1;
                int tail_groups = 0;
                start = 0;
                while (start < tail_n) {
                    const long long first_index = x_tail_order[base + start];
                    const double level = x[base + first_index];
                    long long end = start + 1;
                    while (end < tail_n && x[base + x_tail_order[base + end]] == level) ++end;
                    const long long group_size = end - start;
                    long long top_units = end - (tail_n - q);
                    if (top_units < 0) top_units = 0;
                    if (top_units > group_size) top_units = group_size;
                    long long bottom_units = q - start;
                    if (bottom_units < 0) bottom_units = 0;
                    if (bottom_units > group_size) bottom_units = group_size;
                    const double top = ((double)top_units / (double)group_size) / (double)q;
                    const double bottom = ((double)bottom_units / (double)group_size) / (double)q;
                    for (long long position = start; position < end; ++position) {
                        const long long original = x_tail_order[base + position];
                        top_weight[base + original] = top;
                        bottom_weight[base + original] = bottom;
                    }
                    ++tail_groups;
                    start = end;
                }
                tail_variable[row] = tail_groups > 1 ? 1 : 0;
            }
            ''',
            "rank_tail_rows",
        )
    return _CROSS_SECTIONAL_KERNEL


def cross_sectional_factor_metrics_gpu(
    values: np.ndarray,
    targets: np.ndarray,
    indices: np.ndarray,
    *,
    minimum_assets: int,
    quantile_fraction: float,
    tail_count: int | None = None,
    eligible: np.ndarray | None = None,
) -> dict[str, Any]:
    """Batch causal daily ranks, fixed tail membership, returns, and turnover."""

    cp = _cupy()
    requested = np.asarray(indices, dtype=np.int64)
    x_source = np.asarray(values)[requested]
    y_source = np.asarray(targets)[requested]
    if x_source.shape != y_source.shape or x_source.ndim != 2:
        raise ValueError("GPU cross-sectional inputs must be equal 2-D matrices")
    eligible_source = None if eligible is None else np.asarray(eligible)[requested]
    if eligible_source is not None:
        if eligible_source.shape != x_source.shape or eligible_source.dtype.kind != "b":
            raise ValueError("GPU eligible mask must be boolean and match inputs")
    try:
        x = cp.asarray(x_source, dtype=cp.float64)
        y = cp.asarray(y_source, dtype=cp.float64)
        research_eligible = (
            cp.ones_like(x, dtype=cp.bool_)
            if eligible_source is None
            else cp.asarray(eligible_source, dtype=cp.bool_)
        )
        decision_valid = cp.isfinite(x) & research_eligible
        target_valid = cp.isfinite(y) & research_eligible
        paired_valid = decision_valid & target_valid
        decision_counts = cp.sum(decision_valid, axis=1, dtype=cp.int64)
        paired_counts = cp.sum(paired_valid, axis=1, dtype=cp.int64)
        possible = int(cp.asnumpy(cp.sum(target_valid)))
        target_counts = cp.sum(target_valid, axis=1, dtype=cp.int64)
        possible_dates = int(cp.asnumpy(cp.sum(target_counts >= minimum_assets)))
        x_pair_key = cp.where(paired_valid, x, cp.inf)
        y_pair_key = cp.where(paired_valid, y, cp.inf)
        x_tail_key = cp.where(decision_valid, x, cp.inf)
        x_pair_order = cp.argsort(x_pair_key, axis=1)
        y_pair_order = cp.argsort(y_pair_key, axis=1)
        x_tail_order = cp.argsort(x_tail_key, axis=1)
        x_rank = cp.zeros_like(x, dtype=cp.float64)
        y_rank = cp.zeros_like(y, dtype=cp.float64)
        top_weight = cp.zeros_like(x, dtype=cp.float64)
        bottom_weight = cp.zeros_like(x, dtype=cp.float64)
        ic_variable = cp.zeros(x.shape[0], dtype=cp.uint8)
        tail_variable = cp.zeros(x.shape[0], dtype=cp.uint8)
        _cross_sectional_kernel(cp)(
            (x.shape[0],),
            (1,),
            (
                x,
                y,
                x_pair_order,
                y_pair_order,
                x_tail_order,
                paired_counts,
                decision_counts,
                np.int32(x.shape[0]),
                np.int32(x.shape[1]),
                np.float64(quantile_fraction),
                np.int64(0 if tail_count is None else tail_count),
                x_rank,
                y_rank,
                top_weight,
                bottom_weight,
                ic_variable,
                tail_variable,
            ),
        )
        rank_mean = (paired_counts.astype(cp.float64) - 1.0) / 2.0
        centered_x = cp.where(paired_valid, x_rank - rank_mean[:, None], 0.0)
        centered_y = cp.where(paired_valid, y_rank - rank_mean[:, None], 0.0)
        covariance = cp.sum(centered_x * centered_y, axis=1, dtype=cp.float64)
        variance_x = cp.sum(centered_x * centered_x, axis=1, dtype=cp.float64)
        variance_y = cp.sum(centered_y * centered_y, axis=1, dtype=cp.float64)
        denominator = cp.sqrt(variance_x * variance_y)
        evaluable = (
            (paired_counts >= minimum_assets)
            & (ic_variable != 0)
            & cp.isfinite(denominator)
            & (denominator > 1e-12)
        )
        ic = cp.where(evaluable, covariance / denominator, cp.nan)
        tail_evaluable = (decision_counts >= minimum_assets) & (tail_variable != 0)
        safe_y = cp.where(paired_valid, y, 0.0)
        mean_y = cp.sum(safe_y, axis=1, dtype=cp.float64) / cp.maximum(paired_counts, 1)
        top_observed_weight = cp.sum(
            top_weight * paired_valid, axis=1, dtype=cp.float64
        )
        bottom_observed_weight = cp.sum(
            bottom_weight * paired_valid, axis=1, dtype=cp.float64
        )
        top_return = cp.sum(top_weight * safe_y, axis=1, dtype=cp.float64) / cp.maximum(
            top_observed_weight, 1e-300
        )
        bottom_return = cp.sum(
            bottom_weight * safe_y, axis=1, dtype=cp.float64
        ) / cp.maximum(bottom_observed_weight, 1e-300)
        top_return_evaluable = tail_evaluable & (paired_counts > 0) & (top_observed_weight > 1e-12)
        bottom_return_evaluable = (
            tail_evaluable & (paired_counts > 0) & (bottom_observed_weight > 1e-12)
        )
        spread_evaluable = top_return_evaluable & bottom_return_evaluable
        top_excess = cp.where(top_return_evaluable, top_return - mean_y, cp.nan)
        bottom_excess = cp.where(bottom_return_evaluable, bottom_return - mean_y, cp.nan)
        spread = cp.where(spread_evaluable, top_return - bottom_return, cp.nan)
        evaluable_positions = cp.flatnonzero(evaluable)
        tail_positions = cp.flatnonzero(tail_evaluable)

        def tail_turnover(weights: Any) -> float:
            if int(tail_positions.size) < 1:
                return 0.0
            selected_weights = weights[tail_positions]
            turnover_sum = cp.asarray(1.0, dtype=cp.float64)
            if int(selected_weights.shape[0]) > 1:
                turnover_sum += 0.5 * cp.sum(
                    cp.abs(selected_weights[1:] - selected_weights[:-1]),
                    dtype=cp.float64,
                )
            return float(cp.asnumpy(turnover_sum)) / int(selected_weights.shape[0])

        top_turnover = tail_turnover(top_weight)
        bottom_turnover = tail_turnover(bottom_weight)
        top_missing_weight = (
            float(cp.asnumpy(cp.mean(1.0 - top_observed_weight[tail_positions])))
            if int(tail_positions.size) > 0
            else 0.0
        )
        bottom_missing_weight = (
            float(cp.asnumpy(cp.mean(1.0 - bottom_observed_weight[tail_positions])))
            if int(tail_positions.size) > 0
            else 0.0
        )
        top_positions = cp.flatnonzero(top_return_evaluable)
        bottom_positions = cp.flatnonzero(bottom_return_evaluable)
        spread_positions = cp.flatnonzero(spread_evaluable)
        result = {
            "observations": int(cp.asnumpy(cp.sum(paired_counts))),
            "decision_observations": int(cp.asnumpy(cp.sum(decision_counts))),
            "missing_outcome_observations": int(
                cp.asnumpy(cp.sum(decision_counts - paired_counts))
            ),
            "possible": possible,
            "possible_dates": possible_dates,
            "daily_positions": cp.asnumpy(evaluable_positions).astype(np.int64, copy=False),
            "daily_ic": cp.asnumpy(ic[evaluable_positions]).astype(np.float64, copy=False),
            "daily_top_positions": cp.asnumpy(top_positions).astype(np.int64, copy=False),
            "daily_top_excess": cp.asnumpy(top_excess[top_positions]).astype(
                np.float64, copy=False
            ),
            "daily_bottom_positions": cp.asnumpy(bottom_positions).astype(np.int64, copy=False),
            "daily_bottom_excess": cp.asnumpy(bottom_excess[bottom_positions]).astype(
                np.float64, copy=False
            ),
            "daily_spread_positions": cp.asnumpy(spread_positions).astype(np.int64, copy=False),
            "daily_spread": cp.asnumpy(spread[spread_positions]).astype(
                np.float64, copy=False
            ),
            "tail_selection_dates": int(tail_positions.size),
            "top_return_dates": int(top_positions.size),
            "bottom_return_dates": int(bottom_positions.size),
            "spread_return_dates": int(spread_positions.size),
            "top_tail_missing_outcome_weight_mean": top_missing_weight,
            "bottom_tail_missing_outcome_weight_mean": bottom_missing_weight,
            "top_tail_turnover": top_turnover,
            "bottom_tail_turnover": bottom_turnover,
            "turnover": top_turnover,
        }
        del (
            x,
            y,
            decision_valid,
            research_eligible,
            target_valid,
            paired_valid,
            x_pair_order,
            y_pair_order,
            x_tail_order,
            x_rank,
            y_rank,
            top_weight,
            bottom_weight,
        )
        cp.get_default_memory_pool().free_all_blocks()
        return result
    except cp.cuda.memory.OutOfMemoryError as exc:
        cp.get_default_memory_pool().free_all_blocks()
        raise AcceleratorError("GPU cross-sectional scoring ran out of memory") from exc
    except AcceleratorError:
        raise
    except Exception as exc:
        raise AcceleratorError(f"GPU cross-sectional scoring failed: {exc}") from exc
