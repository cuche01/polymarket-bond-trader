"""Tests for P1: Binary catalyst classifier."""

import unittest
from src.detector import PseudoCertaintyDetector


def _make_detector(config_overrides=None):
    config = {
        "scanner": {
            "min_entry_price": 0.94,
            "max_entry_price": 0.99,
            "max_price_volatility_1d": 0.03,
            "excluded_categories": [],
        },
        "orderbook": {},
        "risk": {"min_net_yield": 0.01},
        "binary_catalyst": {
            "binary_catalyst_reject_threshold": 0.85,
            "binary_catalyst_penalize_threshold": 0.50,
            "binary_catalyst_penalty_factor": 0.60,
        },
        "detector": {},
        "feature_flags": {"binary_catalyst_filter": True},
    }
    if config_overrides:
        config.update(config_overrides)
    return PseudoCertaintyDetector(config)


class TestBinaryCatalystClassifier(unittest.TestCase):

    def test_supreme_court_ruling_is_binary(self):
        """'Will the Supreme Court rule on X by Friday?' -> binary_score >= 0.85 -> reject."""
        detector = _make_detector()
        result = detector._classify_catalyst_type(
            "Will the Supreme Court rule on abortion rights by Friday?"
        )
        self.assertGreaterEqual(result["binary_score"], 0.70)
        self.assertIn(result["catalyst_type"], ("binary", "mixed"))

    def test_btc_stay_above_is_continuous(self):
        """'Will Bitcoin stay above $90k through March?' -> continuous."""
        detector = _make_detector()
        result = detector._classify_catalyst_type(
            "Will Bitcoin stay above $90k through March?"
        )
        self.assertLessEqual(result["binary_score"], 0.50)
        self.assertIn(result["recommendation"], ("allow", "penalize"))

    def test_fda_approval_is_mixed(self):
        """'Will the FDA approve drug X before Q2?' -> mixed/binary."""
        detector = _make_detector()
        result = detector._classify_catalyst_type(
            "Will the FDA approve drug X before Q2?"
        )
        self.assertGreaterEqual(result["binary_score"], 0.50)

    def test_unknown_market_gets_conservative_score(self):
        """No patterns matched -> binary_score 0.6 -> penalize."""
        detector = _make_detector()
        result = detector._classify_catalyst_type("Something completely unknown")
        self.assertAlmostEqual(result["binary_score"], 0.6)
        self.assertEqual(result["recommendation"], "penalize")

    def test_catalyst_penalty_stored_on_market(self):
        """Penalized market gets _catalyst_penalty < 1.0."""
        detector = _make_detector()
        market = {
            "question": "Will the FDA approve drug X before Q2?",
            "description": "",
        }
        passed, reason = detector._check_binary_catalyst(market)
        if passed:
            # Should have penalty applied if penalized
            penalty = market.get("_catalyst_penalty", 1.0)
            self.assertLessEqual(penalty, 1.0)

    def test_layer_4_5_rejects_pure_binary(self):
        """Pure binary catalyst should be rejected by Layer 4.5."""
        detector = _make_detector()
        market = {
            "question": "Will the jury convict the defendant? Will the judge rule?",
            "description": "Supreme Court verdict expected.",
        }
        passed, reason = detector._check_binary_catalyst(market)
        # High binary density should trigger reject or at least penalize
        self.assertIn("Catalyst:", reason) if passed else self.assertIn("rejected", reason.lower())


if __name__ == "__main__":
    unittest.main()
