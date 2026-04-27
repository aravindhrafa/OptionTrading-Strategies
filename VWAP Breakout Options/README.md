# VWAP Breakout Options Play

> Institutional-grade VWAP breakout options strategy with HFT-class risk management.

```
┌─────────────────────────────────────────────────────────────────────┐
│  RISK DISCLAIMER: This is for educational/research purposes only.   │
│  Options trading carries substantial risk of loss. Never trade      │
│  with capital you cannot afford to lose entirely.                   │
│  This is NOT financial advice.                                      │
└─────────────────────────────────────────────────────────────────────┘
```

## Strategy Architecture

```
Market Data Feed (Price + Volume + Options Chain)
                │
                ▼
┌───────────────────────────────────────────────────────────┐
│                    SIGNAL ENGINE                          │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────┐  │
│  │ VWAP Signal │  │ Vol Profile │  │ Options Flow     │  │
│  │ (Primary)   │  │ (Confirm)   │  │ (Institutional)  │  │
│  └─────────────┘  └─────────────┘  └──────────────────┘  │
└───────────────────────────────────────────────────────────┘
                │
                ▼
┌───────────────────────────────────────────────────────────┐
│                  RISK GUARDIAN                            │
│  Pre-trade Greeks • Capital Check • IV Check • Edge Check │
└───────────────────────────────────────────────────────────┘
                │
                ▼
┌───────────────────────────────────────────────────────────┐
│              EXECUTION ENGINE                             │
│  Kelly Sizing • Limit Orders • Fill Tracking • Hedging    │
└───────────────────────────────────────────────────────────┘
                │
                ▼
┌───────────────────────────────────────────────────────────┐
│            REAL-TIME POSITION MONITOR                     │
│  VWAP Re-entry Check • Stop Management • Profit Targets   │
└───────────────────────────────────────────────────────────┘
```

## Core Strategy Logic

### The 3 Edges Exploited

| Edge | Mechanism | Alpha Source |
|------|-----------|--------------|
| **VWAP Breakout** | Price breaks VWAP with volume surge → momentum continuation | Institutional order flow anchoring |
| **Volume Profile** | High-volume nodes act as support/resistance after VWAP break | Market microstructure |
| **Options Flow** | Unusual call/put buying precedes breakout confirmation | Informed order flow detection |

### VWAP Breakout Regimes

```
Type A — Clean Break:
  Price crosses VWAP + Volume > 2×ADTV + Retest holds
  → Buy OTM call/put debit spread (direction of break)
  → Target: 1st standard deviation VWAP band

Type B — Volume Exhaustion Fade:
  Price extends 1.5σ from VWAP + Volume declining + RSI divergence
  → Fade the move via iron condor / mean-reversion spread
  → Target: VWAP recapture (mean reversion)

Type C — Institutional VWAP Hunt:
  Price consolidating near VWAP + Options unusual activity
  → Directional debit spread aligned with options flow
  → Target: 2σ VWAP band
```

## Risk Management Framework

```
┌─────────────────────────────────────────────┐
│          RISK LAYER HIERARCHY               │
│                                             │
│  L1: Hard Limits (never crossed)            │
│      └── Daily loss cap, position cap       │
│                                             │
│  L2: Greeks Budget (pre-trade)              │
│      └── Delta, Gamma, Vega, Charm          │
│                                             │
│  L3: Trade-Level Stops                      │
│      └── VWAP retest fail, vol spike        │
│                                             │
│  L4: System Health                          │
│      └── Data quality, latency, fill rate   │
└─────────────────────────────────────────────┘
```

## Project Structure

```
vwap-breakout-options/
├── src/
│   ├── core/
│   │   ├── strategy.py          # Main orchestrator
│   │   ├── portfolio.py         # Position & P&L tracking
│   │   └── session.py           # Session lifecycle
│   ├── signals/
│   │   ├── vwap_signal.py       # VWAP breakout detector (primary)
│   │   ├── volume_profile.py    # Volume profile & POC analysis
│   │   ├── options_flow.py      # Unusual options activity scanner
│   │   └── composite_signal.py  # Multi-factor signal combiner
│   ├── risk/
│   │   ├── guardian.py          # Pre-trade risk checks (12 gates)
│   │   ├── greeks_monitor.py    # Real-time Greeks tracking
│   │   ├── circuit_breaker.py   # Kill switches (8 triggers)
│   │   └── position_sizer.py    # Kelly + vol-adjusted sizing
│   ├── execution/
│   │   ├── order_manager.py     # Smart order routing
│   │   ├── fill_tracker.py      # Slippage analytics
│   │   └── broker_adapters/
│   │       ├── base.py
│   │       ├── tastytrade.py
│   │       └── ibkr.py
│   └── utils/
│       ├── logger.py
│       ├── metrics.py
│       └── time_utils.py
├── config/
│   ├── base_config.yaml
│   ├── risk_limits.yaml
│   └── signals_config.yaml
├── tests/
│   ├── test_vwap_signal.py
│   ├── test_risk.py
│   └── test_execution.py
├── scripts/
│   ├── backtest.py
│   ├── paper_trade.py
│   └── push_to_github.sh
├── requirements.txt
├── .env.example
├── .gitignore
└── main.py
```

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/vwap-breakout-options
cd vwap-breakout-options
pip install -r requirements.txt
cp .env.example .env             # Fill in your API keys
python scripts/paper_trade.py   # ALWAYS paper trade first
```

## ⚠️ Critical Rules Before Live Trading

1. Paper trade minimum **60 sessions** with documented results
2. Start with `MAX_POSITION_CONTRACTS = 1`
3. `DAILY_LOSS_LIMIT` = max 1% of account
4. **Never trade VWAP breakouts during the first 15 minutes** (erratic volume profile)
5. Never trade within 30 min of major macro releases (FOMC, CPI, NFP)
6. Require VWAP retest confirmation before full-size entry

---
*Built with institutional discipline. Respect the risk.*
