# Earnings Volatility Skew Arbitrage

> Institutional-grade earnings IV skew arbitrage with Jane Street-class risk management.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  RISK DISCLAIMER: For educational/research purposes only. Earnings      │
│  options carry extreme event risk — IV can collapse 50-70% overnight.  │
│  Gaps through stops are common. Never trade with capital you cannot     │
│  afford to lose entirely. This is NOT financial advice.                 │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Strategy Overview: What Is Earnings Volatility Skew Arbitrage?

### The Core Insight

Before earnings, options market makers face a fundamental problem:
they don't know if the stock will beat or miss, but they **know** a big
move is coming. They respond by inflating implied volatility (IV) on
**all** strikes — but not equally.

The market's **fear** creates systematic mispricings between strikes:

```
Typical Pre-Earnings IV Term Structure (AAPL example):

IV%
 80 │    ●  ← 25Δ Put (most expensive — downside fear)
    │   ● ●
 70 │  ●   ●
    │ ●     ●
 60 │●       ● ← 25Δ Call
    │         ●
 50 │          ● ← ATM (IV crush target zone)
    └─────────────────────────
     OTM Put  ATM  OTM Call
         Strike →
```

This **skew** — where OTM puts carry much higher IV than equivalent
OTM calls — is reliably **overstated** before earnings. The market
consistently overprices downside fear relative to what actually
materializes post-earnings.

### The Three Edges We Exploit

| # | Edge | Mechanism | Average Edge |
|---|------|-----------|-------------|
| **1** | **IV Crush Capture** | Sell high pre-earnings IV, buy back at post-earnings IV | 40-60 bps/trade |
| **2** | **Put-Call Skew Arbitrage** | Exploit systematic overpricing of OTM puts vs calls | 25-40 bps/trade |
| **3** | **Term Structure Mis-pricing** | Front-month earnings expiry vs back-month "normal" IV | 30-50 bps/trade |

---

## Strategy Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     SIGNAL ENGINE                                   │
│                                                                     │
│  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────┐   │
│  │  IV Skew Signal  │  │  Term Structure  │  │  Historical IV  │   │
│  │                  │  │  Analyzer        │  │  Analysis       │   │
│  │ • Put/Call skew  │  │ • Front vs back  │  │ • Expected move │   │
│  │ • Risk reversal  │  │   month spread   │  │ • Realized vol  │   │
│  │ • Wing premium   │  │ • Earnings bump  │  │ • IV percentile │   │
│  │ • Skew velocity  │  │ • Post-crush est │  │ • Sector comps  │   │
│  └──────────────────┘  └──────────────────┘  └─────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    EDGE QUANTIFIER                                  │
│                                                                     │
│  • Historical IV crush magnitude (ticker-specific calibration)      │
│  • Skew richness vs sector average                                  │
│  • Expected move vs historical realized earnings moves              │
│  • Options flow — is smart money selling or buying vol?             │
└─────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  TRADE STRUCTURE SELECTOR                           │
│                                                                     │
│  Skew Rich + Flat Expected Move  → Iron Condor (sell vol both sides)│
│  Skew Rich + Bearish Lean        → Risk Reversal (buy call, sell put)│
│  Extreme Put Premium             → Put Ratio Spread (sell 2 buy 1)  │
│  Term Structure Steep            → Calendar Spread (sell front)     │
│  High Confidence Directional     → Debit Spread (defined risk)      │
└─────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│              RISK GUARDIAN (12 Pre-Trade Gates)                     │
│                                                                     │
│  Capital • Greeks • Earnings Calendar • Sector Correlation           │
│  Liquidity • IV Rank • Event Risk • Gap Risk • Sizing               │
└─────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│               EXECUTION ENGINE                                      │
│                                                                     │
│  Pre-earnings entry (1-5 days before) → Overnight hold             │
│  Post-earnings close (first 30 min of next session)                │
│  Emergency stop if stock gaps beyond 2× expected move              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Core Trade Structures

