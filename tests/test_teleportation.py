"""Tests for P0: Teleportation slippage protection."""

import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from src.exit_engine import ExitEngine, ExitDecision


def _make_exit_engine(config_overrides=None):
    """Build an ExitEngine with mocked dependencies."""
    config = {
        "exits": {
            "stop_loss_pct": 0.07,
            "max_holding_days": 14,
            "bond_take_profit_price": 0.995,
            "bond_take_profit_min_hours_to_resolution": 48,
            "generic_take_profit_pct": 0.10,
            "trailing_stop_activation_price": 0.995,
            "trailing_stop_distance_pct": 0.005,
            "tiered_stop_loss": [
                {"min_entry": 0.98, "stop_loss_pct": 0.05},
                {"min_entry": 0.96, "stop_loss_pct": 0.07},
                {"min_entry": 0.93, "stop_loss_pct": 0.10},
                {"min_entry": 0.90, "stop_loss_pct": 0.12},
            ],
            "revalidation_interval_hours": 4,
        },
        "teleportation": {
            "teleportation_max_loss_pct": 0.50,
            "teleportation_detection_multiplier": 2.0,
            "teleportation_exit_slippage_pct": 0.10,
        },
        "feature_flags": {
            "teleportation_detection": True,
        },
    }
    if config_overrides:
        for k, v in config_overrides.items():
            if isinstance(v, dict) and k in config:
                config[k].update(v)
            else:
                config[k] = v

    portfolio = MagicMock()
    risk_engine = MagicMock()
    notifier = AsyncMock()
    return ExitEngine(config, portfolio, risk_engine, notifier)


class TestTeleportationDetection(unittest.TestCase):

    def test_price_gap_from_096_to_015_triggers_teleportation(self):
        """Price gap from $0.96 to $0.15 in a single cycle -> teleportation."""
        engine = _make_exit_engine()
        position = {"id": 1, "entry_price": 0.96, "shares": 100}
        result = engine.check_teleportation(position, current_price=0.15)
        self.assertIsNotNone(result)
        self.assertEqual(result.reason, "teleportation_catastrophic")
        self.assertEqual(result.urgency, "immediate")

    def test_gradual_decline_does_not_trigger_teleportation(self):
        """Gradual decline from $0.96 to $0.89 -> not teleportation (normal stop-loss)."""
        engine = _make_exit_engine()
        position = {"id": 2, "entry_price": 0.96, "shares": 100}
        # 7.3% drop -> within 2x the 7% stop tier
        result = engine.check_teleportation(position, current_price=0.89)
        self.assertIsNone(result)

    def test_teleportation_within_survivable_range(self):
        """Drop to 25% loss (below 2x stop but above max_loss) -> survivable teleportation."""
        engine = _make_exit_engine()
        position = {"id": 3, "entry_price": 0.96, "shares": 100}
        # ~20% drop with 7% stop tier -> 20% > 2*7% = 14% but < 50%
        result = engine.check_teleportation(position, current_price=0.77)
        self.assertIsNotNone(result)
        self.assertEqual(result.reason, "teleportation_detected")

    def test_price_above_stop_trigger_no_teleportation(self):
        """Price still above stop-loss -> no teleportation."""
        engine = _make_exit_engine()
        position = {"id": 4, "entry_price": 0.96, "shares": 100}
        result = engine.check_teleportation(position, current_price=0.94)
        self.assertIsNone(result)

    def test_catastrophic_loss_exceeds_max_loss_pct(self):
        """Loss >= 50% -> catastrophic exit at any price."""
        engine = _make_exit_engine()
        position = {"id": 5, "entry_price": 0.96, "shares": 100}
        result = engine.check_teleportation(position, current_price=0.10)
        self.assertIsNotNone(result)
        self.assertEqual(result.reason, "teleportation_catastrophic")


if __name__ == "__main__":
    unittest.main()
