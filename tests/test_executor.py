"""
Tests for the OrderExecutor module.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.executor import ESTIMATED_FEE_RATE, OrderExecutor


def run(coro):
    """Helper to run async code in tests."""
    return asyncio.run(coro)


def make_test_config() -> dict:
    return {
        "exits": {
            "order_timeout_seconds": 5,  # Short timeout for tests
            "auto_exit_enabled": True,
        },
        "risk": {
            "min_net_yield": 0.01,
        },
    }


def make_market(yes_price: float = 0.97, days: float = 3.0) -> dict:
    """Create a minimal market dictionary for testing."""
    end_date = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    return {
        "id": "test-mkt-001",
        "conditionId": "test-mkt-001",
        "question": "Test market question?",
        "endDate": end_date,
        "_yes_price": yes_price,
        "_yes_token_id": "token-yes-001",
        "_no_token_id": "token-no-001",
        "_bond_score": 0.0012,
        "eventId": "event-001",
    }


def make_position(entry_price: float = 0.97, shares: float = 100.0) -> dict:
    """Create a minimal open position dictionary."""
    return {
        "id": 1,
        "market_id": "test-mkt-001",
        "market_question": "Test market question?",
        "token_id": "token-yes-001",
        "entry_price": entry_price,
        "shares": shares,
        "cost_basis": entry_price * shares,
        "status": "open",
        "fees_paid": 0.0,
    }


class TestPaperModeExecution(unittest.TestCase):
    """Tests for paper mode behavior."""

    def setUp(self):
        self.config = make_test_config()
        self.executor = OrderExecutor(self.config)

    def test_paper_mode_does_not_call_clob_api(self):
        """Paper mode entry should not call any real CLOB API methods."""
        clob_client = MagicMock()
        market = make_market()

        result = run(
            self.executor.execute_entry(
                market=market,
                position_size=1000.0,
                clob_client=clob_client,
                paper_mode=True,
            )
        )

        # In paper mode, no CLOB methods should be called
        clob_client.create_and_post_order.assert_not_called()
        clob_client.get_order.assert_not_called()

        # But a position should be returned
        self.assertIsNotNone(result)
        self.assertEqual(result["paper_trade"], True)
        self.assertTrue(result["order_id"].startswith("PAPER-"))

    def test_paper_mode_exit_does_not_call_clob_api(self):
        """Paper mode exit should not call any real CLOB API methods."""
        clob_client = MagicMock()
        position = make_position()

        result = run(
            self.executor.execute_exit(
                position=position,
                clob_client=clob_client,
                paper_mode=True,
                current_price=1.0,
            )
        )

        clob_client.create_and_post_order.assert_not_called()
        self.assertTrue(result)

    def test_paper_mode_position_has_correct_fields(self):
        """Paper mode position should have all required fields."""
        clob_client = MagicMock()
        market = make_market(yes_price=0.97)

        position = run(
            self.executor.execute_entry(
                market=market,
                position_size=1000.0,
                clob_client=clob_client,
                paper_mode=True,
            )
        )

        required_fields = [
            "market_id", "market_question", "token_id", "entry_price",
            "shares", "cost_basis", "entry_time", "expected_resolution",
            "status", "fees_paid", "bond_score", "paper_trade", "order_id",
        ]
        for field in required_fields:
            self.assertIn(field, position, f"Missing required field: {field}")

    def test_paper_mode_pnl_calculation_on_exit(self):
        """Paper mode exit should correctly calculate P&L."""
        clob_client = MagicMock()
        entry_price = 0.97
        shares = 100.0
        position = make_position(entry_price=entry_price, shares=shares)

        exit_price = 1.0  # Resolution at $1.00

        result = run(
            self.executor.execute_exit(
                position=position,
                clob_client=clob_client,
                paper_mode=True,
                current_price=exit_price,
            )
        )

        self.assertTrue(result)
        # Proceeds = 100 * 1.0 = $100, Cost = 100 * 0.97 = $97
        # Fees ≈ 100 * 1.0 * 0.001 = $0.10
        expected_pnl = shares * exit_price - shares * entry_price - (shares * exit_price * ESTIMATED_FEE_RATE)
        self.assertAlmostEqual(position["pnl"], expected_pnl, delta=0.05)
        self.assertEqual(position["status"], "closed")
        self.assertAlmostEqual(position["exit_price"], exit_price, places=2)


class TestEntryPriceRounding(unittest.TestCase):
    """Tests for price rounding to tick size."""

    def setUp(self):
        self.config = make_test_config()
        self.executor = OrderExecutor(self.config)

    def test_price_rounded_to_tick_size(self):
        """Entry price should be rounded to DEFAULT_TICK_SIZE (0.01)."""
        from src.executor import DEFAULT_TICK_SIZE
        from src.utils import round_to_tick

        # Test that round_to_tick works correctly
        self.assertAlmostEqual(round_to_tick(0.973, DEFAULT_TICK_SIZE), 0.97, places=4)
        self.assertAlmostEqual(round_to_tick(0.976, DEFAULT_TICK_SIZE), 0.98, places=4)
        self.assertAlmostEqual(round_to_tick(0.975, DEFAULT_TICK_SIZE), 0.98, places=4)
        self.assertAlmostEqual(round_to_tick(0.950, DEFAULT_TICK_SIZE), 0.95, places=4)

    def test_paper_mode_uses_rounded_price(self):
        """Paper mode entry should use tick-rounded price."""
        clob_client = MagicMock()
        # Market with price that needs rounding
        market = make_market(yes_price=0.973)

        position = run(
            self.executor.execute_entry(
                market=market,
                position_size=1000.0,
                clob_client=clob_client,
                paper_mode=True,
            )
        )

        self.assertIsNotNone(position)
        # Price should be rounded to nearest 0.01 tick size
        # 0.973 rounds to 0.97
        entry_price = position["entry_price"]
        # Verify price is a multiple of 0.01 within float precision
        rounded = round(entry_price * 100) / 100
        self.assertAlmostEqual(entry_price, rounded, places=4)


class TestOrderTimeout(unittest.TestCase):
    """Tests for order timeout and cancellation."""

    def setUp(self):
        self.config = make_test_config()
        self.executor = OrderExecutor(self.config)

    def test_monitor_fill_returns_none_on_timeout(self):
        """monitor_fill should return (None, None, None) after timeout."""
        clob_client = MagicMock()

        # Mock order that stays in OPEN status (never fills)
        mock_order = MagicMock()
        mock_order.status = "OPEN"
        clob_client.get_order.return_value = mock_order

        import time
        start = time.time()
        result = run(
            self.executor.monitor_fill("order-001", clob_client, timeout_seconds=1)
        )
        elapsed = time.time() - start

        self.assertEqual(result, (None, None, None))
        self.assertGreaterEqual(elapsed, 0.9)  # Should have waited near timeout

    def test_monitor_fill_returns_fill_data_on_success(self):
        """monitor_fill should return fill data when order is filled."""
        clob_client = MagicMock()

        # First call: pending, second call: filled
        pending_order = MagicMock()
        pending_order.status = "OPEN"

        filled_order = MagicMock()
        filled_order.status = "MATCHED"
        filled_order.price = 0.97
        filled_order.size_matched = 100.0

        clob_client.get_order.side_effect = [pending_order, filled_order]

        result = run(
            self.executor.monitor_fill("order-001", clob_client, timeout_seconds=30)
        )

        fill_price, fill_shares, fees = result
        self.assertIsNotNone(fill_price)
        self.assertAlmostEqual(fill_price, 0.97, places=2)
        self.assertAlmostEqual(fill_shares, 100.0, places=1)

    def test_paper_order_id_skips_monitoring(self):
        """Paper order IDs should return immediately without monitoring."""
        clob_client = MagicMock()

        result = run(
            self.executor.monitor_fill("PAPER-abc123", clob_client, timeout_seconds=60)
        )

        # Should return None tuple immediately
        self.assertEqual(result, (None, None, None))
        clob_client.get_order.assert_not_called()


class TestFeeCalculation(unittest.TestCase):
    """Tests for fee calculation and net yield filter."""

    def setUp(self):
        self.config = make_test_config()
        self.executor = OrderExecutor(self.config)

    def test_net_yield_calculation(self):
        """Net yield should account for entry and exit fees."""
        entry_price = 0.97
        shares = 100.0
        resolution_price = 1.0

        net_yield = self.executor.calculate_net_yield(
            entry_price=entry_price,
            shares=shares,
            resolution_price=resolution_price,
        )

        # Gross yield = (1.0 - 0.97) / 0.97 ≈ 3.09%
        # Fees ≈ 2 * 0.1% = 0.2%
        # Net ≈ 2.89%
        self.assertGreater(net_yield, 0.01)  # Above minimum
        self.assertLess(net_yield, 0.04)     # Reasonable upper bound

    def test_min_yield_filter_rejects_low_yield(self):
        """Executor should reject entries with net yield below minimum."""
        clob_client = MagicMock()
        # Price very close to 1.00 → almost no yield
        market = make_market(yes_price=0.9990)

        position = run(
            self.executor.execute_entry(
                market=market,
                position_size=1000.0,
                clob_client=clob_client,
                paper_mode=True,
            )
        )

        # Should be rejected due to insufficient net yield
        self.assertIsNone(position)

    def test_cancel_paper_order_returns_true(self):
        """Cancelling a paper order should always succeed."""
        clob_client = MagicMock()
        result = run(
            self.executor.cancel_order("PAPER-test123", clob_client)
        )
        self.assertTrue(result)
        clob_client.cancel.assert_not_called()

    def test_cancel_real_order_calls_clob(self):
        """Cancelling a real order should call CLOB cancel method."""
        clob_client = MagicMock()
        clob_client.cancel.return_value = {"status": "cancelled"}

        result = run(
            self.executor.cancel_order("real-order-001", clob_client)
        )

        clob_client.cancel.assert_called_once_with("real-order-001")
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
