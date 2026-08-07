"""Cross-sectional diagnostics, stability tests, and multiple testing control."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Sequence

import numpy as np

from .accelerator import cross_sectional_factor_metrics_gpu, permutation_extreme_count_gpu
from .schema import PanelData
from .validation import blocked_folds


EPS = 1e-12


@dataclass(frozen=True)
class FactorMetrics:
    n_dates: int
    n_observations: int
    coverage: float
    mean_ic: float
    ic_std: float
    icir: float
    positive_ic_ratio: float
    positive_fold_ratio: float
    worst_fold_ic: float
    top_excess_mean: float
    long_short_spread_mean: float
    turnover: float
    cost_adjusted_top_excess: float
    stressed_cost_adjusted_top_excess: float
    p_value: float
    q_value: float = 1.0
    evaluable_date_ratio: float = 0.0
    minimum_fold_evaluable_ratio: float = 0.0
    permutation_block_length: int = 0
    bottom_excess_mean: float = 0.0
    cost_adjusted_bottom_excess: float = 0.0
    stressed_cost_adjusted_bottom_excess: float = 0.0
    top_tail_turnover: float = 0.0
    bottom_tail_turnover: float = 0.0
    decision_observations: int = 0
    realized_outcome_observations: int = 0
    missing_outcome_observations: int = 0
    outcome_coverage: float = 0.0
    tail_selection_dates: int = 0
    top_return_dates: int = 0
    bottom_return_dates: int = 0
    spread_return_dates: int = 0
    top_tail_missing_outcome_weight_mean: float = 0.0
    bottom_tail_missing_outcome_weight_mean: float = 0.0
    outcome_return_policy: str = (
        "FIXED_FACTOR_VISIBLE_MEMBERSHIP__OBSERVED_OUTCOMES_RENORMALIZED__NO_REPLACEMENT"
    )

    def to_dict(self) -> dict[str, int | float | str]:
        return asdict(self)


def _rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values), dtype=np.float64)
    sorted_values = values[order]
    if len(sorted_values) < 2 or sorted_values[0] == sorted_values[-1]:
        return np.full(len(values), np.nan, dtype=np.float64)
    starts = np.flatnonzero(np.r_[True, sorted_values[1:] != sorted_values[:-1]])
    ends = np.r_[starts[1:], len(values)]
    averages = (starts + ends - 1) / 2.0
    ranks[order] = np.repeat(averages, ends - starts)
    return ranks


def _rank_correlation(left: np.ndarray, right: np.ndarray) -> float:
    if len(left) < 3:
        return np.nan
    x = _rank(left)
    y = _rank(right)
    if not np.isfinite(x).all() or not np.isfinite(y).all() or np.std(x) <= EPS or np.std(y) <= EPS:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def _tail_weights(values: np.ndarray, size: int, *, top: bool) -> np.ndarray:
    """Tie-invariant quantile weights with exactly ``size`` units of mass."""

    if size < 1 or size > len(values):
        raise ValueError("invalid quantile size")
    weights = np.zeros(len(values), dtype=np.float64)
    levels = np.unique(values)
    if top:
        levels = levels[::-1]
    remaining = float(size)
    for level in levels:
        members = np.flatnonzero(values == level)
        allocation = min(1.0, remaining / len(members))
        weights[members] = allocation
        remaining -= allocation * len(members)
        if remaining <= 1e-12:
            break
    weights /= weights.sum()
    return weights


def _dependence_block_length(full_ic: np.ndarray, minimum: int) -> int:
    """Choose a conservative calendar block from positive serial dependence."""

    finite = np.isfinite(full_ic)
    if int(finite.sum()) < 8:
        return max(1, minimum)
    centered = full_ic.copy()
    centered[finite] -= float(np.mean(centered[finite]))
    variance = float(np.sum(centered[finite] ** 2))
    if variance <= EPS:
        return max(1, minimum)
    threshold = 2.0 / np.sqrt(int(finite.sum()))
    maximum_lag = min(120, max(1, len(full_ic) // 3))
    dependent_lag = 0
    for lag in range(1, maximum_lag + 1):
        pairs = finite[:-lag] & finite[lag:]
        if int(pairs.sum()) < 6:
            break
        correlation = float(
            np.sum(centered[:-lag][pairs] * centered[lag:][pairs]) / variance
        )
        if correlation > threshold:
            dependent_lag = lag
        elif dependent_lag > 0:
            break
    return max(1, int(minimum), dependent_lag + 1)


def minimum_trading_age_mask(panel: PanelData, minimum_sessions: int = 120) -> np.ndarray:
    """Causal minimum-listing-age proxy from observed eligible trading sessions.

    The formal narrow panel does not expose a current-master listing date as a
    signal.  Counting only historical eligible rows avoids current-universe
    backfill and is conservative for securities already listed before the
    snapshot begins.
    """

    if isinstance(minimum_sessions, bool) or not isinstance(minimum_sessions, int):
        raise ValueError("minimum_sessions must be an integer")
    if minimum_sessions < 1:
        raise ValueError("minimum_sessions must be positive")
    counts = np.cumsum(np.asarray(panel.eligible, dtype=np.int32), axis=0)
    return np.asarray(panel.eligible, dtype=bool) & (counts >= minimum_sessions)


def winsorized_cross_sectional_rank(
    values: np.ndarray,
    eligible: np.ndarray | None = None,
    *,
    lower_quantile: float = 0.01,
    upper_quantile: float = 0.99,
    minimum_assets: int = 2,
) -> np.ndarray:
    """Per-date 1/99 winsorization followed by tie-aware percentile ranking.

    Missing values stay missing and no value is carried across dates.  The
    optional eligibility mask is contemporaneous and is used only to define
    the cross section supplied by the caller.
    """

    source = np.asarray(values)
    if source.ndim != 2:
        raise ValueError("values must be a 2-D date-by-asset matrix")
    if not 0.0 <= lower_quantile < upper_quantile <= 1.0:
        raise ValueError("winsorization quantiles must satisfy 0 <= lower < upper <= 1")
    if isinstance(minimum_assets, bool) or not isinstance(minimum_assets, int) or minimum_assets < 2:
        raise ValueError("minimum_assets must be an integer of at least two")
    mask = None
    if eligible is not None:
        mask = np.asarray(eligible, dtype=bool)
        if mask.shape != source.shape:
            raise ValueError("eligible mask must match values")
    result = np.full(source.shape, np.nan, dtype=np.float32)
    for date in range(len(source)):
        valid = np.isfinite(source[date])
        if mask is not None:
            valid &= mask[date]
        count = int(valid.sum())
        if count < minimum_assets:
            continue
        clean = np.asarray(source[date, valid], dtype=np.float64)
        lower, upper = np.quantile(clean, (lower_quantile, upper_quantile))
        ranks = _rank(np.clip(clean, lower, upper))
        if np.isfinite(ranks).all():
            result[date, valid] = (ranks / (count - 1.0) - 0.5).astype(np.float32)
    return result


def forward_total_returns(
    panel: PanelData,
    horizon: int,
    *,
    decision_start: int = 0,
    decision_end: int | None = None,
    decision_eligible: np.ndarray | None = None,
) -> np.ndarray:
    """Action-aware open-to-open target for the formal R0 narrow panel.

    The source field is the one-period close-to-close total return with cash
    reinvestment.  A cumulative log total-return index is combined with the
    same-day raw ``open / close`` ratio, so an action on the entry day is not
    credited to a position bought after the action while an action carried
    overnight into the exit day is credited.  Future execution masks are not
    used.  A missing scheduled exit open may use that same session's observed
    close, but a missing holding-period return or exit-session close remains
    missing.  No stale close is carried forward and no terminal event is
    invented when the source lacks a verified terminal mask.
    """

    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        raise ValueError("horizon must be a positive integer")
    if "realized_total_return" not in panel.fields:
        raise ValueError("action-aware labels require realized_total_return")
    returns = np.asarray(panel.fields["realized_total_return"], dtype=np.float64)
    opens = np.asarray(panel.fields["open"], dtype=np.float64)
    closes = np.asarray(panel.fields["close"], dtype=np.float64)
    if returns.shape != panel.shape:
        raise ValueError("realized_total_return shape mismatch")
    finite_return = np.isfinite(returns)
    if np.any(returns[finite_return] <= -1.0):
        raise ValueError("realized_total_return must be greater than -100%")

    # ``np.asarray`` can return the PanelData mask itself.  The optional
    # decision filter must remain a pure local intersection and never mutate
    # the research universe owned by the caller.
    base_eligible = np.asarray(panel.eligible, dtype=bool).copy()
    if decision_eligible is not None:
        supplied = np.asarray(decision_eligible, dtype=bool)
        if supplied.shape != panel.shape:
            raise ValueError("decision_eligible must match panel shape")
        base_eligible &= supplied

    maximum_decision = len(panel.dates) - horizon - 2
    last_decision = (
        maximum_decision
        if decision_end is None
        else min(int(decision_end), maximum_decision)
    )
    first_decision = max(0, int(decision_start))
    result = np.full(panel.shape, np.nan, dtype=np.float32)
    if first_decision > last_decision:
        return result

    last_source_date = last_decision + horizon + 1
    source_returns = returns[: last_source_date + 1]
    log_return = np.zeros_like(source_returns, dtype=np.float64)
    finite_source_return = finite_return[: last_source_date + 1]
    log_return[finite_source_return] = np.log1p(
        source_returns[finite_source_return]
    )
    log_prefix = np.cumsum(log_return, axis=0, dtype=np.float64)
    missing_prefix = np.cumsum(
        ~finite_return[: last_source_date + 1], axis=0, dtype=np.int32
    )
    valid_open = (
        np.isfinite(opens[: last_source_date + 1])
        & (opens[: last_source_date + 1] > 0.0)
    )
    valid_close = (
        np.isfinite(closes[: last_source_date + 1])
        & (closes[: last_source_date + 1] > 0.0)
    )

    terminal_prefix = np.cumsum(
        (panel.delisted | panel.invalidated)[: last_source_date + 1],
        axis=0,
        dtype=np.int32,
    )
    terminal_status = str(
        panel.metadata.get("terminal_event_mask_status", "CALLER_SUPPLIED_OR_UNSPECIFIED")
    )
    if terminal_status.startswith("UNAVAILABLE") and bool(np.any(terminal_prefix > 0)):
        raise ValueError("terminal masks cannot contain events when metadata marks them unavailable")
    candidate_count = 0
    ordinary_count = 0
    terminal_writeoff_count = 0
    missing_outcome_count = 0
    same_session_close_mark_count = 0
    for decision in range(first_decision, last_decision + 1):
        entry_date = decision + 1
        exit_date = entry_date + horizon
        entry_open = opens[entry_date]
        entry_close = closes[entry_date]
        holding_log_return = log_prefix[exit_date] - log_prefix[entry_date]
        holding_missing = missing_prefix[exit_date] - missing_prefix[entry_date]
        terminal_count = terminal_prefix[exit_date] - terminal_prefix[entry_date - 1]
        valid_entry = (
            base_eligible[decision]
            & valid_open[entry_date]
            & valid_close[entry_date]
        )
        candidate_count += int(valid_entry.sum())
        terminal = valid_entry & (terminal_count > 0)
        result[decision, terminal] = -1.0
        terminal_writeoff_count += int(terminal.sum())

        ordinary = (
            valid_entry
            & (terminal_count == 0)
            & (holding_missing == 0)
            & valid_close[exit_date]
        )
        exit_open_observed = ordinary & valid_open[exit_date]
        exit_close_mark = ordinary & ~valid_open[exit_date]
        total_log_return = np.full(panel.shape[1], np.nan, dtype=np.float64)
        total_log_return[valid_entry] = (
            np.log(entry_close[valid_entry])
            - np.log(entry_open[valid_entry])
            + holding_log_return[valid_entry]
        )
        total_log_return[exit_open_observed] += (
            np.log(opens[exit_date, exit_open_observed])
            - np.log(closes[exit_date, exit_open_observed])
        )
        realized = ordinary & np.isfinite(total_log_return)
        result[decision, realized] = np.expm1(total_log_return[realized]).astype(
            np.float32
        )
        ordinary_count += int(realized.sum())
        same_session_close_mark_count += int((exit_close_mark & realized).sum())
        missing_outcome_count += int((valid_entry & ~terminal & ~realized).sum())

    panel.metadata.setdefault("forward_total_return_diagnostics", {})[str(horizon)] = {
        "decision_start": first_decision,
        "decision_end": last_decision,
        "candidate_count": candidate_count,
        "realized_outcome_count": ordinary_count + terminal_writeoff_count,
        "missing_outcome_count": missing_outcome_count,
        "terminal_writeoff_count": terminal_writeoff_count,
        "same_session_exit_close_mark_count": same_session_close_mark_count,
        "terminal_event_mask_status": terminal_status,
        "missing_outcome_policy": "NAN__NO_STALE_CLOSE_CARRY_FORWARD",
        "terminal_event_policy": (
            "WRITE_OFF_ONLY_EXPLICIT_MASK__NEVER_INFER_FROM_MISSING_OBSERVATIONS"
        ),
    }
    return result


def forward_returns(
    panel: PanelData,
    horizon: int,
    *,
    decision_start: int = 0,
    decision_end: int | None = None,
) -> np.ndarray:
    """Executable-timing target from open(t+1) to open(t+horizon+1).

    Future buy/sell masks are deliberately ignored here.  Conditioning labels
    on future tradability would delete difficult outcomes and create selection
    bias.  A terminal listing event inside the holding window is conservatively
    assigned -100%.  When the fixed exit open is absent because trading is
    temporarily suspended, the position is retained in the sample and marked
    at the latest close available by that date.  The event backtest separately
    models the blocked sale and continued holding period.
    """

    if horizon < 2:
        raise ValueError("horizon must be at least 2 for an A-share T+1-compatible target")
    maximum_decision = len(panel.dates) - horizon - 2
    last_decision = maximum_decision if decision_end is None else min(int(decision_end), maximum_decision)
    first_decision = max(0, int(decision_start))
    if first_decision > last_decision:
        return np.full(panel.shape, np.nan, dtype=np.float32)
    last_source_date = last_decision + horizon + 1
    opens = panel.fields["open"]
    closes = panel.fields["close"]
    result = np.full(panel.shape, np.nan, dtype=np.float32)
    latest_close = np.asarray(closes[: last_source_date + 1], dtype=np.float32).copy()
    for date in range(1, len(latest_close)):
        missing = ~np.isfinite(latest_close[date])
        latest_close[date, missing] = latest_close[date - 1, missing]
    terminal_prefix = np.cumsum(
        (panel.delisted | panel.invalidated)[: last_source_date + 1], axis=0, dtype=np.int32
    )
    for decision in range(first_decision, last_decision + 1):
        entry_date = decision + 1
        exit_date = entry_date + horizon
        entry = opens[entry_date]
        exit_open = opens[exit_date]
        exit_price = np.where(
            np.isfinite(exit_open) & (exit_open > 0.0),
            exit_open,
            latest_close[exit_date],
        )
        terminal_count = terminal_prefix[exit_date] - terminal_prefix[entry_date - 1]
        valid = (
            panel.eligible[decision]
            & np.isfinite(entry)
            & (entry > 0.0)
        )
        ordinary = valid & np.isfinite(exit_price) & (exit_price > 0.0) & (terminal_count == 0)
        result[decision, ordinary] = exit_price[ordinary] / entry[ordinary] - 1.0
        result[decision, valid & (terminal_count > 0)] = -1.0
    return result


def evaluate_factor(
    values: np.ndarray,
    targets: np.ndarray,
    indices: Sequence[int],
    *,
    eligible: np.ndarray | None = None,
    minimum_assets: int = 8,
    quantile_fraction: float = 0.20,
    tail_count: int | None = None,
    round_trip_cost_bps: float = 15.0,
    fold_count: int = 3,
    permutations: int = 0,
    permutation_block_length: int = 5,
    seed: int = 20260715,
    accelerator: str = "cpu",
) -> FactorMetrics:
    """Evaluate IC and fixed, decision-visible factor tails.

    Tail membership and turnover use finite factor values only.  Future target
    availability never adds, removes, or replaces a member.  Returns are then
    measured on the observed outcomes within that frozen tail and the original
    weights are renormalized only among those members; missing target weight is
    reported explicitly below.
    """

    values = np.asarray(values)
    targets = np.asarray(targets)
    if values.shape != targets.shape:
        raise ValueError("factor values and targets must have identical shapes")
    eligibility = None
    if eligible is not None:
        eligibility = np.asarray(eligible)
        if eligibility.shape != values.shape or eligibility.dtype.kind != "b":
            raise ValueError("eligible must be a boolean mask matching factor values")
    if accelerator not in {"cpu", "gpu"}:
        raise ValueError("accelerator must be 'cpu' or 'gpu'")
    if tail_count is not None and (
        isinstance(tail_count, bool) or not isinstance(tail_count, int) or tail_count < 1
    ):
        raise ValueError("tail_count must be a positive integer or None")
    requested = np.asarray(indices, dtype=np.int64)
    daily_ic: list[float] = []
    daily_top_excess: list[float] = []
    daily_bottom_excess: list[float] = []
    daily_spread: list[float] = []
    daily_dates: list[int] = []
    previous_top_weights = np.zeros(values.shape[1], dtype=np.float64)
    previous_bottom_weights = np.zeros(values.shape[1], dtype=np.float64)
    top_turnover_sum = 0.0
    bottom_turnover_sum = 0.0
    tail_selection_dates = 0
    top_return_dates = 0
    bottom_return_dates = 0
    spread_return_dates = 0
    top_missing_weight_sum = 0.0
    bottom_missing_weight_sum = 0.0
    observations = 0
    decision_observations = 0
    missing_outcome_observations = 0
    requested_target_valid = np.isfinite(targets[requested])
    if eligibility is not None:
        requested_target_valid &= eligibility[requested]
    possible = int(requested_target_valid.sum())

    def target_count(date_index: int) -> int:
        finite = np.isfinite(targets[date_index])
        if eligibility is not None:
            finite &= eligibility[date_index]
        return int(finite.sum())

    if accelerator == "gpu":
        batch = cross_sectional_factor_metrics_gpu(
            values,
            targets,
            requested,
            minimum_assets=minimum_assets,
            quantile_fraction=quantile_fraction,
            tail_count=tail_count,
            eligible=eligibility,
        )
        positions = np.asarray(batch["daily_positions"], dtype=np.int64)
        daily_ic = np.asarray(batch["daily_ic"], dtype=np.float64).tolist()
        daily_top_excess = np.asarray(
            batch["daily_top_excess"], dtype=np.float64
        ).tolist()
        daily_bottom_excess = np.asarray(
            batch["daily_bottom_excess"], dtype=np.float64
        ).tolist()
        daily_spread = np.asarray(batch["daily_spread"], dtype=np.float64).tolist()
        daily_dates = requested[positions].astype(np.int64, copy=False).tolist()
        observations = int(batch["observations"])
        decision_observations = int(batch["decision_observations"])
        missing_outcome_observations = int(batch["missing_outcome_observations"])
        possible = int(batch["possible"])
        possible_dates = int(batch["possible_dates"])
        tail_selection_dates = int(batch["tail_selection_dates"])
        top_return_dates = int(batch["top_return_dates"])
        bottom_return_dates = int(batch["bottom_return_dates"])
        spread_return_dates = int(batch["spread_return_dates"])
        top_missing_weight_sum = (
            float(batch["top_tail_missing_outcome_weight_mean"])
            * tail_selection_dates
        )
        bottom_missing_weight_sum = (
            float(batch["bottom_tail_missing_outcome_weight_mean"])
            * tail_selection_dates
        )
        top_turnover = float(batch["top_tail_turnover"])
        bottom_turnover = float(batch["bottom_tail_turnover"])
    else:
        for date in requested:
            x = values[date]
            y = targets[date]
            decision_valid = np.isfinite(x)
            if eligibility is not None:
                decision_valid &= eligibility[date]
            paired_valid = decision_valid & np.isfinite(y)
            decision_count = int(decision_valid.sum())
            paired_count = int(paired_valid.sum())
            decision_observations += decision_count
            observations += paired_count
            missing_outcome_observations += decision_count - paired_count

            if paired_count >= minimum_assets:
                paired_x = x[paired_valid]
                paired_y = y[paired_valid]
                ic = _rank_correlation(paired_x, paired_y)
                if np.isfinite(ic):
                    daily_ic.append(ic)
                    daily_dates.append(int(date))

            if decision_count < minimum_assets:
                continue
            decision_indexes = np.flatnonzero(decision_valid)
            clean_x = x[decision_valid]
            if np.min(clean_x) == np.max(clean_x):
                continue
            quantile_size = (
                min(decision_count, tail_count)
                if tail_count is not None
                else max(1, int(np.floor(decision_count * quantile_fraction)))
            )
            top_weights = _tail_weights(clean_x, quantile_size, top=True)
            bottom_weights = _tail_weights(clean_x, quantile_size, top=False)
            current_top_weights = np.zeros(values.shape[1], dtype=np.float64)
            current_bottom_weights = np.zeros(values.shape[1], dtype=np.float64)
            current_top_weights[decision_indexes] = top_weights
            current_bottom_weights[decision_indexes] = bottom_weights
            if tail_selection_dates == 0:
                top_turnover_sum = 1.0
                bottom_turnover_sum = 1.0
            else:
                top_turnover_sum += 0.5 * float(
                    np.sum(np.abs(current_top_weights - previous_top_weights))
                )
                bottom_turnover_sum += 0.5 * float(
                    np.sum(np.abs(current_bottom_weights - previous_bottom_weights))
                )
            previous_top_weights = current_top_weights
            previous_bottom_weights = current_bottom_weights
            tail_selection_dates += 1

            outcome_observed = np.isfinite(y[decision_indexes])
            top_observed_weight = float(np.sum(top_weights[outcome_observed]))
            bottom_observed_weight = float(np.sum(bottom_weights[outcome_observed]))
            top_missing_weight_sum += 1.0 - top_observed_weight
            bottom_missing_weight_sum += 1.0 - bottom_observed_weight
            if not bool(np.any(outcome_observed)):
                continue
            observed_y = np.asarray(y[decision_indexes][outcome_observed], dtype=np.float64)
            benchmark_return = float(np.mean(observed_y))
            top_return: float | None = None
            bottom_return: float | None = None
            if top_observed_weight > EPS:
                top_return = float(
                    np.sum(top_weights[outcome_observed] * observed_y)
                    / top_observed_weight
                )
                daily_top_excess.append(top_return - benchmark_return)
                top_return_dates += 1
            if bottom_observed_weight > EPS:
                bottom_return = float(
                    np.sum(bottom_weights[outcome_observed] * observed_y)
                    / bottom_observed_weight
                )
                daily_bottom_excess.append(bottom_return - benchmark_return)
                bottom_return_dates += 1
            if top_return is not None and bottom_return is not None:
                daily_spread.append(top_return - bottom_return)
                spread_return_dates += 1
        possible_dates = int(
            sum(target_count(int(date)) >= minimum_assets for date in requested)
        )
        top_turnover = top_turnover_sum / max(1, tail_selection_dates)
        bottom_turnover = bottom_turnover_sum / max(1, tail_selection_dates)

    ic_array = np.asarray(daily_ic, dtype=np.float64)
    ic_std = float(np.std(ic_array, ddof=1)) if len(ic_array) > 1 else 0.0
    cost = round_trip_cost_bps / 10_000.0
    top_excess = float(np.mean(daily_top_excess)) if daily_top_excess else 0.0
    bottom_excess = (
        float(np.mean(daily_bottom_excess)) if daily_bottom_excess else 0.0
    )
    spread_mean = float(np.mean(daily_spread)) if daily_spread else 0.0

    fold_means: list[float] = []
    fold_evaluable_ratios: list[float] = []
    date_to_ic = dict(zip(daily_dates, daily_ic))
    for fold in blocked_folds(requested, fold_count):
        sample = [date_to_ic[int(date)] for date in fold if int(date) in date_to_ic]
        fold_possible = sum(
            target_count(int(date)) >= minimum_assets for date in fold
        )
        fold_evaluable_ratios.append(len(sample) / max(1, fold_possible))
        fold_means.append(float(np.mean(sample)) if sample else 0.0)

    p_value = 1.0
    full_ic = np.full(len(requested), np.nan, dtype=np.float64)
    requested_positions = {int(date): index for index, date in enumerate(requested)}
    for date, value in zip(daily_dates, daily_ic):
        full_ic[requested_positions[date]] = value
    chosen_block_length = _dependence_block_length(full_ic, permutation_block_length)
    if permutations > 0 and len(ic_array) >= 4:
        observed_positions = np.flatnonzero(np.isfinite(full_ic))
        if accelerator == "gpu":
            extreme = permutation_extreme_count_gpu(
                full_ic,
                observed_positions,
                permutations=permutations,
                block_length=chosen_block_length,
                seed=seed,
            )
        else:
            rng = np.random.default_rng(seed)
            block_count = int(np.ceil(len(requested) / chosen_block_length))
            block_signs = rng.choice((-1.0, 1.0), size=(permutations, block_count))
            signs = np.repeat(block_signs, chosen_block_length, axis=1)[:, :len(requested)]
            null_means = np.abs(np.mean(
                signs[:, observed_positions] * full_ic[observed_positions][None, :], axis=1
            ))
            tolerance = 64.0 * np.finfo(np.float64).eps * max(
                1.0, float(np.max(np.abs(full_ic[observed_positions])))
            )
            extreme = int(
                np.sum(null_means >= abs(np.mean(ic_array)) - tolerance)
            )
        p_value = float((1 + extreme) / (permutations + 1))
    return FactorMetrics(
        n_dates=len(ic_array),
        n_observations=observations,
        coverage=observations / max(1, possible),
        mean_ic=float(np.mean(ic_array)) if len(ic_array) else 0.0,
        ic_std=ic_std,
        icir=float(np.mean(ic_array) / ic_std) if ic_std > EPS else 0.0,
        positive_ic_ratio=float(np.mean(ic_array > 0.0)) if len(ic_array) else 0.0,
        positive_fold_ratio=float(np.mean(np.asarray(fold_means) > 0.0)) if fold_means else 0.0,
        worst_fold_ic=float(min(fold_means)) if fold_means else 0.0,
        top_excess_mean=top_excess,
        long_short_spread_mean=spread_mean,
        turnover=top_turnover,
        cost_adjusted_top_excess=top_excess - cost * top_turnover,
        stressed_cost_adjusted_top_excess=top_excess - 2.0 * cost * top_turnover,
        p_value=p_value,
        evaluable_date_ratio=len(ic_array) / max(1, possible_dates),
        minimum_fold_evaluable_ratio=min(fold_evaluable_ratios) if fold_evaluable_ratios else 0.0,
        permutation_block_length=chosen_block_length,
        bottom_excess_mean=bottom_excess,
        cost_adjusted_bottom_excess=bottom_excess - cost * bottom_turnover,
        stressed_cost_adjusted_bottom_excess=(
            bottom_excess - 2.0 * cost * bottom_turnover
        ),
        top_tail_turnover=top_turnover,
        bottom_tail_turnover=bottom_turnover,
        decision_observations=decision_observations,
        realized_outcome_observations=observations,
        missing_outcome_observations=missing_outcome_observations,
        outcome_coverage=observations / max(1, decision_observations),
        tail_selection_dates=tail_selection_dates,
        top_return_dates=top_return_dates,
        bottom_return_dates=bottom_return_dates,
        spread_return_dates=spread_return_dates,
        top_tail_missing_outcome_weight_mean=(
            top_missing_weight_sum / max(1, tail_selection_dates)
        ),
        bottom_tail_missing_outcome_weight_mean=(
            bottom_missing_weight_sum / max(1, tail_selection_dates)
        ),
    )


def benjamini_hochberg(metrics: Sequence[FactorMetrics]) -> list[FactorMetrics]:
    """Control false discovery rate across the full candidate family."""

    if not metrics:
        return []
    p_values = np.asarray([item.p_value for item in metrics], dtype=np.float64)
    order = np.argsort(p_values)
    adjusted = np.ones(len(metrics), dtype=np.float64)
    running = 1.0
    for rank_index in range(len(order) - 1, -1, -1):
        original = order[rank_index]
        rank = rank_index + 1
        running = min(running, p_values[original] * len(metrics) / rank)
        adjusted[original] = min(1.0, running)
    return [replace(item, q_value=float(adjusted[index])) for index, item in enumerate(metrics)]


def benjamini_yekutieli(metrics: Sequence[FactorMetrics]) -> list[FactorMetrics]:
    """FDR control valid under arbitrary candidate dependence."""

    adjusted = benjamini_hochberg(metrics)
    harmonic = float(np.sum(1.0 / np.arange(1, len(metrics) + 1))) if metrics else 1.0
    return [replace(item, q_value=min(1.0, item.q_value * harmonic)) for item in adjusted]


def discovery_score(metrics: FactorMetrics) -> float:
    stability = max(0.0, metrics.positive_fold_ratio) * max(0.0, metrics.mean_ic)
    return (
        3.0 * metrics.cost_adjusted_top_excess
        + 0.50 * stability
        + 0.15 * max(0.0, metrics.icir)
        - 0.02 * metrics.turnover
    )
