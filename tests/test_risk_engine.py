"""
Tests for RiskEngine — all 10 entry checks.
"""

import unittest
from unittest.mock import MagicMock, patch

from src.risk_engine import RiskEngine
from src.portfolio_manager import PortfolioManager


def _make_engine(
    balance=10000.0,
    deployed=0.0,
    todays_pnl=0.0,
    consecutive_losses=0,
    category_exposure=0.0,
    event_exposure=0.0,
    bucket_exposure=0.0,
    config_overrides=None,
):
    """Build a RiskEngine with mocked PortfolioManager."""
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
            "min_viable_position": 50.0,
        }
    }
    if config_overrides:
        config["risk"].update(config_overrides)

    portfolio = MagicMock(spec=PortfolioManager)
    portfolio.get_portfolio_balance.return_value = balance
    portfolio.get_total_deployed.return_value = deployed
    portfolio.get_todays_realized_pnl.return_value = todays_pnl
    portfolio.get_consecutive_losses.return_value = consecutive_losses
    portfolio.get_category_exposure.return_value = category_exposure
    portfolio.get_event_group_exposure.return_value = event_exposure
    portfolio.get_risk_bucket_exposure.return_value = bucket_exposure
    # V3: Bucket confidence scaling mock — assume proven bucket by default
    portfolio.get_bucket_statistics.return_value = {
        "closed_count": 5, "total_pnl": 10.0, "avg_pnl": 2.0,
        "win_count": 4, "loss_count": 1,
    }

    engine = RiskEngine(config, portfolio)
    return engine


class TestRiskEngineSizing(unittest.TestCase):

    def test_position_size_above_hard_cap_rejected(self):
        """Request 5% of portfolio → exceeds 4% hard cap → reject."""
        engine = _make_engine(balance=10000.0)
        ok, reason, size = engine.check_position_size(500.0, 10000.0)  # 5%
        self.assertFalse(ok)
        self.assertIn("hard cap", reason.lower())
        self.assertEqual(size, 0.0)

    def test_position_size_at_3pct_approved(self):
        """Request 3% → approved at exactly 3%."""
        engine = _make_engine(balance=10000.0)
        ok, reason, size = engine.check_position_size(300.0, 10000.0)  # 3%
        self.assertTrue(ok)
        self.assertAlmostEqual(size, 300.0)

    def test_position_size_between_target_and_cap_capped_to_target(self):
        """Request 3.5% (between 3% target and 4% cap) → capped to 3% target."""
        engine = _make_engine(balance=10000.0)
        ok, reason, size = engine.check_position_size(350.0, 10000.0)  # 3.5%
        self.assertTrue(ok)
        self.assertAlmostEqual(size, 300.0)  # capped to target 3%


class TestRiskEngineDeployment(unittest.TestCase):

    def test_deployment_at_70pct_rejects_new_entry(self):
        """Portfolio 70% deployed → reject new entry."""
        engine = _make_engine(balance=10000.0, deployed=7000.0)
        ok, reason = engine.check_deployment_limit(100.0, 10000.0)
        self.assertFalse(ok)
        self.assertIn("deployment", reason.lower())

    def test_deployment_60pct_new_trade_pushes_to_72pct_rejected(self):
        """Portfolio 60% deployed, new trade would push to 72% → reject."""
        engine = _make_engine(balance=10000.0, deployed=6000.0)
        ok, reason = engine.check_deployment_limit(1500.0, 10000.0)  # 6000+1500=7500=75%
        self.assertFalse(ok)

    def test_deployment_within_limits_approved(self):
        """Portfolio 50% deployed, small new trade → approved."""
        engine = _make_engine(balance=10000.0, deployed=5000.0)
        ok, reason = engine.check_deployment_limit(500.0, 10000.0)  # 5500=55%
        self.assertTrue(ok)


class TestRiskEngineCategoryExposure(unittest.TestCase):

    def test_category_at_19pct_new_2pct_would_hit_21pct_rejected(self):
        """Politics at 19%, new 2% trade → total 21% > 20% cap → reject."""
        engine = _make_engine(balance=10000.0, category_exposure=1900.0)
        ok, reason = engine.check_category_exposure("Politics", 200.0, 10000.0)
        self.assertFalse(ok)
        self.assertIn("Politics", reason)

    def test_category_at_15pct_new_2pct_approved(self):
        """Category at 15%, new 2% → total 17% within 20% → approved."""
        engine = _make_engine(balance=10000.0, category_exposure=1500.0)
        ok, reason = engine.check_category_exposure("Politics", 200.0, 10000.0)
        self.assertTrue(ok)


class TestRiskEngineEventGroup(unittest.TestCase):

    def test_event_group_at_14pct_new_2pct_rejected(self):
        """Same event at 14%, new 2% trade → 16% > 15% cap → reject."""
        engine = _make_engine(balance=10000.0, event_exposure=1400.0)
        ok, reason = engine.check_event_group_exposure("event-123", 200.0, 10000.0)
        self.assertFalse(ok)

    def test_event_group_empty_id_passes(self):
        """No event group ID → skip check."""
        engine = _make_engine(balance=10000.0)
        ok, reason = engine.check_event_group_exposure("", 200.0, 10000.0)
        self.assertTrue(ok)


