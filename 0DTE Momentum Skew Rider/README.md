# 0DTE Momentum Skew Rider

> Institutional-grade 0DTE options strategy with HFT-class risk management.

```
┌─────────────────────────────────────────────────────────────────────┐
│  RISK DISCLAIMER: This is for educational/research purposes only.   │
│  0DTE options carry extreme gamma risk. Never trade with capital     │
│  you cannot afford to lose entirely. This is NOT financial advice.  │
└─────────────────────────────────────────────────────────────────────┘
```

## Strategy Architecture

```
Market Data Feed
      │
      ▼
┌─────────────┐    ┌──────────────┐    ┌───────────────┐
│  Signal     │───▶│  Risk        │───▶│  Execution    │
│  Engine     │    │  Guardian    │    │  Engine       │
│  (Skew+Mom) │    │  (Pre-trade) │    │  (Smart OMS)  │
└─────────────┘    └──────────────┘    └───────────────┘
      │                  │                     │
      ▼                  ▼                     ▼
┌─────────────┐    ┌──────────────┐    ┌───────────────┐
│  Greeks     │    │  Circuit     │    │  Position     │
│  Monitor   │    │  Breakers    │    │  Monitor      │
└─────────────┘    └──────────────┘    └───────────────┘
```

## Core Edge

The strategy exploits **3 simultaneous edges**:

| Edge | Description | Alpha Source |
|------|-------------|--------------|
| **Momentum Skew** | IV skew shifts lead underlying moves by 2-8 min | Put/Call IV spread divergence |
| **GEX Pinning** | Gamma Exposure creates intraday gravity wells | Dealer hedging flows |
| **VWAP Reversion** | 0DTE options misprice post-VWAP-break reversion | Retail flow imbalance |

## Risk Management (Jane Street Style)

- **Pre-trade**: Greeks budget, VaR check, correlation throttle
- **Real-time**: Delta-hedge every tick, gamma scalp, vega cap
- **Portfolio**: Kelly-sized, max 3 concurrent positions, hard stop circuit breakers
- **Session**: Kill switch, daily P&L floor, exposure sunset rules

## Project Structure

```
0dte-momentum-skew-rider/
├── src/
│   ├── core/
│   │   ├── strategy.py          # Main orchestrator
│   │   ├── portfolio.py         # Position tracking
│   │   └── session.py           # Session lifecycle
│   ├── signals/
│   │   ├── skew_signal.py       # IV skew momentum detector
│   │   ├── gex_signal.py        # Gamma exposure calculator
│   │   └── composite_signal.py  # Signal combiner with weights
│   ├── risk/
│   │   ├── guardian.py          # Pre-trade risk checks
│   │   ├── greeks_monitor.py    # Real-time Greeks tracking
│   │   ├── circuit_breaker.py   # Kill switches
│   │   └── position_sizer.py    # Kelly + vol-adjusted sizing
│   ├── execution/
│   │   ├── order_manager.py     # Smart order routing
│   │   ├── fill_tracker.py      # Slippage analytics
│   │   └── broker_adapters/     # Broker-specific adapters
│   │       ├── base.py
│   │       ├── tastytrade.py
│   │       └── ibkr.py
│   └── utils/
│       ├── logger.py            # Structured logging
│       ├── metrics.py           # Sharpe, Sortino, Calmar
│       └── time_utils.py        # Market hours, expiry
├── config/
│   ├── base_config.yaml         # Base parameters
│   ├── risk_limits.yaml         # All risk thresholds
│   └── signals_config.yaml      # Signal parameters
├── tests/
│   ├── test_signals.py
│   ├── test_risk.py
│   └── test_execution.py
├── scripts/
│   ├── backtest.py              # Historical backtester
│   └── paper_trade.py          # Paper trading runner
├── requirements.txt
├── .env.example
└── main.py                      # Entry point
```

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/0dte-momentum-skew-rider
cd 0dte-momentum-skew-rider
pip install -r requirements.txt
cp .env.example .env             # Fill in API keys
python scripts/paper_trade.py    # Always paper trade first
```

## ⚠️ Critical Rules Before Live Trading

1. Paper trade minimum **60 sessions** before going live
2. Start with `MAX_POSITION_CONTRACTS = 1`
3. Set `DAILY_LOSS_LIMIT` to max 1% of account
4. Never trade within 30 min of FOMC, CPI, or major events
5. Review fill quality reports weekly

---
*Built with institutional discipline. Respect the risk.*
