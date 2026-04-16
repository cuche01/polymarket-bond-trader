# Polymarket Bond Strategy Bot - Implementation Guide

## Table of Contents

1. [Overview](#overview)
2. [Strategy: The Bond Thesis](#strategy-the-bond-thesis)
3. [Architecture](#architecture)
4. [Module Breakdown](#module-breakdown)
5. [Entry Pipeline](#entry-pipeline)
6. [Exit Engine](#exit-engine)
7. [Risk Management](#risk-management)
8. [Money Management](#money-management)
9. [Expected Value Analysis](#expected-value-analysis)
10. [Win Rate & Risk-Reward](#win-rate--risk-reward)
11. [Notifications & Observability](#notifications--observability)
12. [Configuration Reference](#configuration-reference)
13. [Data Model](#data-model)
14. [Feature Flags](#feature-flags)
15. [Known Risks & Mitigations](#known-risks--mitigations)

---

## Overview

The Polymarket Bond Strategy Bot is an automated trading system that identifies and trades "bond-like" positions on Polymarket - prediction markets where the YES outcome is trading at $0.94-$0.99, indicating the market considers the event highly likely to occur. When these markets resolve YES (as expected), the position pays out $1.00 per share, capturing the remaining spread as profit.

**Core premise:** A market trading at $0.96 implies a 96% probability of YES resolution. If it resolves YES, you earn ~4.2% gross yield. The strategy systematically finds these opportunities, validates them through multiple layers, manages risk across the portfolio, and exits via resolution, stop-loss, or take-profit.

**Modes:** Paper trading (simulated, no real capital) and Live trading (real CLOB orders on Polygon).

**Feature flags:** All advanced features (14 total across P0-P3 priority tiers) are individually toggleable via `feature_flags` in config.yaml without code changes.

---

## Strategy: The Bond Thesis

### Why "Bond"?

Traditional bonds pay a fixed coupon and return par value at maturity. Polymarket YES tokens in high-probability markets behave similarly:

| Property | Traditional Bond | Polymarket Bond |
|---|---|---|
| Purchase price | Below par (e.g., $960 for $1000 face) | Below $1.00 (e.g., $0.96) |
| Maturity payout | Par value ($1000) | $1.00 per share |
| Yield | Coupon + discount to par | Spread: ($1.00 - entry) / entry |
| Default risk | Issuer default | Market resolves NO |
| Time horizon | Fixed maturity date | Market resolution date |

### Yield Mechanics

For an entry at price `P`:

```
Gross yield     = (1.00 - P) / P
Net yield       = Gross yield - (2 * fee_rate)
Annualized yield = Net yield * (365 / days_to_resolution)
```

Example at $0.96 entry, 3-day resolution, 0.2% fee rate:
```
Gross yield     = 0.04 / 0.96 = 4.17%
Net yield       = 4.17% - 0.40% = 3.77%
Annualized yield = 3.77% * (365/3) = 458.8%
```

The annualized yield is extremely high because the holding period is short. However, the absolute dollar gain per trade is small, which means the strategy's profitability depends on:
1. **High win rate** (>85% of trades resolve YES)
2. **Tight loss control** (stop-losses to limit downside on the ~15% that don't)
3. **Volume** (many concurrent positions to compound small edges)

### Why This Works

Polymarket bond opportunities exist because:
- **Liquidity premium:** Market makers hold inventory and demand a spread
- **Time value:** Even near-certain outcomes have a discount until resolution
- **Information asymmetry:** The bot scans thousands of markets faster than manual traders
- **Risk premium:** Participants demand compensation for capital lockup and resolution risk

---

## Architecture

```
main.py (BondBot orchestrator)
 |
 +-- src/scanner.py          -- Fetch & pre-filter from Gamma API
 +-- src/detector.py          -- 8-layer validation pipeline
 +-- src/risk_engine.py       -- 10-check portfolio-level gate + adaptive sizing
 +-- src/executor.py          -- Order placement & fill monitoring
 +-- src/exit_engine.py       -- Active exit management (9 strategies)
 +-- src/orderbook_monitor.py -- P0: Bid depth imbalance detection (live only)
 +-- src/blacklist_learner.py -- P2: Loss feature tracking & penalty scoring
 +-- src/monitor.py           -- Legacy position monitoring & alerts
 +-- src/portfolio_manager.py -- Exposure, drawdown, P&L queries
 +-- src/risk_buckets.py      -- Correlation clustering classifier
 +-- src/database.py          -- SQLite persistence layer + migrations
 +-- src/notifications.py     -- Discord webhook notifications
 +-- src/dashboard.py         -- Terminal dashboard display
 +-- src/utils.py             -- Shared utilities (rate limiter, scoring, config, feature flags)
```

### Event Loop (main.py)

The bot runs a single async event loop with four phases every cycle:

1. **Orderbook monitor** (every 20s, live only) - Fast-cycle bid depth imbalance detection.
2. **Exit cycle** (every 60s) - Evaluate all open positions for exit conditions. Runs BEFORE entries so capital is freed first.
3. **Scan cycle** (every 300s) - Fetch markets, filter candidates, validate, size, execute entries.
4. **Periodic tasks** - Hourly snapshots, daily reports, lifetime performance summaries.

---

## Module Breakdown

### Scanner (`src/scanner.py`)

Fetches all active markets from the Gamma API with pagination (500 per page) and applies fast pre-filters:

| Filter | Threshold | Purpose |
|---|---|---|
| Price range | $0.94 - $0.99 | Bond zone only (tightened from $0.90) |
| Max days to resolution | 14 | Limit time exposure |
| Min liquidity (CLOB) | $5,000 (time-adjusted) | Ensure exit-ability |
| Min 24h volume | $2,500 | Reject dead markets |
| Max 1-day volatility | 5% | Reject unstable markets |
| Excluded categories | Sports, E-Sports, etc. | Avoid gap-down risk markets |
| Volume trend (P1) | 70% of 7d daily avg | Reject declining-volume markets |
| Time-of-day liquidity (P3) | 1.5x off-peak, 2.0x weekend | Higher bar during thin hours |

**Volume Trend Filter (P1):** Rejects markets where 24h volume is less than 70% of the estimated 7-day daily average. This catches markets where activity is drying up — a leading indicator of liquidity withdrawal.

**Time-of-Day Liquidity (P3):** During weekends (Saturday/Sunday) the minimum liquidity threshold is multiplied by 2.0x, and during off-peak hours (outside 14:00-21:00 UTC) by 1.5x. This prevents entries when the orderbook is likely thin and exits would face higher slippage.

After filtering, candidates are scored using `calculate_bond_score()` (with optional blacklist and catalyst penalties) and ranked descending.

### Bond Score (`src/utils.py`)

```python
bond_score = (adjusted_yield / time_factor) * liquidity_weight * stability_weight * catalyst_penalty * blacklist_penalty
```

Where:
- `adjusted_yield = yield_pct * confidence_weight` (penalizes lower-confidence entries)
- `confidence_weight = entry_price ** 2` (a $0.98 entry gets 0.9604 weight vs 0.9025 for $0.95)
- `time_factor = days_to_resolution` (linear mode) or `1 / e^(-decay * days)` (exponential mode)
- `liquidity_weight = min(liquidity / 50000, 1.0)` (saturates at $50k)
- `stability_weight = 1.0 / 0.8 / 0.5` based on 1-day price change
- `catalyst_penalty` — from binary catalyst classifier (1.0 = no penalty, 0.6 = high-risk catalyst)
- `blacklist_penalty` — from blacklist learning loop (1.0 = no penalty, 0.4 = max penalty)

**Confidence-adjusted yield:** Prevents the naive ranking where a $0.90 market (10.5% yield) always outranks a $0.98 market (2.0% yield). The $0.90 market has significantly higher NO-resolution risk, so `entry_price^2` discounts its yield to reflect the true risk-adjusted return.

**Exponential Resolution Proximity (P3):** When enabled via feature flag, replaces the linear `1/days` time factor with `e^(-decay_rate * days)`. This exponentially favors markets resolving sooner — a 1-day market scores 3x+ higher than a 7-day market, reflecting the much lower tail risk of shorter holding periods. Decay rate is configurable (default 0.3).

### Detector (`src/detector.py`)

Eight sequential validation layers — a market must pass ALL layers:

**Layer 1: Category & Blacklist Exclusion**
- Checks config `excluded_categories` and `data/blacklist.json`
- Filters keyword patterns (esports terms, crypto minute-markets, social media)
- Filters explicit market IDs and slugs

**Layer 2: Price Behavior Analysis**
- Confirms YES price is in bond range ($0.94 - $0.99)
- Rejects if 1-day price change exceeds 5% (instability)
- Rejects if 1-week price change exceeds 15%
- Rejects if bid-ask spread exceeds configured max (4%)
- Rejects suspicious volume/liquidity ratios (>10x)

**Layer 2.5: Price Drop Cool-Down (P1)**
- Rejects markets where the price has dropped more than 3% recently
- Requires 80% price recovery before re-entry is allowed
- Prevents catching a falling knife — if a market just dropped from $0.98 to $0.94, something changed and the $0.94 price may not represent stable high-probability

**Layer 2.6: YES+NO Parity Validation (P2)**
- Fetches both YES and NO token prices
- Rejects if YES + NO sum falls outside the 0.96-1.04 range
- Markets where the two sides don't sum near $1.00 have a structural issue (stale pricing, thin books, or manipulation)

**Layer 3: Orderbook Health**
- Validates bid-side depth >= position_size * multiplier (3x)
- Checks CLOB spread doesn't exceed max
- Falls back to market-level liquidity in paper mode

**Layer 4: Resolution Source Validation**
- Rejects subjective resolution criteria ("popular opinion", "twitter poll")
- Rejects risky time-dependent markets ("within 24 hours", "next tweet")
- Requires end date in the future
- **Live-event gate:** Rejects sports markets resolving within 6 hours (gap-down protection)

**Layer 4.5: Binary Catalyst Classification (P1)**
- Classifies the market's resolution trigger as binary or continuous using regex pattern matching
- **Binary patterns** (12): court rulings, regulatory decisions, elections, announcements, social media milestones — these resolve sharply YES or NO with no middle ground
- **Continuous patterns** (6): price thresholds ("above $X"), cumulative stats, duration conditions — these can drift gradually
- Markets with binary catalyst score >= 0.85 are **rejected** (too much gap-down risk)
- Markets with score 0.50-0.85 receive a **penalty multiplier** (0.60) on their bond score
- Markets classified as continuous or scoring < 0.50 pass through unpenalized

**Layer 5: Blacklist Check**
- Final check against explicit market_ids, slugs, and keyword patterns

**Final: Net Yield Gate**
- Rejects if `net_yield = gross_yield - 0.002` is below `min_net_yield` (0.8%)

### Risk Engine (`src/risk_engine.py`)

Ten sequential checks that EVERY entry must pass. No trade opens without full approval:

| # | Check | Threshold | What It Prevents |
|---|---|---|---|
| 1 | Category blocked | UMA dispute flag | Contagion from disputed markets |
| 2 | Daily loss limit | 5% of portfolio | Tilt/revenge trading after bad day |
| 3 | Consecutive losses | 5 losses, 6h cooldown | Systematic edge degradation |
| 4 | Deployment limit | 80% max deployed | Always keep 20% cash reserve |
| 5 | Category exposure | 35% per category | Category concentration risk |
| 6 | Event group exposure | 25% per event group | Correlated same-event markets |
| 7 | Risk bucket exposure | 15-30% per bucket | Cross-category correlation clustering |
| 8 | Position size | 12% hard cap, 8% target (or adaptive) | Single-market blowup protection |
| 9 | Volume-to-size | 5% of 24h volume (rejects 0 volume) | Liquidity exit guarantee |
| 10 | Slippage estimate | 3% max slippage (fail-closed on error) | Orderbook depth validation |

Checks are ordered from cheapest to most expensive (CLOB API call is last). Any failure short-circuits — the remaining checks are skipped.

**Adaptive Position Sizing (P2):** When enabled, overrides the flat 8% target with a dynamic calculation:

```
confidence_factor = (entry_price - 0.90) / (0.99 - 0.90)     # 0.0-1.0, how "safe" the entry
theta_factor      = 1.0 - (days_to_resolution / 14.0)         # 0.0-1.0, closer = better
combined          = 0.6 * confidence_factor + 0.4 * theta_factor
adaptive_size     = min_size + combined * (max_size - min_size)  # 5%-10% range
```

A $0.98 entry resolving in 2 days gets ~9.5% sizing (high confidence, short hold). A $0.94 entry resolving in 10 days gets ~5.8% sizing (lower confidence, longer hold). This concentrates capital in the highest-conviction opportunities.

### Risk Bucket Classifier (`src/risk_buckets.py`)

Markets are classified into correlation buckets independent of Polymarket's category labels:

| Bucket | Max Exposure | Purpose |
|---|---|---|
| politics | 30% | Elections, government, policy |
| crypto | 30% | Bitcoin, Ethereum, DeFi, ETFs |
| macro | 30% | Fed rates, GDP, commodities, geopolitics |
| sports | 25% | Games, matches, tournaments |
| culture | 20% | Entertainment, awards, media |
| science_tech | 25% | AI, space, FDA, patents |
| other | 20% | Fallback bucket |

Classification uses two passes:
1. **Category match:** Polymarket category string against known labels
2. **Keyword match:** Word-boundary regex against market question text

This prevents concentration in correlated markets that Polymarket labels differently (e.g., "Will Trump win?" under "Politics" and "GOP nominee?" under "Elections" both map to `politics`).

### Orderbook Monitor (`src/orderbook_monitor.py`) — P0

A fast-cycle (every 20 seconds) monitor that runs independently of the main exit engine. For each open live-mode position, it fetches the CLOB orderbook and computes two metrics:

1. **Bid depth ratio** = total bid-side depth (USD) / position size (USD)
2. **Bid wall change** = current bid depth / previous bid depth (from last 20s check)

Three escalating thresholds:

| Level | Condition | Action |
|---|---|---|
| WARNING | bid_depth_ratio < 2.0 | Discord notification |
| CRITICAL | bid_depth_ratio < 1.0 OR bid_wall_change < 0.30 | Discord CRITICAL alert |
| EXIT | bid_depth_ratio < 0.5 AND bid_wall_change < 0.30 | Pre-emptive exit signal |

The EXIT condition means there isn't enough bid-side liquidity to cover even half the position AND 70%+ of bids were pulled since the last check — a strong signal that market makers are fleeing before a price collapse.

**Skips paper mode positions** (no CLOB orderbook available). Caches previous depths per position and cleans up when positions close.

### Blacklist Learner (`src/blacklist_learner.py`) — P2

An adaptive learning loop that tracks features of losing trades and penalizes future entries with matching features. This is NOT an auto-reject — it feeds into the bond score as a penalty multiplier, allowing the operator to review and promote entries to the hard blacklist if warranted.

**Feature extraction:** After each losing trade (stop-loss, resolution loss, teleportation exit), the learner extracts:
- Category (e.g., "Crypto", "Politics")
- Risk bucket (e.g., "crypto", "macro")
- Keyword bigrams from the market question (e.g., "bitcoin_above", "iran_meeting")

**Penalty calculation:** When a feature's loss count exceeds the threshold (default: 3 losses in 30 days):
- At threshold (3 losses): 0.7x penalty on bond score
- At 2x threshold (6 losses): 0.4x penalty (maximum)
- The worst penalty across all matching features is applied

**Storage:** Uses the `blacklist_learning` SQLite table with feature_type, feature_value, loss_time, market_id, and pnl columns.

---

## Entry Pipeline

Full flow from scan to executed position:

```
Gamma API (all active markets)
  |
  v
Scanner.filter_candidates()       -- Pre-filter: price, volume, liquidity, date, volume trend, time-of-day
  |
  v
BlacklistLearner.get_penalty()    -- P2: Lookup loss-feature penalty for this market
  |
  v
Scanner.score_candidate()         -- Bond score (with catalyst + blacklist penalties)
  |
  v
Detector.is_valid_opportunity()   -- 8-layer deep validation (+ cooldown, parity, catalyst)
  |
  v
RiskEngine.evaluate_entry()       -- 10-check portfolio gate + adaptive sizing
  |
  v
Executor.execute_entry()          -- Place order (paper: simulate, live: CLOB limit order)
  |
  v
Database.save_position()          -- Persist to SQLite (with catalyst_type, binary_catalyst_score)
  |
  v
Notifier.send_trade_alert()      -- Discord notification with portfolio context
```

### Position Sizing

The target position size is:

```
position_size = portfolio_balance * target_position_pct  (default: 8%)
```

This is then potentially overridden by **adaptive sizing (P2)** when enabled:

```
adaptive_size = portfolio_balance * adaptive_pct   (5%-10%, based on confidence × theta)
```

And further constrained by:
- **Risk engine check #8:** Hard cap at 12%
- **Risk engine check #9:** Capped to 5% of 24h volume
- **Min viable position:** Must be at least $15 USDC

With a $1,000 portfolio, typical position size = $50-$100 (5%-10% adaptive range).

---

## Exit Engine

The exit engine (`src/exit_engine.py`) evaluates every open position each cycle in priority order:

### Exit Priority (Highest First)

| Priority | Trigger | Urgency | Description |
|---|---|---|---|
| 1 | Market resolved | Normal | Gamma API reports resolved/closed. Win = YES, Loss = NO. |
| 2 | UMA dispute | Hold (locked) | Capital locked in dispute. Blocks category for new entries. |
| 2.5 | Teleportation (P0) | Immediate | Gap-down detected past stop-loss level. Emergency exit. |
| 3 | Stop-loss | Immediate | Tiered by entry price. Uses FOK market order. |
| 4 | Trailing stop | Immediate | Activates at $0.995, trails 0.5% below high-water mark. |
| 5 | Time exit | Normal | Dynamic: expected_resolution + 2 days, capped at 14 days. |
| 6 | Take-profit | Normal | Bond: exit at $0.995 with fee optimization (hold vs sell EV). |
| 7 | Portfolio drawdown | Normal | At -5%: close worst position. At -8%: close bottom 3. |
| 7.5 | Re-validation (P2) | Normal | Every 4h, re-runs detector validation on open positions. |
| 8 | Orderbook exit (P0) | Immediate | From orderbook monitor: bid depth ratio < 0.5 + wall pulled. |

### Teleportation Slippage Protection (P0)

Detects "gap-down" events where the price drops past the stop-loss level between monitoring cycles, meaning the stop-loss would never have triggered. Example: price was $0.96, next check it's $0.78 — the 7% stop at $0.893 was never hit.

Detection logic:
```
current_drop = (entry_price - current_price) / entry_price
teleportation = current_drop > stop_loss_pct * detection_multiplier (2.0x)
```

Two severity levels:
- **Catastrophic** (drop > `teleportation_max_loss_pct`, default 50%): Immediate market exit with widened slippage tolerance (10%)
- **Survivable** (drop > 2x stop-loss but < 50%): Immediate exit at normal slippage

On teleportation exit, the bot:
1. Sends a CRITICAL Discord alert with drop percentage and estimated loss
2. Sets `teleportation_flag = 1` on the position record
3. Records loss features in the blacklist learner

### Tiered Stop-Loss

Higher entry prices get tighter stops because the margin of safety is thinner:

| Entry Price | Stop-Loss % | Trigger Price (example) |
|---|---|---|
| >= $0.98 | 5% | $0.931 |
| >= $0.96 | 7% | $0.893 |
| >= $0.93 | 10% | $0.837 |
| >= $0.90 | 12% | $0.792 |

**Justification:** A $0.98 entry has only 2% upside to resolution. A 10% stop-loss would create a 5:1 loss-to-gain ratio. The 5% stop keeps the loss-to-gain ratio at 2.5:1, which is sustainable at 85%+ win rates.

### Trailing Stop (A6)

Activates when price reaches $0.995 (near resolution payout). Once active, the stop follows the high-water mark minus 0.5%. This locks in near-resolution profits if the market briefly spikes then reverses.

### Bond Take-Profit Logic with Fee Optimization (P3)

For bond positions (entry >= $0.95): exit early at $0.995 only if resolution is >48 hours away. If resolution is close (<48h), hold for the full $1.00 payout since the remaining 0.5% gain is nearly guaranteed.

When the `exit_fee_optimization` feature flag is enabled, take-profit decisions use an EV comparator:

```
sell_pnl = (current_price - entry_price) * shares - fee_rate * current_price * shares
hold_ev  = current_price * (1.0 - entry_price) * shares - (1 - current_price) * entry_price * shares * 0.5
verdict  = "hold" if hold_ev > sell_pnl else "sell"
```

This compares the guaranteed profit from selling now (minus fees) against the expected value of holding to resolution (accounting for the small probability of total loss). At prices very close to $1.00 (e.g., $0.998), holding is almost always preferred because the fee to sell erodes a significant portion of the remaining gain.

**Edge case:** Zero shares always returns "sell" to avoid division issues.

### Post-Entry Re-Validation (P2)

Every `revalidation_interval_hours` (default: 4 hours), each open position is re-checked against the detector's validation layers. If the market no longer passes validation (e.g., liquidity dried up, category was blacklisted, or fundamentals changed), the position is flagged for exit.

This catches situations where a market was valid at entry but conditions deteriorated while the position was open. The `last_revalidation_time` column tracks when each position was last checked.

### Dynamic Holding Period

Instead of a fixed max holding period, the engine calculates:

```
dynamic_max = max(days_held + days_to_resolution + 2, config_max_holding_days)
dynamic_max = min(dynamic_max, 14)  # absolute cap
```

This extends holding for markets approaching resolution (where the trade thesis is intact) while still enforcing a 14-day absolute cap.

---

## Risk Management

### Portfolio-Level Controls

| Control | Setting | Purpose |
|---|---|---|
| Max deployed capital | 80% | Always keep 20% cash for exits/opportunities |
| Max single position | 12% hard cap, 5-10% adaptive | Prevent single-market concentration |
| Max per category | 35% | Limit sector exposure |
| Max per event group | 25% | Prevent correlated event concentration |
| Max per risk bucket | 15-30% (varies) | Cross-category correlation clustering |
| Max daily loss | 5% | Circuit breaker for bad days |
| Consecutive loss halt | 5 losses, 6h cooldown | Pause trading during losing streaks |
| Max slippage | 3% | Reject illiquid entries |
| Volume sizing | 5% of 24h volume (0 = reject) | Ensure position is exit-able |
| Volume-to-size zero guard | Reject when volume = 0 | Prevent entries in dead markets |
| Liquidity fail-closed | Reject on API error | Default to rejection on liquidity check failure |

### Circuit Breaker

After 5 consecutive losses (reduced from 8), all new entries are halted for 6 hours. The cooldown is time-based — once 6 hours elapse, trading resumes even if the loss streak hasn't been broken by a win. This prevents the bot from sitting idle indefinitely during volatile periods. The lower threshold triggers earlier to limit damage from systematic edge degradation.

### UMA Dispute Handling (A2)

When a UMA dispute is detected on any position:
1. Position status is marked as "disputed"
2. The market's category is temporarily blocked for new entries (contagion prevention)
3. A CRITICAL alert is sent via Discord
4. Capital remains locked until dispute resolution

### Slippage Tracking (A1)

For stop-loss exits in live mode, the executor compares expected exit price (entry * (1 - stop_loss_pct)) to actual fill price. If slippage exceeds 5%, a CRITICAL alert is sent and the slippage data is persisted for analysis.

---

## Money Management

### Position Sizing Formula

```
base_size = portfolio_balance * target_position_pct     # 8% of portfolio
capped_size = min(base_size, max_single_market_pct * portfolio_balance)  # 12% hard cap
volume_capped = min(capped_size, market_volume_24h * volume_size_max_pct)  # 5% of 24h volume
final_size = volume_capped if volume_capped >= min_viable_position else REJECT
```

### Capital Allocation Example ($1,000 Portfolio)

```
Target position:  $80    (8% of $1,000)
Hard cap:         $120   (12% of $1,000)
Max deployed:     $800   (80% of $1,000)
Max per category: $350   (35% of $1,000)
Max per bucket:   $150-300 (15-30% depending on bucket)
Max positions:    ~10-12  (at $80 each within $800 deployed limit)
Min cash reserve: $200   (20% of $1,000)
```

### Fee Estimation

The bot uses a conservative 0.2% taker fee estimate (`ESTIMATED_FEE_RATE = 0.002`). Fees are deducted from both entry and exit calculations:

```
entry_fees = position_size * 0.002
exit_fees  = shares * exit_price * 0.002
net_pnl    = (shares * exit_price) - cost_basis - exit_fees
```

### Minimum Net Yield Gate

A trade is only taken if:

```
net_yield = (1.00 - entry_price) / entry_price - 0.004 >= 0.008  (0.8%)
```

The 0.004 accounts for round-trip fees (0.2% entry + 0.2% exit). This prevents entries where fees consume most of the spread.

---

## Expected Value Analysis

### The Core EV Equation

```
EV per trade = (win_rate * avg_win) - (loss_rate * avg_loss)
```

### Scenario Analysis

Using the bot's default parameters:

**Conservative scenario** (WR: 80%, avg entry: $0.95):

| Metric | Value |
|---|---|
| Win rate | 80% |
| Avg entry price | $0.95 |
| Avg win (gross) | $0.05/share (5.26%) |
| Avg loss (stop-loss at 10%) | -$0.095/share (-10%) |
| EV per $1 risked | 0.80 * $0.05 - 0.20 * $0.095 = $0.021 |
| **EV per trade ($80 position)** | **+$1.68** |

Note: With the tightened entry band ($0.94+), the conservative scenario now corresponds to the lower edge of acceptable entries rather than the middle.

**Moderate scenario** (WR: 85%, avg entry: $0.96):

| Metric | Value |
|---|---|
| Win rate | 85% |
| Avg entry price | $0.96 |
| Avg win (gross) | $0.04/share (4.17%) |
| Avg loss (stop-loss at 7%) | -$0.067/share (-7%) |
| EV per $1 risked | 0.85 * $0.04 - 0.15 * $0.067 = $0.024 |
| **EV per trade ($80 position)** | **+$1.92** |

**Optimistic scenario** (WR: 90%, avg entry: $0.97):

| Metric | Value |
|---|---|
| Win rate | 90% |
| Avg entry price | $0.97 |
| Avg win (gross) | $0.03/share (3.09%) |
| Avg loss (stop-loss at 7%) | -$0.068/share (-7%) |
| EV per $1 risked | 0.90 * $0.03 - 0.10 * $0.068 = $0.020 |
| **EV per trade ($80 position)** | **+$1.60** |

### Breakeven Win Rate

For the strategy to be profitable, the win rate must exceed:

```
breakeven_WR = avg_loss / (avg_win + avg_loss)
```

| Entry Price | Stop-Loss | Breakeven WR |
|---|---|---|
| $0.95 | 10% | 66.7% |
| $0.96 | 7% | 63.6% |
| $0.97 | 7% | 70.0% |
| $0.98 | 5% | 71.4% |

The bot targets markets with 90%+ implied probability (price >= $0.90), giving substantial headroom above the breakeven win rate.

### Expectancy Formula

The bot computes expectancy in `Database.get_performance_summary()`:

```python
expectancy = win_rate * avg_win_pnl - (1 - win_rate) * abs(avg_loss_pnl)
```

A positive expectancy means the strategy has a mathematical edge. The daily performance summary reports this metric.

---

## Win Rate & Risk-Reward

### Win Rate Expectations

Since the bot only enters markets priced at $0.94-$0.99 (implying 94-99% YES probability), the baseline win rate should exceed 85%. The tightened entry band (from $0.90) combined with the 8-layer validation and risk engine further filter low-quality opportunities.

**Factors that improve win rate:**
- Higher entry price ($0.97+ vs $0.90) = higher implied probability
- Shorter time to resolution = less time for adverse events
- Higher liquidity = more efficient pricing (fewer mispriced markets)
- Objective resolution sources = less ambiguity

**Factors that reduce win rate:**
- Sports/esports markets (gap-down risk from live events)
- Long time to resolution (more time for tail events)
- Subjective resolution criteria
- Low liquidity (potential mispricing)

### Risk-Reward Ratio (R:R)

```
R:R = avg_win / abs(avg_loss)
```

The bond strategy has an **inverted R:R** compared to typical trading strategies:

| Entry | Avg Win | Avg Loss (stop) | R:R |
|---|---|---|---|
| $0.95 | +$0.05 | -$0.095 | 0.53 |
| $0.96 | +$0.04 | -$0.067 | 0.60 |
| $0.97 | +$0.03 | -$0.068 | 0.44 |
| $0.98 | +$0.02 | -$0.049 | 0.41 |

**Why inverted R:R works:** The strategy compensates with a very high win rate. A 0.5 R:R with an 85% win rate produces:

```
EV = 0.85 * W - 0.15 * L = 0.85 * W - 0.15 * 2W = 0.85W - 0.30W = 0.55W
```

This is positive as long as there are any wins. The key insight is that R:R alone is meaningless without win rate context.

### Profit Factor

```
profit_factor = gross_wins / gross_losses
```

A profit factor > 1.0 means the strategy is profitable. For bond strategies:

| Win Rate | R:R | Profit Factor |
|---|---|---|
| 85% | 0.5 | 2.83 |
| 90% | 0.5 | 4.50 |
| 80% | 0.5 | 2.00 |
| 85% | 0.4 | 2.27 |

The bot computes and reports profit factor in the daily performance summary.

---

## Notifications & Observability

### Discord Webhook Notifications

All notifications include a `[PAPER]` or `[LIVE]` mode tag prefix.

| Notification | Trigger | Content |
|---|---|---|
| Trade Alert | Entry/exit execution | Price, shares, P&L, portfolio value |
| Position Alert | Price decline (5%/8%) | Current vs entry price, % change |
| Teleportation Alert (P0) | Gap-down past stop-loss | Drop %, estimated loss, CRITICAL severity |
| Orderbook Alert (P0) | Bid depth imbalance | Bid depth ratio, wall change, WARNING/CRITICAL/EXIT |
| Warning | UMA dispute, anomalies | Description, severity level |
| Error | System/API failures | Error message |
| Critical | Severe slippage >5% | Slippage details |
| Hourly Snapshot | Every hour | Portfolio value, deployed %, P&L |
| Daily Report | Midnight UTC | Day's trades, P&L, win rate |
| Performance Summary | Configurable hour (default midnight) | Lifetime metrics, streaks, drawdown |

### Performance Summary Metrics

The lifetime performance summary (`send_performance_summary()`) reports:

- **Overview:** Closed trades, open trades, win rate (W/L)
- **P&L:** Total P&L, ROI on deployed capital, fees paid
- **Win Stats:** Average win ($, %), max win, max win streak
- **Loss Stats:** Average loss ($, %), max loss, max loss streak
- **Risk/Reward:** R:R ratio, profit factor, expectancy per trade
- **Drawdown:** Peak cumulative P&L, max drawdown from peak
- **Timing:** Average holding duration
- **Exit Reasons:** Breakdown by exit type (resolution_win, stop_loss, take_profit, etc.)

---

## Configuration Reference

### Scanner

| Key | Default | Description |
|---|---|---|
| `scan_interval_seconds` | 300 | Seconds between market scans |
| `min_entry_price` | 0.94 | Minimum YES price for bond zone |
| `max_entry_price` | 0.99 | Maximum YES price |
| `max_days_to_resolution` | 14 | Maximum days until resolution |
| `preferred_resolution_hours` | 72 | Preferred resolution window |
| `min_liquidity` | 5000 | Minimum CLOB liquidity (USD) |
| `min_volume_24h` | 2500 | Minimum 24h volume (USD) |
| `max_price_volatility_1d` | 0.05 | Maximum 1-day price change |
| `volume_trend_min_ratio` | 0.70 | P1: 24h vol must be >= 70% of 7d daily avg |
| `time_filter_weekend_multiplier` | 2.0 | P3: Liquidity multiplier on weekends |
| `time_filter_offpeak_multiplier` | 1.5 | P3: Liquidity multiplier off-peak hours |

### Risk

| Key | Default | Description |
|---|---|---|
| `max_single_market_pct` | 0.12 | Hard cap per position (12%) |
| `target_position_pct` | 0.08 | Default sizing target (8%) |
| `max_correlated_pct` | 0.25 | Max per event group (25%) |
| `max_category_exposure_pct` | 0.35 | Max per category (35%) |
| `max_deployed_pct` | 0.80 | Max total deployment (80%) |
| `max_daily_loss_pct` | 0.05 | Daily loss halt (5%) |
| `consecutive_loss_halt` | 5 | Consecutive losses to halt |
| `consecutive_loss_cooldown_hours` | 6 | Hours before resuming |
| `max_slippage_pct` | 0.03 | Max entry slippage (3%) |
| `volume_size_max_pct` | 0.05 | Max % of 24h volume (5%) |
| `min_viable_position` | 15 | Minimum position size (USD) |
| `min_net_yield` | 0.008 | Minimum net yield (0.8%) |

### Adaptive Sizing (P2)

| Key | Default | Description |
|---|---|---|
| `adaptive_sizing.enabled` | true | Enable confidence × theta sizing |
| `adaptive_sizing.min_size_pct` | 0.05 | Minimum adaptive size (5%) |
| `adaptive_sizing.max_size_pct` | 0.10 | Maximum adaptive size (10%) |

### Exits

| Key | Default | Description |
|---|---|---|
| `stop_loss_pct` | 0.10 | Default stop-loss fallback |
| `tiered_stop_loss` | (4 tiers) | Entry-price-based stop-loss tiers |
| `bond_take_profit_price` | 0.995 | Take-profit price for bonds |
| `bond_take_profit_min_hours_to_resolution` | 48 | Min hours to resolution for TP |
| `max_holding_days` | 14 | Default max holding (extended dynamically) |
| `trailing_stop_activation_price` | 0.995 | Trailing stop activation |
| `trailing_stop_distance_pct` | 0.005 | Trailing stop distance (0.5%) |
| `portfolio_drawdown_alert_pct` | 0.05 | Close worst at -5% |
| `portfolio_drawdown_critical_pct` | 0.08 | Close bottom 3 at -8% |
| `revalidation_interval_hours` | 4 | P2: Re-validation check interval |

### Teleportation (P0)

| Key | Default | Description |
|---|---|---|
| `teleportation_max_loss_pct` | 0.50 | Absolute max loss before forced exit |
| `teleportation_detection_multiplier` | 2.0 | Drop > stop_loss × this = teleportation |
| `teleportation_exit_slippage_pct` | 0.10 | Widened slippage for teleport exits |

### Orderbook Monitor (P0)

| Key | Default | Description |
|---|---|---|
| `orderbook_monitor_interval_seconds` | 20 | Check interval (live mode only) |
| `warning_bid_depth_ratio` | 2.0 | Ratio threshold for WARNING |
| `critical_bid_depth_ratio` | 1.0 | Ratio threshold for CRITICAL |
| `exit_bid_depth_ratio` | 0.5 | Ratio threshold for EXIT signal |
| `bid_wall_pull_threshold` | 0.30 | Wall change threshold for EXIT |

### Binary Catalyst (P1)

| Key | Default | Description |
|---|---|---|
| `binary_catalyst_reject_threshold` | 0.85 | Score above this = reject market |
| `binary_catalyst_penalize_threshold` | 0.50 | Score above this = penalize score |
| `binary_catalyst_penalty_factor` | 0.60 | Penalty multiplier on bond score |

### Detector (P1/P2)

| Key | Default | Description |
|---|---|---|
| `price_drop_cooldown_threshold` | -0.03 | Reject if recent drop > 3% |
| `price_drop_recovery_ratio` | 0.80 | Required recovery before re-entry |
| `min_parity_sum` | 0.96 | Min YES+NO sum for parity check |
| `max_parity_sum` | 1.04 | Max YES+NO sum for parity check |

### Blacklist Learner (P2)

| Key | Default | Description |
|---|---|---|
| `blacklist_learner.enabled` | true | Enable loss feature tracking |
| `blacklist_learner.loss_threshold` | 3 | Losses to trigger penalty |
| `blacklist_learner.window_days` | 30 | Rolling window for loss counting |

### Scoring (P3)

| Key | Default | Description |
|---|---|---|
| `scoring.use_exponential_proximity` | true | Use exponential time weighting |
| `scoring.resolution_proximity_decay_rate` | 0.3 | Decay rate for exponential mode |

---

## Data Model

### SQLite Tables

**positions** - Core trade ledger

| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment ID |
| market_id | TEXT | Polymarket market ID |
| market_question | TEXT | Human-readable question |
| token_id | TEXT | CLOB YES token ID |
| entry_price | REAL | Entry price per share |
| shares | REAL | Number of shares |
| cost_basis | REAL | Total entry cost |
| entry_time | TEXT | ISO timestamp |
| expected_resolution | TEXT | Market end date |
| status | TEXT | open / closed / disputed |
| exit_price | REAL | Exit price per share |
| exit_time | TEXT | ISO timestamp |
| pnl | REAL | Realized P&L |
| fees_paid | REAL | Total fees |
| bond_score | REAL | Score at entry time |
| paper_trade | INTEGER | 1=paper, 0=live |
| exit_reason | TEXT | resolution_win, stop_loss, teleportation, etc. |
| category | TEXT | Polymarket category |
| risk_bucket | TEXT | Classified risk bucket |
| event_group_id | TEXT | Event group for correlation |
| teleportation_flag | INTEGER | P0: 1 if position exited via teleportation |
| orderbook_exit_flag | INTEGER | P0: 1 if position exited via orderbook imbalance |
| catalyst_type | TEXT | P1: "binary" or "continuous" |
| binary_catalyst_score | REAL | P1: Binary catalyst score at entry |
| last_revalidation_time | TEXT | P2: ISO timestamp of last re-validation |

**performance_daily** - Daily aggregated stats

| Column | Type | Description |
|---|---|---|
| date | TEXT UNIQUE | UTC date |
| trades_opened | INTEGER | Positions opened |
| trades_closed | INTEGER | Positions closed |
| realized_pnl | REAL | Day's realized P&L |
| unrealized_pnl | REAL | Snapshot at EOD |
| fees_paid | REAL | Day's fees |
| win_count | INTEGER | Profitable closes |
| loss_count | INTEGER | Losing closes |
| portfolio_balance | REAL | Balance at snapshot |
| total_deployed | REAL | Capital deployed |

**blacklist_learning** (P2) - Loss feature tracking for adaptive penalties

| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment ID |
| feature_type | TEXT | "category", "risk_bucket", or "keyword_bigram" |
| feature_value | TEXT | The feature value (e.g., "crypto", "bitcoin_above") |
| loss_time | TEXT | ISO timestamp of loss recording |
| market_id | TEXT | Market that generated the loss |
| pnl | REAL | P&L of the losing trade |

**scan_log** - Scan cycle audit trail

**alerts** - Alert history with deduplication

**rejected_markets** - Markets rejected by detector (for analysis)

### Schema Migrations

New columns are added via `ALTER TABLE ADD COLUMN` with safe defaults during database initialization. The migration logic checks for existing columns before adding, making it idempotent. New tables (like `blacklist_learning`) are created with `CREATE TABLE IF NOT EXISTS`.

---

## Feature Flags

All 14 advanced features are gated behind individual feature flags in `config.yaml`. Setting a flag to `false` disables the feature without any code changes — the bot falls back to its base behavior.

| Flag | Default | Feature | Priority |
|---|---|---|---|
| `teleportation_detection` | true | Gap-down detection & emergency exit | P0 |
| `orderbook_monitor` | true | Bid depth imbalance monitoring | P0 |
| `binary_catalyst_filter` | true | Binary vs continuous catalyst classification | P1 |
| `volume_trend_filter` | true | 24h volume vs 7d average trend check | P1 |
| `price_drop_cooldown` | true | Reject recently-declined markets | P1 |
| `adaptive_sizing` | true | Confidence × theta position sizing | P2 |
| `secondary_price_validation` | true | YES+NO parity check | P2 |
| `post_entry_revalidation` | true | Periodic re-validation of open positions | P2 |
| `blacklist_learner` | true | Loss feature tracking & penalty scoring | P2 |
| `exit_fee_optimization` | true | Hold vs sell EV comparison at take-profit | P3 |
| `time_of_day_filter` | true | Weekend/off-peak liquidity multiplier | P3 |
| `exponential_proximity` | true | Exponential resolution proximity weighting | P3 |

Feature flags are checked via `feature_enabled(config, flag_name)` in `src/utils.py`, which returns `False` if the flag is missing or the `feature_flags` section doesn't exist. This ensures safe fallback behavior.

---

## Known Risks & Mitigations

### 1. Gap-Down Risk (Sports/Esports/Binary Catalysts)

**Risk:** Live sports markets and binary-catalyst markets can gap from $0.95 to $0.50 in seconds when an unexpected outcome occurs. The 60-second monitoring cycle can't detect and exit fast enough.

**Mitigation:**
- Excluded categories: Sports, E-Sports, Esports, Live Sports
- Keyword blacklist: VCT, Valorant, BO3/BO5, CSGO, League of Legends, etc.
- Live-event gate: Rejects sports-bucket markets resolving within 6 hours
- Risk bucket cap: Sports bucket limited to 25% exposure
- **P0: Teleportation detection** — detects gap-downs that skip past stop-loss levels and forces emergency exit
- **P1: Binary catalyst filter** — classifies and rejects/penalizes markets with sharp binary resolution triggers (court rulings, elections, announcements)
- **P0: Orderbook monitor** — 20-second bid depth checks detect liquidity withdrawal before price collapse

### 2. UMA Dispute Lockup

**Risk:** Polymarket's UMA oracle can enter dispute mode, locking all capital in that market for days/weeks.

**Mitigation:**
- Dispute detection with position status tracking
- Category-level blocking prevents new entries in disputed categories
- CRITICAL alerts notify operator immediately
- Capital is tracked as "disputed" (not lost)

### 3. Thin Liquidity / Slippage

**Risk:** Low-liquidity markets may not have enough orderbook depth to exit at the intended stop-loss price.

**Mitigation:**
- Volume-to-size check: Position can't exceed 5% of 24h volume (zero volume = reject)
- Orderbook depth validation: Bid depth must be 3x position size
- Slippage tracking on live exits with CRITICAL alert at >5%
- FOK (fill-or-kill) orders for urgent exits at 3% below mid-price
- **P0: Orderbook monitor** — 20-second bid depth surveillance detects liquidity withdrawal
- **P1: Volume trend filter** — rejects markets with declining volume (< 70% of 7d avg)
- **P3: Time-of-day filter** — higher liquidity bar during weekends (2x) and off-peak hours (1.5x)
- **Fail-closed on API errors** — liquidity check returns rejection (not approval) when the API call fails

### 4. API Downtime / Rate Limiting

**Risk:** Gamma API or CLOB API may be unavailable, preventing price monitoring or order placement.

**Mitigation:**
- Rate limiter with token bucket (1.5 req/sec, burst 10)
- Retry with exponential backoff (2s, 4s, 8s)
- Cached candidates survive API outages (stale cache returned)
- Graceful shutdown on persistent failures

### 5. Systemic Market Correlation

**Risk:** A macro event (e.g., regulatory crackdown) could cause multiple positions across categories to fail simultaneously.

**Mitigation:**
- Risk bucket caps limit cross-category correlation
- Max deployed at 80% keeps 20% cash reserve
- Portfolio drawdown triggers close worst/bottom 3 positions
- Daily loss circuit breaker at 5%

### 6. Resolution Ambiguity

**Risk:** Markets with subjective or ambiguous resolution criteria may resolve unexpectedly.

**Mitigation:**
- Detector Layer 4 rejects subjective patterns
- Keyword blacklist filters social media and opinion-based markets
- Stale resolution flagging (48h past end date without resolution)

---

## Running the Bot

### Paper Mode (Recommended for Testing)

```bash
# Activate virtual environment
source venv/bin/activate

# Start paper trading (foreground)
python main.py --paper

# Start paper trading (background, persistent)
nohup python main.py --paper > logs/paper_bot.log 2>&1 &
echo $! > logs/bot.pid
```

### Live Mode

```bash
# Required environment variables
export PRIVATE_KEY="your_polygon_private_key"
export PROXY_WALLET_ADDR="your_proxy_wallet_address"
export WEBHOOK_URL="your_discord_webhook_url"

# Start live trading
python main.py
```

### Emergency Stop

```bash
# Graceful shutdown via signal
kill $(cat logs/bot.pid)

# Emergency halt (creates halt file, bot checks each cycle)
touch HALT
```

### Test Suite

```bash
source venv/bin/activate
python -m pytest tests/ -v
```

The test suite includes 197 tests covering all modules and all 14 new features:

| Test File | Tests | Coverage |
|---|---|---|
| `test_teleportation.py` | 5 | Gap-down detection, catastrophic vs survivable, edge cases |
| `test_orderbook_monitor.py` | 5 | Bid depth ratio, wall change, paper mode skip, cleanup |
| `test_binary_catalyst.py` | 6 | Pattern classification, reject/penalize thresholds |
| `test_volume_trend.py` | 4 | Volume trend filter, time-of-day liquidity multipliers |
| `test_price_drop_cooldown.py` | 3 | Cooldown detection, recovery ratio |
| `test_adaptive_sizing.py` | 5 | Confidence × theta weighting, min/max bounds |
| `test_yes_no_parity.py` | 4 | Parity validation, edge cases |
| `test_blacklist_learner.py` | 7 | Feature extraction, penalty escalation, window expiry |
| `test_exit_fee_optimization.py` | 4 | Hold vs sell EV comparison, edge cases |
| `test_resolution_proximity.py` | 6 | Exponential weighting, decay rates, score integration |

---

## V3 Upgrade (2026-04-15) — Post-Tail-Event Corrections

### Trigger
Trade #26 (Claude AI benchmark market) lost -$10.23, erasing 76% of cumulative
P&L. Single-trade loss was 3.15× largest prior win. Profit factor dropped from
2.86 to 1.18. Breakeven WR margin compressed from +23.4pp to +3.5pp.

### Changes Made

#### P0 — Emergency
- **AI/Benchmark/Meta-prediction blacklist:** 30+ keyword patterns added to L1
  filter covering AI models, benchmarks, LLM rankings, specific model families
  (Claude, GPT, Gemini, Llama, DeepSeek). Category blacklist expanded.
- **Commodity direction penalty:** "up or down", "higher or lower" patterns
  apply 0.3× bond score penalty.
- **Stop-loss tiers inverted:** Lower-entry bonds now get TIGHTER stops
  ($0.94→3%, $0.96→4%, $0.98→5%). Would have saved $8.48 on trade #26.
- **$0.98-$0.99 band eliminated:** max_entry_price reduced from 0.99 to 0.975.
  Would have prevented 7 net-negative trades (-$3.19).

#### P1 — Structural
- **Position sizing reduced:** target from 8% to 4%, hard cap from 12% to 6%.
  Reduces worst-case single-trade loss from ~$10 to ~$1.20.
- **Bucket confidence scaling:** New buckets with no history get 0.5× sizing.
  Buckets with negative P&L get 0.5× sizing.
- **Minimum net yield raised:** from 0.8% to 1.5%. Filters marginal trades
  that barely clear fees.
- **Fee-drag dashboard metric:** Alerts when fees exceed 40% of gross edge.

#### P2 — Defensive
- **Fluke filter:** Losses ≥3× trailing average auto-pause the bucket for 24h.
  Prevents rapid accumulation of losses in a new failure mode.
- **Temporary block expiry:** Category blocks now auto-expire after configurable
  duration (default 24h).

### Evaluation Milestones (updated)
- **N=30 closed trades:** Recheck profit factor. Must be >1.3 to continue.
- **N=50 closed trades:** Decide on live deployment readiness. Require PF >1.5,
  WR >70%, no entry band with negative expectancy.
- **N=100 closed trades:** Full go/no-go. If passing, deploy live at $500 with
  eighth-Kelly sizing (~3% per position).

### What the data says about timing
At current rate of ~4 trades/day (likely increasing after $0.98 band removal
frees pipeline capacity), reaching N=50 takes ~8-10 more days. N=100 takes
~20-22 more days. Recommend continuous paper trading through end of April
before any live capital.
