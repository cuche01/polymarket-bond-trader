"""Tests for V4 1.4 dashboard & pipeline-health Discord summary."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from src.dashboard import Dashboard
from src.notifications import Notifier


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestDashboardPipelinePanel(unittest.TestCase):
    def test_renders_pipeline_panel_without_error(self):
        dash = Dashboard({})
        summary = {
            "scans": 288,
            "fetched": 43200,
            "prefilter": 1247,
            "detector": 86,
            "risk": 12,
            "entries": 4,
            "acceptance_rate": 0.012,
            "dry_period_hours": 2.3,
            "top_rejections": [
                ("price_out_of_band", 41953),
                ("liquidity_below_min", 847),
            ],
        }
        panel = dash._make_pipeline_health_panel(summary)
        # Panel is renderable; sanity check that the body contains the numbers
        text = str(panel.renderable)
        self.assertIn("288", text)
        self.assertIn("2.3", text)
        self.assertIn("price_out_of_band", text)

    def test_renders_fee_attribution_panel(self):
        dash = Dashboard({})
        stats = {
            "position_count": 26,
            "gross_revenue": 18.42,
            "legacy_fees": 7.32,
            "actual_fees": 1.08,
        }
        panel = dash._make_fee_attribution_panel(stats)
        text = str(panel.renderable)
        self.assertIn("26", text)
        self.assertIn("18.42", text)
        self.assertIn("6.24", text)  # savings = 7.32 - 1.08


class TestPipelineHealthDiscordSummary(unittest.TestCase):
    def _make_notifier(self):
        cfg = {
            "notifications": {
                "enabled": True,
                "webhook_url": "https://discord.test/webhook",
                "daily_summary": True,
            }
        }
        return Notifier(cfg, paper_mode=True)

    def test_summary_sent_with_top_rejection(self):
        notifier = self._make_notifier()
        summary = {
            "acceptance_rate": 0.012,
            "entries": 4,
            "dry_period_hours": 2.3,
            "top_rejections": [
                ("price_out_of_band", 97),
                ("liquidity_below_min", 3),
            ],
        }
        notifier._post_webhook = AsyncMock(return_value=True)
        ok = _run(notifier.send_pipeline_health_summary(summary, fee_savings_today=0.24))
        self.assertTrue(ok)
        notifier._post_webhook.assert_awaited_once()
        payload = notifier._post_webhook.await_args.args[0]
        embed = payload["embeds"][0]
        self.assertIn("Pipeline Health", embed["title"])
        self.assertIn("[PAPER]", embed["title"])
        names = [f["name"] for f in embed["fields"]]
        self.assertIn("Acceptance rate", names)
        self.assertIn("Top rejection", names)
        self.assertIn("Fee savings (maker)", names)

    def test_summary_skipped_when_daily_summary_disabled(self):
        cfg = {
            "notifications": {
                "enabled": True,
                "webhook_url": "https://discord.test/webhook",
                "daily_summary": False,
            }
        }
        notifier = Notifier(cfg, paper_mode=False)
        notifier._post_webhook = AsyncMock(return_value=True)
        ok = _run(notifier.send_pipeline_health_summary({}))
        self.assertTrue(ok)
        notifier._post_webhook.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
