"""
Max Pain Calculator
====================
Calculates the "Max Pain" strike price — the level where the total
value of all expiring options is minimized — and derives a pin signal
from the distance and confidence of this calculation.

══════════════════════════════════════════════════════════════════
  MATHEMATICAL FOUNDATION
══════════════════════════════════════════════════════════════════

Max Pain Definition:
  For each potential closing price P, the total pain is:

  Total Pain(P) = Σᵢ [max(0, P - Kᵢ) × OI_call_i × 100]   (calls)
                + Σⱼ [max(0, Kⱼ - P) × OI_put_j × 100]    (puts)

  Max Pain = argmin_P [Total Pain(P)]

Weighted Max Pain (more accurate):
  Same formula but weighted by open interest AND current bid price:

  Weighted Pain(P) = Σᵢ [max(0, P - Kᵢ) × OI_call_i × bid_call_i]
                   + Σⱼ [max(0, Kⱼ - P) × OI_put_j × bid_put_j]

  This accounts for the fact that high-OI strikes at deep ITM
  don't represent as much dealer risk as near-the-money strikes.

Max Pain Confidence Score:
  The confidence of the pin signal depends on:
  1. Concentration of OI at/near max pain (higher = more confident)
  2. Distance from current price (closer = more confident)
  3. Historical pin accuracy for this ticker
  4. GEX alignment (are dealers positioned to support the pin?)
  5. Stability (has max pain moved significantly today?)

══════════════════════════════════════════════════════════════════
  PIN MECHANISM EXPLAINED
══════════════════════════════════════════════════════════════════

Why dealers push toward max pain:
  Dealers are typically SHORT options (they sell to retail).
  Being short options means they must delta-hedge.

  At any strike price K:
    Short call → dealer is LONG delta → SELLS stock as price rises
    Short call → dealer is LONG delta → BUYS stock as price falls

  This buying (on dips) and selling (on rallies) near the max
  pain strike creates a price gravity effect that PINS the stock.

  The larger the OI at the max pain strike, the stronger the
  gravitational pull because more dealers need to hedge.

  Near max pain (within 0.5%):
    Gamma of ATM options is at MAXIMUM
    Delta changes most rapidly per dollar move
    Dealers must trade MORE aggressively to hedge
    Self-reinforcing: their hedging further stabilizes price AT the strike

══════════════════════════════════════════════════════════════════
  WHEN MAX PAIN FAILS
══════════════════════════════════════════════════════════════════

Failure modes (your stops should catch these):
  1. NEWS EVENT: External shock overwhelms dealer hedging
  2. THIN OI: Low total OI means few dealers, weak pin effect
  3. TRENDING MARKET: Strong trend overcomes pin gravity
  4. NEGATIVE GEX: When dealers are SHORT gamma, their hedging
     AMPLIFIES moves instead of dampening them (opposite effect!)
  5. QUAD WITCHING CHAOS: Sometimes competing forces cancel out

These failures are detected by:
  - Circuit breaker (VIX, price move monitors)
  - GEX signal (negative GEX → abort)
  - Max pain shift detection (recalculate every 15 min)
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Tuple
import numpy as np
import structlog

log = structlog.get_logger(__name__)


@dataclass
class StrikeOI:
    """Open interest data at a single strike price."""
    strike: float
    call_oi: int
    put_oi: int
    call_bid: float
    put_bid: float
    call_iv: float
    put_iv: float
    call_delta: float
    put_delta: float
    call_gamma: float
    put_gamma: float


@dataclass
class MaxPainResult:
    """Result of max pain calculation for a single expiry."""
    ticker: str
    expiry: str
    calculation_time: datetime
    underlying_price: float

    # Core max pain levels
    max_pain_strike: float               # The calculated max pain level
    weighted_max_pain_strike: float      # OI + bid weighted version

    # Pain landscape
    total_pain_at_max_pain: float        # Dollar value of pain at max pain
    pain_curve: Dict[float, float]       # Strike → total pain (for analysis)

    # OI concentration
    total_call_oi: int
    total_put_oi: int
    oi_at_max_pain_strike: int           # Combined OI at max pain level
    oi_concentration_score: float        # % of total OI within 1% of max pain

    # Distance and direction
    distance_from_current_pct: float     # |current - max_pain| / current
    direction: str                       # "bullish" | "bearish" | "neutral"
    price_must_move: str                 # "up" | "down" | "flat"

    # Nearby competing pins
    secondary_max_pain: Optional[float] = None    # Second-strongest level
    bimodal_distribution: bool = False            # Multiple competing levels (weak signal)

    # Historical context
    historical_pin_accuracy: Optional[float] = None  # This ticker's pin rate

    @property
    def pin_strength(self) -> str:
        """Qualitative pin strength based on distance."""
        d = self.distance_from_current_pct
        if d <= 0.005:
            return "very_strong"    # Within 0.5% — strongest pin
        elif d <= 0.010:
            return "strong"
        elif d <= 0.020:
            return "moderate"
        elif d <= 0.030:
            return "weak"
        else:
            return "none"           # > 3% — no meaningful pin signal

    @property
    def is_bimodal(self) -> bool:
        """True if there are two competing max pain levels — ambiguous signal."""
        return self.bimodal_distribution


@dataclass
class MaxPainSignal:
    """Final signal output from max pain analysis."""
    score: float                    # [-1, +1] — positive = bullish pin
    direction: str                  # "bullish" | "bearish" | "neutral"
    confidence: float               # [0, 1]
    max_pain_level: float
    current_price: float
    distance_pct: float
    pin_strength: str

    # Trade construction levels
    target_price: float             # Where we expect price to pin
    call_short_strike: float        # For risk reversal: sell this put (mispriced)
    put_short_strike: float         # For risk reversal: buy this call
    condor_upper_wing: float        # For condor: upper short strike
    condor_lower_wing: float        # For condor: lower short strike

    oi_concentration_score: float
    bimodal_warning: bool

    timestamp: datetime = field(default_factory=datetime.now)
    diagnostics: dict = field(default_factory=dict)


class MaxPainCalculator:
    """
    Real-time Max Pain Calculator with pin signal generation.

    Core responsibilities:
      1. Fetch options chain OI for all strikes at expiry
      2. Calculate standard and weighted max pain
      3. Assess pin signal quality (strength, confidence)
      4. Track max pain stability (detect shifts)
      5. Output trade-ready pin signal
    """

    def __init__(self, config, underlying: str):
        self.config = config.signals.max_pain
        self.underlying = underlying

        self._last_result: Optional[MaxPainResult] = None
        self._result_history: List[MaxPainResult] = []
        self._pin_accuracy_cache: Dict[str, float] = {}

    async def calculate(
        self,
        expiry: str,
        current_price: float,
        historical_pin_accuracy: Optional[float] = None,
    ) -> Optional[MaxPainSignal]:
        """
        Full max pain calculation and signal generation.

        Args:
            expiry: Expiry date string (YYYY-MM-DD)
            current_price: Current underlying price
            historical_pin_accuracy: From pin history database

        Returns:
            MaxPainSignal if conditions are met, None otherwise
        """
        # Fetch full options chain OI
        chain = await self._fetch_options_chain(expiry)
        if not chain:
            return None

        if len(chain) < 5:
            log.warning("max_pain.insufficient_strikes",
                        ticker=self.underlying, strikes=len(chain))
            return None

        # Calculate max pain
        result = self._calculate_max_pain(
            chain=chain,
            current_price=current_price,
            expiry=expiry,
        )
        result.historical_pin_accuracy = historical_pin_accuracy

        # Check for max pain shifts
        if self._last_result:
            self._check_for_max_pain_shift(result)

        self._last_result = result
        self._result_history.append(result)

        # Validate signal quality
        if not self._validate_signal_quality(result):
            return None

        return self._generate_signal(result)

    def _calculate_max_pain(
        self,
        chain: List[StrikeOI],
        current_price: float,
        expiry: str,
    ) -> MaxPainResult:
        """
        Calculate standard and weighted max pain from options chain OI.
        """
        strikes = [s.strike for s in chain]
        pain_curve: Dict[float, float] = {}
        weighted_pain_curve: Dict[float, float] = {}

        total_call_oi = sum(s.call_oi for s in chain)
        total_put_oi = sum(s.put_oi for s in chain)

        # ── Standard Max Pain ────────────────────────────────────
        # For each potential closing price, compute total options pain
        for test_price in strikes:
            total_pain = 0.0
            for strike_data in chain:
                k = strike_data.strike
                # Call pain: calls are ITM if price > strike
                if test_price > k:
                    total_pain += (test_price - k) * strike_data.call_oi * 100
                # Put pain: puts are ITM if price < strike
                if test_price < k:
                    total_pain += (k - test_price) * strike_data.put_oi * 100
            pain_curve[test_price] = total_pain

        # ── Weighted Max Pain ────────────────────────────────────
        # Weight by bid price × OI (accounts for dollar value of exposure)
        for test_price in strikes:
            weighted_pain = 0.0
            for sd in chain:
                k = sd.strike
                if test_price > k:
                    # Weight by the bid price of the call (dollar exposure)
                    call_weight = max(sd.call_bid, 0.01)
                    weighted_pain += (test_price - k) * sd.call_oi * call_weight
                if test_price < k:
                    put_weight = max(sd.put_bid, 0.01)
                    weighted_pain += (k - test_price) * sd.put_oi * put_weight
            weighted_pain_curve[test_price] = weighted_pain

        # Find minimum pain levels
        max_pain_strike = min(pain_curve, key=pain_curve.get)
        weighted_max_pain = min(weighted_pain_curve, key=weighted_pain_curve.get)

        # ── OI Concentration ─────────────────────────────────────
        oi_at_max_pain = 0
        for sd in chain:
            if abs(sd.strike - max_pain_strike) < 1.0:  # Within $1
                oi_at_max_pain += sd.call_oi + sd.put_oi

        total_oi = total_call_oi + total_put_oi
        oi_within_1pct = sum(
            s.call_oi + s.put_oi for s in chain
            if abs(s.strike - max_pain_strike) / max_pain_strike <= 0.01
        )
        concentration_score = oi_within_1pct / total_oi if total_oi > 0 else 0.0

        # ── Bimodal Detection ─────────────────────────────────────
        # If two strikes have similar pain levels, signal is ambiguous
        sorted_pain = sorted(pain_curve.items(), key=lambda x: x[1])
        bimodal = False
        if len(sorted_pain) >= 2:
            second_lowest = sorted_pain[1][1]
            lowest = sorted_pain[0][1]
            # If second min is within 10% of minimum → bimodal (two competing levels)
            if lowest > 0 and (second_lowest - lowest) / lowest < 0.10:
                bimodal = True
                secondary_max_pain = sorted_pain[1][0]
                log.debug("max_pain.bimodal_detected",
                          primary=max_pain_strike,
                          secondary=secondary_max_pain,
                          ticker=self.underlying)
            else:
                secondary_max_pain = sorted_pain[1][0]

        # ── Direction ─────────────────────────────────────────────
        distance_pct = (max_pain_strike - current_price) / current_price
        if distance_pct > 0.002:         # Max pain > 0.2% above current → bullish
            direction = "bullish"
            price_must_move = "up"
        elif distance_pct < -0.002:       # Max pain > 0.2% below current → bearish
            direction = "bearish"
            price_must_move = "down"
        else:
            direction = "neutral"
            price_must_move = "flat"

        return MaxPainResult(
            ticker=self.underlying,
            expiry=expiry,
            calculation_time=datetime.now(),
            underlying_price=current_price,
            max_pain_strike=max_pain_strike,
            weighted_max_pain_strike=weighted_max_pain,
            total_pain_at_max_pain=pain_curve[max_pain_strike],
            pain_curve=pain_curve,
            total_call_oi=total_call_oi,
            total_put_oi=total_put_oi,
            oi_at_max_pain_strike=oi_at_max_pain,
            oi_concentration_score=concentration_score,
            distance_from_current_pct=abs(distance_pct),
            direction=direction,
            price_must_move=price_must_move,
            secondary_max_pain=secondary_max_pain if bimodal else None,
            bimodal_distribution=bimodal,
        )

    def _generate_signal(self, result: MaxPainResult) -> MaxPainSignal:
        """Convert MaxPainResult into a tradeable MaxPainSignal."""
        # ── Base score from distance ──────────────────────────────
        # Closer to max pain = higher score (easier to pin)
        dist = result.distance_from_current_pct
        if dist <= 0.005:
            distance_score = 1.0
        elif dist <= 0.010:
            distance_score = 0.85
        elif dist <= 0.020:
            distance_score = 0.65
        elif dist <= 0.030:
            distance_score = 0.45
        else:
            distance_score = 0.0

        # ── OI concentration boosts signal ────────────────────────
        oi_boost = min(0.15, result.oi_concentration_score * 0.30)

        # ── Historical accuracy adjustment ────────────────────────
        if result.historical_pin_accuracy:
            hist_factor = result.historical_pin_accuracy / 0.65  # Normalize to expected
            hist_factor = float(np.clip(hist_factor, 0.5, 1.3))
        else:
            hist_factor = 0.85  # Slight penalty when no history

        # ── Bimodal penalty ───────────────────────────────────────
        bimodal_penalty = 0.20 if result.bimodal_distribution else 0.0

        # ── Raw score ─────────────────────────────────────────────
        raw_score = (distance_score + oi_boost - bimodal_penalty) * hist_factor

        # Apply direction
        if result.direction == "bullish":
            final_score = raw_score
        elif result.direction == "bearish":
            final_score = -raw_score
        else:
            final_score = 0.0     # Neutral: use condor structure

        # ── Confidence ────────────────────────────────────────────
        confidence_factors = [
            distance_score,
            result.oi_concentration_score,
            hist_factor * 0.8,
            0.0 if result.bimodal_distribution else 0.8,
        ]
        confidence = float(np.clip(np.mean(confidence_factors), 0.0, 1.0))

        # ── Strike levels for trade construction ──────────────────
        price = result.underlying_price
        max_pain = result.max_pain_strike
        config = self.config

        # Condor wings: sell at small offset from max pain, buy protection
        spread_width = price * 0.008   # 0.8% spread width
        condor_upper = self._round_strike(max_pain + spread_width)
        condor_lower = self._round_strike(max_pain - spread_width)

        # Risk reversal strikes: 25-delta approximation (0.5% OTM per 1% vol)
        # Simplified: put at max_pain - 1.5×spread, call at max_pain
        put_short = self._round_strike(max_pain - spread_width * 1.5)
        call_short = self._round_strike(max_pain)

        return MaxPainSignal(
            score=float(np.clip(final_score, -1.0, 1.0)),
            direction=result.direction,
            confidence=confidence,
            max_pain_level=result.max_pain_strike,
            current_price=result.underlying_price,
            distance_pct=result.distance_from_current_pct,
            pin_strength=result.pin_strength,
            target_price=result.max_pain_strike,
            call_short_strike=call_short,
            put_short_strike=put_short,
            condor_upper_wing=condor_upper,
            condor_lower_wing=condor_lower,
            oi_concentration_score=result.oi_concentration_score,
            bimodal_warning=result.bimodal_distribution,
            diagnostics={
                "ticker": self.underlying,
                "expiry": result.expiry,
                "max_pain": result.max_pain_strike,
                "weighted_max_pain": result.weighted_max_pain_strike,
                "distance_pct": result.distance_from_current_pct,
                "total_oi": result.total_call_oi + result.total_put_oi,
                "oi_concentration": result.oi_concentration_score,
                "bimodal": result.bimodal_distribution,
                "pin_strength": result.pin_strength,
                "hist_pin_accuracy": result.historical_pin_accuracy,
            }
        )

    def _check_for_max_pain_shift(self, new_result: MaxPainResult):
        """
        Monitor max pain for significant shifts during the trading day.
        A shift > 1% means new OI has come in and invalidates the trade thesis.
        """
        if not self._last_result:
            return

        shift_pct = abs(
            new_result.max_pain_strike - self._last_result.max_pain_strike
        ) / self._last_result.max_pain_strike

        if shift_pct >= self.config.max_pain_shift_abort_pct:
            log.critical("max_pain.CRITICAL_SHIFT",
                         ticker=self.underlying,
                         old_max_pain=self._last_result.max_pain_strike,
                         new_max_pain=new_result.max_pain_strike,
                         shift_pct=f"{shift_pct:.3%}",
                         action="REVIEW ALL OPEN POSITIONS IN THIS TICKER")

        elif shift_pct >= self.config.max_pain_shift_alert_pct:
            log.warning("max_pain.significant_shift",
                        ticker=self.underlying,
                        old=self._last_result.max_pain_strike,
                        new=new_result.max_pain_strike,
                        shift_pct=f"{shift_pct:.3%}")

    def _validate_signal_quality(self, result: MaxPainResult) -> bool:
        """Pre-validate result before generating signal."""
        # Reject if too far from max pain
        if result.distance_from_current_pct > self.config.max_pain_distance_entry_pct:
            log.debug("max_pain.too_far_from_max_pain",
                      ticker=self.underlying,
                      distance=result.distance_from_current_pct,
                      max_allowed=self.config.max_pain_distance_entry_pct)
            return False

        # Reject if insufficient OI at max pain
        if result.oi_at_max_pain_strike < self.config.min_oi_per_strike:
            log.debug("max_pain.insufficient_oi",
                      ticker=self.underlying,
                      oi=result.oi_at_max_pain_strike)
            return False

        # Warn but don't reject on bimodal (signal will have lower score)
        if result.bimodal_distribution:
            log.warning("max_pain.bimodal_signal",
                        ticker=self.underlying,
                        primary=result.max_pain_strike,
                        secondary=result.secondary_max_pain)

        return True

    def get_current_max_pain(self) -> Optional[float]:
        """Return the most recently calculated max pain level."""
        return self._last_result.max_pain_strike if self._last_result else None

    def has_max_pain_shifted_critically(self) -> bool:
        """Check if max pain has shifted critically since last calculation."""
        if len(self._result_history) < 2:
            return False
        latest = self._result_history[-1]
        previous = self._result_history[-2]
        shift = abs(latest.max_pain_strike - previous.max_pain_strike)
        return shift / previous.max_pain_strike >= self.config.max_pain_shift_abort_pct

    @staticmethod
    def _round_strike(price: float) -> float:
        """Round to nearest valid options strike."""
        if price >= 1000:
            return round(price / 5) * 5
        elif price >= 200:
            return round(price / 2.5) * 2.5 if round(price / 2.5) * 2.5 > 0 else round(price)
        elif price >= 50:
            return round(price)
        else:
            return round(price / 0.50) * 0.50

    async def _fetch_options_chain(self, expiry: str) -> Optional[List[StrikeOI]]:
        """
        Fetch full options chain open interest for an expiry.

        IMPLEMENTATION: Replace with live data feed.
        Required data per strike: call_oi, put_oi, call_bid, put_bid,
                                  call_iv, put_iv, call_delta, put_delta

        Sources:
          - IBKR: reqContractDetails + reqMktData for OI
          - TastyTrade: /option-chains/{symbol}?expiration={date}
          - Tradier: /markets/options/chains?symbol={ticker}&expiration={date}
          - Polygon.io: /v3/snapshot/options/{underlyingAsset}
          - Yahoo Finance: yf.Ticker(ticker).option_chain(date)
        """
        raise NotImplementedError(
            f"Implement _fetch_options_chain for {self.underlying}. \n"
            "Required: full OI data at all strikes for the target expiry. \n"
            "This is the CRITICAL data dependency for max pain calculation. \n"
            "Use Tradier or Polygon for most reliable OI data."
        )
