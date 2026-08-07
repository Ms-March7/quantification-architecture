from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import ruptures as rpt
from sklearn.model_selection import TimeSeriesSplit


def prepare_backtest_frame(margin: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    frame = market.merge(margin, on="date", how="left").sort_values("date").reset_index(drop=True)
    if "exchange_count" in frame:
        incomplete = frame["exchange_count"].fillna(0) < 2
        frame.loc[incomplete, ["financing_balance_cny", "financing_buy_cny"]] = np.nan
    frame["margin_growth_20"] = frame["financing_balance_cny"].pct_change(20, fill_method=None)
    buy_mean = frame["financing_buy_cny"].rolling(20, min_periods=20).mean()
    frame["buy_intensity_20"] = frame["financing_buy_cny"] / buy_mean - 1.0
    frame["forward_return"] = frame["market_return"].shift(-2)
    frame["execution_date"] = frame["date"].shift(-2)
    frame["signal_available"] = frame[
        ["margin_growth_20", "buy_intensity_20", "forward_return", "execution_date"]
    ].notna().all(axis=1)
    return frame


def walk_forward_exposure(
    frame: pd.DataFrame,
    *,
    n_splits: int = 6,
    gap: int = 5,
    test_size: int = 252,
) -> pd.DataFrame:
    splitter = TimeSeriesSplit(n_splits=n_splits, gap=gap, test_size=test_size)
    outputs: list[pd.DataFrame] = []
    features = frame[["margin_growth_20", "buy_intensity_20"]]
    valid_index = np.flatnonzero(frame["signal_available"].to_numpy())
    for fold, (train_position, test_position) in enumerate(splitter.split(valid_index), start=1):
        train_index = valid_index[train_position]
        test_index = valid_index[test_position]
        train = features.iloc[train_index]
        test = features.iloc[test_index]
        mean = train.mean()
        std = train.std(ddof=0).replace(0, np.nan)
        train_score = ((train - mean) / std).fillna(0.0)
        train_score = train_score["margin_growth_20"] + 0.5 * train_score["buy_intensity_20"]
        lower, upper = train_score.quantile([0.30, 0.70])
        test_score = ((test - mean) / std).fillna(0.0)
        test_score = test_score["margin_growth_20"] + 0.5 * test_score["buy_intensity_20"]
        exposure = np.select([test_score <= lower, test_score >= upper], [0.0, 1.0], default=0.5)
        output = frame.iloc[test_index][["date", "forward_return", "execution_date"]].copy()
        output["signal_date"] = output["date"]
        output["fold"] = fold
        output["score"] = test_score.to_numpy()
        output["lower_threshold"] = float(lower)
        output["upper_threshold"] = float(upper)
        output["exposure"] = exposure
        output["signal_available"] = True
        outputs.append(output)
    predicted = pd.concat(outputs, ignore_index=True).sort_values("signal_date")
    first = int(frame.index[frame["date"] == predicted["date"].min()][0])
    last = int(frame.index[frame["date"] == predicted["date"].max()][0])
    calendar = frame.iloc[first : last + 1][["date", "forward_return", "execution_date"]].copy()
    result = calendar.merge(
        predicted.drop(columns=["forward_return", "execution_date"]), on="date", how="left"
    )
    result["signal_date"] = result["date"]
    result["signal_available"] = result["signal_available"].eq(True)
    result["exposure"] = result["exposure"].fillna(0.0)
    result = result.dropna(subset=["forward_return", "execution_date"]).reset_index(drop=True)
    if (result["execution_date"] <= result["signal_date"]).any():
        raise AssertionError("look-ahead guard failed: execution must be after signal date")
    return result


def _metrics(returns: pd.Series) -> dict[str, float]:
    clean = returns.fillna(0.0).astype(float)
    equity = (1.0 + clean).cumprod()
    years = max(len(clean) / 252.0, 1 / 252.0)
    cumulative = float(equity.iloc[-1] - 1.0)
    drawdown = equity / equity.cummax() - 1.0
    volatility = float(clean.std(ddof=0) * np.sqrt(252))
    return {
        "cumulative_return": cumulative,
        "cagr": float(equity.iloc[-1] ** (1 / years) - 1.0),
        "maximum_drawdown": float(drawdown.min()),
        "annual_volatility": volatility,
        "sharpe_zero_rate": float(clean.mean() / clean.std(ddof=0) * np.sqrt(252)) if clean.std(ddof=0) else 0.0,
        "hit_rate": float((clean > 0).mean()),
    }


def evaluate_backtest(predictions: pd.DataFrame, one_way_cost_bps: float = 10.0) -> tuple[pd.DataFrame, dict[str, Any]]:
    result = predictions.copy()
    turnover = result["exposure"].diff().abs().fillna(result["exposure"].abs())
    result["turnover"] = turnover
    result["gross_return"] = result["exposure"] * result["forward_return"]
    result["cost"] = turnover * one_way_cost_bps / 10_000.0
    result["net_return"] = result["gross_return"] - result["cost"]
    result["benchmark_return"] = result["forward_return"]
    metrics = {
        "strategy": _metrics(result["net_return"]),
        "benchmark": _metrics(result["benchmark_return"]),
        "observations": len(result),
        "folds": int(result["fold"].nunique()),
        "date_start": str(result["execution_date"].min()),
        "date_end": str(result["execution_date"].max()),
        "one_way_cost_bps": one_way_cost_bps,
        "average_exposure": float(result["exposure"].mean()),
        "annual_turnover": float(result["turnover"].mean() * 252),
        "signal_lag_trading_days": 2,
        "promotion_ceiling": "RESEARCH_ONLY",
    }
    return result, metrics


def block_bootstrap_excess(
    returns: pd.Series, *, block_size: int = 20, samples: int = 1000, seed: int = 20260719
) -> dict[str, float]:
    values = returns.dropna().to_numpy(dtype=float)
    if len(values) < block_size:
        raise ValueError("not enough observations for block bootstrap")
    rng = np.random.default_rng(seed)
    starts = np.arange(0, len(values) - block_size + 1)
    annualized: list[float] = []
    blocks_needed = int(np.ceil(len(values) / block_size))
    for _ in range(samples):
        sampled = np.concatenate([values[start : start + block_size] for start in rng.choice(starts, blocks_needed)])
        annualized.append(float(sampled[: len(values)].mean() * 252))
    lower, median, upper = np.quantile(annualized, [0.025, 0.5, 0.975])
    return {"annual_excess_p025": float(lower), "annual_excess_median": float(median), "annual_excess_p975": float(upper)}


def cost_stress(predictions: pd.DataFrame, costs_bps: tuple[float, ...] = (5.0, 10.0, 20.0, 50.0)) -> list[dict[str, float]]:
    rows = []
    for cost in costs_bps:
        _, metrics = evaluate_backtest(predictions, cost)
        rows.append({"one_way_cost_bps": cost, **metrics["strategy"]})
    return rows


def diagnostic_change_points(frame: pd.DataFrame, penalty: float = 12.0) -> list[str]:
    series = frame["margin_growth_20"].replace([np.inf, -np.inf], np.nan).dropna()
    values = ((series - series.mean()) / series.std(ddof=0)).to_numpy().reshape(-1, 1)
    endpoints = rpt.Pelt(model="rbf", min_size=20, jump=5).fit(values).predict(pen=penalty)
    dates = frame.loc[series.index, "date"].reset_index(drop=True)
    return [str(dates.iloc[index - 1]) for index in endpoints[:-1] if 0 < index <= len(dates)]
