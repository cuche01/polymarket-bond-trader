"""
Notification system for trade alerts, warnings, and reports.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import os

import aiohttp

logger = logging.getLogger(__name__)


class Notifier:
    """Sends notifications via Discord webhook or other HTTP endpoints."""

    def __init__(self, config: dict, paper_mode: bool = False):
        """
        Initialize notifier with configuration.

        Args:
            config: Configuration dictionary with 'notifications' section
            paper_mode: Whether the bot is running in paper trading mode
        """
        notif_config = config.get("notifications", {})
        self.enabled = notif_config.get("enabled", False)
        self.webhook_url = notif_config.get("webhook_url", "") or os.getenv("WEBHOOK_URL", "")
        self.alert_on_trade = notif_config.get("alert_on_trade", True)
        self.alert_on_warning = notif_config.get("alert_on_warning", True)
        self.alert_on_error = notif_config.get("alert_on_error", True)
        self.daily_summary = notif_config.get("daily_summary", True)
        self.performance_summary = notif_config.get("performance_summary", True)
        self.paper_mode = paper_mode
        self._session: Optional[aiohttp.ClientSession] = None

    def _mode_tag(self) -> str:
        """Return [PAPER] or [LIVE] tag for notification titles."""
        return "[PAPER]" if self.paper_mode else "[LIVE]"

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post_webhook(self, payload: Dict[str, Any]) -> bool:
        """
        Send a POST request to the webhook URL.

        Args:
            payload: Discord embed payload

        Returns:
            True if sent successfully
        """
        if not self.enabled or not self.webhook_url:
            logger.debug("Notifications disabled or no webhook URL configured")
            return False

        try:
            session = await self._get_session()
            async with session.post(self.webhook_url, json=payload) as resp:
                if resp.status in (200, 204):
                    return True
                else:
                    body = await resp.text()
                    logger.warning(
                        f"Webhook returned {resp.status}: {body[:200]}"
                    )
                    return False
        except asyncio.TimeoutError:
            logger.error("Webhook request timed out")
            return False
        except Exception as e:
            logger.error(f"Failed to send webhook: {e}")
            return False

    def _color_for_level(self, level: str) -> int:
        """Get Discord embed color code for alert level."""
        colors = {
            "info": 0x3498DB,       # Blue
            "success": 0x2ECC71,    # Green
            "warning": 0xF39C12,    # Orange/Yellow
            "error": 0xE74C3C,      # Red
            "yellow": 0xF1C40F,     # Yellow
            "orange": 0xE67E22,     # Orange
            "red": 0xE74C3C,        # Red
        }
        return colors.get(level.lower(), 0x95A5A6)

    async def send_trade_alert(
        self,
        market: Dict[str, Any],
        position: Dict[str, Any],
        portfolio_summary: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send a trade execution alert with portfolio context.

        Args:
            market: Market data dictionary
            position: Position data dictionary
            portfolio_summary: Optional dict with balance, deployed, unrealized_pnl

        Returns:
            True if sent successfully
        """
        if not self.alert_on_trade:
            return True

        action = "OPENED" if position.get("status") == "open" else "CLOSED"
        entry_price = position.get("entry_price", 0)
        shares = position.get("shares", 0)
        cost_basis = position.get("cost_basis", 0)
        pnl = position.get("pnl")
        bond_score = position.get("bond_score", 0)

        title = f"{self._mode_tag()} Trade {action}: {market.get('question', 'Unknown Market')[:80]}"

        fields = [
            {"name": "Market ID", "value": f"`{position.get('market_id', 'N/A')}`", "inline": True},
            {"name": "Entry Price", "value": f"${entry_price:.4f}", "inline": True},
            {"name": "Shares", "value": f"{shares:,.2f}", "inline": True},
            {"name": "Cost Basis", "value": f"${cost_basis:,.2f}", "inline": True},
            {"name": "Bond Score", "value": f"{bond_score:.6f}", "inline": True},
        ]

        if position.get("expected_resolution"):
            fields.append({
                "name": "Expected Resolution",
                "value": position["expected_resolution"][:50],
                "inline": True,
            })

        if pnl is not None:
            exit_price = position.get("exit_price", 0)
            pnl_pct = (pnl / cost_basis * 100) if cost_basis > 0 else 0
            fields.extend([
                {"name": "Exit Price", "value": f"${exit_price:.4f}", "inline": True},
                {"name": "P&L", "value": f"${pnl:+,.2f} ({pnl_pct:+.2f}%)", "inline": True},
            ])

        # Portfolio context
        if portfolio_summary:
            balance = portfolio_summary.get("balance", 0)
            deployed = portfolio_summary.get("deployed", 0)
            unrealized_pnl = portfolio_summary.get("unrealized_pnl", 0)
            portfolio_value = balance + unrealized_pnl
            cash_available = balance - deployed
            fields.extend([
                {"name": "Portfolio Value", "value": f"${portfolio_value:,.2f}", "inline": True},
                {"name": "Cash Available", "value": f"${cash_available:,.2f}", "inline": True},
                {"name": "Capital Deployed", "value": f"${deployed:,.2f}", "inline": True},
            ])

        color = self._color_for_level("success" if action == "OPENED" else "info")
        if pnl is not None and pnl < 0:
            color = self._color_for_level("error")

        payload = {
            "embeds": [{
                "title": title,
                "color": color,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_warning(self, message: str, level: str = "warning") -> bool:
        """
        Send a warning notification.

        Args:
            message: Warning message text
            level: Severity level ('warning', 'yellow', 'orange', 'red')

        Returns:
            True if sent successfully
        """
        if not self.alert_on_warning:
            return True

        level_emoji = {
            "warning": "⚠️",
            "yellow": "🟡",
            "orange": "🟠",
            "red": "🔴",
        }.get(level, "⚠️")

        payload = {
            "embeds": [{
                "title": f"{self._mode_tag()} {level_emoji} {level.upper()} Alert",
                "description": message,
                "color": self._color_for_level(level),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_position_alert(
        self,
        position: Dict[str, Any],
        alert_level: str,
        current_price: float,
    ) -> bool:
        """
        Send a position price alert.

        Args:
            position: Position dictionary
            alert_level: Alert level ('yellow', 'orange', 'red')
            current_price: Current YES price

        Returns:
            True if sent successfully
        """
        entry_price = position.get("entry_price", 0)
        price_change = current_price - entry_price
        price_change_pct = (price_change / entry_price * 100) if entry_price > 0 else 0

        level_labels = {
            "yellow": "Price Decline Warning",
            "orange": "Significant Price Drop",
            "red": "CRITICAL: Auto-Exit Triggered",
        }

        title = f"{self._mode_tag()} {level_labels.get(alert_level, 'Alert')}"
        description = (
            f"**Market:** {position.get('market_question', 'Unknown')[:80]}\n"
            f"**Entry Price:** ${entry_price:.4f}\n"
            f"**Current Price:** ${current_price:.4f}\n"
            f"**Change:** ${price_change:+.4f} ({price_change_pct:+.2f}%)"
        )

        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": self._color_for_level(alert_level),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_error(self, message: str) -> bool:
        """
        Send an error notification.

        Args:
            message: Error message text

        Returns:
            True if sent successfully
        """
        if not self.alert_on_error:
            return True

        payload = {
            "embeds": [{
                "title": f"{self._mode_tag()} Error",
                "description": message[:2000],
                "color": self._color_for_level("error"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_daily_report(self, stats: Dict[str, Any]) -> bool:
        """
        Send a daily performance summary with full portfolio breakdown.

        Args:
            stats: Performance statistics dictionary

        Returns:
            True if sent successfully
        """
        if not self.daily_summary:
            return True

        date = stats.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        realized_pnl = stats.get("realized_pnl", 0.0)
        unrealized_pnl = stats.get("unrealized_pnl", 0.0)
        total_pnl = realized_pnl + unrealized_pnl
        wins = stats.get("win_count", 0)
        losses = stats.get("loss_count", 0)
        total_closed = stats.get("trades_closed", 0)
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0
        fees_paid = stats.get("fees_paid", 0.0)

        color = self._color_for_level("success" if total_pnl >= 0 else "error")

        fields = [
            {"name": "Realized P&L", "value": f"${realized_pnl:+,.2f}", "inline": True},
            {"name": "Unrealized P&L", "value": f"${unrealized_pnl:+,.2f}", "inline": True},
            {"name": "Total P&L", "value": f"${total_pnl:+,.2f}", "inline": True},
            {"name": "Trades Opened", "value": str(stats.get("trades_opened", 0)), "inline": True},
            {"name": "Trades Closed", "value": str(total_closed), "inline": True},
            {"name": "Win Rate", "value": f"{win_rate:.1f}% ({wins}W/{losses}L)", "inline": True},
            {"name": "Fees Paid", "value": f"${fees_paid:,.2f}", "inline": True},
        ]

        # Portfolio value breakdown
        portfolio_balance = stats.get("portfolio_balance", 0)
        if portfolio_balance:
            portfolio_value = portfolio_balance + unrealized_pnl
            deployed = stats.get("total_deployed", 0)
            deployed_pct = (deployed / portfolio_balance * 100) if portfolio_balance > 0 else 0
            cash_available = portfolio_balance - deployed
            fields.extend([
                {"name": "Portfolio Value", "value": f"${portfolio_value:,.2f}", "inline": True},
                {"name": "Capital Deployed", "value": f"${deployed:,.2f} ({deployed_pct:.1f}%)", "inline": True},
                {"name": "Cash Available", "value": f"${cash_available:,.2f}", "inline": True},
            ])

        payload = {
            "embeds": [{
                "title": f"{self._mode_tag()} Daily Report — {date}",
                "color": color,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_performance_summary(self, summary: Dict[str, Any]) -> bool:
        """
        Send a comprehensive lifetime performance summary to Discord.

        Separate from send_daily_report (which is per-day). This embed shows
        all-time metrics: win rate, R:R, profit factor, expectancy, streaks,
        drawdown, exit-reason breakdown.

        Args:
            summary: Output of Database.get_performance_summary()

        Returns:
            True if sent successfully
        """
        if not self.performance_summary:
            return True

        closed = summary.get("closed_trades", 0)
        total_pnl = summary.get("total_pnl", 0.0)

        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if closed == 0:
            # Minimal embed for the "no trades yet" case so the webhook
            # call doesn't crash on empty stats.
            payload = {
                "embeds": [{
                    "title": f"{self._mode_tag()} Performance Summary — {date}",
                    "color": self._color_for_level("info"),
                    "description": "No closed trades yet.",
                    "fields": [
                        {"name": "Open Positions", "value": str(summary.get("open_trades", 0)), "inline": True},
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": {"text": "Polymarket Bond Bot"},
                }]
            }
            return await self._post_webhook(payload)

        color = self._color_for_level("success" if total_pnl >= 0 else "error")

        # Build fields grouped logically. Discord caps at 25 fields per embed;
        # this layout uses 18 at max so we stay well under the limit.
        fields = [
            # Overview
            {"name": "Closed Trades", "value": str(closed), "inline": True},
            {"name": "Open Trades", "value": str(summary.get("open_trades", 0)), "inline": True},
            {"name": "Win Rate", "value": (
                f"{summary.get('win_rate', 0.0):.1f}% "
                f"({summary.get('wins', 0)}W/{summary.get('losses', 0)}L)"
            ), "inline": True},

            # P&L
            {"name": "Total P&L", "value": f"${total_pnl:+,.2f}", "inline": True},
            {"name": "ROI on Deployed", "value": f"{summary.get('roi_on_deployed', 0.0):+.2f}%", "inline": True},
            {"name": "Fees Paid", "value": f"${summary.get('fees_paid', 0.0):,.2f}", "inline": True},

            # Win stats
            {"name": "Avg Win", "value": (
                f"${summary.get('avg_win', 0.0):+,.2f} "
                f"({summary.get('avg_win_pct', 0.0):+.2f}%)"
            ), "inline": True},
            {"name": "Max Win", "value": f"${summary.get('max_win', 0.0):+,.2f}", "inline": True},
            {"name": "Max Win Streak", "value": str(summary.get("max_consecutive_wins", 0)), "inline": True},

            # Loss stats
            {"name": "Avg Loss", "value": (
                f"${summary.get('avg_loss', 0.0):+,.2f} "
                f"({summary.get('avg_loss_pct', 0.0):+.2f}%)"
            ), "inline": True},
            {"name": "Max Loss", "value": f"${summary.get('max_loss', 0.0):+,.2f}", "inline": True},
            {"name": "Max Loss Streak", "value": str(summary.get("max_consecutive_losses", 0)), "inline": True},

            # Risk/Reward
            {"name": "Avg R:R", "value": f"{summary.get('rr_ratio', 0.0):.2f}", "inline": True},
            {"name": "Profit Factor", "value": f"{summary.get('profit_factor', 0.0):.2f}", "inline": True},
            {"name": "Expectancy / Trade", "value": f"${summary.get('expectancy', 0.0):+,.2f}", "inline": True},

            # Drawdown + timing
            {"name": "Peak Cum P&L", "value": f"${summary.get('peak_cum_pnl', 0.0):+,.2f}", "inline": True},
            {"name": "Max Drawdown", "value": f"${summary.get('max_drawdown_from_peak', 0.0):,.2f}", "inline": True},
            {"name": "Avg Hold", "value": f"{summary.get('avg_hold_hours', 0.0):.1f}h", "inline": True},
        ]

        # Exit reason breakdown — single multi-line field, not per-reason
        breakdown = summary.get("exit_reason_breakdown") or {}
        if breakdown:
            lines = [
                f"{reason}: {count}"
                for reason, count in sorted(breakdown.items(), key=lambda kv: -kv[1])
            ]
            fields.append({
                "name": "Exit Reasons",
                "value": "\n".join(lines),
                "inline": False,
            })

        payload = {
            "embeds": [{
                "title": f"{self._mode_tag()} Performance Summary — {date}",
                "color": color,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_hourly_snapshot(self, portfolio: Dict[str, Any]) -> bool:
        """
        Send an hourly portfolio snapshot with full value breakdown.

        Args:
            portfolio: Portfolio status dictionary

        Returns:
            True if sent successfully
        """
        balance = portfolio.get("balance", 0.0)
        deployed = portfolio.get("deployed", 0.0)
        available = portfolio.get("available", 0.0)
        unrealized_pnl = portfolio.get("unrealized_pnl", 0.0)
        open_positions = portfolio.get("open_positions", 0)
        deployed_pct = (deployed / balance * 100) if balance > 0 else 0
        portfolio_value = balance + unrealized_pnl

        fields = [
            {"name": "Portfolio Value", "value": f"${portfolio_value:,.2f}", "inline": True},
            {"name": "Balance", "value": f"${balance:,.2f}", "inline": True},
            {"name": "Deployed", "value": f"${deployed:,.2f} ({deployed_pct:.1f}%)", "inline": True},
            {"name": "Available", "value": f"${available:,.2f}", "inline": True},
            {"name": "Unrealized P&L", "value": f"${unrealized_pnl:+,.2f}", "inline": True},
            {"name": "Open Positions", "value": str(open_positions), "inline": True},
        ]

        # Additional P&L context if provided
        if portfolio.get("realized_pnl_today") is not None:
            fields.append({
                "name": "Realized P&L Today",
                "value": f"${portfolio['realized_pnl_today']:+,.2f}",
                "inline": True,
            })
        if portfolio.get("all_time_pnl") is not None:
            fields.append({
                "name": "All-Time P&L",
                "value": f"${portfolio['all_time_pnl']:+,.2f}",
                "inline": True,
            })

        payload = {
            "embeds": [{
                "title": f"{self._mode_tag()} Hourly Portfolio Snapshot",
                "color": self._color_for_level("info"),
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_critical(self, message: str) -> bool:
        """
        Send a critical alert (e.g., severe stop-loss slippage per Addendum A1).

        Args:
            message: Critical alert message text

        Returns:
            True if sent successfully
        """
        payload = {
            "embeds": [{
                "title": f"{self._mode_tag()} CRITICAL Alert",
                "description": message[:2000],
                "color": 0xFF0000,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_startup_notification(self, mode: str = "live") -> bool:
        """
        Send a bot startup notification.

        Args:
            mode: Trading mode ('live' or 'paper')

        Returns:
            True if sent successfully
        """
        payload = {
            "embeds": [{
                "title": f"{self._mode_tag()} Bond Bot Started ({mode.upper()} mode)",
                "description": "The Polymarket Bond Strategy Bot has started successfully.",
                "color": self._color_for_level("success"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_shutdown_notification(self, reason: str = "User requested") -> bool:
        """
        Send a bot shutdown notification.

        Args:
            reason: Reason for shutdown

        Returns:
            True if sent successfully
        """
        payload = {
            "embeds": [{
                "title": f"{self._mode_tag()} Bond Bot Stopping",
                "description": f"Reason: {reason}",
                "color": self._color_for_level("warning"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_teleportation_alert(
        self,
        position: Dict[str, Any],
        entry_price: float,
        current_price: float,
        drop_pct: float,
    ) -> bool:
        """P0: CRITICAL alert for price teleportation events."""
        question = position.get("market_question", "Unknown")[:80]
        pos_id = position.get("id", "?")
        loss_usd = (entry_price - current_price) * position.get("shares", 0)

        payload = {
            "embeds": [{
                "title": f"{self._mode_tag()} TELEPORTATION DETECTED",
                "description": (
                    f"**Position #{pos_id}**: {question}\n\n"
                    f"Entry: ${entry_price:.4f} -> Current: ${current_price:.4f}\n"
                    f"**Drop: {drop_pct:.1%}** | Est. Loss: ${loss_usd:,.2f}\n\n"
                    f"Price gapped past stop-loss. Emergency exit initiated."
                ),
                "color": 0xFF0000,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot — P0 Teleportation Protection"},
            }]
        }
        return await self._post_webhook(payload)

    async def send_orderbook_alert(
        self,
        position: Dict[str, Any],
        bid_depth_ratio: float,
        bid_wall_change: float,
        level: str = "WARNING",
    ) -> bool:
        """P0: Alert for orderbook imbalance detection."""
        question = position.get("market_question", "Unknown")[:80]
        pos_id = position.get("id", "?")
        color = 0xFF0000 if level in ("CRITICAL", "EXIT") else 0xFFAA00

        payload = {
            "embeds": [{
                "title": f"{self._mode_tag()} Orderbook {level}",
                "description": (
                    f"**Position #{pos_id}**: {question}\n\n"
                    f"Bid Depth Ratio: {bid_depth_ratio:.2f}x position size\n"
                    f"Bid Wall Change: {bid_wall_change:.1%} vs previous\n"
                    f"{'**Pre-emptive exit triggered.**' if level == 'EXIT' else 'Monitoring closely.'}"
                ),
                "color": color,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Bond Bot — P0 Orderbook Monitor"},
            }]
        }
        return await self._post_webhook(payload)
