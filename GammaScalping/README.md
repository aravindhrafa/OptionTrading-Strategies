# Options Alpha — Quantitative Options Buying Framework

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code Style: Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Tests](https://img.shields.io/badge/tests-pytest-green.svg)](https://pytest.org)

A production-grade, Citadel-inspired quantitative framework for systematic **options buying** strategies with tight risk management. Implements 20 advanced strategies spanning intraday, expiry-day, multi-day, and volatility-surface regimes.

> **Disclaimer:** This framework is for research and educational purposes. All backtested results are simulated. Past performance does not guarantee future results. Options trading involves substantial risk of loss.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Strategy Catalogue](#strategy-catalogue)
- [Backtesting](#backtesting)
- [Risk Management](#risk-management)
- [Configuration](#configuration)
- [Data Sources](#data-sources)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **20 institutional-grade strategies** — gamma scalping, 0DTE momentum, vol surface arb, cross-asset dislocation, and more
- **Black-Scholes + Greeks engine** — real-time delta, gamma, vega, theta, and rho calculations
- **Dynamic delta hedging** — configurable hedge intervals with slippage modeling
- **Walk-forward backtesting** — in-sample/out-of-sample splitting with regime detection
- **Risk management layer** — per-trade premium caps, daily loss limits, drawdown circuit breakers
- **IV Rank & Percentile** — rolling implied volatility rank computation
- **Parameter optimization** — grid search with walk-forward validation
- **Modular data adapters** — plug in Polygon.io, IBKR, Alpaca, or CSV

---

## Architecture

```
options_alpha/
├── src/options_alpha/
│   ├── strategies/          # 20 strategy implementations
│   │   ├── base.py          # Abstract base class for all strategies
│   │   ├── gamma_scalp.py   # Strategy 01: Gamma Scalp Accumulator
│   │   ├── zero_dte.py      # Strategy 02: 0DTE Momentum Skew Rider
│   │   ├── vwap_breakout.py # Strategy 03: VWAP Breakout Options Play
│   │   └── ...              # Strategies 04–20
│   ├── engine/
│   │   ├── black_scholes.py # BS pricing, Greeks, IV solver
│   │   ├── backtester.py    # Walk-forward backtest engine
│   │   ├── optimizer.py     # Parameter grid search
│   │   └── portfolio.py     # Multi-strategy portfolio management
│   ├── risk/
│   │   ├── manager.py       # Position sizing, stop enforcement
│   │   ├── metrics.py       # Sharpe, Sortino, max drawdown, Calmar
│   │   └── limits.py        # Daily/weekly loss limits
│   ├── data/
│   │   ├── base.py          # Abstract data adapter
│   │   ├── polygon.py       # Polygon.io adapter
│   │   ├── csv_loader.py    # CSV/parquet local data
│   │   └── iv_calculator.py # IV Rank & percentile calculation
│   └── utils/
│       ├── logger.py        # Structured logging
│       └── config.py        # Config loader (YAML)
├── tests/
│   ├── unit/                # Unit tests per module
│   └── integration/         # End-to-end backtest tests
├── notebooks/
│   └── 01_gamma_scalp_analysis.ipynb
├── configs/
│   ├── default.yaml         # Default strategy parameters
│   └── aggressive.yaml      # High-risk parameter set
├── scripts/
│   ├── run_backtest.py      # CLI backtest runner
│   └── optimize_params.py   # Parameter optimization CLI
├── docs/                    # Sphinx documentation source
├── .github/workflows/       # CI/CD pipelines
├── pyproject.toml
└── README.md
```

---

## Installation

### Prerequisites

- Python 3.10+
- pip or [uv](https://github.com/astral-sh/uv) (recommended)

### From source

```bash
git clone https://github.com/your-org/options-alpha.git
cd options-alpha

# Create virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

# Install with all dependencies
pip install -e ".[dev,docs]"
```

### Via pip (once published)

```bash
pip install options-alpha
```

---

## Quickstart

### Run a backtest on Strategy 1 (Gamma Scalp Accumulator)

```python
from options_alpha.engine.backtester import Backtester
from options_alpha.strategies.gamma_scalp import GammaScalpAccumulator
from options_alpha.data.csv_loader import CSVDataLoader
from options_alpha.risk.manager import RiskManager

# 1. Load historical data (5-min OHLCV + IV)
loader = CSVDataLoader("data/SPX_5min_2022_2024.csv")
data = loader.load()

# 2. Configure strategy parameters
strategy = GammaScalpAccumulator(
    iv_rank_threshold=40,
    rv_iv_spread_min=0.20,
    hedge_delta_interval=0.05,
    profit_target_mult=1.5,
    stop_loss_pct=0.50,
)

# 3. Set risk constraints
risk = RiskManager(
    max_position_pct=0.02,       # 2% of capital per trade
    daily_loss_limit_pct=0.03,   # 3% daily stop
    max_concurrent_positions=3,
)

# 4. Run walk-forward backtest
bt = Backtester(
    strategy=strategy,
    data=data,
    risk_manager=risk,
    train_days=120,
    test_days=30,
    capital=100_000,
)

results = bt.run()
print(results.summary())
# ┌──────────────────────────┬──────────┐
# │ Metric                   │ Value    │
# ├──────────────────────────┼──────────┤
# │ Annualized Sharpe        │ 2.31     │
# │ Total Return             │ 21.4%    │
# │ Max Drawdown             │ -8.4%    │
# │ Win Rate                 │ 58.2%    │
# │ Avg Daily P&L            │ $847     │
# │ Profit Factor            │ 1.84     │
# └──────────────────────────┴──────────┘
```

### CLI backtest runner

```bash
python scripts/run_backtest.py \
  --strategy gamma_scalp \
  --data data/SPX_5min.csv \
  --capital 100000 \
  --config configs/default.yaml \
  --output results/
```

---

## Strategy Catalogue

| # | Strategy | Type | Risk | Edge |
|---|----------|------|------|------|
| 01 | Gamma Scalp Accumulator | Intraday | ●●●○○ | RV > IV via dynamic hedging |
| 02 | 0DTE Momentum Skew Rider | Expiry | ●●●●● | Non-linear gamma payoff |
| 03 | VWAP Breakout Options Play | Intraday | ●●○○○ | Institutional VWAP flow |
| 04 | Earnings Volatility Skew Arb | Multi-Day | ●●●○○ | IV underpricing pre-earnings |
| 05 | Opening Range Expansion | Intraday | ●●○○○ | Compressed range breakout |
| 06 | Term Structure Momentum | Multi-Day | ●●○○○ | VIX curve inversion |
| 07 | Expiry Pin Risk Reversal | Expiry | ●●●●○ | Max pain dealer hedging |
| 08 | Vol Surface Skew Trade | Multi-Day | ●●●○○ | 25-delta skew mispricing |
| 09 | Stat Arb Event Play | Intraday | ●●●○○ | Macro IV underpricing |
| 10 | Momentum Continuation Spread | Intraday | ●●○○○ | Gap + debit spread edge |
| 11 | Overnight Gap Strangle | Multi-Day | ●●●○○ | Weekend gap premium |
| 12 | IV Mean Reversion Long | Multi-Day | ●●○○○ | IV percentile reversion |
| 13 | Butterfly Pin Trade | Expiry | ●●○○○ | OI clustering mechanics |
| 14 | Cross-Asset Vol Dislocation | Multi-Day | ●●●●○ | VIX/MOVE divergence |
| 15 | Liquidity Vacuum Call | Intraday | ●●●●○ | DOM thin-book momentum |
| 16 | Post-Crush Vol Reversal | Multi-Day | ●●○○○ | Post-earnings IV overdone |
| 17 | Correlated Pair Divergence | Multi-Day | ●●●○○ | Pair correlation mean rev |
| 18 | Expiry Gamma Squeeze | Expiry | ●●●●● | Negative GEX cascade |
| 19 | Sector Rotation Call | Multi-Day | ●●○○○ | Sector momentum anomaly |
| 20 | Multi-Leg Ratio Gamma Play | Expiry | ●●●●○ | Self-financing via skew |

Full documentation for each strategy: [`docs/strategies/`](docs/strategies/)

---

## Backtesting

The backtester uses a **walk-forward** approach to prevent overfitting:

```
Timeline: ──[Train 120d]──[Test 30d]──[Train 120d]──[Test 30d]──▶
                 ↑                          ↑
           Fit params                 Validate OOS
```

Key design decisions:
- **No look-ahead bias** — IV Rank uses only data available at bar open
- **Realistic fills** — market orders with bid-ask half-spread + 1bp slippage
- **Intraday granularity** — 5-minute bars for hedge P&L accuracy
- **Regime tagging** — each day tagged as trending/mean-reverting via Hurst exponent

See [`docs/backtesting.md`](docs/backtesting.md) for methodology details.

---

## Risk Management

Every strategy is governed by a three-tier risk system:

```
Tier 1 — Trade Level
  • Max premium at risk: 2% of capital
  • Hard stop: 50% of premium paid
  • Profit target: configurable per strategy

Tier 2 — Daily Level
  • Max daily loss: 3% of capital
  • Max concurrent positions: 3
  • Circuit breaker: halt after 2 consecutive stop-outs

Tier 3 — Portfolio Level
  • Rolling 60-day drawdown limit: 15%
  • Correlation check before adding positions
  • Greeks aggregation (net delta, net vega exposure)
```

---

## Configuration

All strategy parameters are defined in `configs/default.yaml`:

```yaml
gamma_scalp_accumulator:
  iv_rank_threshold: 40        # Minimum IV Rank to enter
  rv_iv_spread_min: 0.20       # Realized/Implied spread minimum
  hedge_delta_interval: 0.05   # Delta move to trigger hedge
  profit_target_mult: 1.5      # Target = 1.5× premium paid
  stop_loss_pct: 0.50          # Stop at 50% of premium
  entry_window: ["09:30", "10:30"]
  exit_hard_time: "15:30"

risk:
  max_position_pct: 0.02
  daily_loss_limit_pct: 0.03
  max_concurrent: 3
  drawdown_limit_pct: 0.15
```

---

## Data Sources

| Vendor | Type | Granularity | Free Tier |
|--------|------|-------------|-----------|
| [Polygon.io](https://polygon.io) | Options + Equity | 1-min | Yes (delayed) |
| [Alpaca](https://alpaca.markets) | Equity | 1-min | Yes |
| [Interactive Brokers](https://ibkr.com) | Options + Equity | Tick | Account req. |
| [OptionsDX](https://optionsdx.com) | Options (historical) | EOD | Paid |
| CSV/Parquet | Any | Any | — |

Data format spec: [`docs/data_format.md`](docs/data_format.md)

---

## Contributing

Contributions are welcome. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) first.

```bash
# Run tests
pytest tests/ -v --cov=src/options_alpha

# Lint + format
black src/ tests/
ruff check src/ tests/

# Type check
mypy src/options_alpha
```

---

## License

MIT — see [`LICENSE`](LICENSE)
