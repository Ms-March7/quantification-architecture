#!/usr/bin/env python3
"""Causal champion-challenger gate for short-cycle A-share research.

This module is deliberately research-only.  It uses the Windows-native,
source-stamped market-data adapter, builds end-of-day signals, assumes
next-session-open entry, enforces
an A-share-compatible minimum holding period, and compares an interpretable
ridge champion with a small PyTorch challenger on expanding time folds.

The challenger is never promoted because it is newer or because it used a GPU.
It must improve cost-adjusted out-of-sample results across time folds and remain
positive when the assumed round-trip cost is doubled.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np

try:
    from cn_market_data import fetch_public_bundle, json_safe
except ImportError:  # pragma: no cover - package-style import
    from .cn_market_data import fetch_public_bundle, json_safe


EPS = 1e-12
FEATURE_NAMES: Tuple[str, ...] = (
    "return_1d",
    "momentum_3d",
    "momentum_5d",
    "momentum_10d",
    "momentum_20d",
    "volatility_5d",
    "volatility_20d",
    "downside_volatility_20d",
    "drawdown_20d",
    "volume_z_20d",
    "amount_z_20d",
    "amihud_z_20d",
    "return_rank",
    "momentum_5d_rank",
    "momentum_20d_rank",
    "volatility_20d_rank",
    "volume_z_20d_rank",
    "benchmark_return_1d",
    "benchmark_momentum_5d",
    "benchmark_volatility_20d",
)


@dataclass(frozen=True)
class Panel:
    symbols: Tuple[str, ...]
    benchmark: str
    dates: Tuple[str, ...]
    features: np.ndarray  # decision_dates x assets x features
    excess_returns: np.ndarray  # next-open to eligible-exit vs benchmark
    asset_returns: np.ndarray
    benchmark_returns: np.ndarray
    volatility: np.ndarray
    daily_returns: np.ndarray  # all aligned close-to-close returns
    decision_source_indexes: np.ndarray


@dataclass(frozen=True)
class Standardizer:
    mean: np.ndarray
    scale: np.ndarray

    def transform(self, values: np.ndarray) -> np.ndarray:
        return (np.asarray(values, dtype=np.float64) - self.mean) / self.scale


def finite(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def safe_std(values: np.ndarray) -> float:
    clean = np.asarray(values, dtype=np.float64)
    clean = clean[np.isfinite(clean)]
    if clean.size < 2:
        return 0.0
    return float(np.std(clean, ddof=1))


def zscore_last(values: np.ndarray) -> float:
    scale = safe_std(values)
    if scale <= EPS:
        return 0.0
    return float((values[-1] - np.mean(values)) / scale)


def percentile_ranks(values: Sequence[float]) -> np.ndarray:
    clean = np.asarray(values, dtype=np.float64)
    if clean.size <= 1:
        return np.zeros(clean.size, dtype=np.float64)
    order = np.argsort(clean, kind="stable")
    ranks = np.empty(clean.size, dtype=np.float64)
    ranks[order] = np.arange(clean.size, dtype=np.float64)
    return ranks / (clean.size - 1.0) - 0.5


def rank_correlation(left: np.ndarray, right: np.ndarray) -> float:
    if len(left) < 3 or safe_std(left) <= EPS or safe_std(right) <= EPS:
        return 0.0
    left_rank = percentile_ranks(left)
    right_rank = percentile_ranks(right)
    value = float(np.corrcoef(left_rank, right_rank)[0, 1])
    return value if math.isfinite(value) else 0.0


def normalize_bars(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, float]]:
    normalized: Dict[str, Dict[str, float]] = {}
    for row in rows:
        timestamp = str(row.get("time") or row.get("date") or "").strip()
        if not timestamp:
            continue
        date = timestamp[:10]
        close = finite(row.get("close"))
        open_price = finite(row.get("open"), close)
        if close <= 0.0 or open_price <= 0.0:
            continue
        volume = max(0.0, finite(row.get("volume")))
        amount = max(0.0, finite(row.get("amount"), close * volume))
        normalized[date] = {
            "open": open_price,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    return normalized


def align_bars(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Tuple[Tuple[str, ...], Tuple[str, ...], Dict[str, np.ndarray]]:
    if len(bars_by_symbol) < 3:
        raise ValueError("at least three instruments are required")
    normalized = {symbol: normalize_bars(rows) for symbol, rows in bars_by_symbol.items()}
    symbols = tuple(sorted(normalized))
    common = set(normalized[symbols[0]])
    for symbol in symbols[1:]:
        common &= set(normalized[symbol])
    dates = tuple(sorted(common))
    if len(dates) < 80:
        raise ValueError(f"at least 80 aligned daily bars are required; found {len(dates)}")
    fields: Dict[str, np.ndarray] = {}
    for field in ("open", "close", "volume", "amount"):
        fields[field] = np.asarray(
            [[normalized[symbol][date][field] for symbol in symbols] for date in dates],
            dtype=np.float64,
        )
    return symbols, dates, fields


def build_panel(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    benchmark: str,
    holding_days: int = 3,
) -> Panel:
    if holding_days < 2:
        raise ValueError("holding_days must be at least 2 for next-open entry under T+1")
    symbols, all_dates, fields = align_bars(bars_by_symbol)
    if benchmark not in symbols:
        raise ValueError(f"benchmark {benchmark} is not in the aligned universe")
    benchmark_index = symbols.index(benchmark)
    opens = fields["open"]
    closes = fields["close"]
    volumes = fields["volume"]
    amounts = fields["amount"]
    daily_returns = np.zeros_like(closes)
    daily_returns[1:] = np.log(closes[1:] / closes[:-1])
    log_volume = np.log1p(volumes)
    log_amount = np.log1p(amounts)
    amihud = np.abs(daily_returns) / np.maximum(amounts, 1.0)

    rows: List[np.ndarray] = []
    targets: List[np.ndarray] = []
    forward_assets: List[np.ndarray] = []
    forward_benchmarks: List[float] = []
    volatility_rows: List[np.ndarray] = []
    dates: List[str] = []
    source_indexes: List[int] = []
    last_decision = len(all_dates) - holding_days - 1
    for index in range(20, last_decision + 1):
        return_1 = daily_returns[index]
        momentum_3 = np.sum(daily_returns[index - 2:index + 1], axis=0)
        momentum_5 = np.sum(daily_returns[index - 4:index + 1], axis=0)
        momentum_10 = np.sum(daily_returns[index - 9:index + 1], axis=0)
        momentum_20 = np.sum(daily_returns[index - 19:index + 1], axis=0)
        vol_5 = np.std(daily_returns[index - 4:index + 1], axis=0, ddof=1)
        vol_20 = np.std(daily_returns[index - 19:index + 1], axis=0, ddof=1)
        downside = np.asarray([
            safe_std(window[window < 0.0])
            for window in daily_returns[index - 19:index + 1].T
        ])
        rolling_high = np.max(closes[index - 19:index + 1], axis=0)
        drawdown = closes[index] / rolling_high - 1.0
        volume_z = np.asarray([
            zscore_last(log_volume[index - 19:index + 1, asset])
            for asset in range(len(symbols))
        ])
        amount_z = np.asarray([
            zscore_last(log_amount[index - 19:index + 1, asset])
            for asset in range(len(symbols))
        ])
        amihud_z = np.asarray([
            zscore_last(amihud[index - 19:index + 1, asset])
            for asset in range(len(symbols))
        ])
        ranks = (
            percentile_ranks(return_1),
            percentile_ranks(momentum_5),
            percentile_ranks(momentum_20),
            percentile_ranks(vol_20),
            percentile_ranks(volume_z),
        )
        benchmark_features = (
            float(return_1[benchmark_index]),
            float(momentum_5[benchmark_index]),
            float(vol_20[benchmark_index]),
        )
        feature_row = np.column_stack([
            return_1,
            momentum_3,
            momentum_5,
            momentum_10,
            momentum_20,
            vol_5,
            vol_20,
            downside,
            drawdown,
            volume_z,
            amount_z,
            amihud_z,
            *ranks,
            *[np.full(len(symbols), value, dtype=np.float64) for value in benchmark_features],
        ])
        entry_index = index + 1
        exit_index = index + holding_days
        asset_forward = closes[exit_index] / opens[entry_index] - 1.0
        benchmark_forward = float(asset_forward[benchmark_index])
        rows.append(np.nan_to_num(feature_row, nan=0.0, posinf=0.0, neginf=0.0))
        targets.append(asset_forward - benchmark_forward)
        forward_assets.append(asset_forward)
        forward_benchmarks.append(benchmark_forward)
        volatility_rows.append(vol_20)
        dates.append(all_dates[index])
        source_indexes.append(index)

    features = np.asarray(rows, dtype=np.float64)
    if features.shape[-1] != len(FEATURE_NAMES):
        raise AssertionError(f"feature contract mismatch: {features.shape[-1]} vs {len(FEATURE_NAMES)}")
    return Panel(
        symbols=symbols,
        benchmark=benchmark,
        dates=tuple(dates),
        features=features,
        excess_returns=np.asarray(targets, dtype=np.float64),
        asset_returns=np.asarray(forward_assets, dtype=np.float64),
        benchmark_returns=np.asarray(forward_benchmarks, dtype=np.float64),
        volatility=np.asarray(volatility_rows, dtype=np.float64),
        daily_returns=daily_returns,
        decision_source_indexes=np.asarray(source_indexes, dtype=np.int32),
    )


def flatten_dates(values: np.ndarray, date_indexes: Sequence[int]) -> np.ndarray:
    return np.asarray(values[np.asarray(date_indexes, dtype=np.int32)], dtype=np.float64).reshape(
        -1, values.shape[-1]
    )


def purged_fit_validation_indexes(
    train_indexes: Sequence[int],
    validation_dates: int,
    holding_days: int,
) -> Tuple[List[int], List[int]]:
    indexes = list(int(value) for value in train_indexes)
    validation_dates = min(max(10, validation_dates), max(10, len(indexes) // 3))
    validation_start = len(indexes) - validation_dates
    fit_stop = validation_start - holding_days
    if fit_stop < 20:
        raise ValueError("insufficient fit dates after validation-boundary purge")
    return indexes[:fit_stop], indexes[-validation_dates:]


def fit_standardizer(values: np.ndarray) -> Standardizer:
    mean = np.mean(values, axis=0)
    scale = np.std(values, axis=0)
    scale = np.where(scale < 1e-8, 1.0, scale)
    return Standardizer(mean=mean, scale=scale)


def fit_ridge(
    features: np.ndarray,
    target: np.ndarray,
    l2: float = 8.0,
) -> Tuple[np.ndarray, Standardizer, float]:
    standardizer = fit_standardizer(features)
    x = standardizer.transform(features)
    y_mean = float(np.mean(target))
    centered_y = np.asarray(target, dtype=np.float64) - y_mean
    system = x.T @ x + max(1e-8, l2) * np.eye(x.shape[1], dtype=np.float64)
    beta = np.linalg.solve(system, x.T @ centered_y)
    return beta, standardizer, y_mean


def predict_ridge(
    model: Tuple[np.ndarray, Standardizer, float],
    features: np.ndarray,
) -> np.ndarray:
    beta, standardizer, y_mean = model
    return standardizer.transform(features) @ beta + y_mean


def choose_device(requested: str) -> str:
    if requested == "cpu":
        return "cpu"
    try:
        import torch
    except ImportError:
        if requested == "cuda":
            raise RuntimeError("PyTorch is required for the CUDA challenger")
        return "cpu"
    if requested == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but torch.cuda.is_available() is false")
    return "cuda" if requested == "cuda" else "cpu"


def fit_torch_mlp(
    train_x: np.ndarray,
    train_y: np.ndarray,
    validation_x: np.ndarray,
    validation_y: np.ndarray,
    requested_device: str,
    seed: int,
    epochs: int,
) -> Tuple[Any, Standardizer, str, Dict[str, float]]:
    import torch
    from torch import nn

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    device_name = choose_device(requested_device)
    device = torch.device(device_name)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(seed)
    standardizer = fit_standardizer(train_x)
    x_train = torch.as_tensor(standardizer.transform(train_x), dtype=torch.float32)
    y_train = torch.as_tensor(train_y[:, None], dtype=torch.float32)
    x_valid = torch.as_tensor(standardizer.transform(validation_x), dtype=torch.float32, device=device)
    y_valid = torch.as_tensor(validation_y[:, None], dtype=torch.float32, device=device)
    model = nn.Sequential(
        nn.Linear(train_x.shape[1], 64),
        nn.GELU(),
        nn.Dropout(0.10),
        nn.Linear(64, 32),
        nn.GELU(),
        nn.Linear(32, 1),
    ).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=8e-4, weight_decay=2e-3)
    loss_fn = nn.HuberLoss(delta=0.015)
    generator = torch.Generator().manual_seed(seed)
    best_state = None
    best_loss = float("inf")
    patience = 0
    batch_size = min(512, max(32, len(x_train)))
    for _ in range(max(1, epochs)):
        order = torch.randperm(len(x_train), generator=generator)
        model.train()
        for start in range(0, len(order), batch_size):
            indexes = order[start:start + batch_size]
            batch_x = x_train[indexes].to(device)
            batch_y = y_train[indexes].to(device)
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(model(batch_x), batch_y)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            optimizer.step()
        model.eval()
        with torch.no_grad():
            validation_loss = float(loss_fn(model(x_valid), y_valid).item())
        if validation_loss + 1e-8 < best_loss:
            best_loss = validation_loss
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
            patience = 0
        else:
            patience += 1
            if patience >= 12:
                break
    if best_state is None:
        raise RuntimeError("challenger training did not produce a valid checkpoint")
    model.load_state_dict(best_state)
    model.eval()
    return model, standardizer, device_name, {"bestValidationHuber": best_loss}


def predict_torch(model: Any, standardizer: Standardizer, device: str, features: np.ndarray) -> np.ndarray:
    import torch

    x = torch.as_tensor(standardizer.transform(features), dtype=torch.float32, device=torch.device(device))
    model.eval()
    with torch.no_grad():
        return model(x).squeeze(-1).detach().cpu().numpy().astype(np.float64)


def correlation_matrix(panel: Panel, decision_index: int, lookback: int = 120) -> np.ndarray:
    source_index = int(panel.decision_source_indexes[decision_index])
    start = max(1, source_index - max(20, lookback) + 1)
    window = panel.daily_returns[start:source_index + 1]
    matrix = np.corrcoef(window.T)
    return np.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)


def select_diversified(
    predictions: np.ndarray,
    correlations: np.ndarray,
    top_k: int,
    correlation_cap: float,
    require_positive_prediction: bool = True,
    excluded_indexes: Sequence[int] = (),
) -> List[int]:
    selected: List[int] = []
    excluded = set(int(value) for value in excluded_indexes)
    for index in np.argsort(predictions)[::-1]:
        if int(index) in excluded:
            continue
        if require_positive_prediction and float(predictions[index]) <= 0.0:
            continue
        if any(float(correlations[index, prior]) >= correlation_cap for prior in selected):
            continue
        selected.append(int(index))
        if len(selected) >= top_k:
            break
    return selected


def max_drawdown(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    wealth = np.cumprod(1.0 + np.asarray(returns, dtype=np.float64))
    peak = np.maximum.accumulate(wealth)
    return float(np.min(wealth / peak - 1.0))


def evaluation_metrics(
    predictions: np.ndarray,
    panel: Panel,
    test_indexes: Sequence[int],
    top_k: int,
    correlation_cap: float,
    cost_bps: float,
    holding_days: int,
) -> Dict[str, Any]:
    returns: List[float] = []
    absolute_returns: List[float] = []
    double_cost_returns: List[float] = []
    information_coefficients: List[float] = []
    holding_counts: List[int] = []
    positive_selected = 0
    selected_observations = 0
    stride_indexes = list(test_indexes)[::holding_days]
    benchmark_index = panel.symbols.index(panel.benchmark)
    for decision_index in stride_indexes:
        prediction = predictions[decision_index]
        correlations = correlation_matrix(panel, decision_index)
        selected = select_diversified(
            prediction,
            correlations,
            top_k,
            correlation_cap,
            excluded_indexes=(benchmark_index,),
        )
        information_coefficients.append(rank_correlation(prediction, panel.excess_returns[decision_index]))
        holding_counts.append(len(selected))
        if not selected:
            returns.append(0.0)
            absolute_returns.append(0.0)
            double_cost_returns.append(0.0)
            continue
        gross_alpha = float(np.mean(panel.excess_returns[decision_index, selected]))
        gross_absolute = float(np.mean(panel.asset_returns[decision_index, selected]))
        cost = max(0.0, cost_bps) / 10000.0
        returns.append(gross_alpha - cost)
        absolute_returns.append(gross_absolute - cost)
        double_cost_returns.append(gross_alpha - 2.0 * cost)
        selected_values = panel.excess_returns[decision_index, selected] - cost
        positive_selected += int(np.sum(selected_values > 0.0))
        selected_observations += len(selected)
    values = np.asarray(returns, dtype=np.float64)
    scale = safe_std(values)
    annualized_sharpe = (
        float(np.mean(values) / scale * math.sqrt(252.0 / holding_days))
        if scale > EPS else 0.0
    )
    return {
        "periods": len(values),
        "meanNetAlphaBps": float(np.mean(values) * 10000.0) if len(values) else 0.0,
        "meanNetAbsoluteReturnBps": (
            float(np.mean(absolute_returns) * 10000.0) if absolute_returns else 0.0
        ),
        "cumulativeNetAlphaPct": float((np.prod(1.0 + values) - 1.0) * 100.0) if len(values) else 0.0,
        "annualizedSharpeLike": annualized_sharpe,
        "maxDrawdownPct": max_drawdown(returns) * 100.0,
        "meanInformationCoefficient": float(np.mean(information_coefficients)) if information_coefficients else 0.0,
        "selectedHitRate": positive_selected / selected_observations if selected_observations else 0.0,
        "averageHoldings": float(np.mean(holding_counts)) if holding_counts else 0.0,
        "meanNetAlphaDoubleCostBps": (
            float(np.mean(double_cost_returns) * 10000.0) if double_cost_returns else 0.0
        ),
    }


def train_fold_models(
    panel: Panel,
    train_indexes: Sequence[int],
    validation_dates: int,
    device: str,
    seed: int,
    epochs: int,
    ridge_l2: float,
    holding_days: int,
) -> Tuple[Any, Any, Dict[str, Any]]:
    fit_indexes, validation_indexes = purged_fit_validation_indexes(
        train_indexes, validation_dates, holding_days
    )
    fit_x = flatten_dates(panel.features, fit_indexes)
    fit_y = panel.excess_returns[fit_indexes].reshape(-1)
    validation_x = flatten_dates(panel.features, validation_indexes)
    validation_y = panel.excess_returns[validation_indexes].reshape(-1)
    champion = fit_ridge(fit_x, fit_y, l2=ridge_l2)
    challenger = fit_torch_mlp(
        fit_x,
        fit_y,
        validation_x,
        validation_y,
        requested_device=device,
        seed=seed,
        epochs=epochs,
    )
    return champion, challenger, {
        "fitDates": len(fit_indexes),
        "validationDates": len(validation_indexes),
        "purgedBetweenFitAndValidation": holding_days,
        "challengerValidation": challenger[3],
    }


def walk_forward_gate(
    panel: Panel,
    holding_days: int,
    folds: int,
    test_dates: int,
    min_train_dates: int,
    validation_dates: int,
    top_k: int,
    correlation_cap: float,
    cost_bps: float,
    device: str,
    seed: int,
    epochs: int,
    ridge_l2: float,
    min_lift_bps: float,
    max_drawdown_pct: float = 10.0,
) -> Dict[str, Any]:
    first_test = len(panel.dates) - folds * test_dates
    if first_test < min_train_dates + holding_days:
        raise ValueError(
            f"insufficient decision dates: need at least "
            f"{min_train_dates + holding_days + folds * test_dates}, "
            f"found {len(panel.dates)}"
        )
    fold_rows: List[Dict[str, Any]] = []
    for fold in range(folds):
        test_start = first_test + fold * test_dates
        test_end = test_start + test_dates
        train_stop = test_start - holding_days
        train_indexes = np.arange(0, train_stop, dtype=np.int32)
        test_indexes = np.arange(test_start, test_end, dtype=np.int32)
        champion, challenger, training = train_fold_models(
            panel,
            train_indexes,
            validation_dates,
            device,
            seed + fold * 101,
            epochs,
            ridge_l2,
            holding_days,
        )
        champion_predictions = np.zeros_like(panel.excess_returns)
        challenger_predictions = np.zeros_like(panel.excess_returns)
        flat_test_x = flatten_dates(panel.features, test_indexes)
        champion_predictions[test_indexes] = predict_ridge(champion, flat_test_x).reshape(
            len(test_indexes), len(panel.symbols)
        )
        challenger_predictions[test_indexes] = predict_torch(
            challenger[0], challenger[1], challenger[2], flat_test_x
        ).reshape(len(test_indexes), len(panel.symbols))
        champion_metrics = evaluation_metrics(
            champion_predictions,
            panel,
            test_indexes,
            top_k,
            correlation_cap,
            cost_bps,
            holding_days,
        )
        challenger_metrics = evaluation_metrics(
            challenger_predictions,
            panel,
            test_indexes,
            top_k,
            correlation_cap,
            cost_bps,
            holding_days,
        )
        fold_rows.append({
            "fold": fold + 1,
            "trainStart": panel.dates[0],
            "trainEnd": panel.dates[train_stop - 1],
            "purgedBeforeTest": holding_days,
            "testStart": panel.dates[test_start],
            "testEnd": panel.dates[test_end - 1],
            "training": training,
            "champion": champion_metrics,
            "challenger": challenger_metrics,
            "challengerLiftBps": (
                challenger_metrics["meanNetAlphaBps"] - champion_metrics["meanNetAlphaBps"]
            ),
        })

    champion_mean = float(np.mean([row["champion"]["meanNetAlphaBps"] for row in fold_rows]))
    challenger_mean = float(np.mean([row["challenger"]["meanNetAlphaBps"] for row in fold_rows]))
    challenger_double_cost = float(np.mean([
        row["challenger"]["meanNetAlphaDoubleCostBps"] for row in fold_rows
    ]))
    challenger_ic = float(np.mean([
        row["challenger"]["meanInformationCoefficient"] for row in fold_rows
    ]))
    challenger_absolute = float(np.mean([
        row["challenger"]["meanNetAbsoluteReturnBps"] for row in fold_rows
    ]))
    champion_worst_drawdown = float(min(
        row["champion"]["maxDrawdownPct"] for row in fold_rows
    ))
    challenger_worst_drawdown = float(min(
        row["challenger"]["maxDrawdownPct"] for row in fold_rows
    ))
    positive_folds = sum(1 for row in fold_rows if row["challenger"]["meanNetAlphaBps"] > 0.0)
    lift_folds = sum(1 for row in fold_rows if row["challengerLiftBps"] >= min_lift_bps)
    gates = {
        "meanLift": challenger_mean - champion_mean >= min_lift_bps,
        "positiveFoldMajority": positive_folds >= math.ceil(0.75 * folds),
        "liftFoldMajority": lift_folds >= math.ceil(0.75 * folds),
        "positiveAtDoubleCost": challenger_double_cost > 0.0,
        "positiveInformationCoefficient": challenger_ic > 0.0,
        "positiveAbsoluteReturn": challenger_absolute > 0.0,
        "absoluteDrawdownLimit": challenger_worst_drawdown >= -abs(max_drawdown_pct),
        "drawdownNotMateriallyWorseThanChampion": (
            challenger_worst_drawdown >= champion_worst_drawdown - 2.0
        ),
    }
    promoted = all(gates.values())
    return {
        "deploymentStatus": "PROMOTED_FOR_SHADOW_ONLY" if promoted else "NO_DEPLOY",
        "promoted": promoted,
        "gates": gates,
        "aggregate": {
            "championMeanNetAlphaBps": champion_mean,
            "challengerMeanNetAlphaBps": challenger_mean,
            "challengerMeanLiftBps": challenger_mean - champion_mean,
            "challengerMeanDoubleCostAlphaBps": challenger_double_cost,
            "challengerMeanInformationCoefficient": challenger_ic,
            "challengerMeanAbsoluteReturnBps": challenger_absolute,
            "championWorstDrawdownPct": champion_worst_drawdown,
            "challengerWorstDrawdownPct": challenger_worst_drawdown,
            "challengerPositiveFolds": positive_folds,
            "challengerLiftFolds": lift_folds,
        },
        "folds": fold_rows,
    }


def fetch_universe(
    symbols: Sequence[str],
    limit: int,
    refresh: bool,
) -> Tuple[
    Dict[str, Sequence[Mapping[str, Any]]],
    Dict[str, str],
    Dict[str, Mapping[str, Any]],
]:
    bars: Dict[str, Sequence[Mapping[str, Any]]] = {}
    names: Dict[str, str] = {}
    quality: Dict[str, Mapping[str, Any]] = {}
    for symbol in symbols:
        bundle = fetch_public_bundle(symbol, "1d", limit)
        if bundle.quality.status == "DATA_BLOCKED" or not bundle.bars:
            raise RuntimeError(
                f"daily data blocked for {symbol}: {', '.join(bundle.quality.reasons)}"
            )
        bars[symbol] = [asdict(value) for value in bundle.bars]
        names[symbol] = symbol
        quality[symbol] = asdict(bundle.quality)
    return bars, names, quality


def parse_symbols(value: str) -> List[str]:
    result: List[str] = []
    for item in value.replace(";", ",").split(","):
        symbol = item.strip()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    symbols = parse_symbols(args.symbols)
    if args.benchmark not in symbols:
        symbols.append(args.benchmark)
    if len(symbols) < 5:
        raise ValueError("at least five symbols including the benchmark are required")
    bars, names, data_quality = fetch_universe(symbols, args.limit, args.refresh_data)
    panel = build_panel(bars, benchmark=args.benchmark, holding_days=args.holding_days)
    started = time.time()
    gate = walk_forward_gate(
        panel=panel,
        holding_days=args.holding_days,
        folds=args.folds,
        test_dates=args.test_dates,
        min_train_dates=args.min_train_dates,
        validation_dates=args.validation_dates,
        top_k=args.top_k,
        correlation_cap=args.correlation_cap,
        cost_bps=args.cost_bps,
        device=args.device,
        seed=args.seed,
        epochs=args.epochs,
        ridge_l2=args.ridge_l2,
        min_lift_bps=args.min_lift_bps,
        max_drawdown_pct=args.max_drawdown_pct,
    )
    return {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "researchOnly": True,
        "brokerConnected": False,
        "elapsedSeconds": round(time.time() - started, 3),
        "protocol": {
            "signalTimestamp": "completed decision-day close",
            "entry": "next trading session open",
            "minimumHoldingDays": args.holding_days,
            "tPlusOneCompatible": args.holding_days >= 2,
            "target": "asset return minus benchmark return",
            "validation": "expanding time folds with train-tail validation",
            "nonOverlappingEvaluation": True,
            "purgedFoldBoundaryDates": args.holding_days,
            "benchmarkExcludedFromSelection": True,
            "roundTripCostBps": args.cost_bps,
            "doubleCostGateBps": args.cost_bps * 2.0,
            "correlationCap": args.correlation_cap,
            "topK": args.top_k,
            "featureNames": list(FEATURE_NAMES),
            "priceAdjustment": "none",
        },
        "universe": {
            "symbols": [{"symbol": symbol, "name": names.get(symbol, symbol)} for symbol in panel.symbols],
            "benchmark": args.benchmark,
            "alignedDecisionDates": len(panel.dates),
            "start": panel.dates[0],
            "end": panel.dates[-1],
            "dataQuality": data_quality,
        },
        "models": {
            "champion": "standardized ridge regression",
            "challenger": "64x32 GELU MLP with Huber loss and validation early stopping",
            "requestedDevice": args.device,
        },
        "gate": gate,
        "notice": (
            "NO_DEPLOY means no signal from this run may be used for real orders. "
            "PROMOTED_FOR_SHADOW_ONLY permits shadow observation only, never automatic trading."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="comma-separated A-share/ETF symbols")
    parser.add_argument("--benchmark", default="512050")
    parser.add_argument("--limit", type=int, default=1300)
    parser.add_argument("--holding-days", type=int, default=3)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--test-dates", type=int, default=35)
    parser.add_argument("--min-train-dates", type=int, default=140)
    parser.add_argument("--validation-dates", type=int, default=25)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--correlation-cap", type=float, default=0.90)
    parser.add_argument("--cost-bps", type=float, default=12.0)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--ridge-l2", type=float, default=8.0)
    parser.add_argument("--min-lift-bps", type=float, default=2.0)
    parser.add_argument("--max-drawdown-pct", type=float, default=10.0)
    parser.add_argument("--refresh-data", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(json_safe(report), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(json_safe({
        "universe": report["universe"],
        "device": report["models"]["requestedDevice"],
        "deploymentStatus": report["gate"]["deploymentStatus"],
        "gates": report["gate"]["gates"],
        "aggregate": report["gate"]["aggregate"],
        "output": str(args.output),
    }), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
