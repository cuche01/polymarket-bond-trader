"""
Orderbook Imbalance Monitor (P0)

Runs on a faster cycle (every 20 seconds) than the main exit engine (60 seconds).
For each open position, fetches the current CLOB orderbook and computes:

1. bid_depth_ratio = total_bid_depth_usd / position_size_usd
2. bid_wall_change = current_bid_depth / previous_bid_depth (from last check)

Triggers:
- WARNING:  bid_depth_ratio < 2.0
- CRITICAL: bid_depth_ratio < 1.0 OR bid_wall_change < 0.30 (70% of bids pulled)
- EXIT:     bid_depth_ratio < 0.5 AND bid_wall_change < 0.30

The EXIT trigger sends a signal to exit the position pre-emptively,
before the price collapses.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class OrderbookMonitor:
    """Monitors orderbook health for open positions (live mode only)."""

    def __init__(
        self,
        config: dict,
        notifier: Any = None,
    ):
        self.config = config
        self.notifier = notifier

        ob_cfg = config.get("orderbook_monitor", {})
        self.check_interval = ob_cfg.get("orderbook_monitor_interval_seconds", 20)
        self.warning_ratio = ob_cfg.get("warning_bid_depth_ratio", 2.0)
        self.critical_ratio = ob_cfg.get("critical_bid_depth_ratio", 1.0)
        self.exit_ratio = ob_cfg.get("exit_bid_depth_ratio", 0.5)
        self.wall_pull_threshold = ob_cfg.get("bid_wall_pull_threshold", 0.30)

        # Cache previous bid depths for delta calculation
        # Key: position_id, Value: float (bid_depth_usd)
        self._prev_depths: Dict[int, float] = {}

    async def run_cycle(
        self,
        open_positions: List[Dict],
        clob_client: Any = None,
    ) -> List[Dict]:
        """
        Check orderbook health for all open positions.
        Returns list of exit signals for positions that fail the imbalance check.

        In paper mode (no clob_client), skips all checks.
        """
        if clob_client is None:
            return []

        exit_signals = []
        for position in open_positions:
            if position.get("paper_trade"):
                continue
            signal = await self._check_position(position, clob_client)
            if signal:
                exit_signals.append(signal)

        return exit_signals

    async def _check_position(
        self,
        position: Dict,
        clob_client: Any,
    ) -> Optional[Dict]:
        """
        Fetch orderbook for position's token_id.
        Compute bid_depth_ratio and bid_wall_change.
        Return exit signal if thresholds breached.
        """
        token_id = position.get("token_id", "")
        position_size = position.get("cost_basis", 0)
        position_id = position.get("id", 0)

        if not token_id or position_size <= 0:
            return None

        try:
            orderbook = clob_client.get_order_book(token_id)
        except Exception as e:
            logger.debug(f"Orderbook fetch failed for {token_id}: {e}")
            return None

        if not orderbook:
            return None

        bids = getattr(orderbook, "bids", []) or []
        if not bids:
            return None

        # Compute bid-side depth in USD
        bid_depth_usd = sum(
            float(getattr(b, "price", 0)) * float(getattr(b, "size", 0))
            for b in bids
        )

        # Compute ratios
        bid_depth_ratio = bid_depth_usd / position_size if position_size > 0 else 0

        # Compute bid wall change vs previous check
        prev_depth = self._prev_depths.get(position_id, 0)
        bid_wall_change = 1.0  # Default: no change
        if prev_depth > 0:
            bid_wall_change = bid_depth_usd / prev_depth

        # Update cache
        self._prev_depths[position_id] = bid_depth_usd

        # EXIT condition
        if bid_depth_ratio < self.exit_ratio and bid_wall_change < self.wall_pull_threshold:
            if self.notifier:
                await self.notifier.send_orderbook_alert(
                    position, bid_depth_ratio, bid_wall_change, "EXIT"
                )
            logger.critical(
                f"ORDERBOOK EXIT: position {position_id}, "
                f"bid_depth_ratio={bid_depth_ratio:.2f}, "
                f"bid_wall_change={bid_wall_change:.2f}"
            )
            return {
                "position_id": position_id,
                "action": "exit",
                "reason": "orderbook_imbalance_exit",
                "urgency": "immediate",
                "bid_depth_ratio": bid_depth_ratio,
                "bid_wall_change": bid_wall_change,
            }

        # CRITICAL warning (no exit, but alert)
        if bid_depth_ratio < self.critical_ratio or bid_wall_change < self.wall_pull_threshold:
            if self.notifier:
                await self.notifier.send_orderbook_alert(
                    position, bid_depth_ratio, bid_wall_change, "CRITICAL"
                )

        # WARNING (informational)
        elif bid_depth_ratio < self.warning_ratio:
            if self.notifier:
                await self.notifier.send_orderbook_alert(
                    position, bid_depth_ratio, bid_wall_change, "WARNING"
                )

        return None

    def cleanup_position(self, position_id: int) -> None:
        """Remove cached depth data when a position is closed."""
        self._prev_depths.pop(position_id, None)
