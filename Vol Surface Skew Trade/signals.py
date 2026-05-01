"""
signals.py
==========
Signal generation and trade leg construction for the Vol Surface Skew Trade.

Strategy Taxonomy
-----------------
The strategy trades *relative value* on the implied-volatility surface.
It never bets on direction — only on mispricing between surface nodes.

Three trade structures are supported:

1. Risk-Reversal (RR) Fade
   ─────────────────────────
   When the 25Δ RR is anomalously steep (put skew too rich):
     • Sell 25Δ put  (sell expensive skew)
     • Buy  25Δ call (buy cheap skew)
   Delta-hedged at entry with underlying.

2. Butterfly Fade
   ──────────────
   When 25Δ butterfly is anomalously rich (wings overpriced vs ATM):
     • Sell  25Δ call  +  Sell  25Δ put
     • Buy 2× ATM straddle
   Vega-neutral at entry; profits from vol surface flattening.

3. Calendar Skew Spread
   ──────────────────────
   When the skew slope differs across expiries (term-structure dislocation):
     • Sell skew on the richer expiry
     • Buy  skew on the cheaper expiry
   Gamma and vega exposures partially offset.

All structures are sized by the RiskManager (risk/manager.py) before
returning executable TradeLegs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from strategy.vol_surface import VolSurface, SkewMetrics
from strategy.pricing import bsm_price, delta as bsm_delta, gamma, vega, theta, vanna, volga


# ── enums ─────────────────────────────────────────────────────────────────────

class TradeType(Enum):
    RR_FADE         = "risk_reversal_fade"
    BUTTERFLY_FADE  = "butterfly_fade"
    CALENDAR_SKEW   = "calendar_skew_spread"
    NO_TRADE        = "no_trade"


class Side(Enum):
    LONG  = +1
    SHORT = -1


# ── leg-level data ────────────────────────────────────────────────────────────

@dataclass
class OptionLeg:
    """
    One option leg inside a multi-leg structure.

    Attributes
    ----------
    flag         : 'c' or 'p'
    strike       : K
    expiry       : T in years
    side         : LONG (+1) or SHORT (−1)
    num_contracts: position size (will be set by RiskManager)
    entry_iv     : σ at trade entry
    entry_price  : BSM fair value at entry
    greeks       : {delta, gamma, vega, theta, vanna, volga} per contract × 100 shares
    """
    flag: str
    strike: float
    expiry: float
    side: Side
    num_contracts: int = 0
    entry_iv: float = 0.0
    entry_price: float = 0.0
    greeks: dict = field(default_factory=dict)

    def signed_greeks(self) -> dict:
        """Greeks scaled by side and contracts (positive = long exposure)."""
        mult = self.side.value * self.num_contracts * 100  # 100 shares/contract
        return {k: v * mult for k, v in self.greeks.items()}


@dataclass
class TradeSignal:
    """
    Executable trade signal with legs, hedge, and metadata.

    Attributes
    ----------
    trade_type   : which structure
    legs         : list of OptionLeg
    hedge_delta  : underlying shares to sell (negative = short)
    signal_score : composite strength [-1, +1]
    rationale    : human-readable reasoning string
    expiry       : primary expiry targeted
    confidence   : 0..1 (based on z-score magnitude)
    """
    trade_type: TradeType
    legs: list[OptionLeg]
    hedge_delta: float            # underlying shares for delta hedge
    signal_score: float
    rationale: str
    expiry: float
    confidence: float = 0.0
    max_contracts: int = 0        # set by RiskManager

    def net_greeks(self) -> dict:
        """Aggregate portfolio greeks across all legs."""
        keys = ["delta", "gamma", "vega", "theta", "vanna", "volga"]
        total = {k: 0.0 for k in keys}
        for leg in self.legs:
            for k, v in leg.signed_greeks().items():
                total[k] += v
        # Add hedge delta
        total["delta"] += self.hedge_delta
        return total

    def is_valid(self) -> bool:
        return self.trade_type != TradeType.NO_TRADE and len(self.legs) > 0

    def description(self) -> str:
        lines = [
            f"Trade  : {self.trade_type.value}",
            f"Score  : {self.signal_score:+.3f}  |  Confidence: {self.confidence:.1%}",
            f"Expiry : {self.expiry * 365:.0f}d",
            f"Rationale: {self.rationale}",
            "Legs:",
        ]
        for leg in self.legs:
            lines.append(
                f"  {'LONG' if leg.side == Side.LONG else 'SHORT':5s} "
                f"{leg.num_contracts:3d}× "
                f"{'CALL' if leg.flag == 'c' else 'PUT ':4s} "
                f"K={leg.strike:.1f}  T={leg.expiry*365:.0f}d  "
                f"IV={leg.entry_iv*100:.1f}%  Px={leg.entry_price:.3f}"
            )
        lines.append(f"  Hedge: {self.hedge_delta:+.1f} shares underlying")
        return "\n".join(lines)


# ── Greek calculator helper ───────────────────────────────────────────────────

def _compute_greeks(spot: float, K: float, T: float, r: float, iv: float, flag: str) -> dict:
    return {
        "delta":  bsm_delta(spot, K, T, r, iv, flag),
        "gamma":  gamma(spot, K, T, r, iv),
        "vega":   vega(spot, K, T, r, iv),
        "theta":  theta(spot, K, T, r, iv, flag),
        "vanna":  vanna(spot, K, T, r, iv),
        "volga":  volga(spot, K, T, r, iv),
    }


# ── signal generator ──────────────────────────────────────────────────────────

class SkewSignalGenerator:
    """
    Analyses the VolSurface and emits TradeSignal objects.

    Parameters
    ----------
    surface          : calibrated VolSurface
    rate             : risk-free rate
    rr_zscore_thresh : minimum |z-score| on RR25 to trigger RR trade
    bf_zscore_thresh : minimum |z-score| on BF25 to trigger butterfly trade
    min_confidence   : minimum signal_score magnitude to emit a signal
    """

    def __init__(
        self,
        surface: VolSurface,
        rate: float = 0.05,
        rr_zscore_thresh: float = 1.5,
        bf_zscore_thresh: float = 1.5,
        min_confidence: float = 0.30,
    ):
        self.surface = surface
        self.rate = rate
        self.rr_thresh = rr_zscore_thresh
        self.bf_thresh = bf_zscore_thresh
        self.min_confidence = min_confidence

    # ── public entry-point ────────────────────────────────────────────────────

    def generate(
        self,
        expiry: float,
        history: pd.DataFrame,
        second_expiry: Optional[float] = None,
    ) -> TradeSignal:
        """
        Main signal factory.  Returns the highest-conviction trade available
        for the given expiry (and optional second_expiry for calendar trades).

        Parameters
        ----------
        expiry        : primary expiry in years
        history       : DataFrame with columns [date, rr25, bf25, atm_iv]
                        used to compute z-scores
        second_expiry : if supplied, also evaluate calendar skew trade
        """
        metrics = self.surface.skew_metrics(expiry, history)
        score = metrics.signal_strength()

        if abs(score) < self.min_confidence:
            return TradeSignal(
                trade_type=TradeType.NO_TRADE, legs=[], hedge_delta=0.0,
                signal_score=score, rationale="Score below threshold",
                expiry=expiry, confidence=0.0,
            )

        # Priority: RR > BF > Calendar
        if abs(metrics.rr25_zscore) >= self.rr_thresh:
            return self._rr_trade(metrics)

        if abs(metrics.bf25_zscore) >= self.bf_thresh and second_expiry is None:
            return self._butterfly_trade(metrics)

        if second_expiry is not None:
            metrics2 = self.surface.skew_metrics(second_expiry, history)
            if abs(metrics.rr25_zscore - metrics2.rr25_zscore) >= 1.0:
                return self._calendar_trade(metrics, metrics2, second_expiry)

        return self._butterfly_trade(metrics)

    # ── trade builders ────────────────────────────────────────────────────────

    def _rr_trade(self, m: SkewMetrics) -> TradeSignal:
        """
        Risk-Reversal Fade.
        If RR < −1.5σ: put skew anomalously rich → sell puts, buy calls.
        If RR > +1.5σ: call skew anomalously rich → sell calls, buy puts.
        """
        spot = self.surface.spot
        T = m.expiry

        k_c25 = self.surface.delta_to_strike(0.25, T, "c")
        k_p25 = self.surface.delta_to_strike(-0.25, T, "p")
        iv_c25 = self.surface.iv_at(k_c25, T)
        iv_p25 = self.surface.iv_at(k_p25, T)

        sell_puts = m.rr25_zscore < 0   # puts rich → sell puts, buy calls

        if sell_puts:
            # Leg 1: Sell 25Δ put
            leg_short = OptionLeg(
                flag="p", strike=k_p25, expiry=T, side=Side.SHORT,
                entry_iv=iv_p25,
                entry_price=bsm_price(spot, k_p25, T, self.rate, iv_p25, "p"),
                greeks=_compute_greeks(spot, k_p25, T, self.rate, iv_p25, "p"),
            )
            # Leg 2: Buy 25Δ call
            leg_long = OptionLeg(
                flag="c", strike=k_c25, expiry=T, side=Side.LONG,
                entry_iv=iv_c25,
                entry_price=bsm_price(spot, k_c25, T, self.rate, iv_c25, "c"),
                greeks=_compute_greeks(spot, k_c25, T, self.rate, iv_c25, "c"),
            )
            rationale = (
                f"Put skew {m.rr25_zscore:+.2f}σ rich (RR={m.rr25*100:.2f}%). "
                f"Sell 25Δ put K={k_p25:.1f} IV={iv_p25*100:.1f}%, "
                f"buy 25Δ call K={k_c25:.1f} IV={iv_c25*100:.1f}%."
            )
        else:
            # Leg 1: Sell 25Δ call
            leg_short = OptionLeg(
                flag="c", strike=k_c25, expiry=T, side=Side.SHORT,
                entry_iv=iv_c25,
                entry_price=bsm_price(spot, k_c25, T, self.rate, iv_c25, "c"),
                greeks=_compute_greeks(spot, k_c25, T, self.rate, iv_c25, "c"),
            )
            # Leg 2: Buy 25Δ put
            leg_long = OptionLeg(
                flag="p", strike=k_p25, expiry=T, side=Side.LONG,
                entry_iv=iv_p25,
                entry_price=bsm_price(spot, k_p25, T, self.rate, iv_p25, "p"),
                greeks=_compute_greeks(spot, k_p25, T, self.rate, iv_p25, "p"),
            )
            rationale = (
                f"Call skew {m.rr25_zscore:+.2f}σ rich (RR={m.rr25*100:.2f}%). "
                f"Sell 25Δ call K={k_c25:.1f} IV={iv_c25*100:.1f}%, "
                f"buy 25Δ put K={k_p25:.1f} IV={iv_p25*100:.1f}%."
            )

        legs = [leg_short, leg_long]
        # Delta hedge: flatten net delta with underlying (1 contract assumed, scaled later)
        net_delta = (
            leg_short.side.value * leg_short.greeks["delta"] +
            leg_long.side.value * leg_long.greeks["delta"]
        ) * 100
        hedge = -net_delta

        confidence = min(abs(m.rr25_zscore) / 3.0, 1.0)

        return TradeSignal(
            trade_type=TradeType.RR_FADE,
            legs=legs,
            hedge_delta=hedge,
            signal_score=m.signal_strength(),
            rationale=rationale,
            expiry=T,
            confidence=confidence,
        )

    def _butterfly_trade(self, m: SkewMetrics) -> TradeSignal:
        """
        Butterfly Fade.
        Wings rich → sell 25Δ strangle, buy 2× ATM straddle.
        Wings cheap → reverse.
        """
        spot = self.surface.spot
        T = m.expiry

        k_c25 = self.surface.delta_to_strike(0.25, T, "c")
        k_p25 = self.surface.delta_to_strike(-0.25, T, "p")
        iv_c25 = self.surface.iv_at(k_c25, T)
        iv_p25 = self.surface.iv_at(k_p25, T)
        iv_atm = m.atm_iv

        wings_rich = m.bf25_zscore > 0

        if wings_rich:
            side_wings, side_atm = Side.SHORT, Side.LONG
            rationale = (
                f"BF25 {m.bf25_zscore:+.2f}σ rich (BF={m.bf25*100:.2f}%). "
                "Sell 25Δ strangle, buy ATM straddle."
            )
        else:
            side_wings, side_atm = Side.LONG, Side.SHORT
            rationale = (
                f"BF25 {m.bf25_zscore:+.2f}σ cheap (BF={m.bf25*100:.2f}%). "
                "Buy 25Δ strangle, sell ATM straddle."
            )

        legs = [
            OptionLeg(flag="c", strike=k_c25, expiry=T, side=side_wings,
                      entry_iv=iv_c25,
                      entry_price=bsm_price(spot, k_c25, T, self.rate, iv_c25, "c"),
                      greeks=_compute_greeks(spot, k_c25, T, self.rate, iv_c25, "c")),
            OptionLeg(flag="p", strike=k_p25, expiry=T, side=side_wings,
                      entry_iv=iv_p25,
                      entry_price=bsm_price(spot, k_p25, T, self.rate, iv_p25, "p"),
                      greeks=_compute_greeks(spot, k_p25, T, self.rate, iv_p25, "p")),
            # 2× ATM call
            OptionLeg(flag="c", strike=spot, expiry=T, side=side_atm,
                      num_contracts=2,
                      entry_iv=iv_atm,
                      entry_price=bsm_price(spot, spot, T, self.rate, iv_atm, "c"),
                      greeks=_compute_greeks(spot, spot, T, self.rate, iv_atm, "c")),
            # 2× ATM put
            OptionLeg(flag="p", strike=spot, expiry=T, side=side_atm,
                      num_contracts=2,
                      entry_iv=iv_atm,
                      entry_price=bsm_price(spot, spot, T, self.rate, iv_atm, "p"),
                      greeks=_compute_greeks(spot, spot, T, self.rate, iv_atm, "p")),
        ]

        confidence = min(abs(m.bf25_zscore) / 2.5, 1.0)

        return TradeSignal(
            trade_type=TradeType.BUTTERFLY_FADE,
            legs=legs,
            hedge_delta=0.0,    # butterfly is approximately delta-neutral
            signal_score=m.signal_strength(),
            rationale=rationale,
            expiry=T,
            confidence=confidence,
        )

    def _calendar_trade(
        self,
        m_near: SkewMetrics,
        m_far: SkewMetrics,
        far_expiry: float,
    ) -> TradeSignal:
        """
        Calendar Skew Spread.
        Sell skew on the expiry where it's richer, buy on cheaper.
        """
        spot = self.surface.spot
        T_near, T_far = m_near.expiry, m_far.expiry

        k_p_near = self.surface.delta_to_strike(-0.25, T_near, "p")
        k_p_far  = self.surface.delta_to_strike(-0.25, T_far,  "p")
        iv_p_near = self.surface.iv_at(k_p_near, T_near)
        iv_p_far  = self.surface.iv_at(k_p_far, T_far)

        near_richer = m_near.rr25_zscore < m_far.rr25_zscore

        if near_richer:
            sell_T, sell_K, sell_iv = T_near, k_p_near, iv_p_near
            buy_T,  buy_K,  buy_iv  = T_far,  k_p_far,  iv_p_far
        else:
            sell_T, sell_K, sell_iv = T_far,  k_p_far,  iv_p_far
            buy_T,  buy_K,  buy_iv  = T_near, k_p_near, iv_p_near

        legs = [
            OptionLeg(flag="p", strike=sell_K, expiry=sell_T, side=Side.SHORT,
                      entry_iv=sell_iv,
                      entry_price=bsm_price(spot, sell_K, sell_T, self.rate, sell_iv, "p"),
                      greeks=_compute_greeks(spot, sell_K, sell_T, self.rate, sell_iv, "p")),
            OptionLeg(flag="p", strike=buy_K, expiry=buy_T, side=Side.LONG,
                      entry_iv=buy_iv,
                      entry_price=bsm_price(spot, buy_K, buy_T, self.rate, buy_iv, "p"),
                      greeks=_compute_greeks(spot, buy_K, buy_T, self.rate, buy_iv, "p")),
        ]

        rr_diff = abs(m_near.rr25_zscore - m_far.rr25_zscore)
        confidence = min(rr_diff / 2.0, 1.0)

        return TradeSignal(
            trade_type=TradeType.CALENDAR_SKEW,
            legs=legs,
            hedge_delta=0.0,
            signal_score=m_near.signal_strength() - m_far.signal_strength(),
            rationale=(
                f"Calendar skew dislocation: near RR z={m_near.rr25_zscore:+.2f}σ "
                f"vs far RR z={m_far.rr25_zscore:+.2f}σ."
            ),
            expiry=T_near,
            confidence=confidence,
        )
