"""
0DTE Momentum Skew Rider — Main Strategy Orchestrator
======================================================
Jane Street-style institutional risk management applied to
retail 0DTE options momentum trading.

Architecture: Event-driven async pipeline
  Market Data → Signal Engine → Risk Guardian → Execution Engine
                                     ↓
                              Circuit Breakers
                              Greeks Monitor
                              P&L Tracker
"""

import asyncio
import signal
import sys
from datetime import datetime, time
from decimal import Decimal
from enum import Enum, auto
from typing import Optional
import structlog

from src.core.portfolio import Portfolio
from src.core.session import SessionManager
from src.signals.composite_signal import CompositeSignalEngine
from src.risk.guardian import RiskGuardian
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerState
from src.risk.greeks_monitor import GreeksMonitor
from src.risk.position_sizer import PositionSizer
from src.execution.order_manager import OrderManager
from src.utils.logger import setup_logger
from src.utils.time_utils import MarketCalendar
from config.loader import load_config, load_risk_limits

log = structlog.get_logger(__name__)


class StrategyState(Enum):
    INITIALIZING = auto()
    WAITING_FOR_OPEN = auto()
    ACTIVE = auto()
    RISK_PAUSED = auto()
    CLOSING_ALL = auto()
    HALTED = auto()
    CLOSED = auto()


