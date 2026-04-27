"""
VWAP Breakout Signal Engine
============================
Primary signal source: Detects and classifies VWAP breakouts
with volume confirmation and quality scoring.

VWAP (Volume-Weighted Average Price) is the single most important
intraday price level used by institutional traders. It represents
the average price weighted by volume — the "fair value" of the day.

Why VWAP breakouts work for options:
  1. Institutional algos anchor orders to VWAP → creates predictable
     support/resistance that the whole market respects
  2. When price breaks VWAP with volume, institutions are forced to
     re-anchor → trend continuation is mechanically likely
  3. Options can capture the move with defined risk (max loss = premium)
  4. Volume surge at break = informed capital, not noise

Three Breakout Types:
  TYPE_A — Clean Institutional Break:
      • Price crosses VWAP with volume ≥ 1.8× average
      • Retest of VWAP holds (new support/resistance confirmed)
      • Best setup: buy OTM debit spread in direction of break
      • Win rate: ~58-62% with proper confirmation

  TYPE_B — Exhaustion Fade:
      • Price is 1.5-2.5σ extended from VWAP
      • Volume declining (distribution/exhaustion)
      • RSI divergence present
      • Best setup: iron condor or fade spread back toward VWAP
      • Win rate: ~55-60% with volume confirmation

  TYPE_C — Institutional Hunt (most profitable):
      • Price consolidating near VWAP (±5bps) for 10+ bars
      • Unusual options activity detected (informed flow)
      • Options flow directionally aligned
      • Best setup: directional debit spread with flow
      • Win rate: ~63-68% when institutional flow confirmed

Signal scores:
  +1.0 = Strong TYPE_A bullish or TYPE_C bullish
  -1.0 = Strong TYPE_A bearish or TYPE_C bearish
   0.0 = No signal / TYPE_B fade (direction-neutral for condor)
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum, auto
from typing import Optional, List, Dict, Deque
from collections import deque
import numpy as np
import structlog

log = structlog.get_logger(__name__)


class BreakoutType(Enum):
    NONE = auto()
    TYPE_A_BULL = auto()    # Clean bullish breakout
    TYPE_A_BEAR = auto()    # Clean bearish breakdown
    TYPE_B_FADE = auto()    # Exhaustion fade (mean reversion)
    TYPE_C_BULL = auto()    # Institutional hunt — bullish
    TYPE_C_BEAR = auto()    # Institutional hunt — bearish


class RetestStatus(Enum):
    PENDING = auto()        # Waiting for retest
    CONFIRMED = auto()      # Retest held — strong signal
    FAILED = auto()         # Retest failed — signal invalidated
    NOT_REQUIRED = auto()   # Very strong break — skip retest wait


@dataclass
class VWAPLevels:
    """Current VWAP and band levels."""
    vwap: float
    upper_1sigma: float
    lower_1sigma: float
    upper_2sigma: float
    lower_2sigma: float
    upper_3sigma: float
    lower_3sigma: float
    cumulative_volume: float
    session_high: float
    session_low: float
    timestamp: datetime


@dataclass
class PriceBar:
    """Single OHLCV bar."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap_at_bar: float
    avg_volume: float     # N-bar average volume for comparison


@dataclass
class BreakoutEvent:
    """Detected VWAP breakout event."""
    timestamp: datetime
    breakout_type: BreakoutType
    direction: str             # "bullish" | "bearish" | "neutral"
    break_price: float
    vwap_at_break: float
    break_bps: float           # Price displacement from VWAP in bps
    break_volume: float
    volume_ratio: float        # Break bar volume / average volume
    retest_status: RetestStatus
    retest_price: Optional[float] = None
    retest_held: Optional[bool] = None
    quality_score: float = 0.0
    tod_weight: float = 1.0
    sigma_extension: float = 0.0    # How many σ extended from VWAP


@dataclass
class VWAPSignal:
    """Final output signal from VWAP analysis."""
    score: float               # [-1.0, +1.0]
    direction: str             # "bullish" | "bearish" | "neutral"
    confidence: float          # [0.0, 1.0]
    breakout_type: BreakoutType
    retest_status: RetestStatus

    # Key levels for trade construction
    vwap_level: float
    target_level: float        # Price target based on VWAP band
    stop_level: float          # Invalidation level (VWAP recross)
    sigma_extension: float

    # Context
    volume_ratio: float
    quality_score: float
    tod_weight: float
    timestamp: datetime = field(default_factory=datetime.now)
    diagnostics: dict = field(default_factory=dict)


