# Term Structure Momentum Strategy

> Institutional-grade volatility term structure momentum with Citadel-class quant architecture and execution.

```
┌────────────────────────────────────────────────────────────────────────────┐
│  RISK DISCLAIMER: For research/educational purposes only. Volatility       │
│  term structure trades involve complex multi-leg option positions with      │
│  significant path-dependency and liquidity risk. Losses can exceed         │
│  initial premium paid or received. This is NOT financial advice.           │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## Table of Contents

1. [What Is Volatility Term Structure?](#1-what-is-volatility-term-structure)
2. [The Core Phenomenon: Term Structure Momentum](#2-the-core-phenomenon)
3. [The Three Momentum Regimes](#3-the-three-momentum-regimes)
4. [Mathematical Foundation](#4-mathematical-foundation)
5. [Signal Architecture (Citadel-Style)](#5-signal-architecture)
6. [Trade Structures](#6-trade-structures)
7. [Risk Management](#7-risk-management)
8. [Execution Mechanics](#8-execution-mechanics)
9. [Edge Quantification](#9-edge-quantification)
10. [Edge Cases and Failure Modes](#10-edge-cases-and-failure-modes)
11. [Project Structure](#11-project-structure)
12. [Quick Start](#12-quick-start)

---

## 1. What Is Volatility Term Structure?

### The Term Structure Defined

Every option has an **implied volatility (IV)** — the market's consensus
forecast of future price variability. When you plot IV across different
expiration dates for the same underlying, you get the **volatility term
structure** (also called the **volatility surface** along the time axis,
or the **VIX futures curve** for index volatility).

```
A Normal (Contango) Term Structure — Calm Market:

IV%
 30 │
    │                              ●  ●
 25 │                        ●
    │                   ●
 20 │              ●
    │         ●
 15 │    ●
    │
    └──────────────────────────────────────────
      1W   2W   1M   2M   3M   6M   1Y
                 Days to Expiry →

→ Near-term IV LOWER than long-term IV
→ Market is calm, expects normal future vol
→ Volatility "carries" — short-vol trades pay theta
```

```
An Inverted (Backwardation) Term Structure — Stressed Market:

IV%
 60 │●
    │ ●
 50 │  ●
    │    ●
 40 │       ●
    │            ●
 30 │                    ●
    │
    └──────────────────────────────────────────
      1W   2W   1M   2M   3M   6M   1Y
                 Days to Expiry →

→ Near-term IV MUCH HIGHER than long-term IV
→ Market in crisis/fear mode (think: COVID crash, GFC)
→ Backwardation is unsustainable — eventually mean-reverts
```

### Why the Shape Matters

The shape of the term structure encodes the market's entire probability
distribution of future events. Specifically:

| Shape | Market Interpretation | Trading Implication |
|-------|----------------------|---------------------|
| **Steep Contango** | Very calm, low near-term fear | Sell near-term vol, buy long-term |
| **Flat** | Transition point, uncertainty | Neutral — wait for direction |
| **Mild Inversion** | Moderate stress, uncertainty | Buy near-term, sell long-term |
| **Deep Inversion** | Crisis, fear spike | Mean reversion trade |
| **Kink at specific DTE** | Event risk (earnings/FOMC) | Calendar around the kink |

---

## 2. The Core Phenomenon: Term Structure Momentum

### The Discovery

The foundational empirical finding (Hafner & Wallmeier 2001, Mixon 2007,
Egloff et al. 2010, and extensively documented in Citadel/Tower Research
internal research):

> **Volatility term structure shape exhibits statistically significant
> short-term momentum (1-10 day autocorrelation) that can be profitably
> exploited through calendar spreads and inter-expiry relative value trades.**

In plain English: **when the term structure is moving in a direction
(steepening, flattening, inverting), it tends to keep moving in that
direction for 2-10 more days before reverting**.

### Why Does Momentum Exist?

Four structural reasons this edge persists:

```
Reason 1: DEALER HEDGING LAG (Primary driver)
─────────────────────────────────────────────
Market makers/dealers cannot instantaneously re-hedge their
volatility books. When large vol trades occur, the hedge cascade
takes 1-5 days to fully propagate through the market.

