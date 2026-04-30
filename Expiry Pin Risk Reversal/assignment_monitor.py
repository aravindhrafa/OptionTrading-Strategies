"""
Assignment Monitor
==================
Tracks short option positions for assignment risk and sends
early warnings before the position goes deep ITM.

CRITICAL CONTEXT: Assignment Risk in Pin Strategy
══════════════════════════════════════════════════
The short put leg of a risk reversal carries assignment risk.
In the pin strategy, we are SHORT a put below max pain.

If price falls BELOW the short put strike at expiry, the option
is exercised and we are ASSIGNED shares at the strike price.
For a $450 short put on 10 contracts:
  Assignment = we buy 1,000 shares at $450 = $450,000 obligation

This is not the end of the world if caught early, but if
allowed to expire ITM without closing:
  1. We receive shares (may not have capital)
  2. Overnight gap risk on assigned shares
  3. Margin call possible

PREVENTION RULES (strictly enforced):
  1. Never hold short puts to expiry
  2. Close if strike goes ITM on expiry day (0.5% buffer)
  3. Alert when strike is within 0.2% ITM intraday
  4. Hard close at T-60 min regardless of P&L

Early Assignment Risk (American-style options):
  European-style (SPX, XSP): NO assignment risk — cash settled
  American-style (SPY, QQQ, stocks): CAN be assigned at any time
  Most common scenario: deep ITM puts on high-dividend stocks
    when dividend > extrinsic value of the option
  Prevention: Avoid short puts on stocks near ex-dividend date
              Close any deep ITM short put within 2 days of ex-div
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from enum import Enum, auto
from typing import Optional, List, Dict, Callable, Awaitable
import structlog

log = structlog.get_logger(__name__)


class AssignmentRisk(Enum):
    """Assignment risk level for a short option position."""
    NONE = auto()         # Option clearly OTM — no risk
    WATCH = auto()        # OTM but within 1% — monitor
    CAUTION = auto()      # Within 0.5% of ITM — ready to close
    HIGH = auto()         # Within 0.2% of ITM — prepare to close
    CRITICAL = auto()     # ITM — close immediately
    ASSIGNED = auto()     # Assignment notice received


@dataclass
class AssignmentStatus:
    """Real-time assignment status for a single short option position."""
    position_id: str
    ticker: str
    option_type: str              # "put" | "call"
    short_strike: float
    current_underlying_price: float
    expiry_date: date
    dte: int
    contracts: int

    # Risk metrics
    intrinsic_value: float        # Max(0, K - S) for puts, Max(0, S - K) for calls
    extrinsic_value: float        # Time value remaining
    distance_to_itm_pct: float    # How far OTM (negative = ITM)
    risk_level: AssignmentRisk

    # Dividend risk
    ex_dividend_date: Optional[date] = None
    dividend_amount: Optional[float] = None
    dividend_assignment_risk: bool = False  # True if dividend > extrinsic value

    # Notices
    assignment_notice_received: bool = False
    early_assignment_risk: bool = False   # American-style only

    # Alerts sent
    alerts_sent: List[str] = field(default_factory=list)
    last_checked: datetime = field(default_factory=datetime.now)

    @property
    def is_itm(self) -> bool:
        return self.intrinsic_value > 0

    @property
    def close_immediately(self) -> bool:
        """Should this be closed right now?"""
        return (
            self.risk_level in (AssignmentRisk.CRITICAL, AssignmentRisk.ASSIGNED) or
            self.dividend_assignment_risk
        )


class AssignmentMonitor:
    """
    Real-time assignment risk monitor for all short option positions.

    Runs as a background task, continuously checking:
      1. Distance of each short option from ITM
      2. Time remaining to expiry
      3. Dividend dates for American-style options
      4. Exercise notices from the broker
    """

    def __init__(
        self,
        risk_limits,
        portfolio,
        on_assignment_alert: Callable[[AssignmentStatus], Awaitable[None]],
        on_force_close: Callable[[str, str], Awaitable[None]],
    ):
        self.risk_limits = risk_limits
        self.portfolio = portfolio
        self.on_assignment_alert = on_assignment_alert
        self.on_force_close = on_force_close

        self._statuses: Dict[str, AssignmentStatus] = {}
        self._running = False

    async def start(self):
        """Start the assignment monitoring background task."""
        self._running = True
        log.info("assignment_monitor.started")
        while self._running:
            try:
                await self._check_all_positions()
                freq = self.risk_limits.execution_risk.assignment_check_frequency_seconds
                await asyncio.sleep(freq)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("assignment_monitor.error", error=str(e))
                await asyncio.sleep(30)

    async def stop(self):
        """Stop the background monitoring task."""
        self._running = False
        log.info("assignment_monitor.stopped")

    async def _check_all_positions(self):
        """Check assignment risk for all short option positions."""
        short_positions = [
            p for p in self.portfolio.open_positions
            if hasattr(p, 'short_strike') and p.short_strike is not None
        ]

        if not short_positions:
            return

        check_tasks = [self._check_position(pos) for pos in short_positions]
        await asyncio.gather(*check_tasks, return_exceptions=True)

    async def _check_position(self, position) -> Optional[AssignmentStatus]:
        """
        Evaluate assignment risk for a single short option position.
        """
        try:
            underlying_price = await self._get_underlying_price(position.underlying)
            if underlying_price is None:
                return None

            expiry = getattr(position, 'expiry_date', None)
            if expiry is None:
                return None

            dte = max(0, (expiry - date.today()).days)
            option_type = getattr(position, 'option_type', 'put')
            short_strike = position.short_strike

            # Calculate ITM-ness
            if option_type == "put":
                # Put is ITM if price < strike
                intrinsic = max(0.0, short_strike - underlying_price)
                distance_to_itm_pct = (underlying_price - short_strike) / underlying_price
                # Positive = OTM, Negative = ITM
            else:
                # Call is ITM if price > strike
                intrinsic = max(0.0, underlying_price - short_strike)
                distance_to_itm_pct = (short_strike - underlying_price) / underlying_price

            # Get current option price for extrinsic calculation
            option_price = await self._get_option_price(
                ticker=position.underlying,
                strike=short_strike,
                expiry=str(expiry),
                option_type=option_type,
            )
            extrinsic = max(0.0, (option_price or 0) - intrinsic)

            # Classify risk level
            risk_level = self._classify_risk(
                distance_to_itm_pct=distance_to_itm_pct,
                dte=dte,
                intrinsic=intrinsic,
                extrinsic=extrinsic,
            )

            # Check dividend risk (American-style only)
            ex_div_date, div_amount, div_risk = await self._check_dividend_risk(
                ticker=position.underlying,
                expiry=expiry,
                extrinsic=extrinsic,
            )

            # Check for assignment notices from broker
            assignment_notice = await self._check_broker_assignment_notices(
                position.id
            )

            status = AssignmentStatus(
                position_id=position.id,
                ticker=position.underlying,
                option_type=option_type,
                short_strike=short_strike,
                current_underlying_price=underlying_price,
                expiry_date=expiry,
                dte=dte,
                contracts=position.contracts,
                intrinsic_value=intrinsic,
                extrinsic_value=extrinsic,
                distance_to_itm_pct=distance_to_itm_pct,
                risk_level=risk_level,
                ex_dividend_date=ex_div_date,
                dividend_amount=div_amount,
                dividend_assignment_risk=div_risk,
                assignment_notice_received=assignment_notice,
                early_assignment_risk=(option_type == "put" and div_risk),
            )

            self._statuses[position.id] = status

            # ── Take action based on risk level ──────────────────
            await self._handle_risk_level(status)

            return status

        except Exception as e:
            log.error("assignment_monitor.position_check_failed",
                      position_id=getattr(position, 'id', 'unknown'),
                      error=str(e))
            return None

    def _classify_risk(
        self,
        distance_to_itm_pct: float,
        dte: int,
        intrinsic: float,
        extrinsic: float,
    ) -> AssignmentRisk:
        """
        Classify assignment risk level based on distance and DTE.
        Risk thresholds tighten as we approach expiry.
        """
        if intrinsic > 0:
            return AssignmentRisk.CRITICAL  # Already ITM

        # Thresholds tighten near expiry
        if dte == 0:    # Expiry day
            if distance_to_itm_pct < 0.005:    return AssignmentRisk.CRITICAL
            elif distance_to_itm_pct < 0.010:  return AssignmentRisk.HIGH
            elif distance_to_itm_pct < 0.020:  return AssignmentRisk.CAUTION
            elif distance_to_itm_pct < 0.040:  return AssignmentRisk.WATCH
            else:                              return AssignmentRisk.NONE

        elif dte == 1:
            if distance_to_itm_pct < 0.010:   return AssignmentRisk.HIGH
            elif distance_to_itm_pct < 0.025:  return AssignmentRisk.CAUTION
            elif distance_to_itm_pct < 0.050:  return AssignmentRisk.WATCH
            else:                              return AssignmentRisk.NONE

        else:   # DTE >= 2
            if distance_to_itm_pct < 0.020:   return AssignmentRisk.CAUTION
            elif distance_to_itm_pct < 0.050:  return AssignmentRisk.WATCH
            else:                              return AssignmentRisk.NONE

    async def _handle_risk_level(self, status: AssignmentStatus):
        """Take appropriate action based on risk level."""

        # Assignment notice → immediate close
        if status.assignment_notice_received:
            status.risk_level = AssignmentRisk.ASSIGNED
            log.critical("assignment_monitor.ASSIGNMENT_NOTICE",
                         ticker=status.ticker,
                         position_id=status.position_id,
                         strike=status.short_strike,
                         contracts=status.contracts)
            await self.on_force_close(
                status.position_id,
                "Assignment notice received — closing immediately"
            )
            return

        # ITM on expiry day → emergency close
        if status.risk_level == AssignmentRisk.CRITICAL:
            alert_key = f"critical_{status.position_id}"
            if alert_key not in status.alerts_sent:
                log.critical("assignment_monitor.CRITICAL_RISK",
                             ticker=status.ticker,
                             strike=status.short_strike,
                             underlying=status.current_underlying_price,
                             dte=status.dte)
                await self.on_assignment_alert(status)
                await self.on_force_close(
                    status.position_id,
                    f"Short {status.option_type} is ITM on DTE={status.dte} — forced close"
                )
                status.alerts_sent.append(alert_key)

        # Dividend assignment risk → close early
        elif status.dividend_assignment_risk:
            alert_key = f"dividend_{status.position_id}"
            if alert_key not in status.alerts_sent:
                log.warning("assignment_monitor.DIVIDEND_RISK",
                            ticker=status.ticker,
                            ex_div_date=str(status.ex_dividend_date),
                            dividend=status.dividend_amount,
                            extrinsic=status.extrinsic_value)
                await self.on_assignment_alert(status)
                status.alerts_sent.append(alert_key)

        # HIGH risk → alert and prepare to close
        elif status.risk_level == AssignmentRisk.HIGH:
            alert_key = f"high_{status.position_id}_{status.dte}"
            if alert_key not in status.alerts_sent:
                log.warning("assignment_monitor.HIGH_RISK",
                            ticker=status.ticker,
                            distance_pct=f"{status.distance_to_itm_pct:.3%}",
                            dte=status.dte)
                await self.on_assignment_alert(status)
                status.alerts_sent.append(alert_key)

        # CAUTION → log only (no action yet)
        elif status.risk_level == AssignmentRisk.CAUTION:
            log.info("assignment_monitor.caution",
                     ticker=status.ticker,
                     distance_pct=f"{status.distance_to_itm_pct:.3%}",
                     dte=status.dte)

    async def _check_dividend_risk(
        self,
        ticker: str,
        expiry: date,
        extrinsic: float,
    ) -> tuple[Optional[date], Optional[float], bool]:
        """
        Check if early assignment is likely due to dividend capture.

        Early assignment of short put is optimal (for holder) when:
          Dividend > Extrinsic Value of the option

        If this is true near ex-dividend date, the put holder will
        exercise early to capture the dividend — we get assigned.
        """
        try:
            ex_div, div_amount = await self._get_dividend_info(ticker)

            if ex_div is None or div_amount is None:
                return None, None, False

            # Is ex-dividend before expiry?
            if ex_div > expiry:
                return ex_div, div_amount, False  # Not a risk (div after expiry)

            # Is dividend > extrinsic? (triggers early assignment)
            days_to_ex_div = (ex_div - date.today()).days
            risk = (div_amount > extrinsic and 0 <= days_to_ex_div <= 2)

            if risk:
                log.warning("assignment_monitor.dividend_assignment_likely",
                            ticker=ticker,
                            dividend=div_amount,
                            extrinsic=extrinsic,
                            ex_div_date=str(ex_div))

            return ex_div, div_amount, risk

        except Exception as e:
            log.error("assignment_monitor.dividend_check_error",
                      ticker=ticker, error=str(e))
            return None, None, False

    def get_all_statuses(self) -> Dict[str, AssignmentStatus]:
        """Return current assignment status for all monitored positions."""
        return dict(self._statuses)

    def get_critical_positions(self) -> List[AssignmentStatus]:
        """Return positions with CRITICAL or ASSIGNED risk."""
        return [
            s for s in self._statuses.values()
            if s.risk_level in (AssignmentRisk.CRITICAL, AssignmentRisk.ASSIGNED)
        ]

    # ──────────────────────────────────────────────────────────────
    # STUBS — implement with live broker data
    # ──────────────────────────────────────────────────────────────

    async def _get_underlying_price(self, ticker: str) -> Optional[float]:
        raise NotImplementedError("Implement live price feed")

    async def _get_option_price(
        self, ticker: str, strike: float, expiry: str, option_type: str
    ) -> Optional[float]:
        raise NotImplementedError("Implement live option price feed")

    async def _get_dividend_info(
        self, ticker: str
    ) -> tuple[Optional[date], Optional[float]]:
        """Get next ex-dividend date and amount. Return (date, amount) or (None, None)."""
        try:
            import yfinance as yf
            ticker_obj = yf.Ticker(ticker)
            info = ticker_obj.info
            ex_div_str = info.get("exDividendDate")
            div_rate = info.get("dividendRate", 0)

            if not ex_div_str or not div_rate:
                return None, None

            ex_div_date = date.fromtimestamp(ex_div_str)
            quarterly_div = div_rate / 4  # Quarterly dividend

            return ex_div_date, quarterly_div

        except Exception:
            return None, None

    async def _check_broker_assignment_notices(self, position_id: str) -> bool:
        """Check if broker has sent an assignment notice for this position."""
        raise NotImplementedError(
            "Implement broker exercise notice check. \n"
            "IBKR: Monitor for 'assignment' message type via EWrapper.execDetails(). \n"
            "TastyTrade: Monitor /accounts/{account-number}/positions for assigned status."
        )
