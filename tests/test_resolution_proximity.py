"""Tests for P3: Resolution proximity exponential weighting."""

import unittest
from src.utils import resolution_proximity_weight, calculate_bond_score


class TestResolutionProximityWeight(unittest.TestCase):

    def test_1_day_higher_than_7_day(self):
        """1-day market should score higher than 7-day."""
        w1 = resolution_proximity_weight(1.0)
        w7 = resolution_proximity_weight(7.0)
        self.assertGreater(w1, w7)

    def test_14_day_near_zero(self):
        """14-day market should have very low weight."""
        w14 = resolution_proximity_weight(14.0)
        self.assertLess(w14, 0.05)

    def test_0_day_near_1(self):
        """Same-day resolution should be near 1.0."""
        w0 = resolution_proximity_weight(0.01)
        self.assertGreater(w0, 0.95)

    def test_custom_decay_rate(self):
        """Higher decay rate -> sharper drop-off."""
        w_low = resolution_proximity_weight(3.0, decay_rate=0.1)
        w_high = resolution_proximity_weight(3.0, decay_rate=0.5)
        self.assertGreater(w_low, w_high)


class TestBondScoreExponentialMode(unittest.TestCase):

    def test_exponential_mode_changes_score(self):
        """Exponential mode should produce different scores than linear."""
        config_exp = {
            "scoring": {"use_exponential_proximity": True, "resolution_proximity_decay_rate": 0.3},
            "feature_flags": {"exponential_proximity": True},
        }
        config_lin = {
            "scoring": {"use_exponential_proximity": False},
            "feature_flags": {"exponential_proximity": False},
        }

        score_exp = calculate_bond_score(0.96, 3.0, 50000, 0.005, config=config_exp)
        score_lin = calculate_bond_score(0.96, 3.0, 50000, 0.005, config=config_lin)
        self.assertNotAlmostEqual(score_exp, score_lin, places=6)

    def test_exponential_favors_short_duration(self):
        """1-day market should be scored much higher than 7-day in exp mode."""
        config = {
            "scoring": {"use_exponential_proximity": True, "resolution_proximity_decay_rate": 0.3},
            "feature_flags": {"exponential_proximity": True},
        }
        score_1d = calculate_bond_score(0.96, 1.0, 50000, 0.005, config=config)
        score_7d = calculate_bond_score(0.96, 7.0, 50000, 0.005, config=config)
        self.assertGreater(score_1d, score_7d * 3)  # Should be much higher


if __name__ == "__main__":
    unittest.main()