### Structure 1: Iron Condor (Most Common — ~50% of trades)
```
Profit Zone
    │ ╔═══════════════════╗ │
    │ ║    MAX PROFIT     ║ │
    │ ╚═══════════════════╝ │
────┼───────┬─────────┬────┼──── Price
   Put     Put      Call  Call
  wing    short    short  wing

Entry: 1-3 days before earnings
Exit:  Morning after earnings (IV crushed)
Edge:  Sell inflated IV on both sides, profit from crush
Risk:  Stock gaps beyond wings (defined max loss)
```

### Structure 2: Risk Reversal (Skew Arb — ~25% of trades)
```
When put skew is extremely rich vs calls:
  Buy OTM Call (cheap IV)  +  Sell OTM Put (expensive IV)

Profits if:
  (a) Stock moves up (you're long delta)
  (b) Skew normalizes (put IV falls faster than call IV)
  (c) IV crush is symmetrical (net credit/small debit)

Risk: Stock drops significantly — put exposure
```

### Structure 3: Calendar Spread (Term Structure — ~15% of trades)
```
Sell front-month (earnings expiry) options
Buy back-month (post-earnings) options

Edge: Front-month has "earnings bump" IV premium that collapses
      Back-month retains its "normal" IV level
Result: Short a high-IV option, long a normal-IV option
Risk: Large gap move (both legs lose)
```

### Structure 4: Debit Spread (Directional — ~10% of trades)
```
When historical pattern + options flow suggests direction:
  Buy ATM option + Sell OTM option (same expiry)

Lower risk, lower reward — used when skew suggests
institutional flow in one direction
```

---

## The IV Crush Mechanism

```
Timeline:
  Day -5    Day -2    Day -1    EARNINGS    Day +1    Day +5
    │          │         │         │           │          │
    ▼          ▼         ▼         ▼           ▼          ▼
  IV=45%    IV=65%    IV=80%   [REPORT]    IV=30%     IV=28%
             │ IV building ──────────┤ IV CRUSH │──────────
             │                      │          │
             │ We SELL here        │         │ We BUY here
                                  Earnings   (buy back cheap)

IV Crush magnitude by sector (historical averages):
  Tech (AAPL, MSFT, GOOGL):    40-55% crush
  Biotech (before FDA + earn): 55-75% crush (extreme!)
  Financials (JPM, BAC):       25-40% crush
  Energy (XOM, CVX):           20-35% crush
  Retail (AMZN, WMT):          35-50% crush
```

---

## Risk Management Framework

### Pre-Trade Risk Gates (12 checks)
1. **Earnings calendar validation** — Confirmed date, not estimated
2. **Capital at risk** — Max 1% per trade, 4% total earnings exposure
3. **IV rank check** — Only trade when IV rank > 60 (vol is actually rich)
4. **Expected move validation** — Options EM vs historical realized EM
5. **Greeks budget** — Portfolio delta/gamma/vega within limits
6. **Sector correlation** — Cap earnings trades in same sector
7. **Liquidity gate** — OI, bid-ask, volume minimums
8. **Wing gap protection** — Wings set beyond 2× expected move
9. **Gap risk assessment** — Historical gap magnitude for this ticker
10. **Options flow check** — Smart money buying or selling vol?
11. **Earnings surprise history** — Beats/misses/whisper calibration
12. **Fat-finger protection** — Price sanity, size limits

### During-Trade Risk Rules
- **Hard stop**: If stock gaps > 2.5× expected move → emergency close
- **Delta hedge**: If net delta exceeds threshold, hedge with underlying
- **IV spike stop**: If post-report IV rises (squeeze) → exit
- **Gamma limit**: Reduce position as gamma accelerates into expiry
- **Correlation monitor**: If sector-wide vol spike → reduce all

### Post-Earnings Exit Rules
```
Priority 1: Close in first 30 minutes after open (IV fully crushed)
Priority 2: Close at 50% of max profit (don't get greedy)
Priority 3: Close at 25% remaining time value (theta irrelevant)
Hard stop:  Close if unrealized loss > 75% of max profit
Never hold an earnings options position to expiry
```

