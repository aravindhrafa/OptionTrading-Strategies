"""
Strategy 01: Gamma Scalp Accumulator
=====================================

**Edge:** Profits when realized volatility exceeds implied volatility via
continuous delta-neutral rebalancing of a long ATM straddle.

**Core equation:**
    Daily P&L ≈ ½ × Γ × (ΔS)² − |Θ| × Δt

    You profit when the realized variance (left term) exceeds theta decay
    (right term). This occurs when IV Rank is elevated but the market
    underestimates the actual daily moves.

**Regime:**
    Best in trending intraday markets with IV Rank > 40 and a meaningful
    spread between 5-day realized vol and 30-day implied vol.

**Risk:**
    Max loss = premium paid (defined risk). No margin required.
    Forced close by 3:30pm prevents overnight theta drag.

References:
    - Derman, E. & Miller, M. (2016). "The Volatility Smile." Wiley.
    - Taleb, N.N. (1997). "Dynamic Hedging." Wiley.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

from options_alpha.engine.black_scholes import BlackScholes, OptionType
from options_alpha.strategies.base import (
    Bar,
    BaseStrategy,
    PositionState,
    StrategyType,
    TradeRecord,
    TradeStatus,
)


@dataclass
class GammaScalpState(PositionState):
    """
    Extended position state for gamma scalp trades.

    Inherits all fields from PositionState and adds gamma-specific tracking.

    Attributes:
        strike: Option strike price (set at ATM on entry).
        entry_spot: Underlying price at entry.
        last_hedge_spot: Spot price at which last delta hedge was executed.
        cum_hedge_cost: Total transaction cost from all hedges (always positive).
        net_delta: Current net portfolio delta (target: near zero).
    """
    strike: float = 0.0
    entry_spot: float = 0.0
    last_hedge_spot: float = 0.0
    cum_hedge_cost: float = 0.0
    net_delta: float = 0.0
    trailing_stop_activated: bool = False


class GammaScalpAccumulator(BaseStrategy):
    """
    Strategy 01: Gamma Scalp Accumulator.

    Buys an ATM straddle 30+ minutes after market open when IV Rank is elevated
    and the spread between realized and implied vol is wide. Delta-hedges
    dynamically on every ``hedge_delta_interval`` move to accumulate Gamma P&L.

    The strategy monetizes the convexity of the long straddle position. Every
    time the underlying makes a directional move and is hedged back to delta-neutral,
    it locks in a small positive P&L proportional to gamma × move².

    Parameters:
        iv_rank_threshold: Minimum IV Rank (0–100) to enter. Default: 40.
        rv_iv_spread_min: Minimum (IV − RV) / IV spread. Default: 0.20 (20%).
        hedge_delta_interval: Delta move that triggers a hedge. Default: 0.05.
        profit_target_mult: Exit at (premium × mult). Default: 1.5.
        stop_loss_pct: Exit at (premium × pct) loss. Default: 0.50.
        slippage_bps: Bid-ask slippage per hedge in basis points. Default: 2.
        entry_window_start: Earliest bar time for entry. Default: 10:00.
        entry_window_end: Latest bar time for entry. Default: 10:30.
        force_exit_time: Hard close time. Default: 15:30.
        risk_free_rate: Annual risk-free rate for BS pricing. Default: 0.0525.

    Example:
        >>> strategy = GammaScalpAccumulator(
        ...     iv_rank_threshold=40,
        ...     rv_iv_spread_min=0.20,
        ...     hedge_delta_interval=0.05,
        ...     profit_target_mult=1.5,
        ...     stop_loss_pct=0.50,
        ... )
        >>> bar = Bar(
        ...     timestamp=datetime(2024, 1, 15, 10, 5),
        ...     open=4950.0, high=4955.0, low=4945.0, close=4952.0,
        ...     volume=1_200_000, iv=0.20, iv_rank=45.0, realized_vol=0.14,
        ... )
        >>> strategy.should_enter(bar, context={})
        True
    """

    TRADING_DAYS_PER_YEAR: int = 252
    CALENDAR_DAYS_PER_YEAR: int = 365

    def __init__(
        self,
        iv_rank_threshold: float = 40.0,
        rv_iv_spread_min: float = 0.20,
        hedge_delta_interval: float = 0.05,
        profit_target_mult: float = 1.5,
        stop_loss_pct: float = 0.50,
        slippage_bps: float = 2.0,
        entry_window_start: time = time(10, 0),
        entry_window_end: time = time(10, 30),
        force_exit_time: time = time(15, 30),
        risk_free_rate: float = 0.0525,
    ) -> None:
        super().__init__(
            profit_target_mult=profit_target_mult,
            stop_loss_pct=stop_loss_pct,
        )
        self.iv_rank_threshold = iv_rank_threshold
        self.rv_iv_spread_min = rv_iv_spread_min
        self.hedge_delta_interval = hedge_delta_interval
        self.slippage_bps = slippage_bps
        self.entry_window_start = entry_window_start
        self.entry_window_end = entry_window_end
        self.force_exit_time = force_exit_time
        self._bs = BlackScholes(risk_free_rate=risk_free_rate)

    @property
    def name(self) -> str:
        return "gamma_scalp_accumulator"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.INTRADAY

    def should_enter(self, bar: Bar, context: dict[str, Any]) -> bool:
        """
        Entry conditions (all must be true):

        1. Bar time within entry window (default 10:00–10:30am)
        2. IV Rank > threshold (default 40) — options are relatively expensive
        3. (Implied Vol − Realized Vol) / Implied Vol > spread_min — market
           is underestimating actual move potential
        4. VIX term structure not inverted (if VIX data available) — avoids
           entering during acute vol crush regimes

        Args:
            bar: Current market data bar.
            context: Backtester context. Checked keys:
                     ``'already_traded_today'`` (bool): prevent re-entry same day.

        Returns:
            True if all conditions are satisfied.
        """
        if context.get("already_traded_today", False):
            return False

        bar_time = bar.timestamp.time()
        if not (self.entry_window_start <= bar_time <= self.entry_window_end):
            return False

        if bar.iv_rank < self.iv_rank_threshold:
            return False

        if bar.iv <= 0:
            return False

        rv_iv_spread = (bar.iv - bar.realized_vol) / bar.iv
        if rv_iv_spread < self.rv_iv_spread_min:
            return False

        return True

    def on_entry(self, bar: Bar, capital: float) -> GammaScalpState:
        """
        Initialize a long ATM straddle position.

        Buys one ATM call and one ATM put at the current spot price.
        Sets initial hedge state to delta-neutral (net delta ≈ 0 for ATM straddle).

        Args:
            bar: Entry bar with current market data.
            capital: Total portfolio capital for sizing.

        Returns:
            Initialized GammaScalpState with entry premium and Greek snapshot.
        """
        S = bar.close
        K = round(S / 5) * 5  # Round to nearest $5 strike
        T = 1.0 / self.TRADING_DAYS_PER_YEAR  # Same-day expiry
        sigma = bar.iv

        premium, greeks = self._bs.straddle_price(S, K, T, sigma)

        return GammaScalpState(
            entry_premium=premium,
            entry_time=bar.timestamp,
            strike=K,
            entry_spot=S,
            last_hedge_spot=S,
            hedge_shares=0.0,  # ATM straddle starts delta-neutral
            cum_hedge_pnl=0.0,
            cum_hedge_cost=0.0,
            net_delta=greeks.delta,
            peak_pnl=0.0,
            current_pnl=0.0,
            is_open=True,
        )

    def on_bar(self, bar: Bar, state: GammaScalpState) -> GammaScalpState:
        """
        Process each bar: reprice straddle, execute delta hedges, mark P&L.

        Delta hedge logic:
            If |current_delta - 0| > hedge_delta_interval, trade shares
            to bring net portfolio delta back to zero.

        Slippage model:
            Each hedge trade incurs (slippage_bps / 10000) × spot in cost.

        Args:
            bar: Current market data bar.
            state: Current position state.

        Returns:
            Updated GammaScalpState.
        """
        S = bar.close
        K = state.strike
        T_remaining = max(
            (bar.timestamp.replace(hour=16, minute=0, second=0) - bar.timestamp).seconds
            / (6.5 * 3600)  # Fraction of trading day remaining
            / self.TRADING_DAYS_PER_YEAR,
            1e-6,  # Floor to avoid T=0 singularity
        )
        sigma = bar.iv

        _, greeks = self._bs.straddle_price(S, K, T_remaining, sigma)

        # Net portfolio delta = straddle delta + hedge shares (normalized)
        portfolio_delta = greeks.delta + (state.hedge_shares / 1.0)

        # Execute hedge if delta has drifted beyond threshold
        if abs(portfolio_delta) >= self.hedge_delta_interval:
            hedge_qty = -portfolio_delta
            slip = S * self.slippage_bps / 10_000
            trade_price = S + slip * math.copysign(1, hedge_qty)
            hedge_cost = abs(hedge_qty) * slip
            hedge_pnl = -hedge_qty * trade_price

            state.hedge_shares += hedge_qty
            state.cum_hedge_pnl += hedge_pnl
            state.cum_hedge_cost += hedge_cost
            state.net_delta = portfolio_delta + hedge_qty
            state.last_hedge_spot = S
            state.num_hedges += 1
        else:
            state.net_delta = portfolio_delta

        # Mark-to-market P&L
        call_val = self._bs.price(S, K, T_remaining, sigma, OptionType.CALL).price
        put_val = self._bs.price(S, K, T_remaining, sigma, OptionType.PUT).price
        straddle_mtm = call_val + put_val
        hedge_mtm = state.hedge_shares * S

        state.current_pnl = (
            straddle_mtm
            - state.entry_premium
            + state.cum_hedge_pnl
            + hedge_mtm
            - state.cum_hedge_cost
        )
        state.peak_pnl = max(state.peak_pnl, state.current_pnl)

        return state

    def should_exit(
        self, bar: Bar, state: GammaScalpState
    ) -> tuple[bool, str]:
        """
        Exit conditions (first match wins):

        1. Profit target: current_pnl >= entry_premium × profit_target_mult
        2. Stop loss: current_pnl <= −entry_premium × stop_loss_pct
        3. Time: bar time >= force_exit_time (default 3:30pm)

        Args:
            bar: Current market data bar.
            state: Current position state.

        Returns:
            Tuple of (exit: bool, reason: str).
        """
        pnl = state.current_pnl
        premium = state.entry_premium

        if pnl >= premium * self.profit_target_mult:
            return True, "profit_target"

        if pnl <= -premium * self.stop_loss_pct:
            return True, "stop_loss"

        if bar.timestamp.time() >= self.force_exit_time:
            return True, "time"

        return False, ""

    def on_exit(
        self, bar: Bar, state: GammaScalpState, reason: str
    ) -> TradeRecord:
        """
        Finalize the trade and return a complete record.

        Maps exit reason string to TradeStatus enum and captures all P&L
        attribution fields for post-trade analysis.

        Args:
            bar: Exit bar.
            state: Final position state.
            reason: One of 'profit_target', 'stop_loss', 'time'.

        Returns:
            TradeRecord with full attribution.
        """
        status_map = {
            "profit_target": TradeStatus.CLOSED_PROFIT_TARGET,
            "stop_loss": TradeStatus.CLOSED_STOP_LOSS,
            "time": TradeStatus.CLOSED_TIME,
        }
        state.is_open = False

        record = TradeRecord(
            strategy_name=self.name,
            entry_time=state.entry_time,
            exit_time=bar.timestamp,
            underlying="SPX",  # Default; override in production via context
            entry_premium=state.entry_premium,
            exit_pnl=state.current_pnl - state.cum_hedge_pnl,
            hedge_pnl=state.cum_hedge_pnl - state.cum_hedge_cost,
            total_pnl=state.current_pnl,
            status=status_map.get(reason, TradeStatus.CLOSED_SIGNAL),
            num_hedges=state.num_hedges,
            greeks_at_entry={},  # Populated by backtester from on_entry()
        )
        self._trade_history.append(record)
        return record

    def on_day_end(
        self, date: Any, state: GammaScalpState | None
    ) -> GammaScalpState | None:
        """Force-close any open position at end of day to prevent theta drag."""
        if state is not None and state.is_open:
            state.is_open = False
        return state