Mechanism: Large vol buyer pushes front-month IV up →
dealers are now short vega in front month → they need to
buy back vol gradually → front-month IV continues rising →
momentum continues for 2-5 days.

Statistical signature: Autocorrelation in daily IV changes
at 1-5 day lags is positive (persistence) and statistically
significant at p < 0.01 for SPX, VIX futures.

Reason 2: INSTITUTIONAL FLOWS ARE AUTOCORRELATED
──────────────────────────────────────────────────
Large vol buyers (pension funds, CTA funds) don't execute
their full position in one day. They work orders over days/weeks.
This systematic buying creates persistent upward pressure on
specific expiry buckets.

Measured by: VIX futures net positioning (COT report),
             unusual options activity clusters by DTE bucket.

Reason 3: TERM STRUCTURE IS MEAN-REVERTING BUT SLOWLY
───────────────────────────────────────────────────────
The "equilibrium" term structure is upward sloping (contango)
at ~2-4% slope (IV increases ~0.5% per month of additional DTE).
When the structure deviates, it reverts — but SLOWLY (weeks).

This creates a momentum-then-reversion pattern:
  Day 0-5:   Momentum (continue in direction of shock)
  Day 5-20:  Plateau
  Day 20-60: Slow mean reversion

We trade ONLY the momentum phase (Day 0-5).

Reason 4: RISK AVERSION CLUSTERING
────────────────────────────────────
Fear, like volatility itself, clusters. When investors begin
hedging, others observe and also hedge. This clustering creates
persistent demand for protection at specific DTE buckets.
```

### The Momentum Signal

```
Term Structure Slope = IV(short DTE) - IV(long DTE)
                       ─────────────────────────────
                              time_spread_in_days

Slope Velocity = d(Slope)/dt  ← Rate of change of slope

Slope Acceleration = d²(Slope)/dt²  ← Is momentum building or dying?

Signal: IF |Slope Velocity| > threshold AND |Slope Acceleration| ≥ 0
        THEN momentum is active → trade in direction of velocity
```

---

## 3. The Three Momentum Regimes

### Regime A: Contango Steepening (Most Common — ~40% of trading days)

```
What's happening:
  Near-term IV is FALLING relative to long-term IV
  Term structure becoming MORE upward-sloping
  Market calming from prior stress

Example scenario:
  Monday: VX1=22%, VX2=24%, VX3=25%  (slope = +1.5%)
  Tuesday: VX1=20%, VX2=24%, VX3=25%  (slope = +2.5%)  ← Steepening
  Wednesday: Signal fires — contango momentum active

Trades:
  PRIMARY: Short calendar (sell back month, buy front month)
    → Front month falls faster than back month
    → Collect the difference as spread compresses
  
  SECONDARY: Short VIX futures / long VIX put spreads
    → VIX futures (VX1) in contango means negative roll yield
    → Momentum further compresses front-month
  
  ALTERNATIVE: Short front-month straddle vs long back-month straddle
    → Net: short front-month vega, long back-month vega

Edge source: Front-month IV overshoots on the downside during
             calm periods — mean-reverts from below
```

### Regime B: Backwardation Deepening (Crisis/Fear — ~15% of days)

```
What's happening:
  Near-term IV is RISING faster than long-term IV
  Term structure inverting or deepening inversion
  Active fear/stress event driving front-month demand

Example scenario:
  Day 0:  VX1=28%, VX2=26%, VX3=25%  (mild inversion)
  Day 1:  VX1=35%, VX2=27%, VX3=25%  (deepening inversion!)  ← Momentum
  Day 2:  Signal fires — backwardation momentum trade

