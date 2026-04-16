"""Tests for P0: Orderbook imbalance monitor."""

import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock

from src.orderbook_monitor import OrderbookMonitor


def _make_monitor(config_overrides=None):
    config = {
        "orderbook_monitor": {
            "orderbook_monitor_interval_seconds": 20,
            "warning_bid_depth_ratio": 2.0,
            "critical_bid_depth_ratio": 1.0,
            "exit_bid_depth_ratio": 0.5,
            "bid_wall_pull_threshold": 0.30,
        }
    }
    if config_overrides:
        config["orderbook_monitor"].update(config_overrides)

    notifier = AsyncMock()
    return OrderbookMonitor(config, notifier), notifier


def _mock_orderbook(bids_usd_pairs):
    """Create a mock orderbook with bids as (price, size) pairs."""
    ob = MagicMock()
    bids = []
    for price, size in bids_usd_pairs:
        bid = MagicMock()
        bid.price = price
        bid.size = size
        bids.append(bid)
    ob.bids = bids
    ob.asks = []
    return ob


class TestOrderbookMonitor(unittest.TestCase):

    def test_healthy_orderbook_no_exit(self):
        """Bids totaling $200 for a $100 position (ratio 2.0) -> WARNING only, no exit."""
        monitor, notifier = _make_monitor()
        clob = MagicMock()
        clob.get_order_book.return_value = _mock_orderbook([(0.95, 210.5)])

        position = {"id": 1, "token_id": "tok-1", "cost_basis": 100, "paper_trade": 0}

        result = asyncio.run(monitor._check_position(position, clob))
        self.assertIsNone(result)

    def test_thin_orderbook_exit_signal(self):
        """Bids totaling $40 for $100 position (ratio 0.4), bid_wall_change 0.25 -> EXIT."""
        monitor, notifier = _make_monitor()
        clob = MagicMock()

        # First call: establish baseline depth of $160
        clob.get_order_book.return_value = _mock_orderbook([(0.80, 200)])
        position = {"id": 1, "token_id": "tok-1", "cost_basis": 100, "paper_trade": 0}
        asyncio.run(monitor._check_position(position, clob))

        # Second call: depth dropped to $40 (wall_change = 40/160 = 0.25)
        clob.get_order_book.return_value = _mock_orderbook([(0.80, 50)])
        result = asyncio.run(monitor._check_position(position, clob))
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "exit")
        self.assertEqual(result["reason"], "orderbook_imbalance_exit")

    def test_paper_mode_skips_all_checks(self):
        """Paper mode positions are skipped."""
        monitor, notifier = _make_monitor()

        positions = [{"id": 1, "token_id": "tok-1", "cost_basis": 100, "paper_trade": 1}]
        result = asyncio.run(monitor.run_cycle(positions, clob_client=MagicMock()))
        self.assertEqual(result, [])

    def test_no_clob_client_skips(self):
        """No CLOB client -> skip all checks."""
        monitor, notifier = _make_monitor()
        positions = [{"id": 1, "token_id": "tok-1", "cost_basis": 100}]
        result = asyncio.run(monitor.run_cycle(positions, clob_client=None))
        self.assertEqual(result, [])

    def test_cleanup_position_removes_cache(self):
        """Cleanup removes cached depth for a position."""
        monitor, _ = _make_monitor()
        monitor._prev_depths[42] = 5000.0
        monitor.cleanup_position(42)
        self.assertNotIn(42, monitor._prev_depths)


if __name__ == "__main__":
    unittest.main()
