"""
V4 Phase 0 regression tests:
 - Adaptive sizing must never exceed max_single_market_pct (hard cap).
 - Webhook POST must retry transient 5xx/timeouts and log CRITICAL on
   persistent failure, but not retry 4xx.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.notifications import Notifier
from src.portfolio_manager import PortfolioManager
from src.risk_engine import RiskEngine


def _run(coro):
    return asyncio.run(coro)


def _v4_config():
    """Config matching the V4 P0 fix: adaptive 2.5%-5%, hard cap 6%."""
    return {
        "risk": {
            "max_single_market_pct": 0.06,
            "target_position_pct": 0.04,
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
            "max_entry_price": 0.975,
            "preferred_resolution_hours": 72,
        },
        "adaptive_sizing": {
            "enabled": True,
            "min_size_pct": 0.025,
            "max_size_pct": 0.05,
        },
        "feature_flags": {"adaptive_sizing": True},
    }


class TestAdaptiveSizingWithinHardCap(unittest.TestCase):
    """Regression: adaptive sizing must never exceed the hard cap."""

    def test_max_adaptive_size_below_hard_cap(self):
        portfolio = MagicMock(spec=PortfolioManager)
        engine = RiskEngine(_v4_config(), portfolio)
        hard_cap_pct = _v4_config()["risk"]["max_single_market_pct"]

        balance = 1000.0
        for price in [0.94, 0.95, 0.96, 0.97, 0.975]:
            for days in [0.25, 0.5, 1.0, 3.0, 7.0, 14.0]:
                size = engine.calculate_adaptive_size(price, days, balance)
                self.assertLessEqual(
                    size,
                    balance * hard_cap_pct + 0.01,
                    f"Adaptive size ${size:.2f} at price={price} days={days} "
                    f"exceeds hard cap ${balance * hard_cap_pct:.2f}",
                )

    def test_min_adaptive_size_is_floor_pct(self):
        portfolio = MagicMock(spec=PortfolioManager)
        engine = RiskEngine(_v4_config(), portfolio)
        balance = 1000.0
        size = engine.calculate_adaptive_size(0.94, 14.0, balance)
        self.assertGreaterEqual(size, balance * 0.025 - 0.01)


class TestWebhookRetry(unittest.TestCase):
    """Webhook retry + backoff behavior."""

    def _notifier(self):
        cfg = {
            "notifications": {
                "enabled": True,
                "webhook_url": "https://example.com/webhook",
                "alert_on_trade": True,
            }
        }
        return Notifier(cfg, paper_mode=True)

    def test_retries_on_503_then_succeeds(self):
        n = self._notifier()
        call_count = {"n": 0}

        class MockResp:
            def __init__(self, status):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def text(self):
                return "bad"

        class MockSession:
            def post(self, url, json):
                call_count["n"] += 1
                status = 503 if call_count["n"] < 3 else 204
                return MockResp(status)

        with patch.object(n, "_get_session", new=AsyncMock(return_value=MockSession())):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = _run(n._post_webhook({"test": 1}))
        self.assertTrue(result)
        self.assertEqual(call_count["n"], 3)

    def test_gives_up_after_max_attempts_logs_critical(self):
        n = self._notifier()

        class MockResp:
            status = 500

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def text(self):
                return "internal server error"

        class MockSession:
            def post(self, url, json):
                return MockResp()

        with patch.object(n, "_get_session", new=AsyncMock(return_value=MockSession())):
            with patch("asyncio.sleep", new=AsyncMock()):
                with self.assertLogs("src.notifications", level="CRITICAL") as cm:
                    result = _run(n._post_webhook({"test": 1}))
        self.assertFalse(result)
        self.assertTrue(any("Webhook delivery failed" in m for m in cm.output))

    def test_no_retry_on_4xx(self):
        n = self._notifier()
        call_count = {"n": 0}

        class MockResp:
            status = 400

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def text(self):
                return "bad request"

        class MockSession:
            def post(self, url, json):
                call_count["n"] += 1
                return MockResp()

        with patch.object(n, "_get_session", new=AsyncMock(return_value=MockSession())):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = _run(n._post_webhook({"test": 1}))
        self.assertFalse(result)
        self.assertEqual(call_count["n"], 1)

    def test_retries_on_timeout(self):
        n = self._notifier()
        call_count = {"n": 0}

        class MockSession:
            def post(self, url, json):
                call_count["n"] += 1
                raise asyncio.TimeoutError()

        with patch.object(n, "_get_session", new=AsyncMock(return_value=MockSession())):
            with patch("asyncio.sleep", new=AsyncMock()):
                with self.assertLogs("src.notifications", level="CRITICAL"):
                    result = _run(n._post_webhook({"test": 1}))
        self.assertFalse(result)
        self.assertEqual(call_count["n"], 3)


if __name__ == "__main__":
    unittest.main()
