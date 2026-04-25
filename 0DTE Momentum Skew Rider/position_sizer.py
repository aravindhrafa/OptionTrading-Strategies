"""
Position Sizer — Kelly Criterion + Volatility-Adjusted Sizing
=============================================================
Determines correct contract count for each trade using:

1. Kelly Criterion (fractional): Maximizes long-run geometric growth
2. Volatility scaling: Reduce size in high-vol environments
3. Confidence weighting: Scale with signal quality
4. Hard capital caps: Never exceed absolute limits

Kelly Formula:
  f* = (p × b - q) / b
  where:
    f* = fraction of bankroll to bet
    p  = probability of winning
    q  = probability of losing (1 - p)
    b  = net odds (profit/loss ratio)

Critical: Always use FRACTIONAL Kelly (0.25× to 0.5×).
Full Kelly is optimal only in theory — in practice it's
catastrophically sensitive to edge estimation errors.
We use 0.25× Kelly as our default (quarter Kelly).

Reference: Ed Thorp, "Beat the Dealer" and "A Man for All Markets"
"""

import math
from dataclasses import dataclass
from typing import Optional
import structlog

log = structlog.get_logger(__name__)


@dataclass
class SizedTrade:
    """Trade with determined contract count."""
    proposal: object       # Original TradeProposal
    contracts: int
    structure: str
    direction: str
    max_risk: float        # Total max risk in dollars
    kelly_fraction: float  # What fraction of Kelly was used
    sizing_rationale: str  # Human-readable sizing explanation
    underlying: str
    # Options legs
    long_strike: Optional[float] = None
    short_strike: Optional[float] = None
    long_wing_strike: Optional[float] = None
    short_wing_strike: Optional[float] = None
    expiry: Optional[str] = None