---

## Edge Quantification

### Historical IV Crush Database
We track for each ticker:
- Median IV 1 day before earnings (pre-crush IV)
- Median IV 1 day after earnings (post-crush IV)
- Median crush magnitude (%)
- Standard deviation of crush
- Crush consistency (% of earnings where crush > 30%)
- Historical stock move vs options expected move

### Skew Richness Score
```
Skew Richness = (25Δ Put IV - 25Δ Call IV) / ATM IV

Score > 0.20:  Puts very rich vs calls → Risk reversal opportunity
Score 0.10-0.20: Moderate skew → Iron condor with put-heavy structure
Score < 0.10:  Balanced skew → Standard iron condor
Score < 0:     Inverted skew → Unusual, investigate before trading
```

---

## Project Structure

```
earnings-vol-skew-arb/
├── src/
│   ├── core/
│   │   ├── strategy.py              # Main orchestrator
│   │   ├── portfolio.py             # Position & Greeks tracking
│   │   ├── session.py               # Session & earnings calendar
│   │   └── earnings_calendar.py     # Earnings date management
│   ├── signals/
│   │   ├── iv_skew_signal.py        # Put/call skew analysis (PRIMARY)
│   │   ├── term_structure.py        # Front/back month IV analysis
│   │   ├── historical_iv.py         # Historical IV crush database
│   │   ├── options_flow.py          # Smart money flow detection
│   │   └── composite_signal.py      # Multi-factor signal combiner
│   ├── risk/
│   │   ├── guardian.py              # 12-gate pre-trade checker
│   │   ├── greeks_monitor.py        # Real-time Greeks
│   │   ├── circuit_breaker.py       # Kill switches
│   │   ├── position_sizer.py        # Kelly + vol-adjusted sizing
│   │   └── gap_risk.py              # Earnings gap risk assessment
│   ├── execution/
│   │   ├── order_manager.py         # Smart order routing
│   │   ├── entry_timer.py           # Optimal pre-earnings entry timing
│   │   ├── exit_manager.py          # Post-earnings exit logic
│   │   └── broker_adapters/
│   │       ├── base.py
│   │       ├── tastytrade.py
│   │       └── ibkr.py
│   └── utils/
│       ├── logger.py
│       ├── metrics.py
│       ├── iv_utils.py              # Black-Scholes, Greeks calculations
│       └── time_utils.py
├── config/
│   ├── base_config.yaml
│   ├── risk_limits.yaml
│   └── ticker_calibration.yaml     # Per-ticker historical IV data
├── data/
│   └── iv_crush_history.csv        # Historical IV crush database
├── tests/
│   ├── test_iv_skew_signal.py
│   ├── test_term_structure.py
│   ├── test_risk.py
│   └── test_execution.py
├── scripts/
│   ├── backtest.py
│   ├── paper_trade.py
│   ├── build_iv_database.py        # Populate historical IV crush data
│   └── push_to_github.sh
├── requirements.txt
├── .env.example
└── main.py
```

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/earnings-vol-skew-arb
cd earnings-vol-skew-arb
pip install -r requirements.txt
cp .env.example .env

# First: Build the historical IV crush database
python scripts/build_iv_database.py --tickers SPY QQQ AAPL MSFT NVDA

# Then paper trade
python scripts/paper_trade.py --mode paper
```

## ⚠️ Critical Rules Before Live Trading

1. Paper trade **minimum 2 earnings seasons** (6+ months) before going live
2. Start with `MAX_POSITION_CONTRACTS = 1` per ticker
3. **Never trade unconfirmed earnings dates** — AMC vs BMO matters enormously
4. **Always verify liquidity** — some earnings chains are extremely illiquid
5. **Sector limit**: Never have >2 earnings trades in same sector simultaneously
6. **Gap protection**: Wings must be placed beyond 2× the expected move
7. **IV rank minimum 60** — below this, options aren't rich enough to sell

---
*Built with institutional discipline. Earnings options are not a coin flip — they require systematic edge quantification and strict risk management.*
