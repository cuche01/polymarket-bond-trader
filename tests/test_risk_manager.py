"""
Tests for the RiskManager module.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.risk_manager import RiskManager


def make_test_config(**overrides) -> dict:
    """Create a test configuration."""
    config = {
        "risk": {
            "max_single_market_pct": 0.10,
            "max_correlated_pct": 0.15,
            "max_deployed_pct": 0.70,
            "max_absolute_position": 5000.0,
            "max_daily_loss_pct": 0.02,
            "consecutive_loss_halt": 3,
            "min_net_yield": 0.01,
        },
        "scanner": {
            "min_entry_price": 0.95,
            "max_entry_price": 0.99,
        },
    }
    for key, val in overrides.items():
        if isinstance(val, dict) and key in config:
            config[key].update(val)
        else:
            config[key] = val
    return config


def make_future_date(days: int = 3) -> str:
    """Return ISO date string `days` in the future."""
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.isoformat()


def make_market(days_to_resolution: float = 3.0, yes_price: float = 0.97) -> dict:
    """Create a minimal market dictionary."""
    return {
        "id": "market-001",
        "endDate": make_future_date(int(days_to_resolution)),
        "_yes_price": yes_price,
        "_days_to_resolution": days_to_resolution,
        "_liquidity": 50000,
        "eventId": "event-001",
    }


def make_position(cost_basis: float = 1000.0, event_id: str = "event-001") -> dict:
    """Create a minimal position dictionary."""
    return {
        "id": 1,
        "market_id": "market-001",
        "cost_basis": cost_basis,
        "entry_price": 0.97,
        "shares": cost_basis / 0.97,
        "status": "open",
        "event_id": event_id,
    }


class TestMaxSingleMarketPct(unittest.TestCase):
    """Tests for max single market percentage enforcement."""

    def setUp(self):
        self.config = make_test_config()
        self.rm = RiskManager(self.config)

    def test_position_capped_at_max_single_pct(self):
        """Position size should not exceed max_single_market_pct of balance."""
        portfolio_balance = 10000.0
        # 10% of $10000 = $1000 max
        # With time factor for 3 days (0.6), max raw = $1000, after time = $600
        market = make_market(days_to_resolution=3.0)
        size = self.rm.calculate_position_size(
            market, portfolio_balance, [], available_liquidity=100000
        )
        # Size should not exceed $1000 (10% of $10000)
        self.assertLessEqual(size, portfolio_balance * self.rm.max_single_market_pct + 0.01)

    def test_position_respects_absolute_max(self):
        """Position size should not exceed MAX_ABSOLUTE_POSITION."""
        portfolio_balance = 100000.0  # Very large balance
        market = make_market(days_to_resolution=1.0)  # Short duration = high time factor
        size = self.rm.calculate_position_size(
            market, portfolio_balance, [], available_liquidity=1000000
        )
        self.assertLessEqual(size, self.rm.max_absolute_position + 0.01)

    def test_zero_balance_returns_zero(self):
        """Zero portfolio balance should return zero position size."""
        market = make_market()
        size = self.rm.calculate_position_size(market, 0.0, [], available_liquidity=50000)
        self.assertEqual(size, 0.0)

    def test_liquidity_cap_applied(self):
        """Position size should be capped at 10% of available bid liquidity."""
        portfolio_balance = 50000.0
        # 10% of $500 liquidity = $50
        market = make_market()
        size = self.rm.calculate_position_size(
            market, portfolio_balance, [], available_liquidity=500.0
        )
        # Should be capped by liquidity: 10% of $500 = $50 before time factor
        self.assertLessEqual(size, 50.0 + 0.01)


class TestMaxDeployedPct(unittest.TestCase):
    """Tests for max deployed percentage enforcement."""

    def setUp(self):
        self.config = make_test_config()
        self.rm = RiskManager(self.config)

    def test_blocks_new_position_when_over_deployed(self):
        """Should block new positions when portfolio would exceed max_deployed_pct."""
        portfolio_balance = 10000.0
        # Already deployed $6900 (69% of $10000)
        existing = [make_position(cost_basis=6900.0)]

        # Adding $200 more would bring to 71% (> 70% max)
        allowed, reason = self.rm.check_portfolio_limits(
            existing, 200.0, portfolio_balance
        )
        self.assertFalse(allowed)
        self.assertIn("deployed", reason)

    def test_allows_position_within_deployed_limit(self):
        """Should allow new positions when within max_deployed_pct."""
        portfolio_balance = 10000.0
        existing = [make_position(cost_basis=5000.0)]  # 50% deployed

        # Adding $500 brings to 55% (< 70% max)
        allowed, reason = self.rm.check_portfolio_limits(
            existing, 500.0, portfolio_balance
        )
        self.assertTrue(allowed, f"Expected allowed but got: {reason}")

    def test_empty_positions_always_allowed(self):
        """Should allow positions when no existing positions."""
        portfolio_balance = 10000.0
        allowed, reason = self.rm.check_portfolio_limits([], 1000.0, portfolio_balance)
        self.assertTrue(allowed)

    def test_correlated_exposure_limit(self):
        """Should limit correlated positions from same event."""
        portfolio_balance = 10000.0
        # Already deployed $1400 in event-001 (14% of $10000)
        existing = [make_position(cost_basis=1400.0, event_id="event-001")]

        new_market = {"eventId": "event-001", "_yes_price": 0.97}
        # Adding $200 would bring event-001 exposure to 16% (> 15% max)
        allowed, reason = self.rm.check_portfolio_limits(
            existing, 200.0, portfolio_balance, new_market
        )
        self.assertFalse(allowed)
        self.assertIn("correlated", reason.lower())


class TestDailyLossLimit(unittest.TestCase):
    """Tests for daily loss limit halting."""

    def setUp(self):
        self.config = make_test_config()
        self.rm = RiskManager(self.config)

    def test_daily_loss_below_limit_allows_trading(self):
        """Trading should continue when daily loss is below limit."""
        portfolio_balance = 10000.0
        daily_pnl = -150.0  # 1.5% loss, below 2% limit
        should_halt = self.rm.check_daily_loss_limit(daily_pnl, portfolio_balance)
        self.assertFalse(should_halt)

    def test_daily_loss_at_limit_halts_trading(self):
        """Trading should halt when daily loss reaches the limit."""
        portfolio_balance = 10000.0
        daily_pnl = -200.0  # Exactly 2% loss
        should_halt = self.rm.check_daily_loss_limit(daily_pnl, portfolio_balance)
        self.assertTrue(should_halt)

    def test_daily_loss_above_limit_halts_trading(self):
        """Trading should halt when daily loss exceeds the limit."""
        portfolio_balance = 10000.0
        daily_pnl = -500.0  # 5% loss, well above 2% limit
        should_halt = self.rm.check_daily_loss_limit(daily_pnl, portfolio_balance)
        self.assertTrue(should_halt)

    def test_positive_pnl_never_halts(self):
        """Positive P&L should never trigger the halt."""
        portfolio_balance = 10000.0
        daily_pnl = 300.0  # Profit
        should_halt = self.rm.check_daily_loss_limit(daily_pnl, portfolio_balance)
        self.assertFalse(should_halt)

    def test_zero_balance_triggers_halt(self):
        """Zero balance should trigger halt to prevent division by zero issues."""
        should_halt = self.rm.check_daily_loss_limit(-100.0, 0.0)
        self.assertTrue(should_halt)


class TestConsecutiveLossCircuitBreaker(unittest.TestCase):
    """Tests for consecutive loss circuit breaker."""

    def setUp(self):
        self.config = make_test_config()
        self.rm = RiskManager(self.config)

    def _make_closed_position(self, pnl: float) -> dict:
        """Create a closed position with given P&L."""
        return {"status": "closed", "pnl": pnl, "cost_basis": 1000.0}

    def test_no_positions_no_halt(self):
        """Empty position list should not trigger circuit breaker."""
        self.assertFalse(self.rm.check_consecutive_losses([]))

    def test_three_consecutive_losses_triggers_halt(self):
        """Three consecutive losses should trigger the circuit breaker."""
        positions = [
            self._make_closed_position(-50),   # Most recent
            self._make_closed_position(-30),
            self._make_closed_position(-20),
        ]
        self.assertTrue(self.rm.check_consecutive_losses(positions))

    def test_two_consecutive_losses_no_halt(self):
        """Two consecutive losses should NOT trigger (limit is 3)."""
        positions = [
            self._make_closed_position(-50),   # Most recent
            self._make_closed_position(-30),
        ]
        self.assertFalse(self.rm.check_consecutive_losses(positions))

    def test_win_breaks_consecutive_streak(self):
        """A win in the middle should break the loss streak."""
        positions = [
            self._make_closed_position(-50),   # Most recent: loss
            self._make_closed_position(-30),   # Loss
            self._make_closed_position(100),   # Win - breaks streak
            self._make_closed_position(-20),   # Old loss (doesn't count)
        ]
        self.assertFalse(self.rm.check_consecutive_losses(positions))

    def test_four_consecutive_losses_triggers_halt(self):
        """More than 3 consecutive losses should also trigger halt."""
        positions = [
            self._make_closed_position(-50),
            self._make_closed_position(-30),
            self._make_closed_position(-20),
            self._make_closed_position(-10),
        ]
        self.assertTrue(self.rm.check_consecutive_losses(positions))


class TestTimeFactor(unittest.TestCase):
    """Tests for time factor calculation."""

    def setUp(self):
        self.config = make_test_config()
        self.rm = RiskManager(self.config)

    def test_6_hours_returns_1_0(self):
        """6 hours to resolution should return time factor 1.0."""
        end_date = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        self.assertAlmostEqual(self.rm.get_time_factor(end_date), 1.0, places=1)

    def test_24_hours_returns_0_8(self):
        """24 hours to resolution should return time factor 0.8."""
        end_date = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        self.assertAlmostEqual(self.rm.get_time_factor(end_date), 0.8, places=1)

    def test_72_hours_returns_0_6(self):
        """72 hours (3 days) to resolution should return time factor 0.6."""
        end_date = (datetime.now(timezone.utc) + timedelta(hours=60)).isoformat()
        self.assertAlmostEqual(self.rm.get_time_factor(end_date), 0.6, places=1)

    def test_7_days_returns_0_4(self):
        """7 days to resolution should return time factor 0.4."""
        end_date = (datetime.now(timezone.utc) + timedelta(days=6)).isoformat()
        self.assertAlmostEqual(self.rm.get_time_factor(end_date), 0.4, places=1)

    def test_14_days_returns_0_2(self):
        """14 days to resolution should return time factor 0.2."""
        end_date = (datetime.now(timezone.utc) + timedelta(days=12)).isoformat()
        self.assertAlmostEqual(self.rm.get_time_factor(end_date), 0.2, places=1)

    def test_past_date_returns_zero(self):
        """Past end date should return 0 time factor."""
        end_date = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assertEqual(self.rm.get_time_factor(end_date), 0.0)


class TestPositionSizeFormula(unittest.TestCase):
    """Tests for position size formula edge cases."""

    def setUp(self):
        self.config = make_test_config()
        self.rm = RiskManager(self.config)

    def test_position_size_formula_basic(self):
        """Position size should follow the spec formula."""
        portfolio_balance = 10000.0
        market = make_market(days_to_resolution=3.0, yes_price=0.97)
        available_liquidity = 50000.0

        size = self.rm.calculate_position_size(
            market, portfolio_balance, [], available_liquidity
        )
        # min(10%*10000, 10%*50000, 5000) * time_factor(3d=0.6)
        # = min(1000, 5000, 5000) * 0.6 = 600
        self.assertAlmostEqual(size, 600.0, delta=5.0)

    def test_max_deployed_caps_available(self):
        """Should cap size to remaining available balance."""
        portfolio_balance = 10000.0
        # Already 65% deployed
        existing = [make_position(cost_basis=6500.0)]
        market = make_market(days_to_resolution=1.0)

        size = self.rm.calculate_position_size(
            market, portfolio_balance, existing, available_liquidity=100000
        )
        # Available balance = $10000 - $6500 = $3500
        self.assertLessEqual(size, 3500.0 + 1.0)
        self.assertGreater(size, 0.0)

    def test_very_low_liquidity_shrinks_position(self):
        """Very low liquidity should dramatically shrink position size."""
        portfolio_balance = 50000.0
        market = make_market()
        # Only $200 available liquidity → 10% = $20
        size = self.rm.calculate_position_size(
            market, portfolio_balance, [], available_liquidity=200.0
        )
        self.assertLessEqual(size, 20.0 + 1.0)


if __name__ == "__main__":
    unittest.main()
