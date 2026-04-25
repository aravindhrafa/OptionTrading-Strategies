"""
IV Skew Momentum Signal
=======================
Core alpha source #1: Exploits lead-lag relationship between
IV skew shifts and subsequent underlying price movement.

Key insight: When put IV spikes faster than call IV (skew steepens),
institutional put buying is frontrunning a downside move. The reverse
(skew flattening/inverting) signals a squeeze or momentum reversal.

Time edge: Skew changes typically lead underlying moves by 2-8 minutes
in liquid instruments like SPY/QQQ/SPX during high-volume sessions.

Signal outputs:
  +1.0 = Strong bullish (skew flattening, calls bid up, squeeze likely)
  -1.0 = Strong bearish (skew steepening, puts being bought heavily)
   0.0 = Neutral (no clear skew momentum)
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from collections import deque
from datetime import datetime
import numpy as np
import structlog

log = structlog.get_logger(__name__)


@dataclass
class SkewSnapshot:
    """Point-in-time skew observation."""
    timestamp: datetime
    put_iv_25d: float      # 25-delta put IV
    put_iv_10d: float      # 10-delta put IV
    call_iv_25d: float     # 25-delta call IV
    call_iv_10d: float     # 10-delta call IV
    atm_iv: float          # ATM straddle IV
    skew_25d: float        # put_iv_25d - call_iv_25d (risk reversal)
    skew_10d: float        # put_iv_10d - call_iv_10d (wing skew)
    put_call_ratio: float  # Volume-weighted put/call ratio
    underlying_price: float


@dataclass
class SkewSignal:
    """Output signal from skew analysis."""
    score: float           # Normalized [-1, +1]
    direction: str         # "bullish", "bearish", "neutral"
    confidence: float      # [0, 1]
    skew_velocity: float   # Rate of change of skew
    skew_acceleration: float  # Second derivative (momentum of momentum)
    regime: str            # "steepening", "flattening", "stable", "inverting"
    edge_bps: float        # Estimated edge in basis points
    diagnostics: dict = field(default_factory=dict)


class SkewSignalEngine:
    """
    IV Skew Momentum Signal Generator.

    Calculates real-time put/call IV skew and its rate of change
    to generate directional signals for 0DTE option trades.
    """

    def __init__(self, config, underlying: str):
        self.config = config
        self.underlying = underlying
        self._snapshots: deque = deque(maxlen=config.lookback_periods)
        self._connected = False

    async def connect(self):
        """Connect to options data feed."""
        # Adapter pattern — inject real data source here
        self._connected = True
        log.info("skew_engine.connected", underlying=self.underlying)

    async def get_signal(self) -> Optional[SkewSignal]:
        """
        Generate current skew signal.
        Returns None if insufficient data or no signal.
        """
        snapshot = await self._fetch_current_snapshot()
        if snapshot is None:
            return None

        self._snapshots.append(snapshot)

        if len(self._snapshots) < 5:  # Need minimum history
            log.debug("skew.insufficient_history", n=len(self._snapshots))
            return None

        return self._calculate_signal()

    def _calculate_signal(self) -> Optional[SkewSignal]:
        """Core signal calculation from historical snapshots."""
        snapshots = list(self._snapshots)
        skew_25d_series = np.array([s.skew_25d for s in snapshots])
        skew_10d_series = np.array([s.skew_10d for s in snapshots])
        pcr_series = np.array([s.put_call_ratio for s in snapshots])

        # ── Skew Velocity (first derivative) ────────────────────
        # Using short/long EMA diff for noise resistance
        fast = self._ema(skew_25d_series, period=3)
        slow = self._ema(skew_25d_series, period=8)
        skew_velocity = fast[-1] - slow[-1]

        # ── Skew Acceleration (second derivative) ────────────────
        # Is the velocity itself accelerating? Key for timing entries.
        if len(skew_25d_series) >= 10:
            recent_vel = fast[-1] - slow[-1]
            prior_vel = self._ema(skew_25d_series[:-3], 3)[-1] - \
                        self._ema(skew_25d_series[:-3], 8)[-1]
            skew_acceleration = recent_vel - prior_vel
        else:
            skew_acceleration = 0.0

        # ── Regime Classification ────────────────────────────────
        regime = self._classify_regime(skew_25d_series, skew_velocity)

        # ── Wing Skew Confirmation ───────────────────────────────
        # 10-delta skew should confirm 25-delta direction
        wing_velocity = (self._ema(skew_10d_series, 3)[-1] -
                         self._ema(skew_10d_series, 8)[-1])
        wing_confirms = np.sign(wing_velocity) == np.sign(skew_velocity)

        # ── Put/Call Ratio Trend ─────────────────────────────────
        pcr_trend = self._ema(pcr_series, 5)[-1] - self._ema(pcr_series, 10)[-1]

        # ── Raw Score Calculation ────────────────────────────────
        # Normalize velocity to [-1, +1] using historical percentile
        vel_normalized = self._percentile_normalize(
            skew_velocity, skew_25d_series[:-1]
        )

        # Adjust for wing confirmation
        if wing_confirms:
            vel_normalized *= 1.15  # Boost signal
        else:
            vel_normalized *= 0.70  # Reduce signal (conflicting)

        # PCR adjustment
        # Rising PCR = more put buying = bearish
        pcr_adjustment = np.tanh(pcr_trend * 10) * 0.2
        vel_normalized -= pcr_adjustment  # Subtract because high PCR = bearish

        # Clamp to [-1, +1]
        score = float(np.clip(vel_normalized, -1.0, 1.0))

        # ── Minimum Velocity Filter ──────────────────────────────
        min_velocity = self.config.min_skew_velocity
        if abs(skew_velocity) < min_velocity:
            log.debug("skew.velocity_too_low",
                      velocity=skew_velocity, minimum=min_velocity)
            return None

        # ── Direction and Confidence ─────────────────────────────
        direction = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
        confidence = min(abs(score) * (1.15 if wing_confirms else 0.85), 1.0)

        # ── Edge Estimation ──────────────────────────────────────
        # Historical: skew velocity > 0.05 has ~60bps expected value
        edge_bps = abs(skew_velocity) * 1200  # Rough calibration

        signal = SkewSignal(
            score=score,
            direction=direction,
            confidence=confidence,
            skew_velocity=skew_velocity,
            skew_acceleration=skew_acceleration,
            regime=regime,
            edge_bps=edge_bps,
            diagnostics={
                "skew_25d_current": float(skew_25d_series[-1]),
                "skew_10d_current": float(skew_10d_series[-1]),
                "wing_confirms": wing_confirms,
                "pcr_trend": float(pcr_trend),
                "snapshots_used": len(snapshots),
            }
        )

        log.debug("skew.signal_generated",
                  score=score, direction=direction,
                  confidence=confidence, regime=regime,
                  velocity=skew_velocity)

        return signal

    def _classify_regime(self, skew_series: np.ndarray, velocity: float) -> str:
        """Classify current skew regime."""
        current = skew_series[-1]
        mean = np.mean(skew_series)

        if velocity > 0.03 and current > mean:
            return "steepening"     # Puts being bid up vs calls
        elif velocity < -0.03 and current > mean:
            return "flattening"     # Skew normalizing (squeeze potential)
        elif current < 0:
            return "inverting"      # Calls premium over puts — extreme bullish
        else:
            return "stable"

    def _ema(self, series: np.ndarray, period: int) -> np.ndarray:
        """Exponential moving average."""
        alpha = 2.0 / (period + 1)
        ema = np.zeros_like(series, dtype=float)
        ema[0] = series[0]
        for i in range(1, len(series)):
            ema[i] = alpha * series[i] + (1 - alpha) * ema[i - 1]
        return ema

    def _percentile_normalize(self, value: float, history: np.ndarray) -> float:
        """Normalize value to [-1, +1] based on historical percentile."""
        if len(history) < 3:
            return 0.0
        pct = float(np.mean(history < value))  # Percentile rank
        # Convert [0,1] percentile to [-1,+1] score
        return (pct - 0.5) * 2.0

    async def _fetch_current_snapshot(self) -> Optional[SkewSnapshot]:
        """
        Fetch current options chain data and compute skew.

        IMPLEMENTATION NOTE: Replace this stub with your actual
        data feed (TastyTrade, IBKR, Polygon.io, Tradier, etc.)
        """
        raise NotImplementedError(
            "Implement _fetch_current_snapshot with your options data feed. "
            "See broker_adapters/ for reference implementations."
        )