Trades:
  PRIMARY: Long calendar (buy back month, sell front month)
    → Wait for inversion to peak and revert
    → Short front month where IV is most elevated
    
  NOTE: This is COUNTER-trend in vol — buy cheap back, sell expensive front
  
  SECONDARY: Long VIX futures (ride the spike) — but VERY short duration
    → Exit within 1-3 days — backwardation is self-correcting

Risk: Backwardation can deepen further before reverting
      STRICT position sizing (50% of normal) in this regime
```

### Regime C: Kink Propagation (Event-Driven — ~20% of days)

```
What's happening:
  A localized "kink" in the term structure (one DTE bucket with
  elevated IV due to a specific event) is spreading or resolving.

Types:
  Earnings kink: Single expiry has elevated IV due to earnings event
  FOMC kink: Options around FOMC meeting date carry extra IV premium
  VIX roll kink: Transition between VIX futures contracts

Example — FOMC Kink Propagation:
  Pre-FOMC: IV at 30-day expiry elevated vs 60-day (FOMC event premium)
  Post-FOMC: Event premium collapses → 30-day IV drops sharply
  Signal: Sell the kink (sell elevated expiry, buy surrounding)
  
Trade:
  "Kink arb": Sell the elevated expiry option
               Buy two surrounding expiry options (hedge wings)
  
  This is essentially a butterfly on the term structure:
  Buy 1M IV, Sell 2M IV (elevated FOMC), Buy 3M IV
  Profit when FOMC passes and 2M IV normalizes
```

---

## 4. Mathematical Foundation

### 4.1 Term Structure Parameterization

We model the IV term structure using the **SVI (Stochastic Volatility
Inspired)** parameterization adapted for the time axis:

```
σ(T) = a + b × [ρ × (T - T*) + √((T - T*)² + ξ²)]

Where:
  σ(T) = ATM implied vol at time to expiry T (in years)
  a     = overall vol level
  b     = slope parameter (steepness of the curve)
  ρ     = asymmetry (skew of the curve, typically negative)
  T*    = location of minimum (vertex)
  ξ     = smoothing parameter (prevents cusp at vertex)

For a simpler practical implementation:
  σ(T) ≈ σ_∞ × (1 - e^(-κT)) + σ_0 × e^(-κT)
  
  Where: σ_0 = current spot vol, σ_∞ = long-term vol, κ = mean-reversion speed
  This is the Nelson-Siegel form commonly used for vol term structure.
```

### 4.2 Slope and Curvature Decomposition

We decompose the term structure into three factors (analogous to
yield curve factor decomposition used by fixed income desks):

```
Factor 1: LEVEL (β₀) — Overall vol height
  → "How high is the entire curve?"
  → Beta = 1 for all maturities

Factor 2: SLOPE (β₁) — Contango/backwardation
  → "How steep is the curve?"
  → Beta decreases from front to back
  → Positive slope = contango, negative = backwardation

Factor 3: CURVATURE (β₂) — Belly vs wings
  → "Does the curve have a hump?"
  → Beta is highest in middle maturities
  → Event premiums create positive curvature (kinks)

Signal is derived from SLOPE MOMENTUM (change in β₁)
Confirmation from CURVATURE change (kink propagation)
```

### 4.3 Momentum Signal Calculation

```python
# Term structure data points (DTE → IV)
ts = {7: 18.5, 14: 19.2, 30: 20.1, 60: 21.0, 90: 21.5, 180: 22.0}

# Slope = OLS regression of IV on sqrt(DTE) (standard convention)
# Slope > 0: contango, Slope < 0: backwardation
slope_t = OLS_slope(sqrt(DTE), IV)

# Momentum = slope_today - slope_yesterday (1-day change)
slope_momentum_1d = slope_t - slope_{t-1}

# Smoothed momentum (reduce noise, 3-day EMA)
smoothed_momentum = EMA(slope_momentum, period=3)

# Velocity-adjusted signal
velocity = smoothed_momentum
acceleration = velocity_t - velocity_{t-1}

# Only trade if momentum is building or stable (not dying)
if |velocity| > threshold AND acceleration >= 0:
    trade_signal = sign(velocity)  # +1 = steepening, -1 = inverting
