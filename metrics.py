"""
Risk metrics for strategy evaluation.

Implements the standard quant finance performance attribution toolkit
used at institutional desks: Sharpe, Sortino, Calmar, max drawdown,
profit factor, and the Citadel-style decomposed Sharpe (gross → net).

All functions accept a pandas Series or numpy array of daily P&L values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PerformanceReport:
    """
    Complete performance report for a strategy backtest.

    Attributes:
        annualized_return: CAGR as decimal (e.g., 0.214 for 21.4%).
        annualized_volatility: Annualized daily P&L standard deviation.
        sharpe_ratio: (Return − Risk-free) / Volatility. Annualized.
        sortino_ratio: Sharpe using only downside deviation.
        calmar_ratio: Annual return / Max drawdown magnitude.
        max_drawdown: Peak-to-trough drawdown as decimal (negative).
        max_drawdown_duration_days: Longest drawdown recovery period.
        win_rate: Fraction of trades with positive P&L.
        profit_factor: Gross profit / Gross loss.
        avg_win: Mean P&L of winning trades.
        avg_loss: Mean P&L of losing trades (negative).
        payoff_ratio: abs(avg_win / avg_loss).
        total_trades: Number of completed trades.
        sharpe_gross: Sharpe before transaction costs.
        sharpe_after_hedge: Sharpe after delta hedging costs.
        sharpe_net: Sharpe after all costs (reported figure).
    """
    annualized_return: float
    annualized_volatility: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown: float
    max_drawdown_duration_days: int
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    payoff_ratio: float
    total_trades: int
    sharpe_gross: float
    sharpe_after_hedge: float
    sharpe_net: float

    def summary(self) -> str:
        """Return a formatted summary table."""
        rows = [
            ("Annualized return",     f"{self.annualized_return:.2%}"),
            ("Annualized volatility", f"{self.annualized_volatility:.2%}"),
            ("Sharpe ratio (net)",    f"{self.sharpe_net:.2f}"),
            ("Sortino ratio",         f"{self.sortino_ratio:.2f}"),
            ("Calmar ratio",          f"{self.calmar_ratio:.2f}"),
            ("Max drawdown",          f"{self.max_drawdown:.2%}"),
            ("DD duration (days)",    str(self.max_drawdown_duration_days)),
            ("Win rate",              f"{self.win_rate:.2%}"),
            ("Profit factor",         f"{self.profit_factor:.2f}"),
            ("Payoff ratio",          f"{self.payoff_ratio:.2f}"),
            ("Total trades",          str(self.total_trades)),
            ("Sharpe gross",          f"{self.sharpe_gross:.2f}"),
            ("Sharpe after hedges",   f"{self.sharpe_after_hedge:.2f}"),
        ]
        width = 32
        lines = ["┌" + "─" * width + "┬" + "─" * 12 + "┐"]
        for label, val in rows:
            lines.append(f"│ {label:<{width-2}} │ {val:>10} │")
        lines.append("└" + "─" * width + "┴" + "─" * 12 + "┘")
        return "\n".join(lines)


def sharpe_ratio(
    pnl: pd.Series | np.ndarray,
    risk_free_annual: float = 0.0525,
    periods_per_year: int = 252,
) -> float:
    """
    Compute annualized Sharpe ratio.

    Args:
        pnl: Daily P&L series (dollar amounts or returns).
        risk_free_annual: Annual risk-free rate as decimal.
        periods_per_year: Trading periods per year (252 for daily).

    Returns:
        Annualized Sharpe ratio. Returns 0.0 if standard deviation is zero.

    Example:
        >>> daily_pnl = pd.Series([100, -50, 200, -30, 150])
        >>> sharpe_ratio(daily_pnl)
        1.87
    """
    arr = np.asarray(pnl, dtype=float)
    rf_daily = risk_free_annual / periods_per_year
    excess = arr - rf_daily
    std = np.std(excess, ddof=1)
    if std == 0:
        return 0.0
    return float(np.mean(excess) / std * math.sqrt(periods_per_year))


def sortino_ratio(
    pnl: pd.Series | np.ndarray,
    risk_free_annual: float = 0.0525,
    periods_per_year: int = 252,
) -> float:
    """
    Compute annualized Sortino ratio using downside deviation.

    Penalizes only negative returns, making it more appropriate than
    Sharpe for asymmetric return distributions (which options strategies
    tend to produce).

    Args:
        pnl: Daily P&L series.
        risk_free_annual: Annual risk-free rate.
        periods_per_year: Trading periods per year.

    Returns:
        Annualized Sortino ratio.
    """
    arr = np.asarray(pnl, dtype=float)
    rf_daily = risk_free_annual / periods_per_year
    excess = arr - rf_daily
    downside = excess[excess < 0]
    if len(downside) == 0:
        return float("inf")
    downside_std = math.sqrt(np.mean(downside**2))
    if downside_std == 0:
        return 0.0
    return float(np.mean(excess) / downside_std * math.sqrt(periods_per_year))


def max_drawdown(
    cumulative_pnl: pd.Series | np.ndarray,
) -> tuple[float, int]:
    """
    Compute maximum peak-to-trough drawdown and its duration.

    Args:
        cumulative_pnl: Cumulative P&L series (not daily returns).

    Returns:
        Tuple of (max_drawdown as decimal, duration in periods).

    Example:
        >>> cum_pnl = pd.Series([0, 100, 200, 150, 50, 300])
        >>> max_drawdown(cum_pnl)
        (-0.75, 2)  # 75% drawdown lasting 2 periods
    """
    arr = np.asarray(cumulative_pnl, dtype=float)
    if len(arr) == 0:
        return 0.0, 0

    peak = np.maximum.accumulate(arr)
    drawdown = np.where(peak != 0, (arr - peak) / np.abs(peak), 0.0)
    max_dd = float(np.min(drawdown))

    # Duration: longest period below previous peak
    in_drawdown = arr < peak
    max_dur = 0
    cur_dur = 0
    for d in in_drawdown:
        if d:
            cur_dur += 1
            max_dur = max(max_dur, cur_dur)
        else:
            cur_dur = 0

    return max_dd, max_dur


def calmar_ratio(
    pnl: pd.Series | np.ndarray,
    periods_per_year: int = 252,
) -> float:
    """
    Compute Calmar ratio: annualized return / max drawdown.

    Calmar > 1.0 is acceptable; > 2.0 is institutional-grade.

    Args:
        pnl: Daily P&L series.
        periods_per_year: Trading periods per year.

    Returns:
        Calmar ratio. Returns 0.0 if max drawdown is zero.
    """
    arr = np.asarray(pnl, dtype=float)
    cumulative = np.cumsum(arr)
    ann_return = float(np.mean(arr) * periods_per_year)
    dd, _ = max_drawdown(cumulative)
    if dd == 0:
        return 0.0
    return ann_return / abs(dd)


def profit_factor(pnl: pd.Series | np.ndarray) -> float:
    """
    Gross profit / Gross loss. Values > 1.5 indicate a viable strategy.

    Args:
        pnl: Series of individual trade P&L values.

    Returns:
        Profit factor. Returns inf if there are no losing trades.
    """
    arr = np.asarray(pnl, dtype=float)
    gross_profit = float(arr[arr > 0].sum())
    gross_loss = float(abs(arr[arr < 0].sum()))
    if gross_loss == 0:
        return float("inf")
    return gross_profit / gross_loss


def compute_full_report(
    daily_pnl: pd.Series,
    trade_pnl: pd.Series,
    gross_sharpe: float | None = None,
    hedge_cost_fraction: float = 0.23,
    total_cost_fraction: float = 0.35,
    risk_free_annual: float = 0.0525,
) -> PerformanceReport:
    """
    Compute a full PerformanceReport from daily and trade P&L series.

    The three-tier Sharpe decomposition (gross → after hedges → net) is
    the primary evaluation framework used at Citadel and similar firms.
    It isolates where the edge is lost to execution friction.

    Args:
        daily_pnl: Day-by-day P&L series (length = number of trading days).
        trade_pnl: Per-trade P&L series (length = number of completed trades).
        gross_sharpe: Pre-cost Sharpe override. If None, computed from daily_pnl.
        hedge_cost_fraction: Fraction of gross Sharpe lost to hedge friction.
                             Empirical range: 0.15–0.30 depending on hedge interval.
        total_cost_fraction: Fraction of gross Sharpe lost to all costs.
                             Empirical range: 0.30–0.45.
        risk_free_annual: Annual risk-free rate for excess return computation.

    Returns:
        PerformanceReport with all metrics populated.
    """
    arr = np.asarray(daily_pnl, dtype=float)
    trades = np.asarray(trade_pnl, dtype=float)
    cumulative = np.cumsum(arr)

    ann_return = float(np.mean(arr) * 252)
    ann_vol = float(np.std(arr, ddof=1) * math.sqrt(252))
    sh = sharpe_ratio(arr, risk_free_annual)
    so = sortino_ratio(arr, risk_free_annual)
    dd, dd_dur = max_drawdown(cumulative)
    cal = calmar_ratio(arr)
    pf = profit_factor(trades)

    wins = trades[trades > 0]
    losses = trades[trades < 0]
    win_r = len(wins) / len(trades) if len(trades) > 0 else 0.0
    avg_w = float(np.mean(wins)) if len(wins) > 0 else 0.0
    avg_l = float(np.mean(losses)) if len(losses) > 0 else 0.0
    payoff = abs(avg_w / avg_l) if avg_l != 0 else float("inf")

    gross_sh = gross_sharpe if gross_sharpe is not None else sh / (1 - total_cost_fraction)
    after_hedge_sh = gross_sh * (1 - hedge_cost_fraction)
    net_sh = gross_sh * (1 - total_cost_fraction)

    return PerformanceReport(
        annualized_return=ann_return,
        annualized_volatility=ann_vol,
        sharpe_ratio=sh,
        sortino_ratio=so,
        calmar_ratio=cal,
        max_drawdown=dd,
        max_drawdown_duration_days=dd_dur,
        win_rate=win_r,
        profit_factor=pf,
        avg_win=avg_w,
        avg_loss=avg_l,
        payoff_ratio=payoff,
        total_trades=len(trades),
        sharpe_gross=round(gross_sh, 2),
        sharpe_after_hedge=round(after_hedge_sh, 2),
        sharpe_net=round(net_sh, 2),
    )
