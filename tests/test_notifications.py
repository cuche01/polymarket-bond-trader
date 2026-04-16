"""
Tests for Notifier.send_performance_summary embed construction.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.notifications import Notifier


def run(coro):
    return asyncio.run(coro)


def make_notifier(enabled=True, performance_summary=True, paper_mode=True):
    config = {
        "notifications": {
            "enabled": enabled,
            "webhook_url": "https://example.com/webhook",
            "performance_summary": performance_summary,
            "daily_summary": True,
        }
    }
    return Notifier(config, paper_mode=paper_mode)


class TestSendPerformanceSummary(unittest.TestCase):
    """Tests for Notifier.send_performance_summary()."""

    def test_disabled_flag_skips_send(self):
        n = make_notifier(performance_summary=False)
        with patch.object(n, "_post_webhook", new=AsyncMock(return_value=True)) as mock_post:
            result = run(n.send_performance_summary({"closed_trades": 5}))
            self.assertTrue(result)
            mock_post.assert_not_called()

    def test_empty_summary_sends_minimal_embed(self):
        n = make_notifier()
        with patch.object(n, "_post_webhook", new=AsyncMock(return_value=True)) as mock_post:
            run(n.send_performance_summary({
                "closed_trades": 0,
                "open_trades": 3,
            }))
            mock_post.assert_called_once()
            payload = mock_post.call_args[0][0]
            embed = payload["embeds"][0]
            self.assertIn("No closed trades yet", embed["description"])
            self.assertIn("[PAPER]", embed["title"])

    def test_full_summary_embed_structure(self):
        n = make_notifier()
        summary = {
            "total_trades": 10,
            "closed_trades": 8,
            "open_trades": 2,
            "wins": 6,
            "losses": 2,
            "win_rate": 75.0,
            "total_pnl": 42.50,
            "total_cost_basis": 800.0,
            "roi_on_deployed": 5.31,
            "fees_paid": 2.0,
            "avg_win": 8.0,
            "avg_loss": -3.75,
            "avg_win_pct": 4.2,
            "avg_loss_pct": -2.0,
            "max_win": 15.0,
            "max_loss": -5.0,
            "profit_factor": 2.5,
            "rr_ratio": 2.13,
            "expectancy": 4.31,
            "max_consecutive_wins": 4,
            "max_consecutive_losses": 1,
            "peak_cum_pnl": 50.0,
            "max_drawdown_from_peak": 7.5,
            "avg_hold_hours": 12.3,
            "exit_reason_breakdown": {
                "resolution_win": 4,
                "take_profit": 2,
                "stop_loss": 2,
            },
        }
        with patch.object(n, "_post_webhook", new=AsyncMock(return_value=True)) as mock_post:
            run(n.send_performance_summary(summary))
            mock_post.assert_called_once()
            payload = mock_post.call_args[0][0]
            embed = payload["embeds"][0]

            # Title tag + date
            self.assertIn("[PAPER]", embed["title"])
            self.assertIn("Performance Summary", embed["title"])

            # Flatten fields by name for lookup
            field_by_name = {f["name"]: f["value"] for f in embed["fields"]}

            self.assertEqual(field_by_name["Closed Trades"], "8")
            self.assertIn("75.0%", field_by_name["Win Rate"])
            self.assertIn("6W/2L", field_by_name["Win Rate"])
            self.assertIn("$+42.50", field_by_name["Total P&L"])
            self.assertIn("+5.31%", field_by_name["ROI on Deployed"])
            self.assertEqual(field_by_name["Max Win Streak"], "4")
            self.assertEqual(field_by_name["Max Loss Streak"], "1")
            self.assertIn("2.50", field_by_name["Profit Factor"])
            self.assertIn("2.13", field_by_name["Avg R:R"])
            self.assertIn("$+4.31", field_by_name["Expectancy / Trade"])

            # Exit reasons joined multi-line
            exit_reasons_field = next(
                f for f in embed["fields"] if f["name"] == "Exit Reasons"
            )
            self.assertIn("resolution_win: 4", exit_reasons_field["value"])
            self.assertIn("take_profit: 2", exit_reasons_field["value"])
            self.assertIn("stop_loss: 2", exit_reasons_field["value"])
            self.assertFalse(exit_reasons_field["inline"])

    def test_loss_summary_uses_red_color(self):
        n = make_notifier()
        summary = {
            "closed_trades": 3, "open_trades": 0,
            "wins": 1, "losses": 2, "win_rate": 33.3,
            "total_pnl": -12.0, "total_cost_basis": 200.0,
            "roi_on_deployed": -6.0, "fees_paid": 0.5,
            "avg_win": 4.0, "avg_loss": -8.0,
            "avg_win_pct": 2.0, "avg_loss_pct": -4.0,
            "max_win": 4.0, "max_loss": -10.0,
            "profit_factor": 0.25, "rr_ratio": 0.5, "expectancy": -4.0,
            "max_consecutive_wins": 1, "max_consecutive_losses": 2,
            "peak_cum_pnl": 4.0, "max_drawdown_from_peak": 16.0,
            "avg_hold_hours": 6.0,
            "exit_reason_breakdown": {"stop_loss": 2, "resolution_win": 1},
        }
        with patch.object(n, "_post_webhook", new=AsyncMock(return_value=True)) as mock_post:
            run(n.send_performance_summary(summary))
            payload = mock_post.call_args[0][0]
            embed = payload["embeds"][0]
            # Red color code
            self.assertEqual(embed["color"], 0xE74C3C)


if __name__ == "__main__":
    unittest.main()
