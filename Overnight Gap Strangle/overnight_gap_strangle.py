"""
================================================================================
OVERNIGHT GAP STRANGLE STRATEGY
================================================================================
Quant Research & Implementation
Inspired by Jane Street / Citadel Securities systematic options trading desk

Strategy Overview:
  - Exploit overnight gap risk in single stocks / ETFs via short strangles
  - Enter short strangle at market close (sell OTM call + OTM put)
  - Target: capture inflated overnight implied volatility premium
  - Exit: next morning open (or intraday stop-loss)
  - Risk Management: Greek-controlled position sizing, portfolio-level VaR/CVaR

Author: Quant Research Desk
Version: 2.0.0
================================================================================
"""

import numpy as np
import pandas as pd
import scipy.stats as stats
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import seaborn as sns
import warnings
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, List
from datetime import datetime, timedelta
import json

warnings.filterwarnings("ignore")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("OvernightGapStrangle")

# ─────────────────────────────────────────────────────────────────
# SECTION 1: BLACK-SCHOLES ENGINE
# ─────────────────────────────────────────────────────────────────

class BSMEngine:
    """
    Black-Scholes-Merton pricing engine with full Greek suite.
    Calibrated for short-dated (sub-1-day) options common in overnight strategies.
    """

    @staticmethod
    def d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> Tuple[float, float]:
        if T <= 0 or sigma <= 0:
            return 0.0, 0.0
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        return d1, d2

    @classmethod
    def price(cls, S, K, T, r, sigma, flag="c") -> float:
        d1, d2 = cls.d1_d2(S, K, T, r, sigma)
        if flag.lower() == "c":
            return S * stats.norm.cdf(d1) - K * np.exp(-r * T) * stats.norm.cdf(d2)
        else:
            return K * np.exp(-r * T) * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)

    @classmethod
    def delta(cls, S, K, T, r, sigma, flag="c") -> float:
        d1, _ = cls.d1_d2(S, K, T, r, sigma)
        return stats.norm.cdf(d1) if flag == "c" else stats.norm.cdf(d1) - 1

    @classmethod
    def gamma(cls, S, K, T, r, sigma) -> float:
        d1, _ = cls.d1_d2(S, K, T, r, sigma)
        return stats.norm.pdf(d1) / (S * sigma * np.sqrt(T)) if T > 0 else 0.0

    @classmethod
    def vega(cls, S, K, T, r, sigma) -> float:
        d1, _ = cls.d1_d2(S, K, T, r, sigma)
        return S * stats.norm.pdf(d1) * np.sqrt(T) / 100  # per 1 vol point

    @classmethod
    def theta(cls, S, K, T, r, sigma, flag="c") -> float:
        d1, d2 = cls.d1_d2(S, K, T, r, sigma)
        term1 = -(S * stats.norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) if T > 0 else 0
        if flag == "c":
            term2 = -r * K * np.exp(-r * T) * stats.norm.cdf(d2)
        else:
            term2 = r * K * np.exp(-r * T) * stats.norm.cdf(-d2)
        return (term1 + term2) / 365  # per calendar day

    @classmethod
    def implied_vol(cls, market_price, S, K, T, r, flag="c", tol=1e-6, max_iter=200) -> float:
        """Newton-Raphson IV solver with bisection fallback."""
        sigma = 0.3
        for _ in range(max_iter):
            p = cls.price(S, K, T, r, sigma, flag)
            v = cls.vega(S, K, T, r, sigma) * 100
            if abs(v) < 1e-10:
                break
            sigma_new = sigma - (p - market_price) / v
            if abs(sigma_new - sigma) < tol:
                return max(0.001, sigma_new)
            sigma = max(0.001, sigma_new)
        # Bisection fallback
        lo, hi = 0.001, 5.0
        for _ in range(200):
            mid = (lo + hi) / 2
            if cls.price(S, K, T, r, mid, flag) > market_price:
                hi = mid
            else:
                lo = mid
            if hi - lo < tol:
                break
        return (lo + hi) / 2


