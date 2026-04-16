"""Tests for P1: Price drop cool-down."""

import unittest
from src.detector import PseudoCertaintyDetector


def _make_detector():
    config = {
        "scanner": {
            "min_entry_price": 0.94,
            "max_entry_price": 0.99,
            "max_price_volatility_1d": 0.03,
            "excluded_categories": [],
        },
        "orderbook": {},
        "risk": {"min_net_yield": 0.01},
        "binary_catalyst": {},
        "detector": {
            "price_drop_cooldown_threshold": -0.03,
            "price_drop_recovery_ratio": 0.80,
        },
        "feature_flags": {"price_drop_cooldown": True},
    }
    return PseudoCertaintyDetector(config)


class TestPriceDropCooldown(unittest.TestCase):

    def test_5pct_drop_triggers_cooldown(self):
        """Market dropped 5% -> reject."""
        detector = _make_detector()
        market = {"oneDayPriceChange": -0.05}
        passed, reason = detector._check_price_drop_cooldown(market)
        self.assertFalse(passed)
        self.assertIn("cool-down", reason.lower())

    def test_2pct_drop_no_cooldown(self):
        """Market dropped 2% -> no cooldown triggered, allow."""
        detector = _make_detector()
        market = {"oneDayPriceChange": -0.02}
        passed, reason = detector._check_price_drop_cooldown(market)
        self.assertTrue(passed)

    def test_positive_change_passes(self):
        """Price went up -> passes."""
        detector = _make_detector()
        market = {"oneDayPriceChange": 0.01}
        passed, reason = detector._check_price_drop_cooldown(market)
        self.assertTrue(passed)


if __name__ == "__main__":
    unittest.main()
