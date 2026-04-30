"""
Pin Gravity Score Engine
=========================
Quantifies the STRENGTH of the gravitational pull toward max pain.

Max pain tells us WHERE price should pin. Pin gravity tells us
HOW STRONG that pull is. Two positions can have the same max pain
distance but very different trade worthiness based on gravity strength.

Four gravity components:

  1. OI CONCENTRATION (40% weight)
     How much of total OI is clustered near max pain?
     High concentration → dealers need to hedge aggressively → strong pin
     Metric: % of total OI within ±1% of max pain strike

  2. GEX ALIGNMENT (30% weight)
     Are dealers currently positioned to support the pin?
     Positive GEX → dealers long gamma → hedging pushes price toward max pain
     Negative GEX → dealers short gamma → hedging AMPLIFIES moves AWAY from pin
     Metric: GEX magnitude and sign, distance to GEX flip

  3. PRICE MOMENTUM (15% weight)
     Is price already moving TOWARD max pain?
     Momentum toward max pain = pin effect already active
     Momentum away = trading against the flow
     Metric: Direction of intraday price move vs max pain location

  4. TERM STRUCTURE (15% weight)
     Does the IV term structure support the pin thesis?
     Steep front-end inversion → dealers expect low realized vol → pin likely
     Flat/normal term structure → no special near-expiry IV behavior
     Metric: Near-term IV vs historical realized vol ratio
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List
import numpy as np
import structlog

log = structlog.get_logger(__name__)


@dataclass
class GravityComponents:
    """Individual gravity component scores."""
    oi_concentration: float      # [0, 1]
    gex_alignment: float         # [0, 1]
    price_momentum: float        # [0, 1]
    term_structure: float        # [0, 1]

    @property
    def weighted_score(self) -> float:
        """Compute weighted composite gravity score."""
        return (
            self.oi_concentration * 0.40 +
            self.gex_alignment * 0.30 +
            self.price_momentum * 0.15 +
            self.term_structure * 0.15
        )


@dataclass
class PinGravitySignal:
    """Output from pin gravity analysis."""
    gravity_score: float         # [0, 1] — how strong is the pin pull?
    gravity_grade: str           # "A" | "B" | "C" | "D" | "F"
    components: GravityComponents
    is_tradeable: bool           # Meets minimum gravity threshold
    warnings: List[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)

    @property
    def grade(self) -> str:
        score = self.gravity_score
        if score >= 0.85:
            return "A"
        elif score >= 0.75:
            return "B"
        elif score >= 0.65:
            return "C"
        elif score >= 0.55:
            return "D"
        else:
            return "F"


class PinGravityEngine:
    """
    Pin Gravity Scoring Engine.

    Evaluates four independent signals to score the overall strength
    of the gravitational pull toward max pain on expiration day.
    """

    def __init__(self, config):
        self.config = config.signals.pin_gravity
        self.risk_config = config.signals.gex_pin

    async def score(
        self,
        max_pain_level: float,
        current_price: float,
        gex_total: float,
        gex_flip_level: float,
        total_oi: int,
        oi_within_1pct_of_max_pain: int,
        intraday_price_series: List[float],
        front_iv: float,
        back_iv: float,
        historical_realized_vol: float,
        dte: int,
    ) -> PinGravitySignal:
        """
        Calculate composite pin gravity score.

        Args:
            max_pain_level: Calculated max pain strike
            current_price: Current underlying price
            gex_total: Total GEX in dollars
            gex_flip_level: Strike where GEX crosses zero
            total_oi: Total options OI across chain
            oi_within_1pct_of_max_pain: OI concentrated near max pain
            intraday_price_series: List of intraday prices (for momentum)
            front_iv: Near-expiry ATM IV
            back_iv: Back-month ATM IV
            historical_realized_vol: 20-day realized vol
            dte: Days to expiration

        Returns:
            PinGravitySignal with composite score and components
        """
        warnings = []

        # ── Component 1: OI Concentration ────────────────────────
        oi_score = self._score_oi_concentration(
            total_oi=total_oi,
            oi_near_max_pain=oi_within_1pct_of_max_pain,
        )

        # ── Component 2: GEX Alignment ───────────────────────────
        gex_score, gex_warning = self._score_gex_alignment(
            gex_total=gex_total,
            gex_flip_level=gex_flip_level,
            current_price=current_price,
        )
        if gex_warning:
            warnings.append(gex_warning)

        # CRITICAL: Negative GEX means dealers SHORT gamma
        # Their hedging AMPLIFIES moves → pin effect is REVERSED
        # This is a trade-stopper, not just a score reducer
        if gex_total < self.risk_config.negative_gex_abort:
            log.warning("pin_gravity.negative_gex_abort",
                        gex_total=gex_total,
                        threshold=self.risk_config.negative_gex_abort)
            return PinGravitySignal(
                gravity_score=0.0,
                gravity_grade="F",
                components=GravityComponents(
                    oi_concentration=oi_score,
                    gex_alignment=0.0,
                    price_momentum=0.0,
                    term_structure=0.0,
                ),
                is_tradeable=False,
                warnings=["ABORT: Negative GEX — dealers short gamma, pin effect reversed"],
            )

        # ── Component 3: Price Momentum ───────────────────────────
        momentum_score = self._score_price_momentum(
            price_series=intraday_price_series,
            max_pain=max_pain_level,
            current_price=current_price,
        )

        # ── Component 4: Term Structure ───────────────────────────
        ts_score = self._score_term_structure(
            front_iv=front_iv,
            back_iv=back_iv,
            realized_vol=historical_realized_vol,
            dte=dte,
        )

        # ── Composite Score ───────────────────────────────────────
        components = GravityComponents(
            oi_concentration=oi_score,
            gex_alignment=gex_score,
            price_momentum=momentum_score,
            term_structure=ts_score,
        )
        composite = components.weighted_score

        # ── DTE Adjustment ────────────────────────────────────────
        # Pin gravity is stronger closer to expiry
        # Scale score UP for 0DTE/1DTE (max gravity)
        # Scale score DOWN for 5DTE (early cycle, weak gravity)
        dte_multiplier = self._get_dte_multiplier(dte)
        adjusted_score = float(np.clip(composite * dte_multiplier, 0.0, 1.0))

        is_tradeable = adjusted_score >= self.config.min_gravity_score

        if not is_tradeable:
            warnings.append(
                f"Gravity score {adjusted_score:.2f} below minimum "
                f"{self.config.min_gravity_score}"
            )

        signal = PinGravitySignal(
            gravity_score=adjusted_score,
            gravity_grade="",  # computed by property
            components=components,
            is_tradeable=is_tradeable,
            warnings=warnings,
            diagnostics={
                "oi_score": oi_score,
                "gex_score": gex_score,
                "momentum_score": momentum_score,
                "ts_score": ts_score,
                "composite_pre_dte": composite,
                "dte_multiplier": dte_multiplier,
                "final_score": adjusted_score,
                "gex_total_billions": gex_total / 1e9,
                "gex_flip": gex_flip_level,
            }
        )
        signal.gravity_grade = signal.grade  # Force compute

        log.debug("pin_gravity.scored",
                  ticker="",
                  score=adjusted_score,
                  grade=signal.gravity_grade,
                  components={
                      "oi": oi_score,
                      "gex": gex_score,
                      "momentum": momentum_score,
                      "ts": ts_score,
                  })

        return signal

    # ──────────────────────────────────────────────────────────────
    # COMPONENT SCORERS
    # ──────────────────────────────────────────────────────────────

    def _score_oi_concentration(
        self, total_oi: int, oi_near_max_pain: int
    ) -> float:
        """
        Score OI concentration at max pain.

        High concentration → many dealers with large hedges at this strike
        → stronger and more persistent pinning effect
        """
        if total_oi == 0:
            return 0.0

        concentration_pct = oi_near_max_pain / total_oi
        threshold = self.config.oi_at_max_pain_threshold_pct

        # Linear scoring from 0 to 2× threshold
        # At threshold: score = 0.70 (decent)
        # At 2× threshold: score = 1.0 (maximum)
        if concentration_pct >= threshold * 2:
            score = 1.0
        elif concentration_pct >= threshold:
            # Linear interpolation from 0.70 to 1.0
            score = 0.70 + (concentration_pct - threshold) / threshold * 0.30
        elif concentration_pct >= threshold * 0.5:
            # Below threshold: scale from 0.30 to 0.70
            score = 0.30 + (concentration_pct / threshold) * 0.40
        else:
            score = float(concentration_pct / (threshold * 0.5)) * 0.30

        return float(np.clip(score, 0.0, 1.0))

    def _score_gex_alignment(
        self,
        gex_total: float,
        gex_flip_level: float,
        current_price: float,
    ) -> tuple[float, Optional[str]]:
        """
        Score GEX alignment with pin thesis.

        Positive GEX = dealers long gamma = their hedging stabilizes price = pin supportive
        Near GEX flip = unstable, may flip from stabilizing to amplifying
        """
        warning = None
        config = self.risk_config

        # Check GEX flip proximity
        flip_distance_bps = abs(current_price - gex_flip_level) / current_price * 10000
        near_flip_threshold = config.gex_flip_proximity_bps

        if flip_distance_bps < near_flip_threshold:
            warning = f"Price within {flip_distance_bps:.1f}bps of GEX flip — pin unreliable"
            return 0.20, warning  # Very low score near flip

        # Score based on GEX magnitude and sign
        min_gex = config.min_gex_for_pin_confirmation

        if gex_total >= min_gex * 2:
            # Very strong positive GEX → maximum pin support
            score = 1.0
        elif gex_total >= min_gex:
            # Good positive GEX
            score = 0.75 + (gex_total - min_gex) / min_gex * 0.25
        elif gex_total > 0:
            # Positive but below threshold — weak support
            score = 0.40 + (gex_total / min_gex) * 0.35
        else:
            # Negative GEX — already handled as abort above threshold
            score = max(0.0, 0.40 + gex_total / abs(config.negative_gex_abort) * 0.40)

        # Penalize proximity to flip
        flip_penalty = max(0, 1 - flip_distance_bps / 30) * 0.20
        score = float(np.clip(score - flip_penalty, 0.0, 1.0))

        return score, warning

    def _score_price_momentum(
        self,
        price_series: List[float],
        max_pain: float,
        current_price: float,
    ) -> float:
        """
        Score intraday price momentum toward max pain.

        If price is already drifting toward max pain, the pin effect
        is already active → stronger signal and higher probability of success.
        """
        if len(price_series) < 5:
            return 0.50  # Neutral when insufficient data

        recent_prices = price_series[-10:]  # Last 10 price bars
        early_price = recent_prices[0]

        # Are we moving toward or away from max pain?
        distance_start = max_pain - early_price      # + if max_pain above start
        distance_current = max_pain - current_price   # + if max_pain above current

        # If distance_current < distance_start → moving TOWARD max pain → bullish
        # If distance_current > distance_start → moving AWAY → bearish for pin
        if abs(distance_start) < 0.01:
            return 0.60  # Already near max pain — neutral on momentum

        momentum_ratio = abs(distance_current) / abs(distance_start)

        # Momentum ratio < 1.0 = price moved toward max pain
        # Momentum ratio > 1.0 = price moved away from max pain
        if momentum_ratio < 0.5:
            score = 1.0   # Moving strongly toward pin
        elif momentum_ratio < 0.8:
            score = 0.80
        elif momentum_ratio < 1.0:
            score = 0.65  # Slight movement toward pin
        elif momentum_ratio < 1.2:
            score = 0.40  # Slight movement away
        elif momentum_ratio < 1.5:
            score = 0.25  # Moving away from pin
        else:
            score = 0.10  # Strongly moving away — weak pin signal

        # Also check price volatility (choppy price = hard to pin)
        price_std = float(np.std(recent_prices)) / current_price
        if price_std > 0.005:   # > 0.5% intraday std dev → choppy
            score *= 0.80

        return float(np.clip(score, 0.0, 1.0))

    def _score_term_structure(
        self,
        front_iv: float,
        back_iv: float,
        realized_vol: float,
        dte: int,
    ) -> float:
        """
        Score IV term structure for pin supportiveness.

        Near expiry, a strongly inverted term structure (front IV >> back IV)
        suggests the market is pricing in near-term stability after the expiry
        event — consistent with a pinning scenario.

        Also: front IV vs realized vol ratio tells us if near-expiry options
        are "overpriced" relative to recent realized moves — selling premium
        is more attractive when this ratio is high.
        """
        if back_iv == 0 or front_iv == 0:
            return 0.50

        # Term structure inversion score
        # Front IV / Back IV: >1.0 = inverted (normal near expiry), <1.0 = upward slope
        ts_ratio = front_iv / back_iv

        if ts_ratio >= 1.4:
            ts_score = 1.0     # Strongly inverted — high conviction
        elif ts_ratio >= 1.2:
            ts_score = 0.80
        elif ts_ratio >= 1.0:
            ts_score = 0.60    # Slight inversion — normal
        else:
            ts_score = 0.30    # Upward sloping — unusual near expiry

        # IV vs realized vol ratio
        # If front IV >> realized vol → options are rich → selling premium is profitable
        if realized_vol > 0:
            iv_rv_ratio = front_iv / realized_vol
            if iv_rv_ratio >= 1.5:
                rv_score = 1.0
            elif iv_rv_ratio >= 1.2:
                rv_score = 0.75
            elif iv_rv_ratio >= 1.0:
                rv_score = 0.50
            else:
                rv_score = 0.20    # IV below realized — premium is cheap
        else:
            rv_score = 0.50

        # DTE adjustment: inverted TS is more meaningful close to expiry
        dte_relevance = 1.0 if dte <= 2 else 0.80 if dte <= 3 else 0.60

        combined = (ts_score * 0.60 + rv_score * 0.40) * dte_relevance
        return float(np.clip(combined, 0.0, 1.0))

    def _get_dte_multiplier(self, dte: int) -> float:
        """
        DTE-based gravity multiplier.
        Pin gravity strengthens dramatically as expiry approaches.

        DTE 0: 1.25× (strongest — apply on 0DTE with caution due to gamma)
        DTE 1: 1.15×
        DTE 2: 1.00× (baseline)
        DTE 3: 0.88×
        DTE 4: 0.75×
        DTE 5: 0.65×
        """
        multipliers = {0: 1.25, 1: 1.15, 2: 1.00, 3: 0.88, 4: 0.75, 5: 0.65}
        return multipliers.get(dte, 0.60)  # Default 0.60 for DTE > 5
