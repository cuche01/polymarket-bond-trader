"""Tests for V4 Phase 1.5 — loss-mitigation fixes:

- Widened tiered stop-loss
- Minimum-hold grace period (exit_engine)
- Per-underlying concentration cap (risk_engine)
- Per-underlying post-stop-out cooldown (risk_engine)
- classify_underlying() utility
"""

import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.exit_engine import ExitEngine
from src.risk_engine import RiskEngine
from src.utils import classify_underlying


# ─── classify_underlying ────────────────────────────────────────────────────

class TestClassifyUnderlying(unittest.TestCase):
    def test_bitcoin_variants(self):
        self.assertEqual(classify_underlying("Will the price of Bitcoin be above $70,000?"), "BTC")
        self.assertEqual(classify_underlying("BTC above $80k by Friday?"), "BTC")

    def test_ethereum_variants(self):
        self.assertEqual(classify_underlying("Will Ethereum close above $2,200?"), "ETH")
        self.assertEqual(classify_underlying("Will ETH hit $3k?"), "ETH")

    def test_solana(self):
        self.assertEqual(classify_underlying("Solana above $80 today?"), "SOL")

    def test_equity_ticker(self):
        self.assertEqual(classify_underlying("Will NVDA close above $160?"), "NVDA")
        self.assertEqual(classify_underlying("Will Microsoft finish week above $320?"), "MSFT")

    def test_oil(self):
        self.assertEqual(classify_underlying("Will WTI Crude Oil hit (LOW) $90?"), "WTI")

    def test_no_match_returns_none(self):
        self.assertIsNone(classify_underlying("Will Bruno Mars top Spotify monthly listeners?"))
        self.assertIsNone(classify_underlying(""))

    def test_case_insensitive(self):
        self.assertEqual(classify_underlying("bitcoin above 70k"), "BTC")


# ─── Grace period (exit_engine) ─────────────────────────────────────────────

def _make_exit_engine(grace_hours=2, tiered=None):
    if tiered is None:
        tiered = [
            {"min_entry": 0.96, "stop_loss_pct": 0.07},
            {"min_entry": 0.94, "stop_loss_pct": 0.06},
        ]
    config = {
        "exits": {
            "stop_loss_pct": 0.10,
            "entry_grace_period_hours": grace_hours,
            "tiered_stop_loss": tiered,
            "trailing_stop_activation_price": 0.995,
            "trailing_stop_distance_pct": 0.005,
            "max_holding_days": 14,
            "stale_resolution_hours": 48,
            "bond_take_profit_price": 0.995,
            "bond_take_profit_min_hours_to_resolution": 48,
            "generic_take_profit_pct": 0.10,
            "partial_scaling_enabled": False,
            "partial_close_trigger_pct": 0.08,
            "partial_close_pct": 0.50,
            "portfolio_drawdown_alert_pct": 0.05,
            "portfolio_drawdown_critical_pct": 0.08,
        },
        "alerts": {"yellow_loss_pct": 0.05, "orange_loss_pct": 0.08},
    }
    portfolio = MagicMock()
    return ExitEngine(config, portfolio, MagicMock(), notifier=None)


def _pos(entry_price=0.96, held_minutes=10, high_water_mark=None):
    entry_time = datetime.now(timezone.utc) - timedelta(minutes=held_minutes)
    return {
        "id": 1, "market_id": "m1", "market_question": "test",
        "entry_price": entry_price, "shares": 100.0,
        "entry_time": entry_time.isoformat(),
        "high_water_mark": high_water_mark,
    }


class TestGracePeriod(unittest.TestCase):
    def test_stop_loss_suppressed_inside_grace(self):
        engine = _make_exit_engine(grace_hours=2)
        # Entry 0.96, current 0.88 → 8.3% drop (would trigger 7% stop), but 10 min old
        pos = _pos(entry_price=0.96, held_minutes=10)
        decision = engine.check_stop_loss(pos, current_price=0.88)
        self.assertIsNone(decision)

    def test_stop_loss_fires_after_grace_elapses(self):
        engine = _make_exit_engine(grace_hours=2)
        pos = _pos(entry_price=0.96, held_minutes=181)  # past 2h + 1min
        decision = engine.check_stop_loss(pos, current_price=0.88)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "stop_loss")

    def test_trailing_stop_suppressed_inside_grace(self):
        engine = _make_exit_engine(grace_hours=2)
        # Even with high-water mark set and current < trail, grace defers it.
        pos = _pos(entry_price=0.96, held_minutes=30, high_water_mark=0.999)
        decision = engine.check_trailing_stop(pos, current_price=0.988)
        self.assertIsNone(decision)

    def test_trailing_stop_fires_after_grace(self):
        engine = _make_exit_engine(grace_hours=2)
        pos = _pos(entry_price=0.96, held_minutes=180, high_water_mark=0.999)
        decision = engine.check_trailing_stop(pos, current_price=0.988)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "trailing_stop")

    def test_zero_grace_disables_suppression(self):
        engine = _make_exit_engine(grace_hours=0)
        pos = _pos(entry_price=0.96, held_minutes=1)
        decision = engine.check_stop_loss(pos, current_price=0.88)
        self.assertIsNotNone(decision)

    def test_teleportation_still_fires_in_grace(self):
        """Grace period must NOT block teleportation (emergency gap-down)."""
        engine = _make_exit_engine(grace_hours=2)
        pos = _pos(entry_price=0.96, held_minutes=5)
        # entry 0.96, tier stop 7% → teleport trigger is > 14% drop
        # current 0.75 → 21.9% drop, well past teleport threshold
        decision = engine.check_teleportation(pos, current_price=0.75)
        self.assertIsNotNone(decision)
        self.assertIn("teleportation", decision.reason)


