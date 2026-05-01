"""
pricing.py
==========
Black-Scholes-Merton engine: option pricing, full Greek suite, and
Newton-Raphson implied-volatility solver with bisection fallback.

All functions are pure (no side-effects) and stateless so they can be
vectorised with numpy broadcasting or called in a tight inner loop.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import norm
from typing import Literal


# ── type aliases ──────────────────────────────────────────────────────────────
Flag = Literal["c", "p"]   # call or put


# ── internal helpers ──────────────────────────────────────────────────────────

def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    """Standard BSM d1/d2 terms.  T in years, sigma annualised."""
    if T <= 0.0 or sigma <= 0.0:
        return 0.0, 0.0
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return d1, d2


# ── public API ────────────────────────────────────────────────────────────────

def bsm_price(S: float, K: float, T: float, r: float, sigma: float, flag: Flag = "c") -> float:
    """
    Black-Scholes-Merton option price.

    Parameters
    ----------
    S     : underlying spot price
    K     : strike price
    T     : time-to-expiry in years
    r     : continuously compounded risk-free rate
    sigma : annualised implied volatility
    flag  : 'c' = call, 'p' = put

    Returns
    -------
    float : BSM fair value
    """
    if T <= 0.0:
        # intrinsic value at expiry
        return max(S - K, 0.0) if flag == "c" else max(K - S, 0.0)

    d1, d2 = _d1_d2(S, K, T, r, sigma)
    discount = np.exp(-r * T)

    if flag == "c":
        return S * norm.cdf(d1) - K * discount * norm.cdf(d2)
    return K * discount * norm.cdf(-d2) - S * norm.cdf(-d1)


def delta(S: float, K: float, T: float, r: float, sigma: float, flag: Flag = "c") -> float:
    """First derivative of option price with respect to spot (Δ)."""
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return norm.cdf(d1) if flag == "c" else norm.cdf(d1) - 1.0


def gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Second derivative of option price with respect to spot (Γ)."""
    if T <= 0.0 or sigma <= 0.0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return norm.pdf(d1) / (S * sigma * np.sqrt(T))


def vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Sensitivity to a 1-vol-point (1%) move in IV (ν / 100)."""
    if T <= 0.0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return S * norm.pdf(d1) * np.sqrt(T) / 100.0


def theta(S: float, K: float, T: float, r: float, sigma: float, flag: Flag = "c") -> float:
    """Daily time decay (Θ per calendar day)."""
    if T <= 0.0:
        return 0.0
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    decay = -(S * norm.pdf(d1) * sigma) / (2.0 * np.sqrt(T))
    if flag == "c":
        carry = -r * K * np.exp(-r * T) * norm.cdf(d2)
    else:
        carry = r * K * np.exp(-r * T) * norm.cdf(-d2)
    return (decay + carry) / 365.0


def vanna(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Mixed second-order Greek: dΔ/dσ = dν/dS.
    Critical for skew trades — measures how delta changes with vol.
    """
    if T <= 0.0 or sigma <= 0.0:
        return 0.0
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    return -norm.pdf(d1) * d2 / sigma


def volga(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Vomma / Volga: second derivative of price w.r.t. vol (dν/dσ).
    Measures convexity of the vol surface — key for skew P&L.
    """
    if T <= 0.0 or sigma <= 0.0:
        return 0.0
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    v = vega(S, K, T, r, sigma) * 100.0   # un-scaled vega for formula
    return v * d1 * d2 / sigma


def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    flag: Flag = "c",
    tol: float = 1e-7,
    max_iter: int = 200,
) -> float:
    """
    Newton-Raphson IV solver with bisection fallback.

    Raises
    ------
    ValueError : if market_price is below intrinsic value
    """
    intrinsic = max(S - K, 0.0) if flag == "c" else max(K - S, 0.0)
    if market_price < intrinsic - 1e-4:
        raise ValueError(
            f"market_price {market_price:.4f} below intrinsic {intrinsic:.4f}"
        )
    if market_price <= intrinsic + 1e-8:
        return 0.0

    sigma = 0.25   # initial guess

    # Newton-Raphson
    for _ in range(max_iter):
        price = bsm_price(S, K, T, r, sigma, flag)
        v = vega(S, K, T, r, sigma) * 100.0   # dP/dσ (unscaled)
        if abs(v) < 1e-10:
            break
        sigma_new = sigma - (price - market_price) / v
        sigma_new = max(1e-4, min(sigma_new, 10.0))
        if abs(sigma_new - sigma) < tol:
            return sigma_new
        sigma = sigma_new

    # Bisection fallback
    lo, hi = 1e-4, 10.0
    for _ in range(300):
        mid = 0.5 * (lo + hi)
        if bsm_price(S, K, T, r, mid, flag) > market_price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)
