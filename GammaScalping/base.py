"""
Abstract base class for all Options Alpha strategies.

Every strategy must inherit from BaseStrategy and implement the required
abstract methods. This ensures a consistent interface across all 20 strategies
and allows the backtester and portfolio manager to treat them uniformly.

Design Pattern:
    Strategy lifecycle:
        1. ``should_enter()``  — evaluate entry conditions from bar data
        2. ``on_entry()``      — initialize position, compute sizing
        3. ``on_bar()``        — intraday logic (hedges, partial exits)
        4. ``should_exit()``   — evaluate exit conditions
        5. ``on_exit()``       — record trade result, reset state
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import pandas as pd


class StrategyType(str, Enum):
    """Classification of strategy by holding period and regime."""
    INTRADAY = "intraday"
    EXPIRY = "expiry"
    MULTI_DAY = "multi_day"
    VOL_BASED = "vol_based"


class TradeStatus(str, Enum):
    """Current status of an open or closed trade."""
    OPEN = "open"
    CLOSED_PROFIT_TARGET = "closed_profit_target"
    CLOSED_STOP_LOSS = "closed_stop_loss"
    CLOSED_TIME = "closed_time"
    CLOSED_SIGNAL = "closed_signal"


@dataclass
class Bar:
    """
    Single OHLCV bar with options market data.

    Attributes:
        timestamp: Bar open time (timezone-aware recommended).
        open: Opening price.
        high: High price.
        low: Low price.
        close: Closing price.
        volume: Volume (shares/contracts).
        iv: At-the-money implied volatility (decimal, e.g. 0.18).
        iv_rank: IV Rank 0–100, computed over trailing 252 trading days.
        realized_vol: 5-day realized volatility (decimal).
        vix: VIX index value (only for index underlyings).
    """
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    iv: float
    iv_rank: float
    realized_vol: float
    vix: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeRecord:
    """
    Immutable record of a completed trade for P&L attribution.

    Attributes:
        strategy_name: Human-readable strategy identifier.
        entry_time: Bar timestamp at entry.
        exit_time: Bar timestamp at exit.
        underlying: Ticker symbol.
        entry_premium: Total premium paid at entry.
        exit_pnl: Net P&L at close (positive = profit).
        hedge_pnl: P&L from delta hedges during the trade.
        total_pnl: exit_pnl + hedge_pnl.
        status: How the trade was closed.
        num_hedges: Number of delta hedges executed.
        greeks_at_entry: Greeks snapshot at trade entry.
    """
    strategy_name: str
    entry_time: datetime
    exit_time: datetime
    underlying: str
    entry_premium: float
    exit_pnl: float
    hedge_pnl: float
    total_pnl: float
    status: TradeStatus
    num_hedges: int
    greeks_at_entry: dict[str, float]


@dataclass
class PositionState:
    """
    Mutable state maintained during an open trade.

    Strategies update this during ``on_bar()`` and ``on_exit()``.
    """
    entry_premium: float
    entry_time: datetime
    hedge_shares: float = 0.0
    cum_hedge_pnl: float = 0.0
    num_hedges: int = 0
    peak_pnl: float = 0.0
    current_pnl: float = 0.0
    is_open: bool = True


class BaseStrategy(abc.ABC):
    """
    Abstract base class for all quantitative options buying strategies.

    Subclasses must implement:
        - ``name`` property
        - ``strategy_type`` property
        - ``should_enter()``
        - ``on_entry()``
        - ``on_bar()``
        - ``should_exit()``
        - ``on_exit()``

    Optional overrides:
        - ``on_day_start()`` — called once per trading day before bars
        - ``on_day_end()``   — called once per day after last bar (force-close logic)

    Example:
        >>> class MyStrategy(BaseStrategy):
        ...     @property
        ...     def name(self) -> str:
        ...         return "my_strategy"
        ...
        ...     @property
        ...     def strategy_type(self) -> StrategyType:
        ...         return StrategyType.INTRADAY
        ...
        ...     def should_enter(self, bar: Bar, context: dict) -> bool:
        ...         return bar.iv_rank > 40
        ...
        ...     # ... implement remaining abstract methods
    """

    def __init__(
        self,
        profit_target_mult: float = 1.5,
        stop_loss_pct: float = 0.50,
        max_hold_bars: int = 78,  # Full 6.5-hour session in 5-min bars
    ) -> None:
        """
        Args:
            profit_target_mult: Exit when P&L >= (premium × this multiplier).
            stop_loss_pct: Exit when loss >= (premium × this fraction).
            max_hold_bars: Maximum number of 5-min bars to hold before forced exit.
        """
        self.profit_target_mult = profit_target_mult
        self.stop_loss_pct = stop_loss_pct
        self.max_hold_bars = max_hold_bars
        self._position: PositionState | None = None
        self._bars_held: int = 0
        self._trade_history: list[TradeRecord] = []

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique snake_case strategy identifier."""
        ...

    @property
    @abc.abstractmethod
    def strategy_type(self) -> StrategyType:
        """Classification of this strategy."""
        ...

    @abc.abstractmethod
    def should_enter(self, bar: Bar, context: dict[str, Any]) -> bool:
        """
        Evaluate whether entry conditions are satisfied for this bar.

        This is called on every bar while no position is open. It should be
        fast and purely read-only — do NOT modify strategy state here.

        Args:
            bar: Current market data bar.
            context: Shared context dict from the backtester (e.g. market regime,
                     portfolio Greeks, capital available).

        Returns:
            True if entry conditions are met, False otherwise.
        """
        ...

    @abc.abstractmethod
    def on_entry(self, bar: Bar, capital: float) -> PositionState:
        """
        Initialize position state when entering a trade.

        Called immediately after ``should_enter()`` returns True. Should compute
        entry premium, initial hedge state, and position sizing.

        Args:
            bar: Bar at which the trade is entered.
            capital: Total portfolio capital for position sizing.

        Returns:
            Initialized PositionState for this trade.
        """
        ...

    @abc.abstractmethod
    def on_bar(self, bar: Bar, state: PositionState) -> PositionState:
        """
        Process each bar while a position is open.

        This is where intraday logic lives: delta hedging, partial profit taking,
        trailing stop updates, etc.

        Args:
            bar: Current market data bar.
            state: Current position state (mutable — return updated copy).

        Returns:
            Updated PositionState.
        """
        ...

    @abc.abstractmethod
    def should_exit(self, bar: Bar, state: PositionState) -> tuple[bool, str]:
        """
        Evaluate whether the position should be closed.

        Args:
            bar: Current market data bar.
            state: Current position state.

        Returns:
            Tuple of (should_exit: bool, reason: str).
            Reason should be one of: 'profit_target', 'stop_loss', 'time', 'signal'.
        """
        ...

    @abc.abstractmethod
    def on_exit(self, bar: Bar, state: PositionState, reason: str) -> TradeRecord:
        """
        Finalize and record a closed trade.

        Args:
            bar: Bar at which the trade is closed.
            state: Final position state.
            reason: Exit reason string from ``should_exit()``.

        Returns:
            Completed TradeRecord for P&L attribution.
        """
        ...

    def on_day_start(self, date: pd.Timestamp, context: dict[str, Any]) -> None:
        """
        Called once per trading day before any bars are processed.

        Override to reset daily counters, update regime flags, etc.
        Default implementation is a no-op.
        """

    def on_day_end(self, date: pd.Timestamp, state: PositionState | None) -> PositionState | None:
        """
        Called once per day after the last bar.

        Override to implement forced end-of-day exits (required for intraday
        strategies). Default returns state unchanged.
        """
        return state

    @property
    def is_in_position(self) -> bool:
        """True if a position is currently open."""
        return self._position is not None and self._position.is_open

    @property
    def trade_history(self) -> list[TradeRecord]:
        """List of all completed trades for this strategy instance."""
        return self._trade_history.copy()

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"profit_target={self.profit_target_mult}×, "
            f"stop_loss={self.stop_loss_pct:.0%})"
        )
