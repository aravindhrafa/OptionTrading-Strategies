"""
Earnings Volatility Skew Arbitrage — Main Strategy Orchestrator
================================================================
Event-driven orchestrator that manages the full lifecycle of
earnings vol trades: from calendar scanning → signal generation
→ risk validation → execution → overnight hold → post-earnings exit.

Key differences from intraday strategies:
  1. CALENDAR DRIVEN — trades triggered by earnings calendar, not market conditions
  2. MULTI-DAY HOLD — position opened 1-5 days before, closed morning after
  3. OVERNIGHT RISK — unhedgeable gap risk between entry and post-earnings open
  4. IV CRUSH EXIT — exit triggered by IV collapse, not price stop
  5. DATABASE DEPENDENCY — requires historical IV data for calibration

Daily workflow:
  Pre-market:
    1. Refresh earnings calendar
    2. Check upcoming events (next 5 days)
    3. Fetch IV snapshots for eligible tickers
    4. Generate signals
    5. Risk validate and size each trade

  Market open:
    6. Execute new entries
    7. Monitor overnight positions
    8. Post-earnings: exit in first 30 minutes

  Post-market:
    9. Record post-earnings IV data to database
    10. Update P&L and performance metrics
    11. Review circuit breakers and daily limits
"""

import asyncio
import signal
import sys
from datetime import datetime, date, time as dtime, timedelta
from decimal import Decimal
from enum import Enum, auto
from typing import Optional, List, Dict
import structlog

from src.core.earnings_calendar import EarningsCalendar, EarningsEvent, EarningsTiming
from src.core.portfolio import Portfolio
from src.core.session import SessionManager
from src.signals.iv_skew_signal import IVSkewSignalEngine, RecommendedStructure
from src.signals.historical_iv import IVCrushDatabase
from src.risk.guardian import RiskGuardian
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerState
from src.risk.position_sizer import PositionSizer
from src.risk.gap_risk import GapRiskAssessor
from src.execution.order_manager import OrderManager
from src.execution.exit_manager import PostEarningsExitManager
from src.utils.logger import setup_logger
from config.loader import load_config, load_risk_limits

log = structlog.get_logger(__name__)


class StrategyState(Enum):
    INITIALIZING = auto()
    PRE_MARKET_SCAN = auto()
    ACTIVE_ENTRY = auto()         # Within entry window, can open new positions
    HOLDING_OVERNIGHT = auto()    # Positions held, no new entries after cutoff
    POST_EARNINGS_EXIT = auto()   # Executing post-earnings closes
    RISK_PAUSED = auto()
    HALTED = auto()
    CLOSED = auto()


