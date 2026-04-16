"""
Risk management module for position sizing and portfolio limits.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .utils import calculate_time_factor, get_days_to_resolution

logger = logging.getLogger(__name__)


class RiskManager:
    """Manages position sizing and portfolio risk controls."""

    def __init__(self, config: dict):
        """
        Initialize risk manager with configuration.

        Args:
            config: Full configuration dictionary
        """
        self.config = config
        risk_cfg = config.get("risk", {})

        self.max_single_market_pct = risk_cfg.get("max_single_market_pct", 0.10)
        self.max_correlated_pct = risk_cfg.get("max_correlated_pct", 0.15)
        self.max_deployed_pct = risk_cfg.get("max_deployed_pct", 0.70)
        self.max_absolute_position = risk_cfg.get("max_absolute_position", 5000.0)
        self.max_daily_loss_pct = risk_cfg.get("max_daily_loss_pct", 0.02)
        self.consecutive_loss_halt = risk_cfg.get("consecutive_loss_halt", 3)
        self.min_net_yield = risk_cfg.get("min_net_yield", 0.01)

    def get_time_factor(self, end_date: str) -> float:
        """
        Get position size time factor based on days to resolution.

        Args:
            end_date: ISO format end date string

        Returns:
            Time factor multiplier (0.2 to 1.0)
        """
        days = get_days_to_resolution(end_date)
        if days <= 0:
            return 0.0
        return calculate_time_factor(days)

    def calculate_position_size(
        self,
        market: Dict[str, Any],
        portfolio_balance: float,
        existing_positions: List[Dict],
        available_liquidity: float,
    ) -> float:
        """
        Calculate the appropriate position size for a market.

        Position size is the minimum of:
        1. portfolio_balance * MAX_SINGLE_MARKET_PCT
        2. available_bid_liquidity * 0.10
        3. MAX_ABSOLUTE_POSITION ($5000)

        Then multiplied by the time factor.

        Args:
            market: Market dictionary with enriched price/liquidity data
            portfolio_balance: Total portfolio value in USD
            existing_positions: List of currently open position dicts
            available_liquidity: Available bid-side liquidity in USD

        Returns:
            Position size in USD
        """
        if portfolio_balance <= 0:
            logger.warning("Portfolio balance is zero or negative, cannot size position")
            return 0.0

        # Calculate deployed capital
        deployed = sum(p.get("cost_basis", 0) for p in existing_positions)
        available_balance = portfolio_balance - deployed

        if available_balance <= 0:
            logger.warning("No available balance for new positions")
            return 0.0

        # Component 1: Max single market percentage
        size_by_pct = portfolio_balance * self.max_single_market_pct

        # Component 2: 10% of available bid liquidity
        size_by_liquidity = available_liquidity * 0.10

        # Component 3: Absolute maximum
        size_by_absolute = self.max_absolute_position

        # Take the minimum of all three
        raw_size = min(size_by_pct, size_by_liquidity, size_by_absolute)

        # Apply time factor
        end_date = market.get("endDate") or market.get("end_date_iso", "")
        time_factor = self.get_time_factor(end_date) if end_date else 0.6

        sized = raw_size * time_factor

        # Cap to available balance
        final_size = min(sized, available_balance)

        logger.debug(
            f"Position sizing: pct=${size_by_pct:.2f}, liq=${size_by_liquidity:.2f}, "
            f"abs=${size_by_absolute:.2f}, time_factor={time_factor:.1f}, "
            f"final=${final_size:.2f}"
        )

        return max(0.0, final_size)

    def check_portfolio_limits(
        self,
        existing_positions: List[Dict],
        new_position_size: float,
        portfolio_balance: float,
        new_market: Optional[Dict] = None,
    ) -> Tuple[bool, str]:
        """
        Check if adding a new position would violate portfolio limits.

        Args:
            existing_positions: List of currently open positions
            new_position_size: Proposed new position size in USD
            portfolio_balance: Total portfolio value in USD
            new_market: The market being considered (for correlation check)

        Returns:
            (allowed, reason) tuple
        """
        if portfolio_balance <= 0:
            return False, "Portfolio balance is zero or negative"

        # Check total deployed percentage
        deployed = sum(p.get("cost_basis", 0) for p in existing_positions)
        new_deployed = deployed + new_position_size
        new_deployed_pct = new_deployed / portfolio_balance

        if new_deployed_pct > self.max_deployed_pct:
            return (
                False,
                f"Would exceed max deployed: {new_deployed_pct:.1%} > {self.max_deployed_pct:.1%}",
            )

        # Check single market percentage
        single_pct = new_position_size / portfolio_balance
        if single_pct > self.max_single_market_pct:
            return (
                False,
                f"Single market percentage {single_pct:.1%} exceeds max {self.max_single_market_pct:.1%}",
            )

        # Check correlated exposure (same event_id)
        if new_market:
            new_event_id = new_market.get("eventId") or new_market.get("event_id", "")
            if new_event_id:
                correlated_deployed = sum(
                    p.get("cost_basis", 0)
                    for p in existing_positions
                    if p.get("event_id") == new_event_id
                )
                correlated_total = correlated_deployed + new_position_size
                correlated_pct = correlated_total / portfolio_balance
                if correlated_pct > self.max_correlated_pct:
                    return (
                        False,
                        f"Correlated exposure {correlated_pct:.1%} exceeds max {self.max_correlated_pct:.1%}",
                    )

        return True, f"Portfolio limits OK: {new_deployed_pct:.1%} deployed"

    def check_daily_loss_limit(
        self,
        daily_pnl: float,
        portfolio_balance: float,
    ) -> bool:
        """
        Check if the daily loss limit has been breached.

        Args:
            daily_pnl: Today's realized + unrealized P&L
            portfolio_balance: Total portfolio balance

        Returns:
            True if trading should HALT (limit breached), False if OK to continue
        """
        if portfolio_balance <= 0:
            return True

        loss_pct = abs(min(daily_pnl, 0)) / portfolio_balance
        if loss_pct >= self.max_daily_loss_pct:
            logger.warning(
                f"Daily loss limit breached: {loss_pct:.2%} >= {self.max_daily_loss_pct:.2%}"
            )
            return True
        return False

    def check_consecutive_losses(self, recent_positions: List[Dict]) -> bool:
        """
        Check if consecutive loss circuit breaker should trigger.

        Args:
            recent_positions: List of recently closed positions (most recent first)

        Returns:
            True if trading should HALT, False if OK to continue
        """
        if not recent_positions:
            return False

        closed = [p for p in recent_positions if p.get("status") == "closed"]
        if not closed:
            return False

        # Count consecutive losses from most recent
        consecutive_losses = 0
        for pos in closed[:self.consecutive_loss_halt + 1]:
            pnl = pos.get("pnl")
            if pnl is not None and pnl < 0:
                consecutive_losses += 1
            else:
                break

        if consecutive_losses >= self.consecutive_loss_halt:
            logger.warning(
                f"Consecutive loss circuit breaker triggered: "
                f"{consecutive_losses} losses in a row"
            )
            return True

        return False

    def validate_entry(
        self,
        market: Dict[str, Any],
        position_size: float,
        portfolio_balance: float,
        existing_positions: List[Dict],
        daily_pnl: float,
    ) -> Tuple[bool, str]:
        """
        Comprehensive entry validation combining all risk checks.

        Args:
            market: Market dictionary
            position_size: Proposed position size
            portfolio_balance: Total portfolio balance
            existing_positions: Current open positions
            daily_pnl: Today's P&L

        Returns:
            (approved, reason) tuple
        """
        # Check minimum position size
        if position_size < 10.0:
            return False, f"Position size ${position_size:.2f} too small (min $10)"

        # Check daily loss limit
        if self.check_daily_loss_limit(daily_pnl, portfolio_balance):
            return False, "Daily loss limit breached - trading halted"

        # Check consecutive losses
        if self.check_consecutive_losses(existing_positions):
            return False, f"Consecutive loss circuit breaker: {self.consecutive_loss_halt} losses"

        # Check portfolio limits
        allowed, reason = self.check_portfolio_limits(
            existing_positions, position_size, portfolio_balance, market
        )
        if not allowed:
            return False, reason

        # Check net yield
        yes_price = market.get("_yes_price", 0)
        if yes_price > 0:
            gross_yield = (1.0 - yes_price) / yes_price
            net_yield = gross_yield - 0.001  # Approximate fee
            if net_yield < self.min_net_yield:
                return (
                    False,
                    f"Net yield {net_yield:.4f} below minimum {self.min_net_yield}",
                )

        return True, "Entry approved"

    def calculate_unrealized_pnl(
        self,
        positions: List[Dict],
        current_prices: Dict[str, float],
    ) -> float:
        """
        Calculate total unrealized P&L across all open positions.

        Args:
            positions: List of open position dicts
            current_prices: Dict mapping market_id to current YES price

        Returns:
            Total unrealized P&L in USD
        """
        total_unrealized = 0.0
        for pos in positions:
            if pos.get("status") != "open":
                continue
            market_id = pos.get("market_id", "")
            current_price = current_prices.get(market_id)
            if current_price is None:
                continue
            shares = pos.get("shares", 0)
            entry_price = pos.get("entry_price", 0)
            current_value = shares * current_price
            cost = shares * entry_price
            unrealized = current_value - cost
            total_unrealized += unrealized
        return total_unrealized

    def get_portfolio_summary(
        self,
        positions: List[Dict],
        portfolio_balance: float,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """
        Get a summary of current portfolio state.

        Args:
            positions: All positions (open and closed)
            portfolio_balance: Total portfolio value
            current_prices: Optional dict of current market prices

        Returns:
            Portfolio summary dictionary
        """
        open_positions = [p for p in positions if p.get("status") == "open"]
        deployed = sum(p.get("cost_basis", 0) for p in open_positions)
        available = portfolio_balance - deployed
        deployed_pct = deployed / portfolio_balance if portfolio_balance > 0 else 0

        unrealized_pnl = 0.0
        if current_prices:
            unrealized_pnl = self.calculate_unrealized_pnl(open_positions, current_prices)

        return {
            "balance": portfolio_balance,
            "deployed": deployed,
            "available": available,
            "deployed_pct": deployed_pct,
            "unrealized_pnl": unrealized_pnl,
            "open_positions": len(open_positions),
        }
