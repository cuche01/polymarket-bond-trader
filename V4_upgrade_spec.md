# Polymarket Bond Bot — V4 Upgrade Specification

**Document purpose:** Actionable engineering spec for Claude Code to implement improvements to the Polymarket Bond Strategy Bot. Each change is scoped, prioritized, and gated by safety checks that prevent the bot from starving itself of eligible trades.

**Audience:** Claude Code (executing against the existing codebase) and the human operator (approving phase transitions).

**Base version:** V3 (2026-04-15, post-Trade-#26 corrections)
**Target version:** V4
**Prerequisites:** V3 bot running in paper mode with Gamma/CLOB API access, SQLite persistence, Discord webhook.

---

## 0. Prime Directives (Read Before Every Change)

These are non-negotiable invariants. Every change must preserve them.

### 0.1 Pipeline Non-Starvation Guarantee

**Rule:** The system must *always* have a path to at least one eligible trade per 24-hour window under typical market conditions.

**Enforcement mechanism** (implement this FIRST, before any other change):

Create `src/pipeline_health.py` with the following logic:

```
For each scan cycle, record:
  - candidates_fetched (from Gamma)
  - candidates_passed_prefilter
  - candidates_passed_detector
  - candidates_passed_risk_engine
  - entries_executed

Daily aggregated metrics stored in `pipeline_health` table:
  - date, total_scans, median_candidates_fetched,
  - median_passed_prefilter, median_passed_detector,
  - median_passed_risk_engine, total_entries,
  - acceptance_rate = total_entries / total_candidates_fetched

Alert conditions:
  WARNING: acceptance_rate < 0.5% over 24h
  CRITICAL: zero entries for 18+ hours
  STARVATION: zero entries for 36+ hours → auto-relaxation protocol
```

**Auto-relaxation protocol** (only fires at STARVATION):
1. Log the starvation event with full filter-rejection breakdown
2. Send CRITICAL Discord alert
3. Automatically raise `scanner.max_entry_price` by 0.005 (capped at 0.985)
4. Automatically lower `risk.min_net_yield` by 0.001 (floor at 0.010)
5. Require human acknowledgment before further auto-relaxation
6. Revert to baseline after 72h if not re-triggered

**Test:** Before any filter-tightening change is enabled, run it in shadow mode for 5 scan cycles. If shadow-mode acceptance rate is zero, the filter is blocked from activation.

### 0.2 Shadow-Mode Validation for Every New Filter

**Rule:** No new rejection filter gets activated without 24h of shadow-mode data proving it would not have starved the pipeline.

**Implementation pattern:**

```python
# In src/utils.py, add shadow_filter decorator
def shadow_filter(filter_name: str):
    """Decorator that logs a filter's verdict without enforcing it.

    When `feature_flags[filter_name + '_shadow']` is true and
    `feature_flags[filter_name]` is false, the filter RUNS and logs
    its decision but does NOT reject. After validation, the operator
    flips the real flag on and shadow flag off.
    """
    ...
```

**Operator workflow:**
1. Merge filter code with shadow flag true, real flag false
2. Let bot run 24h
3. Query `pipeline_health` table: if `shadow_{filter}_rejections / total_candidates` > 30%, filter is too aggressive → revise
4. If shadow data is acceptable, flip flags and re-verify for 24h
5. Only then declare the filter "enabled"

### 0.3 Reversibility

Every change must be reversible via a single config flag flip or a single `git revert`. No change may:
- Modify historical data in `positions` or `performance_daily` tables (migrations only ADD columns)
- Remove any feature flag without a 30-day deprecation notice
- Break backwards compatibility with existing `config.yaml` (new keys must have safe defaults)

### 0.4 Don't Trust Your Own P&L Yet

At n=26 trades with one known tail event, the realized P&L is not statistically meaningful. Every change must be justified by **mechanism** (what structural improvement does this give?) rather than **fitting** (this would have avoided loss X).

---

## 1. Platform Context (What Changed, What Matters)

Research findings from Polymarket documentation and third-party analysis (verified April 2026). These inform the changes in §2 onwards.

### 1.1 Fee structure is dynamic, not flat

Polymarket's 2026 fee schedule:

- **Taker fees only.** Makers pay 0, receive rebates (20% of crypto fees, 25% of sports fees returned to liquidity providers).
- **Symmetric around $0.50** — fee scales with `p × (1-p)`. At p=0.96, effective rate is ~15.4% of peak.
- **Category-dependent peaks:**

| Category | Peak taker rate | Effective rate at p=0.96 |
|---|---|---|
| Geopolitics | 0% | **0%** (fee-free) |
| Politics | 1.00% | ~0.154% |
| Sports | 0.75% | ~0.115% |
| Finance | 1.00% | ~0.154% |
| Tech | 1.00% | ~0.154% |
| Culture | 1.25% | ~0.192% |
| Weather | 1.25% | ~0.192% |
| Economics | 1.50% | ~0.231% |
| Crypto | 1.80% | ~0.277% |
| Mentions | 1.56% | ~0.240% |

- **Per-market overrides:** As of March 31, 2026, each Gamma market object includes a `feeSchedule` field with authoritative fee data. **Do not hardcode category values; read from the API.**
- **Winning payouts are always free** — redeeming YES at $1.00 upon resolution incurs zero fee.

### 1.2 Holding Rewards: 4% APY on eligible positions

Polymarket pays **4% APY in USDC** on eligible market positions, calculated and distributed daily. Eligibility varies by market (not all markets qualify; check the market object).

**Implication for bond strategy:**
At 4% APY × 3-day average hold = 0.033% additional yield per trade. Small per-trade but compounds: on the moderate scenario ($0.96 entry, 4.17% gross yield), this improves net yield by ~0.8% relative (not absolute). More importantly, longer holds (5-14 day resolution) benefit proportionally more — counteracts the current time-penalty bias toward short durations.

### 1.3 Liquidity Rewards via quadratic Q-score

Polymarket pays makers who post limit orders within `max_spread` of the adjusted midpoint. Scoring is **quadratic** — tighter to midpoint pays much more than wider.

**Critical constraint for bond zone:** In the price range [0.90, 1.00], Q-score requires **double-sided quotes** (both YES and NO). A bot posting only YES bids will earn zero LP rewards in the bond zone. To capture LP rewards, the bot would need to simultaneously post NO sell quotes (or equivalently YES buy quotes at ($1 − target) on the complementary token).

**Decision for V4:** Do NOT attempt full double-sided market making in V4 (requires significant architecture change and inventory risk management). Instead:
1. Flag markets with active LP rewards (`rewardsEnabled`, `rewardsMaxSpread`, `rewardsMinSize`) as **preferred** in the bond score
2. Capture LP rewards on the BID side only when the bot naturally posts tight quotes (which is already the case for maker entries)
3. Log earned LP rewards from daily payouts (they arrive automatically) and track as a separate P&L stream
4. Future V5 could evaluate full two-sided market making in selected markets

### 1.4 Post-only order execution

The CLOB API supports `postOnly=true` on limit orders. This guarantees maker execution — if the order would cross the spread, it's rejected rather than taken. This is the **cleanest way to guarantee zero fees on entry**.

Currently the bot places "CLOB limit orders" for entries but doesn't specify postOnly. This should change.

### 1.5 WebSocket streaming

The CLOB offers WebSocket channels:
- `wss://ws-subscriptions-clob.polymarket.com/ws/market` — market data (prices, trades)
- User channel — order updates, fills

Current bot polls the Gamma API every 300s. Rate limits allow 9,000 CLOB reqs / 10s. WebSocket eliminates polling overhead and reduces reaction latency from minutes to milliseconds.

V4 will NOT fully migrate to WebSockets (significant work), but will introduce WebSocket monitoring for the open-position subset, complementing the existing 20s orderbook monitor loop.

### 1.6 NegRisk merge capability

On NegRisk-enabled events, the smart contract allows conversion of 1 YES + 1 NO → 1 USDC before resolution. This creates a hard arbitrage ceiling: YES + NO cannot sustainably exceed $1.00.

**Implication:** When YES+NO < $0.98 (your current parity check lower bound), there's a free 2¢ arbitrage: buy both, merge to USDC. V4 will **detect and alert** on these opportunities without auto-executing (execution requires CTF contract interactions — future work).

### 1.7 Polymarket US is a separate platform

Polymarket US (polymarketexchange.com) runs a CFTC-regulated DCM with simpler 0.30% flat taker fee and 0.20% maker rebate. API is separate (api.polymarket.us, Ed25519 auth).

**Relevance:** Only accessible to US residents. Nigerian operator cannot use it. But the *existence* of US platform changes Polymarket Global's regulatory risk profile (offshore side continues but with uncertain long-term status).

---

## 2. Phase 1 — Foundation Fixes (P0, BLOCKING)

**Phase goal:** Correct the fee model, guarantee maker execution on entries, and install pipeline health monitoring. These changes do not expand the candidate universe but make every trade more profitable and every system decision visible.

**Success criteria for phase:** Fee calculations match Polymarket's `feeSchedule` within 0.01% per trade; ≥90% of entries fill as maker (post-only); pipeline health dashboard active; no regression in acceptance rate.

### Change 1.1 — Install Pipeline Health Module

**Priority:** P0 (must be first; gates all subsequent changes)

**Files to create:**
- `src/pipeline_health.py`
- `tests/test_pipeline_health.py`

**Files to modify:**
- `src/database.py` — add `pipeline_health` table migration
- `src/scanner.py` — emit per-scan metrics
- `src/detector.py` — emit per-filter-layer rejection reasons
- `src/risk_engine.py` — emit per-check rejection reasons
- `main.py` — initialize and run periodic starvation check

**Database migration:**

```sql
CREATE TABLE IF NOT EXISTS pipeline_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_time TEXT NOT NULL,
    candidates_fetched INTEGER NOT NULL,
    candidates_passed_prefilter INTEGER NOT NULL,
    candidates_passed_detector INTEGER NOT NULL,
    candidates_passed_risk_engine INTEGER NOT NULL,
    entries_executed INTEGER NOT NULL,
    rejection_reasons_json TEXT,
    mode TEXT NOT NULL  -- 'paper' or 'live'
);

CREATE INDEX IF NOT EXISTS idx_pipeline_health_time
  ON pipeline_health(scan_time);
```

**Module contract:**

```python
# src/pipeline_health.py
class PipelineHealth:
    def record_scan(self, metrics: dict) -> None:
        """Called once per scan cycle with the full funnel."""

    def get_acceptance_rate(self, hours: int = 24) -> float:
        """entries_executed / candidates_fetched over window."""

    def get_dry_period_hours(self) -> float:
        """Hours since last entry (paper or live, per mode)."""

    def get_top_rejection_reasons(self, limit: int = 5, hours: int = 24) -> list[tuple[str, int]]:
        """Most common rejection reason codes in window."""

    def check_starvation(self) -> tuple[str, str | None]:
        """Returns (severity, action_required).
        severity ∈ {'OK', 'WARNING', 'CRITICAL', 'STARVATION'}
        action ∈ {None, 'alert', 'auto_relax'}
        """
```

**Config additions to `config.yaml`:**

```yaml
pipeline_health:
  enabled: true
  warning_dry_period_hours: 12
  critical_dry_period_hours: 18
  starvation_dry_period_hours: 36
  min_acceptance_rate_24h: 0.005
  auto_relaxation:
    enabled: false   # explicitly OFF by default; operator opts in
    max_entry_price_step: 0.005
    max_entry_price_ceiling: 0.985
    min_net_yield_step: 0.001
    min_net_yield_floor: 0.010
    require_human_ack_after_first_trigger: true
```

**Integration points:**

In `main.py` event loop, after each scan cycle completes:
```python
metrics = scanner.last_scan_metrics  # populated during scan
metrics['mode'] = 'paper' if bot.paper_mode else 'live'
pipeline_health.record_scan(metrics)

severity, action = pipeline_health.check_starvation()
if severity in ('CRITICAL', 'STARVATION'):
    notifier.send_critical(f"Pipeline {severity}: {action}")
if severity == 'STARVATION' and config['pipeline_health']['auto_relaxation']['enabled']:
    relax_filters(config, pipeline_health)
```

**Tests:**
- `test_record_scan_persists_metrics`
- `test_acceptance_rate_calculation`
- `test_dry_period_detection`
- `test_starvation_triggers_alert`
- `test_auto_relaxation_respects_ceiling`
- `test_shadow_mode_does_not_auto_relax`

**Acceptance criteria:**
- Dashboard shows 24h acceptance rate before this phase ends
- Starvation alert fires within 1h of reaching threshold in simulation
- No change to actual filter behaviour (purely observability)

---

### Change 1.2 — Dynamic Fee Model from Gamma feeSchedule

**Priority:** P0

**Rationale:** The bot currently hardcodes `ESTIMATED_FEE_RATE = 0.002` (flat 0.2%). Real fees are category- and price-dependent and can be read from `market["feeSchedule"]` per Gamma API.

**Files to modify:**
- `src/utils.py` — add `calculate_taker_fee()` and `calculate_maker_fee()` utilities
- `src/scanner.py` — extract `feeSchedule` when fetching markets
- `src/detector.py` — use dynamic fees in net-yield gate
- `src/risk_engine.py` — use dynamic fees in sizing/slippage calcs
- `src/exit_engine.py` — use dynamic fees in hold-vs-sell EV
- `src/database.py` — add `fee_schedule_json` column to `positions`

**New utility:**

```python
# src/utils.py
def calculate_taker_fee(price: float, shares: float,
                        fee_schedule: dict | None) -> float:
    """Compute taker fee in USDC for a trade at given price.

    Polymarket formula: fee = C × Θ × p × (1-p), where C = shares,
    Θ = fee coefficient, p = trade price.

    If fee_schedule is None or fees_enabled=False, returns 0.
    If fee_schedule is a dict, uses its 'takerFeeBps' and
    'feeCoefficient' fields.

    For market categories with no fee (e.g. Geopolitics), returns 0.
    """
    if not fee_schedule or not fee_schedule.get('feesEnabled', False):
        return 0.0

    theta = fee_schedule.get('takerFeeCoefficient',
                              fee_schedule.get('takerFeeBps', 0) / 10000 * 4)
    # Θ × 4 reconstruction from Bps assumes Bps is the max (at p=0.5).
    # Validate against actual Gamma response shape during integration.

    p = max(0.01, min(0.99, price))
    return shares * theta * p * (1 - p)


def calculate_maker_fee(price: float, shares: float,
                         fee_schedule: dict | None) -> float:
    """Makers pay no fees on Polymarket (they receive rebates).
    Returns 0 always, kept as symmetric API for clarity.
    """
    return 0.0


def estimate_round_trip_fee_rate(entry_price: float,
                                  exit_price: float | None,
                                  fee_schedule: dict | None,
                                  entry_is_maker: bool = True,
                                  exit_is_taker: bool = True,
                                  exit_is_resolution: bool = False) -> float:
    """Return round-trip fee as a fraction of position value.

    If exit_is_resolution=True, no exit fee (resolution payout is free).
    """
    if fee_schedule is None or not fee_schedule.get('feesEnabled', False):
        return 0.0

    entry_fee_rate = 0.0 if entry_is_maker else _taker_rate_at(entry_price, fee_schedule)

    if exit_is_resolution:
        exit_fee_rate = 0.0
    else:
        exit_price_est = exit_price if exit_price else 1.0
        exit_fee_rate = _taker_rate_at(exit_price_est, fee_schedule) if exit_is_taker else 0.0

    return entry_fee_rate + exit_fee_rate
```

**Config update:**

```yaml
fees:
  use_dynamic_fees: true             # read from market.feeSchedule
  fallback_taker_rate: 0.002         # used only if feeSchedule missing
  assume_entry_maker: true           # post-only entries = 0 fee
  assume_exit_taker: true            # stop-loss/teleport = taker
  resolution_is_free: true           # winning redemption = no fee

# Deprecate (keep for rollback but mark unused):
# ESTIMATED_FEE_RATE: 0.002
```

**Min-net-yield gate update** (in detector.py):

```python
# OLD: net_yield = gross_yield - 0.004 (flat 0.4%)
# NEW:
round_trip_fee = estimate_round_trip_fee_rate(
    entry_price=entry_price,
    exit_price=None,  # unknown at entry
    fee_schedule=market.get('feeSchedule'),
    entry_is_maker=config['fees']['assume_entry_maker'],
    exit_is_taker=True,  # worst case for gate
    exit_is_resolution=False,  # conservative: assume early exit
)
net_yield = gross_yield - round_trip_fee
```

**Positions table migration:**

```sql
ALTER TABLE positions ADD COLUMN fee_schedule_json TEXT;
ALTER TABLE positions ADD COLUMN estimated_entry_fee REAL DEFAULT 0;
ALTER TABLE positions ADD COLUMN estimated_exit_fee REAL DEFAULT 0;
ALTER TABLE positions ADD COLUMN actual_entry_fee REAL;   -- populated from fills
ALTER TABLE positions ADD COLUMN actual_exit_fee REAL;
```

**Tests:**
- `test_taker_fee_symmetric_around_half`
- `test_maker_fee_always_zero`
- `test_geopolitics_no_fee`
- `test_round_trip_respects_resolution_flag`
- `test_fallback_when_fee_schedule_missing`
- `test_net_yield_gate_with_dynamic_fees`

**Acceptance criteria:**
- For 10 sample markets across categories, computed fees match Polymarket's UI within 0.01%
- Min-net-yield gate passes more markets on fee-free categories (Geopolitics) than fee-bearing (Crypto)
- No regression: acceptance rate in 24h shadow mode is within ±25% of V3 baseline

**Guardrail:** If the new min-net-yield gate causes acceptance rate to drop by >40% vs V3, automatically add `min_net_yield_override_by_category` as compensation and alert operator.

---

### Change 1.3 — Post-Only Limit Orders for Entries

**Priority:** P0

**Rationale:** The bot says "CLOB limit order" for entries but doesn't enforce maker execution. Using `postOnly=true` guarantees zero entry fees. If the order would cross, it's rejected — the bot then retries at a better price or skips.

**Files to modify:**
- `src/executor.py` — add post-only placement with retry/cancel logic

**Behaviour contract:**

```python
# src/executor.py
async def place_entry_order(self, market, size_usd, entry_price_target,
                             max_attempts=3, retry_wait_sec=10):
    """Place a post-only limit order for entry.

    Strategy:
    1. Attempt 1: price = best_bid (sit behind spread, most conservative)
    2. If rejected (would cross) or unfilled in retry_wait_sec:
    3. Attempt 2: price = best_bid + 1 tick (aggressive maker)
    4. If rejected or unfilled:
    5. Attempt 3: price = midpoint - 1 tick (step into spread, last chance)
    6. If all fail, SKIP this entry (do not fall back to taker)

    Returns (filled_price, filled_shares, fee_paid) on success,
            None on all-attempts-fail.
    """
    if not self.live_mode:
        # Paper mode: simulate instantaneous maker fill at entry_price_target
        return (entry_price_target, size_usd / entry_price_target, 0.0)

    for attempt in range(max_attempts):
        price = self._compute_entry_price_for_attempt(market, attempt, entry_price_target)
        order = self.clob_client.create_order(
            OrderArgs(
                price=price,
                size=size_usd / price,
                side=BUY,
                token_id=market['clob_token_ids']['yes'],
            ),
        )
        # postOnly as separate parameter
        resp = await self.clob_client.post_order(order, OrderType.GTC, post_only=True)

        if resp.status == 'rejected_would_cross':
            continue  # retry with more aggressive price

        if resp.status == 'live':
            filled = await self._wait_for_fill(resp.order_id, retry_wait_sec)
            if filled and filled.fully_filled:
                return (filled.avg_price, filled.total_shares, filled.fee_paid)
            else:
                await self.clob_client.cancel_order(resp.order_id)
                continue

    # All attempts exhausted
    self.notifier.send_warning(f"Entry attempt exhausted for {market['id']}")
    return None
```

**Config additions:**

```yaml
executor:
  entry_strategy: "post_only_ladder"  # or "taker" (V3 fallback, discouraged)
  post_only_max_attempts: 3
  post_only_retry_wait_sec: 10
  allow_taker_fallback: false  # hard-off by default
  tick_size: 0.01  # verify against Polymarket; some markets use 0.001
```

**Exit order types (unchanged logic, explicit policy):**

Exits should remain as they are — stop-loss/teleportation use FOK market orders (taker, but necessary for urgency). Bond take-profit at $0.995 should ATTEMPT post-only first (cheap, not time-critical), fallback to taker if unfilled after 2 minutes.

**Tests:**
- `test_post_only_first_attempt_at_best_bid`
- `test_post_only_retries_on_rejection`
- `test_post_only_gives_up_after_max_attempts`
- `test_paper_mode_simulates_maker_fill`
- `test_no_taker_fallback_when_flag_off`
- `test_wait_for_fill_timeout_cancels_order`

**Acceptance criteria:**
- 24h shadow mode: ≥80% of attempted entries succeed as maker (remainder = rejected would-cross, which is OK — skip the trade)
- Fee drag on entries drops to ~0 in accounting
- No entries filled as taker while `allow_taker_fallback=false`

**Guardrail:** If `allow_taker_fallback=false` causes entry success rate to drop below 60% (most entries failing to fill as maker), operator may flip to true for one 24h period to diagnose whether the retry ladder prices are wrong.

---

### Change 1.4 — Dashboard & Observability Extensions

**Priority:** P0

**Files to modify:**
- `src/dashboard.py` — add pipeline health and fee attribution panels
- `src/notifications.py` — add daily pipeline health summary

**New dashboard panels:**

```
┌─── Pipeline Health (24h) ──────────────────┐
│ Scans: 288      Acceptance rate: 1.2%      │
│ Fetched: 43,200  Prefilter: 1,247 (2.9%)   │
│ Detector: 86 (0.20%) RiskEng: 12 (0.03%)   │
│ Entries: 4      Dry period: 2.3h           │
│                                             │
│ Top rejection reasons:                      │
│  1. price_out_of_band        41,953         │
│  2. liquidity_below_min         847         │
│  3. volatility_exceeded         231         │
│  4. min_net_yield_gate          118         │
│  5. volume_trend_declining       53         │
└────────────────────────────────────────────┘

┌─── Fee Attribution (lifetime) ─────────────┐
│ Position count: 26                          │
│ Gross revenue: $18.42                       │
│ Estimated fees (V3 model): $7.32  (39.7%)  │
│ Actual fees (V4 model):    $1.08  (5.9%)   │
│ Savings from maker execution: $6.24         │
└────────────────────────────────────────────┘
```

**Daily Discord summary additions:**

```
📊 Pipeline Health — 2026-04-16
• Acceptance rate: 1.2% (↑ from 0.8% yesterday)
• Top rejection: price_out_of_band (97%)
• Dry period peak: 8h
• Entries executed: 4 (target: 3–6)
• Fee savings from maker execution: $0.24 today
```

**Tests:**
- `test_dashboard_renders_pipeline_panel`
- `test_fee_attribution_accurate`
- `test_discord_summary_format`

---

## 3. Phase 2 — Candidate Universe Expansion (P1)

**Phase goal:** Increase the number of eligible trades per day from ~4 to 8–12 without sacrificing quality. The fee model changes in Phase 1 already enable this for fee-free categories; Phase 2 makes it systematic.

**Pre-condition:** Phase 1 must be complete and showing acceptance rate ≥0.3% for 7 consecutive days.

### Change 2.1 — Category-Specific Min Net Yield

**Priority:** P1

**Rationale:** The V3 1.5% flat min_net_yield is calibrated against the worst-case fee category (Crypto at 0.277% effective). For fee-free categories (Geopolitics) and low-fee categories (Sports at 0.115%), 1.5% leaves substantial opportunity on the table.

**Config changes:**

```yaml
risk:
  min_net_yield: 0.015  # global fallback, unchanged

  min_net_yield_by_category:
    Geopolitics: 0.010   # fee-free → can accept lower yield
    Politics: 0.012      # low fee
    Sports: 0.012
    Tech: 0.012
    Finance: 0.013
    Weather: 0.014
    Culture: 0.014
    Economics: 0.015
    Crypto: 0.017        # high fee → demand higher yield
    Mentions: 0.016
    _unknown: 0.015      # default
```

**Detector update:**

```python
# src/detector.py, in net-yield gate
category = market.get('category', '_unknown')
min_yield = config['risk']['min_net_yield_by_category'].get(
    category, config['risk']['min_net_yield']
)
if net_yield < min_yield:
    reject('min_net_yield_gate_by_category', {'category': category, 'min': min_yield, 'actual': net_yield})
```

**Safety:** The global `min_net_yield` remains as a floor. Category-specific overrides can be less permissive than the floor but not more permissive than `floor - 0.005`.

**Tests:**
- `test_geopolitics_accepts_lower_yield`
- `test_crypto_requires_higher_yield`
- `test_unknown_category_uses_global_floor`
- `test_override_cannot_go_below_hard_floor`

**Acceptance criteria:**
- Shadow mode for 7 days: Geopolitics and Politics acceptance rate rises by ≥50% vs V3
- Aggregate acceptance rate rises 20–80% (if >100%, review for over-relaxation)
- No net-negative EV entries added (verify via post-hoc EV attribution)

---

### Change 2.2 — Widen Entry Band for Fee-Free Categories

**Priority:** P1

**Rationale:** V3 tightened `max_entry_price` from 0.99 to 0.975 to avoid the $0.98-$0.99 band's poor R:R. This was correct under flat 0.4% fee assumption. Under corrected maker-entry fees, the top of the band is more viable for fee-free categories.

**Config changes:**

```yaml
scanner:
  min_entry_price: 0.94
  max_entry_price: 0.975       # global default

  entry_band_by_category:
    Geopolitics:
      min: 0.93                # slightly wider low bound — 0 fees subsidize risk
      max: 0.985
    Politics:
      min: 0.935
      max: 0.980
    # ... (default to global band for others)
```

**Scanner update:** Apply category-specific band during pre-filter, with per-category logging of in-band / out-of-band counts.

**Tests:**
- `test_geopolitics_accepts_0_935_entry`
- `test_crypto_rejects_entries_above_0_975`
- `test_band_override_logged_in_rejection_reasons`

**Guardrail:** Shadow mode for 7 days before activation. If Geopolitics WR on entries in the $0.93-$0.94 range is less than Politics WR on the same range, abort this change — it means the wider band is adding adverse selection, not alpha.

---

### Change 2.3 — Capture Holding Rewards in EV Model

**Priority:** P1

**Rationale:** Polymarket pays 4% APY on eligible positions. This is genuine additional return on held positions that the V3 EV model ignores.

**Scanner change:** Extract `holdingRewardsEnabled` (or equivalent field — verify actual Gamma schema) and `holdingRewardsApr` from each market. Store on position at entry.

**Utility:**

```python
# src/utils.py
def estimate_holding_rewards(position_value_usd: float,
                             days_held: float,
                             holding_rewards_apr: float = 0.04) -> float:
    """Expected USDC rewards for holding position to maturity.

    4% APY × days / 365 × position_value.
    """
    return position_value_usd * holding_rewards_apr * (days_held / 365)
```

**Bond score enhancement:**

```python
# src/utils.py: calculate_bond_score
# Add: if holding_rewards_enabled, boost score by holding reward yield
holding_reward_yield = (days_to_resolution / 365) * 0.04 if market.get('holding_rewards_enabled') else 0

adjusted_yield = (yield_pct + holding_reward_yield) * confidence_weight
```

**Positions table migration:**

```sql
ALTER TABLE positions ADD COLUMN holding_rewards_enabled INTEGER DEFAULT 0;
ALTER TABLE positions ADD COLUMN holding_rewards_apr REAL DEFAULT 0;
ALTER TABLE positions ADD COLUMN estimated_holding_rewards REAL DEFAULT 0;
ALTER TABLE positions ADD COLUMN actual_holding_rewards REAL;  -- reconciled daily
```

**Daily reconciliation:**

```python
# src/monitor.py or new src/rewards_reconciler.py
async def reconcile_holding_rewards():
    """Fetch actual rewards paid via Data API and update positions table."""
    # Data API: data-api.polymarket.com/rewards?address={funder}&date={yesterday}
    # Attribute payments to positions open during that day
    ...
```

**Tests:**
- `test_holding_rewards_estimate_matches_4pct_apy`
- `test_bond_score_boost_for_reward_eligible`
- `test_non_eligible_markets_unchanged_score`
- `test_reconciliation_populates_actual_rewards`

**Acceptance criteria:**
- Post-change bond scores for reward-eligible markets are 2-6% higher than non-eligible (within-category)
- Daily reconciliation populates `actual_holding_rewards` within 24h of payment
- Dashboard shows cumulative holding rewards earned

---

### Change 2.4 — Resolution-Date Clustering Check

**Priority:** P1

**Rationale:** Current risk bucket classifier groups markets by topic but misses same-catalyst correlation (e.g., Fed meeting day, election night). All markets resolving within a 24h window around a catalyst are correlated regardless of category.

**Files to modify:**
- `src/risk_engine.py` — add as check #11
- `src/portfolio_manager.py` — new method `get_resolution_date_exposure()`

**Logic:**

```python
# src/risk_engine.py
def _check_resolution_date_cluster(self, position_size_usd, market_resolution_time):
    """Reject if adding this position would make >25% of deployed capital
    resolve within any 24-hour window.
    """
    same_day_exposure = self.portfolio.get_resolution_date_exposure(
        market_resolution_time, window_hours=24
    )
    total_after = same_day_exposure + position_size_usd
    portfolio_value = self.portfolio.current_portfolio_value()

    if total_after / portfolio_value > 0.25:
        return CheckResult(
            passed=False,
            reason='resolution_date_cluster',
            detail=f'{total_after:.0f} would be in 24h window ({total_after/portfolio_value:.1%})',
        )
    return CheckResult(passed=True)
```

**Config:**

```yaml
risk:
  resolution_date_cluster_pct: 0.25  # max 25% resolving same day
```

**Tests:**
- `test_cluster_check_rejects_over_25pct`
- `test_cluster_check_windowed_correctly`
- `test_resolution_time_parsed_to_utc`

**Acceptance criteria:**
- Under synthetic "10 Fed markets same day" stress test, only ~3 get accepted (~25% × 12 slots × 4% sizing = ~12% per slot → ~3 positions max)
- No regression in normal conditions (no more than 5% of previously-accepted entries now rejected by this check)

---

### Change 2.5 — LP Rewards Market Preference

**Priority:** P1

**Rationale:** Markets with active LP rewards have deeper books and tighter spreads (they're attracting MMs), which reduces slippage risk on both entry and exit. Even without bot earning the LP reward itself (which requires two-sided quoting), these markets are higher-quality candidates.

**Scanner change:**

```python
# src/scanner.py: filter_candidates
# Extract rewards info from market object
rewards_enabled = market.get('rewardsEnabled', False)
rewards_max_spread = market.get('rewardsMaxSpread', None)
rewards_min_size = market.get('rewardsMinSize', None)
rewards_daily_rate = market.get('rewardsDailyRate', 0)

# Add to bond score
rewards_boost = 1.15 if rewards_enabled and rewards_daily_rate > 100 else 1.0
```

**Bond score update:**

```python
# src/utils.py: calculate_bond_score
bond_score = (adjusted_yield / time_factor) \
    * liquidity_weight * stability_weight \
    * catalyst_penalty * blacklist_penalty \
    * rewards_boost   # NEW: 1.0 or 1.15
```

**Tests:**
- `test_rewards_enabled_markets_score_higher`
- `test_small_reward_pools_no_boost`
- `test_score_ranking_prefers_reward_markets`

**Acceptance criteria:**
- Reward-enabled markets appear in top 10 of candidates at least 40% of the time
- No regression in actual win rate of top-ranked candidates

**Note:** This is a ranking boost, not a filter. Non-reward markets are still eligible.

---

## 4. Phase 3 — Execution Quality (P1)

**Phase goal:** Upgrade execution infrastructure to reduce latency and improve fill quality. Requires Phase 1 complete.

### Change 3.1 — WebSocket Price Feed for Open Positions

**Priority:** P1

**Rationale:** The 60s exit cycle is too slow for positions approaching resolution. A WebSocket subscription provides sub-second updates. Scope this narrowly: only WebSocket-monitor open positions, not the full candidate universe.

**Files to create:**
- `src/ws_price_monitor.py`

**Files to modify:**
- `main.py` — spawn WS monitor as separate asyncio task
- `src/exit_engine.py` — accept price updates from WS channel
- `src/orderbook_monitor.py` — optionally use WS for orderbook if WS orderbook channel available

**Module contract:**

```python
# src/ws_price_monitor.py
class WSPriceMonitor:
    def __init__(self, clob_url, token_ids: list[str], update_callback):
        self.url = clob_url
        self.token_ids = token_ids
        self.callback = update_callback
        self._ws = None

    async def run(self):
        """Long-lived WS connection. Auto-reconnects on drop.
        Calls self.callback(token_id, price, timestamp) on each update.
        """

    async def subscribe(self, token_id: str):
        """Add a token to the subscription."""

    async def unsubscribe(self, token_id: str):
        """Remove when position closes."""
```

**Integration:**

```python
# main.py
async def main_loop():
    ws_monitor = WSPriceMonitor(
        clob_url='wss://ws-subscriptions-clob.polymarket.com/ws/market',
        token_ids=[],
        update_callback=lambda tid, p, ts: exit_engine.on_price_update(tid, p, ts),
    )
    asyncio.create_task(ws_monitor.run())

    # When a position opens, subscribe
    async def on_position_open(position):
        await ws_monitor.subscribe(position.token_id)

    async def on_position_close(position):
        await ws_monitor.unsubscribe(position.token_id)
```

**Exit engine additions:**

```python
# src/exit_engine.py
def on_price_update(self, token_id, price, timestamp):
    """Fast-path check for critical exits on every WS tick.
    Only evaluates stop-loss and teleportation; other exit types
    still run on the 60s cycle.
    """
    position = self.positions_by_token.get(token_id)
    if not position: return

    # Ultra-fast teleportation check
    drop = (position.entry_price - price) / position.entry_price
    if drop > position.stop_loss_pct * 2.0:
        self._emergency_exit(position, 'teleportation_ws')
        return

    # Fast stop-loss check
    if price <= position.stop_loss_trigger:
        self._emergency_exit(position, 'stop_loss_ws')
```

**Config:**

```yaml
websocket:
  enabled: true
  url: "wss://ws-subscriptions-clob.polymarket.com/ws/market"
  reconnect_delay_sec: 5
  max_reconnect_attempts: 10
  fallback_to_polling_after_failures: 3
```

**Tests:**
- `test_ws_subscribes_on_position_open`
- `test_ws_unsubscribes_on_position_close`
- `test_ws_teleportation_faster_than_cycle`
- `test_ws_reconnects_on_drop`
- `test_fallback_to_polling_on_persistent_failure`

**Acceptance criteria:**
- Median latency from price move to exit decision: <1 second (vs 60s cycle)
- WS uptime in 24h: >99%
- No double-exit race conditions (WS exit + cycle exit on same position)

**Guardrail:** WS is purely additive for speed. The 60s cycle remains the source of truth. If WS is broken, bot still functions on cycle-based exits.

---

### Change 3.2 — Better Slippage Accounting & Prediction

**Priority:** P1

**Files to modify:**
- `src/executor.py` — record expected vs actual fill for every order
- `src/database.py` — add slippage tracking columns
- `src/risk_engine.py` — use historical slippage to tune volume cap

**Positions table migration:**

```sql
ALTER TABLE positions ADD COLUMN expected_entry_price REAL;
ALTER TABLE positions ADD COLUMN actual_entry_price REAL;
ALTER TABLE positions ADD COLUMN entry_slippage_pct REAL;
ALTER TABLE positions ADD COLUMN expected_exit_price REAL;
ALTER TABLE positions ADD COLUMN actual_exit_price REAL;
ALTER TABLE positions ADD COLUMN exit_slippage_pct REAL;
```

**Risk engine update:**

```python
# src/risk_engine.py: _check_slippage
# Use 30-day rolling median slippage for the position's category
historical_slippage = self.db.get_median_slippage(category=market['category'],
                                                    days=30)
# Predicted slippage = weighted average of orderbook estimate + historical
predicted = 0.7 * orderbook_estimate + 0.3 * historical_slippage

if predicted > config['risk']['max_slippage_pct']:
    return CheckResult(passed=False, reason='predicted_slippage_high', ...)
```

**Tests:**
- `test_slippage_recorded_on_fill`
- `test_historical_slippage_weighted_with_orderbook`
- `test_slippage_prediction_rejects_high_risk`

---

### Change 3.3 — Tick-Size Awareness

**Priority:** P1

**Rationale:** Polymarket markets have varying tick sizes (typically $0.01 but some use $0.001). The post-only retry ladder must use the correct tick size to avoid placing invalid orders.

**Scanner change:** Extract `minimumTickSize` from each market.

**Executor update:**

```python
# src/executor.py
def _compute_entry_price_for_attempt(self, market, attempt, target):
    tick = market.get('minimumTickSize', 0.01)
    best_bid = market['orderbook']['bids'][0]['price'] if market['orderbook']['bids'] else target - tick
    midpoint = (best_bid + market['orderbook']['asks'][0]['price']) / 2 if market['orderbook']['asks'] else target

    if attempt == 0:
        return best_bid
    elif attempt == 1:
        return round(best_bid + tick, 4)
    else:
        return round(midpoint - tick, 4)
```

**Tests:**
- `test_tick_size_001_uses_correct_increment`
- `test_tick_size_0001_uses_correct_increment`
- `test_invalid_tick_size_falls_back_to_001`

---

## 5. Phase 4 — New Signal Sources (P2)

**Phase goal:** Add optional advanced features that open new opportunities without changing core strategy. All Phase 4 items are lower priority; skip any that don't fit available engineering time.

### Change 4.1 — NegRisk Arbitrage Alert

**Priority:** P2

**Rationale:** On NegRisk-enabled events, YES+NO sum can't exceed $1.00 because of the merge capability. When YES+NO < $0.98, free arbitrage exists (buy both, merge to USDC). The bot's existing parity check just filters these — it should also ALERT on exploitable gaps.

**Files to create:**
- `src/arbitrage_detector.py`

**Files to modify:**
- `src/scanner.py` — pass through NegRisk flag

**Module contract:**

```python
# src/arbitrage_detector.py
class ArbitrageDetector:
    def check_negrisk_arb(self, market):
        """Return dict with arb details if exploitable, None otherwise.

        Requires:
        - market['negRiskEnabled'] = True
        - yes_price + no_price < 0.99 (1c threshold to cover fees+slippage)
        - Sufficient depth on both sides
        """
        if not market.get('negRiskEnabled'): return None

        yes_book = market['orderbook_yes']
        no_book = market['orderbook_no']

        best_yes_ask = yes_book['asks'][0] if yes_book['asks'] else None
        best_no_ask = no_book['asks'][0] if no_book['asks'] else None

        if not best_yes_ask or not best_no_ask: return None

        combined = best_yes_ask['price'] + best_no_ask['price']
        if combined >= 0.99: return None

        # Limit to minimum depth available on both sides
        max_shares = min(best_yes_ask['size'], best_no_ask['size'])
        gross_profit = (1.00 - combined) * max_shares
        # Account for fees on both legs (both are taker buys)
        fee_yes = calculate_taker_fee(best_yes_ask['price'], max_shares,
                                       market.get('feeSchedule'))
        fee_no = calculate_taker_fee(best_no_ask['price'], max_shares,
                                      market.get('feeSchedule'))
        net_profit = gross_profit - fee_yes - fee_no

        if net_profit < 0.50: return None  # $0.50 minimum to be worth attention

        return {
            'market_id': market['id'],
            'combined_price': combined,
            'max_shares': max_shares,
            'net_profit_usd': net_profit,
            'yes_ask': best_yes_ask,
            'no_ask': best_no_ask,
        }
```

**Integration:**

```python
# main.py or src/scanner.py, after market fetch
for market in markets:
    arb = arbitrage_detector.check_negrisk_arb(market)
    if arb:
        notifier.send_alert(
            f"NegRisk arb: {market['question'][:60]}\n"
            f"Combined: {arb['combined_price']:.4f}, "
            f"Size: {arb['max_shares']:.0f}, "
            f"Net: ${arb['net_profit_usd']:.2f}"
        )
        db.record_arb_opportunity(arb)
```

**Do NOT auto-execute.** The bot's current architecture isn't set up for atomic two-leg execution and merge. Alert only.

**Database:**

```sql
CREATE TABLE IF NOT EXISTS arb_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    combined_price REAL,
    max_shares REAL,
    net_profit_usd REAL,
    executed INTEGER DEFAULT 0,
    operator_notes TEXT
);
```

**Tests:**
- `test_negrisk_arb_detected_when_combined_under_99`
- `test_non_negrisk_markets_not_flagged`
- `test_arb_requires_min_profit_threshold`
- `test_arb_accounts_for_fees`

---

### Change 4.2 — Kalshi Cross-Venue Sanity Check

**Priority:** P2

**Rationale:** For events that trade on both Polymarket and Kalshi (elections, macro, sports), a meaningful price divergence is either arbitrage or a signal that something's wrong. Daily check helps detect the latter before you're the one caught.

**Files to create:**
- `src/kalshi_checker.py` (read-only; no Kalshi account needed if public API used)

**Logic:**

```python
# src/kalshi_checker.py
class KalshiChecker:
    async def compare_polymarket_to_kalshi(self, polymarket_market):
        """If equivalent Kalshi market exists, return price comparison.
        Returns dict or None.
        """
        # Match by keyword/event taxonomy — approximate, best-effort
        kalshi_match = await self._find_equivalent(polymarket_market)
        if not kalshi_match: return None

        return {
            'polymarket_price': polymarket_market['yes_price'],
            'kalshi_price': kalshi_match['yes_price'],
            'divergence_pct': abs(...) / ...,
            'warning': ... > 0.03,  # 3% divergence worth flagging
        }
```

**Daily sanity run:**

Once per day, for all open positions, check Kalshi equivalent. If divergence > 3%, alert operator.

**Tests:**
- `test_kalshi_matching_by_keyword`
- `test_divergence_alert_threshold`
- `test_no_kalshi_match_returns_none_gracefully`

**Note:** This is informational only. Do not filter or block trades based on it.

---

### Change 4.3 — Gamma Snapshot Archiver (Foundation for Replay)

**Priority:** P2

**Rationale:** Build the foundation for a historical replay harness. V4 starts collecting; V5 can build replay. Even partial data is valuable.

**Files to create:**
- `src/snapshot_archiver.py`
- `data/snapshots/` directory

**Behaviour:**

```python
# src/snapshot_archiver.py
class SnapshotArchiver:
    def __init__(self, archive_dir, retention_days=90):
        self.dir = archive_dir
        self.retention_days = retention_days

    async def snapshot_scan(self, markets_list):
        """Save the raw Gamma API response for this scan.

        File: data/snapshots/{YYYY-MM-DD}/{HH-MM-SS}.json.gz
        """

    async def snapshot_orderbook(self, token_id, orderbook):
        """Save orderbook snapshots when bot fetches them."""

    def prune_old_snapshots(self):
        """Delete snapshots older than retention_days."""
```

**Config:**

```yaml
snapshot_archiver:
  enabled: true
  retention_days: 90
  compress: true  # gzip
  max_daily_size_mb: 500  # alert if exceeded
```

**Why this matters:** Without historical snapshots, every strategy change requires weeks of paper trading to evaluate. With 3-6 months of archived data, V5 can backtest new filters in hours.

**Tests:**
- `test_snapshot_creates_file`
- `test_snapshot_compresses`
- `test_prune_removes_old_files`
- `test_retention_respects_setting`

---

## 6. Phase 5 — Statistical Rigor (P2)

### Change 5.1 — A/B Framework for Filter Changes

**Priority:** P2

**Rationale:** Going forward, every new filter should be testable in an A/B framework. Random-assignment isn't quite right (markets aren't independent), but you can use chronological-split or within-market-A/B.

**Design:** Tag each position with which filter-set version it came from. Compare realized PF, WR, avg_win, avg_loss across tags.

**Positions table migration:**

```sql
ALTER TABLE positions ADD COLUMN filter_version TEXT;
ALTER TABLE positions ADD COLUMN filter_variant TEXT;  -- 'control' or 'treatment'
```

**Utility:**

```python
# src/ab_framework.py
class ABFramework:
    def assign_variant(self, market_id, experiment_name) -> str:
        """Deterministic assignment based on hash(market_id + experiment_name).
        Returns 'control' or 'treatment'.
        """

    def record_outcome(self, position_id, experiment_name, variant, pnl):
        """Log outcome for later analysis."""

    def analyze(self, experiment_name, min_sample=30):
        """Compute metrics by variant, with confidence intervals if sample sufficient."""
```

---

### Change 5.2 — Bayesian Edge Estimation

**Priority:** P2

**Rationale:** Point-estimates of WR and PF from small samples are misleading. Use Beta-Binomial conjugate for WR, log-normal for win/loss magnitudes, to produce posterior distributions.

**Utility:**

```python
# src/statistics.py
class EdgeEstimator:
    def posterior_win_rate(self, wins: int, losses: int,
                           prior_alpha=1, prior_beta=1):
        """Beta(alpha+wins, beta+losses). Returns {mean, ci_low, ci_high}."""

    def posterior_profit_factor(self, wins_pnl: list, losses_pnl: list):
        """Bootstrap confidence interval for PF."""

    def kelly_fraction_posterior(self, n_samples=1000):
        """Monte Carlo: sample from WR and PF posteriors, compute Kelly per draw."""
```

**Dashboard:**

```
┌─── Edge Posterior (based on 26 trades) ─────────┐
│ Win rate:  mean=80% (95% CI: 65-91%)             │
│ Profit factor: mean=1.18 (95% CI: 0.3-2.4)       │
│ Implied Kelly: mean=0% (95% CI: -15% to +18%)    │
│ Probability of true edge > 0: 56%                │
│                                                   │
│ Trades needed to tighten CI to ±10pp: ~180       │
└──────────────────────────────────────────────────┘
```

This is far more honest than point estimates.

**Tests:**
- `test_posterior_wins_adds_to_prior`
- `test_bootstrap_pf_ci_wider_for_small_sample`
- `test_kelly_posterior_accounts_for_uncertainty`

---

## 7. Deferred / Explicitly NOT in V4

These are noted here so they don't get accidentally pulled in. Consider for V5+.

1. **Full two-sided market making for LP rewards.** Requires inventory management, NO-token positions (currently bot only trades YES), and meaningfully different architecture. V4 captures LP rewards only on the bid side where it naturally sits.

2. **Auto-execute NegRisk arbitrage.** Requires atomic two-leg execution and merge smart contract interaction. Alert only in V4.

3. **Polymarket US platform support.** Blocked by operator's Nigerian residency. Re-evaluate if platform expands geographically.

4. **Full WebSocket for all candidates.** V4 uses WS only for open positions. Polling still used for candidate discovery.

5. **Machine-learned entry filter.** The blacklist learner (V3) is already a crude version. A proper ML model needs more data than n=100-500 trades can provide. Revisit at n=1000+.

6. **Cross-market correlation beyond date clustering.** Requires correlation estimation, which requires significantly more data and engineering.

---

## 8. Implementation Order (for Claude Code)

Execute in this order. Do not skip ahead.

1. **Phase 1** (all of it, sequentially):
   - 1.1 Pipeline Health ← foundational, do first
   - 1.2 Dynamic Fee Model
   - 1.3 Post-Only Entries
   - 1.4 Dashboard extensions
2. **Validation window:** 7 days of paper trading with Phase 1 changes. Require acceptance rate ≥ V3 baseline. If not, investigate before proceeding.
3. **Phase 2** (can be parallelized):
   - 2.1 Category-specific min yield
   - 2.2 Category entry bands
   - 2.3 Holding rewards (depends on 1.2)
   - 2.4 Resolution-date clustering
   - 2.5 LP rewards preference
4. **Validation window:** 7 days. Verify ≥1.5× entry volume vs V3 baseline without negative WR drift.
5. **Phase 3** (execution quality):
   - 3.1 WebSocket (substantial engineering)
   - 3.2 Slippage accounting
   - 3.3 Tick-size awareness
6. **Validation window:** 3 days post-Phase-3; WS stability under load.
7. **Phase 4** (optional, pick what matters):
   - 4.1 NegRisk arb alerts
   - 4.2 Kalshi comparison
   - 4.3 Snapshot archiver (start ASAP even if Phase 5 deferred)
8. **Phase 5** (optional statistical rigor):
   - 5.1 A/B framework
   - 5.2 Bayesian estimator

---

## 9. Per-Change Checklist for Claude Code

For every change, before marking complete:

- [ ] Code implemented with type hints and docstrings
- [ ] Unit tests added (see per-change test lists)
- [ ] Integration tested against paper mode with real Gamma API (at least 1 full scan cycle)
- [ ] Config additions documented in `CONFIG.md` (create if doesn't exist)
- [ ] Migration tested on copy of production SQLite (non-destructive ALTER ADD)
- [ ] Feature flag added and defaults to OFF (then enabled via config file)
- [ ] Shadow mode verified for 24h before real activation (for filter changes)
- [ ] Rollback procedure documented (usually: flip feature flag off)
- [ ] Dashboard/notifications updated if relevant
- [ ] `IMPLEMENTATION.md` V4 section updated with the change

---

## 10. Success Metrics for V4

### Throughput metrics
- Daily entries: **8–12** (vs V3 baseline ~4)
- Acceptance rate: **>0.8%** over any 7-day window
- Dry period: **<12h** in 95% of 24h windows
- Zero starvation events in 30 days

### Economic metrics
- Average fee per trade: **<0.05%** of position value (via maker entries + free resolution)
- Captured holding rewards: **≥50%** of theoretical 4% APY on eligible positions
- LP rewards captured (opportunistic): **>$0** (any amount indicates mechanism works)

### Quality metrics
- Realized WR: **≥72%** (if drops below, investigate adverse selection in new categories)
- Realized PF: **≥1.2** over rolling 50-trade window
- Maximum single-trade loss: **≤2× largest win** (post-Trade-#26 style containment)
- Slippage on stop-loss exits: **<3%** median

### Operational metrics
- Pipeline health dashboard uptime: **>99%**
- WebSocket uptime (after Phase 3): **>99%**
- Critical alerts per week: **<3** (signal, not noise)
- Test suite pass rate: **100%**

---

## 11. What to Do If Something Breaks

**Scenario A: Acceptance rate drops to zero after a change**
1. Check pipeline_health dashboard for rejection reason
2. If reason is a new filter, flip that feature flag OFF
3. If reason is a fee calculation error, check that feeSchedule is being read correctly (not falling to fallback)
4. Open issue with full rejection-reason breakdown

**Scenario B: Entries fail as maker, no taker fallback**
1. Check if post-only retry ladder prices are correct
2. Check if tick_size is correctly extracted
3. Temporarily set `allow_taker_fallback: true` for 24h to collect data
4. Investigate why orders are crossing

**Scenario C: Unexpected large loss (Trade-#26 style)**
1. Do NOT immediately add reactive filters (don't repeat V3's over-fit to one data point)
2. Record the event comprehensively: what filters it passed, what the orderbook looked like, what the resolution was
3. Let the blacklist learner pick up the pattern organically
4. Only after 3 similar events should a new structural filter be added

**Scenario D: Holding rewards not being paid**
1. Check that bot's Polygon address is whitelisted (some markets may have eligibility requirements)
2. Check Polymarket Portfolio → Rewards UI manually
3. Reconciliation job queries Data API directly; verify API response format hasn't changed

**Scenario E: Pipeline health reports impossible numbers**
1. Scan timestamp drift — check server time
2. SQLite corruption — check DB integrity
3. Rate limiter throttling making some scans return empty

---

## 12. Notes to Operator

**On the pace of deployment:**
V3 was a reactive response to one tail event. V4 should be the opposite — structural improvements based on understanding the platform mechanics. Resist the urge to fold new features in based on single trade outcomes.

**On live deployment timing:**
V4 changes should all complete in paper mode first. The prior review recommended N≥200 before live deployment. V4 accelerates trade count (from ~4/day to ~10/day), so N=200 is reachable in ~3 weeks of Phase 2+ operation. That's still the earliest justifiable live deployment date, not a target.

**On the holding rewards finding:**
4% APY on eligible positions is a non-trivial enhancement that should be verified empirically as soon as the reconciliation job is live. If actual rewards match theoretical, this is "found money" worth ~$8-15/month at current $500 portfolio scale. If actual is much lower, there's an eligibility/visibility issue to debug.

**On fee model correctness:**
The single most important validation is: after Phase 1, pick 10 closed trades and manually reconcile the bot's recorded fee against Polymarket's Portfolio fee history. Any discrepancy >5% means the fee model is still wrong and needs more work.

**On the danger zone:**
If V4 deployment produces another Trade-#26-style event (single loss >3× largest win) within the first 50 trades, do NOT respond with reactive filters. Either:
1. The edge is real and this is natural variance — keep going, collect data
2. The edge is not real — stop paper trading, re-evaluate whether strategy should be abandoned

The wrong response is more V3-style whack-a-mole. That path leads to an overfit rule-set that works on paper and fails live.

---

*End of V4 Upgrade Specification*
*Estimated engineering time: 40-60 hours for Phases 1-3; +15-25 hours for Phases 4-5.*