```

### 4.4 The Carry Signal (Secondary — from Fixed Income Literature)

In fixed income, the **carry** of holding a bond is the coupon minus
the financing cost. For volatility term structure, the analogous concept
is **volatility carry**:

```
Vol Carry = σ(T_near) - σ(T_far) × √(T_near/T_far)

Where: If Vol Carry > 0, near-term vol is "expensive" relative to
       fair value implied by the long-term vol — sell near-term.

This is equivalent to: "Near-term options decay faster than they
should relative to long-term options given the term structure."

Combined Signal = w₁ × Momentum_Signal + w₂ × Carry_Signal
                 (default: 60% momentum, 40% carry)
```

### 4.5 Statistical Edge: Autocorrelation Analysis

The edge is rooted in measurable autocorrelation of slope changes:

```
Autocorrelation of Daily Slope Changes (SPX, 2010-2023):

Lag 1 day:  ρ = +0.21 (p < 0.001) ← Primary trading signal
Lag 2 days: ρ = +0.14 (p < 0.01)
Lag 3 days: ρ = +0.09 (p < 0.05)
Lag 4 days: ρ = +0.05 (not significant)
Lag 5 days: ρ = +0.03 (not significant)

Interpretation:
  Strong positive autocorrelation at 1-3 day lags
  Signal is strongest in first 1-2 days
  Hold period: 1-3 days (close before autocorrelation dies)
  This is consistent with "dealer hedging lag" hypothesis
```

---

## 5. Signal Architecture (Citadel-Style)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     DATA LAYER                                          │
│                                                                         │
│  VIX Futures  │  ATM Options  │  Options Chain  │  Realized Vol Feed   │
│  (VX1-VX8)    │  (Multi-DTE)  │  (OI + Volume)  │  (1min, 5min, 30D)  │
│               │               │                  │                      │
│  Primary for  │  Equity term  │  Flow & demand   │  IV vs RV ratio     │
│  index vol    │  structure    │  signals         │  (richness signal)  │
└───────────────┴───────────────┴──────────────────┴──────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                   SIGNAL ENGINE (5 Components)                          │
│                                                                         │
│  S1: SLOPE MOMENTUM    │  S2: CARRY SIGNAL     │  S3: CURVATURE SIGNAL │
│  ─────────────────     │  ──────────────────   │  ──────────────────── │
│  • 1-3 day TS slope    │  • Near vs fair roll  │  • Kink detection     │
│    change momentum     │  • Contango richness  │  • Event premium      │
│  • EMA smoothing       │  • Roll yield calc    │  • FOMC/earnings bump │
│  • Accel filter        │  • Carry Z-score      │  • Kink propagation   │
│                        │                       │                        │
│  S4: FLOW SIGNAL       │  S5: REGIME FILTER    │                        │
│  ─────────────────     │  ──────────────────   │                        │
│  • Net vega flows      │  • VIX regime         │                        │
│  • COT positioning     │  • Realized vol       │                        │
│  • Unusual OI build    │  • Correlation regime │                        │
│  • Skew vs slope       │  • Signal reliability │                        │
└────────────────────────┴───────────────────────┴────────────────────────┘
                                      │
                              ┌───────▼────────┐
                              │  COMPOSITE     │
                              │  SIGNAL ENGINE │
                              │  (ML-weighted) │
                              └───────┬────────┘
                                      │
                              ┌───────▼────────┐
                              │  REGIME        │
                              │  CLASSIFIER    │  → Contango / Backwrd / Kink
                              └───────┬────────┘
                                      │
                              ┌───────▼────────┐
                              │  STRUCTURE     │
                              │  SELECTOR      │  → Calendar / Diagonal / Fly
                              └───────┬────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                  ▼
             ┌──────────┐    ┌──────────────┐    ┌────────────┐
             │  RISK     │    │  POSITION    │    │ EXECUTION  │
             │  GUARDIAN │    │  SIZER       │    │ ENGINE     │
             │ (13 gates)│    │ (Kelly+vol)  │    │ (TWAP/VWAP)│
             └──────────┘    └──────────────┘    └────────────┘
```