# ─── Per-underlying concentration cap ───────────────────────────────────────

def _make_risk_engine(
    balance=1000.0,
    max_per_underlying=2,
    cooldown_hours=6,
    open_positions=None,
):
    config = {
        "risk": {
            "max_single_market_pct": 0.04,
            "target_position_pct": 0.03,
            "max_correlated_pct": 0.15,
            "max_category_exposure_pct": 0.20,
            "max_deployed_pct": 0.70,
            "max_daily_loss_pct": 0.02,
            "consecutive_loss_halt": 3,
            "max_slippage_pct": 0.02,
            "volume_size_max_pct": 0.02,
            "min_viable_position": 15.0,
            "max_positions_per_underlying": max_per_underlying,
            "underlying_cooldown_hours": cooldown_hours,
        }
    }
    portfolio = MagicMock()
    portfolio.get_portfolio_balance.return_value = balance
    portfolio.db = MagicMock()
    portfolio.db.get_open_positions.return_value = open_positions or []
    return RiskEngine(config, portfolio)


class TestUnderlyingConcentration(unittest.TestCase):
    def test_allows_entry_when_under_cap(self):
        open_positions = [
            {"market_question": "Will Bitcoin be above $70k on April 20?"},
        ]
        engine = _make_risk_engine(max_per_underlying=2, open_positions=open_positions)
        ok, reason = engine.check_underlying_exposure(
            "Will Bitcoin be above $72k on April 21?"
        )
        self.assertTrue(ok, f"Should allow 2nd BTC position, got: {reason}")

    def test_rejects_entry_when_at_cap(self):
        open_positions = [
            {"market_question": "Bitcoin above $70k"},
            {"market_question": "Bitcoin above $72k"},
        ]
        engine = _make_risk_engine(max_per_underlying=2, open_positions=open_positions)
        ok, reason = engine.check_underlying_exposure("Will Bitcoin hit $74k?")
        self.assertFalse(ok)
        self.assertIn("BTC", reason)
        self.assertIn("2 open", reason)

    def test_different_underlyings_dont_interfere(self):
        open_positions = [
            {"market_question": "Bitcoin above $70k"},
            {"market_question": "Bitcoin above $72k"},
        ]
        engine = _make_risk_engine(max_per_underlying=2, open_positions=open_positions)
        ok, reason = engine.check_underlying_exposure(
            "Will Ethereum be above $2,200?"
        )
        self.assertTrue(ok)

    def test_unknown_underlying_bypasses_check(self):
        """Markets we can't classify shouldn't be blocked by this check."""
        open_positions = [{"market_question": "X"}, {"market_question": "Y"}]
        engine = _make_risk_engine(max_per_underlying=2, open_positions=open_positions)
        ok, _ = engine.check_underlying_exposure("Some obscure question about foo")
        self.assertTrue(ok)

    def test_cap_disabled_when_zero(self):
        open_positions = [
            {"market_question": "Bitcoin above $70k"},
            {"market_question": "Bitcoin above $72k"},
            {"market_question": "Bitcoin above $74k"},
        ]
        engine = _make_risk_engine(max_per_underlying=0, open_positions=open_positions)
        ok, _ = engine.check_underlying_exposure("Will Bitcoin hit $76k?")
        self.assertTrue(ok)


# ─── Per-underlying cooldown ────────────────────────────────────────────────

