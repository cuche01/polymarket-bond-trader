"""
Position monitor for tracking open positions and triggering alerts/exits.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .utils import get_days_to_resolution, safe_json_parse

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


class PositionMonitor:
    """Monitors open positions for price alerts and resolution."""

    def __init__(self, config: dict):
        """
        Initialize position monitor with configuration.

        Args:
            config: Full configuration dictionary
        """
        self.config = config
        exits_cfg = config.get("exits", {})
        self.auto_exit_enabled = exits_cfg.get("auto_exit_enabled", True)
        self.yellow_threshold = exits_cfg.get("yellow_alert_threshold", 0.92)
        self.orange_threshold = exits_cfg.get("orange_alert_threshold", 0.88)
        self.red_threshold = exits_cfg.get("red_alert_exit_threshold", 0.80)

        self._session: Optional[aiohttp.ClientSession] = None
        self._yellow_alert_positions: set = set()  # IDs of positions on yellow alert
        self._check_interval_normal = 60  # seconds
        self._check_interval_alert = 15   # seconds for yellow-alert positions

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def close(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    def check_position_health(
        self,
        position: Dict[str, Any],
        current_price: float,
    ) -> Optional[str]:
        """
        Determine alert level for a position based on current price.

        Args:
            position: Position dictionary
            current_price: Current YES price

        Returns:
            Alert level string: None, 'yellow', 'orange', or 'red'
        """
        entry_price = position.get("entry_price", 0)
        if entry_price <= 0:
            return None

        # Alert thresholds are absolute price levels
        if current_price <= self.red_threshold:
            return "red"
        elif current_price <= self.orange_threshold:
            return "orange"
        elif current_price <= self.yellow_threshold:
            return "yellow"
        return None

    async def get_current_price(
        self,
        market_id: str,
        token_id: Optional[str] = None,
        clob_client: Optional[Any] = None,
    ) -> Optional[float]:
        """
        Get the current YES price for a market.

        Tries CLOB client first, falls back to Gamma API.

        Args:
            market_id: Market condition ID
            token_id: YES token ID (for CLOB)
            clob_client: Optional CLOB client for orderbook price

        Returns:
            Current YES price or None
        """
        # Try CLOB orderbook first for best accuracy
        if clob_client and token_id:
            try:
                orderbook = clob_client.get_order_book(token_id)
                if orderbook:
                    bids = getattr(orderbook, "bids", []) or []
                    asks = getattr(orderbook, "asks", []) or []
                    if bids and asks:
                        best_bid = float(getattr(bids[0], "price", 0))
                        best_ask = float(getattr(asks[0], "price", 0))
                        if best_bid > 0 and best_ask > 0:
                            return (best_bid + best_ask) / 2
                    elif bids:
                        return float(getattr(bids[0], "price", 0))
            except Exception as e:
                logger.debug(f"CLOB price check failed for {token_id}: {e}")

        # Fall back to Gamma API
        try:
            session = await self._get_session()
            url = f"{GAMMA_API_BASE}/markets/{market_id}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    outcome_prices = safe_json_parse(data.get("outcomePrices"))
                    if outcome_prices and len(outcome_prices) >= 1:
                        return float(outcome_prices[0])
                    last_price = data.get("lastTradePrice") or data.get("price")
                    if last_price:
                        return float(last_price)
        except Exception as e:
            logger.warning(f"Gamma API price check failed for {market_id}: {e}")

        return None

    async def check_resolution(
        self,
        position: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if a market has resolved and what the outcome was.

        Args:
            position: Position dictionary

        Returns:
            (resolved, outcome) where outcome is 'YES', 'NO', or None
        """
        market_id = position.get("market_id", "")
        if not market_id:
            return False, None

        try:
            session = await self._get_session()
            url = f"{GAMMA_API_BASE}/markets/{market_id}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False, None

                data = await resp.json()

                # Check if market is resolved/closed
                if data.get("resolved") or data.get("closed"):
                    # Determine outcome from resolution fields
                    winner = data.get("winner") or data.get("resolvedOutcome")
                    if winner:
                        outcome = "YES" if str(winner).upper() in ("YES", "1", "TRUE") else "NO"
                        return True, outcome

                    # Check outcome prices at resolution (1.0 = resolved YES)
                    outcome_prices = safe_json_parse(data.get("outcomePrices"))
                    if outcome_prices:
                        yes_price = float(outcome_prices[0])
                        if yes_price >= 0.99:
                            return True, "YES"
                        elif yes_price <= 0.01:
                            return True, "NO"

                # Check if past end date but not yet resolved
                end_date = data.get("endDate") or data.get("end_date_iso")
                if end_date:
                    days_remaining = get_days_to_resolution(end_date)
                    if days_remaining < -1:  # More than 1 day past end date
                        logger.warning(
                            f"Market {market_id} past end date by {-days_remaining:.1f} days, "
                            f"awaiting resolution"
                        )

        except Exception as e:
            logger.warning(f"Resolution check failed for {market_id}: {e}")

        return False, None

    async def handle_alert(
        self,
        position: Dict[str, Any],
        alert_level: str,
        executor: Any,
        notifier: Optional[Any],
        current_price: float,
        clob_client: Optional[Any] = None,
        paper_mode: bool = False,
    ) -> bool:
        """
        Handle an alert by notifying and optionally exiting the position.

        Args:
            position: Position dictionary
            alert_level: 'yellow', 'orange', or 'red'
            executor: OrderExecutor instance
            notifier: Optional Notifier instance
            current_price: Current market price
            clob_client: CLOB client for order placement
            paper_mode: If True, simulate exits

        Returns:
            True if position was exited, False otherwise
        """
        position_id = position.get("id", "unknown")
        market_question = position.get("market_question", "Unknown")

        logger.warning(
            f"Position {position_id} ({market_question[:40]}) | "
            f"{alert_level.upper()} alert at ${current_price:.4f}"
        )

        # Track yellow alert for increased check frequency
        if alert_level == "yellow":
            self._yellow_alert_positions.add(position_id)
        elif alert_level in ("orange", "red"):
            self._yellow_alert_positions.discard(position_id)

        # Send notification
        if notifier:
            try:
                await notifier.send_position_alert(position, alert_level, current_price)
            except Exception as e:
                logger.error(f"Failed to send alert notification: {e}")

        # Auto-exit on red alert
        if alert_level == "red" and self.auto_exit_enabled:
            logger.warning(
                f"Auto-exiting position {position_id} due to RED alert "
                f"(price ${current_price:.4f} <= ${self.red_threshold:.4f})"
            )
            if clob_client or paper_mode:
                success = await executor.execute_exit(
                    position, clob_client, paper_mode=paper_mode, current_price=current_price
                )
                if success:
                    self._yellow_alert_positions.discard(position_id)
                    return True
                else:
                    logger.error(f"Failed to auto-exit position {position_id}")
            return False

        return False

    async def monitor_positions(
        self,
        positions: List[Dict[str, Any]],
        clob_client: Any,
        executor: Any,
        notifier: Optional[Any],
        db: Optional[Any] = None,
        paper_mode: bool = False,
    ) -> Dict[str, Any]:
        """
        Monitor a batch of open positions for alerts and resolutions.

        Args:
            positions: List of open position dictionaries
            clob_client: CLOB client for price/order data
            executor: OrderExecutor for triggering exits
            notifier: Notifier for sending alerts
            db: Database for persisting updates
            paper_mode: If True, simulate exits

        Returns:
            Summary dict with alerts triggered, positions closed, etc.
        """
        results = {
            "positions_checked": len(positions),
            "alerts_triggered": [],
            "positions_closed": [],
            "positions_resolved": [],
            "errors": [],
        }

        check_tasks = []
        for position in positions:
            if position.get("status") != "open":
                continue
            check_tasks.append(
                self._check_single_position(
                    position, clob_client, executor, notifier, db, paper_mode
                )
            )

        # Run checks concurrently
        if check_tasks:
            check_results = await asyncio.gather(*check_tasks, return_exceptions=True)
            for i, result in enumerate(check_results):
                if isinstance(result, Exception):
                    logger.error(f"Error checking position {positions[i].get('id')}: {result}")
                    results["errors"].append(str(result))
                elif result:
                    action, data = result
                    if action == "alert":
                        results["alerts_triggered"].append(data)
                    elif action == "closed":
                        results["positions_closed"].append(data)
                    elif action == "resolved":
                        results["positions_resolved"].append(data)

        return results

    async def _check_single_position(
        self,
        position: Dict[str, Any],
        clob_client: Any,
        executor: Any,
        notifier: Optional[Any],
        db: Optional[Any],
        paper_mode: bool,
    ) -> Optional[Tuple[str, Any]]:
        """
        Check a single position for alerts and resolution.

        Returns:
            Tuple of (action, data) or None
        """
        market_id = position.get("market_id", "")
        token_id = position.get("token_id", "")
        position_id = position.get("id")

        # Get current price
        current_price = await self.get_current_price(market_id, token_id, clob_client)
        if current_price is None:
            logger.debug(f"Could not get price for position {position_id}")
            return None

        # Update position with current price for display
        position["_current_price"] = current_price

        # Check if resolved
        resolved, outcome = await self.check_resolution(position)
        if resolved:
            resolution_price = 1.0 if outcome == "YES" else 0.0
            logger.info(
                f"Position {position_id} resolved: outcome={outcome}, "
                f"price=${resolution_price}"
            )

            success = await executor.execute_exit(
                position, clob_client, paper_mode=paper_mode,
                current_price=resolution_price
            )

            if success and db:
                db.update_position(position_id, {
                    "status": "closed",
                    "exit_price": position.get("exit_price", resolution_price),
                    "exit_time": position.get("exit_time"),
                    "pnl": position.get("pnl", 0),
                    "fees_paid": position.get("fees_paid", 0),
                })

            if notifier and success:
                market_data = {"question": position.get("market_question", "")}
                await notifier.send_trade_alert(market_data, position)

            return ("resolved", {"position_id": position_id, "outcome": outcome})

        # Check alert level
        alert_level = self.check_position_health(position, current_price)
        if alert_level:
            if db:
                db.log_alert(
                    position_id,
                    alert_level,
                    current_price,
                    f"Price dropped to ${current_price:.4f}",
                )

            exited = await self.handle_alert(
                position, alert_level, executor, notifier,
                current_price, clob_client, paper_mode
            )

            if exited and db:
                db.update_position(position_id, {
                    "status": "closed",
                    "exit_price": position.get("exit_price", current_price),
                    "exit_time": position.get("exit_time"),
                    "pnl": position.get("pnl", 0),
                    "fees_paid": position.get("fees_paid", 0),
                })

            return ("closed" if exited else "alert", {
                "position_id": position_id,
                "alert_level": alert_level,
                "current_price": current_price,
            })

        return None

    async def cleanup_stale_orders(
        self,
        clob_client: Any,
        max_age_seconds: int = 3600,
    ) -> int:
        """
        Cancel any orders that have been open too long.

        Args:
            clob_client: CLOB client for order management
            max_age_seconds: Maximum age of orders to keep

        Returns:
            Number of orders cancelled
        """
        cancelled_count = 0
        try:
            open_orders = clob_client.get_orders()
            if not open_orders:
                return 0

            now = datetime.now(timezone.utc).timestamp()
            for order in open_orders:
                created_at = getattr(order, "created_at", None)
                if created_at:
                    try:
                        age = now - float(created_at)
                        if age > max_age_seconds:
                            order_id = getattr(order, "id", None)
                            if order_id:
                                result = clob_client.cancel(order_id)
                                if result:
                                    cancelled_count += 1
                                    logger.info(
                                        f"Cancelled stale order {order_id} "
                                        f"({age/3600:.1f}h old)"
                                    )
                    except (ValueError, TypeError) as e:
                        logger.debug(f"Error processing order age: {e}")

        except Exception as e:
            logger.warning(f"Stale order cleanup failed: {e}")

        if cancelled_count:
            logger.info(f"Cleaned up {cancelled_count} stale orders")
        return cancelled_count