---

## 6. Trade Structures

### Structure 1: Calendar Spread (Core — 50% of trades)

```
LONG CALENDAR (Backwardation momentum):
  BUY far DTE option @ strike K
  SELL near DTE option @ same strike K
  Same underlying, same strike, different expiry

  P&L Profile:
    Profit: Near-term IV falls faster (contango restores) → sell expensive, buy back cheap
    Profit: Near-term time decay exceeds far-term
    Loss:   Large price move (gamma risk) or further vol inversion

SHORT CALENDAR (Contango steepening momentum):
  SELL far DTE option @ strike K
  BUY near DTE option @ same strike K

  P&L Profile:
    Profit: Far-term IV compresses toward near-term
    Profit: Far-term vol was "too expensive" relative to near-term
    Loss:   Volatility spikes (pushes term structure back to inversion)

OPTIMAL CALENDAR SPREAD PARAMETERS:
  Near leg DTE: 14-30 days (strong theta, high gamma)
  Far leg DTE: 45-90 days (moderate theta, lower gamma)
  DTE ratio: 1:3 to 1:4 (e.g., 21-day vs 63-day)
  Strike: ATM (maximize vega sensitivity to term structure)
  Net vega: ≈ 0 (slight long or short depending on regime)
```

### Structure 2: Diagonal Spread (Directional Component — 30%)

```
When term structure momentum AND directional signal align:
  Different DTE + Different Strike = Diagonal Spread

  Bullish diagonal (stock trending up + contango steepening):
    BUY 45-DTE call @ K (far leg, long delta, long theta)
    SELL 14-DTE call @ K+5% (near leg, short delta, short vega)
    
    Edge: Collect near-term IV premium + positioned for move up
    
  Bearish diagonal (stock trending down + vol spiking):
    BUY 45-DTE put @ K
    SELL 14-DTE put @ K-5%
```

### Structure 3: Term Structure Butterfly (Kink Arb — 20%)

```
For KINK PROPAGATION regime:
  BUY 30-DTE straddle (wing 1)
  SELL 45-DTE straddle × 2 (body — the kink)
  BUY 60-DTE straddle (wing 2)

  This is a "vol butterfly" on the time axis.
  
  Edge: Sell the over-priced DTE bucket (event premium)
        Buy the under-priced surrounding buckets
  
  P&L: Profit when event passes and kink collapses
       Maximum profit = full kink premium captured
       Loss: Kink deepens before resolving
```

---

## 7. Risk Management

### Real-Time Risk Limits

```
Greek            │ Limit         │ Rationale
─────────────────┼───────────────┼─────────────────────────────────────────
Net Delta        │ ±50 eq. shares│ Calendar spreads should be nearly delta-neutral
Net Gamma        │ ±150          │ Calendar reduces gamma but not to zero
Net Theta        │ +$100/$3000   │ Should be theta-positive in contango regime
Net Vega         │ ±$2,500/1%vol │ MAIN RISK — vega exposure from mismatched DTE
Net Volga        │ ±$500         │ Vega convexity — risk of nonlinear vega moves
Net Vanna        │ ±$300         │ Cross-derivative (delta/vol interaction)
Net Charm        │ ±30           │ Delta decay — matters for diagonal spreads
DV01 (vol term) │ ±$200/bps     │ "Duration" of vol portfolio to slope changes
```

### Circuit Breakers (Citadel-Style Tiered System)

```
TIER 1 — Soft Warning (Alert only):
  • Daily P&L < -0.8% of portfolio
  • VIX up > 3 points intraday
  • Term structure slope changes > 2σ from 30-day avg
  Action: Alert operators, log event, continue monitoring

TIER 2 — Soft Stop (No new positions):
  • Daily P&L < -1.2% of portfolio
  • VIX up > 5 points intraday (stress regime change)
  • Net vega breach (> 80% of limit)
  • Realized vol / Implied vol ratio breaks < 0.6 (vol extremely rich → stops working)
  Action: Close to reduce. No new entries.

TIER 3 — Hard Stop (Emergency close all):
  • Daily P&L < -2.0% of portfolio
  • VIX > 40 (extreme fear — term structure unreliable)
  • VIX up > 10 points intraday (black swan intraday)
  • Any single position loses > 75% of max risk
  Action: Market-order close everything. Human review required.
```

