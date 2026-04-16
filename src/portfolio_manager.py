"""
Portfolio manager: wraps Database with portfolio-level state and calculations.

Tracks peak portfolio value for drawdown calculations, provides exposure
queries for the risk engine, and computes unrealized P&L from monitored positions.
"""

import logging
from typing import Any, Dict, List, Optional

from .database import Database

logger = logging.getLogger(__name__)


class PortfolioManager:
    """
    Portfolio state manager. Wraps Database with higher-level portfolio logic.

    All exposure queries route through here so the RiskEngine and ExitEngine
    have a single source of truth.
    """

    def __init__(self, db: Database):
        self.db = db
        self._portfolio_balance: float = 0.0
        self._peak_portfolio_value: float = 0.0
        self._paper_mode: bool = False

    def set_portfolio_balance(self, balance: float, paper_mode: bool = False) -> None:
        """
        Update portfolio balance and track the peak for drawdown calculations.

        Args:
            balance: Current portfolio balance in USDC
            paper_mode: Whether this is paper trading
        """
        self._portfolio_balance = balance
        self._paper_mode = paper_mode
        effective = self.get_effective_portfolio_value([])
        if effective > self._peak_portfolio_value:
            self._peak_portfolio_value = effective

    def get_portfolio_balance(self) -> float:
        return self._portfolio_balance

    # ─── Deployment ───────────────────────────────────────────────────────────

    def get_total_deployed(self) -> float:
        """Sum of cost_basis for all open positions."""
        return self.db.get_total_deployed(paper_trade=self._paper_mode)

    def get_deployment_pct(self) -> float:
        """Fraction of portfolio currently deployed."""
        if self._portfolio_balance <= 0:
            return 0.0
        return self.get_total_deployed() / self._portfolio_balance

    # ─── Exposure queries ─────────────────────────────────────────────────────

    def get_category_exposure(self, category: str) -> float:
        """Sum of cost_basis for open positions in this category."""
        return self.db.get_category_exposure(category, paper_trade=self._paper_mode)

    def get_event_group_exposure(self, event_group_id: str) -> float:
        """Sum of cost_basis for open positions in this event group."""
        return self.db.get_event_group_exposure(event_group_id, paper_trade=self._paper_mode)

    def get_risk_bucket_exposure(self, bucket_name: str) -> float:
        """Sum of cost_basis for open positions in this risk bucket."""
        return self.db.get_risk_bucket_exposure(bucket_name, paper_trade=self._paper_mode)

    # ─── P&L and loss tracking ────────────────────────────────────────────────

    def get_todays_realized_pnl(self) -> float:
        """Sum of pnl for positions closed today (UTC)."""
        return self.db.get_todays_realized_pnl(paper_trade=self._paper_mode)

    def get_consecutive_losses(self) -> int:
        """Count consecutive losses from the most recent closed positions."""
        return self.db.get_consecutive_losses(paper_trade=self._paper_mode)

    def get_unrealized_pnl(self, open_positions: List[Dict[str, Any]]) -> float:
        """
        Compute total unrealized P&L.

        Requires open_positions to have '_current_price' set (done by the monitor cycle).
        Falls back to entry_price if current price is unavailable.
        """
        total = 0.0
        for pos in open_positions:
            current = pos.get("_current_price") or pos.get("entry_price", 0)
            entry = pos.get("entry_price", 0)
            shares = pos.get("shares", 0)
            total += (current - entry) * shares
        return total

    # ─── Portfolio value and drawdown ─────────────────────────────────────────

    def get_effective_portfolio_value(
        self, open_positions: List[Dict[str, Any]]
    ) -> float:
        """
        Total portfolio value = liquid balance + deployed (at current prices).
        Used for drawdown calculations and bucket exposure percentages.
        """
        unrealized = self.get_unrealized_pnl(open_positions)
        return self._portfolio_balance + unrealized

    def get_portfolio_drawdown_pct(
        self, open_positions: List[Dict[str, Any]]
    ) -> float:
        """
        Current drawdown from peak portfolio value.
        Returns a negative fraction (e.g., -0.03 for -3%).
        Returns 0.0 if no peak has been established yet.
        """
        if self._peak_portfolio_value <= 0:
            return 0.0
        current = self.get_effective_portfolio_value(open_positions)
        return (current - self._peak_portfolio_value) / self._peak_portfolio_value

    def get_weakest_positions(
        self, n: int, open_positions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Return the n worst-performing open positions sorted by unrealized P&L %.
        Requires positions to have '_current_price' set.
        """
        def unrealized_pct(pos: Dict) -> float:
            entry = pos.get("entry_price", 0)
            current = pos.get("_current_price") or entry
            if entry <= 0:
                return 0.0
            return (current - entry) / entry

        sorted_positions = sorted(open_positions, key=unrealized_pct)
        return sorted_positions[:n]

    # ─── High-water mark ──────────────────────────────────────────────────────

    def update_high_water_mark(self, position_id: int, high_water_mark: float) -> None:
        """Persist updated high-water mark for a position."""
        self.db.update_high_water_mark(position_id, high_water_mark)

    # ─── Status updates ───────────────────────────────────────────────────────

    def update_position_status(self, position_id: int, status: str) -> None:
        """Update a position's status (e.g., 'disputed')."""
        self.db.update_position(position_id, {"status": status})

    # ─── Win-rate analytics (for breakeven calculation) ───────────────────────

    def get_avg_winning_trade_pnl(self) -> float:
        """Average P&L of profitable closed trades."""
        return self.db.get_avg_trade_pnl(wins_only=True, paper_trade=self._paper_mode)

    def get_avg_losing_trade_pnl(self) -> float:
        """Average P&L (negative number) of losing closed trades."""
        return self.db.get_avg_trade_pnl(losses_only=True, paper_trade=self._paper_mode)

    def get_win_rate(self) -> float:
        """Fraction of closed trades that were profitable."""
        return self.db.get_win_rate(paper_trade=self._paper_mode)

    # ─── V3: Bucket statistics for confidence scaling ─────────────────────────

    def get_bucket_statistics(self, bucket: str) -> dict:
        """Get closed-trade stats for a specific risk bucket."""
        return self.db.get_bucket_statistics(bucket, paper_trade=self._paper_mode)

    def get_trailing_avg_loss(self, exclude_id: int = None, lookback: int = 10) -> float:
        """Average loss amount across the last N losing trades, optionally excluding one."""
        return self.db.get_trailing_avg_loss(
            exclude_id=exclude_id, lookback=lookback, paper_trade=self._paper_mode
        )
