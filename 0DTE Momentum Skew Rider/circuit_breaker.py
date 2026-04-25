"""
Circuit Breaker System
======================
Automatic kill switches that halt trading when predefined
risk thresholds are breached.

Design philosophy (Jane Street / Two Sigma approach):
  "It's not about predicting when things go wrong.
   It's about ensuring survival when they do."

Breaker levels:
  WARNING  → Reduce size, tighten stops, alert operator
  HALT     → Stop new positions, manage existing
  EMERGENCY → Close all immediately, full stop

Recovery:
  All halts require manual review and confirmation.
  The strategy cannot self-resume from a HALT state.
  This is intentional. Human judgment required.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Callable, Optional, List, Awaitable
import structlog

log = structlog.get_logger(__name__)


class CircuitBreakerState(Enum):
    OK = auto()
    WARNING = auto()
    HALT = auto()
    EMERGENCY = auto()


@dataclass
class CBStatus:
    state: CircuitBreakerState
    reason: str = ""
    triggered_at: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None


class CircuitBreaker:
    """
    Market-wide and strategy-specific circuit breaker system.
    Continuously monitors conditions and triggers halts automatically.
    """

    def __init__(self, limits, on_halt: Callable[[str], Awaitable[None]]):
        self.limits = limits
        self.on_halt = on_halt
        self._state = CircuitBreakerState.OK
        self._halt_reason: Optional[str] = None
        self._halt_time: Optional[datetime] = None
        self._cooldown_until: Optional[datetime] = None
        self._triggered_breakers: List[str] = []

        # Tracked metrics (updated by check())
        self._consecutive_losses = 0
        self._daily_slippage_bps = 0.0
        self._fill_rejection_rate = 0.0
        self._last_latency_ms = 0.0
        self._data_staleness_seconds = 0.0
        self._api_error_rate = 0.0

    async def check(self) -> CBStatus:
        """
        Run all circuit breaker checks.
        Call this every tick before any trading logic.
        """
        if self._state == CircuitBreakerState.HALT:
            # Check if cooldown has expired
            if self._cooldown_until and datetime.now() > self._cooldown_until:
                log.info("circuit_breaker.cooldown_expired",
                         halt_reason=self._halt_reason)
                # Still requires manual reset — cooldown just allows re-check
            return CBStatus(
                state=self._state,
                reason=self._halt_reason or "Previously halted",
                triggered_at=self._halt_time,
                cooldown_until=self._cooldown_until,
            )

        # Refresh metrics from market data / system monitors
        await self._refresh_metrics()

        # Run all checks
        checks = [
            self._check_vix(),
            self._check_market_gaps(),
            self._check_consecutive_losses(),
            self._check_slippage(),
            self._check_fill_rejection_rate(),
            self._check_system_health(),
            self._check_market_halts(),
        ]

        for check_result in checks:
            if check_result.state == CircuitBreakerState.HALT:
                await self._trigger_halt(check_result.reason)
                return check_result
            elif check_result.state == CircuitBreakerState.WARNING:
                log.warning("circuit_breaker.warning", reason=check_result.reason)

        return CBStatus(state=CircuitBreakerState.OK)

    # ──────────────────────────────────────────────────────────────
    # INDIVIDUAL BREAKER CHECKS
    # ──────────────────────────────────────────────────────────────

    def _check_vix(self) -> CBStatus:
        """VIX-based volatility circuit breaker."""
        vix = self._get_current_vix()

        if vix >= self.limits.vix_liquidate_threshold:
            return CBStatus(
                state=CircuitBreakerState.HALT,
                reason=f"VIX emergency threshold: {vix:.1f} >= {self.limits.vix_liquidate_threshold}"
            )
        elif vix >= self.limits.vix_halt_threshold:
            return CBStatus(
                state=CircuitBreakerState.HALT,
                reason=f"VIX halt threshold: {vix:.1f} >= {self.limits.vix_halt_threshold}"
            )
        elif vix >= self.limits.vix_halt_threshold * 0.85:  # 85% of halt = warning
            return CBStatus(
                state=CircuitBreakerState.WARNING,
                reason=f"VIX approaching halt threshold: {vix:.1f}"
            )

        return CBStatus(state=CircuitBreakerState.OK)

    def _check_market_gaps(self) -> CBStatus:
        """Check for gap opens that indicate unstable market conditions."""
        gap_pct = self._get_market_gap_pct()

        if abs(gap_pct) >= self.limits.spy_gap_pct:
            return CBStatus(
                state=CircuitBreakerState.HALT,
                reason=f"Market gap detected: {gap_pct:.2%} >= {self.limits.spy_gap_pct:.2%}. "
                       f"Waiting 30 minutes for conditions to normalize."
            )

        return CBStatus(state=CircuitBreakerState.OK)

    def _check_consecutive_losses(self) -> CBStatus:
        """Halt after N consecutive losing trades."""
        max_losses = self.limits.consecutive_losses_halt

        if self._consecutive_losses >= max_losses:
            return CBStatus(
                state=CircuitBreakerState.HALT,
                reason=f"Consecutive losses: {self._consecutive_losses} >= {max_losses}. "
                       f"Strategy may be misaligned with current market regime. "
                       f"Manual review required."
            )

        if self._consecutive_losses == max_losses - 1:
            return CBStatus(
                state=CircuitBreakerState.WARNING,
                reason=f"One loss from halt ({self._consecutive_losses}/{max_losses})"
            )

        return CBStatus(state=CircuitBreakerState.OK)

    def _check_slippage(self) -> CBStatus:
        """Halt if slippage is systematically high (algo may be front-run)."""
        max_slippage = self.limits.max_slippage_per_day_bps

        if self._daily_slippage_bps > max_slippage:
            return CBStatus(
                state=CircuitBreakerState.HALT,
                reason=f"Slippage too high: {self._daily_slippage_bps:.1f}bps > {max_slippage}bps. "
                       f"Possible front-running or poor market conditions."
            )

        if self._daily_slippage_bps > max_slippage * 0.7:
            return CBStatus(
                state=CircuitBreakerState.WARNING,
                reason=f"Slippage elevated: {self._daily_slippage_bps:.1f}bps"
            )

        return CBStatus(state=CircuitBreakerState.OK)

    def _check_fill_rejection_rate(self) -> CBStatus:
        """Halt if broker is rejecting too many orders (connectivity issue)."""
        max_rate = self.limits.fill_rejection_rate_halt

        if self._fill_rejection_rate >= max_rate:
            return CBStatus(
                state=CircuitBreakerState.HALT,
                reason=f"Fill rejection rate: {self._fill_rejection_rate:.1%} >= {max_rate:.1%}. "
                       f"Possible broker connectivity or compliance issue."
            )

        return CBStatus(state=CircuitBreakerState.OK)

    def _check_system_health(self) -> CBStatus:
        """Halt on system degradation (latency, stale data, API errors)."""
        max_latency = self.limits.max_latency_ms
        max_staleness = self.limits.data_staleness_seconds
        max_error_rate = self.limits.max_api_error_rate

        if self._last_latency_ms > max_latency:
            return CBStatus(
                state=CircuitBreakerState.HALT,
                reason=f"Execution latency too high: {self._last_latency_ms:.0f}ms > {max_latency}ms. "
                       f"Cannot trade safely with degraded execution."
            )

        if self._data_staleness_seconds > max_staleness:
            return CBStatus(
                state=CircuitBreakerState.HALT,
                reason=f"Market data stale: {self._data_staleness_seconds:.1f}s > {max_staleness}s. "
                       f"Cannot make informed decisions without live data."
            )

        if self._api_error_rate > max_error_rate:
            return CBStatus(
                state=CircuitBreakerState.HALT,
                reason=f"API error rate: {self._api_error_rate:.1%} > {max_error_rate:.1%}"
            )

        return CBStatus(state=CircuitBreakerState.OK)

    def _check_market_halts(self) -> CBStatus:
        """Detect market-wide circuit breakers (Level 1/2/3)."""
        if not self.limits.market_halt_detection:
            return CBStatus(state=CircuitBreakerState.OK)

        market_halted = self._detect_market_halt()
        if market_halted:
            return CBStatus(
                state=CircuitBreakerState.HALT,
                reason="Market-wide circuit breaker detected. Trading suspended."
            )

        return CBStatus(state=CircuitBreakerState.OK)

    # ──────────────────────────────────────────────────────────────
    # HALT MANAGEMENT
    # ──────────────────────────────────────────────────────────────

    async def _trigger_halt(self, reason: str):
        """Trigger a halt — close positions and stop trading."""
        if self._state == CircuitBreakerState.HALT:
            return  # Already halted

        self._state = CircuitBreakerState.HALT
        self._halt_reason = reason
        self._halt_time = datetime.now()
        self._cooldown_until = datetime.now() + timedelta(
            minutes=self.limits.cooldown_after_halt_minutes
        )

        log.critical("circuit_breaker.halt_triggered",
                     reason=reason,
                     cooldown_until=self._cooldown_until.isoformat())

        # Notify strategy to close all positions
        await self.on_halt(reason)

    def manual_reset(self, operator_id: str, reason: str) -> bool:
        """
        Manually reset circuit breaker after human review.
        Requires manual_override_required = True to be configured.

        Returns True if reset successful, False if conditions still unsafe.
        """
        if not self.limits.manual_override_required:
            log.warning("circuit_breaker.reset_skipped_no_override_required")
            return False

        if self._cooldown_until and datetime.now() < self._cooldown_until:
            remaining = (self._cooldown_until - datetime.now()).seconds // 60
            log.warning("circuit_breaker.reset_rejected_cooldown",
                        minutes_remaining=remaining)
            return False

        # Verify conditions are safe before resetting
        vix_safe = self._get_current_vix() < self.limits.vix_halt_threshold * 0.9
        if not vix_safe:
            log.warning("circuit_breaker.reset_rejected_unsafe_vix")
            return False

        log.warning("circuit_breaker.manually_reset",
                    operator=operator_id,
                    reason=reason,
                    previous_halt_reason=self._halt_reason)

        self._state = CircuitBreakerState.OK
        self._halt_reason = None
        self._consecutive_losses = 0  # Reset on manual override
        return True

    def record_loss(self):
        """Record a losing trade for consecutive loss tracking."""
        self._consecutive_losses += 1
        log.info("circuit_breaker.loss_recorded",
                 consecutive_losses=self._consecutive_losses,
                 halt_threshold=self.limits.consecutive_losses_halt)

    def record_win(self):
        """Record a winning trade — resets consecutive loss counter."""
        if self._consecutive_losses > 0:
            log.info("circuit_breaker.consecutive_losses_reset",
                     previous_count=self._consecutive_losses)
        self._consecutive_losses = 0

    def update_slippage(self, fill_bps: float):
        """Update daily average slippage tracking."""
        # Simple EMA update
        alpha = 0.1
        self._daily_slippage_bps = (
            alpha * fill_bps + (1 - alpha) * self._daily_slippage_bps
        )

    def update_latency(self, latency_ms: float):
        """Update execution latency tracking."""
        self._last_latency_ms = latency_ms

    def update_data_staleness(self, staleness_seconds: float):
        """Update market data staleness tracking."""
        self._data_staleness_seconds = staleness_seconds

    # ──────────────────────────────────────────────────────────────
    # DATA FETCH STUBS (implement with live data)
    # ──────────────────────────────────────────────────────────────

    def _get_current_vix(self) -> float:
        """Fetch current VIX level. Replace with live data feed."""
        # Implementation: fetch $VIX from your data provider
        raise NotImplementedError("Implement VIX fetch from live data feed")

    def _get_market_gap_pct(self) -> float:
        """Calculate market open gap vs previous close."""
        raise NotImplementedError("Implement gap calculation from open/prev_close")

    def _detect_market_halt(self) -> bool:
        """Detect if market-wide circuit breaker is active."""
        raise NotImplementedError("Implement market halt detection")

    async def _refresh_metrics(self):
        """Refresh all monitored metrics. Called every tick."""
        # In production: these update from live feeds asynchronously
        # This method orchestrates the refresh
        pass
