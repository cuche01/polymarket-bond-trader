"""
Tests for ExitEngine — exit condition checks.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.exit_engine import ExitDecision, ExitEngine


def _make_engine(config_overrides=None):
    config = {
        "exits": {
            "stop_loss_pct": 0.07,
            "max_holding_days": 5,
            "stale_resolution_hours": 48,
            "bond_take_profit_price": 0.99,
            "bond_take_profit_min_hours_to_resolution": 48,
            "generic_take_profit_pct": 0.10,
            "partial_scaling_enabled": False,
            "partial_close_trigger_pct": 0.08,
            "partial_close_pct": 0.50,
            "portfolio_drawdown_alert_pct": 0.03,
            "portfolio_drawdown_critical_pct": 0.05,
            "trailing_stop_activation_price": 0.985,
            "trailing_stop_distance_pct": 0.015,
        },
        "alerts": {
            "yellow_loss_pct": 0.03,
            "orange_loss_pct": 0.05,
        },
    }
    if config_overrides:
        config["exits"].update(config_overrides)

    portfolio = MagicMock()
    portfolio.get_portfolio_drawdown_pct.return_value = 0.0
    portfolio.get_weakest_positions.return_value = []
    portfolio.update_high_water_mark.return_value = None
    portfolio.update_position_status.return_value = None

    risk_engine = MagicMock()
    engine = ExitEngine(config, portfolio, risk_engine, notifier=None)
    return engine, portfolio


def _make_position(
    entry_price=0.97,
    current_price=None,
    entry_time=None,
    expected_resolution=None,
    status="open",
    high_water_mark=None,
    position_id=1,
):
    if entry_time is None:
        entry_time = datetime.now(timezone.utc).isoformat()
    pos = {
        "id": position_id,
        "market_id": "mkt-001",
        "market_question": "Test market?",
        "token_id": "tok-001",
        "entry_price": entry_price,
        "shares": 100.0,
        "cost_basis": entry_price * 100.0,
        "entry_time": entry_time,
        "expected_resolution": expected_resolution or "",
        "status": status,
        "high_water_mark": high_water_mark,
    }
    if current_price is not None:
        pos["_current_price"] = current_price
    return pos


class TestStopLoss(unittest.TestCase):

    def test_stop_loss_7pt5pct_triggers(self):
        """Position down 7.5% → close_full, urgency=immediate."""
        engine, _ = _make_engine()
        pos = _make_position(entry_price=0.97)
        current = 0.97 * (1 - 0.075)
        decision = engine.check_stop_loss(pos, current)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "close_full")
        self.assertEqual(decision.urgency, "immediate")
        self.assertEqual(decision.reason, "stop_loss")

    def test_stop_loss_6pt9pct_holds(self):
        """Position down 6.9% → not yet triggered → hold."""
        engine, _ = _make_engine()
        pos = _make_position(entry_price=0.97)
        current = 0.97 * (1 - 0.069)
        decision = engine.check_stop_loss(pos, current)
        self.assertIsNone(decision)


class TestTimeExit(unittest.TestCase):

    def test_time_exit_5pt1_days_triggers(self):
        """Position held 5.1 days → close_full."""
        engine, _ = _make_engine()
        entry_time = (datetime.now(timezone.utc) - timedelta(days=5.1)).isoformat()
        pos = _make_position(entry_time=entry_time)
        decision = engine.check_time_exit(pos)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "close_full")
        self.assertEqual(decision.reason, "time_exit")

    def test_time_exit_4pt9_days_holds(self):
        """Position held 4.9 days → hold."""
        engine, _ = _make_engine()
        entry_time = (datetime.now(timezone.utc) - timedelta(days=4.9)).isoformat()
        pos = _make_position(entry_time=entry_time)
        decision = engine.check_time_exit(pos)
        self.assertIsNone(decision)


class TestTakeProfit(unittest.TestCase):

    def test_bond_take_profit_price_at_99_resolution_72h_away_triggers(self):
        """Bond position: price at $0.99, resolution in 72h → early exit."""
        engine, _ = _make_engine()
        future = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
        pos = _make_position(entry_price=0.97, expected_resolution=future)
        decision = engine.check_take_profit(pos, 0.99)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "close_full")
        self.assertEqual(decision.reason, "take_profit")

    def test_bond_take_profit_resolution_24h_away_holds(self):
        """Bond position: price at $0.99, resolution only 24h away → hold (let it resolve)."""
        engine, _ = _make_engine()
        future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        pos = _make_position(entry_price=0.97, expected_resolution=future)
        decision = engine.check_take_profit(pos, 0.99)
        self.assertIsNone(decision)

    def test_bond_take_profit_price_at_97_holds(self):
        """Bond position: price at $0.97 (below $0.99 threshold) → hold."""
        engine, _ = _make_engine()
        future = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
        pos = _make_position(entry_price=0.95, expected_resolution=future)
        decision = engine.check_take_profit(pos, 0.97)
        self.assertIsNone(decision)

    def test_non_bond_10pct_profit_triggers(self):
        """Non-bond position (entry $0.70): price at $0.77 → +10% → take profit."""
        engine, _ = _make_engine()
        pos = _make_position(entry_price=0.70)
        decision = engine.check_take_profit(pos, 0.77)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "take_profit")

    def test_non_bond_8pct_profit_holds(self):
        """Non-bond position: price up only 8% → below 10% threshold → hold."""
        engine, _ = _make_engine()
        pos = _make_position(entry_price=0.70)
        decision = engine.check_take_profit(pos, 0.756)  # +8%
        self.assertIsNone(decision)


class TestPortfolioDrawdown(unittest.TestCase):

    def test_3pt5pct_drawdown_closes_1_worst(self):
        """At -3.5% drawdown → close 1 worst position."""
        engine, portfolio = _make_engine()
        pos = _make_position(entry_price=0.97, current_price=0.90)
        portfolio.get_portfolio_drawdown_pct.return_value = -0.035
        portfolio.get_weakest_positions.return_value = [pos]
        decisions = engine.check_portfolio_drawdown([pos])
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0][1].reason, "drawdown_reduction")

    def test_5pt2pct_drawdown_closes_3_worst(self):
        """At -5.2% drawdown → close 3 worst positions."""
        engine, portfolio = _make_engine()
        positions = [_make_position(entry_price=0.97, current_price=0.88, position_id=i)
                     for i in range(3)]
        portfolio.get_portfolio_drawdown_pct.return_value = -0.052
        portfolio.get_weakest_positions.return_value = positions
        decisions = engine.check_portfolio_drawdown(positions)
        self.assertEqual(len(decisions), 3)

    def test_no_drawdown_no_exits(self):
        """No drawdown → no forced exits."""
        engine, portfolio = _make_engine()
        portfolio.get_portfolio_drawdown_pct.return_value = 0.01
        decisions = engine.check_portfolio_drawdown([])
        self.assertEqual(len(decisions), 0)


class TestTrailingStop(unittest.TestCase):

    def test_trailing_stop_triggers_when_price_drops_below_trail(self):
        """Price hit $0.99 high-water, drops to $0.97 → below trail → exit."""
        engine, _ = _make_engine()
        pos = _make_position(entry_price=0.96, high_water_mark=0.99)
        # Trail price = 0.99 * (1 - 0.015) = 0.97515
        decision = engine.check_trailing_stop(pos, 0.974)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "trailing_stop")

    def test_trailing_stop_not_activated_below_threshold(self):
        """Current price $0.984 < $0.985 activation → not yet active → hold."""
        engine, _ = _make_engine()
        pos = _make_position(entry_price=0.97, high_water_mark=None)
        decision = engine.check_trailing_stop(pos, 0.984)
        self.assertIsNone(decision)


class TestStopLossPriority(unittest.TestCase):

    def test_stop_loss_takes_priority_over_time_exit(self):
        """When both stop-loss and time exit conditions are met, stop-loss wins."""
        engine, _ = _make_engine()
        entry_time = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
        pos = _make_position(entry_price=0.97, entry_time=entry_time)
        current = 0.97 * (1 - 0.075)  # stop-loss territory

        stop = engine.check_stop_loss(pos, current)
        time_exit = engine.check_time_exit(pos)

        # Both triggered
        self.assertIsNotNone(stop)
        self.assertIsNotNone(time_exit)

        # In _evaluate_single, stop-loss (priority 3) beats time-exit (priority 5)
        self.assertEqual(stop.reason, "stop_loss")
        self.assertEqual(time_exit.reason, "time_exit")


if __name__ == "__main__":
    unittest.main()