class TestUnderlyingCooldown(unittest.TestCase):
    def test_no_cooldown_when_never_stopped_out(self):
        engine = _make_risk_engine(cooldown_hours=6)
        ok, _ = engine.check_underlying_cooldown("Will Bitcoin hit $70k?")
        self.assertTrue(ok)

    def test_rejects_entry_during_cooldown(self):
        engine = _make_risk_engine(cooldown_hours=6)
        engine.register_underlying_stopout("Will Bitcoin hit $70k?")
        ok, reason = engine.check_underlying_cooldown("Will Bitcoin hit $72k?")
        self.assertFalse(ok)
        self.assertIn("BTC", reason)
        self.assertIn("cooldown", reason.lower())

    def test_allows_entry_after_cooldown_elapses(self):
        engine = _make_risk_engine(cooldown_hours=6)
        engine.register_underlying_stopout("Will Bitcoin hit $70k?")
        # Rewind the tracked timestamp to simulate 7h elapsed
        engine._underlying_stopout_at["BTC"] = time.time() - 7 * 3600
        ok, _ = engine.check_underlying_cooldown("Will Bitcoin hit $72k?")
        self.assertTrue(ok)
        # Should have been cleared after expiry
        self.assertNotIn("BTC", engine._underlying_stopout_at)

    def test_cooldown_per_underlying(self):
        """Stop-out on BTC shouldn't block ETH entries."""
        engine = _make_risk_engine(cooldown_hours=6)
        engine.register_underlying_stopout("Will Bitcoin hit $70k?")
        ok, _ = engine.check_underlying_cooldown("Will Ethereum hit $2,200?")
        self.assertTrue(ok)

    def test_register_ignores_unknown_underlying(self):
        engine = _make_risk_engine(cooldown_hours=6)
        engine.register_underlying_stopout("Will Bruno Mars top Spotify?")
        self.assertEqual(engine._underlying_stopout_at, {})

    def test_cooldown_disabled_when_zero(self):
        engine = _make_risk_engine(cooldown_hours=0)
        engine.register_underlying_stopout("Will Bitcoin hit $70k?")
        self.assertEqual(engine._underlying_stopout_at, {})
        ok, _ = engine.check_underlying_cooldown("Will Bitcoin hit $72k?")
        self.assertTrue(ok)


# ─── Widened tier verification ──────────────────────────────────────────────

class TestTieredStopLoss(unittest.TestCase):
    def test_0_96_entry_tolerates_up_to_7pct(self):
        engine = _make_exit_engine(
            grace_hours=0,
            tiered=[
                {"min_entry": 0.96, "stop_loss_pct": 0.07},
                {"min_entry": 0.94, "stop_loss_pct": 0.06},
            ],
        )
        pos = _pos(entry_price=0.96, held_minutes=10_000)  # past grace
        # 5% drop below entry: should NOT stop (was stopping at 4% under old tiers)
        self.assertIsNone(engine.check_stop_loss(pos, current_price=0.912))
        # 7.5% drop: should stop
        decision = engine.check_stop_loss(pos, current_price=0.888)
        self.assertIsNotNone(decision)

    def test_0_98_entry_tolerates_up_to_8pct(self):
        engine = _make_exit_engine(
            grace_hours=0,
            tiered=[{"min_entry": 0.98, "stop_loss_pct": 0.08}],
        )
        pos = _pos(entry_price=0.98, held_minutes=10_000)
        # 6% drop: should NOT stop
        self.assertIsNone(engine.check_stop_loss(pos, current_price=0.9212))
        # 9% drop: should stop
        self.assertIsNotNone(engine.check_stop_loss(pos, current_price=0.8918))


# ─── Sports prefilter (scanner) ─────────────────────────────────────────────

def _base_market(**overrides):
    m = {
        "active": True,
        "closed": False,
        "question": "Will BTC close above $70k?",
        "outcomePrices": '["0.96", "0.04"]',
        "endDate": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        "liquidityClob": 50_000,
        "volume24hr": 20_000,
        "volume1wk": 150_000,
        "oneDayPriceChange": 0.0,
        "category": "Crypto",
        "clobTokenIds": '["111","222"]',
    }
    m.update(overrides)
    return m


def _make_scanner():
    from src.scanner import MarketScanner

    return MarketScanner({
        "scanner": {
            "min_entry_price": 0.94,
            "max_entry_price": 0.975,
            "max_days_to_resolution": 14,
            "min_liquidity": 10_000,
            "min_volume_24h": 5_000,
            "max_price_volatility_1d": 0.05,
            "excluded_categories": ["Sports", "E-Sports"],
        },
        "feature_flags": {
            "time_of_day_filter": False,
            "volume_trend_filter": False,
        },
        "fees": {},
    })


class TestSportsPrefilter(unittest.TestCase):
    def test_rejects_fee_type_sports(self):
        scanner = _make_scanner()
        market = _base_market(
            question="NBA Playoffs: Spurs vs Trail Blazers",
            feeType="sports_fees_v2",
            category="",
        )
        self.assertEqual(scanner.filter_candidates([market]), [])

    def test_rejects_sports_market_type(self):
        scanner = _make_scanner()
        market = _base_market(
            question="Madrid Open: Player A vs Player B",
            sportsMarketType="moneyline",
            category="",
        )
        self.assertEqual(scanner.filter_candidates([market]), [])

    def test_rejects_child_moneyline_esports(self):
        scanner = _make_scanner()
        market = _base_market(
            question="Dota 2: Team A vs Team B",
            sportsMarketType="child_moneyline",
            category="",
        )
        self.assertEqual(scanner.filter_candidates([market]), [])

    def test_passes_non_sports_market(self):
        scanner = _make_scanner()
        market = _base_market()  # plain crypto bond, no sports tags
        result = scanner.filter_candidates([market])
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
