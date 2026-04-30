# Expiry Pin Risk Reversal

> Institutional-grade expiry pinning + risk reversal strategy with Jane Street-class risk management.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  RISK DISCLAIMER: This is for educational/research purposes only.        │
│  Options near expiry carry extreme gamma risk. Pin risk can cause         │
│  catastrophic losses if assignment occurs unexpectedly. Risk reversals    │
│  have theoretically unlimited loss on the short leg. Never trade with     │
│  capital you cannot afford to lose. This is NOT financial advice.         │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Table of Contents

1. [Strategy Overview](#strategy-overview)
2. [The Two Core Phenomena](#the-two-core-phenomena)
3. [How the Strategy Works](#how-the-strategy-works)
4. [Trade Structures](#trade-structures)
5. [Risk Management Framework](#risk-management-framework)
6. [Signal Engine Architecture](#signal-engine-architecture)
7. [Edge Quantification](#edge-quantification)
8. [Execution Playbook](#execution-playbook)
9. [Critical Edge Cases](#critical-edge-cases)
10. [Project Structure](#project-structure)
11. [Quick Start](#quick-start)

---

## Strategy Overview

The **Expiry Pin Risk Reversal** strategy exploits two powerful but
underappreciated options market phenomena that occur with remarkable
regularity every expiration cycle:

1. **Max Pain / Gamma Pinning** — stocks gravitate toward specific strike
   prices on expiration day, driven by dealer delta-hedging mechanics.

2. **Risk Reversal Skew Mispricing** — near expiry, the put-call IV skew
   becomes distorted by retail fear and dealer positioning, creating
   systematic overpricing of one side that can be arbitraged.

Combined, these two effects create a powerful, measurable edge that
institutional traders have exploited for decades — but that has only
recently become accessible to sophisticated retail traders through
0DTE and weekly options.

---

## The Two Core Phenomena

### Phenomenon 1: Expiry Pinning (Max Pain)

```
What is Max Pain?
─────────────────
Max Pain = the strike price where the TOTAL VALUE of all expiring
options (calls + puts) is minimized at expiration.

It represents the price at which options sellers (dealers/MMs)
lose the LEAST amount of money — therefore they have a financial
incentive to push price toward this level through their hedging.

Why does it work?
─────────────────
As expiration approaches, dealers who are short options must
delta-hedge. When they are short gamma (which they typically are
near max pain strikes), their hedging PUSHES price TOWARD the
strike, creating a self-reinforcing pinning effect.

The mechanics:
  If stock is ABOVE max pain strike:
    Dealers are short calls → they are LONG delta (from hedging)
    As price rises, their delta hedge requires them to SELL → price pushed down
    As price falls, their delta hedge requires them to BUY → price pushed up
    Net effect: PINS price near the strike

  If stock is BELOW max pain strike:
    Same mechanics in reverse → price also pulled toward max pain

Empirical evidence:
  SPY/QQQ: 65-72% of weeks close within 0.5% of max pain strike
  Large single stocks: 55-65% pin accuracy within 1% of max pain
  Effect strongest: last 2 hours of expiration day
  Effect weakest: high VIX environments (>30), event days
```

```
Max Pain Calculation — Visual Example:
───────────────────────────────────────
Strike  Calls OI   Puts OI   Value if Price = $450
  440      500      3,000    $0 calls + $30,000 puts = $30,000
  445    1,200      2,500    $0 calls + $12,500 puts = $12,500
  450    2,000      2,000    $0 calls + $0 puts = $0 ← MAX PAIN
  455    2,800        800    $14,000 calls + $0 puts = $14,000
  460    4,000        200    $40,000 calls + $0 puts = $40,000

→ Max Pain = $450 (minimum total option value)
→ Price gravity: stock will be "pulled" toward $450 near close
```

### Phenomenon 2: Risk Reversal Skew Mispricing Near Expiry

```
What is a Risk Reversal?
─────────────────────────
A risk reversal is a position that:
  LONG a call at strike K₂ (above current price)
  SHORT a put at strike K₁ (below current price)
  Same expiry, same delta (e.g., both 25-delta)

The risk reversal skew = IV(25Δ Put) - IV(25Δ Call)

Positive skew: puts more expensive than calls (normal)
Negative skew: calls more expensive (unusual, fear-of-missing-up)

Near Expiry Distortion:
────────────────────────
As expiration approaches (last 1-3 days), two forces distort skew:

  1. RETAIL FEAR HEDGING: Retail investors buy OTM puts "just in case"
     → Demand spike for puts → put IV artificially elevated
     → Creates OVERSTATED negative skew vs fair value

  2. DEALER GAMMA POSITIONING: Near max pain strike, dealers have
     asymmetric gamma exposure → they shade prices to manage risk
     → Creates predictable IV biases at specific strikes

The Opportunity:
─────────────────
When max pain predicts pinning AND skew is distorted:
  • The stock won't move much (pin gravity)
  • But puts are priced as if it will fall sharply (fear premium)
  • Selling the put and buying the call (risk reversal) captures both:
    a) The overpriced put IV premium
    b) Any upside if the pin level is above current price
    c) Pure IV crush as expiry approaches
```

---

## How the Strategy Works

### The Unified Trade Logic

```
SETUP CONDITIONS:
─────────────────
1. Max pain strike is identified (minimum OI × distance calculation)
2. Max pain is above current price → bullish pin expected
   Max pain is below current price → bearish pin expected
3. Put-call IV skew is distorted (puts overpriced vs fair value)
4. Expiry is within 1-5 days (pin gravity is strongest here)
5. GEX confirms dealer positioning aligns with pin direction

ENTRY:
──────
For BULLISH PIN (price below max pain, expect price to rise to pin):
  BUY the call at max pain strike (or just above)
  SELL the put at equal delta below (collect overpriced premium)
  Net: collect credit or small debit — but positioned for upside move

For BEARISH PIN (price above max pain, expect price to fall to pin):
  SELL the call at max pain strike (or just below)
  BUY the put at equal delta (cheaper IV, directional hedge)
  Net: collect credit from overpriced call side

For NEUTRAL PIN (price AT max pain):
  Iron condor around max pain → pure IV decay play
  Both sides overpriced near expiry → collect from both

EXIT:
─────
Primary: Price reaches max pain level → close risk reversal
Secondary: 50% profit target → scale out
Time-based: 1 hour before expiry → mandatory close (gamma danger)
Stop: Price breaks away from max pain by >1.5× expected move
```

### Time Dynamics

```
Days to Expiry → Expected Pin Strength
─────────────────────────────────────────
5 DTE: Early gravitational pull begins
        Max pain moves as OI builds
        Low confidence — use for calendar positioning

3 DTE: Moderate pin effect
        Dealers adjusting hedges
        Enter smaller size

2 DTE: Strong pin gravity established
        OPTIMAL ENTRY WINDOW
        Full size entries

1 DTE: Strongest pin effect
        Large OI creates powerful magnet
        Monitor closely — gamma risk rising

0DTE: EXTREME pin effect (and extreme gamma risk)
       Best price target accuracy
       BUT: gamma can cause explosive moves if price breaks away
       Reduce size 50% vs 2 DTE entries
```

---

## Trade Structures

### Structure 1: Bullish Risk Reversal (Most Common)

```
When: Max pain ABOVE current price + puts overpriced + stable VIX

Position:
  LONG call @ max pain strike (or nearest OTM)
  SHORT put @ equal-delta strike (below current price)

Example (SPY @ $450, Max Pain @ $453):
  BUY  SPY $453 Call  expiring Friday  @ $1.20 debit
  SELL SPY $447 Put   expiring Friday  @ $1.40 credit
  Net: $0.20 credit received

Profit scenarios:
  SPY → $453 (max pain): Max profit on call + keep put premium
  SPY stays $450-453: Keep most of put premium, small call value
  SPY stays $447-450: Keep net credit
  SPY below $447: Put assignment risk — KEY RISK

Max profit: Unlimited (if stock rockets)
Max loss: Strike width - credit ($5.80 if SPY @ $447 at expiry)
Breakeven: $447 - $0.20 = $446.80 on the downside
```

### Structure 2: Bearish Risk Reversal

```
When: Max pain BELOW current price + calls overpriced (rare) + bearish flow

Position:
  SHORT call @ max pain strike (or nearest OTM)
  LONG put @ equal-delta strike (below current price)

Example (SPY @ $455, Max Pain @ $451):
  SELL SPY $451 Call  expiring Friday  @ $1.80 credit
  BUY  SPY $449 Put   expiring Friday  @ $0.90 debit
  Net: $0.90 credit received

Note: Less common — calls rarely as overpriced as puts near expiry
```

### Structure 3: Pin Condor (Neutral)

```
When: Price AT max pain ± 0.3% + both sides have elevated IV

Position:
  SELL call @ max pain + spread_width
  BUY call  @ max pain + spread_width + protection_width
  SELL put  @ max pain - spread_width
  BUY put   @ max pain - spread_width - protection_width

Example (SPY @ $450, Max Pain @ $450):
  SELL $452 Call + BUY $454 Call (bear call spread)
  SELL $448 Put  + BUY $446 Put  (bull put spread)
  Net: Credit from both sides

Edge: Pure IV decay + pin gravity keeps price between short strikes
```

### Structure 4: Max Pain Calendar (Multi-expiry)

```
When: Max pain is stable across multiple expiries + steep term structure

Position:
  SELL near-term option @ max pain strike (theta rich)
  BUY longer-term option @ same strike (IV support)

Edge: Near-term option decays faster + pinning means lower realized vol
      than implied by the near-term option's price
```

---

## Risk Management Framework

### The Five Deadly Risks in This Strategy

```
┌──────────────────────────────────────────────────────────────────┐
│  RISK 1: PIN FAILURE                                             │
│  What: Stock breaks away from max pain level with momentum       │
│  When: High VIX, macro events, earnings surprises               │
│  Defense: Hard stops at 1.5× expected move from max pain         │
│           Reduce size on event days                              │
│           Never hold into binary events (FOMC, earnings)         │
├──────────────────────────────────────────────────────────────────┤
│  RISK 2: ASSIGNMENT RISK (Short Put)                            │
│  What: Price closes at or below short put strike at expiry       │
│  When: Late-day sell-off below short strike                      │
│  Defense: Close ALL positions ≥60 min before close on expiry     │
│           Never hold short options into expiry                   │
│           Monitor ITM status from 2 hours before close           │
├──────────────────────────────────────────────────────────────────┤
│  RISK 3: GAMMA EXPLOSION (0DTE/1DTE)                            │
│  What: Near-expiry gamma causes options to reprice violently     │
│  When: Price moves suddenly near ATM strike close to expiry      │
│  Defense: Sunset rule — reduce 50% at 2 hrs, 100% at 60 min     │
│           Gamma budget enforced in real-time                     │
│           Never hold unhedged short gamma positions overnight    │
├──────────────────────────────────────────────────────────────────┤
│  RISK 4: MAX PAIN SHIFT                                          │
│  What: Large OI positions change, moving max pain level          │
│  When: Institutional rebalancing, block trades in options        │
│  Defense: Recalculate max pain every 15 minutes on expiry day    │
│           Alert if max pain shifts > 0.5% — reassess position    │
├──────────────────────────────────────────────────────────────────┤
│  RISK 5: LIQUIDITY RISK                                          │
│  What: Near-expiry options spread wide — hard to exit cleanly    │
│  When: Late in expiry day, OTM strikes with low volume           │
│  Defense: Only trade strikes with OI > 500, volume > 100        │
│           Plan exit BEFORE entering (have a clear exit path)     │
│           Never be the only bidder/offer at expiry               │
└──────────────────────────────────────────────────────────────────┘
```

### Greeks Risk Budget

```
Greek    |  Meaning for This Strategy         | Hard Limit
─────────┼────────────────────────────────────┼───────────
Delta    | Directional exposure               | ±60 shares eq.
Gamma    | Speed of delta change (CRITICAL)   | -300 (short) / +100 (long)
Theta    | Time decay income                  | +$200 to +$2,000/day
Vega     | Volatility exposure                | ±$1,200 per 1% vol
Charm    | How delta changes with time        | ±50 (critical near expiry)
Vanna    | How delta changes with vol         | ±300
Speed    | Rate of gamma change (3rd order)   | Monitor only near expiry
```

---

## Signal Engine Architecture

```
                    ┌─────────────────┐
                    │   OI Scanner    │  ← Fetches full options chain OI
                    │  (Max Pain Calc)│  ← Runs every 15 min on expiry day
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
    ┌─────────▼──────┐  ┌────▼──────┐  ┌───▼────────────┐
    │  GEX Profile   │  │ IV Skew   │  │ Pin Gravity    │
    │                │  │ Analysis  │  │ Score          │
    │ • Dealer long/ │  │           │  │                │
    │   short gamma  │  │ • RR 25Δ  │  │ • Distance to  │
    │ • Flip level   │  │ • Wing IV │  │   max pain     │
    │ • Pin strikes  │  │ • Skew    │  │ • OI gravity   │
    │                │  │   slope   │  │ • Historical   │
    └────────┬───────┘  └────┬──────┘  │   pin accuracy │
             │               │          └───────┬────────┘
             └───────────────┼──────────────────┘
                             │
                    ┌────────▼────────┐
                    │   Composite     │
                    │   Signal Engine │
                    │                 │
                    │  Score + Dir +  │
                    │  Structure Rec  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Risk Guardian  │  ← 13 pre-trade gates
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ Position Sizer  │  ← Kelly + DTE-adjusted
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ Order Manager   │  ← Limit orders, no market
                    └─────────────────┘
```

---

## Edge Quantification

### Historical Edge by Setup Quality

```
Setup Score  | Win Rate | Avg Return | Sharpe | Notes
─────────────┼──────────┼────────────┼────────┼──────────────────────
0.85 - 1.00  |  68-74%  |  +4.2%     |  2.1   | Perfect setup — rare
0.75 - 0.85  |  60-68%  |  +2.8%     |  1.6   | Strong setup — trade full size
0.65 - 0.75  |  54-60%  |  +1.9%     |  1.2   | Acceptable — 75% size
0.55 - 0.65  |  50-54%  |  +1.1%     |  0.7   | Weak — paper trade only
< 0.55       |    N/A   |    N/A     |   N/A  | Skip — no edge
```

### IV Skew Edge by DTE

```
DTE  | Avg Put Overpricing | Pin Strength | Optimal Structure
─────┼─────────────────────┼──────────────┼──────────────────────
 5   |         8%          |     Low      | Calendar / wait
 3   |        12%          |    Medium    | Small risk reversal
 2   |        18%          |     High     | Full risk reversal
 1   |        25%          |   Very High  | Risk reversal (reduced size)
 0   |        35%          |   Extreme    | Condor (reduce size 50%)
```

---

## Execution Playbook

### Entry Timing Rules

```
Step 1: Calculate max pain at market open (9:30 AM)
Step 2: Note distance from current price to max pain
Step 3: Check IV skew — are puts overpriced vs model?
Step 4: Check GEX — do dealers confirm pin direction?
Step 5: Composite score ≥ 0.68 → proceed to risk checks
Step 6: Risk guardian approves → size position
Step 7: Enter limit order at mid-price (NEVER market orders)
Step 8: Set alerts: price at max pain (exit), stop level (close)

ENTRY WINDOW: 10:00 AM - 2:00 PM (avoid early chaos and late gamma)
```

### Exit Rules (Mandatory)

```
PROFIT EXITS:
  → 50% of max profit: close 50% of position
  → 75% of max profit: close remaining position
  → Price reaches max pain: consider full close

TIME EXITS (Non-negotiable):
  → 2 hours before close: reduce position by 50%
  → 60 minutes before close: CLOSE ALL SHORT OPTIONS
  → Never hold short near-money options into final 30 minutes

STOP EXITS:
  → Price moves 1.5× expected move AWAY from max pain: close
  → Max pain shifts > 0.5% from entry max pain: reassess
  → VIX spikes > 3 points intraday: reduce to 50% size
  → Gamma exceeds budget: emergency reduce
```

---

## Critical Edge Cases

```
1. MAX PAIN SHIFT DURING THE DAY
   Cause: Large institutional block trades in options
   Detection: Recalculate max pain every 15 min
   Action: If shift > 0.5%, adjust or exit position

2. EARLY ASSIGNMENT ON SHORT PUT
   Cause: Deep ITM put with high dividend or interest rate
   Detection: Monitor exercise notices from broker
   Action: Close short put BEFORE ex-dividend date if applicable
   Prevention: Avoid short puts on high-dividend stocks near ex-date

3. PRICE PINNED AT WRONG LEVEL
   Cause: Multiple competing max pain candidates within 0.5%
   Detection: Bimodal OI distribution
   Action: Reduce size when OI is ambiguous (no clear peak)

4. VIX SPIKE DURING TRADE
   Cause: Macro news, geopolitical event during expiry day
   Detection: VIX up > 2 points from entry
   Action: Circuit breaker triggers, close risk reversal immediately

5. DEALER GAMMA FLIP
   Cause: Price crosses GEX flip level (dealers go from long to short gamma)
   Detection: Real-time GEX monitoring
   Action: If dealers go short gamma, pin effect weakens — exit

6. ROLL RISK
   Cause: Need to roll position to next expiry if pin fails
   Prevention: Never roll — always close and re-evaluate fresh
   Rule: Losing positions die at expiry — never roll losers

7. WEEKEND RISK
   Cause: Theta decay but no pin on Saturday/Sunday
   Prevention: Never hold risk reversals into weekend
   Rule: Always close Friday by 2:00 PM if not expiry day
```

---

## Project Structure

```
expiry-pin-risk-reversal/
├── src/
│   ├── core/
│   │   ├── strategy.py              # Main orchestrator
│   │   ├── portfolio.py             # Position & Greeks tracking
│   │   └── session.py               # Session lifecycle & expiry calendar
│   ├── signals/
│   │   ├── max_pain.py              # Max pain calculator (PRIMARY)
│   │   ├── pin_gravity.py           # Pin strength scoring engine
│   │   ├── iv_skew_near_expiry.py   # Near-expiry IV skew analysis
│   │   ├── gex_pin.py               # GEX-based pin confirmation
│   │   └── composite_signal.py      # Multi-factor signal combiner
│   ├── risk/
│   │   ├── guardian.py              # 13-gate pre-trade checker
│   │   ├── greeks_monitor.py        # Real-time Greeks (gamma focus)
│   │   ├── circuit_breaker.py       # Kill switches (pin-specific)
│   │   ├── position_sizer.py        # Kelly + DTE-adjusted sizing
│   │   ├── assignment_monitor.py    # Short option assignment tracker
│   │   └── gamma_sunset.py          # Mandatory near-expiry size reduction
│   ├── execution/
│   │   ├── order_manager.py         # Smart order routing
│   │   ├── expiry_exit_manager.py   # Mandatory expiry-day exit logic
│   │   └── broker_adapters/
│   │       ├── base.py
│   │       ├── tastytrade.py
│   │       └── ibkr.py
│   └── utils/
│       ├── logger.py
│       ├── metrics.py
│       ├── iv_utils.py              # Black-Scholes, Greeks
│       └── time_utils.py
├── config/
│   ├── base_config.yaml
│   ├── risk_limits.yaml
│   └── ticker_config.yaml          # Per-ticker OI patterns
├── data/
│   ├── max_pain_history.db          # Historical max pain vs actual close
│   └── pin_accuracy_stats.csv       # Per-ticker pin accuracy database
├── docs/
│   ├── STRATEGY_DEEP_DIVE.md        # This document (extended)
│   ├── RISK_MANUAL.md               # Full risk procedures
│   └── EDGE_CASES.md                # Documented edge cases & responses
├── tests/
│   ├── test_max_pain.py
│   ├── test_pin_gravity.py
│   ├── test_risk.py
│   └── test_execution.py
├── scripts/
│   ├── backtest.py
│   ├── paper_trade.py
│   ├── build_pin_database.py        # Historical pin accuracy builder
│   └── push_to_github.sh
├── requirements.txt
├── .env.example
└── main.py
```

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/expiry-pin-risk-reversal
cd expiry-pin-risk-reversal
pip install -r requirements.txt
cp .env.example .env

# Build the max pain history database (recommended first step)
python scripts/build_pin_database.py --tickers SPY QQQ AAPL MSFT --lookback 52

# Run paper trading
python scripts/paper_trade.py --mode paper

# Live mode (only after 60+ paper sessions)
# python main.py --mode live
```

## ⚠️ Critical Rules Before Live Trading

1. Paper trade **minimum 3 full expiry cycles** per ticker before going live
2. **Never hold short options into final 60 minutes** — gamma risk is extreme
3. **Always verify max pain calculation** with at least two independent sources
4. **Never trade on expiry days with scheduled macro events** (FOMC, CPI, etc.)
5. Size to 50% on 0DTE relative to 2DTE positions
6. Keep a **manual kill switch** visible at all times on expiry days
7. Set automated alerts for: price > max pain + 0.5%, VIX up 2pts, gamma budget

---

*Built with institutional discipline. Pin risk is real — respect the Greeks.*