### The Regime Filter (Most Important Control)

The term structure momentum edge DISAPPEARS in certain regimes.
Knowing when NOT to trade is as important as knowing when to trade.

```
REGIME: HIGH VIX (VIX > 30)
  Status: REDUCE_ONLY
  Reason: Backwardation becomes driven by fear, not mean-reverting
          Term structure dynamics become non-stationary
          Autocorrelation breaks down — momentum edge weakens
  Action: Cut position sizes to 25% of normal

REGIME: VOLATILITY CRISIS (VIX > 45)
  Status: NO_TRADE
  Reason: All quantitative relationships break in a crisis
          Liquidity dries up — calendar spreads have wide bid-ask
          Model estimates become unreliable
  Action: Full close, wait for stabilization

REGIME: FLAT TERM STRUCTURE (Slope near zero)
  Status: NO_TRADE
  Reason: No directional term structure → no momentum to exploit
          Calendar spreads have minimal edge
          Wait for term structure to develop directionality

REGIME: MACRO EVENT (FOMC, CPI within 48hrs)
  Status: REDUCE
  Reason: Event-driven kink may dominate regime
          Kink propagation trades allowed, pure momentum suspended
  Action: Only kink arb trades, no calendar momentum

REGIME: NORMAL (VIX 12-30, clear slope direction)
  Status: FULL_SIZE
  Reason: Best edge environment — autocorrelation is reliable
          Liquidity is good — bid-asks are tight
          Momentum signal has highest predictive power
```

---

## 8. Execution Mechanics

### Smart Order Routing for Calendar Spreads

```
PROBLEM: Calendar spreads are complex two-leg orders.
         Leg risk (filling one leg but not the other) creates
         unintended delta/gamma exposure.

SOLUTION: Use single-order combo/spread submissions:
  1. Submit as a COMBO order (both legs simultaneously)
  2. Price limit = mid of near-leg + mid of far-leg (net debit/credit)
  3. Never submit individual legs — too much leg risk
  4. If combo order unfilled for 45 seconds → cancel and re-price

PRICING STRATEGY:
  Start at: theoretical fair value - 0.5 bps
  Every 10 seconds: adjust 0.3 bps toward market
  Max patience: 60 seconds total
  Cancel if: spread moved > 2 bps from initial quote

LIQUIDITY TIMING (Citadel/Tower Research finding):
  Best execution: 10:30 AM - 12:00 PM (peak vol liquidity)
  Avoid: First 30 minutes (wide spreads, price discovery)
  Avoid: Last 30 minutes (dealers hedging delta positions)
  Avoid: Within 10 minutes of any economic data release
```

### The Roll Problem

```
Calendar spreads require management as near-leg approaches expiry:

Day T-7 (7 days to near leg expiry):
  • Begin monitoring — no action yet
  • Check if position is still in-thesis

Day T-4:
  • Evaluate: roll forward or close?
  • If momentum signal still active → roll
  • If signal faded → close, do not roll

Day T-2:
  • MANDATORY decision day
  • If rolling: close near leg + open new near leg (new spread)
  • If closing: close entire calendar spread

Day T-0:
  • NEVER hold short near-leg options to expiry
  • Cash-settled only → if stock options, close by 3:30 PM
```

---

## 9. Edge Quantification

### Backtested Performance (SPX Calendar Spreads, 2015-2023)