class TestRiskEngineCircuitBreakers(unittest.TestCase):

    def test_daily_loss_at_2pt1pct_rejects(self):
        """2.1% daily loss → halt new entries."""
        engine = _make_engine(balance=10000.0, todays_pnl=-210.0)
        ok, reason = engine.check_daily_loss_limit(10000.0)
        self.assertFalse(ok)
        self.assertIn("daily loss", reason.lower())

    def test_daily_loss_at_1pt9pct_allowed(self):
        """1.9% daily loss → still within limit."""
        engine = _make_engine(balance=10000.0, todays_pnl=-190.0)
        ok, reason = engine.check_daily_loss_limit(10000.0)
        self.assertTrue(ok)

    def test_3_consecutive_losses_halts(self):
        """3 consecutive losses → halt."""
        engine = _make_engine(consecutive_losses=3)
        ok, reason = engine.check_consecutive_losses()
        self.assertFalse(ok)
        self.assertIn("consecutive", reason.lower())

    def test_2_consecutive_losses_allowed(self):
        """2 consecutive losses → still allowed."""
        engine = _make_engine(consecutive_losses=2)
        ok, reason = engine.check_consecutive_losses()
        self.assertTrue(ok)


class TestRiskEngineVolumeSize(unittest.TestCase):

    def test_volume_too_low_for_viable_position(self):
        """Market volume $1000/day → max $20 (< $50 min) → reject."""
        engine = _make_engine()
        ok, reason, size = engine.check_volume_to_size(1000.0, 200.0)
        self.assertFalse(ok)
        self.assertIn("volume", reason.lower())

    def test_position_capped_by_volume(self):
        """Market $50k/day, request $2000 → capped to $1000 (2% of 50k)."""
        engine = _make_engine()
        ok, reason, adjusted = engine.check_volume_to_size(50000.0, 2000.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(adjusted, 1000.0)

    def test_no_volume_data_rejected(self):
        """No volume data → reject entry (R8: fail closed)."""
        engine = _make_engine()
        ok, reason, size = engine.check_volume_to_size(0.0, 300.0)
        self.assertFalse(ok)
        self.assertAlmostEqual(size, 0.0)


class TestRiskEngineEvaluateEntry(unittest.TestCase):

    def test_all_checks_pass_returns_approved(self):
        """When all checks pass, evaluate_entry returns approved with adjusted size."""
        engine = _make_engine(
            balance=10000.0,
            deployed=2000.0,
            todays_pnl=50.0,
            consecutive_losses=0,
            category_exposure=500.0,
            event_exposure=0.0,
            bucket_exposure=200.0,
        )
        approved, reason, size = engine.evaluate_entry(
            market_id="mkt-001",
            category="Sports",
            event_group_id="evt-001",
            requested_size=200.0,
            entry_price=0.97,
            market_question="Will team X win?",
            market_volume_24h=20000.0,
        )
        self.assertTrue(approved)
        self.assertGreater(size, 0)


class TestRiskEngineCategoryBlock(unittest.TestCase):

    def test_blocked_category_rejects_entry(self):
        """Category blocked due to UMA dispute → reject."""
        engine = _make_engine()
        engine.add_temporary_category_block("Politics", "UMA dispute in market X")
        ok, reason = engine.check_category_blocked("Politics")
        self.assertFalse(ok)
        self.assertIn("blocked", reason.lower())

    def test_unblocked_category_passes(self):
        """Category not blocked → passes."""
        engine = _make_engine()
        ok, reason = engine.check_category_blocked("Sports")
        self.assertTrue(ok)

    def test_remove_block_re_enables_category(self):
        """After removing a block, category passes again."""
        engine = _make_engine()
        engine.add_temporary_category_block("Crypto", "dispute")
        engine.remove_category_block("Crypto")
        ok, reason = engine.check_category_blocked("Crypto")
        self.assertTrue(ok)


class TestRiskBucketClassification(unittest.TestCase):
    """Tests for improved risk bucket keyword classification."""

    def setUp(self):
        from src.risk_buckets import RiskBucketClassifier
        self.classifier = RiskBucketClassifier()

    def test_nba_team_names_classify_as_sports(self):
        """NBA team names should land in sports bucket."""
        bucket = self.classifier.classify("", "76ers vs. Pacers")
        self.assertEqual(bucket, "sports")

    def test_vs_pattern_classifies_as_sports(self):
        """'X vs. Y' pattern should land in sports bucket."""
        bucket = self.classifier.classify("", "Heat vs. Wizards")
        self.assertEqual(bucket, "sports")

    def test_tennis_player_classifies_as_sports(self):
        """Tennis player names should land in sports bucket."""
        bucket = self.classifier.classify("", "Rolex Monte Carlo Masters: Carlos Alcaraz vs Alexander Bublik")
        self.assertEqual(bucket, "sports")

    def test_asian_basketball_classifies_as_sports(self):
        """Asian basketball team names should land in sports bucket."""
        bucket = self.classifier.classify("", "Shanghai Sharks vs. Fujian Sturgeons")
        self.assertEqual(bucket, "sports")

    def test_stock_market_classifies_as_macro(self):
        """Stock price questions should land in macro bucket."""
        bucket = self.classifier.classify("", "Will Meta (META) finish week of April 6 above $520?")
        self.assertEqual(bucket, "macro")

    def test_redistricting_classifies_as_politics(self):
        """Redistricting/referendum questions should land in politics bucket."""
        bucket = self.classifier.classify("", "Will the Virginia redistricting referendum pass?")
        self.assertEqual(bucket, "politics")

    def test_special_election_classifies_as_politics(self):
        """Special election questions should land in politics."""
        bucket = self.classifier.classify("", "Will Analilia Mejia win the NJ-11 special election?")
        self.assertEqual(bucket, "politics")

    def test_bulgarian_seats_classifies_as_politics(self):
        """Parliamentary seats questions should land in politics."""
        bucket = self.classifier.classify("", "Will Progressive Bulgaria (PB) win the most seats in the 2026 Bulgarian election?")
        self.assertEqual(bucket, "politics")


if __name__ == "__main__":
    unittest.main()
