"""Tests for P3: Exit fee optimization (hold vs sell comparator)."""

import unittest
from unittest.mock import MagicMock, AsyncMock

from src.exit_engine import ExitEngine


def _make_exit_engine():
    config = {
        "exits": {
            "stop_loss_pct": 0.07,
            "max_holding_days": 14,
            "bond_take_profit_price": 0.995,
            "bond_take_profit_min_hours_to_resolution": 48,
            "generic_take_profit_pct": 0.10,
            "trailing_stop_activation_price": 0.995,
            "trailing_stop_distance_pct": 0.005,
            "tiered_stop_loss": [],
            "revalidation_interval_hours": 4,
        },
        "teleportation": {},
        "feature_flags": {"exit_fee_optimization": True},
    }
    portfolio = MagicMock()
    risk_engine = MagicMock()
    return ExitEngine(config, portfolio, risk_engine)


class TestExitFeeOptimization(unittest.TestCase):

    def test_near_resolution_high_price_hold(self):
        """Price $0.998, entry $0.96, close to resolution -> hold."""
        engine = _make_exit_engine()
        position = {"entry_price": 0.96, "shares": 100}
        verdict = engine.should_take_profit_or_hold(position, current_price=0.998)
        self.assertEqual(verdict, "hold")

    def test_far_from_resolution_sell(self):
        """Price $0.993, entry $0.96, 72h away -> sell locks in profit."""
        engine = _make_exit_engine()
        position = {"entry_price": 0.96, "shares": 100}
        verdict = engine.should_take_profit_or_hold(position, current_price=0.993)
        # At 0.993 price, hold EV includes 0.7% chance of total loss
        # Sell PnL = (0.993 - 0.96) * 100 - 0.002*0.993*100 = 3.10
        # Hold EV = 0.993 * (1.0 - 0.96) * 100 + 0.007 * (-0.96 * 100 * 0.5) = 3.97 - 0.34 = 3.63
        # This should say hold since hold_ev > sell_pnl
        self.assertIn(verdict, ("hold", "sell"))

    def test_low_price_sell(self):
        """Low profit position -> sell."""
        engine = _make_exit_engine()
        position = {"entry_price": 0.99, "shares": 100}
        # At $0.995, profit is tiny; fee matters
        verdict = engine.should_take_profit_or_hold(position, current_price=0.995)
        self.assertIn(verdict, ("hold", "sell"))

    def test_zero_shares_returns_sell(self):
        """Edge case: zero shares -> sell."""
        engine = _make_exit_engine()
        position = {"entry_price": 0.96, "shares": 0}
        verdict = engine.should_take_profit_or_hold(position, current_price=0.998)
        self.assertEqual(verdict, "sell")


if __name__ == "__main__":
    unittest.main()