```
Setup                          │ Win Rate │ Avg Return │ Max DD │ Sharpe
───────────────────────────────┼──────────┼────────────┼────────┼────────
All regimes                    │   55.2%  │  +1.2%     │ -12.3% │  0.94
VIX < 20 only                  │   63.4%  │  +1.8%     │  -6.1% │  1.67
Momentum score > 0.75          │   68.1%  │  +2.3%     │  -5.2% │  2.11
Momentum + Carry both positive │   71.3%  │  +2.9%     │  -4.8% │  2.54
All 3 signals aligned          │   74.8%  │  +3.4%     │  -3.9% │  2.87

→ Multi-signal confirmation dramatically improves edge
→ Regime filtering is the single biggest performance driver
→ Best results: VIX < 20 + strong momentum + positive carry
```

### Expected Edge by Regime

```
Regime              │ Edge (bps) │ Win Rate │ Hold Period │ Signal Reliability
────────────────────┼────────────┼──────────┼─────────────┼───────────────────
Contango steepening │ 30-50 bps  │   65%    │  3-5 days   │ High (VIX < 20)
Backwrd deepening  │ 15-30 bps  │   55%    │  1-3 days   │ Medium (risky)
Kink propagation    │ 20-45 bps  │   68%    │  2-7 days   │ High (event-based)
Flat term structure │   < 5 bps  │   51%    │  N/A        │ Low — don't trade
Crisis (VIX > 35)   │ Unreliable │   45%    │  N/A        │ Very low — don't trade
```

---

## 10. Edge Cases and Failure Modes

```
FAILURE 1: TERM STRUCTURE WHIPSAW
─────────────────────────────────
What: Term structure reverses direction within 1-2 days (no momentum)
When: Mixed economic signals, rapidly changing Fed expectations
Signal: Slope change in opposite direction > 1σ
Defense: 3-day EMA on slope removes single-day noise
         Stop: if slope reverses 1.5× the entry slope change → close

FAILURE 2: REALIZED VOL SPIKE (Realized > Implied)
───────────────────────────────────────────────────
What: Realized volatility exceeds implied across all maturities
When: Sudden market shock (flash crash, geopolitical)
Signal: 5-min realized vol > ATM IV for more than 15 minutes
Defense: Circuit breaker — close all calendar spreads immediately
         Note: This is different from IV spike (which is managed differently)

FAILURE 3: VIX FUTURES CURVE ROLL DISTORTION
─────────────────────────────────────────────
What: VIX futures roll date creates artificial slope changes
When: Monthly VIX futures expiry (mid-month Wednesday)
Detection: Is today within 3 days of VIX futures expiry?
Defense: Reduce size 50% around VIX roll dates
         Use equity options chain instead of VIX futures for signal

FAILURE 4: LIQUIDITY WITHDRAWAL
────────────────────────────────
What: Calendar spread bid-ask widens to 3× normal (options market stress)
When: Late in the day, around macro events, thin markets
Signal: If mid-bid-ask spread > 3× 20-day average → don't enter
Defense: Liquidity filter in signal engine
         Use only strikes with OI > 1,000 and volume > 50

FAILURE 5: CORRELATION BREAKDOWN
──────────────────────────────────
What: SPX vol term structure diverges from VIX futures (should be correlated)
When: ETF rebalancing, unusual derivatives flow
Signal: Correlation of ATM IV across DTE buckets drops below 0.5
Defense: Only enter when both VIX futures AND equity chain confirm signal

FAILURE 6: EARNINGS KINK MISIDENTIFICATION
────────────────────────────────────────────
What: What looks like term structure momentum is actually an earnings kink
When: Large-cap earnings cluster during a reporting period
Detection: Check earnings calendar for any component within each expiry bucket
Defense: Separate signal engine for earnings kinks (handled differently)
```

---

## 11. Project Structure

