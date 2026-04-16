"""Tests for P2: YES+NO parity check."""

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
            "min_parity_sum": 0.96,
            "max_parity_sum": 1.04,
        },
        "feature_flags": {"secondary_price_validation": True},
    }
    return PseudoCertaintyDetector(config)


class TestYesNoParity(unittest.TestCase):

    def test_normal_parity_passes(self):
        """YES=$0.96, NO=$0.03 -> sum=0.99 -> pass."""
        detector = _make_detector()
        market = {"_yes_price": 0.96, "_no_price": 0.03}
        passed, reason = detector._check_yes_no_parity(market)
        self.assertTrue(passed)

    def test_excessive_parity_rejected(self):
        """YES=$0.96, NO=$0.15 -> sum=1.11 -> reject."""
        detector = _make_detector()
        market = {"_yes_price": 0.96, "_no_price": 0.15}
        passed, reason = detector._check_yes_no_parity(market)
        self.assertFalse(passed)
        self.assertIn("parity violation", reason.lower())

    def test_low_parity_rejected(self):
        """YES=$0.50, NO=$0.40 -> sum=0.90 -> reject."""
        detector = _make_detector()
        market = {"_yes_price": 0.50, "_no_price": 0.40}
        passed, reason = detector._check_yes_no_parity(market)
        self.assertFalse(passed)

    def test_no_no_price_skips_check(self):
        """YES=$0.96, NO=0 -> skip check, pass."""
        detector = _make_detector()
        market = {"_yes_price": 0.96, "_no_price": 0}
        passed, reason = detector._check_yes_no_parity(market)
        self.assertTrue(passed)
        self.assertIn("skipped", reason.lower())


if __name__ == "__main__":
    unittest.main()
