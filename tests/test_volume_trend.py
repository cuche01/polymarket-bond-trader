"""Tests for P1: Volume trend filter."""

import unittest
from src.scanner import MarketScanner


def _make_scanner(config_overrides=None):
    config = {
        "scanner": {
            "scan_interval_seconds": 300,
            "min_entry_price": 0.94,
            "max_entry_price": 0.99,
            "max_days_to_resolution": 14,
            "preferred_resolution_hours": 72,
            "min_liquidity": 5000,
            "min_volume_24h": 2500,
            "max_price_volatility_1d": 0.05,
            "volume_trend_min_ratio": 0.70,
            "time_filter_weekend_multiplier": 2.0,
            "time_filter_offpeak_multiplier": 1.5,
            "excluded_categories": [],
        },
        "feature_flags": {"volume_trend_filter": True, "time_of_day_filter": True},
    }
    if config_overrides:
        config["scanner"].update(config_overrides)
    return MarketScanner(config)


class TestVolumeTrendFilter(unittest.TestCase):

    def test_healthy_volume_trend_passes(self):
        """24h volume at 80% of estimated daily avg -> passes."""
        scanner = _make_scanner()
        market = {
            "volume24hr": 5000,
            "volume": 50000,  # lifetime
            "endDate": "2026-04-17T00:00:00Z",  # ~7 days away
        }
        self.assertTrue(scanner._check_volume_trend(market))

    def test_declining_volume_rejected(self):
        """24h volume at 20% of estimated daily avg -> rejected."""
        scanner = _make_scanner()
        market = {
            "volume24hr": 500,
            "volume": 50000,  # lifetime, avg ~3500/day over 14 days
            "endDate": "2026-04-17T00:00:00Z",
        }
        self.assertFalse(scanner._check_volume_trend(market))

    def test_no_volume_data_passes_if_above_min(self):
        """No lifetime volume data -> falls back to min_volume_24h."""
        scanner = _make_scanner()
        market = {
            "volume24hr": 3000,
            "volume": 0,
            "endDate": "2026-04-17T00:00:00Z",
        }
        self.assertTrue(scanner._check_volume_trend(market))


class TestTimeOfDayFilter(unittest.TestCase):

    def test_base_liquidity_returned(self):
        """Should always return at least base min_liquidity."""
        scanner = _make_scanner()
        adjusted = scanner._get_time_adjusted_min_liquidity()
        self.assertGreaterEqual(adjusted, scanner.min_liquidity)


if __name__ == "__main__":
    unittest.main()
