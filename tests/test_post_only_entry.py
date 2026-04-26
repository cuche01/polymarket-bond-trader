"""Tests for V4 1.3 post-only limit order entries."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.executor import OrderExecutor


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _base_config(**overrides):
    cfg = {
        "exits": {"order_timeout_seconds": 60},
        "risk": {"min_net_yield": 0.01},
        "fees": {
            "use_dynamic_fees": True,
            "assume_entry_maker": True,
            "assume_exit_taker": True,
        },
        "executor": {
            "entry_strategy": "post_only_ladder",
            "post_only_max_attempts": 3,
            "post_only_retry_wait_sec": 1,
            "allow_taker_fallback": False,
            "tick_size": 0.01,
        },
    }
    cfg["executor"].update(overrides.get("executor", {}))
    return cfg


class TestLadderPrice(unittest.TestCase):
    def test_first_attempt_uses_best_bid(self):
        executor = OrderExecutor(_base_config())
        market = {"_orderbook": {"bids": [{"price": 0.95}], "asks": [{"price": 0.97}]}}
        price = executor._compute_post_only_ladder_price(market, 0, 0.96, 0.01)
        self.assertAlmostEqual(price, 0.95, places=4)

    def test_second_attempt_adds_one_tick(self):
        executor = OrderExecutor(_base_config())
        market = {"_orderbook": {"bids": [{"price": 0.95}], "asks": [{"price": 0.97}]}}
        price = executor._compute_post_only_ladder_price(market, 1, 0.96, 0.01)
        self.assertAlmostEqual(price, 0.96, places=4)

    def test_third_attempt_steps_into_spread(self):
        executor = OrderExecutor(_base_config())
        market = {"_orderbook": {"bids": [{"price": 0.95}], "asks": [{"price": 0.97}]}}
        price = executor._compute_post_only_ladder_price(market, 2, 0.96, 0.01)
        # midpoint=0.96, minus tick=0.95
        self.assertAlmostEqual(price, 0.95, places=4)

    def test_price_never_exceeds_target(self):
        executor = OrderExecutor(_base_config())
        market = {"_orderbook": {"bids": [{"price": 0.98}], "asks": [{"price": 1.00}]}}
        target = 0.96
        # best_bid (0.98) is already over target → clamp
        for attempt in range(3):
            price = executor._compute_post_only_ladder_price(market, attempt, target, 0.01)
            self.assertLessEqual(price, target)


class TestPostOnlyEntry(unittest.TestCase):
    def _make_market(self):
        return {
            "id": "mkt123",
            "question": "Test?",
            "_yes_price": 0.96,
            "_yes_token_id": "token-yes",
            "_no_token_id": "token-no",
            "_orderbook": {"bids": [{"price": 0.95}], "asks": [{"price": 0.97}]},
            "_fee_schedule": {"feesEnabled": True, "takerFeeCoefficient": 0.01},
            "category": "Politics",
        }

    def test_paper_mode_simulates_maker_fill(self):
        executor = OrderExecutor(_base_config())
        market = self._make_market()
        position = _run(
            executor.execute_entry(market, 40.0, clob_client=None, paper_mode=True)
        )
        self.assertIsNotNone(position)
        self.assertEqual(position["fees_paid"], 0.0)
        self.assertTrue(position["order_id"].startswith("PAPER-"))

    def test_ladder_skips_when_all_attempts_fail(self):
        executor = OrderExecutor(_base_config())
        market = self._make_market()
        clob = MagicMock()
        # Each placement returns rejected_would_cross
        clob.create_and_post_order.return_value = SimpleNamespace(
            status="rejected_would_cross", orderID=None
        )
        position = _run(
            executor.execute_entry(market, 40.0, clob_client=clob, paper_mode=False)
        )
        self.assertIsNone(position)
        # Tried all three attempts
        self.assertEqual(clob.create_and_post_order.call_count, 3)

    def test_no_taker_fallback_when_flag_off(self):
        executor = OrderExecutor(_base_config())
        self.assertFalse(executor.allow_taker_fallback)
        market = self._make_market()
        clob = MagicMock()
        clob.create_and_post_order.return_value = SimpleNamespace(
            status="rejected_would_cross", orderID=None
        )
        position = _run(
            executor.execute_entry(market, 40.0, clob_client=clob, paper_mode=False)
        )
        self.assertIsNone(position)
        # Exactly N attempts, no extra taker call
        self.assertEqual(clob.create_and_post_order.call_count, 3)

    def test_post_only_flag_present_in_orders(self):
        executor = OrderExecutor(_base_config())
        market = self._make_market()
        clob = MagicMock()
        clob.create_and_post_order.return_value = SimpleNamespace(
            status="rejected_would_cross", orderID=None
        )
        _run(executor.execute_entry(market, 40.0, clob_client=clob, paper_mode=False))
        for call in clob.create_and_post_order.call_args_list:
            order = call.args[0]
            self.assertTrue(order.get("post_only"))
            self.assertEqual(order["side"], "BUY")


if __name__ == "__main__":
    unittest.main()