class EarningsVolSkewArb:
    """
    Main strategy class.

    Manages the complete lifecycle of earnings IV skew arbitrage trades
    with institutional-grade risk management at every stage.
    """

    def __init__(self, config_path: str = "config/base_config.yaml"):
        self.config = load_config(config_path)
        self.risk_limits = load_risk_limits("config/risk_limits.yaml")
        self.state = StrategyState.INITIALIZING
        self.logger = setup_logger(self.config.logging)

        # Core components
        self.portfolio = Portfolio(self.config, self.risk_limits)
        self.session = SessionManager(self.config.strategy.session)

        # Earnings-specific components
        self.calendar = EarningsCalendar(self.config)
        self.iv_database = IVCrushDatabase()

        # Signal engines — one per ticker
        self._signal_engines: Dict[str, IVSkewSignalEngine] = {}
        self._init_signal_engines()

        # Risk layer
        self.risk_guardian = RiskGuardian(self.risk_limits, self.portfolio)
        self.circuit_breaker = CircuitBreaker(
            self.risk_limits.circuit_breakers,
            on_halt=self._emergency_halt,
        )
        self.gap_risk = GapRiskAssessor(self.risk_limits, self.iv_database)
        self.position_sizer = PositionSizer(self.config, self.risk_limits)

        # Execution
        self.order_manager = OrderManager(self.config.execution, self.risk_guardian)
        self.exit_manager = PostEarningsExitManager(
            self.config, self.order_manager, self.portfolio
        )

        # State
        self._daily_pnl = Decimal("0")
        self._daily_peak_pnl = Decimal("0")
        self._consecutive_losses = 0
        self._trades_this_season = 0
        self._halted_reason: Optional[str] = None
        self._pending_events: List[EarningsEvent] = []

        # Register OS signals
        signal.signal(signal.SIGINT, self._handle_os_signal)
        signal.signal(signal.SIGTERM, self._handle_os_signal)

        log.info("strategy.initialized",
                 mode=self.config.strategy.mode,
                 universe=list(self.config.strategy.universe.values()))

    def _init_signal_engines(self):
        """Initialize per-ticker signal engines."""
        all_tickers = (
            self.config.strategy.universe.get("liquid_large_cap", []) +
            self.config.strategy.universe.get("financials", []) +
            self.config.strategy.universe.get("etfs", [])
        )
        for ticker in all_tickers:
            self._signal_engines[ticker] = IVSkewSignalEngine(
                config=self.config.signals, underlying=ticker
            )

    # ──────────────────────────────────────────────────────────────
    # MAIN LOOP
    # ──────────────────────────────────────────────────────────────

    async def run(self):
        """Main async event loop."""
        log.info("strategy.starting")

        try:
            await self._validate_environment()
            await self._initialize_connections()

            while self.state not in (StrategyState.HALTED, StrategyState.CLOSED):
                try:
                    await self._main_tick()
                    await asyncio.sleep(30)  # 30-second tick (not HFT speed)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    await self._handle_unexpected_error(e)
        finally:
            await self._graceful_shutdown()

    async def _main_tick(self):
        """Single iteration of the main strategy loop."""
        now = datetime.now()
        current_time = now.time()

        # ── Pre-market: Daily calendar scan ─────────────────────
        if current_time < dtime(9, 30):
            await self._pre_market_workflow()
            return

        # ── Circuit breaker check ────────────────────────────────
        cb_status = await self.circuit_breaker.check()
        if cb_status.state != CircuitBreakerState.OK:
            if cb_status.state == CircuitBreakerState.HALT:
                await self._emergency_halt(cb_status.reason)
                return

        # ── P&L limit check ──────────────────────────────────────
        if self._daily_pnl <= -self.risk_limits.portfolio_risk.daily_loss_limit_pct:
            await self._emergency_halt(
                f"Daily loss limit breached: {self._daily_pnl:.2%}"
            )
            return

        # ── Post-earnings exit window ────────────────────────────
        if await self._is_post_earnings_exit_window():
            self.state = StrategyState.POST_EARNINGS_EXIT
            await self._execute_post_earnings_exits()
            return

        # ── Manage existing positions ────────────────────────────
        await self._manage_overnight_positions()

        # ── Entry logic (if within entry window) ─────────────────
        entry_cutoff = dtime(
            *[int(x) for x in self.config.strategy.session.entry_cutoff.split(":")]
        )
        if current_time > entry_cutoff:
            self.state = StrategyState.HOLDING_OVERNIGHT
            return

        if self.state != StrategyState.RISK_PAUSED:
            self.state = StrategyState.ACTIVE_ENTRY
            await self._process_new_entries()

    # ──────────────────────────────────────────────────────────────
    # PRE-MARKET WORKFLOW
    # ──────────────────────────────────────────────────────────────

    async def _pre_market_workflow(self):
        """
        Daily pre-market routine.
        Runs before market open to prepare for the day.
        """
        self.state = StrategyState.PRE_MARKET_SCAN
        log.info("strategy.pre_market_scan_starting")

        # Step 1: Initialize IV database
        await self.iv_database.initialize()

        # Step 2: Refresh earnings calendar
        self._pending_events = await self.calendar.refresh()
        log.info("pre_market.calendar_refreshed",
                 upcoming_events=len(self._pending_events),
                 events=[e.ticker for e in self._pending_events])

        # Step 3: Check for any date changes on existing positions
        await self._audit_open_positions_for_date_changes()

        # Step 4: Record pre-earnings IV for tonight's reporters
        await self._record_pre_earnings_iv_snapshots()

        # Step 5: Alert if positions are holding overnight with earnings tonight
        overnight = [
            p for p in self.portfolio.open_positions
            if await self._has_earnings_tonight(p.underlying)
        ]
        if overnight:
            log.warning("pre_market.positions_with_tonight_earnings",
                        positions=[p.underlying for p in overnight],
                        action="Verify these are intentional earnings holds")

        log.info("pre_market.workflow_complete")

    # ──────────────────────────────────────────────────────────────
    # NEW ENTRY PROCESSING
    # ──────────────────────────────────────────────────────────────

    async def _process_new_entries(self):
        """
        Evaluate all upcoming earnings events for new trade entries.
        """
        if not self._pending_events:
            return

        # Check portfolio capacity
        limits = self.risk_limits.portfolio_risk
        if self.portfolio.position_count >= limits.max_concurrent_earnings_positions:
            return

        for event in self._pending_events:
            # Skip if already have a position
            if self.portfolio.has_position(event.ticker):
                continue

            await self._evaluate_earnings_entry(event)

    async def _evaluate_earnings_entry(self, event: EarningsEvent):
        """
        Full evaluation pipeline for a single earnings entry candidate.
        """
        ticker = event.ticker
        log.info("entry.evaluating", ticker=ticker,
                 report_date=str(event.report_date),
                 timing=event.timing.value,
                 days_to_earnings=event.days_to_earnings)

        # ── Step 1: Calendar validation ──────────────────────────
        calendar_ok, reason = self.calendar.validate_event_for_trading(
            ticker, self.config, self.risk_limits
        )
        if not calendar_ok:
            log.info("entry.calendar_rejected",
                     ticker=ticker, reason=reason)
            return

        # ── Step 2: Gap risk pre-screen ──────────────────────────
        gap_assessment = await self.gap_risk.assess(event, self.iv_database)
        if gap_assessment.too_risky:
            log.info("entry.gap_risk_rejected",
                     ticker=ticker,
                     reason=gap_assessment.rejection_reason)
            return

        # ── Step 3: Generate IV skew signal ─────────────────────
        signal_engine = self._signal_engines.get(ticker)
        if not signal_engine:
            log.warning("entry.no_signal_engine", ticker=ticker)
            return

        signal = await signal_engine.get_signal(event, self.iv_database)
        if signal is None:
            log.debug("entry.no_signal", ticker=ticker)
            return

        if signal.recommended_structure == RecommendedStructure.NO_TRADE:
            log.info("entry.no_trade_recommended",
                     ticker=ticker,
                     iv_rank=signal.iv_rank,
                     rr_25d=signal.risk_reversal_25d)
            return

        log.info("entry.signal_approved",
                 ticker=ticker,
                 structure=signal.recommended_structure.name,
                 iv_rank=signal.iv_rank,
                 edge_bps=signal.edge.total_edge_bps,
                 regime=signal.regime.name)

        # ── Step 4: Build trade proposal ─────────────────────────
        proposal = await self._build_trade_proposal(event, signal)
        if proposal is None:
            return

        # ── Step 5: Risk guardian (12 pre-trade checks) ──────────
        risk_decision = await self.risk_guardian.evaluate(proposal)
        if not risk_decision.approved:
            log.warning("entry.risk_rejected",
                        ticker=ticker,
                        reason=risk_decision.rejection_reason)
            return

        # ── Step 6: Sector concentration check ───────────────────
        sector_ok = self._check_sector_concentration(ticker)
        if not sector_ok:
            log.warning("entry.sector_concentration",
                        ticker=ticker,
                        action="Max same-sector positions reached")
            return

        # ── Step 7: Kelly-adjusted sizing ────────────────────────
        sized_trade = self.position_sizer.size(
            proposal=proposal,
            portfolio=self.portfolio,
            kelly_fraction=0.20,  # 20% Kelly — very conservative for earnings
        )

        if sized_trade.contracts == 0:
            log.warning("entry.zero_contracts", ticker=ticker)
            return

        # ── Step 8: Execute ───────────────────────────────────────
        result = await self.order_manager.submit(sized_trade)
        if result.filled:
            position = self.portfolio.add_position(result)
            self._trades_this_season += 1

            log.info("entry.filled",
                     ticker=ticker,
                     structure=sized_trade.structure,
                     contracts=result.contracts_filled,
                     avg_price=result.avg_fill_price,
                     max_risk=sized_trade.max_risk,
                     report_date=str(event.report_date),
                     timing=event.timing.value)

            # Send alert: position opened with overnight gap risk
            await self._send_position_opened_alert(position, event, signal)

    # ──────────────────────────────────────────────────────────────
    # POST-EARNINGS EXIT
    # ──────────────────────────────────────────────────────────────

    async def _execute_post_earnings_exits(self):
        """
        Execute post-earnings exits for all positions that reported.

        This is time-critical: IV crush happens immediately at open.
        Execute ALL exits before IV partially recovers.
        """
        positions_to_exit = []
        for position in list(self.portfolio.open_positions):
            event = self.calendar.get_event(position.underlying)
            if event and self._has_reported(event):
                positions_to_exit.append(position)

        if not positions_to_exit:
            return

        log.info("post_earnings.exits_starting",
                 positions=[p.underlying for p in positions_to_exit])

        # Execute all exits in parallel (time-critical!)
        exit_tasks = [
            self.exit_manager.close_post_earnings(position)
            for position in positions_to_exit
        ]
        results = await asyncio.gather(*exit_tasks, return_exceptions=True)

        for position, result in zip(positions_to_exit, results):
            if isinstance(result, Exception):
                log.error("post_earnings.exit_failed",
                          ticker=position.underlying,
                          error=str(result))
                # Attempt aggressive close
                await self.order_manager.close_position(position, aggressive=True)
            else:
                # Record post-earnings IV for database
                await self._record_post_earnings_data(position, result)
                self._update_consecutive_losses(position)

    # ──────────────────────────────────────────────────────────────
    # OVERNIGHT POSITION MANAGEMENT
    # ──────────────────────────────────────────────────────────────

    async def _manage_overnight_positions(self):
        """
        Monitor and manage positions between entry and earnings.

        Pre-earnings: if something goes wrong (date change, vol spike),
        we may need to exit early. This is NOT the post-earnings exit.
        """
        for position in list(self.portfolio.open_positions):
            await self._monitor_pre_earnings_position(position)

    async def _monitor_pre_earnings_position(self, position):
        """Monitor a single pre-earnings holding."""
        await position.refresh()
        event = self.calendar.get_event(position.underlying)

        # ── Check for earnings date change ───────────────────────
        # CRITICAL: A date change invalidates our entire position thesis
        if event and position.entry_report_date:
            if str(event.report_date) != str(position.entry_report_date):
                log.critical("position.earnings_date_changed",
                             ticker=position.underlying,
                             entry_date=str(position.entry_report_date),
                             new_date=str(event.report_date))
                await self.order_manager.close_position(position)
                await self._send_critical_alert(
                    f"EARNINGS DATE CHANGED for {position.underlying}! "
                    f"Position closed. Was: {position.entry_report_date}, "
                    f"Now: {event.report_date}"
                )
                return

        # ── Intraday stop loss (pre-earnings) ────────────────────
        # If position is losing significantly BEFORE earnings → something's wrong
        stop_pct = self.risk_limits.per_trade_risk.intraday_stop_loss_pct
        if position.unrealized_pnl_pct <= -stop_pct:
            log.warning("position.pre_earnings_stop",
                        ticker=position.underlying,
                        pnl=position.unrealized_pnl_pct)
            await self.order_manager.close_position(position)
            return

        # ── Unexpected IV spike (someone knows something) ────────
        current_iv = await self._get_current_atm_iv(position.underlying)
        if current_iv and position.entry_atm_iv:
            iv_spike = (current_iv - position.entry_atm_iv) / position.entry_atm_iv
            if iv_spike > 0.30:  # IV up > 30% since entry — unusual
                log.warning("position.iv_spike_detected",
                            ticker=position.underlying,
                            entry_iv=position.entry_atm_iv,
                            current_iv=current_iv,
                            spike_pct=f"{iv_spike:.1%}")
                # Don't auto-close, but alert for human review
                await self._send_alert(
                    f"WARNING: {position.underlying} IV spiked {iv_spike:.0%} "
                    f"since entry. Possible informed activity. Review position."
                )

        # ── Emergency gap risk check ──────────────────────────────
        # If stock has already moved significantly (scandal, news), close early
        current_price = await self._get_current_price(position.underlying)
        if current_price and position.entry_underlying_price:
            move = abs(current_price - position.entry_underlying_price) / \
                   position.entry_underlying_price
            if move > 0.05:  # Stock moved >5% since entry intraday
                log.warning("position.large_intraday_move",
                            ticker=position.underlying,
                            move=f"{move:.1%}")
                # At discretion: could close early if wings at risk

    # ──────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────

    async def _is_post_earnings_exit_window(self) -> bool:
        """Check if we're in the post-earnings exit window for any position."""
        now = datetime.now()
        market_open = now.replace(hour=9, minute=30, second=0)
        exit_window_end = market_open + timedelta(
            minutes=self.config.strategy.earnings.post_earnings_exit_window_minutes
        )

        if not (market_open <= now <= exit_window_end):
            return False

        # Check if any position reported last night
        for position in self.portfolio.open_positions:
            event = self.calendar.get_event(position.underlying)
            if event and self._has_reported(event):
                return True

        return False

    def _has_reported(self, event: EarningsEvent) -> bool:
        """Has this earnings event already been reported?"""
        today = date.today()
        if event.timing == EarningsTiming.AMC:
            # Reported last night → exit this morning
            return event.report_date == today - timedelta(days=1)
        elif event.timing == EarningsTiming.BMO:
            # Reported this morning → exit now
            return event.report_date == today
        return False

    async def _has_earnings_tonight(self, ticker: str) -> bool:
        """Does this ticker have earnings tonight (AMC)?"""
        event = self.calendar.get_event(ticker)
        if not event:
            return False
        return (
            event.timing == EarningsTiming.AMC and
            event.report_date == date.today()
        )

    def _check_sector_concentration(self, ticker: str) -> bool:
        """
        Check sector concentration limits.
        Max 2 positions in same sector simultaneously.
        """
        sector_map = {
            "AAPL": "tech", "MSFT": "tech", "GOOGL": "tech",
            "AMZN": "tech", "META": "tech", "NVDA": "tech", "TSLA": "tech",
            "JPM": "financials", "GS": "financials", "BAC": "financials",
            "XOM": "energy", "CVX": "energy",
            "QQQ": "etf", "SPY": "etf",
        }

        ticker_sector = sector_map.get(ticker, "other")
        same_sector = [
            p for p in self.portfolio.open_positions
            if sector_map.get(p.underlying, "other") == ticker_sector
        ]

        max_sector = self.risk_limits.portfolio_risk.max_same_sector_positions
        return len(same_sector) < max_sector

    async def _build_trade_proposal(self, event: EarningsEvent, signal):
        """Build a structured trade proposal from an earnings event and signal."""
        # Implementation connects signal strikes to a full proposal object
        # with Greeks, risk metrics, and pricing
        # In production: fetch live options chain, price the spread
        raise NotImplementedError(
            "Implement _build_trade_proposal with live options chain data."
        )

    async def _record_pre_earnings_iv_snapshots(self):
        """Record IV snapshots for all tonight's/tomorrow's reporters."""
        upcoming = self.calendar.get_upcoming_events(within_days=2)
        for event in upcoming:
            try:
                await self._record_ticker_pre_iv(event)
            except Exception as e:
                log.warning("pre_market.iv_record_failed",
                            ticker=event.ticker, error=str(e))

    async def _record_ticker_pre_iv(self, event: EarningsEvent):
        """Record pre-earnings IV to database. Implement with live data."""
        raise NotImplementedError(
            "Fetch live IV surface and record to IVCrushDatabase.record_pre_earnings()"
        )

    async def _record_post_earnings_data(self, position, exit_result):
        """Record post-earnings IV and outcome to database."""
        raise NotImplementedError(
            "Fetch post-earnings IV and call IVCrushDatabase.record_post_earnings()"
        )

    async def _audit_open_positions_for_date_changes(self):
        """Check if any open positions have had earnings dates change."""
        for position in self.portfolio.open_positions:
            event = self.calendar.get_event(position.underlying)
            entry_date = getattr(position, 'entry_report_date', None)
            if event and entry_date and str(event.report_date) != str(entry_date):
                log.critical("audit.earnings_date_changed",
                             ticker=position.underlying,
                             entry_date=str(entry_date),
                             current_date=str(event.report_date))

    def _update_consecutive_losses(self, position):
        """Track consecutive losses for circuit breaker."""
        if position.realized_pnl < 0:
            self._consecutive_losses += 1
            self.circuit_breaker.record_loss()
        else:
            self._consecutive_losses = 0
            self.circuit_breaker.record_win()

    async def _emergency_halt(self, reason: str):
        """Emergency halt — close all positions immediately."""
        self._halted_reason = reason
        self.state = StrategyState.HALTED

        log.critical("strategy.emergency_halt",
                     reason=reason,
                     open_positions=self.portfolio.position_count,
                     daily_pnl=str(self._daily_pnl))

        close_tasks = [
            self.order_manager.close_position(pos, aggressive=True)
            for pos in self.portfolio.open_positions
        ]

        if close_tasks:
            results = await asyncio.gather(*close_tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                await self._send_critical_alert(
                    f"EMERGENCY HALT: {len(errors)} positions failed to close! "
                    "MANUAL INTERVENTION REQUIRED IMMEDIATELY."
                )

        await self._send_halt_alert(reason)

    async def _handle_unexpected_error(self, error: Exception):
        """Handle unexpected errors gracefully."""
        log.error("strategy.unexpected_error",
                  error=str(error), error_type=type(error).__name__)
        if self.portfolio.position_count > 0:
            await self._send_alert(
                f"Strategy error: {error}. {self.portfolio.position_count} open positions. "
                "Monitoring continues."
            )

    async def _graceful_shutdown(self):
        """Clean shutdown."""
        log.info("strategy.shutting_down")
        if self.portfolio.position_count > 0:
            log.warning("shutdown.open_positions",
                        count=self.portfolio.position_count,
                        tickers=[p.underlying for p in self.portfolio.open_positions])
        await self.order_manager.disconnect()
        log.info("strategy.shutdown_complete")

    async def _validate_environment(self):
        """Validate environment and config before starting."""
        import os
        errors = []
        for var in ["BROKER_API_KEY", "BROKER_API_SECRET"]:
            if not os.getenv(var):
                errors.append(f"Missing: {var}")

        if self.config.strategy.mode == "live":
            log.warning("LIVE TRADING MODE — Real capital at risk!")

        if errors:
            raise ValueError(f"Environment validation failed: {errors}")

    async def _initialize_connections(self):
        """Initialize all connections."""
        await self.iv_database.initialize()
        await self.order_manager.connect()
        log.info("connections.established")

    async def _get_current_atm_iv(self, ticker: str) -> Optional[float]:
        raise NotImplementedError("Implement ATM IV fetch")

    async def _get_current_price(self, ticker: str) -> Optional[float]:
        raise NotImplementedError("Implement price fetch")

    async def _send_alert(self, message: str):
        log.info("alert", message=message)

    async def _send_critical_alert(self, message: str):
        log.critical("alert.critical", message=message)

    async def _send_halt_alert(self, reason: str):
        log.critical("alert.halt", reason=reason)

    async def _send_position_opened_alert(self, position, event, signal):
        log.info("alert.position_opened",
                 ticker=position.underlying,
                 structure=signal.recommended_structure.name,
                 report_date=str(event.report_date),
                 timing=event.timing.value,
                 edge_bps=signal.edge.total_edge_bps)

    def _handle_os_signal(self, sig, frame):
        log.info("strategy.os_signal", signal=sig)
        asyncio.create_task(self._graceful_shutdown())
