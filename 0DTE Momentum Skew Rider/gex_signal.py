"""
Gamma Exposure (GEX) Signal
===========================
Core alpha source #2: Dealer gamma positioning as a directional predictor.

The key insight:
  GEX > 0 (Dealers long gamma):
    - Dealers SELL into rallies and BUY dips to delta-hedge
    - Effect: Price tends to REVERT to major strike prices (pin)
    - Strategy: Sell straddles / iron condors near GEX-weighted strikes

  GEX < 0 (Dealers short gamma):
    - Dealers BUY into rallies and SELL into dips to delta-hedge
    - Effect: Price AMPLIFIES moves (dealers add fuel to the fire)
    - Strategy: Buy straddles / debit spreads in the trend direction

GEX Flip Zone:
    - The strike price where GEX crosses zero is critical
    - Price crossing above GEX flip = explosive upside (dealers buying)
    - Price crossing below GEX flip = explosive downside (dealers selling)
    - BEST 0DTE setup: Enter direction trade as price crosses flip zone

Reference: SpotGamma research, Brent Kochuba's GEX framework
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import numpy as np
import structlog

log = structlog.get_logger(__name__)


@dataclass
class StrikeGEX:
    """GEX at a specific strike."""
    strike: float
    calls_oi: int
    puts_oi: int
    calls_gamma: float    # Per-share gamma
    puts_gamma: float     # Per-share gamma
    call_gex: float       # Net GEX contribution (calls)
    put_gex: float        # Net GEX contribution (puts)
    net_gex: float        # Total GEX at this strike
    underlying_price: float


@dataclass
class GEXProfile:
    """Full GEX landscape for the current session."""
    timestamp: datetime
    underlying_price: float
    total_gex: float           # Total market GEX in $
    gex_flip_level: float      # Strike where GEX = 0
    largest_positive_strike: float  # Biggest positive GEX magnet (pin)
    largest_negative_strike: float  # Biggest negative GEX accelerant
    gex_by_strike: List[StrikeGEX]

    @property
    def is_positive_gex(self) -> bool:
        return self.total_gex > 0

    @property
    def distance_to_flip_bps(self) -> float:
        """Distance from current price to GEX flip in basis points."""
        return abs(self.underlying_price - self.gex_flip_level) / \
               self.underlying_price * 10000


@dataclass
class GEXSignal:
    """Signal output from GEX analysis."""
    score: float             # Normalized [-1, +1]
    regime: str              # "pinning", "trending", "explosive"
    direction_bias: str      # "bullish", "bearish", "neutral"
    confidence: float        # [0, 1]
    gex_total: float
    gex_flip: float
    distance_to_flip_bps: float
    nearest_pin_strike: float
    diagnostics: dict


class GEXSignalEngine:
    """
    Gamma Exposure Signal Generator.

    Calculates dealer gamma positioning to determine:
    1. Whether market is in pinning or trending/explosive regime
    2. Key strike levels acting as magnets or repellers
    3. Directional bias when price is near GEX flip zone
    """

    def __init__(self, config, underlying: str):
        self.config = config
        self.underlying = underlying
        self._last_profile: Optional[GEXProfile] = None
        self._profile_history: List[GEXProfile] = []

    async def get_signal(self) -> Optional[GEXSignal]:
        """Generate current GEX signal."""
        profile = await self._fetch_gex_profile()
        if profile is None:
            return None

        # Track history for velocity calculations
        if self._last_profile:
            self._profile_history.append(self._last_profile)
            if len(self._profile_history) > 20:
                self._profile_history.pop(0)

        self._last_profile = profile
        return self._calculate_signal(profile)

    def _calculate_signal(self, profile: GEXProfile) -> GEXSignal:
        """Core GEX signal logic."""
        limits = self.config  # gex config section

        # ── Regime Classification ────────────────────────────────
        if profile.total_gex >= limits.positive_gex_threshold:
            regime = "pinning"
        elif profile.total_gex <= limits.negative_gex_threshold:
            regime = "explosive"
        else:
            regime = "trending"

        # ── Directional Bias ─────────────────────────────────────
        price = profile.underlying_price
        flip = profile.gex_flip_level
        dist_bps = profile.distance_to_flip_bps

        # Near the GEX flip zone (within threshold): directional setup
        flip_zone_bps = limits.gex_flip_zone_bps

        if dist_bps <= flip_zone_bps:
            # Price is AT the flip — explosive move brewing
            if price > flip:
                direction = "bullish"   # Above flip: dealers will buy
                raw_score = 0.85
            else:
                direction = "bearish"   # Below flip: dealers will sell
                raw_score = -0.85
        elif regime == "pinning":
            # Price will gravitate toward nearest pin strike
            direction = "neutral"
            raw_score = 0.0  # No strong directional edge in pinning
        elif regime == "explosive":
            # No gamma cushion — follow momentum
            direction = "neutral"
            raw_score = 0.0  # Signal comes from momentum, not GEX alone
        else:
            # Trending regime
            if profile.total_gex > 0:
                direction = "neutral"   # Mild suppression, no strong bias
                raw_score = 0.2
            else:
                direction = "neutral"
                raw_score = -0.2

        # ── GEX Change Velocity ──────────────────────────────────
        # Rapidly falling GEX = increasing volatility risk
        if len(self._profile_history) >= 3:
            gex_values = [p.total_gex for p in self._profile_history[-5:]]
            gex_velocity = (gex_values[-1] - gex_values[0]) / len(gex_values)
            # Negative velocity (GEX falling) amplifies the signal
            if gex_velocity < -1e8:  # GEX falling fast
                raw_score *= 1.2
                log.debug("gex.velocity_amplifying",
                          gex_velocity=gex_velocity)

        # ── Confidence ───────────────────────────────────────────
        # Higher confidence when:
        # 1. GEX is extreme (strong signal)
        # 2. Price is very close to flip zone
        # 3. GEX has been stable (not rapidly changing)

        gex_magnitude_factor = min(
            abs(profile.total_gex) / 1e9, 1.0  # Normalize to $1B scale
        )
        proximity_factor = max(0, 1 - (dist_bps / 50))  # 50bps = zero proximity
        confidence = float(np.clip(
            (gex_magnitude_factor * 0.6 + proximity_factor * 0.4),
            0.0, 1.0
        ))

        # ── Nearest Pin Strike (for condor placement) ────────────
        nearest_pin = self._find_nearest_large_positive_strike(
            profile, price
        )

        signal = GEXSignal(
            score=float(np.clip(raw_score, -1.0, 1.0)),
            regime=regime,
            direction_bias=direction,
            confidence=confidence,
            gex_total=profile.total_gex,
            gex_flip=flip,
            distance_to_flip_bps=dist_bps,
            nearest_pin_strike=nearest_pin,
            diagnostics={
                "underlying_price": price,
                "total_gex_billions": profile.total_gex / 1e9,
                "largest_pin": profile.largest_positive_strike,
                "largest_accel": profile.largest_negative_strike,
                "regime": regime,
            }
        )

        log.debug("gex.signal_generated",
                  regime=regime, score=signal.score,
                  gex_billions=profile.total_gex / 1e9,
                  dist_to_flip_bps=dist_bps)

        return signal

    def _find_nearest_large_positive_strike(
        self, profile: GEXProfile, current_price: float
    ) -> float:
        """Find the nearest strike with large positive GEX (pin magnet)."""
        positive_strikes = [
            s for s in profile.gex_by_strike
            if s.net_gex > 1e8  # > $100M positive GEX
        ]

        if not positive_strikes:
            return current_price  # No meaningful pin

        return min(
            positive_strikes,
            key=lambda s: abs(s.strike - current_price)
        ).strike

    def get_key_levels(self) -> Dict[str, float]:
        """
        Returns key GEX levels for strike selection.
        Used by trade_proposal builder to place spreads.
        """
        if not self._last_profile:
            return {}

        return {
            "gex_flip": self._last_profile.gex_flip_level,
            "largest_pin": self._last_profile.largest_positive_strike,
            "largest_accel": self._last_profile.largest_negative_strike,
            "underlying": self._last_profile.underlying_price,
        }

    def get_current_regime(self) -> str:
        """Return current market regime based on GEX."""
        if not self._last_profile:
            return "unknown"

        if self._last_profile.total_gex >= self.config.positive_gex_threshold:
            return "pinning"
        elif self._last_profile.total_gex <= self.config.negative_gex_threshold:
            return "explosive"
        else:
            return "trending"

    @staticmethod
    def calculate_strike_gex(
        strike: float,
        call_oi: int,
        put_oi: int,
        call_gamma: float,
        put_gamma: float,
        underlying_price: float,
        multiplier: int = 100,
    ) -> StrikeGEX:
        """
        Calculate GEX for a single strike.

        Formula: GEX = OI × Gamma × Underlying² × Multiplier
        Calls contribute positive GEX (dealers long gamma)
        Puts contribute negative GEX (dealers short gamma from put selling)

        Note: This assumes dealers are short the puts and long the calls
        (typical for market makers). Real GEX requires flow data to
        determine actual dealer positioning.
        """
        call_gex = call_oi * call_gamma * underlying_price ** 2 * multiplier
        put_gex = -put_oi * put_gamma * underlying_price ** 2 * multiplier  # Negative

        return StrikeGEX(
            strike=strike,
            calls_oi=call_oi,
            puts_oi=put_oi,
            calls_gamma=call_gamma,
            puts_gamma=put_gamma,
            call_gex=call_gex,
            put_gex=put_gex,
            net_gex=call_gex + put_gex,
            underlying_price=underlying_price,
        )

    async def _fetch_gex_profile(self) -> Optional[GEXProfile]:
        """
        Fetch full options chain and compute GEX profile.

        IMPLEMENTATION NOTE: Replace with your data source.
        SpotGamma provides professional GEX data.
        Can also compute from raw options chain (IBKR, TastyTrade, Polygon).
        """
        raise NotImplementedError(
            "Implement _fetch_gex_profile with your options chain data. "
            "For open-source: fetch chain from your broker, "
            "compute gammas via py_vollib, sum GEX by strike."
        )
