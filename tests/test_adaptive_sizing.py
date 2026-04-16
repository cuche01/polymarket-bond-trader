"""Tests for P2: Adaptive position sizing."""

import unittest
from unittest.mock import MagicMock

from src.risk_engine import RiskEngine
from src.portfolio_manager import PortfolioManager


def _make_engine(adaptive_enabled=True):
    config = {
        "risk": {
            "max_single_market_pct": 0.12,
            "target_position_pct": 0.08,
            "max_correlated_pct": 0.25,
            "max_category_exposure_pct": 0.35,
            "max_deployed_pct": 0.80,
            "max_daily_loss_pct": 0.05,
            "consecutive_loss_halt": 5,
            "max_slippage_pct": 0.03,
            "volume_size_max_pct": 0.05,
            "min_viable_position": 15,
        },
        "scanner": {
            "min_entry_price": 0.94,
            "max_entry_price": 0.99,
            "preferred_resolution_hours": 72,
        },
        "adaptive_sizing": {
            "enabled": adaptive_enabled,
            "min_size_pct": 0.05,
            "max_size_pct": 0.10,
        },
        "feature_flags": {"adaptive_sizing": adaptive_enabled},
    }
    portfolio = MagicMock(spec=PortfolioManager)
    return RiskEngine(config, portfolio)


class TestAdaptiveSizing(unittest.TestCase):

    def test_high_confidence_short_duration_gets_more_capital(self):
        """$0.98 entry, 1 day -> higher sizing than $0.94, 14 days."""
        engine = _make_engine()
        high_conf = engine.calculate_adaptive_size(0.98, 1.0, 10000)
        low_conf = engine.calculate_adaptive_size(0.94, 14.0, 10000)
        self.assertGreater(high_conf, low_conf)

    def test_size_within_bounds(self):
        """All sizes should be between 5% and 10% of portfolio."""
        engine = _make_engine()
        portfolio = 10000
        for price in [0.94, 0.96, 0.98, 0.99]:
            for days in [0.5, 1, 3, 7, 14]:
                size = engine.calculate_adaptive_size(price, days, portfolio)
                self.assertGreaterEqual(size, portfolio * 0.05 - 1)  # small float tolerance
                self.assertLessEqual(size, portfolio * 0.10 + 1)

    def test_098_1day_near_max(self):
        """$0.98 entry, 1 day -> near max sizing (10%)."""
        engine = _make_engine()
        size = engine.calculate_adaptive_size(0.98, 1.0, 10000)
        # Should be close to $1000 (10% of $10k)
        self.assertGreater(size, 800)

    def test_094_14day_near_min(self):
        """$0.94 entry, 14 days -> near min sizing (5%)."""
        engine = _make_engine()
        size = engine.calculate_adaptive_size(0.94, 14.0, 10000)
        # Should be close to $500 (5% of $10k)
        self.assertLess(size, 700)

    def test_never_exceeds_hard_cap(self):
        """Adaptive size should not exceed 12% hard cap (enforced by check_position_size)."""
        engine = _make_engine()
        size = engine.calculate_adaptive_size(0.99, 0.1, 10000)
        self.assertLessEqual(size, 10000 * 0.12)


if __name__ == "__main__":
    unittest.main()
