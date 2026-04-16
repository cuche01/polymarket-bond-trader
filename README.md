# Polymarket Bond Strategy Bot

Automated trading bot that identifies and trades "bond-like" positions on Polymarket — high-probability YES markets priced at $0.94–$0.975 that pay $1.00 on YES resolution. The strategy captures the small residual spread across many short-duration positions, relying on high win rates and tight loss control rather than large per-trade gains.

**Modes:** Paper (simulated, no capital) and Live (real CLOB orders on Polygon).

## Features

- **8-layer entry validation** — category/blacklist filter, price behavior, orderbook health, resolution source, binary catalyst classification, YES+NO parity, price-drop cooldown, keyword blacklist
- **10-check risk engine** — category exposure, event-group correlation, risk-bucket clustering, deployment limits, daily loss / consecutive loss circuit breakers, slippage and volume caps
- **9 exit strategies** — tiered stop-loss (inverted: tighter stops on lower-entry riskier bonds), trailing stop, time exit, dynamic bond take-profit, teleportation gap-down protection, orderbook imbalance exit, portfolio drawdown triggers, post-entry re-validation
- **Adaptive position sizing** — confidence × theta weighting, bucket confidence scaling, Kelly-calibrated targets
- **Fluke filter** — auto-pauses a risk bucket for 24h when a loss exceeds 3× trailing average
- **Discord notifications** — trade alerts, hourly snapshots, daily reports, lifetime performance summary
- **Feature flags** — every advanced feature is individually toggleable in `config.yaml`

Full architecture, exit logic, and risk-model details are in [`IMPLEMENTATION.md`](IMPLEMENTATION.md).

## Requirements

- Python 3.11+
- Polygon wallet (for live mode)
- Discord webhook (optional, for notifications)

## Setup

```bash
# Clone and enter the repo
cd polymarket-bond-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy env template and fill in your values
cp .env.example .env
# Edit .env: PRIVATE_KEY, PROXY_WALLET_ADDR, WEBHOOK_URL
```

## Running

### Paper mode (recommended for testing)

```bash
source venv/bin/activate
python main.py --paper

# Background / persistent
nohup python main.py --paper > logs/paper_bot.log 2>&1 &
echo $! > logs/bot.pid
```

### Live mode

```bash
source venv/bin/activate
python main.py
```

Live mode requires `PRIVATE_KEY` and `PROXY_WALLET_ADDR` in `.env`.

### Stop the bot

```bash
# Graceful
kill $(cat logs/bot.pid)

# Emergency halt (bot checks each cycle)
touch HALT
```

## Configuration

All tunables live in `config.yaml`:

- **scanner** — entry price band, liquidity/volume minimums, excluded categories
- **risk** — position sizing, exposure caps, circuit breakers
- **exits** — tiered stop-loss, take-profit, trailing stop, portfolio drawdown
- **risk_buckets** — per-bucket exposure caps
- **feature_flags** — toggle advanced features without code changes

See [`IMPLEMENTATION.md`](IMPLEMENTATION.md) § Configuration Reference for every key and default.

## Testing

```bash
source venv/bin/activate
python -m pytest tests/ -v
```

The suite covers all modules and every feature-flagged enhancement.

## Project Layout

```
main.py                   BondBot orchestrator + event loop
config.yaml               All tunable parameters
src/
  scanner.py              Gamma API fetch + pre-filter
  detector.py             8-layer entry validation
  risk_engine.py          10-check portfolio gate + adaptive sizing
  executor.py             Order placement / fill monitoring
  exit_engine.py          Active exit management
  orderbook_monitor.py    Live-mode bid depth surveillance
  blacklist_learner.py    Loss-feature penalty scoring
  portfolio_manager.py    Exposure, drawdown, P&L queries
  risk_buckets.py         Correlation clustering classifier
  database.py             SQLite persistence
  notifications.py        Discord webhooks
  utils.py                Scoring, rate limiting, config helpers
tests/                    Unit + integration tests
data/                     SQLite DB, blacklist, caution list (gitignored)
logs/                     Runtime logs (gitignored)
reports/                  Analysis HTML reports (gitignored)
```

## Safety Notes

- **Start in paper mode.** Validate strategy performance before committing capital.
- **Never commit `.env`** — it contains your wallet private key. `.gitignore` excludes it by default.
- **Back up `data/bond_bot.db`** before major config changes or upgrades.
- **Monitor Discord alerts.** Teleportation and orderbook-exit signals require prompt attention in live mode.
- This bot places real orders in live mode. You are solely responsible for any financial loss.

## License

Private / unlicensed. Not for redistribution.
