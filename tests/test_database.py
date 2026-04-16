"""
Tests for Database.get_performance_summary aggregation.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import Database


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestGetPerformanceSummary(unittest.TestCase):
    """Tests for Database.get_performance_summary()."""

    def setUp(self):
        # Use a temp file so WAL mode works (sqlite :memory: doesn't play well
        # with the WAL pragma set in Database._get_connection).
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(db_path=self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _seed(self, positions):
        """Insert fixture positions directly via SQL."""
        with self.db._get_connection() as conn:
            for i, p in enumerate(positions):
                conn.execute(
                    """
                    INSERT INTO positions (
                        id, market_id, market_question, token_id, entry_price,
                        shares, cost_basis, entry_time, status, exit_price,
                        exit_time, pnl, fees_paid, paper_trade, exit_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        i + 1,
                        p.get("market_id", f"m{i}"),
                        p.get("question", f"Question {i}?"),
                        p.get("token_id", f"t{i}"),
                        p["entry_price"],
                        p["shares"],
                        p["cost_basis"],
                        p["entry_time"],
                        p["status"],
                        p.get("exit_price"),
                        p.get("exit_time"),
                        p.get("pnl"),
                        p.get("fees_paid", 0.0),
                        1 if p.get("paper_trade", True) else 0,
                        p.get("exit_reason"),
                    ),
                )

    def test_empty_database_returns_zero_defaults(self):
        summary = self.db.get_performance_summary()
        self.assertEqual(summary["closed_trades"], 0)
        self.assertEqual(summary["total_trades"], 0)
        self.assertEqual(summary["wins"], 0)
        self.assertEqual(summary["losses"], 0)
        self.assertEqual(summary["win_rate"], 0.0)
        self.assertEqual(summary["total_pnl"], 0.0)
        self.assertEqual(summary["profit_factor"], 0.0)
        self.assertEqual(summary["expectancy"], 0.0)
        self.assertEqual(summary["max_consecutive_wins"], 0)
        self.assertEqual(summary["max_consecutive_losses"], 0)
        self.assertEqual(summary["exit_reason_breakdown"], {})

    def test_all_wins(self):
        base = datetime(2026, 4, 1, tzinfo=timezone.utc)
        self._seed([
            {
                "entry_price": 0.95, "shares": 100, "cost_basis": 95.0,
                "entry_time": _iso(base), "status": "closed",
                "exit_price": 1.0, "exit_time": _iso(base + timedelta(hours=2)),
                "pnl": 5.0, "fees_paid": 0.1, "exit_reason": "resolution_win",
            },
            {
                "entry_price": 0.96, "shares": 100, "cost_basis": 96.0,
                "entry_time": _iso(base + timedelta(hours=3)), "status": "closed",
                "exit_price": 1.0, "exit_time": _iso(base + timedelta(hours=4)),
                "pnl": 4.0, "fees_paid": 0.1, "exit_reason": "resolution_win",
            },
        ])
        s = self.db.get_performance_summary()
        self.assertEqual(s["closed_trades"], 2)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 0)
        self.assertEqual(s["win_rate"], 100.0)
        self.assertAlmostEqual(s["total_pnl"], 9.0)
        self.assertAlmostEqual(s["avg_win"], 4.5)
        self.assertEqual(s["max_consecutive_wins"], 2)
        self.assertEqual(s["max_consecutive_losses"], 0)
        # No losses => profit factor should be 0 (div-by-zero guard)
        self.assertEqual(s["profit_factor"], 0.0)
        # R:R also 0 when no losses
        self.assertEqual(s["rr_ratio"], 0.0)
        self.assertEqual(s["exit_reason_breakdown"], {"resolution_win": 2})

    def test_mixed_wins_and_losses(self):
        base = datetime(2026, 4, 1, tzinfo=timezone.utc)
        # Sequence: W, W, L, W, L, L, W — 4W / 3L
        # pnls:     +10 +5 -8 +6 -4 -10 +3
        seq = [
            (10.0, "resolution_win"),
            (5.0, "take_profit"),
            (-8.0, "stop_loss"),
            (6.0, "resolution_win"),
            (-4.0, "trailing_stop"),
            (-10.0, "stop_loss"),
            (3.0, "take_profit"),
        ]
        positions = []
        for i, (pnl, reason) in enumerate(seq):
            positions.append({
                "entry_price": 0.95, "shares": 100, "cost_basis": 95.0,
                "entry_time": _iso(base + timedelta(hours=i * 2)),
                "status": "closed",
                "exit_price": 0.95 + pnl / 100,
                "exit_time": _iso(base + timedelta(hours=i * 2 + 1)),
                "pnl": pnl, "fees_paid": 0.1, "exit_reason": reason,
            })
        self._seed(positions)
        s = self.db.get_performance_summary()

        self.assertEqual(s["closed_trades"], 7)
        self.assertEqual(s["wins"], 4)
        self.assertEqual(s["losses"], 3)
        self.assertAlmostEqual(s["win_rate"], 4 / 7 * 100.0)
        self.assertAlmostEqual(s["total_pnl"], 2.0)  # 24 - 22
        self.assertAlmostEqual(s["avg_win"], 24.0 / 4)  # 6.0
        self.assertAlmostEqual(s["avg_loss"], -22.0 / 3)
        self.assertAlmostEqual(s["max_win"], 10.0)
        self.assertAlmostEqual(s["max_loss"], -10.0)
        # Profit factor = gross_wins / gross_losses = 24 / 22
        self.assertAlmostEqual(s["profit_factor"], 24.0 / 22.0, places=5)
        # Streaks: WW -> 2, then L, W, LL -> 2
        self.assertEqual(s["max_consecutive_wins"], 2)
        self.assertEqual(s["max_consecutive_losses"], 2)
        # Exit reason breakdown
        self.assertEqual(s["exit_reason_breakdown"], {
            "resolution_win": 2,
            "take_profit": 2,
            "stop_loss": 2,
            "trailing_stop": 1,
        })
        # Expectancy = win_rate * avg_win - (1-win_rate) * |avg_loss|
        expected_expectancy = (4 / 7) * 6.0 - (3 / 7) * (22.0 / 3)
        self.assertAlmostEqual(s["expectancy"], expected_expectancy, places=5)

    def test_drawdown_calculation(self):
        base = datetime(2026, 4, 1, tzinfo=timezone.utc)
        # Cumulative: +10, +20 (peak), +15, +5, -5, so peak=20, final=-5, maxDD=25
        pnls = [10.0, 10.0, -5.0, -10.0, -10.0]
        positions = [
            {
                "entry_price": 0.95, "shares": 100, "cost_basis": 95.0,
                "entry_time": _iso(base + timedelta(hours=i * 2)),
                "status": "closed",
                "exit_price": 0.95 + p / 100,
                "exit_time": _iso(base + timedelta(hours=i * 2 + 1)),
                "pnl": p, "fees_paid": 0.0,
                "exit_reason": "take_profit" if p > 0 else "stop_loss",
            }
            for i, p in enumerate(pnls)
        ]
        self._seed(positions)
        s = self.db.get_performance_summary()
        self.assertAlmostEqual(s["peak_cum_pnl"], 20.0)
        self.assertAlmostEqual(s["max_drawdown_from_peak"], 25.0)
        self.assertAlmostEqual(s["total_pnl"], -5.0)

    def test_hold_duration_average(self):
        base = datetime(2026, 4, 1, tzinfo=timezone.utc)
        self._seed([
            {
                "entry_price": 0.95, "shares": 100, "cost_basis": 95.0,
                "entry_time": _iso(base), "status": "closed",
                "exit_price": 1.0, "exit_time": _iso(base + timedelta(hours=4)),
                "pnl": 5.0, "exit_reason": "resolution_win",
            },
            {
                "entry_price": 0.95, "shares": 100, "cost_basis": 95.0,
                "entry_time": _iso(base), "status": "closed",
                "exit_price": 1.0, "exit_time": _iso(base + timedelta(hours=6)),
                "pnl": 5.0, "exit_reason": "resolution_win",
            },
        ])
        s = self.db.get_performance_summary()
        self.assertAlmostEqual(s["avg_hold_hours"], 5.0)

    def test_paper_trade_filter(self):
        base = datetime(2026, 4, 1, tzinfo=timezone.utc)
        self._seed([
            {
                "entry_price": 0.95, "shares": 100, "cost_basis": 95.0,
                "entry_time": _iso(base), "status": "closed",
                "exit_price": 1.0, "exit_time": _iso(base + timedelta(hours=1)),
                "pnl": 5.0, "paper_trade": True, "exit_reason": "win",
            },
            {
                "entry_price": 0.95, "shares": 100, "cost_basis": 95.0,
                "entry_time": _iso(base), "status": "closed",
                "exit_price": 0.85, "exit_time": _iso(base + timedelta(hours=1)),
                "pnl": -10.0, "paper_trade": False, "exit_reason": "stop_loss",
            },
        ])
        paper = self.db.get_performance_summary(paper_trade=True)
        live = self.db.get_performance_summary(paper_trade=False)
        self.assertEqual(paper["closed_trades"], 1)
        self.assertAlmostEqual(paper["total_pnl"], 5.0)
        self.assertEqual(live["closed_trades"], 1)
        self.assertAlmostEqual(live["total_pnl"], -10.0)

    def test_open_positions_counted_separately(self):
        base = datetime(2026, 4, 1, tzinfo=timezone.utc)
        self._seed([
            {
                "entry_price": 0.95, "shares": 100, "cost_basis": 95.0,
                "entry_time": _iso(base), "status": "open",
            },
            {
                "entry_price": 0.96, "shares": 100, "cost_basis": 96.0,
                "entry_time": _iso(base), "status": "closed",
                "exit_price": 1.0, "exit_time": _iso(base + timedelta(hours=1)),
                "pnl": 4.0, "exit_reason": "take_profit",
            },
        ])
        s = self.db.get_performance_summary()
        self.assertEqual(s["total_trades"], 2)
        self.assertEqual(s["closed_trades"], 1)
        self.assertEqual(s["open_trades"], 1)


if __name__ == "__main__":
    unittest.main()