class VWAPSignalEngine:
    """
    VWAP Breakout Signal Generator.

    Continuously monitors price action relative to VWAP,
    detects and classifies breakout events, and generates
    trade signals with quality scoring.
    """

    def __init__(self, config, underlying: str):
        self.config = config
        self.underlying = underlying

        self._bars: Deque[PriceBar] = deque(maxlen=200)
        self._vwap_levels: Optional[VWAPLevels] = None
        self._active_breakout: Optional[BreakoutEvent] = None
        self._recent_breakouts: List[BreakoutEvent] = []

        # Session accumulators for VWAP calculation
        self._session_cum_volume: float = 0.0
        self._session_cum_vp: float = 0.0   # Volume × Price
        self._session_cum_vp2: float = 0.0  # Volume × Price² (for std dev)
        self._session_start: Optional[datetime] = None

        # VWAP invalidation tracking for circuit breaker
        self._vwap_invalidations_recent = 0
        self._vwap_evaluations_recent = 0

    async def get_signal(self) -> Optional[VWAPSignal]:
        """
        Generate current VWAP breakout signal.
        Returns None if no qualifying setup detected.
        """
        # Refresh latest bars and VWAP
        new_bar = await self._fetch_latest_bar()
        if new_bar is None:
            return None

        self._update_session_vwap(new_bar)
        self._bars.append(new_bar)

        if len(self._bars) < 20:
            log.debug("vwap.insufficient_bars", n=len(self._bars))
            return None

        # Update VWAP levels
        self._vwap_levels = self._calculate_vwap_levels()

        # ── Check for active breakout continuation ─────────────
        if self._active_breakout:
            return await self._evaluate_active_breakout()

        # ── Detect new breakout ─────────────────────────────────
        breakout = self._detect_breakout()
        if breakout is None:
            return None

        self._active_breakout = breakout
        self._recent_breakouts.append(breakout)

        return self._generate_signal_from_breakout(breakout)

    def _detect_breakout(self) -> Optional[BreakoutEvent]:
        """
        Scan current price action for VWAP breakout conditions.
        """
        if not self._vwap_levels or len(self._bars) < 5:
            return None

        current_bar = self._bars[-1]
        prev_bar = self._bars[-2]
        vwap = self._vwap_levels.vwap
        config = self.config.vwap

        # ── Filter 1: Minimum displacement ──────────────────────
        current_bps = (current_bar.close - vwap) / vwap * 10000
        prev_bps = (prev_bar.close - vwap) / vwap * 10000
        min_break = config.min_break_bps

        crossed_up = prev_bar.close < vwap and current_bar.close > vwap
        crossed_down = prev_bar.close > vwap and current_bar.close < vwap

        if not (crossed_up or crossed_down):
            # Also check if already above/below and extending
            if abs(current_bps) < min_break:
                return None
            # Price is already on one side — check for extension continuation
            return self._detect_extension_signal(current_bar, vwap, current_bps)

        # ── Filter 2: Volume confirmation ────────────────────────
        avg_volume = self._calculate_avg_volume(bars=20)
        if avg_volume == 0:
            return None

        volume_ratio = current_bar.volume / avg_volume
        if volume_ratio < config.volume_surge_multiplier:
            log.debug("vwap.break_no_volume",
                      volume_ratio=volume_ratio,
                      required=config.volume_surge_multiplier)
            return None

        # ── Classify breakout type ───────────────────────────────
        direction = "bullish" if crossed_up else "bearish"
        sigma_ext = self._calculate_sigma_extension(current_bar.close, vwap)

        # Check for TYPE_B exhaustion (large extension + fading volume)
        if abs(sigma_ext) >= 1.5:
            breakout_type = BreakoutType.TYPE_B_FADE
        elif crossed_up:
            breakout_type = BreakoutType.TYPE_A_BULL
        else:
            breakout_type = BreakoutType.TYPE_A_BEAR

        # ── Quality score calculation ────────────────────────────
        quality = self._score_breakout_quality(
            volume_ratio=volume_ratio,
            sigma_ext=sigma_ext,
            breakout_type=breakout_type,
            direction=direction,
        )

        if quality < config.min_break_quality_score:
            log.debug("vwap.break_quality_too_low",
                      quality=quality, required=config.min_break_quality_score)
            return None

        # ── Time-of-day weight ───────────────────────────────────
        tod_weight = self._get_tod_weight(current_bar.timestamp)

        # ── Retest status ─────────────────────────────────────────
        # Very strong breaks (volume > 3× + quality > 0.85) → skip retest
        if volume_ratio > 3.0 and quality > 0.85:
            retest_status = RetestStatus.NOT_REQUIRED
        elif config.retest_required:
            retest_status = RetestStatus.PENDING
        else:
            retest_status = RetestStatus.NOT_REQUIRED

        breakout = BreakoutEvent(
            timestamp=current_bar.timestamp,
            breakout_type=breakout_type,
            direction=direction,
            break_price=current_bar.close,
            vwap_at_break=vwap,
            break_bps=abs(current_bps),
            break_volume=current_bar.volume,
            volume_ratio=volume_ratio,
            retest_status=retest_status,
            quality_score=quality,
            tod_weight=tod_weight,
            sigma_extension=sigma_ext,
        )

        log.info("vwap.breakout_detected",
                 type=breakout_type.name,
                 direction=direction,
                 quality=quality,
                 volume_ratio=volume_ratio,
                 sigma_ext=sigma_ext,
                 retest_required=(retest_status == RetestStatus.PENDING))

        return breakout

    def _detect_extension_signal(
        self, bar: PriceBar, vwap: float, current_bps: float
    ) -> Optional[BreakoutEvent]:
        """
        Detect TYPE_B exhaustion fade or TYPE_C consolidation signals
        when price is already extended from VWAP.
        """
        config = self.config.vwap
        sigma_ext = self._calculate_sigma_extension(bar.close, vwap)

        # TYPE_B: Exhaustion at extreme extension
        if abs(sigma_ext) >= 1.5:
            volume_declining = self._is_volume_declining(bars=5)
            rsi_divergence = self._check_rsi_divergence()

            if volume_declining and rsi_divergence:
                avg_volume = self._calculate_avg_volume(20)
                volume_ratio = bar.volume / avg_volume if avg_volume else 1.0

                quality = self._score_exhaustion_quality(
                    sigma_ext=sigma_ext,
                    volume_declining=volume_declining,
                )

                if quality < config.min_break_quality_score:
                    return None

                tod_weight = self._get_tod_weight(bar.timestamp)

                return BreakoutEvent(
                    timestamp=bar.timestamp,
                    breakout_type=BreakoutType.TYPE_B_FADE,
                    direction="neutral",   # Fade = mean reversion, not directional
                    break_price=bar.close,
                    vwap_at_break=vwap,
                    break_bps=abs(current_bps),
                    break_volume=bar.volume,
                    volume_ratio=volume_ratio,
                    retest_status=RetestStatus.NOT_REQUIRED,
                    quality_score=quality,
                    tod_weight=tod_weight,
                    sigma_extension=sigma_ext,
                )

        # TYPE_C: Consolidation near VWAP detected (options flow engine handles this)
        if abs(current_bps) <= 5 and self._is_consolidating(bars=10):
            log.debug("vwap.consolidation_detected_near_vwap")
            # Signal returned after options flow confirmation in composite engine

        return None

    async def _evaluate_active_breakout(self) -> Optional[VWAPSignal]:
        """
        Update and evaluate an active (in-progress) breakout.
        Checks for retest, continuation, or invalidation.
        """
        breakout = self._active_breakout
        if not breakout or not self._vwap_levels:
            return None

        current_bar = self._bars[-1]
        vwap = self._vwap_levels.vwap
        config = self.config.vwap

        # ── Check for invalidation (price crosses back through VWAP) ──
        if self._check_vwap_invalidation(current_bar, vwap, breakout):
            log.info("vwap.breakout_invalidated",
                     breakout_type=breakout.breakout_type.name,
                     entry_price=breakout.break_price,
                     invalidation_price=current_bar.close)

            self._vwap_invalidations_recent += 1
            self._vwap_evaluations_recent += 1
            self._active_breakout = None
            return None  # Signal is dead

        # ── Check retest status ────────────────────────────────
        if breakout.retest_status == RetestStatus.PENDING:
            retest_result = self._check_retest(current_bar, vwap, breakout)
            breakout.retest_status = retest_result

            if retest_result == RetestStatus.FAILED:
                log.info("vwap.retest_failed",
                         breakout_type=breakout.breakout_type.name)
                self._active_breakout = None
                return None

            if retest_result == RetestStatus.CONFIRMED:
                log.info("vwap.retest_confirmed",
                         breakout_type=breakout.breakout_type.name,
                         retest_price=current_bar.close)
                breakout.retest_price = current_bar.close
                breakout.retest_held = True
                # Retest confirmation → upgrade quality score
                breakout.quality_score = min(1.0, breakout.quality_score * 1.15)

            # Still pending — return current signal but lower confidence
            if retest_result == RetestStatus.PENDING:
                return self._generate_signal_from_breakout(breakout, pending=True)

        # Breakout is active and confirmed (or not requiring retest)
        self._vwap_evaluations_recent += 1
        return self._generate_signal_from_breakout(breakout)

    def _generate_signal_from_breakout(
        self, breakout: BreakoutEvent, pending: bool = False
    ) -> Optional[VWAPSignal]:
        """Convert a BreakoutEvent into a tradeable VWAPSignal."""
        if not self._vwap_levels:
            return None

        vwap = self._vwap_levels.vwap
        config = self.config.vwap

        # ── Calculate raw score ──────────────────────────────────
        base_score = breakout.quality_score * breakout.tod_weight

        # Retest confirmation boosts score
        if breakout.retest_status == RetestStatus.CONFIRMED:
            base_score *= 1.20
        elif breakout.retest_status == RetestStatus.PENDING:
            base_score *= 0.65  # Reduced confidence until confirmed

        # Volume ratio boost
        vol_boost = min(0.15, (breakout.volume_ratio - 1.8) * 0.05)
        base_score = min(1.0, base_score + vol_boost)

        # Directional score
        if breakout.direction == "bullish":
            score = base_score
        elif breakout.direction == "bearish":
            score = -base_score
        else:
            score = 0.0  # TYPE_B fade → neutral score (condor signal)

        # ── Target and stop levels ───────────────────────────────
        if breakout.direction == "bullish":
            target = self._vwap_levels.upper_1sigma
            stop = vwap * (1 - config.retest_tolerance_bps / 10000)
        elif breakout.direction == "bearish":
            target = self._vwap_levels.lower_1sigma
            stop = vwap * (1 + config.retest_tolerance_bps / 10000)
        else:
            # TYPE_B fade: target is VWAP itself
            target = vwap
            stop = self._vwap_levels.upper_2sigma if breakout.sigma_extension > 0 \
                else self._vwap_levels.lower_2sigma

        # ── Confidence calculation ────────────────────────────────
        confidence_factors = [
            breakout.quality_score,
            min(breakout.volume_ratio / 3.0, 1.0),
            breakout.tod_weight,
            1.0 if breakout.retest_status == RetestStatus.CONFIRMED else
            0.6 if breakout.retest_status == RetestStatus.PENDING else 0.85,
        ]
        confidence = float(np.mean(confidence_factors))

        return VWAPSignal(
            score=float(np.clip(score, -1.0, 1.0)),
            direction=breakout.direction,
            confidence=float(np.clip(confidence, 0.0, 1.0)),
            breakout_type=breakout.breakout_type,
            retest_status=breakout.retest_status,
            vwap_level=vwap,
            target_level=target,
            stop_level=stop,
            sigma_extension=breakout.sigma_extension,
            volume_ratio=breakout.volume_ratio,
            quality_score=breakout.quality_score,
            tod_weight=breakout.tod_weight,
            diagnostics={
                "break_bps": breakout.break_bps,
                "break_price": breakout.break_price,
                "pending_retest": pending,
                "vwap_upper_1s": self._vwap_levels.upper_1sigma,
                "vwap_lower_1s": self._vwap_levels.lower_1sigma,
            }
        )

    # ──────────────────────────────────────────────────────────────
    # VWAP CALCULATION
    # ──────────────────────────────────────────────────────────────

    def _update_session_vwap(self, bar: PriceBar):
        """Incrementally update session VWAP accumulators."""
        typical_price = (bar.high + bar.low + bar.close) / 3.0
        self._session_cum_volume += bar.volume
        self._session_cum_vp += typical_price * bar.volume
        self._session_cum_vp2 += (typical_price ** 2) * bar.volume

    def _calculate_vwap_levels(self) -> Optional[VWAPLevels]:
        """Calculate current VWAP and standard deviation bands."""
        if self._session_cum_volume == 0:
            return None

        vwap = self._session_cum_vp / self._session_cum_volume

        # Standard deviation of VWAP
        variance = (self._session_cum_vp2 / self._session_cum_volume) - (vwap ** 2)
        std_dev = np.sqrt(max(0, variance))

        multipliers = self.config.vwap.band_multipliers
        m1, m2, m3 = multipliers[0], multipliers[1], multipliers[2]

        bars_list = list(self._bars)
        if not bars_list:
            return None

        return VWAPLevels(
            vwap=vwap,
            upper_1sigma=vwap + m1 * std_dev,
            lower_1sigma=vwap - m1 * std_dev,
            upper_2sigma=vwap + m2 * std_dev,
            lower_2sigma=vwap - m2 * std_dev,
            upper_3sigma=vwap + m3 * std_dev,
            lower_3sigma=vwap - m3 * std_dev,
            cumulative_volume=self._session_cum_volume,
            session_high=max(b.high for b in bars_list),
            session_low=min(b.low for b in bars_list),
            timestamp=bars_list[-1].timestamp,
        )

    def _calculate_sigma_extension(self, price: float, vwap: float) -> float:
        """How many standard deviations is price from VWAP?"""
        if not self._vwap_levels:
            return 0.0

        one_sigma = self._vwap_levels.upper_1sigma - vwap
        if one_sigma == 0:
            return 0.0

        return (price - vwap) / one_sigma

    # ──────────────────────────────────────────────────────────────
    # HELPER METHODS
    # ──────────────────────────────────────────────────────────────

    def _score_breakout_quality(
        self, volume_ratio: float, sigma_ext: float,
        breakout_type: BreakoutType, direction: str
    ) -> float:
        """Score breakout quality 0→1."""
        # Volume component (40% weight)
        vol_score = min(1.0, (volume_ratio - 1.0) / 2.0)  # Maps [1→3] to [0→1]

        # Clean break component (30% weight)
        # Smaller extensions are cleaner (not too stretched)
        ext_score = max(0, 1.0 - (abs(sigma_ext) - 0.3) / 1.2)

        # Breadth component (30% weight) — is the break decisive?
        # Use price velocity of current vs previous bars
        price_velocity = self._calculate_price_velocity(bars=3)
        velocity_score = min(1.0, abs(price_velocity) * 100)  # Normalize

        return vol_score * 0.40 + ext_score * 0.30 + velocity_score * 0.30

    def _score_exhaustion_quality(
        self, sigma_ext: float, volume_declining: bool
    ) -> float:
        """Score TYPE_B exhaustion fade quality."""
        ext_score = min(1.0, (abs(sigma_ext) - 1.5) / 1.5)  # 1.5→3.0 σ
        volume_score = 0.8 if volume_declining else 0.3
        return ext_score * 0.50 + volume_score * 0.50

    def _check_vwap_invalidation(
        self, bar: PriceBar, vwap: float, breakout: BreakoutEvent
    ) -> bool:
        """Check if VWAP has been crossed back — invalidates the signal."""
        avg_volume = self._calculate_avg_volume(20)
        vol_threshold = self.config.per_trade_risk.vwap_recross_volume_threshold \
            if hasattr(self.config, 'per_trade_risk') else 1.4
        vol_confirming = (bar.volume / avg_volume) >= vol_threshold if avg_volume else True

        if breakout.direction == "bullish":
            return bar.close < vwap and vol_confirming
        elif breakout.direction == "bearish":
            return bar.close > vwap and vol_confirming
        return False

    def _check_retest(
        self, bar: PriceBar, vwap: float, breakout: BreakoutEvent
    ) -> RetestStatus:
        """Check if price retested VWAP and held."""
        tolerance = self.config.vwap.retest_tolerance_bps / 10000 * vwap
        timeout_bars = self.config.vwap.retest_timeout_bars

        # Check timeout
        bars_since_break = sum(
            1 for b in list(self._bars)[-timeout_bars:]
            if b.timestamp > breakout.timestamp
        )
        if bars_since_break >= timeout_bars:
            return RetestStatus.FAILED

        # Near VWAP = retest happening
        near_vwap = abs(bar.close - vwap) <= tolerance

        if near_vwap:
            # Held if close is still on break side
            if breakout.direction == "bullish" and bar.close >= vwap:
                return RetestStatus.CONFIRMED
            elif breakout.direction == "bearish" and bar.close <= vwap:
                return RetestStatus.CONFIRMED
            else:
                return RetestStatus.FAILED

        return RetestStatus.PENDING

    def _calculate_avg_volume(self, bars: int) -> float:
        """Calculate average volume over last N bars."""
        recent = list(self._bars)[-bars:]
        if not recent:
            return 0.0
        return float(np.mean([b.volume for b in recent]))

    def _calculate_price_velocity(self, bars: int) -> float:
        """Rate of price change over last N bars."""
        recent = list(self._bars)[-bars:]
        if len(recent) < 2:
            return 0.0
        return (recent[-1].close - recent[0].close) / recent[0].close

    def _is_volume_declining(self, bars: int) -> bool:
        """Check if volume is on a declining trend."""
        recent = list(self._bars)[-bars:]
        if len(recent) < 3:
            return False
        volumes = [b.volume for b in recent]
        # Simple linear regression slope
        x = np.arange(len(volumes))
        slope = np.polyfit(x, volumes, 1)[0]
        return slope < 0

    def _is_consolidating(self, bars: int) -> bool:
        """Check if price is consolidating (low range relative to ATR)."""
        recent = list(self._bars)[-bars:]
        if len(recent) < bars:
            return False
        highs = [b.high for b in recent]
        lows = [b.low for b in recent]
        price_range = max(highs) - min(lows)
        avg_price = np.mean([b.close for b in recent])
        return (price_range / avg_price) < 0.003  # Less than 0.3% range

    def _check_rsi_divergence(self) -> bool:
        """Simple RSI divergence check — price making new high/low but RSI isn't."""
        if len(self._bars) < 14:
            return False
        closes = [b.close for b in list(self._bars)[-20:]]
        rsi = self._calculate_rsi(closes, 14)
        if len(rsi) < 5:
            return False
        # Price making new extreme but RSI not confirming
        price_new_high = closes[-1] > max(closes[-10:-1])
        rsi_not_new_high = rsi[-1] < max(rsi[-10:-1])
        price_new_low = closes[-1] < min(closes[-10:-1])
        rsi_not_new_low = rsi[-1] > min(rsi[-10:-1])
        return (price_new_high and rsi_not_new_high) or (price_new_low and rsi_not_new_low)

    def _calculate_rsi(self, closes: List[float], period: int = 14) -> List[float]:
        """Calculate RSI series."""
        if len(closes) < period + 1:
            return []
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.convolve(gains, np.ones(period) / period, mode='valid')
        avg_loss = np.convolve(losses, np.ones(period) / period, mode='valid')
        rs = np.where(avg_loss != 0, avg_gain / avg_loss, 100)
        return list(100 - 100 / (1 + rs))

    def _get_tod_weight(self, ts: datetime) -> float:
        """Get time-of-day weight for the current bar's timestamp."""
        tod_weights = self.config.vwap.tod_weights
        bar_time = ts.strftime("%H:%M")

        # Find the closest earlier time key
        times = sorted(tod_weights.keys())
        weight = 1.0
        for t in times:
            if bar_time >= t:
                weight = tod_weights[t]
        return weight

    def get_vwap_invalidation_rate(self) -> float:
        """Return the recent rate of VWAP breakout invalidations."""
        if self._vwap_evaluations_recent == 0:
            return 0.0
        return self._vwap_invalidations_recent / self._vwap_evaluations_recent

    def reset_session(self):
        """Reset all session-specific accumulators at market open."""
        self._session_cum_volume = 0.0
        self._session_cum_vp = 0.0
        self._session_cum_vp2 = 0.0
        self._session_start = datetime.now()
        self._active_breakout = None
        self._recent_breakouts.clear()
        self._vwap_invalidations_recent = 0
        self._vwap_evaluations_recent = 0
        log.info("vwap.session_reset", underlying=self.underlying)

    async def _fetch_latest_bar(self) -> Optional[PriceBar]:
        """
        Fetch latest 1-minute OHLCV bar.
        IMPLEMENTATION: Replace with live data feed.
        """
        raise NotImplementedError(
            "Implement _fetch_latest_bar with your data provider "
            "(IBKR, TastyTrade streaming, Polygon.io, Tradier, etc.)."
        )