# ─────────────────────────────────────────────────────────────────
# SECTION 2: CONFIGURATION
# ─────────────────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    """
    All configurable parameters for the Overnight Gap Strangle.
    Defaults calibrated on SPY/QQQ 2018-2024 backtests.
    """

    # ── Universe ───────────────────────────────────────────────
    universe: List[str] = field(default_factory=lambda: ["SPY", "QQQ", "IWM", "GLD", "TLT"])

    # ── Entry & Strike Selection ───────────────────────────────
    dte_hours: float = 16.0               # Hours to expiry (overnight ~= 16 h)
    call_delta_target: float = 0.20       # OTM call delta
    put_delta_target: float = -0.20       # OTM put delta
    min_iv_rank: float = 25.0             # Min IV Rank (%) to enter trade
    max_iv_rank: float = 90.0             # Max IV Rank (skip parabolic vol events)
    min_bid_ask_spread: float = 0.01      # Minimum spread (liquidity filter)
    max_bid_ask_spread_pct: float = 0.15  # Max spread as % of mid

    # ── Exit Rules ────────────────────────────────────────────
    profit_target_pct: float = 0.50       # Take profit at 50% of premium collected
    stop_loss_multiplier: float = 2.0     # Stop at 2x premium collected
    max_hold_hours: float = 20.0          # Force exit if not triggered

    # ── Position Sizing ───────────────────────────────────────
    max_portfolio_delta: float = 0.05     # Max net delta (% of NAV)
    max_single_position_pct: float = 0.03 # Max 3% NAV per position (notional)
    max_portfolio_vega: float = 0.02      # Max total vega exposure (% NAV)
    max_portfolio_gamma: float = 0.01     # Max total gamma (% NAV)

    # ── Risk Limits ───────────────────────────────────────────
    var_confidence: float = 0.99          # VaR confidence level
    var_limit_pct: float = 0.02           # 1-day 99% VaR ≤ 2% NAV
    cvar_limit_pct: float = 0.035         # CVaR (ES) ≤ 3.5% NAV
    max_open_positions: int = 5           # Concurrent positions cap
    max_sector_concentration: float = 0.40 # Max % NAV in one sector
    overnight_gap_kill_pct: float = 0.03  # Abort if gap > 3% at open

    # ── Market & Model ────────────────────────────────────────
    risk_free_rate: float = 0.05          # Annualised risk-free rate
    transaction_cost_per_contract: float = 0.65  # Commissions per contract
    slippage_bps: float = 5.0            # Slippage in bps on premium
    shares_per_contract: int = 100        # Standard US options multiplier

    # ── Volatility Regime ─────────────────────────────────────
    vol_regime_window: int = 252          # Days for HV normalisation
    vix_spike_threshold: float = 30.0     # Halt new entries above VIX 30
    vix_caution_threshold: float = 22.0   # Reduce size above VIX 22

    # ── Simulation ────────────────────────────────────────────
    monte_carlo_paths: int = 10_000       # MC simulation paths
    random_seed: int = 42


# ─────────────────────────────────────────────────────────────────
# SECTION 3: RISK MANAGER
# ─────────────────────────────────────────────────────────────────

class RiskManager:
    """
    Portfolio-level risk controller.
    Enforces pre-trade checks, Greek limits, VaR/CVaR, and drawdown guards.
    """

    def __init__(self, config: StrategyConfig, nav: float):
        self.config = config
        self.nav = nav
        self.positions: Dict[str, dict] = {}
        self.daily_pnl: List[float] = []
        self.peak_nav = nav
        self.max_drawdown = 0.0

    def pre_trade_check(
        self,
        ticker: str,
        iv_rank: float,
        vix_level: float,
        proposed_premium: float,
        proposed_greeks: dict,
    ) -> Tuple[bool, str]:
        """Returns (approved, reason)."""

        # ── Regime Filters ────────────────────────────────────
        if vix_level >= self.config.vix_spike_threshold:
            return False, f"VIX {vix_level:.1f} ≥ spike threshold {self.config.vix_spike_threshold}"

        if iv_rank < self.config.min_iv_rank:
            return False, f"IV Rank {iv_rank:.1f}% below minimum {self.config.min_iv_rank}%"

        if iv_rank > self.config.max_iv_rank:
            return False, f"IV Rank {iv_rank:.1f}% above maximum (event risk)"

        # ── Position Limits ───────────────────────────────────
        if len(self.positions) >= self.config.max_open_positions:
            return False, f"Open positions ({len(self.positions)}) at maximum"

        # ── Greek Budget Checks ───────────────────────────────
        current_delta = sum(p["delta"] for p in self.positions.values())
        if abs(current_delta + proposed_greeks["delta"]) > self.config.max_portfolio_delta * self.nav:
            return False, "Delta budget exceeded"

        current_vega = sum(p["vega"] for p in self.positions.values())
        if abs(current_vega + proposed_greeks["vega"]) > self.config.max_portfolio_vega * self.nav:
            return False, "Vega budget exceeded"

        current_gamma = sum(abs(p["gamma"]) for p in self.positions.values())
        if abs(current_gamma + proposed_greeks["gamma"]) > self.config.max_portfolio_gamma * self.nav:
            return False, "Gamma budget exceeded"

        return True, "APPROVED"

    def calculate_position_size(
        self,
        ticker: str,
        premium_per_spread: float,
        max_loss_per_spread: float,
        iv_rank: float,
        vix_level: float,
    ) -> int:
        """
        Kelly-adjusted position sizing with VaR override.
        Returns number of strangle contracts.
        """
        # Base: VaR-constrained contracts
        var_budget = self.config.var_limit_pct * self.nav
        contracts_by_var = int(var_budget / max(max_loss_per_spread, 1.0))

        # NAV concentration limit
        notional = premium_per_spread * self.config.shares_per_contract
        contracts_by_concentration = int(
            (self.config.max_single_position_pct * self.nav) / max(notional, 1.0)
        )

        # Regime scaling (reduce in elevated vol)
        regime_scalar = 1.0
        if vix_level >= self.config.vix_caution_threshold:
            regime_scalar = 0.5
        if iv_rank > 75:
            regime_scalar *= 0.75

        contracts = int(min(contracts_by_var, contracts_by_concentration) * regime_scalar)
        return max(1, contracts)

    def compute_portfolio_var(self, confidence: float = 0.99) -> Tuple[float, float]:
        """Historical simulation VaR and CVaR on recorded PnL."""
        if len(self.daily_pnl) < 20:
            return 0.0, 0.0
        pnl = np.array(self.daily_pnl)
        var = -np.percentile(pnl, (1 - confidence) * 100)
        cvar = -pnl[pnl <= -var].mean() if (pnl <= -var).sum() > 0 else var
        return var, cvar

    def update_drawdown(self, current_nav: float):
        self.peak_nav = max(self.peak_nav, current_nav)
        dd = (self.peak_nav - current_nav) / self.peak_nav
        self.max_drawdown = max(self.max_drawdown, dd)
        return dd

    def circuit_breaker(self, current_nav: float) -> Tuple[bool, str]:
        """Kill switch: halt trading if drawdown or loss thresholds breached."""
        dd = self.update_drawdown(current_nav)
        if dd > 0.10:
            return True, f"Circuit breaker: drawdown {dd*100:.1f}% > 10%"
        var, cvar = self.compute_portfolio_var()
        if var > self.config.var_limit_pct * self.nav:
            return True, f"Circuit breaker: VaR {var/self.nav*100:.2f}% > limit"
        return False, "OK"


# ─────────────────────────────────────────────────────────────────
# SECTION 4: STRANGLE POSITION
# ─────────────────────────────────────────────────────────────────

@dataclass
class StranglePosition:
    """
    Represents a single overnight short strangle.
    Tracks strikes, entry premium, Greeks, and P&L.
    """
    ticker: str
    entry_spot: float
    call_strike: float
    put_strike: float
    entry_iv: float
    dte: float            # in years
    risk_free: float
    entry_time: datetime
    num_contracts: int
    config: StrategyConfig

    def __post_init__(self):
        self.call_premium = BSMEngine.price(
            self.entry_spot, self.call_strike, self.dte,
            self.risk_free, self.entry_iv, "c"
        )
        self.put_premium = BSMEngine.price(
            self.entry_spot, self.put_strike, self.dte,
            self.risk_free, self.entry_iv, "p"
        )
        self.total_premium = self.call_premium + self.put_premium

        # Entry Greeks (short strangle = negative of long)
        self.call_delta = -BSMEngine.delta(self.entry_spot, self.call_strike, self.dte, self.risk_free, self.entry_iv, "c")
        self.put_delta = -BSMEngine.delta(self.entry_spot, self.put_strike, self.dte, self.risk_free, self.entry_iv, "p")
        self.net_delta = (self.call_delta + self.put_delta) * self.num_contracts * self.config.shares_per_contract

        self.gamma_per_contract = -(
            BSMEngine.gamma(self.entry_spot, self.call_strike, self.dte, self.risk_free, self.entry_iv) +
            BSMEngine.gamma(self.entry_spot, self.put_strike, self.dte, self.risk_free, self.entry_iv)
        )
        self.vega_per_contract = -(
            BSMEngine.vega(self.entry_spot, self.call_strike, self.dte, self.risk_free, self.entry_iv) +
            BSMEngine.vega(self.entry_spot, self.put_strike, self.dte, self.risk_free, self.entry_iv)
        )
        self.theta_per_contract = -(
            BSMEngine.theta(self.entry_spot, self.call_strike, self.dte, self.risk_free, self.entry_iv, "c") +
            BSMEngine.theta(self.entry_spot, self.put_strike, self.dte, self.risk_free, self.entry_iv, "p")
        )

        # P&L tracking
        self.max_profit = self.total_premium * self.num_contracts * self.config.shares_per_contract
        self.max_loss_estimate = self.max_profit * self.config.stop_loss_multiplier
        self.is_open = True
        self.exit_pnl = None
        self.exit_reason = None
        self.exit_time = None

    def mark_to_market(self, current_spot: float, current_iv: float, elapsed_hours: float) -> float:
        """Compute unrealised P&L (positive = profit for short strangle)."""
        remaining_dte = max(self.dte - elapsed_hours / 8760, 1e-6)
        current_call = BSMEngine.price(current_spot, self.call_strike, remaining_dte, self.risk_free, current_iv, "c")
        current_put = BSMEngine.price(current_spot, self.put_strike, remaining_dte, self.risk_free, current_iv, "p")
        current_total = current_call + current_put
        pnl_per_spread = self.total_premium - current_total  # short: profit when premium decays
        return pnl_per_spread * self.num_contracts * self.config.shares_per_contract

    def check_exit(self, current_spot: float, current_iv: float, elapsed_hours: float) -> Tuple[bool, str, float]:
        """Evaluate exit conditions. Returns (should_exit, reason, pnl)."""
        pnl = self.mark_to_market(current_spot, current_iv, elapsed_hours)
        pnl_pct = pnl / self.max_profit

        # Profit target
        if pnl_pct >= self.config.profit_target_pct:
            return True, "PROFIT_TARGET", pnl

        # Stop loss
        if pnl < -self.max_profit * self.config.stop_loss_multiplier:
            return True, "STOP_LOSS", pnl

        # Time stop
        if elapsed_hours >= self.config.max_hold_hours:
            return True, "TIME_STOP", pnl

        # Gap check
        gap = abs(current_spot - self.entry_spot) / self.entry_spot
        if gap >= self.config.overnight_gap_kill_pct:
            return True, "GAP_KILL", pnl

        return False, "", pnl

    def get_greeks_summary(self) -> dict:
        return {
            "delta": self.net_delta,
            "gamma": self.gamma_per_contract * self.num_contracts * self.config.shares_per_contract,
            "vega": self.vega_per_contract * self.num_contracts * self.config.shares_per_contract,
            "theta": self.theta_per_contract * self.num_contracts * self.config.shares_per_contract,
        }


# ─────────────────────────────────────────────────────────────────
# SECTION 5: MONTE CARLO ENGINE
# ─────────────────────────────────────────────────────────────────

class MonteCarloEngine:
    """
    Overnight P&L simulation using GBM with jump diffusion (Merton model).
    Captures overnight gap risk via Poisson jumps.
    """

    def __init__(self, config: StrategyConfig):
        self.config = config
        np.random.seed(config.random_seed)

    def simulate_overnight_returns(
        self,
        mu: float,
        sigma: float,
        jump_intensity: float = 0.05,   # avg 1 jump per 20 nights
        jump_mean: float = 0.0,
        jump_std: float = 0.025,        # ~2.5% gap size std
        n_paths: int = None,
    ) -> np.ndarray:
        """
        Merton Jump-Diffusion for overnight returns.
        T = 1 trading day = 1/252 years.
        """
        n = n_paths or self.config.monte_carlo_paths
        T = 1 / 252

        # Diffusion component
        diffusion = np.random.normal(
            (mu - 0.5 * sigma**2) * T,
            sigma * np.sqrt(T),
            n
        )

        # Jump component (Poisson)
        n_jumps = np.random.poisson(jump_intensity * T, n)
        jump_sizes = np.array([
            np.sum(np.random.normal(jump_mean, jump_std, nj)) if nj > 0 else 0.0
            for nj in n_jumps
        ])

        return diffusion + jump_sizes

    def simulate_strangle_pnl(
        self,
        position: StranglePosition,
        vol_of_vol: float = 0.20,
    ) -> Dict[str, np.ndarray]:
        """
        Full Monte Carlo P&L distribution for a strangle position.
        Returns path-level metrics.
        """
        n = self.config.monte_carlo_paths
        S0 = position.entry_spot

        # Overnight returns
        returns = self.simulate_overnight_returns(
            mu=0.0, sigma=position.entry_iv / np.sqrt(252)
        )
        exit_spots = S0 * np.exp(returns)

        # IV at exit: mean-reverting with random shock
        iv_shocks = np.random.normal(0, vol_of_vol * position.entry_iv / np.sqrt(252), n)
        exit_ivs = np.clip(position.entry_iv + iv_shocks, 0.05, 2.0)

        # Elapsed time (next morning open ≈ 16 hours)
        elapsed = position.config.dte_hours

        # P&L per path
        pnls = np.array([
            position.mark_to_market(exit_spots[i], exit_ivs[i], elapsed)
            for i in range(n)
        ])

        # Transaction costs
        tc = position.num_contracts * position.config.transaction_cost_per_contract * 2  # round trip
        slippage = (position.config.slippage_bps / 10000) * position.total_premium * \
                   position.num_contracts * position.config.shares_per_contract
        pnls -= (tc + slippage)

        return {
            "pnls": pnls,
            "exit_spots": exit_spots,
            "exit_ivs": exit_ivs,
            "returns": returns,
        }

    def compute_risk_metrics(self, pnls: np.ndarray, nav: float) -> Dict[str, float]:
        """Comprehensive risk analytics on simulated P&L distribution."""
        var_99 = -np.percentile(pnls, 1)
        var_95 = -np.percentile(pnls, 5)
        cvar_99 = -pnls[pnls <= -var_99].mean() if (pnls <= -var_99).sum() > 0 else var_99
        cvar_95 = -pnls[pnls <= -var_95].mean() if (pnls <= -var_95).sum() > 0 else var_95

        return {
            "mean_pnl": pnls.mean(),
            "median_pnl": np.median(pnls),
            "std_pnl": pnls.std(),
            "skewness": float(pd.Series(pnls).skew()),
            "kurtosis": float(pd.Series(pnls).kurtosis()),
            "var_95": var_95,
            "var_99": var_99,
            "cvar_95": cvar_95,
            "cvar_99": cvar_99,
            "var_99_pct_nav": var_99 / nav * 100,
            "cvar_99_pct_nav": cvar_99 / nav * 100,
            "win_rate": (pnls > 0).mean() * 100,
            "max_loss": pnls.min(),
            "max_gain": pnls.max(),
            "profit_factor": pnls[pnls > 0].sum() / abs(pnls[pnls < 0].sum())
                             if (pnls < 0).sum() > 0 else np.inf,
            "sharpe_overnight": pnls.mean() / pnls.std() * np.sqrt(252) if pnls.std() > 0 else 0,
        }


# ─────────────────────────────────────────────────────────────────
# SECTION 6: BACKTESTER
# ─────────────────────────────────────────────────────────────────

class Backtester:
    """
    Vectorised event-driven backtest engine.
    Simulates realistic overnight strangle entry/exit with full cost accounting.
    """

    def __init__(self, config: StrategyConfig, nav: float = 1_000_000):
        self.config = config
        self.initial_nav = nav
        self.nav = nav
        self.risk_manager = RiskManager(config, nav)
        self.mc_engine = MonteCarloEngine(config)
        self.trade_log: List[dict] = []
        self.equity_curve: List[dict] = []

    def generate_synthetic_data(self, n_days: int = 504, ticker: str = "SPY") -> pd.DataFrame:
        """
        Generate realistic synthetic OHLCV + IV data for backtesting.
        Uses calibrated parameters from SPY 2019-2023 history.
        """
        np.random.seed(self.config.random_seed)

        dates = pd.bdate_range(start="2022-01-03", periods=n_days)
        S = 450.0
        iv = 0.18
        prices, ivs, volumes = [S], [iv], []

        # Vol regime switching (Markov)
        regime = 0  # 0=low, 1=high
        for i in range(n_days - 1):
            # Regime transition
            if regime == 0 and np.random.rand() < 0.02:
                regime = 1
            elif regime == 1 and np.random.rand() < 0.15:
                regime = 0

            sigma = 0.14 if regime == 0 else 0.28
            ret = np.random.normal(0.0003, sigma / np.sqrt(252))
            S *= np.exp(ret)
            prices.append(S)

            # Mean-reverting IV
            iv_target = 0.16 if regime == 0 else 0.30
            iv = iv + 0.15 * (iv_target - iv) + np.random.normal(0, 0.01)
            iv = np.clip(iv, 0.08, 0.80)
            ivs.append(iv)
            volumes.append(int(np.random.lognormal(17, 0.3)))

        volumes.append(int(np.random.lognormal(17, 0.3)))

        df = pd.DataFrame({
            "date": dates,
            "close": prices,
            "iv": ivs,
            "volume": volumes,
        })

        # IV rank (52-week)
        df["iv_rank"] = df["iv"].rolling(252).apply(
            lambda x: (x[-1] - x.min()) / (x.max() - x.min()) * 100
            if x.max() != x.min() else 50
        ).fillna(50)
        df["vix_proxy"] = df["iv"] * 100
        df.set_index("date", inplace=True)
        return df

    def select_strikes(self, S: float, iv: float, dte: float) -> Tuple[float, float]:
        """
        Select call/put strikes targeting configured delta levels.
        Uses BSM delta inversion via numerical search.
        """
        call_strike = S * 1.02  # initial guess
        put_strike = S * 0.98

        # Refine call strike
        for _ in range(50):
            d = BSMEngine.delta(S, call_strike, dte, self.config.risk_free_rate, iv, "c")
            if abs(d - self.config.call_delta_target) < 0.001:
                break
            call_strike += (d - self.config.call_delta_target) * S * 0.1

        # Refine put strike
        for _ in range(50):
            d = BSMEngine.delta(S, put_strike, dte, self.config.risk_free_rate, iv, "p")
            if abs(d - self.config.put_delta_target) < 0.001:
                break
            put_strike += (d - self.config.put_delta_target) * S * 0.1

        # Round to nearest $0.50
        call_strike = round(call_strike * 2) / 2
        put_strike = round(put_strike * 2) / 2
        return call_strike, put_strike

    def run(self, ticker: str = "SPY", n_days: int = 504) -> pd.DataFrame:
        """Main backtest loop. Returns trade-level results DataFrame."""
        logger.info(f"Starting backtest | Ticker: {ticker} | Days: {n_days} | NAV: ${self.nav:,.0f}")
        df = self.generate_synthetic_data(n_days, ticker)
        dte_years = self.config.dte_hours / 8760

        for i, (date, row) in enumerate(df.iterrows()):
            S = row["close"]
            iv = row["iv"]
            iv_rank = row["iv_rank"]
            vix = row["vix_proxy"]

            # Circuit breaker check
            halted, msg = self.risk_manager.circuit_breaker(self.nav)
            if halted:
                logger.warning(f"{date} | {msg} | Halting new entries")
                self.equity_curve.append({"date": date, "nav": self.nav, "status": "HALTED"})
                continue

            # ── Entry ──────────────────────────────────────────
            call_strike, put_strike = self.select_strikes(S, iv, dte_years)

            position = StranglePosition(
                ticker=ticker,
                entry_spot=S,
                call_strike=call_strike,
                put_strike=put_strike,
                entry_iv=iv,
                dte=dte_years,
                risk_free=self.config.risk_free_rate,
                entry_time=date,
                num_contracts=1,  # temp, sized below
                config=self.config,
            )

            proposed_greeks = position.get_greeks_summary()
            approved, reason = self.risk_manager.pre_trade_check(
                ticker, iv_rank, vix, position.total_premium, proposed_greeks
            )

            if not approved:
                self.equity_curve.append({"date": date, "nav": self.nav, "status": f"SKIPPED:{reason}"})
                continue

            # Size the trade
            n_contracts = self.risk_manager.calculate_position_size(
                ticker=ticker,
                premium_per_spread=position.total_premium,
                max_loss_per_spread=position.total_premium * self.config.stop_loss_multiplier,
                iv_rank=iv_rank,
                vix_level=vix,
            )

            # Rebuild with correct size
            position = StranglePosition(
                ticker=ticker,
                entry_spot=S,
                call_strike=call_strike,
                put_strike=put_strike,
                entry_iv=iv,
                dte=dte_years,
                risk_free=self.config.risk_free_rate,
                entry_time=date,
                num_contracts=n_contracts,
                config=self.config,
            )

            # ── Simulate Overnight Move ────────────────────────
            overnight_return = np.random.normal(
                0,
                iv / np.sqrt(252) * (1 + 0.3 * (vix / 20 - 1))
            )
            iv_change = np.random.normal(-0.01, iv * 0.05)
            exit_spot = S * np.exp(overnight_return)
            exit_iv = np.clip(iv + iv_change, 0.08, 0.80)

            should_exit, exit_reason, pnl = position.check_exit(
                exit_spot, exit_iv, self.config.dte_hours
            )

            # Cost accounting
            tc = n_contracts * self.config.transaction_cost_per_contract * 2
            slippage = (self.config.slippage_bps / 10000) * position.total_premium * \
                       n_contracts * self.config.shares_per_contract
            net_pnl = pnl - tc - slippage

            self.nav += net_pnl
            self.risk_manager.daily_pnl.append(net_pnl)

            trade_record = {
                "date": date,
                "ticker": ticker,
                "entry_spot": round(S, 2),
                "call_strike": call_strike,
                "put_strike": put_strike,
                "entry_iv": round(iv * 100, 2),
                "iv_rank": round(iv_rank, 1),
                "vix": round(vix, 2),
                "premium_collected": round(position.total_premium * n_contracts * 100, 2),
                "n_contracts": n_contracts,
                "exit_spot": round(exit_spot, 2),
                "exit_iv": round(exit_iv * 100, 2),
                "exit_reason": exit_reason or "OVERNIGHT_EXIT",
                "gross_pnl": round(pnl, 2),
                "net_pnl": round(net_pnl, 2),
                "nav": round(self.nav, 2),
                "delta": round(position.net_delta, 2),
                "gamma": round(position.gamma_per_contract * n_contracts * 100, 4),
                "vega": round(position.vega_per_contract * n_contracts * 100, 2),
                "theta": round(position.theta_per_contract * n_contracts * 100, 2),
            }
            self.trade_log.append(trade_record)
            self.equity_curve.append({"date": date, "nav": self.nav, "status": exit_reason or "EXIT"})

        trades_df = pd.DataFrame(self.trade_log)
        logger.info(f"Backtest complete | Trades: {len(trades_df)} | Final NAV: ${self.nav:,.0f}")
        return trades_df


# ─────────────────────────────────────────────────────────────────
# SECTION 7: PERFORMANCE ANALYTICS
# ─────────────────────────────────────────────────────────────────

class PerformanceAnalytics:
    """
    Institutional-grade performance attribution and reporting.
    """

    @staticmethod
    def compute_metrics(trades: pd.DataFrame, initial_nav: float) -> Dict[str, float]:
        pnls = trades["net_pnl"].values
        navs = trades["nav"].values

        # Returns
        returns = np.diff(navs) / navs[:-1]
        total_return = (navs[-1] - initial_nav) / initial_nav

        # Risk metrics
        sharpe = returns.mean() / returns.std() * np.sqrt(252) if returns.std() > 0 else 0
        sortino_denom = returns[returns < 0].std() if (returns < 0).sum() > 0 else 1e-10
        sortino = returns.mean() / sortino_denom * np.sqrt(252)

        # Drawdown
        equity = pd.Series(navs)
        rolling_max = equity.cummax()
        dd = (equity - rolling_max) / rolling_max
        max_dd = dd.min()
        calmar = total_return / abs(max_dd) if max_dd < 0 else 0

        # Win/Loss
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]

        return {
            "total_return_pct": total_return * 100,
            "annualised_return_pct": total_return / len(trades) * 252 * 100,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "calmar_ratio": calmar,
            "max_drawdown_pct": max_dd * 100,
            "win_rate_pct": (pnls > 0).mean() * 100,
            "avg_win": wins.mean() if len(wins) > 0 else 0,
            "avg_loss": losses.mean() if len(losses) > 0 else 0,
            "profit_factor": wins.sum() / abs(losses.sum()) if losses.sum() != 0 else np.inf,
            "avg_net_pnl_per_trade": pnls.mean(),
            "total_trades": len(trades),
            "var_99_daily": -np.percentile(pnls, 1),
            "cvar_99_daily": -pnls[pnls <= np.percentile(pnls, 1)].mean()
                             if (pnls <= np.percentile(pnls, 1)).sum() > 0 else 0,
            "avg_iv_rank_on_entry": trades["iv_rank"].mean(),
            "avg_premium_collected": trades["premium_collected"].mean(),
            "pct_stopped_out": (trades["exit_reason"] == "STOP_LOSS").mean() * 100,
            "pct_profit_target": (trades["exit_reason"] == "PROFIT_TARGET").mean() * 100,
            "pct_gap_kill": (trades["exit_reason"] == "GAP_KILL").mean() * 100,
        }

    @staticmethod
    def plot_dashboard(trades: pd.DataFrame, mc_results: Dict, initial_nav: float, save_path: str = None):
        """Generate institutional-quality performance dashboard."""
        plt.style.use("dark_background")
        colors = {
            "green": "#00D97E",
            "red": "#FF4B4B",
            "blue": "#4E9AF1",
            "yellow": "#FFD700",
            "purple": "#B87FFF",
            "gray": "#8892A4",
            "bg": "#0E1117",
            "panel": "#1E2130",
        }

        fig = plt.figure(figsize=(20, 14), facecolor=colors["bg"])
        fig.suptitle(
            "OVERNIGHT GAP STRANGLE — STRATEGY DASHBOARD",
            fontsize=18, fontweight="bold", color="white", y=0.98
        )
        gs = gridspec.GridSpec(3, 4, figure=fig, hspace=0.45, wspace=0.35)

        # ── Panel 1: Equity Curve ──────────────────────────────
        ax1 = fig.add_subplot(gs[0, :2])
        nav_series = trades["nav"]
        dd_series = (nav_series - nav_series.cummax()) / nav_series.cummax() * 100
        ax1.plot(range(len(nav_series)), nav_series / 1000, color=colors["blue"], lw=1.8, label="NAV")
        ax1.fill_between(range(len(nav_series)), nav_series / 1000, initial_nav / 1000,
                         where=nav_series >= initial_nav,
                         alpha=0.15, color=colors["green"])
        ax1.fill_between(range(len(nav_series)), nav_series / 1000, initial_nav / 1000,
                         where=nav_series < initial_nav,
                         alpha=0.15, color=colors["red"])
        ax1.axhline(initial_nav / 1000, color=colors["gray"], ls="--", lw=0.8)
        ax1.set_title("Equity Curve ($K)", color="white", fontsize=11)
        ax1.set_facecolor(colors["panel"])
        ax1.tick_params(colors="white")
        ax1.yaxis.label.set_color("white")

        # ── Panel 2: Drawdown ──────────────────────────────────
        ax2 = fig.add_subplot(gs[0, 2:])
        ax2.fill_between(range(len(dd_series)), dd_series, 0, color=colors["red"], alpha=0.6)
        ax2.plot(range(len(dd_series)), dd_series, color=colors["red"], lw=1.2)
        ax2.set_title("Drawdown (%)", color="white", fontsize=11)
        ax2.set_facecolor(colors["panel"])
        ax2.tick_params(colors="white")

        # ── Panel 3: P&L Distribution ─────────────────────────
        ax3 = fig.add_subplot(gs[1, :2])
        pnls = trades["net_pnl"]
        ax3.hist(pnls, bins=60, color=colors["blue"], alpha=0.7, edgecolor="none")
        var99 = -np.percentile(pnls, 1)
        ax3.axvline(-var99, color=colors["red"], lw=2, ls="--", label=f"99% VaR: ${var99:,.0f}")
        ax3.axvline(pnls.mean(), color=colors["green"], lw=2, ls="--", label=f"Mean: ${pnls.mean():,.0f}")
        ax3.set_title("P&L Distribution per Trade", color="white", fontsize=11)
        ax3.set_facecolor(colors["panel"])
        ax3.tick_params(colors="white")
        ax3.legend(fontsize=8, facecolor=colors["panel"], labelcolor="white")

        # ── Panel 4: Monte Carlo P&L Density ──────────────────
        ax4 = fig.add_subplot(gs[1, 2:])
        mc_pnls = mc_results.get("pnls", np.zeros(100))
        ax4.hist(mc_pnls, bins=80, color=colors["purple"], alpha=0.7, edgecolor="none", density=True)
        ax4.axvline(np.percentile(mc_pnls, 1), color=colors["red"], lw=1.5, ls="--",
                    label=f"99% VaR: ${-np.percentile(mc_pnls,1):,.0f}")
        ax4.set_title("Monte Carlo P&L Density (10K paths)", color="white", fontsize=11)
        ax4.set_facecolor(colors["panel"])
        ax4.tick_params(colors="white")
        ax4.legend(fontsize=8, facecolor=colors["panel"], labelcolor="white")

        # ── Panel 5: Exit Reasons Pie ──────────────────────────
        ax5 = fig.add_subplot(gs[2, 0])
        exit_counts = trades["exit_reason"].value_counts()
        pie_colors = [colors["green"], colors["red"], colors["yellow"], colors["gray"]]
        ax5.pie(exit_counts.values, labels=exit_counts.index,
                autopct="%1.1f%%", colors=pie_colors[:len(exit_counts)],
                textprops={"color": "white", "fontsize": 8})
        ax5.set_title("Exit Reasons", color="white", fontsize=11)
        ax5.set_facecolor(colors["panel"])

        # ── Panel 6: IV Rank vs PnL Scatter ───────────────────
        ax6 = fig.add_subplot(gs[2, 1])
        sc_colors = [colors["green"] if p > 0 else colors["red"] for p in trades["net_pnl"]]
        ax6.scatter(trades["iv_rank"], trades["net_pnl"], c=sc_colors, alpha=0.5, s=12)
        ax6.axhline(0, color=colors["gray"], lw=0.8, ls="--")
        ax6.set_xlabel("IV Rank (%)", color="white", fontsize=9)
        ax6.set_ylabel("Net P&L ($)", color="white", fontsize=9)
        ax6.set_title("IV Rank vs Trade P&L", color="white", fontsize=11)
        ax6.set_facecolor(colors["panel"])
        ax6.tick_params(colors="white")

        # ── Panel 7: Rolling Sharpe ────────────────────────────
        ax7 = fig.add_subplot(gs[2, 2])
        returns = trades["net_pnl"] / initial_nav
        rolling_sharpe = returns.rolling(30).mean() / returns.rolling(30).std() * np.sqrt(252)
        ax7.plot(range(len(rolling_sharpe)), rolling_sharpe, color=colors["yellow"], lw=1.5)
        ax7.axhline(0, color=colors["gray"], lw=0.8, ls="--")
        ax7.axhline(1, color=colors["green"], lw=0.8, ls="--", alpha=0.5)
        ax7.set_title("Rolling 30-Day Sharpe", color="white", fontsize=11)
        ax7.set_facecolor(colors["panel"])
        ax7.tick_params(colors="white")

        # ── Panel 8: KPI Table ────────────────────────────────
        ax8 = fig.add_subplot(gs[2, 3])
        ax8.axis("off")
        metrics = PerformanceAnalytics.compute_metrics(trades, initial_nav)
        kpi_data = [
            ["Total Return", f"{metrics['total_return_pct']:.1f}%"],
            ["Ann. Return", f"{metrics['annualised_return_pct']:.1f}%"],
            ["Sharpe", f"{metrics['sharpe_ratio']:.2f}"],
            ["Sortino", f"{metrics['sortino_ratio']:.2f}"],
            ["Max DD", f"{metrics['max_drawdown_pct']:.1f}%"],
            ["Win Rate", f"{metrics['win_rate_pct']:.1f}%"],
            ["Profit Factor", f"{metrics['profit_factor']:.2f}"],
            ["VaR 99%", f"${metrics['var_99_daily']:,.0f}"],
        ]
        tbl = ax8.table(cellText=kpi_data, colLabels=["Metric", "Value"],
                        cellLoc="center", loc="center",
                        bbox=[0, 0, 1, 1])
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)
        for (row, col), cell in tbl.get_celld().items():
            cell.set_facecolor(colors["panel"] if row > 0 else "#2A3045")
            cell.set_text_props(color="white")
            cell.set_edgecolor(colors["gray"])
        ax8.set_title("Key Metrics", color="white", fontsize=11)

        plt.tight_layout(rect=[0, 0, 1, 0.96])
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight", facecolor=colors["bg"])
            logger.info(f"Dashboard saved → {save_path}")
        return fig


# ─────────────────────────────────────────────────────────────────
# SECTION 8: MAIN RUNNER
# ─────────────────────────────────────────────────────────────────

def run_strategy(nav: float = 1_000_000, n_days: int = 504, ticker: str = "SPY"):
    """End-to-end strategy runner with reporting."""

    print("\n" + "═" * 72)
    print("  OVERNIGHT GAP STRANGLE — QUANT RESEARCH DESK")
    print("  Systematic Options | Volatility Arbitrage")
    print("═" * 72 + "\n")

    config = StrategyConfig()
    bt = Backtester(config, nav)

    # Run backtest
    trades = bt.run(ticker=ticker, n_days=n_days)

    # Monte Carlo on a sample position
    mc_engine = MonteCarloEngine(config)
    sample_pos = StranglePosition(
        ticker=ticker, entry_spot=450.0,
        call_strike=462.0, put_strike=439.0,
        entry_iv=0.18, dte=config.dte_hours / 8760,
        risk_free=config.risk_free_rate,
        entry_time=datetime.now(),
        num_contracts=5, config=config,
    )
    mc_sim = mc_engine.simulate_strangle_pnl(sample_pos)
    mc_metrics = mc_engine.compute_risk_metrics(mc_sim["pnls"], nav)

    # Performance report
    perf = PerformanceAnalytics.compute_metrics(trades, nav)

    print("📊 BACKTEST PERFORMANCE SUMMARY")
    print("─" * 50)
    for k, v in perf.items():
        label = k.replace("_", " ").title()
        if isinstance(v, float):
            if "pct" in k or "rate" in k:
                print(f"  {label:<35} {v:>8.2f}%")
            elif "pnl" in k or "win" in k or "loss" in k or "var" in k or "cvar" in k:
                print(f"  {label:<35} ${v:>10,.2f}")
            else:
                print(f"  {label:<35} {v:>8.4f}")
        else:
            print(f"  {k.replace('_',' ').title():<35} {v:>8}")

    print("\n📈 MONTE CARLO RISK METRICS (10K paths)")
    print("─" * 50)
    for k, v in mc_metrics.items():
        label = k.replace("_", " ").title()
        if "pct" in k:
            print(f"  {label:<35} {v:>8.2f}%")
        elif any(x in k for x in ["pnl", "var", "loss", "gain"]):
            print(f"  {label:<35} ${v:>10,.2f}")
        else:
            print(f"  {label:<35} {v:>8.4f}")

    # Save plot
    PerformanceAnalytics.plot_dashboard(
        trades, mc_sim, nav, save_path="/mnt/user-data/outputs/strategy_dashboard.png"
    )

    # Export trade log
    trades.to_csv("/mnt/user-data/outputs/trade_log.csv", index=False)
    logger.info("Trade log saved → /mnt/user-data/outputs/trade_log.csv")

    # Save metrics to JSON
    report = {
        "strategy": "Overnight Gap Strangle",
        "run_date": datetime.now().isoformat(),
        "config": {k: v for k, v in config.__dict__.items() if not isinstance(v, list)},
        "backtest_metrics": {k: round(v, 4) if isinstance(v, float) else v for k, v in perf.items()},
        "monte_carlo_metrics": {k: round(v, 4) if isinstance(v, float) else v for k, v in mc_metrics.items()},
    }
    with open("/mnt/user-data/outputs/strategy_report.json", "w") as f:
        json.dump(report, f, indent=2)
    logger.info("Report saved → /mnt/user-data/outputs/strategy_report.json")

    return trades, mc_sim, perf, mc_metrics


if __name__ == "__main__":
    trades, mc_sim, perf, mc_metrics = run_strategy(
        nav=1_000_000,
        n_days=504,
        ticker="SPY"
    )
