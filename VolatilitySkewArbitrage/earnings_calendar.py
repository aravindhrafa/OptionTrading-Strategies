"""
Earnings Calendar Manager
==========================
Manages earnings dates, validates confirmations, and tracks
upcoming events across the strategy universe.

CRITICAL: In earnings trading, the date is everything.
  - AMC (After Market Close): Report tonight → gap at tomorrow's open
  - BMO (Before Market Open): Report before open → gap at today's open

A single day's error in timing can mean:
  - Entering when IV is already partially crushed (less edge)
  - Missing the position entirely (no fill before event)
  - Being exposed over a weekend you didn't plan for

Data sources (in priority order):
  1. Broker API (highest confidence — they get paid to be right)
  2. earningswhispers.com (community-verified, high accuracy)
  3. yfinance / Yahoo Finance (good for historical, less reliable for upcoming)
  4. Estimize / Wall Street Horizon (professional financial data)

Date confidence scoring:
  1.0 = Multiple sources agree, confirmed by company IR
  0.9 = Two major sources agree
  0.7 = Single reliable source
  0.5 = Estimated (NEVER TRADE on this)
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from enum import Enum, auto
from typing import Optional, List, Dict, Tuple
import structlog

log = structlog.get_logger(__name__)


class EarningsTiming(Enum):
    """When the earnings report will be released relative to market."""
    BMO = "BMO"    # Before Market Open — gap at today's open
    AMC = "AMC"    # After Market Close — gap at tomorrow's open
    DMH = "DMH"    # During Market Hours — rare, intraday event
    UNKNOWN = "UNKNOWN"


class DateConfidence(Enum):
    """Confidence level in the earnings date."""
    CONFIRMED = auto()    # Company-confirmed or broker-confirmed
    HIGH = auto()         # 2+ reliable sources agree
    MEDIUM = auto()       # Single reliable source
    LOW = auto()          # Estimated/projected
    UNVERIFIED = auto()   # Do not trade


@dataclass
class EarningsEvent:
    """Full earnings event descriptor."""
    ticker: str
    report_date: date
    timing: EarningsTiming
    confidence: DateConfidence
    confidence_score: float          # 0.0 - 1.0

    # Analysts' estimates (from consensus)
    eps_estimate: Optional[float] = None
    revenue_estimate: Optional[float] = None
    whisper_number: Optional[float] = None  # EarningsWhispers EPS

    # Options market data at time of detection
    atm_iv: Optional[float] = None
    expected_move_pct: Optional[float] = None
    iv_rank: Optional[float] = None

    # Historical context
    median_historical_move_pct: Optional[float] = None
    median_iv_crush_pct: Optional[float] = None
    historical_beat_rate: Optional[float] = None

    # Trade window
    entry_open_date: Optional[date] = None    # First day we can enter
    entry_close_date: Optional[date] = None   # Last day to enter (day before report)
    exit_date: Optional[date] = None          # Day to close (morning after report)

    # Metadata
    data_sources: List[str] = field(default_factory=list)
    last_updated: datetime = field(default_factory=datetime.now)
    notes: str = ""

    @property
    def days_to_earnings(self) -> int:
        """Calendar days until earnings report."""
        return (self.report_date - date.today()).days

    @property
    def is_tradeable(self) -> bool:
        """
        Is this event in the tradeable window with sufficient confidence?
        The most important gate in the entire strategy.
        """
        # HARD REQUIREMENTS — all must pass
        if self.confidence_score < 0.90:
            return False  # Not confident enough in date

        if self.timing == EarningsTiming.UNKNOWN:
            return False  # Don't know AMC vs BMO — can't size correctly

        if self.days_to_earnings < 1:
            return False  # Too late — IV may already be crushed or event passed

        if self.days_to_earnings > 5:
            return False  # Too early — IV not at peak yet

        return True

    @property
    def gap_exposure_open(self) -> bool:
        """
        True if the position will be exposed to a gap open.
        AMC: gap at next morning's open
        BMO: gap at today's open (if we're holding from prior day)
        """
        return self.timing in (EarningsTiming.AMC, EarningsTiming.BMO)

    def compute_entry_window(self, config) -> Tuple[date, date]:
        """Compute the valid entry window for this earnings event."""
        max_days_before = config.earnings.max_days_before_entry
        min_days_before = config.earnings.min_days_before_entry

        entry_open = self.report_date - timedelta(days=max_days_before)
        entry_close = self.report_date - timedelta(days=min_days_before)

        return entry_open, entry_close


class EarningsCalendar:
    """
    Manages the upcoming earnings calendar across the strategy universe.

    Core responsibilities:
      1. Fetch and validate upcoming earnings dates
      2. Score date confidence (only trade HIGH+ confidence)
      3. Compute trade entry/exit windows
      4. Alert on date changes (massive risk event)
      5. Track historical accuracy of each data source
    """

    def __init__(self, config):
        self.config = config
        self._events: Dict[str, EarningsEvent] = {}
        self._date_change_history: List[dict] = []
        self._source_accuracy: Dict[str, float] = {}

    async def refresh(self) -> List[EarningsEvent]:
        """
        Refresh the full earnings calendar for all universe tickers.
        Should be called once daily before market open.
        """
        all_events = []
        universe = (
            self.config.strategy.universe.get("liquid_large_cap", []) +
            self.config.strategy.universe.get("financials", []) +
            self.config.strategy.universe.get("etfs", [])
        )

        refresh_tasks = [self._fetch_event(ticker) for ticker in universe]
        results = await asyncio.gather(*refresh_tasks, return_exceptions=True)

        for ticker, result in zip(universe, results):
            if isinstance(result, Exception):
                log.error("calendar.fetch_failed", ticker=ticker, error=str(result))
                continue

            if result is None:
                continue

            # Check for date changes vs previous version
            if ticker in self._events:
                prev = self._events[ticker]
                if prev.report_date != result.report_date:
                    self._record_date_change(ticker, prev, result)

            self._events[ticker] = result

            if result.is_tradeable:
                all_events.append(result)

        log.info("calendar.refreshed",
                 total_tickers=len(universe),
                 tradeable_events=len(all_events),
                 events=[e.ticker for e in all_events])

        return all_events

    def get_upcoming_events(
        self,
        within_days: int = 5,
        min_confidence: float = 0.90,
    ) -> List[EarningsEvent]:
        """Get all upcoming earnings events within the trading window."""
        return [
            event for event in self._events.values()
            if (
                0 < event.days_to_earnings <= within_days
                and event.confidence_score >= min_confidence
                and event.timing != EarningsTiming.UNKNOWN
            )
        ]

    def get_event(self, ticker: str) -> Optional[EarningsEvent]:
        """Get the next earnings event for a specific ticker."""
        return self._events.get(ticker)

    def is_earnings_week(self, ticker: str) -> bool:
        """Is this ticker reporting earnings within 5 trading days?"""
        event = self._events.get(ticker)
        if not event:
            return False
        return 0 < event.days_to_earnings <= 5

    def validate_event_for_trading(
        self, ticker: str, config, risk_limits
    ) -> Tuple[bool, str]:
        """
        Full validation of whether an earnings event is safe to trade.
        Returns (approved, reason_if_rejected).
        """
        event = self._events.get(ticker)

        if not event:
            return False, f"No earnings event found for {ticker}"

        # ── Date confidence gate ─────────────────────────────────
        min_confidence = config.earnings.min_date_confidence
        if event.confidence_score < min_confidence:
            return False, (
                f"Date confidence too low: {event.confidence_score:.0%} < "
                f"{min_confidence:.0%} required. Never trade unconfirmed dates."
            )

        # ── Timing gate ──────────────────────────────────────────
        if event.timing == EarningsTiming.UNKNOWN:
            return False, "AMC vs BMO timing unknown — cannot size gap risk correctly"

        if event.timing == EarningsTiming.DMH:
            return False, "During-market-hours earnings not supported — unpredictable IV behavior"

        # ── Entry window gate ────────────────────────────────────
        today = date.today()
        entry_open, entry_close = event.compute_entry_window(config)

        if today < entry_open:
            return False, (
                f"Too early — entry window opens {entry_open}. "
                f"IV not at peak yet (still {event.days_to_earnings} days away)"
            )

        if today > entry_close:
            return False, (
                f"Too late — entry window closed {entry_close}. "
                f"IV may already be partially crushed or event already occurred."
            )

        # ── Historical data gate ─────────────────────────────────
        min_history = risk_limits.per_trade_risk.min_earnings_history_cycles
        history_available = 8  # Would come from IV crush database in production
        if history_available < min_history:
            return False, (
                f"Insufficient earnings history: {history_available} < "
                f"{min_history} required cycles. Cannot calibrate edge."
            )

        # ── Historical gap risk gate ──────────────────────────────
        # (populated from IV crush database in production)
        max_gap_rate = risk_limits.per_trade_risk.max_acceptable_gap_history_pct
        if event.median_historical_move_pct and event.expected_move_pct:
            historical_beyond_wings_rate = self._estimate_wing_breach_rate(event)
            if historical_beyond_wings_rate > max_gap_rate:
                return False, (
                    f"Historical gap risk too high: {historical_beyond_wings_rate:.0%} "
                    f"of prior earnings gapped beyond wings. Max allowed: {max_gap_rate:.0%}"
                )

        log.info("calendar.event_validated",
                 ticker=ticker,
                 report_date=str(event.report_date),
                 timing=event.timing.value,
                 confidence=event.confidence_score,
                 days_to_earnings=event.days_to_earnings)

        return True, "Approved"

    def _estimate_wing_breach_rate(self, event: EarningsEvent) -> float:
        """
        Estimate probability that the stock will gap beyond our wings.
        Uses historical move vs expected move calibration.
        """
        if not event.median_historical_move_pct or not event.expected_move_pct:
            return 0.0

        wing_level = event.expected_move_pct * \
            self.config.trade_structure.iron_condor.min_wing_multiplier

        # Rough estimate: if median historical move is > 70% of wing level,
        # there's meaningful risk of wing breach
        # In production: use actual distribution from historical database
        ratio = event.median_historical_move_pct / wing_level
        if ratio > 0.9:
            return 0.25   # High breach probability
        elif ratio > 0.7:
            return 0.15
        elif ratio > 0.5:
            return 0.08
        else:
            return 0.03

    def _record_date_change(self, ticker: str, old: EarningsEvent, new: EarningsEvent):
        """
        Record a date change — CRITICAL RISK EVENT.
        Any open position in this ticker must be reviewed immediately.
        """
        change = {
            "ticker": ticker,
            "old_date": str(old.report_date),
            "new_date": str(new.report_date),
            "old_timing": old.timing.value,
            "new_timing": new.timing.value,
            "detected_at": datetime.now().isoformat(),
        }
        self._date_change_history.append(change)

        log.critical(
            "calendar.DATE_CHANGE_DETECTED",
            **change,
            action_required="Review any open positions in this ticker IMMEDIATELY"
        )

    async def _fetch_event(self, ticker: str) -> Optional[EarningsEvent]:
        """
        Fetch earnings event from multiple sources and cross-validate.

        IMPLEMENTATION: Replace stubs with actual data source calls.
        Priority order:
          1. Broker API (most reliable)
          2. earningswhispers.com
          3. yfinance
        """
        # Fetch from multiple sources in parallel
        sources = await asyncio.gather(
            self._fetch_from_broker(ticker),
            self._fetch_from_earnings_whispers(ticker),
            self._fetch_from_yfinance(ticker),
            return_exceptions=True
        )

        valid_sources = [
            s for s in sources
            if not isinstance(s, Exception) and s is not None
        ]

        if not valid_sources:
            return None

        # Cross-validate dates across sources
        return self._reconcile_sources(ticker, valid_sources)

    def _reconcile_sources(
        self, ticker: str, sources: List[dict]
    ) -> Optional[EarningsEvent]:
        """
        Reconcile earnings data from multiple sources.
        Computes confidence score based on agreement.
        """
        if not sources:
            return None

        dates = [s.get("date") for s in sources if s.get("date")]
        if not dates:
            return None

        # Check if all sources agree
        unique_dates = set(str(d) for d in dates)
        timings = [s.get("timing", EarningsTiming.UNKNOWN) for s in sources]
        unique_timings = set(str(t) for t in timings)

        # Confidence scoring
        if len(unique_dates) == 1 and len(unique_timings) == 1:
            # Perfect agreement across all sources
            if len(sources) >= 3:
                confidence = 0.98
                confidence_level = DateConfidence.CONFIRMED
            elif len(sources) == 2:
                confidence = 0.92
                confidence_level = DateConfidence.HIGH
            else:
                confidence = 0.80
                confidence_level = DateConfidence.MEDIUM
        elif len(unique_dates) == 1:
            # Dates agree but timing differs — use mode timing
            confidence = 0.85
            confidence_level = DateConfidence.HIGH
        else:
            # Date disagreement — DO NOT TRADE
            confidence = 0.40
            confidence_level = DateConfidence.LOW
            log.warning("calendar.date_disagreement",
                        ticker=ticker, dates=list(unique_dates))

        # Use most common date
        from collections import Counter
        most_common_date = Counter(str(d) for d in dates).most_common(1)[0][0]
        report_date = date.fromisoformat(most_common_date)

        # Use most common timing
        timing_counts = Counter(str(t) for t in timings)
        most_common_timing_str = timing_counts.most_common(1)[0][0]

        try:
            timing = EarningsTiming(most_common_timing_str)
        except ValueError:
            timing = EarningsTiming.UNKNOWN

        event = EarningsEvent(
            ticker=ticker,
            report_date=report_date,
            timing=timing,
            confidence=confidence_level,
            confidence_score=confidence,
            data_sources=[s.get("source", "unknown") for s in sources],
            last_updated=datetime.now(),
        )

        # Set trade windows
        event.entry_open_date = report_date - timedelta(
            days=self.config.strategy.earnings.max_days_before_entry
        )
        event.entry_close_date = report_date - timedelta(
            days=self.config.strategy.earnings.min_days_before_entry
        )
        event.exit_date = (
            report_date if timing == EarningsTiming.BMO
            else report_date + timedelta(days=1)
        )

        return event

    # ──────────────────────────────────────────────────────────────
    # DATA SOURCE STUBS
    # ──────────────────────────────────────────────────────────────

    async def _fetch_from_broker(self, ticker: str) -> Optional[dict]:
        """
        Fetch earnings date from broker API.
        IMPLEMENTATION: Use your broker's calendar endpoint.
        IBKR: reqFundamentalData with 'CalendarReport'
        TastyTrade: /market-data/earnings endpoint
        """
        raise NotImplementedError(
            f"Implement broker earnings calendar fetch for {ticker}. "
            "IBKR: reqFundamentalData(). TastyTrade: /market-data/earnings"
        )

    async def _fetch_from_earnings_whispers(self, ticker: str) -> Optional[dict]:
        """
        Fetch from EarningsWhispers.com — community-verified dates + whisper numbers.
        IMPLEMENTATION: Use their API or scrape their calendar page.
        """
        raise NotImplementedError(
            f"Implement EarningsWhispers fetch for {ticker}. "
            "API docs: https://www.earningswhispers.com/api"
        )

    async def _fetch_from_yfinance(self, ticker: str) -> Optional[dict]:
        """
        Fetch earnings date from Yahoo Finance via yfinance.
        Less reliable for upcoming dates — use as tertiary source only.
        """
        try:
            import yfinance as yf
            ticker_obj = yf.Ticker(ticker)
            cal = ticker_obj.calendar
            if cal is None:
                return None

            earnings_date = cal.get("Earnings Date")
            if earnings_date is None:
                return None

            # yfinance doesn't reliably provide AMC/BMO
            return {
                "source": "yfinance",
                "date": earnings_date[0].date() if hasattr(earnings_date[0], 'date') else earnings_date[0],
                "timing": EarningsTiming.UNKNOWN,  # yfinance doesn't provide this reliably
            }
        except Exception as e:
            log.warning("calendar.yfinance_error", ticker=ticker, error=str(e))
            return None