class PositionSizer:
    """
    Volatility-adjusted Kelly position sizer for 0DTE options.
    """

    def __init__(self, config, risk_limits):
        self.config = config
        self.risk_limits = risk_limits

    def size(
        self,
        proposal,
        portfolio,
        kelly_fraction: float = 0.25,
    ) -> SizedTrade:
        """
        Calculate position size for a trade proposal.

        Args:
            proposal: TradeProposal with structure and risk metrics
            portfolio: Current portfolio state
            kelly_fraction: Fraction of full Kelly to use (default 0.25)

        Returns:
            SizedTrade with validated contract count
        """
        portfolio_value = portfolio.total_value

        # ── Step 1: Kelly-based sizing ───────────────────────────
        kelly_contracts = self._kelly_size(
            proposal=proposal,
            portfolio_value=portfolio_value,
            kelly_fraction=kelly_fraction,
        )

        # ── Step 2: Capital cap ──────────────────────────────────
        max_capital_pct = self.risk_limits.portfolio_risk.max_per_trade_capital_pct
        max_capital = portfolio_value * max_capital_pct
        max_risk_per_contract = getattr(proposal, 'max_risk_per_contract', 100)

        if max_risk_per_contract > 0:
            capital_cap_contracts = max(1, int(max_capital / max_risk_per_contract))
        else:
            capital_cap_contracts = kelly_contracts

        # ── Step 3: Volatility adjustment ───────────────────────
        vol_factor = self._volatility_adjustment(proposal)

        # ── Step 4: Signal confidence scaling ───────────────────
        confidence_factor = self._confidence_scaling(proposal.signal.confidence)

        # ── Step 5: Combine all factors ─────────────────────────
        raw_contracts = min(kelly_contracts, capital_cap_contracts)
        adjusted_contracts = raw_contracts * vol_factor * confidence_factor

        # ── Step 6: Hard limits ──────────────────────────────────
        max_hard_limit = self.risk_limits.execution_risk.max_order_size_contracts
        final_contracts = max(0, min(
            int(adjusted_contracts),
            max_hard_limit
        ))

        # ── Step 7: Minimum size check ───────────────────────────
        if final_contracts < 1:
            log.debug("sizing.zero_contracts",
                      kelly=kelly_contracts,
                      capital_cap=capital_cap_contracts,
                      vol_factor=vol_factor,
                      confidence_factor=confidence_factor)
            return SizedTrade(
                proposal=proposal,
                contracts=0,
                structure=proposal.structure,
                direction=proposal.direction,
                max_risk=0,
                kelly_fraction=kelly_fraction,
                sizing_rationale="Insufficient edge for minimum 1 contract",
                underlying=proposal.underlying,
            )

        total_max_risk = final_contracts * max_risk_per_contract

        rationale = (
            f"Kelly={kelly_contracts} → CapCap={capital_cap_contracts} → "
            f"VolAdj(×{vol_factor:.2f})={int(raw_contracts * vol_factor)} → "
            f"ConfAdj(×{confidence_factor:.2f})={int(adjusted_contracts)} → "
            f"Final={final_contracts} contracts"
        )

        log.info("sizing.determined",
                 contracts=final_contracts,
                 max_risk=total_max_risk,
                 kelly_fraction=kelly_fraction,
                 vol_factor=vol_factor,
                 confidence_factor=confidence_factor,
                 rationale=rationale)

        return SizedTrade(
            proposal=proposal,
            contracts=final_contracts,
            structure=proposal.structure,
            direction=proposal.direction,
            max_risk=total_max_risk,
            kelly_fraction=kelly_fraction,
            sizing_rationale=rationale,
            underlying=proposal.underlying,
            long_strike=getattr(proposal, 'long_strike', None),
            short_strike=getattr(proposal, 'short_strike', None),
            long_wing_strike=getattr(proposal, 'long_wing_strike', None),
            short_wing_strike=getattr(proposal, 'short_wing_strike', None),
            expiry=getattr(proposal, 'expiry', None),
        )

    def _kelly_size(
        self, proposal, portfolio_value: float, kelly_fraction: float
    ) -> int:
        """
        Calculate Kelly-optimal contract count.

        Converts Kelly fraction of bankroll to number of contracts,
        using the trade's max_risk as the "bet size".
        """
        # Extract edge statistics from signal
        # In production: calibrate from backtested edge statistics
        signal_score = abs(proposal.signal.score)
        confidence = proposal.signal.confidence

        # Estimate win probability from signal score and confidence
        # Score of 1.0 with 100% confidence → ~65% win rate (calibrated)
        # Score of 0.7 with 70% confidence → ~55% win rate
        base_win_rate = 0.50
        win_rate_boost = signal_score * confidence * 0.20
        p_win = min(0.70, base_win_rate + win_rate_boost)
        p_lose = 1.0 - p_win

        # Estimate profit/loss ratio from structure
        structure = proposal.structure
        risk_reward = getattr(proposal, 'risk_reward', None)

        if risk_reward and risk_reward > 0:
            b = risk_reward  # Use actual risk/reward from proposal
        else:
            # Default by structure type
            structure_rr = {
                "debit_spread": 1.5,      # Risk $1 to make $1.50
                "iron_condor": 0.8,       # Risk $1 to make $0.80 (premium)
                "long_straddle": 2.0,     # Risk $1 to make $2.00
            }
            b = structure_rr.get(structure, 1.0)

        # Kelly formula: f* = (p*b - q) / b
        kelly_full = (p_win * b - p_lose) / b

        if kelly_full <= 0:
            log.debug("sizing.negative_kelly",
                      p_win=p_win, b=b, kelly=kelly_full)
            return 0

        # Apply fractional Kelly
        kelly_applied = kelly_full * kelly_fraction

        # Convert to dollar amount, then to contracts
        kelly_dollars = portfolio_value * kelly_applied
        max_risk_per_contract = getattr(proposal, 'max_risk_per_contract', 100)

        if max_risk_per_contract <= 0:
            return 1

        contracts = max(1, int(kelly_dollars / max_risk_per_contract))

        log.debug("sizing.kelly_calc",
                  p_win=p_win, b=b, kelly_full=kelly_full,
                  kelly_applied=kelly_applied, kelly_dollars=kelly_dollars,
                  contracts=contracts)

        return contracts

    def _volatility_adjustment(self, proposal) -> float:
        """
        Reduce size in high-volatility environments.
        0DTE options become extremely dangerous when VIX is elevated.

        Returns a multiplier [0.25, 1.0]:
          VIX < 15: full size (1.0)
          VIX 15-20: 0.85x
          VIX 20-25: 0.70x
          VIX 25-30: 0.50x
          VIX 30-35: 0.35x
          VIX > 35:  0.25x (circuit breaker should halt before this)
        """
        current_vix = getattr(proposal, 'current_vix', None)
        if current_vix is None:
            log.warning("sizing.no_vix_data_using_default")
            return 0.75  # Conservative default when VIX unknown

        if current_vix < 15:
            return 1.0
        elif current_vix < 20:
            return 0.85
        elif current_vix < 25:
            return 0.70
        elif current_vix < 30:
            return 0.50
        elif current_vix < 35:
            return 0.35
        else:
            return 0.25

    def _confidence_scaling(self, confidence: float) -> float:
        """
        Scale position size with signal confidence.
        Low confidence = smaller size.

        Confidence [0.70, 1.0] → Factor [0.50, 1.0]
        (We never trade with confidence < 0.70 due to signal threshold)
        """
        if confidence >= 0.90:
            return 1.0
        elif confidence >= 0.80:
            return 0.80
        elif confidence >= 0.70:
            return 0.60
        else:
            return 0.50
