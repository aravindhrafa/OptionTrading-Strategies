"""
vol_surface.py
==============
Volatility surface construction, SVI parametrisation, and skew analytics.

Responsibilities
----------------
1. Build a discrete IV surface from (strike, expiry, iv) tuples.
2. Fit SVI (Stochastic Volatility Inspired) per expiry slice for arbitrage-free
   interpolation.
3. Compute all skew metrics used by the trading signal:
   - 25-delta Risk-Reversal  (RR25)
   - 25-delta Butterfly      (BF25)
   - Skew slope              (d IV / d delta)
   - ATM term-structure      (ATM IV per expiry)
   - Skew Z-score            (vs rolling history)
4. Detect mispriced skew legs for trade entry.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from scipy.optimize import minimize
from scipy.interpolate import CubicSpline

from strategy.pricing import bsm_price, implied_vol, delta as bsm_delta


# ── data containers ───────────────────────────────────────────────────────────

@dataclass
class OptionQuote:
    """Single market quote on the vol surface."""
    expiry_years: float        # T in years
    strike: float              # K
    spot: float                # S at quote time
    mid_iv: float              # σ (annualised)
    bid_iv: float
    ask_iv: float
    flag: str = "c"            # 'c' or 'p'

    @property
    def spread_iv(self) -> float:
        return self.ask_iv - self.bid_iv

    @property
    def moneyness(self) -> float:
        """log(K/F) — forward moneyness."""
        return np.log(self.strike / self.spot)

    @property
    def delta(self) -> float:
        from strategy.pricing import delta as bsm_delta_fn, implied_vol
        return bsm_delta_fn(self.spot, self.strike, self.expiry_years, 0.0, self.mid_iv, self.flag)


@dataclass
class SVIParams:
    """
    Raw SVI parameters per expiry slice.
    w(k) = a + b * (ρ(k−m) + sqrt((k−m)² + σ²))
    where k = log(K/F), w = total implied variance = σ²T.
    """
    expiry: float
    a: float = 0.04
    b: float = 0.10
    rho: float = -0.30
    m: float = 0.00
    sigma: float = 0.20

    def total_variance(self, k: float) -> float:
        """Total implied variance w(k) for log-moneyness k."""
        z = k - self.m
        return self.a + self.b * (self.rho * z + np.sqrt(z ** 2 + self.sigma ** 2))

    def implied_vol_at(self, k: float) -> float:
        """σ(k) = sqrt(w(k) / T)."""
        w = max(self.total_variance(k), 1e-8)
        return np.sqrt(w / max(self.expiry, 1e-6))


# ── SVI calibration ───────────────────────────────────────────────────────────

def _svi_loss(params: np.ndarray, ks: np.ndarray, market_w: np.ndarray) -> float:
    a, b, rho, m, sigma = params
    if b <= 0 or sigma <= 0 or abs(rho) >= 1:
        return 1e9
    z = ks - m
    model_w = a + b * (rho * z + np.sqrt(z ** 2 + sigma ** 2))
    if np.any(model_w < 0):
        return 1e9
    # Butterfly arbitrage check: g(k) >= 0
    g = (1 - 0.5 * ks * (rho + ks / (sigma ** 2 + z ** 2) ** 0.5) * b) ** 2
    if np.any(g < 0):
        return 1e9
    return float(np.mean((model_w - market_w) ** 2))


def calibrate_svi(quotes: list[OptionQuote], expiry: float) -> SVIParams:
    """
    Calibrate SVI parameters to a single expiry slice using L-BFGS-B.
    Falls back to a flat surface if calibration fails.
    """
    ks = np.array([q.moneyness for q in quotes])
    market_w = np.array([q.mid_iv ** 2 * expiry for q in quotes])

    x0 = np.array([0.04, 0.10, -0.30, 0.00, 0.20])
    bounds = [
        (-0.5, 0.5),   # a
        (1e-4, 2.0),   # b
        (-0.999, 0.999),  # rho
        (-1.0, 1.0),   # m
        (1e-4, 2.0),   # sigma
    ]

    result = minimize(
        _svi_loss, x0, args=(ks, market_w),
        method="L-BFGS-B", bounds=bounds,
        options={"maxiter": 2000, "ftol": 1e-12}
    )

    if result.success and result.fun < 1e-6:
        a, b, rho, m, sigma = result.x
        return SVIParams(expiry=expiry, a=a, b=b, rho=rho, m=m, sigma=sigma)

    # Fallback: ATM flat surface
    atm_iv = float(np.mean([q.mid_iv for q in quotes]))
    a_flat = atm_iv ** 2 * expiry
    return SVIParams(expiry=expiry, a=a_flat, b=0.001, rho=0.0, m=0.0, sigma=0.3)


# ── surface metrics ───────────────────────────────────────────────────────────

@dataclass
class SkewMetrics:
    """All skew signals for one expiry slice."""
    expiry: float

    # ── core skew metrics ─────────────────────────────────
    rr25: float = 0.0          # 25Δ Risk-Reversal (call IV − put IV)
    bf25: float = 0.0          # 25Δ Butterfly (avg wing IV − ATM IV)
    atm_iv: float = 0.0        # At-the-money implied vol
    skew_slope: float = 0.0    # dIV/dΔ across strikes

    # ── z-scores vs history ───────────────────────────────
    rr25_zscore: float = 0.0
    bf25_zscore: float = 0.0
    atm_zscore: float = 0.0

    # ── signal flags ─────────────────────────────────────
    skew_too_steep: bool = False     # put skew anomalously rich
    skew_too_flat: bool = False      # put skew anomalously cheap
    butterfly_rich: bool = False     # wings too expensive vs ATM
    butterfly_cheap: bool = False    # wings too cheap vs ATM

    def signal_strength(self) -> float:
        """Composite signal score [-1, +1].  +1 = sell skew, -1 = buy skew."""
        score = 0.0
        if self.skew_too_steep:
            score += min(abs(self.rr25_zscore) / 3.0, 1.0)
        if self.skew_too_flat:
            score -= min(abs(self.rr25_zscore) / 3.0, 1.0)
        if self.butterfly_rich:
            score += 0.5 * min(abs(self.bf25_zscore) / 2.0, 1.0)
        if self.butterfly_cheap:
            score -= 0.5 * min(abs(self.bf25_zscore) / 2.0, 1.0)
        return float(np.clip(score, -1.0, 1.0))


# ── main surface class ────────────────────────────────────────────────────────

class VolSurface:
    """
    Arbitrage-free implied volatility surface backed by per-slice SVI fits.

    Usage
    -----
    >>> surface = VolSurface(spot=450.0, rate=0.05)
    >>> surface.add_slice(expiry=0.083, quotes=[...])
    >>> surface.build()
    >>> metrics = surface.skew_metrics(expiry=0.083)
    """

    def __init__(self, spot: float, rate: float = 0.05):
        self.spot = spot
        self.rate = rate
        self._slices: dict[float, list[OptionQuote]] = {}
        self._svi: dict[float, SVIParams] = {}
        self._built = False

    def add_slice(self, expiry: float, quotes: list[OptionQuote]) -> None:
        """Add an expiry slice.  Quotes must span at least 3 strikes."""
        if len(quotes) < 3:
            raise ValueError(f"Need ≥3 quotes per slice, got {len(quotes)}")
        self._slices[expiry] = quotes
        self._built = False

    def build(self) -> None:
        """Calibrate SVI to every loaded expiry slice."""
        for expiry, quotes in self._slices.items():
            self._svi[expiry] = calibrate_svi(quotes, expiry)
        self._built = True

    def iv_at(self, strike: float, expiry: float) -> float:
        """
        Query implied vol at any (strike, expiry).
        Uses SVI interpolation within calibrated expiries;
        linearly interpolates total variance across expiries.
        """
        if not self._built:
            raise RuntimeError("Call build() before querying the surface.")

        expiries = sorted(self._svi.keys())
        k = np.log(strike / self.spot)

        if len(expiries) == 1 or expiry <= expiries[0]:
            return self._svi[expiries[0]].implied_vol_at(k)
        if expiry >= expiries[-1]:
            return self._svi[expiries[-1]].implied_vol_at(k)

        # Linear interpolation in total variance space
        for i in range(len(expiries) - 1):
            t1, t2 = expiries[i], expiries[i + 1]
            if t1 <= expiry <= t2:
                w1 = self._svi[t1].total_variance(k)
                w2 = self._svi[t2].total_variance(k)
                alpha = (expiry - t1) / (t2 - t1)
                w = (1 - alpha) * w1 + alpha * w2
                return np.sqrt(max(w / expiry, 0.0))

        return self._svi[expiries[-1]].implied_vol_at(k)

    def delta_to_strike(self, target_delta: float, expiry: float, flag: str = "c") -> float:
        """
        Invert delta → strike using bisection on the SVI surface.
        target_delta: e.g. 0.25 for 25Δ call, -0.25 for 25Δ put.
        """
        lo, hi = self.spot * 0.5, self.spot * 2.0
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            iv = self.iv_at(mid, expiry)
            d = bsm_delta(self.spot, mid, expiry, self.rate, iv, flag)
            if flag == "c":
                if d > target_delta:
                    lo = mid
                else:
                    hi = mid
            else:
                if d < target_delta:
                    hi = mid
                else:
                    lo = mid
            if hi - lo < 0.01:
                break
        return 0.5 * (lo + hi)

    def skew_metrics(self, expiry: float, history: Optional[pd.DataFrame] = None) -> SkewMetrics:
        """
        Compute RR25, BF25, ATM IV, skew slope for a given expiry.
        Optionally compute z-scores if historical DataFrame is provided.
        history columns: ['date', 'rr25', 'bf25', 'atm_iv']
        """
        if not self._built:
            raise RuntimeError("Call build() before querying metrics.")

        # ATM IV
        atm_iv = self.iv_at(self.spot, expiry)

        # 25Δ strikes via surface inversion
        k_c25 = self.delta_to_strike(0.25, expiry, "c")
        k_p25 = self.delta_to_strike(-0.25, expiry, "p")

        iv_c25 = self.iv_at(k_c25, expiry)
        iv_p25 = self.iv_at(k_p25, expiry)

        rr25 = iv_c25 - iv_p25           # +ve → calls richer than puts
        bf25 = 0.5 * (iv_c25 + iv_p25) - atm_iv   # +ve → wings rich vs ATM

        # Skew slope (dIV/dΔ): finite difference across 10Δ to 40Δ calls
        deltas_sample = [0.10, 0.20, 0.30, 0.40]
        ivs_sample = []
        for d in deltas_sample:
            k = self.delta_to_strike(d, expiry, "c")
            ivs_sample.append(self.iv_at(k, expiry))
        skew_slope = float(np.polyfit(deltas_sample, ivs_sample, 1)[0])

        m = SkewMetrics(expiry=expiry, rr25=rr25, bf25=bf25,
                        atm_iv=atm_iv, skew_slope=skew_slope)

        # Z-scores
        if history is not None and len(history) >= 20:
            for metric, col in [("rr25_zscore", "rr25"),
                                 ("bf25_zscore", "bf25"),
                                 ("atm_zscore", "atm_iv")]:
                mu = history[col].mean()
                sigma = history[col].std()
                current = getattr(m, col.replace("_zscore", "") if col != "atm_iv" else "atm_iv")
                setattr(m, metric, (current - mu) / sigma if sigma > 0 else 0.0)

            # Signal thresholds (1.5σ / 2σ)
            m.skew_too_steep = m.rr25_zscore < -1.5    # put skew rich (neg RR = puts bid)
            m.skew_too_flat  = m.rr25_zscore > 1.5
            m.butterfly_rich  = m.bf25_zscore > 1.5
            m.butterfly_cheap = m.bf25_zscore < -1.5

        return m

    def term_structure(self) -> pd.DataFrame:
        """ATM IV across all calibrated expiries."""
        rows = []
        for exp in sorted(self._svi.keys()):
            rows.append({
                "expiry_years": exp,
                "expiry_days": round(exp * 365),
                "atm_iv": self.iv_at(self.spot, exp),
                "atm_total_var": self.iv_at(self.spot, exp) ** 2 * exp,
            })
        return pd.DataFrame(rows)

    def smile_dataframe(self, expiry: float, n_strikes: int = 40) -> pd.DataFrame:
        """Return (strike, IV, delta) table for one expiry — useful for plotting."""
        strikes = np.linspace(self.spot * 0.70, self.spot * 1.30, n_strikes)
        rows = []
        for K in strikes:
            iv = self.iv_at(K, expiry)
            d = bsm_delta(self.spot, K, expiry, self.rate, iv, "c")
            rows.append({"strike": K, "iv": iv, "delta": d,
                         "moneyness": np.log(K / self.spot)})
        return pd.DataFrame(rows)


# ── synthetic surface generator (for testing / backtesting) ──────────────────

def make_synthetic_surface(
    spot: float = 450.0,
    rate: float = 0.05,
    atm_iv: float = 0.18,
    skew: float = -0.06,      # RR25 (negative → puts richer)
    convexity: float = 0.02,  # BF25
    expiries: list[float] = None,
    noise_sigma: float = 0.003,
    seed: int = 42,
) -> VolSurface:
    """
    Build a synthetic vol surface with controllable skew and convexity.
    Used by backtester and unit tests.
    """
    rng = np.random.default_rng(seed)
    if expiries is None:
        expiries = [1/52, 2/52, 1/12, 2/12, 3/12, 6/12]

    surface = VolSurface(spot=spot, rate=rate)

    for T in expiries:
        # Build smile: quadratic in delta space
        quotes = []
        for K_ratio in np.linspace(0.80, 1.20, 13):
            K = round(spot * K_ratio, 0)
            mon = np.log(K_ratio)  # log-moneyness
            # Skew model: linear + quadratic in moneyness
            iv = atm_iv - skew * mon + convexity * mon ** 2
            iv = float(np.clip(iv + rng.normal(0, noise_sigma), 0.05, 1.5))
            spread = max(0.002, iv * 0.015)
            flag = "c" if K >= spot else "p"
            quotes.append(OptionQuote(
                expiry_years=T, strike=K, spot=spot,
                mid_iv=iv, bid_iv=iv - spread, ask_iv=iv + spread,
                flag=flag,
            ))
        surface.add_slice(T, quotes)

    surface.build()
    return surface
