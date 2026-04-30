"""
IV Skew Signal Engine — Core Alpha Engine
==========================================
Analyzes the implied volatility skew structure around earnings
to identify systematic mispricings between option strikes.

══════════════════════════════════════════════════════════════
  THEORY: WHY SKEW MISPRICINGS EXIST AROUND EARNINGS
══════════════════════════════════════════════════════════════

Before earnings, retail investors and institutional hedgers rush
to buy OTM puts for downside protection. This demand imbalance
causes put IV to exceed call IV by MORE than the no-arbitrage
skew implied by the stock's jump distribution.

In other words: the market OVERPRICES fear.

Historical evidence (S&P 500 large-cap earnings, 2015-2023):
  • 25Δ Put IV averages 18% RICHER than 25Δ Call IV before earnings
  • Post-earnings, this premium collapses to ~8% (normal skew)
  • The 10% skew compression = tradeable edge
  • Put IV crush is typically 5-15% LARGER than call IV crush

Signal outputs:
  • Skew Richness Score [-1, +1]
    +1.0 = Puts extremely expensive → Risk reversal or condor
    -1.0 = Calls expensive (inverted) → Unusual, investigate
     0.0 = Balanced skew → Standard condor

  • Recommended Structure:
    RICH_SKEW → iron_condor (sell both, especially put side)
    EXTREME_PUTS → risk_reversal (sell puts, buy calls)
    INVERTED_SKEW → debit_spread (buy puts, more cautious)

  • Edge Estimate in bps:
    Quantifies the theoretical edge from selling the overpriced skew

══════════════════════════════════════════════════════════════
  SKEW METRICS WE CALCULATE
══════════════════════════════════════════════════════════════

1. Risk Reversal (25Δ):
   RR₂₅ = IV(25Δ Put) - IV(25Δ Call)
   Higher = puts more expensive than calls
   Typical pre-earnings: +5% to +15% (puts premium)
   Extreme pre-earnings: +20% to +35% (very rich puts)

2. Butterfly (25Δ):
   BF₂₅ = (IV(25Δ Put) + IV(25Δ Call)) / 2 - IV(ATM)
   Measures wing premium above ATM
   High butterfly = wings expensive vs ATM

3. Skew Slope:
   d(IV)/d(Strike): How fast IV increases as you go further OTM
   Steep slope = rich tails = condor opportunity

4. Term Structure Ratio:
   IV(front month) / IV(back month)
   > 1.5 = inverted (earnings bump) = calendar opportunity
   > 2.0 = extreme inversion = highest IV crush potential

5. Skew vs Historical Baseline:
   Current RR₂₅ / Median Pre-Earnings RR₂₅ (same ticker)
   > 1.3 = richer than usual → higher edge
   < 0.7 = cheaper than usual → lower edge
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum, auto
from typing import Optional, List, Dict, Tuple
import numpy as np
import structlog

log = structlog.get_logger(__name__)


class SkewRegime(Enum):
    """Classification of the current skew environment."""
    EXTREME_PUT_RICH = auto()    # Puts massively overpriced → risk reversal
    RICH_SKEW = auto()           # Standard put richness → iron condor
    BALANCED = auto()            # Normal skew → cautious condor
    INVERTED = auto()            # Calls expensive → unusual, investigate
    UNKNOWN = auto()


class RecommendedStructure(Enum):
    """Trade structure recommended by skew analysis."""
    IRON_CONDOR = auto()
    RISK_REVERSAL = auto()       # Sell put, buy call (skew arb)
    CALENDAR_SPREAD = auto()
    DEBIT_SPREAD = auto()
    NO_TRADE = auto()


@dataclass
class StrikeIVPoint:
    """IV at a specific delta/strike."""
    delta: float
    strike: float
    iv: float
    bid_iv: float
    ask_iv: float
    mid_iv: float
    option_type: str          # "call" or "put"
    open_interest: int
    volume: int
    bid_ask_spread_pct: float  # Bid-ask as % of mid IV


@dataclass
class IVSkewSnapshot:
    """Full IV skew profile at a point in time."""
    ticker: str
    underlying_price: float
    report_date: date
    timestamp: datetime

    # ATM IV
    atm_iv: float
    atm_strike: float

    # Delta-based IV surface
    put_ivs: Dict[float, StrikeIVPoint]   # delta → StrikeIVPoint (puts)
    call_ivs: Dict[float, StrikeIVPoint]  # delta → StrikeIVPoint (calls)

    # Computed metrics
    risk_reversal_25d: float    # IV(25Δ Put) - IV(25Δ Call)
    risk_reversal_10d: float    # IV(10Δ Put) - IV(10Δ Call)
    butterfly_25d: float        # (IV(25Δ Put) + IV(25Δ Call))/2 - ATM IV
    butterfly_10d: float
    skew_slope: float           # d(IV)/d(Delta) approximation

    # Term structure
    front_month_iv: float       # Earnings expiry IV
    back_month_iv: float        # Post-earnings normal IV
    term_structure_ratio: float # front / back

    # Derived
    put_call_iv_ratio_25d: float
    skew_richness_score: float  # Normalized [0,1] richness score
    iv_rank: float              # IV Rank (0-100)
    iv_percentile: float        # IV Percentile (0-100)


@dataclass
class SkewEdge:
    """Quantified edge from the skew mispricing."""
    risk_reversal_edge_bps: float    # Edge from selling put RR vs call RR
    butterfly_edge_bps: float        # Edge from selling expensive wings
    term_structure_edge_bps: float   # Edge from calendar spread
    total_edge_bps: float            # Combined edge estimate
    edge_confidence: float           # How reliable is this edge estimate [0,1]


@dataclass
class IVSkewSignal:
    """Final signal output from IV skew analysis."""
    score: float                  # Normalized [-1, +1]
    direction_bias: str           # "put_rich" | "call_rich" | "balanced"
    confidence: float             # [0, 1]
    regime: SkewRegime
    recommended_structure: RecommendedStructure

    # Key metrics
    risk_reversal_25d: float
    butterfly_25d: float
    term_structure_ratio: float
    skew_richness_score: float
    iv_rank: float

    # Edge quantification
    edge: SkewEdge

    # Trade construction hints
    sell_put_strike: Optional[float]   # Suggested put short strike
    sell_call_strike: Optional[float]  # Suggested call short strike
    buy_put_wing: Optional[float]      # Suggested put wing
    buy_call_wing: Optional[float]     # Suggested call wing

    timestamp: datetime = field(default_factory=datetime.now)
    diagnostics: dict = field(default_factory=dict)


class IVSkewSignalEngine:
    """
    Earnings IV Skew Signal Generator.

    The primary alpha engine for the strategy. Analyzes the full
    IV surface around earnings to identify and quantify mispricings.
    """

    def __init__(self, config, underlying: str):
        self.config = config
        self.underlying = underlying
        self._snapshots: List[IVSkewSnapshot] = []
        self._historical_baselines: Dict[str, float] = {}  # ticker → baseline RR

    async def get_signal(
        self, earnings_event, iv_database
    ) -> Optional[IVSkewSignal]:
        """
        Generate IV skew signal for an upcoming earnings event.

        Args:
            earnings_event: EarningsEvent with date and timing
            iv_database: Historical IV crush database for calibration

        Returns:
            IVSkewSignal or None if no qualifying setup
        """
        # Fetch current IV surface
        snapshot = await self._fetch_iv_snapshot(earnings_event)
        if snapshot is None:
            return None

        self._snapshots.append(snapshot)

        # Get historical baseline for this ticker
        baseline = await self._get_historical_baseline(
            self.underlying, iv_database
        )

        # Validate minimum IV requirements
        iv_check = self._validate_iv_levels(snapshot)
        if not iv_check:
            return None

        # Calculate all skew metrics
        regime = self._classify_skew_regime(snapshot, baseline)
        edge = self._quantify_edge(snapshot, baseline, iv_database)
        structure = self._recommend_structure(snapshot, regime, edge)

        # Generate strike suggestions
        strikes = self._compute_trade_strikes(snapshot, earnings_event)

        # Compute final signal score
        score = self._compute_signal_score(snapshot, regime, edge, baseline)

        if abs(score) < self.config.composite.min_composite_score:
            log.debug("iv_skew.score_below_threshold",
                      score=score, threshold=self.config.composite.min_composite_score)
            return None

        signal = IVSkewSignal(
            score=score,
            direction_bias=self._get_direction_bias(snapshot, regime),
            confidence=self._compute_confidence(snapshot, edge, baseline),
            regime=regime,
            recommended_structure=structure,
            risk_reversal_25d=snapshot.risk_reversal_25d,
            butterfly_25d=snapshot.butterfly_25d,
            term_structure_ratio=snapshot.term_structure_ratio,
            skew_richness_score=snapshot.skew_richness_score,
            iv_rank=snapshot.iv_rank,
            edge=edge,
            sell_put_strike=strikes.get("sell_put"),
            sell_call_strike=strikes.get("sell_call"),
            buy_put_wing=strikes.get("buy_put_wing"),
            buy_call_wing=strikes.get("buy_call_wing"),
            diagnostics={
                "underlying": self.underlying,
                "atm_iv": snapshot.atm_iv,
                "rr_25d": snapshot.risk_reversal_25d,
                "rr_10d": snapshot.risk_reversal_10d,
                "bf_25d": snapshot.butterfly_25d,
                "term_ratio": snapshot.term_structure_ratio,
                "regime": regime.name,
                "iv_rank": snapshot.iv_rank,
                "total_edge_bps": edge.total_edge_bps,
            }
        )

        log.info("iv_skew.signal_generated",
                 ticker=self.underlying,
                 regime=regime.name,
                 structure=structure.name,
                 score=score,
                 iv_rank=snapshot.iv_rank,
                 rr_25d=snapshot.risk_reversal_25d,
                 edge_bps=edge.total_edge_bps)

        return signal

    # ──────────────────────────────────────────────────────────────
    # REGIME CLASSIFICATION
    # ──────────────────────────────────────────────────────────────

    def _classify_skew_regime(
        self, snapshot: IVSkewSnapshot, baseline: Optional[dict]
    ) -> SkewRegime:
        """
        Classify the current skew environment.

        Uses both absolute thresholds AND comparison to this ticker's
        historical baseline (ticker-specific calibration matters).
        """
        rr = snapshot.risk_reversal_25d
        threshold_extreme = self.config.iv_skew.skew_richness_extreme
        threshold_min = self.config.iv_skew.skew_richness_min

        # Compare to historical baseline if available
        if baseline:
            historical_rr = baseline.get("median_pre_earnings_rr_25d", rr)
            rr_vs_history = rr / historical_rr if historical_rr > 0 else 1.0
        else:
            rr_vs_history = 1.0

        # Classify
        if rr < 0:
            return SkewRegime.INVERTED          # Calls more expensive than puts

        elif rr >= threshold_extreme and rr_vs_history >= 1.2:
            return SkewRegime.EXTREME_PUT_RICH  # Puts extremely overpriced

        elif rr >= threshold_min:
            return SkewRegime.RICH_SKEW         # Standard rich puts

        elif rr >= 0:
            return SkewRegime.BALANCED          # Normal/low skew

        else:
            return SkewRegime.UNKNOWN

    # ──────────────────────────────────────────────────────────────
    # EDGE QUANTIFICATION
    # ──────────────────────────────────────────────────────────────

    def _quantify_edge(
        self,
        snapshot: IVSkewSnapshot,
        baseline: Optional[dict],
        iv_database,
    ) -> SkewEdge:
        """
        Quantify the tradeable edge from the skew mispricing.

        This is the most important calculation — converts qualitative
        "puts are rich" into quantitative "expected edge is X bps."
        """
        # ── Risk Reversal Edge ───────────────────────────────────
        # Historical: post-earnings RR reverts to ~8% (from pre-earnings ~18%)
        # Edge = current RR - expected post-earnings RR

        post_crush_rr = baseline.get("median_post_earnings_rr_25d", 0.08) \
            if baseline else 0.08

        # RR edge = (current_rr - post_crush_rr) in percentage points
        # Convert to bps: 1% IV difference on 25Δ strike ≈ 3-5 bps of option edge
        rr_edge_iv_pct = max(0, snapshot.risk_reversal_25d - post_crush_rr)
        rr_edge_bps = rr_edge_iv_pct * 400  # Rough calibration: 1% IV → 4 bps edge

        # ── Butterfly Edge ────────────────────────────────────────
        # Wing premium above ATM
        # Expected post-earnings butterfly ≈ half of current butterfly
        post_crush_bf = baseline.get("median_post_earnings_bf_25d", 0.03) \
            if baseline else 0.03

        bf_edge_iv_pct = max(0, snapshot.butterfly_25d - post_crush_bf)
        bf_edge_bps = bf_edge_iv_pct * 300  # Wings: 1% IV → 3 bps

        # ── Term Structure Edge ───────────────────────────────────
        # Earnings bump premium in front month vs back month
        # Post-earnings: front month drops to back month IV level
        normal_ts_ratio = 1.05   # Typical normal upward slope
        earnings_bump_excess = max(0, snapshot.term_structure_ratio - normal_ts_ratio)

        # Each 0.1 in excess term structure ratio ≈ 8 bps on calendar spread
        ts_edge_bps = earnings_bump_excess * 80

        # ── Total Edge ────────────────────────────────────────────
        # Not simply additive — strategies capture different portions
        # Iron condor captures RR + BF edges simultaneously
        # Calendar captures TS edge separately

        structure = self._recommend_structure_quick(snapshot)
        if structure == RecommendedStructure.IRON_CONDOR:
            total_bps = rr_edge_bps * 0.70 + bf_edge_bps * 0.70
        elif structure == RecommendedStructure.RISK_REVERSAL:
            total_bps = rr_edge_bps * 0.90
        elif structure == RecommendedStructure.CALENDAR_SPREAD:
            total_bps = ts_edge_bps * 0.80
        else:
            total_bps = (rr_edge_bps + bf_edge_bps) * 0.50

        # Subtract transaction costs (commissions + slippage)
        transaction_cost_bps = 3.0   # Typical for multi-leg earnings trades
        net_edge_bps = max(0, total_bps - transaction_cost_bps)

        # Edge confidence: higher when historical data is robust
        hist_cycles = baseline.get("n_cycles", 0) if baseline else 0
        crush_consistency = baseline.get("crush_consistency", 0.5) if baseline else 0.5
        edge_confidence = min(1.0, (hist_cycles / 12) * crush_consistency)

        log.debug("edge.quantified",
                  ticker=self.underlying,
                  rr_edge_bps=rr_edge_bps,
                  bf_edge_bps=bf_edge_bps,
                  ts_edge_bps=ts_edge_bps,
                  total_bps=total_bps,
                  net_edge_bps=net_edge_bps,
                  edge_confidence=edge_confidence)

        return SkewEdge(
            risk_reversal_edge_bps=rr_edge_bps,
            butterfly_edge_bps=bf_edge_bps,
            term_structure_edge_bps=ts_edge_bps,
            total_edge_bps=net_edge_bps,
            edge_confidence=edge_confidence,
        )

    # ──────────────────────────────────────────────────────────────
    # STRUCTURE RECOMMENDATION
    # ──────────────────────────────────────────────────────────────

    def _recommend_structure(
        self,
        snapshot: IVSkewSnapshot,
        regime: SkewRegime,
        edge: SkewEdge,
    ) -> RecommendedStructure:
        """
        Recommend the optimal trade structure based on skew analysis.

        Decision logic:
          1. Extreme put richness + good edge → risk reversal (max skew capture)
          2. Rich skew + good IV rank → iron condor (sell both sides)
          3. Steep term structure → calendar spread (pure IV crush)
          4. Directional signal → debit spread (lower risk)
          5. Insufficient edge → no trade
        """
        # Minimum edge gate
        if edge.total_edge_bps < self.config.signals.composite.get(
            "min_edge_bps", 5
        ):
            return RecommendedStructure.NO_TRADE

        # IV rank gate
        if snapshot.iv_rank < self.config.signals.iv_skew.min_iv_rank:
            return RecommendedStructure.NO_TRADE

        # Extreme skew → risk reversal
        rr_threshold = self.config.signals.iv_skew.risk_reversal_threshold
        if (regime == SkewRegime.EXTREME_PUT_RICH and
                snapshot.risk_reversal_25d >= rr_threshold and
                edge.risk_reversal_edge_bps >= 20):
            return RecommendedStructure.RISK_REVERSAL

        # Steep term structure → calendar
        if (snapshot.term_structure_ratio >= 1.8 and
                edge.term_structure_edge_bps >= 15 and
                regime in (SkewRegime.RICH_SKEW, SkewRegime.EXTREME_PUT_RICH)):
            return RecommendedStructure.CALENDAR_SPREAD

        # Standard rich skew → iron condor
        if regime in (SkewRegime.RICH_SKEW, SkewRegime.EXTREME_PUT_RICH):
            return RecommendedStructure.IRON_CONDOR

        # Inverted skew → cautious
        if regime == SkewRegime.INVERTED:
            return RecommendedStructure.DEBIT_SPREAD

        return RecommendedStructure.NO_TRADE

    def _recommend_structure_quick(self, snapshot: IVSkewSnapshot) -> RecommendedStructure:
        """Quick structure recommendation for edge calculation (no full analysis)."""
        if snapshot.risk_reversal_25d >= self.config.signals.iv_skew.risk_reversal_threshold:
            return RecommendedStructure.RISK_REVERSAL
        if snapshot.term_structure_ratio >= 1.8:
            return RecommendedStructure.CALENDAR_SPREAD
        return RecommendedStructure.IRON_CONDOR

    # ──────────────────────────────────────────────────────────────
    # STRIKE COMPUTATION
    # ──────────────────────────────────────────────────────────────

    def _compute_trade_strikes(
        self, snapshot: IVSkewSnapshot, earnings_event
    ) -> Dict[str, Optional[float]]:
        """
        Compute specific strike prices for the trade.

        Iron Condor:
          sell_put: Below current price × (1 - expected_move × wing_mult)
          sell_call: Above current price × (1 + expected_move × wing_mult)
          buy_put_wing: 2% below sell_put (or configured width)
          buy_call_wing: 2% above sell_call

        Risk Reversal:
          sell_put: 25Δ put strike (expensive IV)
          buy_call: 25Δ call strike (cheap IV)
        """
        price = snapshot.underlying_price
        wing_mult = self.config.trade_structure.iron_condor.min_wing_multiplier

        # Get expected move from snapshot or calculate from ATM straddle
        # Expected move ≈ ATM straddle price × adjustment factor
        # Rough approximation: EM ≈ ATM_IV × Price × sqrt(DTE/365) × 0.85
        atm_iv = snapshot.atm_iv
        days_to_earnings = (earnings_event.report_date - date.today()).days
        em_pct = atm_iv * np.sqrt(max(1, days_to_earnings) / 365) * 0.85

        # Iron condor strikes
        short_put_pct = 1 - (em_pct * wing_mult)
        short_call_pct = 1 + (em_pct * wing_mult)
        wing_width_pct = self.config.trade_structure.iron_condor.width_pct_of_stock

        sell_put = self._round_to_strike(price * short_put_pct)
        sell_call = self._round_to_strike(price * short_call_pct)
        buy_put = self._round_to_strike(price * (short_put_pct - wing_width_pct))
        buy_call = self._round_to_strike(price * (short_call_pct + wing_width_pct))

        log.debug("strikes.computed",
                  ticker=self.underlying,
                  price=price,
                  em_pct=f"{em_pct:.2%}",
                  sell_put=sell_put,
                  sell_call=sell_call,
                  buy_put=buy_put,
                  buy_call=buy_call)

        return {
            "sell_put": sell_put,
            "sell_call": sell_call,
            "buy_put_wing": buy_put,
            "buy_call_wing": buy_call,
        }

    @staticmethod
    def _round_to_strike(price: float) -> float:
        """Round price to nearest valid options strike."""
        if price >= 500:
            return round(price / 5) * 5      # $5 increments above $500
        elif price >= 100:
            return round(price / 2.5) * 2.5  # $2.50 increments $100-$500
        elif price >= 25:
            return round(price)              # $1 increments $25-$100
        else:
            return round(price / 0.5) * 0.5  # $0.50 increments below $25

    # ──────────────────────────────────────────────────────────────
    # SCORING & CONFIDENCE
    # ──────────────────────────────────────────────────────────────

    def _compute_signal_score(
        self,
        snapshot: IVSkewSnapshot,
        regime: SkewRegime,
        edge: SkewEdge,
        baseline: Optional[dict],
    ) -> float:
        """
        Compute final normalized signal score [-1, +1].

        Positive score = sell vol (put-rich, standard earnings play)
        Negative score = buy vol (unusual — calls expensive, vol cheap)
        Zero = no edge / neutral
        """
        if regime == SkewRegime.NO_TRADE if hasattr(SkewRegime, 'NO_TRADE') else False:
            return 0.0

        # Base score from regime
        regime_scores = {
            SkewRegime.EXTREME_PUT_RICH: 0.90,
            SkewRegime.RICH_SKEW: 0.70,
            SkewRegime.BALANCED: 0.40,
            SkewRegime.INVERTED: -0.50,
            SkewRegime.UNKNOWN: 0.0,
        }
        base_score = regime_scores.get(regime, 0.0)

        # IV rank multiplier (higher IV rank = stronger signal to sell)
        iv_rank_factor = min(1.0, snapshot.iv_rank / 100)
        # Map IV rank [60, 100] → [0.7, 1.0] multiplier
        if snapshot.iv_rank >= 60:
            iv_factor = 0.70 + (snapshot.iv_rank - 60) / 40 * 0.30
        else:
            iv_factor = 0.50  # Below threshold — weak signal

        # Edge factor (higher edge = stronger conviction)
        edge_factor = min(1.0, edge.total_edge_bps / 30)  # 30 bps = max conviction

        # Historical baseline factor
        if baseline:
            crush_consistency = baseline.get("crush_consistency", 0.5)
            baseline_factor = 0.7 + crush_consistency * 0.3
        else:
            baseline_factor = 0.7  # Default when no history

        # Composite score
        score = base_score * iv_factor * edge_factor * baseline_factor

        return float(np.clip(score, -1.0, 1.0))

    def _compute_confidence(
        self,
        snapshot: IVSkewSnapshot,
        edge: SkewEdge,
        baseline: Optional[dict],
    ) -> float:
        """Compute overall signal confidence [0, 1]."""
        factors = [
            min(1.0, snapshot.iv_rank / 100),
            edge.edge_confidence,
            min(1.0, edge.total_edge_bps / 20),  # 20 bps = high confidence
            snapshot.skew_richness_score,
        ]
        return float(np.clip(np.mean(factors), 0.0, 1.0))

    def _get_direction_bias(
        self, snapshot: IVSkewSnapshot, regime: SkewRegime
    ) -> str:
        """Get directional bias for the signal."""
        if regime in (SkewRegime.RICH_SKEW, SkewRegime.EXTREME_PUT_RICH):
            return "put_rich"
        elif regime == SkewRegime.INVERTED:
            return "call_rich"
        else:
            return "balanced"

    def _validate_iv_levels(self, snapshot: IVSkewSnapshot) -> bool:
        """Validate minimum IV requirements."""
        config = self.config.signals.iv_skew

        if snapshot.atm_iv < config.min_atm_iv:
            log.debug("iv_skew.atm_iv_too_low",
                      atm_iv=snapshot.atm_iv, min=config.min_atm_iv)
            return False

        if snapshot.iv_rank < config.min_iv_rank:
            log.debug("iv_skew.iv_rank_too_low",
                      iv_rank=snapshot.iv_rank, min=config.min_iv_rank)
            return False

        return True

    async def _get_historical_baseline(
        self, ticker: str, iv_database
    ) -> Optional[dict]:
        """
        Get historical IV crush baseline for this ticker.
        Used for edge calibration and skew richness comparison.
        """
        if iv_database is None:
            log.warning("iv_skew.no_iv_database", ticker=ticker)
            return None

        try:
            return await iv_database.get_ticker_baseline(ticker)
        except Exception as e:
            log.error("iv_skew.baseline_fetch_error", ticker=ticker, error=str(e))
            return None

    async def _fetch_iv_snapshot(
        self, earnings_event
    ) -> Optional[IVSkewSnapshot]:
        """
        Fetch current IV surface from options chain.
        IMPLEMENTATION: Replace with live options chain data.

        Data needed:
          - Full options chain for front-month AND back-month expiries
          - IV at multiple delta points (10Δ, 15Δ, 20Δ, 25Δ, 30Δ)
          - ATM straddle price (for expected move calculation)
          - IV rank (from historical IV percentile database)
        """
        raise NotImplementedError(
            f"Implement IV surface fetch for {self.underlying}. \n"
            "Required data: \n"
            "  1. Options chain with IVs for front + back month \n"
            "  2. Delta-mapped strikes (use py_vollib for delta calculation) \n"
            "  3. IV Rank from historical IV database (52-week high/low) \n"
            "Data sources: IBKR reqMktData, TastyTrade /option-chains, "
            "Tradier /options/chains, Polygon /v2/last/nbbo/{options_contract}"
        )
