"""
Gamma Sunset Engine
====================
Enforces mandatory position reduction as expiration approaches.

This module exists because near-expiry gamma is the single most
dangerous force in options trading. It CANNOT be negotiated with.

══════════════════════════════════════════════════════════════════
  WHY GAMMA EXPLODES NEAR EXPIRY
══════════════════════════════════════════════════════════════════

Gamma for an ATM option as DTE approaches zero:

  Gamma = N'(d1) / (S × σ × √T)

  As T → 0:
    √T → 0
    Denominator → 0
    Gamma → ∞ (theoretically infinite!)

  In practice, ATM gamma goes from ~0.05 at 30 DTE
  to ~0.20 at 5 DTE to ~0.80+ at 0DTE

What this means for P&L:
  A $1 move in the underlying causes:
    30 DTE: delta changes by 0.05 → small position adjustment needed
    5 DTE:  delta changes by 0.20 → moderate P&L impact
    0DTE:   delta changes by 0.80+ → CATASTROPHIC P&L impact

Concrete example ($450 SPY, short 10 ATM calls):
  SPY moves $1 against us:
    30 DTE: Lose ~$50 from gamma (manageable)
    5 DTE:  Lose ~$200 from gamma (painful)
    0DTE:   Lose ~$800+ from gamma (potential margin call)

  SPY moves $5 against us on 0DTE (happens in 1-2% of sessions):
    Gamma loss = ~$4,000-8,000 per position (potentially catastrophic)

The Sunset Solution:
  Mandatory position reduction on a fixed schedule.
  NO DISCRETION — the engine closes positions mechanically.

══════════════════════════════════════════════════════════════════
  THE SUNSET SCHEDULE (non-negotiable)
══════════════════════════════════════════════════════════════════

  On ANY expiry day:
    14:00 ET: No new entries (enforced in strategy.py)
    14:00 ET: Alert — positions approaching sunset
    14:30 ET: Reduce ALL positions by 50% (mandatory)
    15:00 ET: Close ALL remaining positions (HARD STOP)
    15:30 ET: Final check — any remaining short options → emergency close

  On NON-expiry days (holding into next session):
    Strategy does NOT hold short near-money options overnight
    All positions closed by 15:30 if DTE ≤ 1

  Why these times?
    14:30 = T-60 min: Gamma starts becoming dangerous
    15:00 = T-30 min: Gamma is extremely dangerous
    15:30 = T-0:  Exercise/assignment window — NEVER be short here
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, date, timedelta
from enum import Enum, auto
from typing import List, Optional, Callable, Awaitable
import structlog

log = structlog.get_logger(__name__)


class SunsetPhase(Enum):
    """Current phase of the gamma sunset protocol."""
    NORMAL = auto()         # >2 hrs to close — full size OK
    ALERT = auto()          # T-120 min: alert operators
    REDUCE_50 = auto()      # T-90 min: reduce all to 50%
    CLOSE_ALL = auto()      # T-60 min: close everything
    EMERGENCY = auto()      # T-30 min: emergency close any survivors
    PAST_CLOSE = auto()     # After close — should be flat


@dataclass
class SunsetStatus:
    """Current sunset phase and actions required."""
    phase: SunsetPhase
    minutes_to_close: float
    positions_requiring_action: List[str]   # Position IDs needing close/reduce
    message: str
    action_required: bool


class GammaSunsetEngine:
    """
    Mandatory gamma risk reduction engine for expiry day.

    Runs as a background task from market open on expiry day.
    Enforces the sunset schedule without exception.
    All actions are logged, auditable, and cannot be disabled.
    """

    # Market close time
    MARKET_CLOSE = time(16, 0, 0)

    # Sunset schedule: (minutes_before_close, phase, action)
    SUNSET_SCHEDULE = [
        (120, SunsetPhase.ALERT,     "Alert: 2 hours to close — review positions"),
        (90,  SunsetPhase.REDUCE_50, "MANDATORY: Reduce all short option positions by 50%"),
        (60,  SunsetPhase.CLOSE_ALL, "MANDATORY: Close ALL short option positions"),
        (30,  SunsetPhase.EMERGENCY, "EMERGENCY: Any remaining short options — close NOW"),
        (0,   SunsetPhase.PAST_CLOSE, "Market closed — verify flat"),
    ]

    def __init__(
        self,
        config,
        portfolio,
        order_manager,
        on_phase_change: Callable[[SunsetPhase, str], Awaitable[None]],
    ):
        self.config = config
        self.portfolio = portfolio
        self.order_manager = order_manager
        self.on_phase_change = on_phase_change

        self._current_phase = SunsetPhase.NORMAL
        self._running = False
        self._actions_taken: List[dict] = []

    async def start_expiry_day_monitoring(self, expiry_date: date):
        """
        Start the sunset monitoring for an expiry day.
        MUST be called at market open (9:30 AM) on expiry days.
        """
        if date.today() != expiry_date:
            log.info("gamma_sunset.not_expiry_day",
                     today=str(date.today()), expiry=str(expiry_date))
            return

        self._running = True
        log.info("gamma_sunset.monitoring_started",
                 expiry_date=str(expiry_date))

        while self._running:
            try:
                await self._check_and_enforce_sunset()
                await asyncio.sleep(30)  # Check every 30 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("gamma_sunset.error", error=str(e))
                # Even on error, try to close positions if near close
                if self._minutes_to_close() <= 60:
                    await self._emergency_close_all("Sunset engine error near close")

    async def stop(self):
        """Stop monitoring."""
        self._running = False

    async def _check_and_enforce_sunset(self):
        """Check current time and enforce the appropriate sunset phase."""
        minutes_remaining = self._minutes_to_close()
        required_phase = self._determine_phase(minutes_remaining)

        # Phase transition
        if required_phase != self._current_phase:
            await self._transition_to_phase(required_phase, minutes_remaining)

    def _determine_phase(self, minutes_remaining: float) -> SunsetPhase:
        """Determine which sunset phase we should be in."""
        if minutes_remaining > 120:
            return SunsetPhase.NORMAL
        elif minutes_remaining > 90:
            return SunsetPhase.ALERT
        elif minutes_remaining > 60:
            return SunsetPhase.REDUCE_50
        elif minutes_remaining > 30:
            return SunsetPhase.CLOSE_ALL
        elif minutes_remaining > 0:
            return SunsetPhase.EMERGENCY
        else:
            return SunsetPhase.PAST_CLOSE

    async def _transition_to_phase(self, new_phase: SunsetPhase, minutes_remaining: float):
        """Execute actions required for a phase transition."""
        old_phase = self._current_phase
        self._current_phase = new_phase

        log.warning("gamma_sunset.phase_transition",
                    from_phase=old_phase.name,
                    to_phase=new_phase.name,
                    minutes_to_close=minutes_remaining)

        # Notify observers
        message = self._get_phase_message(new_phase, minutes_remaining)
        await self.on_phase_change(new_phase, message)

        # Execute mandatory actions
        if new_phase == SunsetPhase.ALERT:
            await self._send_alert(message)

        elif new_phase == SunsetPhase.REDUCE_50:
            await self._reduce_all_positions_by_50_percent()

        elif new_phase == SunsetPhase.CLOSE_ALL:
            await self._close_all_short_options()

        elif new_phase == SunsetPhase.EMERGENCY:
            await self._emergency_close_all("Emergency sunset — T-30 min to close")

        elif new_phase == SunsetPhase.PAST_CLOSE:
            await self._verify_flat_at_close()

    async def _reduce_all_positions_by_50_percent(self):
        """
        Reduce ALL short option positions by 50%.
        THIS IS MANDATORY — no exceptions, no overrides.
        """
        short_positions = self._get_short_option_positions()

        if not short_positions:
            log.info("gamma_sunset.reduce_50.no_positions")
            return

        log.warning("gamma_sunset.REDUCING_50_PERCENT",
                    position_count=len(short_positions),
                    tickers=[p.underlying for p in short_positions])

        reduce_tasks = []
        for position in short_positions:
            contracts_to_close = max(1, position.contracts // 2)
            reduce_tasks.append(
                self.order_manager.partial_close(
                    position,
                    contracts_to_close,
                    reason="Mandatory gamma sunset: T-90min reduction",
                )
            )

        results = await asyncio.gather(*reduce_tasks, return_exceptions=True)

        for position, result in zip(short_positions, results):
            if isinstance(result, Exception):
                log.error("gamma_sunset.reduce_failed",
                          position_id=position.id,
                          ticker=position.underlying,
                          error=str(result))
                # If reduce fails, escalate to full close
                await self.order_manager.close_position(
                    position,
                    aggressive=True,
                    reason="Gamma sunset reduce failed — escalating to full close",
                )
            else:
                self._actions_taken.append({
                    "time": datetime.now().isoformat(),
                    "action": "reduce_50",
                    "position_id": position.id,
                    "ticker": position.underlying,
                })

    async def _close_all_short_options(self):
        """
        Close ALL short option positions.
        THIS IS MANDATORY — no exceptions, no overrides.
        """
        short_positions = self._get_short_option_positions()

        if not short_positions:
            log.info("gamma_sunset.close_all.no_positions")
            return

        log.critical("gamma_sunset.CLOSING_ALL_SHORT_OPTIONS",
                     position_count=len(short_positions),
                     tickers=[p.underlying for p in short_positions])

        close_tasks = [
            self.order_manager.close_position(
                position,
                aggressive=True,
                reason="Mandatory gamma sunset: T-60min close",
            )
            for position in short_positions
        ]

        results = await asyncio.gather(*close_tasks, return_exceptions=True)

        errors = [(pos, r) for pos, r in zip(short_positions, results)
                  if isinstance(r, Exception)]

        if errors:
            for position, error in errors:
                log.critical("gamma_sunset.CLOSE_FAILED",
                             position_id=position.id,
                             ticker=position.underlying,
                             error=str(error),
                             action="IMMEDIATE MANUAL INTERVENTION REQUIRED")
        else:
            log.info("gamma_sunset.all_positions_closed",
                     count=len(short_positions))

        for position in short_positions:
            self._actions_taken.append({
                "time": datetime.now().isoformat(),
                "action": "close_all",
                "position_id": position.id,
                "ticker": position.underlying,
            })

    async def _emergency_close_all(self, reason: str):
        """Emergency close — any survivors at T-30 min."""
        remaining = self._get_short_option_positions()
        if not remaining:
            return

        log.critical("gamma_sunset.EMERGENCY_CLOSE",
                     reason=reason,
                     positions_remaining=len(remaining))

        for position in remaining:
            try:
                await self.order_manager.close_position(
                    position,
                    aggressive=True,
                    reason=f"EMERGENCY GAMMA SUNSET: {reason}",
                )
            except Exception as e:
                log.critical("gamma_sunset.EMERGENCY_CLOSE_FAILED",
                             position_id=position.id,
                             error=str(e),
                             action="CALL BROKER IMMEDIATELY TO CLOSE POSITION")

    async def _verify_flat_at_close(self):
        """Verify we are flat at market close."""
        remaining = self._get_short_option_positions()
        if remaining:
            log.critical("gamma_sunset.NOT_FLAT_AT_CLOSE",
                         positions_remaining=[p.underlying for p in remaining],
                         action="MANUAL BROKER CALL REQUIRED — POTENTIAL ASSIGNMENT RISK")
        else:
            log.info("gamma_sunset.VERIFIED_FLAT_AT_CLOSE")

    def _get_short_option_positions(self):
        """Get all positions with short option legs."""
        return [
            p for p in self.portfolio.open_positions
            if hasattr(p, 'has_short_leg') and p.has_short_leg
        ]

    def _minutes_to_close(self) -> float:
        """Minutes remaining until market close (4:00 PM ET)."""
        now = datetime.now()
        close_today = now.replace(
            hour=self.MARKET_CLOSE.hour,
            minute=self.MARKET_CLOSE.minute,
            second=0, microsecond=0
        )
        delta = (close_today - now).total_seconds() / 60
        return max(0.0, delta)

    def _get_phase_message(self, phase: SunsetPhase, minutes: float) -> str:
        messages = {
            SunsetPhase.NORMAL: f"Normal trading — {minutes:.0f} min to close",
            SunsetPhase.ALERT: "⚠️  ALERT: T-120 min — review all short option positions",
            SunsetPhase.REDUCE_50: "🟠 MANDATORY: Reducing all positions 50% — T-90min",
            SunsetPhase.CLOSE_ALL: "🔴 MANDATORY: Closing ALL short options — T-60min",
            SunsetPhase.EMERGENCY: "🚨 EMERGENCY: T-30min — any remaining short options closed",
            SunsetPhase.PAST_CLOSE: "Market closed — verifying flat",
        }
        return messages.get(phase, "Unknown phase")

    async def _send_alert(self, message: str):
        log.warning("gamma_sunset.alert", message=message)

    def get_audit_log(self) -> List[dict]:
        """Return full audit log of all sunset actions taken."""
        return list(self._actions_taken)

    def get_current_phase(self) -> SunsetPhase:
        return self._current_phase
