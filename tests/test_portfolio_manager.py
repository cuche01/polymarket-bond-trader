"""
Tests for PortfolioManager.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from src.database import Database
from src.portfolio_manager import PortfolioManager


def _make_portfolio(tmp_path=None):
    if tmp_path is None:
        tmp_path = tempfile.mkdtemp()
    db = Database(os.path.join(tmp_path, "test.db"))
    pm = PortfolioManager(db)
    pm.set_portfolio_balance(10000.0, paper_mode=True)
    return pm, db


def _open_position(db, market_id, cost_basis, category="Sports", risk_bucket="sports",
                   event_group_id=""):
    pos = {
        "market_id": market_id,
        "market_question": f"Question for {market_id}",
        "token_id": f"tok-{market_id}",
        "entry_price": 0.97,
        "shares": cost_basis / 0.97,
        "cost_basis": cost_basis,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "fees_paid": 0.0,
        "bond_score": 0.001,
        "paper_trade": True,
        "order_id": f"PAPER-{market_id}",
        "event_id": event_group_id,
        "category": category,
        "event_group_id": event_group_id,
        "risk_bucket": risk_bucket,
        "capital_state": "deployed",
    }
    return db.save_position(pos)


def _close_position(db, pos_id, pnl):
    db.update_position(pos_id, {
        "status": "closed",
        "exit_price": 1.0 if pnl > 0 else 0.5,
        "exit_time": datetime.now(timezone.utc).isoformat(),
        "pnl": pnl,
        "fees_paid": 0.5,
    })


class TestGetTotalDeployed(unittest.TestCase):

    def test_sum_of_all_open_position_cost_bases(self):
        """get_total_deployed returns sum of all open position cost bases."""
        pm, db = _make_portfolio()
        _open_position(db, "mkt-a", 300.0)
        _open_position(db, "mkt-b", 200.0)
        self.assertAlmostEqual(pm.get_total_deployed(), 500.0)


class TestCategoryExposure(unittest.TestCase):

    def test_correct_sum_for_specific_category(self):
        """get_category_exposure returns only positions in that category."""
        pm, db = _make_portfolio()
        _open_position(db, "mkt-1", 300.0, category="Sports")
        _open_position(db, "mkt-2", 200.0, category="Crypto")
        self.assertAlmostEqual(pm.get_category_exposure("Sports"), 300.0)
        self.assertAlmostEqual(pm.get_category_exposure("Crypto"), 200.0)
        self.assertAlmostEqual(pm.get_category_exposure("Politics"), 0.0)


class TestConsecutiveLosses(unittest.TestCase):

    def test_zero_when_last_trade_was_win(self):
        """get_consecutive_losses returns 0 when last trade was a win."""
        pm, db = _make_portfolio()
        pos_id = _open_position(db, "mkt-w", 200.0)
        _close_position(db, pos_id, pnl=5.0)  # win
        self.assertEqual(pm.get_consecutive_losses(), 0)

    def test_3_when_last_3_were_losses(self):
        """get_consecutive_losses returns 3 when last 3 were losses."""
        pm, db = _make_portfolio()
        for i in range(3):
            pos_id = _open_position(db, f"mkt-l{i}", 200.0)
            _close_position(db, pos_id, pnl=-10.0)  # loss
        self.assertEqual(pm.get_consecutive_losses(), 3)

    def test_stops_at_first_win(self):
        """If last was win, losses before it don't count."""
        pm, db = _make_portfolio()
        pos_id = _open_position(db, "mkt-loss1", 200.0)
        _close_position(db, pos_id, pnl=-10.0)
        pos_id2 = _open_position(db, "mkt-win", 200.0)
        _close_position(db, pos_id2, pnl=5.0)
        self.assertEqual(pm.get_consecutive_losses(), 0)


class TestWeakestPositions(unittest.TestCase):

    def test_returns_correct_sort_order(self):
        """get_weakest_positions returns worst performers first by unrealized P&L%."""
        pm, db = _make_portfolio()
        pos_good = {"entry_price": 0.97, "_current_price": 0.99, "id": 1, "shares": 100}
        pos_bad = {"entry_price": 0.97, "_current_price": 0.90, "id": 2, "shares": 100}
        pos_neutral = {"entry_price": 0.97, "_current_price": 0.97, "id": 3, "shares": 100}

        positions = [pos_good, pos_bad, pos_neutral]
        weakest = pm.get_weakest_positions(2, positions)
        self.assertEqual(len(weakest), 2)
        self.assertEqual(weakest[0]["id"], 2)  # worst first


class TestPortfolioDrawdown(unittest.TestCase):

    def test_drawdown_computed_from_peak(self):
        """Drawdown is negative when current value < peak."""
        pm, db = _make_portfolio()
        pm.set_portfolio_balance(10000.0)
        pm._peak_portfolio_value = 11000.0  # simulate prior peak
        drawdown = pm.get_portfolio_drawdown_pct([])
        self.assertLess(drawdown, 0)
        self.assertAlmostEqual(drawdown, -1000.0 / 11000.0)

    def test_no_drawdown_when_at_peak(self):
        """No drawdown when current equals peak."""
        pm, db = _make_portfolio()
        pm.set_portfolio_balance(10000.0)
        pm._peak_portfolio_value = 10000.0
        drawdown = pm.get_portfolio_drawdown_pct([])
        self.assertAlmostEqual(drawdown, 0.0)


class TestSchemaMigration(unittest.TestCase):

    def test_migration_runs_cleanly_on_fresh_db(self):
        """Schema migration runs without error on a new database."""
        tmp = tempfile.mkdtemp()
        db = Database(os.path.join(tmp, "fresh.db"))
        # If we get here without exception, migration passed
        self.assertTrue(True)

    def test_migration_is_idempotent_on_existing_db(self):
        """Schema migration runs cleanly a second time (columns already exist)."""
        tmp = tempfile.mkdtemp()
        db1 = Database(os.path.join(tmp, "existing.db"))
        db2 = Database(os.path.join(tmp, "existing.db"))  # re-opens same file
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
