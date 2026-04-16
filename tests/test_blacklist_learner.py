"""Tests for P2: Blacklist learning loop."""

import unittest
from unittest.mock import MagicMock

from src.blacklist_learner import BlacklistLearner


class TestBlacklistLearner(unittest.TestCase):

    def test_feature_extraction_from_position(self):
        """Extracts category, risk_bucket, and keyword bigrams."""
        db = MagicMock()
        learner = BlacklistLearner(db, {"blacklist_learner": {"enabled": True}})
        position = {
            "category": "Crypto",
            "risk_bucket": "crypto",
            "market_question": "Will Bitcoin stay above 90k?",
        }
        features = learner._extract_features(position)
        types = [f[0] for f in features]
        self.assertIn("category", types)
        self.assertIn("risk_bucket", types)
        self.assertIn("keyword_bigram", types)

    def test_feature_extraction_from_market(self):
        """Extracts features from candidate market dict."""
        db = MagicMock()
        learner = BlacklistLearner(db, {"blacklist_learner": {"enabled": True}})
        market = {
            "category": "Politics",
            "_risk_bucket": "politics",
            "question": "Will the Senate confirm the nominee?",
        }
        features = learner._extract_features_from_market(market)
        self.assertTrue(len(features) >= 2)

    def test_disabled_learner_returns_no_penalty(self):
        """Disabled learner always returns 1.0 penalty."""
        db = MagicMock()
        learner = BlacklistLearner(db, {"blacklist_learner": {"enabled": False}})
        penalty = learner.get_penalty({"category": "Crypto"})
        self.assertEqual(penalty, 1.0)

    def test_record_loss_calls_db(self):
        """Recording a loss should write to DB."""
        db = MagicMock()
        learner = BlacklistLearner(db, {"blacklist_learner": {"enabled": True}})
        position = {
            "market_id": "mkt-1",
            "pnl": -50.0,
            "category": "Crypto",
            "risk_bucket": "crypto",
            "market_question": "Will BTC hit 100k?",
        }
        learner.record_loss(position)
        self.assertTrue(db.execute_write.called)

    def test_penalty_below_threshold_is_neutral(self):
        """2 losses (below threshold of 3) -> no penalty."""
        db = MagicMock()
        db.execute_read.return_value = [{"cnt": 2}]
        learner = BlacklistLearner(db, {
            "blacklist_learner": {"enabled": True, "loss_threshold": 3, "window_days": 30}
        })
        penalty = learner.get_penalty({"category": "Crypto"})
        self.assertEqual(penalty, 1.0)

    def test_penalty_at_threshold(self):
        """3 losses (at threshold) -> penalty 0.7."""
        db = MagicMock()
        db.execute_read.return_value = [{"cnt": 3}]
        learner = BlacklistLearner(db, {
            "blacklist_learner": {"enabled": True, "loss_threshold": 3, "window_days": 30}
        })
        penalty = learner.get_penalty({"category": "Crypto"})
        self.assertAlmostEqual(penalty, 0.7, places=1)

    def test_penalty_at_double_threshold(self):
        """6 losses (2x threshold) -> penalty 0.4."""
        db = MagicMock()
        db.execute_read.return_value = [{"cnt": 6}]
        learner = BlacklistLearner(db, {
            "blacklist_learner": {"enabled": True, "loss_threshold": 3, "window_days": 30}
        })
        penalty = learner.get_penalty({"category": "Crypto"})
        self.assertAlmostEqual(penalty, 0.4, places=1)


if __name__ == "__main__":
    unittest.main()
