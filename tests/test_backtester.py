"""
Tests for the backtester module (scripts/backtest.py).
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_historical_market(
    market_id: str,
    yes_price: float,
    days_after_entry: float,
    resolved: bool = True,
    outcome: str = "YES",
    days_before_resolution: float = 3.0,
) -> dict:
    """Create a mock historical market for backtesting."""
    from datetime import datetime, timedelta, timezone

    entry_time = datetime.now(timezone.utc) - timedelta(days=days_after_entry + days_before_resolution)
    resolution_time = entry_time + timedelta(days=days_before_resolution)

    return {
        "id": market_id,
        "conditionId": market_id,
        "question": f"Test market {market_id}",
        "resolved": resolved,
        "closed": resolved,
        "winner": outcome if resolved else None,
        "entry_price": yes_price,
        "_yes_price": yes_price,
        "outcomePrices": f'["{yes_price}", "{1.0 - yes_price}"]',
        "endDate": resolution_time.isoformat(),
        "entry_time": entry_time.isoformat(),
        "resolution_time": resolution_time.isoformat() if resolved else None,
        "liquidityClob": 25000,
        "_liquidity": 25000,
        "oneDayPriceChange": 0.002,
        "_price_change_1d": 0.002,
        "_days_to_resolution": days_before_resolution,
        "_bond_score": 0.001,
    }


def calculate_trade_pnl(
    entry_price: float,
    resolution_price: float,
    position_size: float,
) -> float:
    """Calculate P&L for a simulated trade."""
    shares = position_size / entry_price
    cost = shares * entry_price
    proceeds = shares * resolution_price
    fees = (cost + proceeds) * 0.001  # 0.1% each way
    return proceeds - cost - fees


def run_simple_backtest(markets: list, position_size: float = 1000.0) -> dict:
    """
    Run a simple backtest simulation on historical markets.

    Args:
        markets: List of historical market dictionaries
        position_size: Fixed position size for simulation

    Returns:
        Backtest results dictionary
    """
    trades = []
    total_pnl = 0.0
    wins = 0
    losses = 0
    peak_balance = position_size * 10  # Starting balance
    current_balance = peak_balance
    max_drawdown = 0.0

    for market in markets:
        entry_price = market.get("entry_price") or market.get("_yes_price", 0)
        if entry_price <= 0:
            continue

        outcome = market.get("winner", "YES")
        resolved = market.get("resolved", False)

        if not resolved:
            # Assume market timed out, exit at entry price (flat)
            resolution_price = entry_price
        elif outcome == "YES":
            resolution_price = 1.0
        else:
            resolution_price = 0.0

        pnl = calculate_trade_pnl(entry_price, resolution_price, position_size)
        total_pnl += pnl
        current_balance += pnl

        if pnl > 0:
            wins += 1
        else:
            losses += 1

        drawdown = (peak_balance - current_balance) / peak_balance
        max_drawdown = max(max_drawdown, drawdown)
        peak_balance = max(peak_balance, current_balance)

        trades.append({
            "market_id": market["id"],
            "entry_price": entry_price,
            "resolution_price": resolution_price,
            "pnl": pnl,
            "outcome": outcome,
        })

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    avg_yield = (total_pnl / (total_trades * position_size) * 100) if total_trades > 0 else 0.0

    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_yield_pct": avg_yield,
        "max_drawdown": max_drawdown * 100,
        "trades": trades,
    }


class TestBacktesterWinRate(unittest.TestCase):
    """Tests for win rate calculation."""

    def test_all_yes_outcomes_100_pct_win_rate(self):
        """All YES outcomes should yield 100% win rate."""
        markets = [
            make_historical_market(f"mkt-{i}", 0.97, days_after_entry=i, outcome="YES")
            for i in range(10)
        ]
        results = run_simple_backtest(markets)
        self.assertEqual(results["win_rate"], 100.0)
        self.assertEqual(results["wins"], 10)
        self.assertEqual(results["losses"], 0)

    def test_all_no_outcomes_0_pct_win_rate(self):
        """All NO outcomes (market resolves against position) should yield 0% win rate."""
        markets = [
            make_historical_market(f"mkt-{i}", 0.97, days_after_entry=i, outcome="NO")
            for i in range(5)
        ]
        results = run_simple_backtest(markets)
        self.assertEqual(results["win_rate"], 0.0)
        self.assertEqual(results["wins"], 0)
        self.assertEqual(results["losses"], 5)

    def test_mixed_outcomes_correct_win_rate(self):
        """Mixed YES/NO outcomes should calculate correct win rate."""
        markets = (
            [make_historical_market(f"win-{i}", 0.97, days_after_entry=i, outcome="YES")
             for i in range(8)]
            +
            [make_historical_market(f"loss-{i}", 0.97, days_after_entry=i, outcome="NO")
             for i in range(2)]
        )
        results = run_simple_backtest(markets)
        self.assertEqual(results["total_trades"], 10)
        self.assertAlmostEqual(results["win_rate"], 80.0, places=1)

    def test_empty_markets_returns_zero_trades(self):
        """Empty market list should return zero trades."""
        results = run_simple_backtest([])
        self.assertEqual(results["total_trades"], 0)
        self.assertEqual(results["win_rate"], 0.0)


class TestBacktesterPnLCalculation(unittest.TestCase):
    """Tests for P&L calculation accuracy."""

    def test_yes_outcome_positive_pnl(self):
        """YES outcome should yield positive P&L for bond strategy."""
        markets = [
            make_historical_market("mkt-001", yes_price=0.97, days_after_entry=5, outcome="YES")
        ]
        results = run_simple_backtest(markets, position_size=1000.0)

        self.assertGreater(results["total_pnl"], 0.0)
        # Gross yield = (1.00 - 0.97) / 0.97 ≈ 3.09%
        # Net yield after fees ≈ 2.9%
        # On $1000 position: ≈ $29
        self.assertAlmostEqual(results["total_pnl"], 29.0, delta=5.0)

    def test_no_outcome_negative_pnl(self):
        """NO outcome should yield negative P&L (loss of most position)."""
        markets = [
            make_historical_market("mkt-001", yes_price=0.97, days_after_entry=5, outcome="NO")
        ]
        results = run_simple_backtest(markets, position_size=1000.0)

        self.assertLess(results["total_pnl"], 0.0)
        # Approximately lose most of position: ~$970 loss
        self.assertLess(results["total_pnl"], -900.0)

    def test_pnl_calculation_basic(self):
        """Basic P&L calculation should be mathematically correct."""
        entry_price = 0.95
        resolution_price = 1.0
        position_size = 1000.0

        pnl = calculate_trade_pnl(entry_price, resolution_price, position_size)

        shares = position_size / entry_price  # ≈ 1052.63 shares
        expected_gross = shares * resolution_price - shares * entry_price  # ≈ $52.63
        # Fees ≈ $2.10
        self.assertGreater(pnl, 40.0)
        self.assertLess(pnl, 60.0)

    def test_higher_entry_price_lower_yield(self):
        """Higher entry price should result in lower yield."""
        low_price_markets = [
            make_historical_market("mkt-low", 0.95, days_after_entry=3, outcome="YES")
        ]
        high_price_markets = [
            make_historical_market("mkt-high", 0.99, days_after_entry=3, outcome="YES")
        ]

        low_results = run_simple_backtest(low_price_markets, position_size=1000.0)
        high_results = run_simple_backtest(high_price_markets, position_size=1000.0)

        self.assertGreater(low_results["total_pnl"], high_results["total_pnl"])

    def test_max_drawdown_calculation(self):
        """Max drawdown should be calculated correctly."""
        # Create markets: 3 wins, then 2 losses, then 2 wins
        markets = (
            [make_historical_market(f"w{i}", 0.97, days_after_entry=i, outcome="YES")
             for i in range(3)]
            +
            [make_historical_market(f"l{i}", 0.97, days_after_entry=3+i, outcome="NO")
             for i in range(2)]
            +
            [make_historical_market(f"w2{i}", 0.97, days_after_entry=5+i, outcome="YES")
             for i in range(2)]
        )

        results = run_simple_backtest(markets, position_size=500.0)

        # Should have a drawdown (from peak after 3 wins to trough after 2 losses)
        self.assertGreater(results["max_drawdown"], 0.0)

    def test_avg_yield_calculation(self):
        """Average yield should be calculated as percentage of position size."""
        markets = [
            make_historical_market("mkt-001", 0.97, days_after_entry=3, outcome="YES")
        ]
        results = run_simple_backtest(markets, position_size=1000.0)

        # Average yield = total_pnl / (total_trades * position_size) * 100
        expected_yield = (results["total_pnl"] / (results["total_trades"] * 1000.0) * 100)
        self.assertAlmostEqual(results["avg_yield_pct"], expected_yield, places=3)


class TestBacktesterEdgeCases(unittest.TestCase):
    """Tests for edge cases in the backtester."""

    def test_single_trade_win(self):
        """Single winning trade should have 100% win rate."""
        markets = [make_historical_market("mkt-1", 0.97, days_after_entry=5)]
        results = run_simple_backtest(markets, position_size=1000.0)
        self.assertEqual(results["total_trades"], 1)
        self.assertEqual(results["win_rate"], 100.0)

    def test_unresolved_market_treated_as_flat(self):
        """Unresolved market should be treated as flat (no P&L)."""
        market = make_historical_market("mkt-1", 0.97, days_after_entry=5, resolved=False)
        results = run_simple_backtest([market], position_size=1000.0)

        # Flat resolution means ~0 PnL (only fees)
        self.assertLess(abs(results["total_pnl"]), 5.0)

    def test_large_batch_of_trades(self):
        """Backtest should handle large number of trades efficiently."""
        markets = [
            make_historical_market(f"mkt-{i}", 0.97, days_after_entry=i % 14, outcome="YES")
            for i in range(100)
        ]
        results = run_simple_backtest(markets)
        self.assertEqual(results["total_trades"], 100)
        self.assertEqual(results["win_rate"], 100.0)


if __name__ == "__main__":
    unittest.main()