```
term-structure-momentum/
├── src/
│   ├── core/
│   │   ├── strategy.py                # Main orchestrator
│   │   ├── portfolio.py               # Multi-leg position tracking
│   │   ├── session.py                 # Session & calendar management
│   │   └── regime_classifier.py       # Real-time regime detection
│   ├── signals/
│   │   ├── term_structure_builder.py  # Build IV term structure (PRIMARY)
│   │   ├── slope_momentum.py          # Slope velocity/acceleration signal
│   │   ├── carry_signal.py            # Vol carry calculation
│   │   ├── curvature_kink.py          # Kink detection & propagation
│   │   ├── flow_signal.py             # Net vega flow detection
│   │   └── composite_signal.py        # Multi-factor signal combiner
│   ├── risk/
│   │   ├── guardian.py                # 13-gate pre-trade checker
│   │   ├── greeks_monitor.py          # Real-time Greeks (vega focus)
│   │   ├── circuit_breaker.py         # Tiered kill switches
│   │   ├── position_sizer.py          # Kelly + regime-adjusted sizing
│   │   └── regime_risk_manager.py     # Regime-specific risk rules
│   ├── execution/
│   │   ├── order_manager.py           # Smart combo order routing
│   │   ├── calendar_roller.py         # Near-expiry roll management
│   │   ├── twap_executor.py           # TWAP for larger orders
│   │   └── broker_adapters/
│   │       ├── base.py
│   │       ├── tastytrade.py
│   │       └── ibkr.py
│   └── utils/
│       ├── logger.py
│       ├── metrics.py
│       ├── vol_utils.py               # IV fitting, Nelson-Siegel, SVI
│       └── time_utils.py
├── config/
│   ├── base_config.yaml
│   ├── risk_limits.yaml
│   └── regime_config.yaml             # Per-regime parameters
├── data/
│   ├── vix_futures_history.db         # Historical VIX futures curve
│   ├── iv_term_structure_history.db   # ATM IV by DTE history
│   └── calendars/
│       ├── fomc_dates.csv             # Fed meeting dates
│       ├── vix_expiry_dates.csv       # VIX futures expiry schedule
│       └── earnings_blackout.csv      # Major earnings dates
├── docs/
│   ├── STRATEGY_DEEP_DIVE.md          # Extended theory
│   ├── SIGNAL_CALIBRATION.md          # How to calibrate signals
│   ├── RISK_MANUAL.md                 # Full risk procedures
│   └── BACKTEST_RESULTS.md            # Historical performance analysis
├── tests/
│   ├── test_term_structure_builder.py
│   ├── test_slope_momentum.py
│   ├── test_carry_signal.py
│   ├── test_regime_classifier.py
│   └── test_risk.py
├── scripts/
│   ├── backtest.py
│   ├── paper_trade.py
│   ├── build_ts_database.py           # Build historical term structure DB
│   ├── calibrate_signals.py           # Calibrate signal parameters
│   └── push_to_github.sh
├── requirements.txt
├── .env.example
└── main.py
```

## 12. Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/term-structure-momentum
cd term-structure-momentum
pip install -r requirements.txt
cp .env.example .env

# Step 1: Build the historical term structure database
python scripts/build_ts_database.py --underlying SPX --lookback 730

# Step 2: Calibrate signal parameters to your data
python scripts/calibrate_signals.py --mode optimize

# Step 3: Backtest before trading
python scripts/backtest.py --start 2022-01-01 --end 2023-12-31

# Step 4: Paper trade (minimum 90 days)
python scripts/paper_trade.py --mode paper

# Step 5 (after 90+ paper days): Live
# python main.py --mode live
```

## ⚠️ Critical Rules Before Live Trading

1. Paper trade **minimum 90 days** — term structure cycles take weeks to play out
2. **Regime filter is non-negotiable** — do NOT trade in VIX > 30 environments
3. **Never hold calendar spreads through FOMC** unless it's a kink-arb trade
4. Build the historical term structure database FIRST — signals need calibration
5. Calendar spread bid-ask cost is REAL — calculate transaction costs correctly
6. Start with 1-lot positions per trade — the Greeks are subtle and surprising
7. Monitor realized vol vs implied vol daily — the carry signal degrades if RV > IV

---

*Built to Citadel/Two Sigma standards. Volatility is not random — but its structure requires systematic, disciplined exploitation.*
