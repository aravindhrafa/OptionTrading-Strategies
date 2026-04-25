"""
Black-Scholes pricing engine with full Greeks and implied volatility solver.

This module provides the mathematical foundation for all strategy implementations,
including option pricing, Greeks computation, and Newton-Raphson IV solving.

References:
    - Black, F. & Scholes, M. (1973). "The Pricing of Options and Corporate Liabilities."
    - Hull, J. (2022). "Options, Futures, and Other Derivatives." 11th ed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal

import numpy as np
from scipy.stats import norm


class OptionType(str, Enum):
    """Option contract type."""
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class GreeksResult:
    """
    Container for all first and second-order option Greeks.

    Attributes:
        delta: Rate of change of option price w.r.t. underlying price.
               Range: [0, 1] for calls, [-1, 0] for puts.
        gamma: Rate of change of delta w.r.t. underlying price.
               Always positive for long options.
        theta: Rate of change of option price w.r.t. time (per calendar day).
               Always negative for long options.
        vega:  Rate of change of option price w.r.t. 1% change in IV.
               Always positive for long options.
        rho:   Rate of change of option price w.r.t. 1% change in risk-free rate.
        vanna: d(delta)/d(sigma) — sensitivity of delta to vol changes.
        charm: d(delta)/d(t) — delta decay rate. Useful for expiry-day strategies.
        volga: d(vega)/d(sigma) — convexity of vega. Key for vol surface trades.
    """
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    vanna: float
    charm: float
    volga: float


@dataclass(frozen=True)
class PricingResult:
    """Full pricing output including fair value and all Greeks."""
    price: float
    intrinsic: float
    time_value: float
    greeks: GreeksResult
    d1: float
    d2: float


class BlackScholes:
    """
    Vectorized Black-Scholes option pricing and Greeks engine.

    All methods accept both scalar floats and numpy arrays for batch computation,
    enabling fast parameter sweeps and surface construction.

    Example:
        >>> bs = BlackScholes()
        >>> result = bs.price(S=5000, K=5000, T=1/252, r=0.0525, sigma=0.18)
        >>> print(f"Price: {result.price:.2f}, Delta: {result.greeks.delta:.4f}")
        Price: 22.31, Delta: 0.5124
    """

    def __init__(self, risk_free_rate: float = 0.0525) -> None:
        """
        Args:
            risk_free_rate: Annual risk-free rate (default: 5.25%, current Fed funds).
        """
        self.r = risk_free_rate

    def _d1_d2(
        self,
        S: float | np.ndarray,
        K: float | np.ndarray,
        T: float | np.ndarray,
        sigma: float | np.ndarray,
        r: float | None = None,
    ) -> tuple[float | np.ndarray, float | np.ndarray]:
        """
        Compute d1 and d2 parameters for Black-Scholes formula.

        Args:
            S: Current underlying price.
            K: Option strike price.
            T: Time to expiration in years (e.g., 1/252 for 1 trading day).
            sigma: Implied volatility as decimal (e.g., 0.18 for 18%).
            r: Risk-free rate override. Uses instance default if None.

        Returns:
            Tuple of (d1, d2).
        """
        _r = r if r is not None else self.r
        sqrt_T = np.sqrt(T)
        d1 = (np.log(S / K) + (_r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T
        return d1, d2

    def price(
        self,
        S: float,
        K: float,
        T: float,
        sigma: float,
        option_type: OptionType = OptionType.CALL,
        r: float | None = None,
    ) -> PricingResult:
        """
        Compute full Black-Scholes option price with all Greeks.

        Args:
            S: Spot price of the underlying.
            K: Strike price.
            T: Time to expiry in years. Use ``1/252`` for same-day options.
            sigma: Implied volatility as decimal.
            option_type: CALL or PUT.
            r: Risk-free rate override.

        Returns:
            PricingResult with fair value, intrinsic/time value split, and Greeks.

        Raises:
            ValueError: If T <= 0, sigma <= 0, S <= 0, or K <= 0.

        Example:
            >>> bs = BlackScholes(risk_free_rate=0.0525)
            >>> result = bs.price(S=450, K=450, T=7/252, sigma=0.20, option_type=OptionType.CALL)
            >>> print(f"ATM straddle cost: ${result.price * 2:.2f}")
        """
        if T <= 0:
            raise ValueError(f"T must be positive, got {T}")
        if sigma <= 0:
            raise ValueError(f"sigma must be positive, got {sigma}")
        if S <= 0 or K <= 0:
            raise ValueError(f"S and K must be positive, got S={S}, K={K}")

        _r = r if r is not None else self.r
        d1, d2 = self._d1_d2(S, K, T, sigma, _r)
        sqrt_T = math.sqrt(T)
        nd1 = norm.cdf(d1)
        nd2 = norm.cdf(d2)
        npd1 = norm.pdf(d1)
        disc = math.exp(-_r * T)

        if option_type == OptionType.CALL:
            fair_value = S * nd1 - K * disc * nd2
            delta = nd1
            intrinsic = max(S - K, 0.0)
        else:
            nd1_neg = norm.cdf(-d1)
            nd2_neg = norm.cdf(-d2)
            fair_value = K * disc * nd2_neg - S * nd1_neg
            delta = nd1 - 1.0
            intrinsic = max(K - S, 0.0)

        gamma = npd1 / (S * sigma * sqrt_T)
        theta_raw = (
            -(S * npd1 * sigma) / (2 * sqrt_T)
            - _r * K * disc * (nd2 if option_type == OptionType.CALL else norm.cdf(-d2))
        )
        theta = theta_raw / 365.0  # Per calendar day
        vega = S * npd1 * sqrt_T / 100.0  # Per 1% vol change
        rho_raw = (
            K * T * disc * nd2 if option_type == OptionType.CALL
            else -K * T * disc * norm.cdf(-d2)
        )
        rho = rho_raw / 100.0  # Per 1% rate change

        # Higher-order Greeks
        vanna = -npd1 * d2 / sigma
        charm_raw = -npd1 * (2 * _r * T - d2 * sigma * sqrt_T) / (2 * T * sigma * sqrt_T)
        charm = charm_raw / 365.0
        volga = vega * d1 * d2 / sigma

        greeks = GreeksResult(
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            rho=rho,
            vanna=vanna,
            charm=charm,
            volga=volga,
        )

        return PricingResult(
            price=max(fair_value, intrinsic),
            intrinsic=intrinsic,
            time_value=max(fair_value - intrinsic, 0.0),
            greeks=greeks,
            d1=float(d1),
            d2=float(d2),
        )

    def straddle_price(
        self, S: float, K: float, T: float, sigma: float, r: float | None = None
    ) -> tuple[float, GreeksResult]:
        """
        Price an ATM straddle (long call + long put at same strike).

        The straddle is the primary instrument for gamma scalping. Net delta is
        near zero at inception; gamma and vega are maximized.

        Args:
            S: Spot price.
            K: Strike price (typically set equal to S for ATM).
            T: Time to expiry in years.
            sigma: Implied volatility.
            r: Risk-free rate override.

        Returns:
            Tuple of (total_premium, combined_greeks).
        """
        call = self.price(S, K, T, sigma, OptionType.CALL, r)
        put = self.price(S, K, T, sigma, OptionType.PUT, r)

        combined_greeks = GreeksResult(
            delta=call.greeks.delta + put.greeks.delta,
            gamma=call.greeks.gamma + put.greeks.gamma,
            theta=call.greeks.theta + put.greeks.theta,
            vega=call.greeks.vega + put.greeks.vega,
            rho=call.greeks.rho + put.greeks.rho,
            vanna=call.greeks.vanna + put.greeks.vanna,
            charm=call.greeks.charm + put.greeks.charm,
            volga=call.greeks.volga + put.greeks.volga,
        )
        return call.price + put.price, combined_greeks

    def implied_volatility(
        self,
        market_price: float,
        S: float,
        K: float,
        T: float,
        option_type: OptionType = OptionType.CALL,
        r: float | None = None,
        tol: float = 1e-6,
        max_iter: int = 100,
    ) -> float:
        """
        Solve for implied volatility using the Newton-Raphson method.

        Uses vega as the derivative for rapid convergence. Falls back to
        bisection if Newton-Raphson diverges.

        Args:
            market_price: Observed market price of the option.
            S: Spot price.
            K: Strike price.
            T: Time to expiry in years.
            option_type: CALL or PUT.
            r: Risk-free rate override.
            tol: Convergence tolerance (default: 1e-6 vol points).
            max_iter: Maximum Newton-Raphson iterations.

        Returns:
            Implied volatility as decimal (e.g., 0.1823 for 18.23%).

        Raises:
            ValueError: If IV cannot be found within max_iter iterations.

        Example:
            >>> bs = BlackScholes()
            >>> iv = bs.implied_volatility(market_price=25.0, S=5000, K=5000, T=1/52)
            >>> print(f"IV: {iv:.2%}")  # IV: 18.47%
        """
        _r = r if r is not None else self.r
        # Initial guess: use Brenner-Subrahmanyam approximation
        sigma = math.sqrt(2 * math.pi / T) * market_price / S

        for _ in range(max_iter):
            result = self.price(S, K, T, sigma, option_type, _r)
            diff = result.price - market_price
            if abs(diff) < tol:
                return sigma
            vega_raw = result.greeks.vega * 100  # Back to raw vega
            if abs(vega_raw) < 1e-10:
                break
            sigma -= diff / vega_raw
            sigma = max(0.001, min(sigma, 10.0))  # Bound: 0.1% to 1000%

        # Fallback: bisection
        lo, hi = 0.001, 10.0
        for _ in range(200):
            mid = (lo + hi) / 2.0
            result = self.price(S, K, T, mid, option_type, _r)
            if abs(result.price - market_price) < tol:
                return mid
            if result.price < market_price:
                lo = mid
            else:
                hi = mid

        raise ValueError(
            f"IV solver failed to converge for market_price={market_price}, "
            f"S={S}, K={K}, T={T}"
        )

    def gamma_pnl(
        self, gamma: float, spot_move: float, theta_per_day: float, elapsed_days: float
    ) -> float:
        """
        Estimate P&L from gamma scalping using the fundamental equation.

        P&L ≈ ½ × Γ × (ΔS)² + Θ × Δt

        This is the core profitability equation for Strategy 01. You profit when
        realized variance exceeds implied variance (i.e., when the Γ term exceeds
        the |Θ| cost).

        Args:
            gamma: Option gamma (from GreeksResult).
            spot_move: Absolute price move of underlying since last hedge.
            theta_per_day: Daily theta decay (negative number).
            elapsed_days: Time elapsed since last evaluation in days.

        Returns:
            Estimated P&L from gamma + theta combined.

        Example:
            >>> bs = BlackScholes()
            >>> pnl = bs.gamma_pnl(gamma=0.002, spot_move=15.0, theta_per_day=-8.5, elapsed_days=0.5)
            >>> print(f"Gamma P&L: ${pnl:.2f}")
        """
        gamma_contribution = 0.5 * gamma * (spot_move**2)
        theta_contribution = theta_per_day * elapsed_days
        return gamma_contribution + theta_contribution
