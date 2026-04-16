"""
Order execution module for entering and exiting positions.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from .utils import get_days_to_resolution, round_to_tick, safe_json_parse

logger = logging.getLogger(__name__)

# Polymarket uses 0.01 tick size by default
DEFAULT_TICK_SIZE = 0.01
# Conservative fee estimate — assumes taker fee for yield calculations
ESTIMATED_FEE_RATE = 0.002


class OrderExecutor:
    """Handles order placement, monitoring, and cancellation."""

    def __init__(self, config: dict):
        """
        Initialize executor with configuration.

        Args:
            config: Full configuration dictionary
        """
        self.config = config
        exits_cfg = config.get("exits", {})
        self.order_timeout = exits_cfg.get("order_timeout_seconds", 300)
        self.auto_exit_enabled = exits_cfg.get("auto_exit_enabled", True)
        risk_cfg = config.get("risk", {})
        self.min_net_yield = risk_cfg.get("min_net_yield", 0.01)

    async def execute_entry(
        self,
        market: Dict[str, Any],
        position_size: float,
        clob_client: Any,
        paper_mode: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Execute an entry order for a YES position.

        Args:
            market: Market dictionary with price/token data
            position_size: USD amount to invest
            clob_client: Authenticated CLOB client (can be None in paper mode)
            paper_mode: If True, simulate without placing real orders

        Returns:
            Position dictionary if successful, None otherwise
        """
        yes_price = market.get("_yes_price", 0)
        yes_token_id = market.get("_yes_token_id")
        market_id = market.get("id") or market.get("conditionId", "")
        market_question = market.get("question") or market.get("title", "Unknown Market")

        if not yes_price or not yes_token_id:
            logger.error(
                f"Missing price ({yes_price}) or token ID ({yes_token_id}) for {market_id}"
            )
            return None

        if position_size <= 0:
            logger.error(f"Invalid position size: {position_size}")
            return None

        # Round entry price to tick size
        entry_price = round_to_tick(yes_price, DEFAULT_TICK_SIZE)

        # Calculate shares from position size
        shares = position_size / entry_price
        shares = round(shares, 4)

        # Calculate fees
        fees = position_size * ESTIMATED_FEE_RATE
        net_yield = ((1.0 - entry_price) / entry_price) - ESTIMATED_FEE_RATE

        if net_yield < self.min_net_yield:
            logger.warning(
                f"Net yield {net_yield:.4f} below minimum {self.min_net_yield}, skipping entry"
            )
            return None

        logger.info(
            f"{'[PAPER] ' if paper_mode else ''}Entering: {market_question[:60]} | "
            f"Price=${entry_price:.4f} | Shares={shares:.2f} | Size=${position_size:.2f}"
        )

        order_id = None

        if paper_mode:
            # Simulate order without hitting real API
            order_id = f"PAPER-{market_id[:8]}-{int(time.time())}"
            logger.info(f"[PAPER] Simulated order placed: {order_id}")
        else:
            # Place real limit order at current ask price
            try:
                order_args = {
                    "token_id": yes_token_id,
                    "price": entry_price,
                    "size": shares,
                    "side": "BUY",
                }
                response = clob_client.create_and_post_order(order_args)

                if not response:
                    logger.error(f"No response from order placement for {market_id}")
                    return None

                order_id = getattr(response, "orderID", None) or str(response)
                logger.info(f"Order placed: {order_id} for {market_id}")

                # Wait for fill
                filled_price, filled_shares, actual_fees = await self.monitor_fill(
                    order_id, clob_client, self.order_timeout
                )

                if filled_price is None:
                    logger.warning(f"Order {order_id} not filled within timeout, cancelling")
                    await self.cancel_order(order_id, clob_client)
                    return None

                # Update with actual fill data
                entry_price = filled_price or entry_price
                shares = filled_shares or shares
                fees = actual_fees or fees

            except Exception as e:
                logger.error(f"Order placement failed for {market_id}: {e}", exc_info=True)
                return None

        cost_basis = entry_price * shares

        position = {
            "market_id": market_id,
            "market_question": market_question,
            "token_id": yes_token_id,
            "entry_price": entry_price,
            "shares": shares,
            "cost_basis": cost_basis,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "expected_resolution": market.get("endDate") or market.get("end_date_iso", ""),
            "status": "open",
            "fees_paid": fees,
            "bond_score": market.get("_bond_score", 0.0),
            "paper_trade": paper_mode,
            "order_id": order_id,
            "event_id": market.get("eventId") or market.get("event_id", ""),
        }

        logger.info(
            f"{'[PAPER] ' if paper_mode else ''}Position opened: "
            f"market={market_id} | price=${entry_price:.4f} | "
            f"shares={shares:.2f} | cost=${cost_basis:.2f}"
        )

        return position

    async def execute_exit(
        self,
        position: Dict[str, Any],
        clob_client: Any,
        paper_mode: bool = False,
        current_price: Optional[float] = None,
    ) -> bool:
        """
        Execute an exit order to close a position.

        Args:
            position: Position dictionary with entry data
            clob_client: Authenticated CLOB client
            paper_mode: If True, simulate without placing real orders
            current_price: Current market price (used for paper mode P&L)

        Returns:
            True if exit was successful
        """
        market_id = position.get("market_id", "")
        token_id = position.get("token_id", "")
        shares = position.get("shares", 0)
        entry_price = position.get("entry_price", 0)
        cost_basis = position.get("cost_basis", 0)

        if shares <= 0:
            logger.error(f"Invalid shares for exit: {shares}")
            return False

        logger.info(
            f"{'[PAPER] ' if paper_mode else ''}Exiting position: {market_id} | "
            f"Shares={shares:.2f} | Entry=${entry_price:.4f}"
        )

        exit_price = current_price or 1.0  # Default to resolution price
        actual_fees = shares * exit_price * ESTIMATED_FEE_RATE

        if paper_mode:
            # Simulate exit
            logger.info(f"[PAPER] Simulated exit at ${exit_price:.4f}")
        else:
            try:
                if not token_id:
                    logger.error(f"No token ID for exit of position {position.get('id')}")
                    return False

                # Place sell order at current bid price (limit sell)
                sell_price = round_to_tick(
                    exit_price - DEFAULT_TICK_SIZE if exit_price > DEFAULT_TICK_SIZE else exit_price,
                    DEFAULT_TICK_SIZE,
                )

                order_args = {
                    "token_id": token_id,
                    "price": sell_price,
                    "size": shares,
                    "side": "SELL",
                }
                response = clob_client.create_and_post_order(order_args)

                if not response:
                    logger.error(f"No response from exit order for {market_id}")
                    return False

                order_id = getattr(response, "orderID", None) or str(response)
                logger.info(f"Exit order placed: {order_id}")

                # Wait for fill
                filled_price, filled_shares, fill_fees = await self.monitor_fill(
                    order_id, clob_client, self.order_timeout
                )

                if filled_price is None:
                    logger.warning(f"Exit order {order_id} not filled, attempting cancel")
                    await self.cancel_order(order_id, clob_client)
                    return False

                exit_price = filled_price or exit_price
                actual_fees = fill_fees or actual_fees

            except Exception as e:
                logger.error(f"Exit order failed for {market_id}: {e}", exc_info=True)
                return False

        # Calculate P&L
        proceeds = shares * exit_price
        total_fees = position.get("fees_paid", 0) + actual_fees
        pnl = proceeds - cost_basis - actual_fees

        logger.info(
            f"Position closed: market={market_id} | exit=${exit_price:.4f} | "
            f"P&L=${pnl:+.2f} | fees=${actual_fees:.2f}"
        )

        # Update position fields in-place so caller can persist
        position["exit_price"] = exit_price
        position["exit_time"] = datetime.now(timezone.utc).isoformat()
        position["pnl"] = pnl
        position["fees_paid"] = total_fees
        position["status"] = "closed"

        return True

    async def cancel_order(
        self,
        order_id: str,
        clob_client: Any,
    ) -> bool:
        """
        Cancel an open order.

        Args:
            order_id: Order ID to cancel
            clob_client: Authenticated CLOB client

        Returns:
            True if successfully cancelled
        """
        if not order_id or order_id.startswith("PAPER-"):
            return True

        try:
            result = clob_client.cancel(order_id)
            success = result is not None
            if success:
                logger.info(f"Cancelled order: {order_id}")
            else:
                logger.warning(f"Cancel may have failed for order: {order_id}")
            return success
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def monitor_fill(
        self,
        order_id: str,
        clob_client: Any,
        timeout_seconds: int,
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Monitor an order until it fills or times out.

        Args:
            order_id: Order ID to monitor
            clob_client: Authenticated CLOB client
            timeout_seconds: Maximum wait time in seconds

        Returns:
            Tuple of (fill_price, fill_shares, fees) or (None, None, None) on timeout
        """
        if not order_id or order_id.startswith("PAPER-"):
            # Paper mode: instant fill
            return None, None, None

        start_time = time.time()
        check_interval = 5  # seconds between checks

        logger.info(f"Monitoring fill for order {order_id} (timeout={timeout_seconds}s)")

        while time.time() - start_time < timeout_seconds:
            try:
                order = clob_client.get_order(order_id)
                if not order:
                    logger.warning(f"Order {order_id} not found in CLOB")
                    await asyncio.sleep(check_interval)
                    continue

                status = getattr(order, "status", "").upper()

                if status in ("MATCHED", "FILLED", "MATCHED_FULLY"):
                    fill_price = getattr(order, "price", None)
                    fill_size = getattr(order, "size_matched", None) or getattr(order, "size", None)
                    if fill_price and fill_size:
                        fees = float(fill_price) * float(fill_size) * ESTIMATED_FEE_RATE
                        logger.info(
                            f"Order {order_id} filled: price=${fill_price}, size={fill_size}"
                        )
                        return float(fill_price), float(fill_size), fees
                    return None, None, None

                elif status in ("CANCELLED", "EXPIRED"):
                    logger.info(f"Order {order_id} was {status}")
                    return None, None, None

                # Still pending/open
                elapsed = time.time() - start_time
                logger.debug(
                    f"Order {order_id} status={status}, elapsed={elapsed:.0f}s"
                )

            except Exception as e:
                logger.warning(f"Error checking order {order_id}: {e}")

            await asyncio.sleep(check_interval)

        logger.warning(f"Order {order_id} timed out after {timeout_seconds}s")
        return None, None, None

    async def cancel_all_open_orders(self, clob_client: Any) -> int:
        """
        Cancel all open orders (used during shutdown).

        Args:
            clob_client: Authenticated CLOB client

        Returns:
            Number of orders cancelled
        """
        try:
            result = clob_client.cancel_all()
            count = len(result) if isinstance(result, list) else 1
            logger.info(f"Cancelled {count} open orders")
            return count
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return 0

    async def close_position(
        self,
        position: Dict[str, Any],
        close_pct: float = 1.0,
        urgency: str = "normal",
        reason: str = "",
        clob_client: Any = None,
        paper_mode: bool = False,
        current_price: Optional[float] = None,
        notifier: Optional[Any] = None,
        db: Optional[Any] = None,
    ) -> bool:
        """
        Close a position (fully or partially) with slippage tracking (A1).

        Args:
            position: Position dictionary
            close_pct: Fraction of shares to sell (0.0 to 1.0)
            urgency: "immediate" → FOK market order, "normal" → limit at best bid
            reason: Exit reason for audit trail
            clob_client: Authenticated CLOB client
            paper_mode: If True, simulate without real orders
            current_price: Current market price
            notifier: Optional notifier for critical slippage alerts
            db: Optional database for persisting slippage data

        Returns:
            True if exit was successful
        """
        market_id = position.get("market_id", "")
        entry_price = position.get("entry_price", 0)
        total_shares = position.get("shares", 0)
        cost_basis = position.get("cost_basis", 0)

        if total_shares <= 0:
            logger.error(f"close_position: invalid shares {total_shares} for {market_id}")
            return False

        close_pct = max(0.0, min(1.0, close_pct))
        shares_to_close = round(total_shares * close_pct, 4)
        if shares_to_close <= 0:
            return False

        is_partial = close_pct < 1.0
        cost_of_closed = cost_basis * close_pct

        # Calculate expected exit price for slippage comparison (A1)
        stop_loss_pct = self.config.get("exits", {}).get("stop_loss_pct", 0.07)
        expected_exit_price = entry_price * (1 - stop_loss_pct) if reason == "stop_loss" else (
            current_price or entry_price
        )

        logger.info(
            f"{'[PAPER] ' if paper_mode else ''}close_position: {market_id} | "
            f"reason={reason} | close_pct={close_pct:.0%} | urgency={urgency} | "
            f"shares={shares_to_close:.2f}"
        )

        exit_price = current_price or 1.0
        actual_fees = shares_to_close * exit_price * ESTIMATED_FEE_RATE

        if paper_mode:
            logger.info(f"[PAPER] Simulated close at ${exit_price:.4f}")
        else:
            if not clob_client:
                logger.error(f"close_position: no CLOB client for live exit of {market_id}")
                return False

            token_id = position.get("token_id", "")
            if not token_id:
                logger.error(f"close_position: no token_id for {market_id}")
                return False

            try:
                if urgency == "immediate":
                    # FOK market order — stop-losses must execute regardless of price
                    order_args = {
                        "token_id": token_id,
                        "price": round_to_tick(exit_price * 0.97, DEFAULT_TICK_SIZE),  # 3% below mid
                        "size": shares_to_close,
                        "side": "SELL",
                    }
                else:
                    sell_price = round_to_tick(
                        (exit_price - DEFAULT_TICK_SIZE)
                        if exit_price > DEFAULT_TICK_SIZE else exit_price,
                        DEFAULT_TICK_SIZE,
                    )
                    order_args = {
                        "token_id": token_id,
                        "price": sell_price,
                        "size": shares_to_close,
                        "side": "SELL",
                    }

                response = clob_client.create_and_post_order(order_args)
                if not response:
                    logger.error(f"close_position: no response from CLOB for {market_id}")
                    return False

                order_id = getattr(response, "orderID", None) or str(response)
                filled_price, filled_shares, fill_fees = await self.monitor_fill(
                    order_id, clob_client, self.order_timeout
                )

                if filled_price is None:
                    logger.warning(
                        f"close_position: order {order_id} not filled, cancelling"
                    )
                    await self.cancel_order(order_id, clob_client)
                    return False

                exit_price = filled_price
                actual_fees = fill_fees or actual_fees

                # A1: Slippage tracking
                if reason == "stop_loss" and expected_exit_price > 0:
                    slippage = (expected_exit_price - exit_price) / expected_exit_price
                    if slippage > 0.05:  # > 5% slippage beyond target
                        logger.critical(
                            f"SEVERE SLIPPAGE on {position.get('market_question', market_id)}: "
                            f"expected_exit=${expected_exit_price:.4f}, "
                            f"actual_fill=${exit_price:.4f}, slippage={slippage:.2%}"
                        )
                        if notifier:
                            await notifier.send_critical(
                                f"Stop-loss slippage: {slippage:.2%} on "
                                f"{position.get('market_question', market_id)[:80]}"
                            )
                    if db and position.get("id"):
                        db.update_position(position["id"], {
                            "expected_exit_price": expected_exit_price,
                            "actual_exit_price": exit_price,
                            "exit_slippage_pct": slippage,
                        })

            except Exception as e:
                logger.error(f"close_position failed for {market_id}: {e}", exc_info=True)
                return False

        # Calculate P&L for the closed portion
        proceeds = shares_to_close * exit_price
        pnl = proceeds - cost_of_closed - actual_fees

        if is_partial:
            # Update position for partial close
            remaining_shares = round(total_shares - shares_to_close, 4)
            remaining_cost = cost_basis * (1 - close_pct)
            partial_count = position.get("partial_close_count", 0) + 1
            position["shares"] = remaining_shares
            position["cost_basis"] = remaining_cost
            position["partial_close_count"] = partial_count
            if position.get("id") and db:
                db.update_position(position["id"], {
                    "shares": remaining_shares,
                    "cost_basis": remaining_cost,
                    "partial_close_count": partial_count,
                })
            logger.info(
                f"Partial close ({close_pct:.0%}): {market_id} | "
                f"P&L=${pnl:+.2f} | remaining_shares={remaining_shares:.2f}"
            )
        else:
            # Full close — update position in-place
            position["exit_price"] = exit_price
            position["exit_time"] = datetime.now(timezone.utc).isoformat()
            position["pnl"] = pnl
            position["fees_paid"] = position.get("fees_paid", 0) + actual_fees
            position["status"] = "closed"
            position["exit_reason"] = reason
            if position.get("id") and db:
                db.update_position(position["id"], {
                    "status": "closed",
                    "exit_price": exit_price,
                    "exit_time": position["exit_time"],
                    "pnl": pnl,
                    "fees_paid": position["fees_paid"],
                    "exit_reason": reason,
                })
            logger.info(
                f"Position closed [{reason}]: {market_id} | "
                f"exit=${exit_price:.4f} | P&L=${pnl:+.2f}"
            )

        return True

    def calculate_net_yield(
        self,
        entry_price: float,
        shares: float,
        resolution_price: float = 1.0,
    ) -> float:
        """
        Calculate net yield for a position at resolution.

        Args:
            entry_price: Entry price per share
            shares: Number of shares
            resolution_price: Expected resolution price (1.0 for YES win)

        Returns:
            Net yield as a decimal (e.g., 0.04 for 4%)
        """
        cost = entry_price * shares
        proceeds = resolution_price * shares
        fees = cost * ESTIMATED_FEE_RATE + proceeds * ESTIMATED_FEE_RATE
        net_pnl = proceeds - cost - fees
        return net_pnl / cost if cost > 0 else 0.0