class ZeroDTEMomentumSkewRider:
    """
    Main strategy class. Orchestrates the full pipeline from signal
    generation to execution with institutional-grade risk management.

    NEVER instantiate this with live=True without passing 60+ paper sessions.
    """

    def __init__(self, config_path: str = "config/base_config.yaml"):
        self.config = load_config(config_path)
        self.risk_limits = load_risk_limits("config/risk_limits.yaml")
        self.state = StrategyState.INITIALIZING
        self.logger = setup_logger(self.config.logging)

        # Core components
        self.portfolio = Portfolio(self.config, self.risk_limits)
        self.session = SessionManager(self.config.strategy.session)
        self.calendar = MarketCalendar()

        # Signal engine
        self.signal_engine = CompositeSignalEngine(
            config=self.config.signals,
            universe=self.config.strategy.universe,
        )

        # Risk layer — the most critical component
        self.risk_guardian = RiskGuardian(
            risk_limits=self.risk_limits,
            portfolio=self.portfolio,
        )
        self.circuit_breaker = CircuitBreaker(
            limits=self.risk_limits.circuit_breakers,
            on_halt=self._emergency_halt,
        )
        self.greeks_monitor = GreeksMonitor(
            limits=self.risk_limits.greeks_limits,
            portfolio=self.portfolio,
            on_breach=self._handle_greeks_breach,
        )
        self.position_sizer = PositionSizer(
            config=self.config,
            risk_limits=self.risk_limits,
        )

        # Execution
        self.order_manager = OrderManager(
            config=self.config.execution,
            risk_guardian=self.risk_guardian,
        )

        # State tracking
        self._consecutive_losses = 0
        self._daily_pnl = Decimal("0")
        self._daily_peak_pnl = Decimal("0")
        self._trades_today = 0
        self._halted_reason: Optional[str] = None

        # Register OS signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)

        log.info("strategy.initialized",
                 mode=self.config.strategy.mode,
                 universe=self.config.strategy.universe)

    # ──────────────────────────────────────────────────────────────
    # MAIN LOOP
    # ──────────────────────────────────────────────────────────────

    async def run(self):
        """Main async event loop."""
        log.info("strategy.starting")

        try:
            await self._validate_environment()
            await self._initialize_connections()
            self.state = StrategyState.WAITING_FOR_OPEN

            while self.state not in (StrategyState.HALTED, StrategyState.CLOSED):
                try:
                    await self._main_tick()
                    await asyncio.sleep(1)  # 1-second tick for 0DTE
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    await self._handle_unexpected_error(e)

        finally:
            await self._graceful_shutdown()

    async def _main_tick(self):
        """Single iteration of the main strategy loop."""

        # ── 1. Session Management ────────────────────────────────
        session_status = await self.session.check()

        if not session_status.is_market_open:
            self.state = StrategyState.WAITING_FOR_OPEN
            return

        if session_status.is_hard_close_time:
            await self._initiate_close_all(reason="Hard close time reached")
            return

        if session_status.is_entry_cutoff:
            # No new entries, but manage existing positions
            await self._manage_existing_positions()
            return

        if session_status.blackout_event:
            log.warning("session.news_blackout",
                        event=session_status.blackout_event,
                        minutes_remaining=session_status.blackout_minutes_remaining)
            await self._manage_existing_positions()
            return

        # ── 2. Circuit Breaker Check ─────────────────────────────
        cb_status = await self.circuit_breaker.check()
        if cb_status.state != CircuitBreakerState.OK:
            if cb_status.state == CircuitBreakerState.HALT:
                await self._emergency_halt(cb_status.reason)
                return
            elif cb_status.state == CircuitBreakerState.WARNING:
                log.warning("circuit_breaker.warning", reason=cb_status.reason)

        # ── 3. Daily P&L Check ──────────────────────────────────
        pnl_check = self._check_daily_pnl_limits()
        if pnl_check.halt_required:
            await self._emergency_halt(pnl_check.reason)
            return
        if pnl_check.no_new_trades:
            await self._manage_existing_positions()
            return

        # ── 4. Greeks Monitoring ─────────────────────────────────
        await self.greeks_monitor.update()
        greeks_status = self.greeks_monitor.get_status()

        if greeks_status.emergency_breach:
            await self._handle_greeks_breach(greeks_status)
            return

        # ── 5. Existing Position Management ─────────────────────
        await self._manage_existing_positions()

        # ── 6. Signal Generation ─────────────────────────────────
        if self.state == StrategyState.RISK_PAUSED:
            return  # Don't generate new signals when paused

        if self.portfolio.position_count >= self.risk_limits.portfolio_risk.max_concurrent_positions:
            log.debug("strategy.max_positions_reached",
                      count=self.portfolio.position_count)
            return

        # Generate composite signal
        signal = await self.signal_engine.generate()

        if signal is None or signal.score < self.config.signals.composite.min_composite_score:
            return

        log.info("signal.generated",
                 score=signal.score,
                 direction=signal.direction,
                 skew_component=signal.skew_score,
                 gex_component=signal.gex_score,
                 momentum_component=signal.momentum_score)

        # ── 7. Pre-Trade Risk Check ──────────────────────────────
        trade_proposal = await self._build_trade_proposal(signal)
        if trade_proposal is None:
            return

        risk_decision = await self.risk_guardian.evaluate(trade_proposal)

        if not risk_decision.approved:
            log.warning("risk.trade_rejected",
                        reason=risk_decision.rejection_reason,
                        signal_score=signal.score)
            return

        # ── 8. Size the Trade ────────────────────────────────────
        sized_trade = self.position_sizer.size(
            proposal=trade_proposal,
            portfolio=self.portfolio,
            kelly_fraction=0.25,  # Quarter Kelly — conservative
        )

        if sized_trade.contracts == 0:
            log.warning("sizing.zero_contracts", reason="Insufficient edge or capital")
            return

        # ── 9. Execute ───────────────────────────────────────────
        order_result = await self.order_manager.submit(sized_trade)

        if order_result.filled:
            self._trades_today += 1
            position = self.portfolio.add_position(order_result)
            log.info("trade.entered",
                     position_id=position.id,
                     structure=sized_trade.structure,
                     contracts=order_result.contracts_filled,
                     avg_price=order_result.avg_fill_price,
                     max_risk=sized_trade.max_risk)

    # ──────────────────────────────────────────────────────────────
    # POSITION MANAGEMENT
    # ──────────────────────────────────────────────────────────────

    async def _manage_existing_positions(self):
        """
        Manage all open positions:
        - Check stop losses
        - Take profits
        - Delta hedge
        - Charm/gamma unwind near expiry
        """
        for position in list(self.portfolio.open_positions):
            try:
                await self._manage_position(position)
            except Exception as e:
                log.error("position.management_error",
                          position_id=position.id, error=str(e))
                # On error: attempt to close the position defensively
                await self._defensive_close(position, reason=f"Management error: {e}")

    async def _manage_position(self, position):
        """Manage a single open position."""
        # Refresh Greeks and mark-to-market
        await position.refresh()

        current_pnl_pct = position.unrealized_pnl_pct
        time_to_close = self.session.time_to_hard_close()

        # ── Gamma Risk Sunset Rule ───────────────────────────────
        # 0DTE gamma explodes in the last 90 minutes.
        # Mandatory size reduction to prevent catastrophic loss.
        if time_to_close.seconds < 90 * 60:  # < 90 minutes to close
            gamma_factor = self._calculate_gamma_sunset_factor(time_to_close)
            if gamma_factor < 1.0:
                await self._reduce_position(position, factor=gamma_factor,
                                            reason="Gamma sunset rule")
                return

        # ── Hard Stop Loss ───────────────────────────────────────
        stop_pct = self.risk_limits.per_trade_risk.stop_loss_trigger_pct
        if current_pnl_pct <= -stop_pct:
            await self.order_manager.close_position(position)
            self._record_loss(position)
            log.warning("position.stopped_out",
                        position_id=position.id,
                        pnl_pct=current_pnl_pct)
            return

        # ── Trailing Stop from Peak ──────────────────────────────
        trail_pct = self.risk_limits.per_trade_risk.trailing_stop_from_peak_pct
        drawdown_from_peak = (position.peak_pnl_pct - current_pnl_pct)
        if position.peak_pnl_pct > 0 and drawdown_from_peak >= trail_pct:
            await self.order_manager.close_position(position)
            log.info("position.trailing_stop_triggered",
                     position_id=position.id,
                     peak_pnl=position.peak_pnl_pct,
                     current_pnl=current_pnl_pct)
            return

        # ── Scale Out Profit Taking ──────────────────────────────
        scale_levels = self.risk_limits.per_trade_risk.scale_out_at_pct
        for level in scale_levels:
            if current_pnl_pct >= level and not position.has_scaled_at(level):
                await self._scale_out(position, target_pct=level)
                break

        # ── Full Profit Target ───────────────────────────────────
        profit_target = self.risk_limits.per_trade_risk.profit_target_pct
        if current_pnl_pct >= profit_target:
            await self.order_manager.close_position(position)
            log.info("position.profit_target_reached",
                     position_id=position.id, pnl_pct=current_pnl_pct)
            return

        # ── Delta Hedge Check ────────────────────────────────────
        delta_threshold = self.risk_limits.greeks_limits.delta_hedge_threshold
        if abs(position.delta) > delta_threshold:
            await self._hedge_delta(position)

    def _calculate_gamma_sunset_factor(self, time_to_close) -> float:
        """
        Returns a fraction [0, 1] representing what portion of
        original size to keep based on time remaining.

        Schedule:
          > 90 min: 100% (no reduction)
          90 → 60 min: 75%
          60 → 45 min: 50%
          45 → 30 min: 25%
          < 30 min: 0% (must be fully closed)
        """
        minutes_remaining = time_to_close.seconds / 60
        if minutes_remaining > 90:
            return 1.0
        elif minutes_remaining > 60:
            return 0.75
        elif minutes_remaining > 45:
            return 0.50
        elif minutes_remaining > 30:
            return 0.25
        else:
            return 0.0  # Force close

    # ──────────────────────────────────────────────────────────────
    # RISK MANAGEMENT
    # ──────────────────────────────────────────────────────────────

    def _check_daily_pnl_limits(self):
        """Check all daily P&L limits. Returns decision object."""

        class PnLDecision:
            def __init__(self):
                self.halt_required = False
                self.no_new_trades = False
                self.reason = ""

        decision = PnLDecision()
        limits = self.risk_limits.portfolio_risk

        # Update peak
        if self._daily_pnl > self._daily_peak_pnl:
            self._daily_peak_pnl = self._daily_pnl

        # Hard daily loss limit
        if self._daily_pnl <= -limits.daily_loss_limit_pct:
            decision.halt_required = True
            decision.reason = f"Daily loss limit breached: {self._daily_pnl:.2%}"
            return decision

        # Trailing drawdown from peak
        drawdown = self._daily_peak_pnl - self._daily_pnl
        if drawdown >= limits.trailing_drawdown_from_peak_pct and self._daily_peak_pnl > 0:
            decision.halt_required = True
            decision.reason = f"Trailing drawdown from peak breached: {drawdown:.2%}"
            return decision

        # Daily profit target — stop new trades (protect profits)
        if self._daily_pnl >= limits.daily_profit_target_pct:
            decision.no_new_trades = True
            decision.reason = f"Daily profit target reached: {self._daily_pnl:.2%}"
            return decision

        # Consecutive losses
        if self._consecutive_losses >= self.risk_limits.circuit_breakers.consecutive_losses_halt:
            decision.halt_required = True
            decision.reason = f"Consecutive losses: {self._consecutive_losses}"

        return decision

    async def _handle_greeks_breach(self, greeks_status):
        """Handle Greeks limit breach — reduce exposure proportionally."""
        log.warning("greeks.breach_detected",
                    breached=greeks_status.breached_metrics,
                    values=greeks_status.current_values)

        if greeks_status.emergency_breach:
            # Close all positions immediately
            await self._initiate_close_all(reason="Emergency Greeks breach")
        else:
            # Reduce most exposed positions
            self.state = StrategyState.RISK_PAUSED
            await self._reduce_greek_exposure(greeks_status)

    async def _emergency_halt(self, reason: str):
        """
        EMERGENCY HALT — Most critical function in the codebase.
        Atomically close all positions and halt trading.
        """
        self._halted_reason = reason
        self.state = StrategyState.HALTED

        log.critical("strategy.emergency_halt",
                     reason=reason,
                     open_positions=self.portfolio.position_count,
                     daily_pnl=str(self._daily_pnl))

        # Close everything — in parallel for speed
        close_tasks = [
            self.order_manager.close_position(pos)
            for pos in self.portfolio.open_positions
        ]

        if close_tasks:
            results = await asyncio.gather(*close_tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                log.critical("halt.close_errors",
                             error_count=len(errors),
                             errors=[str(e) for e in errors])
                # If positions couldn't be closed, send alert immediately
                await self._send_critical_alert(
                    f"EMERGENCY: {len(errors)} positions could not be closed! Manual intervention required."
                )

        await self._send_halt_alert(reason)

    async def _initiate_close_all(self, reason: str):
        """Orderly close of all positions (non-emergency)."""
        self.state = StrategyState.CLOSING_ALL
        log.info("strategy.closing_all", reason=reason,
                 position_count=self.portfolio.position_count)

        for position in list(self.portfolio.open_positions):
            await self.order_manager.close_position(position)

        self.state = StrategyState.CLOSED

    # ──────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────

    async def _build_trade_proposal(self, signal):
        """Build a structured trade proposal from a signal."""
        # Select structure based on market regime
        regime = await self.signal_engine.get_market_regime()

        structure_map = {
            "trending": self.config.trade_structure.trending_regime,
            "pinning": self.config.trade_structure.pinning_regime,
            "explosive": self.config.trade_structure.explosive_regime,
        }

        structure = structure_map.get(regime, "debit_spread")

        return await self.signal_engine.build_trade_proposal(
            signal=signal,
            structure=structure,
            config=self.config.trade_structure,
        )

    async def _scale_out(self, position, target_pct: float):
        """Close 50% of the position as partial profit taking."""
        contracts_to_close = max(1, position.contracts // 2)
        await self.order_manager.partial_close(position, contracts_to_close)
        position.mark_scaled_at(target_pct)
        log.info("position.scaled_out",
                 position_id=position.id,
                 target_pct=target_pct,
                 contracts_closed=contracts_to_close)

    async def _reduce_position(self, position, factor: float, reason: str):
        """Reduce position to `factor` of original size."""
        target_contracts = int(position.contracts * factor)
        if target_contracts < position.contracts:
            contracts_to_close = position.contracts - target_contracts
            await self.order_manager.partial_close(position, contracts_to_close)
            log.info("position.reduced",
                     position_id=position.id,
                     factor=factor,
                     reason=reason,
                     contracts_closed=contracts_to_close)

    async def _hedge_delta(self, position):
        """Delta hedge using underlying shares/ETF."""
        delta = position.delta
        log.info("position.delta_hedging",
                 position_id=position.id, delta=delta)
        # Hedge via underlying — implementation in order_manager
        await self.order_manager.hedge_delta(position)

    async def _reduce_greek_exposure(self, greeks_status):
        """Systematically reduce exposure for each breached Greek."""
        # Sort positions by greek contribution, reduce largest contributors
        for metric in greeks_status.breached_metrics:
            positions_by_exposure = sorted(
                self.portfolio.open_positions,
                key=lambda p: abs(getattr(p.greeks, metric, 0)),
                reverse=True
            )
            for position in positions_by_exposure[:2]:  # Reduce top 2 contributors
                await self._reduce_position(position, factor=0.5,
                                            reason=f"Greek limit: {metric}")

    def _record_loss(self, position):
        """Record a losing trade for consecutive loss tracking."""
        if position.realized_pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

    async def _validate_environment(self):
        """Validate configuration and environment before starting."""
        errors = []

        if self.config.strategy.mode == "live":
            log.warning("⚠️  LIVE TRADING MODE ACTIVE — Real capital at risk!")

        # Check required environment variables
        import os
        required_env = ["BROKER_API_KEY", "BROKER_API_SECRET"]
        for var in required_env:
            if not os.getenv(var):
                errors.append(f"Missing environment variable: {var}")

        # Validate risk limits are sane
        limits = self.risk_limits.portfolio_risk
        if limits.daily_loss_limit_pct > 0.05:
            errors.append("daily_loss_limit_pct > 5% — extremely dangerous for 0DTE")
        if limits.max_concurrent_positions > 5:
            errors.append("max_concurrent_positions > 5 — dangerous for 0DTE")

        if errors:
            for err in errors:
                log.error("validation.failed", error=err)
            raise ValueError(f"Environment validation failed: {errors}")

        log.info("validation.passed")

    async def _initialize_connections(self):
        """Initialize broker, data feed, and monitoring connections."""
        await self.order_manager.connect()
        await self.signal_engine.connect()
        log.info("connections.established")

    async def _graceful_shutdown(self):
        """Clean shutdown — close positions, disconnect, save state."""
        log.info("strategy.shutting_down",
                 state=self.state.name,
                 open_positions=self.portfolio.position_count)

        # Close any remaining positions
        if self.portfolio.position_count > 0:
            await self._initiate_close_all(reason="Graceful shutdown")

        # Disconnect from brokers
        await self.order_manager.disconnect()
        await self.signal_engine.disconnect()

        # Save session report
        await self._save_session_report()
        log.info("strategy.shutdown_complete")

    async def _save_session_report(self):
        """Save end-of-day performance report."""
        report = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "daily_pnl": str(self._daily_pnl),
            "trades": self._trades_today,
            "consecutive_losses": self._consecutive_losses,
            "halt_reason": self._halted_reason,
            "final_state": self.state.name,
        }
        log.info("session.report", **report)

    async def _send_halt_alert(self, reason: str):
        """Send alert when strategy halts."""
        log.critical("alert.halt", reason=reason)
        # Implement Slack/email notification here

    async def _send_critical_alert(self, message: str):
        """Send critical alert requiring immediate human attention."""
        log.critical("alert.critical", message=message)
        # Implement PagerDuty/SMS notification here

    def _handle_shutdown_signal(self, sig, frame):
        """Handle OS shutdown signals gracefully."""
        log.info("strategy.os_signal_received", signal=sig)
        asyncio.create_task(self._graceful_shutdown())

    async def _defensive_close(self, position, reason: str):
        """Last-resort defensive close for a problematic position."""
        try:
            await self.order_manager.close_position(position, aggressive=True)
            log.warning("position.defensive_close", position_id=position.id, reason=reason)
        except Exception as e:
            log.critical("position.defensive_close_failed",
                         position_id=position.id, error=str(e))
            await self._send_critical_alert(
                f"CRITICAL: Cannot close position {position.id}. Manual intervention required!"
            )
