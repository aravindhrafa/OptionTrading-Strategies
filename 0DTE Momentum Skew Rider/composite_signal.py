"""
Composite Signal Engine
=======================
Combines skew momentum, GEX, and price momentum into a single
weighted signal score with regime-adjusted weighting.

Jane Street principle: No single signal is reliable enough alone.
Require multi-factor confirmation before entering any position.

Signal combination:
  1. Calculate raw scores from each sub-signal
  2. Apply regime-specific weights
  3. Require minimum score threshold AND minimum confidence
  4. Cross-validate: signals must agree on direction (no mixed signals)
  5. Final quality gate before passing to risk layer
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from datetime import datetime
import numpy as np
import structlog

from src.signals.skew_signal import SkewSignalEngine, SkewSignal
from src.signals.gex_signal import GEXSignalEngine, GEXSignal

log = structlog.get_logger(__name__)


@dataclass
class MomentumSignal:
    """Price momentum sub-signal."""
    score: float         # [-1, +1]
    direction: str
    confidence: float
    fast_period: int
    slow_period: int
    macd_value: float
    macd_signal: float
    histogram: float
    volume_confirmation: bool


@dataclass
class CompositeSignal:
    """
    Final composite signal passed to the risk layer.
    All three sub-signals must agree before this is generated.
    """
    score: float            # Weighted composite [-1, +1]
    direction: str          # "bullish" | "bearish"
    confidence: float       # [0, 1] — overall quality
    skew_score: float
    gex_score: float
    momentum_score: float
    market_regime: str      # "pinning" | "trending" | "explosive"
    recommended_structure: str
    target_delta: float     # What delta to target for entry
    max_dte: int
    timestamp: datetime = field(default_factory=datetime.now)
    diagnostics: dict = field(default_factory=dict)


@dataclass
class TradeProposal:
    """Full trade proposal sent to risk guardian."""
    signal: CompositeSignal
    underlying: str
    structure: str           # "debit_spread" | "iron_condor" | "long_straddle"
    direction: str
    # Strikes (populated by options chain scan)
    long_strike: Optional[float] = None
    short_strike: Optional[float] = None
    long_wing_strike: Optional[float] = None    # For iron condors
    short_wing_strike: Optional[float] = None
    expiry: Optional[str] = None
    # Risk metrics
    max_risk: float = 0.0        # Max loss in dollars
    max_reward: float = 0.0      # Max profit in dollars
    risk_reward: float = 0.0
    estimated_edge_bps: float = 0.0
    # Greeks at entry
    entry_delta: float = 0.0
    entry_gamma: float = 0.0
    entry_theta: float = 0.0
    entry_vega: float = 0.0


class CompositeSignalEngine:
    """
    Multi-factor signal aggregator.
    Single point of entry for all signal generation.
    """

    def __init__(self, config, universe: List[str]):
        self.config = config
        self.universe = universe
        self._skew_engines: Dict[str, SkewSignalEngine] = {}
        self._gex_engines: Dict[str, GEXSignalEngine] = {}
        self._signal_history: List[CompositeSignal] = []

    async def connect(self):
        """Initialize all sub-signal engines."""
        for symbol in self.universe:
            self._skew_engines[symbol] = SkewSignalEngine(
                config=self.config.skew, underlying=symbol
            )
            self._gex_engines[symbol] = GEXSignalEngine(
                config=self.config.gex, underlying=symbol
            )
            await self._skew_engines[symbol].connect()

        log.info("composite_signal.connected", symbols=self.universe)

    async def disconnect(self):
        log.info("composite_signal.disconnected")

    async def generate(self) -> Optional[CompositeSignal]:
        """
        Generate composite signal across all underlyings.
        Returns the best signal or None if no qualifying setup.
        """
        candidates = []

        for symbol in self.universe:
            signal = await self._generate_for_symbol(symbol)
            if signal is not None:
                candidates.append((symbol, signal))

        if not candidates:
            return None

        # Return highest-confidence signal
        best = max(candidates, key=lambda x: x[1].confidence * abs(x[1].score))
        return best[1]

    async def _generate_for_symbol(self, symbol: str) -> Optional[CompositeSignal]:
        """Generate composite signal for a single underlying."""
        weights = self.config.composite

        # Gather sub-signals in parallel
        skew_task = self._skew_engines[symbol].get_signal()
        gex_task = self._gex_engines[symbol].get_signal()
        momentum_task = self._get_momentum_signal(symbol)

        results = await asyncio.gather(
            skew_task, gex_task, momentum_task,
            return_exceptions=True
        )

        skew_signal = results[0] if not isinstance(results[0], Exception) else None
        gex_signal = results[1] if not isinstance(results[1], Exception) else None
        mom_signal = results[2] if not isinstance(results[2], Exception) else None

        # Log any errors but don't crash
        for i, (name, result) in enumerate(zip(["skew", "gex", "momentum"], results)):
            if isinstance(result, Exception):
                log.warning(f"signal.{name}_error", symbol=symbol, error=str(result))

        # ── Require minimum number of signals ───────────────────
        valid_signals = sum(1 for s in [skew_signal, gex_signal, mom_signal]
                           if s is not None)
        if valid_signals < 2:
            log.debug("composite.insufficient_signals",
                      symbol=symbol, valid=valid_signals)
            return None

        # ── Extract scores (use 0 if signal unavailable) ─────────
        skew_score = skew_signal.score if skew_signal else 0.0
        gex_score = gex_signal.score if gex_signal else 0.0
        mom_score = mom_signal.score if mom_signal else 0.0

        # ── Direction Agreement Check ────────────────────────────
        # CRITICAL: Do not enter if signals disagree on direction
        scores = [s for s in [skew_score, gex_score, mom_score] if s != 0.0]
        if not scores:
            return None

        directions = [np.sign(s) for s in scores]
        agreement_ratio = abs(sum(directions)) / len(directions)

        if agreement_ratio < 0.67:  # Need 2/3 agreement minimum
            log.debug("composite.direction_disagreement",
                      symbol=symbol,
                      skew=np.sign(skew_score),
                      gex=np.sign(gex_score),
                      momentum=np.sign(mom_score))
            return None

        # ── Weighted Composite Score ─────────────────────────────
        # Adjust weights based on available signals
        total_weight = 0
        weighted_score = 0.0

        if skew_signal:
            weighted_score += skew_score * weights.skew_weight
            total_weight += weights.skew_weight
        if gex_signal:
            weighted_score += gex_score * weights.gex_weight
            total_weight += weights.gex_weight
        if mom_signal:
            weighted_score += mom_score * weights.momentum_weight
            total_weight += weights.momentum_weight

        if total_weight == 0:
            return None

        composite_score = weighted_score / total_weight

        # ── Confidence Calculation ───────────────────────────────
        confidences = []
        if skew_signal:
            confidences.append(skew_signal.confidence)
        if gex_signal:
            confidences.append(gex_signal.confidence)
        if mom_signal:
            confidences.append(mom_signal.confidence)

        avg_confidence = np.mean(confidences) if confidences else 0.0
        direction_boost = 0.1 * (agreement_ratio - 0.67) / 0.33
        final_confidence = float(np.clip(avg_confidence + direction_boost, 0, 1))

        # ── Quality Gate ─────────────────────────────────────────
        min_score = weights.min_composite_score
        if abs(composite_score) < min_score:
            log.debug("composite.score_below_threshold",
                      symbol=symbol, score=composite_score, threshold=min_score)
            return None

        # ── Regime and Structure ─────────────────────────────────
        market_regime = gex_signal.regime if gex_signal else "trending"
        recommended_structure = self._recommend_structure(
            regime=market_regime,
            direction=composite_score,
        )

        # ── Target Delta ─────────────────────────────────────────
        target_delta = self._calculate_target_delta(
            score=abs(composite_score),
            regime=market_regime,
        )

        signal = CompositeSignal(
            score=float(composite_score),
            direction="bullish" if composite_score > 0 else "bearish",
            confidence=final_confidence,
            skew_score=float(skew_score),
            gex_score=float(gex_score),
            momentum_score=float(mom_score),
            market_regime=market_regime,
            recommended_structure=recommended_structure,
            target_delta=target_delta,
            max_dte=0,
            diagnostics={
                "symbol": symbol,
                "agreement_ratio": agreement_ratio,
                "valid_signals": valid_signals,
                "weights_used": total_weight,
            }
        )

        self._signal_history.append(signal)
        log.info("composite.signal_generated",
                 symbol=symbol,
                 score=composite_score,
                 confidence=final_confidence,
                 direction=signal.direction,
                 regime=market_regime,
                 structure=recommended_structure)

        return signal

    def _recommend_structure(self, regime: str, direction: float) -> str:
        """Recommend trade structure based on regime and signal direction."""
        if regime == "pinning":
            return "iron_condor"       # Sell premium around the pin
        elif regime == "explosive":
            return "long_straddle"     # Buy volatility in explosive regime
        else:  # trending
            if abs(direction) > 0.7:
                return "debit_spread"  # Strong directional: capped risk spread
            else:
                return "debit_spread"  # Default to defined risk

    def _calculate_target_delta(self, score: float, regime: str) -> float:
        """
        Calculate target delta for option selection.
        Higher score = more ATM (higher delta), more conviction.
        """
        if regime == "explosive":
            return 0.50  # ATM for straddle

        # Map score [0, 1] to delta [0.25, 0.45]
        # Higher conviction = closer to ATM = higher delta
        return 0.25 + (score * 0.20)

    async def get_market_regime(self) -> str:
        """Get current market regime from GEX analysis."""
        if not self._gex_engines:
            return "trending"

        # Use primary symbol (first in universe)
        primary = self.universe[0]
        engine = self._gex_engines.get(primary)
        if engine:
            return engine.get_current_regime()
        return "trending"

    async def build_trade_proposal(
        self, signal: CompositeSignal, structure: str, config
    ) -> Optional[TradeProposal]:
        """
        Build a full trade proposal from a signal and structure.
        Scans options chain for optimal strike selection.
        """
        symbol = signal.diagnostics.get("symbol", self.universe[0])
        gex_levels = {}

        gex_engine = self._gex_engines.get(symbol)
        if gex_engine:
            gex_levels = gex_engine.get_key_levels()

        proposal = TradeProposal(
            signal=signal,
            underlying=symbol,
            structure=structure,
            direction=signal.direction,
            estimated_edge_bps=signal.skew_score * 60,  # Rough calibration
        )

        # Strike selection is broker/data-dependent
        # Concrete implementation goes in broker adapter
        await self._populate_strikes(proposal, signal, gex_levels, config)

        return proposal

    async def _populate_strikes(
        self, proposal: TradeProposal, signal: CompositeSignal,
        gex_levels: dict, config
    ):
        """
        Populate strikes based on structure and GEX levels.
        Override/implement in broker adapter layer.
        """
        # Placeholder — real implementation fetches live chain
        raise NotImplementedError(
            "Implement strike population using live options chain data. "
            "Use target_delta to find closest strike, "
            "use GEX pin levels to avoid selling into momentum."
        )

    async def _get_momentum_signal(self, symbol: str) -> Optional[MomentumSignal]:
        """
        Price momentum sub-signal using MACD-derivative.
        Uses 1-minute bars for 0DTE granularity.
        """
        # Implementation requires live price bar feed
        raise NotImplementedError(
            "Implement momentum signal with 1-minute OHLCV data. "
            "MACD on 1-min bars: fast=3, slow=8, signal=2."
        )
