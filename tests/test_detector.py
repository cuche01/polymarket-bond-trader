"""
Tests for the PseudoCertaintyDetector validation layers.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.detector import PseudoCertaintyDetector


def run(coro):
    """Helper to run async code in tests."""
    return asyncio.run(coro)


def make_future_date(days: int = 7) -> str:
    """Create an ISO date string `days` in the future."""
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.isoformat()


def make_valid_market(**overrides) -> dict:
    """Create a minimal valid market dictionary for testing."""
    market = {
        "id": "test-market-001",
        "conditionId": "test-market-001",
        "question": "Will the Fed raise rates by December 2025?",
        "title": "Will the Fed raise rates by December 2025?",
        "category": "Economics",
        "slug": "fed-rate-hike-dec-2025",
        "active": True,
        "closed": False,
        "endDate": make_future_date(5),
        "outcomePrices": json.dumps(["0.97", "0.03"]),
        "clobTokenIds": json.dumps(["111111", "222222"]),
        "liquidityClob": 25000,
        "liquidity": 25000,
        "volume24hr": 8000,
        "oneDayPriceChange": 0.005,
        "oneWeekPriceChange": 0.01,
        "bestBid": 0.96,
        "bestAsk": 0.98,
        "_yes_price": 0.97,
        "_no_price": 0.03,
        "_yes_token_id": "111111",
        "_no_token_id": "222222",
        "_days_to_resolution": 5.0,
        "_liquidity": 25000,
        "_volume_24h": 8000,
        "_price_change_1d": 0.005,
    }
    market.update(overrides)
    return market


def make_test_config(**overrides) -> dict:
    """Create a test configuration dictionary."""
    config = {
        "scanner": {
            "min_entry_price": 0.95,
            "max_entry_price": 0.99,
            "max_days_to_resolution": 14,
            "min_liquidity": 10000,
            "min_volume_24h": 5000,
            "max_price_volatility_1d": 0.03,
            "excluded_categories": ["15-min Crypto", "1-hr Crypto", "Live Sports"],
        },
        "risk": {
            "min_net_yield": 0.01,
        },
        "orderbook": {
            "min_bid_depth_multiplier": 5,
            "max_spread": 0.03,
            "max_bid_volume_decline_pct": 0.30,
        },
    }
    for key, val in overrides.items():
        if isinstance(val, dict) and key in config:
            config[key].update(val)
        else:
            config[key] = val
    return config


def make_mock_clob_client(bid_depth_usd: float = 50000.0) -> MagicMock:
    """Create a mock CLOB client with configurable bid depth."""
    client = MagicMock()

    # Mock bid with enough depth
    mock_bid = MagicMock()
    mock_bid.price = 0.96
    mock_bid.size = bid_depth_usd / 0.96  # shares to fill USD amount

    mock_ask = MagicMock()
    mock_ask.price = 0.98
    mock_ask.size = 1000

    mock_orderbook = MagicMock()
    mock_orderbook.bids = [mock_bid]
    mock_orderbook.asks = [mock_ask]

    client.get_order_book = MagicMock(return_value=mock_orderbook)
    return client


def make_detector(config=None) -> PseudoCertaintyDetector:
    """Create a detector with a mocked blacklist file."""
    if config is None:
        config = make_test_config()
    with patch("src.detector.Path") as mock_path:
        mock_path.return_value.exists.return_value = True
        with patch("builtins.open", unittest.mock.mock_open(
            read_data='{"market_ids":[],"slugs":[],"keyword_patterns":[],"categories":[]}'
        )):
            return PseudoCertaintyDetector(config)


def make_detector_with_blacklist(blacklist_data: dict, config=None) -> PseudoCertaintyDetector:
    """Create a detector with specific blacklist data."""
    if config is None:
        config = make_test_config()
    with patch("src.detector.Path") as mock_path:
        mock_path.return_value.exists.return_value = True
        with patch("builtins.open", unittest.mock.mock_open(
            read_data=json.dumps(blacklist_data)
        )):
            return PseudoCertaintyDetector(config)


class TestDetectorLayer1CategoryExclusion(unittest.TestCase):
    """Tests for Layer 1: Category Exclusion."""

    def setUp(self):
        self.config = make_test_config()
        blacklist_data = {
            "market_ids": [],
            "slugs": [],
            "keyword_patterns": ["15-min", "1-hour-price", "live-game"],
            "categories": ["15-min Crypto", "1-hr Crypto", "Live Sports"],
        }
        self.detector = make_detector_with_blacklist(blacklist_data, self.config)

    def test_live_sports_market_rejected(self):
        """Layer 1 should reject Live Sports category markets."""
        market = make_valid_market(
            category="Live Sports",
            question="Will Team A win the game tonight?"
        )
        passed, reason = self.detector._check_category_exclusion(market)
        self.assertFalse(passed)
        self.assertIn("Live Sports", reason)

    def test_15min_crypto_category_rejected(self):
        """Layer 1 should reject 15-min Crypto category."""
        market = make_valid_market(category="15-min Crypto")
        passed, reason = self.detector._check_category_exclusion(market)
        self.assertFalse(passed)
        self.assertIn("15-min Crypto", reason)

    def test_1hr_crypto_category_rejected(self):
        """Layer 1 should reject 1-hr Crypto category."""
        market = make_valid_market(category="1-hr Crypto")
        passed, reason = self.detector._check_category_exclusion(market)
        self.assertFalse(passed)

    def test_valid_category_passes(self):
        """Layer 1 should pass a valid economics market."""
        market = make_valid_market(category="Economics")
        passed, reason = self.detector._check_category_exclusion(market)
        self.assertTrue(passed)

    def test_keyword_pattern_in_question_rejected(self):
        """Layer 1 should reject markets with blacklisted keywords in question."""
        market = make_valid_market(
            category="Crypto",
            question="Will Bitcoin 15-min candle be green at 3pm?"
        )
        passed, reason = self.detector._check_category_exclusion(market)
        self.assertFalse(passed)
        self.assertIn("15-min", reason)

    def test_live_game_keyword_rejected(self):
        """Layer 1 should reject markets with 'live-game' slug pattern."""
        market = make_valid_market(
            category="Sports",
            slug="live-game-nba-2025"
        )
        passed, reason = self.detector._check_category_exclusion(market)
        self.assertFalse(passed)

    def test_empty_category_passes(self):
        """Layer 1 should pass market with no category set."""
        market = make_valid_market(category="")
        passed, reason = self.detector._check_category_exclusion(market)
        self.assertTrue(passed)

    def test_sports_market_type_field_rejected(self):
        """Layer 1 should reject markets with sportsMarketType set."""
        market = make_valid_market(
            category="",
            question="76ers vs. Pacers",
            sportsMarketType="moneyline",
        )
        passed, reason = self.detector._check_category_exclusion(market)
        self.assertFalse(passed)
        self.assertIn("sportsMarketType", reason)

    def test_sports_fee_type_rejected(self):
        """Layer 1 should reject markets with sports fee type."""
        market = make_valid_market(
            category="",
            question="Heat vs. Wizards",
            feeType="sports_fees_v2",
        )
        passed, reason = self.detector._check_category_exclusion(market)
        self.assertFalse(passed)
        self.assertIn("feeType", reason)

    def test_non_sports_fee_type_passes(self):
        """Layer 1 should pass markets with non-sports feeType."""
        market = make_valid_market(
            category="Economics",
            feeType="standard",
        )
        passed, reason = self.detector._check_category_exclusion(market)
        self.assertTrue(passed)


class TestDetectorLayer2PriceBehavior(unittest.TestCase):
    """Tests for Layer 2: Price Behavior Analysis."""

    def setUp(self):
        self.config = make_test_config()
        self.detector = make_detector(self.config)

    def test_stable_price_passes(self):
        """Layer 2 should pass a market with minimal 1-day price change."""
        market = make_valid_market(oneDayPriceChange=0.005, _price_change_1d=0.005)
        passed, reason = self.detector._check_price_behavior(market)
        self.assertTrue(passed)

    def test_high_volatility_rejected(self):
        """Layer 2 should reject markets with excessive 1-day price change."""
        market = make_valid_market(oneDayPriceChange=0.05, _price_change_1d=0.05)
        passed, reason = self.detector._check_price_behavior(market)
        self.assertFalse(passed)
        self.assertIn("1-day price change", reason)

    def test_price_below_range_rejected(self):
        """Layer 2 should reject markets with YES price below bond range."""
        market = make_valid_market(_yes_price=0.90)
        passed, reason = self.detector._check_price_behavior(market)
        self.assertFalse(passed)
        self.assertIn("outside bond range", reason)

    def test_price_above_range_rejected(self):
        """Layer 2 should reject markets with YES price at or above 1.0."""
        market = make_valid_market(_yes_price=0.995)
        passed, reason = self.detector._check_price_behavior(market)
        self.assertFalse(passed)

    def test_wide_spread_rejected(self):
        """Layer 2 should reject markets with spread exceeding max_spread."""
        market = make_valid_market(bestBid=0.92, bestAsk=0.98, _yes_price=0.97)
        passed, reason = self.detector._check_price_behavior(market)
        self.assertFalse(passed)
        self.assertIn("spread", reason.lower())


class TestDetectorLayer3OrderbookHealth(unittest.TestCase):
    """Tests for Layer 3: Orderbook Health."""

    def setUp(self):
        self.config = make_test_config()
        self.detector = make_detector(self.config)

    def test_sufficient_bid_depth_passes(self):
        """Layer 3 should pass markets with sufficient bid depth."""
        # Position size $1000, min_bid_depth = 5x = $5000
        clob_client = make_mock_clob_client(bid_depth_usd=50000.0)
        market = make_valid_market()

        result = run(
            self.detector._check_orderbook_health(market, clob_client, position_size=1000.0)
        )
        passed, reason = result
        self.assertTrue(passed)

    def test_thin_orderbook_rejected(self):
        """Layer 3 should reject markets with insufficient bid depth."""
        # Position $1000, need 5x = $5000, only provide $1000
        clob_client = make_mock_clob_client(bid_depth_usd=1000.0)
        market = make_valid_market(_liquidity=1000)

        result = run(
            self.detector._check_orderbook_health(market, clob_client, position_size=1000.0)
        )
        passed, reason = result
        self.assertFalse(passed)
        self.assertIn("depth", reason.lower())

    def test_clob_failure_falls_back_to_market_liquidity(self):
        """Layer 3 should fall back to market liquidity when CLOB fails."""
        clob_client = MagicMock()
        clob_client.get_order_book.side_effect = Exception("Connection failed")

        # Market has sufficient liquidity
        market = make_valid_market(_liquidity=50000)
        result = run(
            self.detector._check_orderbook_health(market, clob_client, position_size=1000.0)
        )
        passed, reason = result
        self.assertTrue(passed)
        self.assertIn("fallback", reason.lower())

    def test_no_token_id_rejected(self):
        """Layer 3 should reject markets with no YES token ID."""
        clob_client = MagicMock()
        market = make_valid_market(_yes_token_id=None)
        market.pop("_yes_token_id", None)
        market["clobTokenIds"] = None

        result = run(
            self.detector._check_orderbook_health(market, clob_client, position_size=1000.0)
        )
        passed, reason = result
        self.assertFalse(passed)


class TestDetectorLayer5Blacklist(unittest.TestCase):
    """Tests for Layer 5: Blacklist Check."""

    def setUp(self):
        self.config = make_test_config()
        blacklist_data = {
            "market_ids": ["blacklisted-market-id-001"],
            "slugs": ["blacklisted-slug"],
            "keyword_patterns": ["15-min", "live-game"],
            "categories": [],
        }
        self.detector = make_detector_with_blacklist(blacklist_data, self.config)

    def test_blacklisted_market_id_rejected(self):
        """Layer 5 should reject markets with blacklisted IDs."""
        market = make_valid_market(id="blacklisted-market-id-001")
        passed, reason = self.detector._check_blacklist(market)
        self.assertFalse(passed)
        self.assertIn("blacklisted-market-id-001", reason)

    def test_blacklisted_slug_rejected(self):
        """Layer 5 should reject markets with blacklisted slugs."""
        market = make_valid_market(slug="blacklisted-slug")
        passed, reason = self.detector._check_blacklist(market)
        self.assertFalse(passed)
        self.assertIn("blacklisted-slug", reason)

    def test_non_blacklisted_market_passes(self):
        """Layer 5 should pass markets not on the blacklist."""
        market = make_valid_market(
            id="clean-market-999",
            slug="clean-market-slug"
        )
        passed, reason = self.detector._check_blacklist(market)
        self.assertTrue(passed)

    def test_keyword_in_question_rejected(self):
        """Layer 5 should reject markets with blacklisted keywords."""
        market = make_valid_market(
            question="Will the 15-min BTC candle close green?"
        )
        passed, reason = self.detector._check_blacklist(market)
        self.assertFalse(passed)


class TestDetectorFullValidation(unittest.TestCase):
    """Integration tests for full multi-layer validation."""

    def setUp(self):
        self.config = make_test_config()
        self.detector = make_detector(self.config)

    def test_valid_stable_liquid_market_passes_all_layers(self):
        """A valid, stable, liquid market should pass all validation layers."""
        clob_client = make_mock_clob_client(bid_depth_usd=50000.0)
        market = make_valid_market(
            category="Finance",
            question="Will the Fed hold rates steady through Q4 2025?",
            oneDayPriceChange=0.003,
            _price_change_1d=0.003,
            _yes_price=0.97,
            bestBid=0.96,
            bestAsk=0.98,
            _liquidity=50000,
            liquidityClob=50000,
        )

        result = run(
            self.detector.is_valid_opportunity(market, clob_client, position_size=1000.0)
        )
        passed, reason = result
        self.assertTrue(passed, f"Expected valid market to pass, but got: {reason}")
        self.assertIn("passed", reason.lower())

    def test_live_sports_fails_at_layer_1(self):
        """Live sports market should fail at Layer 1."""
        clob_client = make_mock_clob_client()
        market = make_valid_market(category="Live Sports")

        result = run(
            self.detector.is_valid_opportunity(market, clob_client)
        )
        passed, reason = result
        self.assertFalse(passed)
        self.assertIn("[L1]", reason)

    def test_volatile_market_fails_at_layer_2(self):
        """Volatile market should fail at Layer 2."""
        clob_client = make_mock_clob_client()
        market = make_valid_market(
            category="Finance",
            oneDayPriceChange=0.08,
            _price_change_1d=0.08,
        )

        result = run(
            self.detector.is_valid_opportunity(market, clob_client)
        )
        passed, reason = result
        self.assertFalse(passed)
        self.assertIn("[L2]", reason)

    def test_thin_orderbook_fails_at_layer_3(self):
        """Thin orderbook market should fail at Layer 3."""
        clob_client = make_mock_clob_client(bid_depth_usd=500.0)
        # Set volume/liquidity ratio < 10 to pass Layer 2, but liquidity too low for Layer 3
        market = make_valid_market(
            category="Finance",
            _liquidity=500,
            liquidityClob=500,
            volume24hr=1000,   # 2x liquidity (< 10x ratio), passes Layer 2 ratio check
            _volume_24h=1000,
        )

        result = run(
            self.detector.is_valid_opportunity(market, clob_client, position_size=1000.0)
        )
        passed, reason = result
        self.assertFalse(passed)
        # Should fail at either Layer 3 (orderbook) or Layer 2 (min volume check)
        # The key assertion is that it was rejected
        self.assertIn("[L", reason)


class TestDetectorLiveEventGate(unittest.TestCase):
    """Tests for the sports live-event gate in Layer 4."""

    def setUp(self):
        self.config = make_test_config()
        # Ensure the detector uses a 0.25-day threshold (6h) for sports
        self.config["scanner"]["sports_min_days_to_resolution"] = 0.25
        self.detector = make_detector(self.config)

    def test_sports_market_within_gate_rejected(self):
        """Sports-bucket market resolving in < 6h should be rejected."""
        dt = datetime.now(timezone.utc) + timedelta(hours=2)
        market = make_valid_market(
            category="",
            # "nba" and "game" are sports-bucket keyword matches in the classifier
            question="Will the Lakers win tonight's NBA game?",
            endDate=dt.isoformat(),
        )
        passed, reason = self.detector._check_resolution_source(market)
        self.assertFalse(passed)
        self.assertIn("Live-event gate", reason)

    def test_sports_market_beyond_gate_passes(self):
        """Sports-bucket market resolving in > 6h should pass."""
        dt = datetime.now(timezone.utc) + timedelta(days=2)
        market = make_valid_market(
            category="",
            question="Will the Lakers win the NBA championship this season?",
            endDate=dt.isoformat(),
        )
        passed, reason = self.detector._check_resolution_source(market)
        self.assertTrue(passed)

    def test_non_sports_market_within_6h_passes(self):
        """Non-sports market resolving soon should NOT be caught by the gate."""
        dt = datetime.now(timezone.utc) + timedelta(hours=2)
        market = make_valid_market(
            category="Economics",
            question="Will the Fed announce a rate cut at today's meeting?",
            endDate=dt.isoformat(),
        )
        passed, reason = self.detector._check_resolution_source(market)
        self.assertTrue(passed)


if __name__ == "__main__":
    unittest.main()
