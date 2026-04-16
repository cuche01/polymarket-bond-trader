"""
Polymarket Bond Strategy Bot - Main Entry Point

Orchestrates market scanning, opportunity detection, position management,
and portfolio monitoring for the bond strategy.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# Load environment variables before importing modules
load_dotenv()

from src.blacklist_learner import BlacklistLearner
from src.database import Database
from src.dashboard import Dashboard
from src.detector import PseudoCertaintyDetector
from src.executor import OrderExecutor
from src.exit_engine import ExitEngine
from src.monitor import PositionMonitor
from src.notifications import Notifier
from src.orderbook_monitor import OrderbookMonitor
from src.portfolio_manager import PortfolioManager
from src.risk_engine import RiskEngine
from src.scanner import MarketScanner
from src.utils import (
    feature_enabled,
    is_halt_requested,
    load_config,
    setup_logging,
)

logger = logging.getLogger(__name__)


class BondBot:
    """
    Main orchestrator for the Polymarket Bond Strategy Bot.

    Coordinates scanning, detection, risk management, execution,
    and monitoring in an async event loop.
    """

    def __init__(self, config: dict, paper_mode: bool = False):
        """
        Initialize the BondBot with configuration.

        Args:
            config: Full configuration dictionary
            paper_mode: If True, simulate trades without real API calls
        """
        self.config = config
        self.paper_mode = paper_mode
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Portfolio state
        self.portfolio_balance: float = 0.0
        self.daily_pnl: float = 0.0
        self._initial_paper_balance: float = config.get("paper_balance", 10000.0)

        # Initialize modules
        log_cfg = config.get("logging", {})
        db_path = log_cfg.get("db_path", "data/bond_bot.db")

        self.db = Database(db_path)
        self.scanner = MarketScanner(config)
        self.detector = PseudoCertaintyDetector(config)
        self.executor = OrderExecutor(config)
        self.monitor = PositionMonitor(config)
        self.notifier = Notifier(config, paper_mode=paper_mode)
        self.dashboard = Dashboard(config)

        # New risk/exit modules
        self.portfolio_manager = PortfolioManager(self.db)
        self.risk_engine = RiskEngine(config, self.portfolio_manager)
        self.exit_engine = ExitEngine(
            config, self.portfolio_manager, self.risk_engine, self.notifier
        )
        self.orderbook_monitor = OrderbookMonitor(config, self.notifier)
        self.blacklist_learner = BlacklistLearner(self.db, config)

        # CLOB client (initialized in startup)
        self.clob_client: Optional[Any] = None

        # Cached data
        self._open_positions: List[Dict] = []
        self._candidates: List[Dict] = []
        self._last_hourly_task: float = 0
        self._last_daily_task: float = 0
        # Once-per-day dedupe for the lifetime performance summary notification.
        # Stored as a UTC date string so restarts mid-window don't re-fire.
        self._last_perf_summary_date: Optional[str] = None
        self._perf_summary_hour_utc: int = int(
            config.get("notifications", {}).get("performance_summary_hour_utc", 0)
        )

    def _init_clob_client(self) -> Optional[Any]:
        """
        Initialize the CLOB client with credentials from environment.

        Returns:
            Initialized ClobClient or None in paper mode
        """
        if self.paper_mode:
            logger.info("Paper mode: CLOB client not initialized (simulated trading)")
            return None

        private_key = os.getenv("PRIVATE_KEY")
        proxy_wallet = os.getenv("PROXY_WALLET_ADDR")

        if not private_key:
            logger.error("PRIVATE_KEY environment variable not set")
            return None
        if not proxy_wallet:
            logger.error("PROXY_WALLET_ADDR environment variable not set")
            return None

        try:
            from py_clob_client.client import ClobClient

            host = "https://clob.polymarket.com"
            chain_id = 137  # Polygon
            sig_type = self.config.get("wallet", {}).get("signature_type", 1)

            client = ClobClient(
                host,
                key=private_key,
                chain_id=chain_id,
                signature_type=sig_type,
                funder=proxy_wallet,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            logger.info("CLOB client initialized successfully")
            return client

        except ImportError:
            logger.error("py-clob-client not installed. Run: pip install py-clob-client")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}", exc_info=True)
            return None

    async def _get_portfolio_balance(self) -> float:
        """
        Get current portfolio balance from CLOB or use fallback.

        Returns:
            Portfolio balance in USD
        """
        if self.paper_mode:
            # Paper mode: initial balance + all realized P&L from closed trades
            total_pnl = self.db.get_all_time_stats(paper_trade=True).get("total_pnl", 0.0)
            self.portfolio_balance = self._initial_paper_balance + total_pnl
            return self.portfolio_balance

        if not self.clob_client:
            return self.portfolio_balance

        try:
            balance_info = self.clob_client.get_balance()
            if balance_info:
                balance = float(getattr(balance_info, "balance", 0) or balance_info)
                self.portfolio_balance = balance
                return balance
        except Exception as e:
            logger.warning(f"Could not fetch portfolio balance: {e}")

        return self.portfolio_balance

    async def startup(self) -> bool:
        """
        Initialize all modules and recover existing open positions.

        Returns:
            True if startup succeeded
        """
        logger.info(
            f"Starting Polymarket Bond Bot (mode={'PAPER' if self.paper_mode else 'LIVE'})"
        )

        # Initialize CLOB client
        self.clob_client = self._init_clob_client()
        if not self.paper_mode and not self.clob_client:
            logger.error("Failed to initialize CLOB client in live mode")
            return False

        # Get initial portfolio balance
        self.portfolio_balance = await self._get_portfolio_balance()
        logger.info(f"Portfolio balance: ${self.portfolio_balance:,.2f}")
        self.portfolio_manager.set_portfolio_balance(
            self.portfolio_balance, paper_mode=self.paper_mode
        )

        # Recover open positions from database
        self._open_positions = self.db.get_open_positions(
            paper_trade=self.paper_mode
        )
        logger.info(f"Recovered {len(self._open_positions)} open positions from database")

        # Retroactively fix any positions stuck in the 'other' bucket
        reclassified = self.db.reclassify_open_positions(
            self.risk_engine.bucket_classifier
        )
        if reclassified:
            logger.info(f"Startup reclassification: fixed {reclassified} position(s)")

        # Send startup notification
        await self.notifier.send_startup_notification(
            "paper" if self.paper_mode else "live"
        )

        self.dashboard.add_log_entry(
            f"Bot started in {'PAPER' if self.paper_mode else 'LIVE'} mode"
        )
        self.dashboard.add_log_entry(
            f"Balance: ${self.portfolio_balance:,.2f} | "
            f"Open positions: {len(self._open_positions)}"
        )

        return True

    async def scan_cycle(self) -> int:
        """
        Execute one full scan-filter-validate-size-execute cycle.

        Returns:
            Number of trades executed
        """
        scan_start = time.time()
        trades_executed = 0
        error_message = None

        try:
            logger.info("Starting scan cycle...")

            # Scan and rank markets
            candidates = await self.scanner.run_scan_cycle()
            self._candidates = candidates
            logger.info(f"Found {len(candidates)} bond candidates")

            # Get current balance
            self.portfolio_balance = await self._get_portfolio_balance()

            all_open = self._open_positions

            # Sync portfolio manager with current balance
            self.portfolio_manager.set_portfolio_balance(
                self.portfolio_balance, paper_mode=self.paper_mode
            )

            # R6: Use target_position_pct directly instead of legacy RiskManager
            target_pct = self.config.get("risk", {}).get("target_position_pct", 0.025)

            # Process each candidate
            for market in candidates:
                if not self._running or is_halt_requested():
                    logger.info("HALT detected during scan, stopping")
                    break

                # Skip markets we already have a position in
                market_id = market.get("id") or market.get("conditionId", "")
                existing = self.db.get_position_by_market(market_id, status="open")
                if existing:
                    logger.debug(f"Already have position in {market_id}, skipping")
                    continue

                # Estimate position size for detector validation
                position_estimate = self.portfolio_balance * target_pct

                # P2: Apply blacklist learner penalty to market
                if feature_enabled(self.config, "blacklist_learner"):
                    bl_penalty = self.blacklist_learner.get_penalty(market)
                    market["_blacklist_penalty"] = bl_penalty

                is_valid, reason = await self.detector.is_valid_opportunity(
                    market, self.clob_client, position_estimate
                )

                if not is_valid:
                    logger.debug(f"Market rejected: {reason}")
                    self.db.log_rejection(
                        market_id=market_id,
                        market_question=market.get("question") or market.get("title", "")[:200],
                        layer=int(reason[2]) if reason.startswith("[L") else 0,
                        reason=reason,
                        yes_price=market.get("_yes_price", 0),
                        liquidity=market.get("_liquidity", 0),
                        days_to_resolution=market.get("_days_to_resolution", 0),
                    )
                    continue

                # Initial position size from target percentage
                position_size = self.portfolio_balance * target_pct

                # Run through risk engine (all 10 checks, may adjust size)
                category = market.get("category") or market.get("marketType", "")
                event_group_id = market.get("eventId") or market.get("event_id", "")
                approved, approval_reason, position_size = self.risk_engine.evaluate_entry(
                    market_id=market_id,
                    category=category,
                    event_group_id=event_group_id,
                    requested_size=position_size,
                    entry_price=market.get("_yes_price", 0),
                    market_question=market.get("question") or market.get("title", ""),
                    market_volume_24h=market.get("_volume_24h", 0),
                    clob_client=self.clob_client,
                    token_id=market.get("_yes_token_id"),
                    days_to_resolution=market.get("_days_to_resolution", 7.0),
                )

                if not approved:
                    logger.info(f"Entry blocked by risk engine: {approval_reason}")
                    continue

                # Execute order
                position = await self.executor.execute_entry(
                    market=market,
                    position_size=position_size,
                    clob_client=self.clob_client,
                    paper_mode=self.paper_mode,
                )

                if position:
                    # Tag position with category and risk bucket before saving
                    position["category"] = category
                    position["event_group_id"] = event_group_id
                    position["risk_bucket"] = self.risk_engine.bucket_classifier.classify(
                        category,
                        market.get("question") or market.get("title", ""),
                    )
                    position["catalyst_type"] = market.get("_catalyst_type", "unknown")
                    position["binary_catalyst_score"] = market.get("_binary_catalyst_score", 0.0)
                    # Save to database
                    pos_id = self.db.save_position(position)
                    position["id"] = pos_id
                    self._open_positions.append(position)
                    trades_executed += 1

                    logger.info(
                        f"Trade executed: {market.get('question', '')[:50]} | "
                        f"Price=${position['entry_price']:.4f} | "
                        f"Size=${position['cost_basis']:.2f}"
                    )

                    # Build portfolio context for notification
                    portfolio_summary = self._build_portfolio_summary(all_open)
                    await self.notifier.send_trade_alert(market, position, portfolio_summary=portfolio_summary)
                    self.dashboard.add_log_entry(
                        f"ENTRY: {market.get('question', 'Unknown')[:40]} @ "
                        f"${position['entry_price']:.4f}"
                    )

        except Exception as e:
            error_message = str(e)
            logger.error(f"Scan cycle error: {e}", exc_info=True)
            await self.notifier.send_error(f"Scan cycle failed: {e}")

        finally:
            scan_duration_ms = int((time.time() - scan_start) * 1000)
            self.db.log_scan(
                markets_scanned=len(self._candidates),
                candidates_found=len([c for c in self._candidates if c.get("_bond_score", 0) > 0]),
                trades_executed=trades_executed,
                scan_duration_ms=scan_duration_ms,
                error_message=error_message,
            )

        return trades_executed

    def _build_portfolio_summary(self, positions: List[Dict] = None) -> Dict[str, Any]:
        """Build portfolio summary dict for notifications."""
        if positions is None:
            positions = self._open_positions
        deployed = sum(p.get("cost_basis", 0) for p in positions)
        unrealized_pnl = sum(
            (p.get("_current_price", p.get("entry_price", 0)) - p.get("entry_price", 0))
            * p.get("shares", 0)
            for p in positions
        )
        return {
            "balance": self.portfolio_balance,
            "deployed": deployed,
            "unrealized_pnl": unrealized_pnl,
        }

    async def exit_cycle(self) -> int:
        """
        Run the exit engine over all open positions.
        Exits run BEFORE entries so capital is freed and risk limits enforced first.

        Returns:
            Number of positions closed.
        """
        self._open_positions = self.db.get_open_positions(paper_trade=self.paper_mode)
        if not self._open_positions:
            return 0

        positions_closed = 0

        # Per-position exit decisions
        decisions = await self.exit_engine.evaluate_all_positions(
            self._open_positions, self.clob_client
        )
        for position, decision in decisions:
            if decision.action in ("close_full", "close_partial"):
                success = await self.executor.close_position(
                    position=position,
                    close_pct=decision.close_pct,
                    urgency=decision.urgency,
                    reason=decision.reason,
                    clob_client=self.clob_client,
                    paper_mode=self.paper_mode,
                    current_price=position.get("_current_price"),
                    notifier=self.notifier,
                    db=self.db,
                )
                if success:
                    positions_closed += 1
                    # P2: Record loss for blacklist learner
                    if decision.reason in ("stop_loss", "resolution_loss",
                                           "teleportation_detected", "teleportation_catastrophic"):
                        self.blacklist_learner.record_loss(position)
                    # V3: Fluke loss detection — pause bucket on outsized stop-loss
                    if decision.reason in ("stop_loss", "teleportation_detected",
                                           "teleportation_catastrophic"):
                        closed_pos = self.db.get_position_by_id(position.get("id"))
                        if closed_pos and closed_pos.get("pnl") is not None:
                            self.exit_engine.check_for_fluke_loss(position, closed_pos["pnl"])
                    # Flag teleportation exits in DB
                    if decision.reason.startswith("teleportation"):
                        self.db.update_position(position.get("id"), {"teleportation_flag": 1})
                    if decision.action == "close_full":
                        market_data = {"question": position.get("market_question", "")}
                        portfolio_summary = self._build_portfolio_summary()
                        await self.notifier.send_trade_alert(market_data, position, portfolio_summary=portfolio_summary)
                    logger.info(
                        f"EXIT [{decision.reason}]: {position.get('market_question', '')[:50]} "
                        f"| urgency={decision.urgency}"
                    )
                    self.dashboard.add_log_entry(
                        f"EXIT [{decision.reason}]: "
                        f"{position.get('market_question', 'Unknown')[:40]}"
                    )

        # Portfolio-level drawdown reduction
        drawdown_decisions = self.exit_engine.check_portfolio_drawdown(self._open_positions)
        for position, decision in drawdown_decisions:
            # Skip if already closed above
            if position.get("status") == "closed":
                continue
            success = await self.executor.close_position(
                position=position,
                close_pct=1.0,
                urgency="normal",
                reason="drawdown_reduction",
                clob_client=self.clob_client,
                paper_mode=self.paper_mode,
                current_price=position.get("_current_price"),
                notifier=self.notifier,
                db=self.db,
            )
            if success:
                positions_closed += 1

        if positions_closed:
            self._open_positions = self.db.get_open_positions(paper_trade=self.paper_mode)
            # Refresh paper balance to reflect realized P&L from closed positions
            if self.paper_mode:
                self.portfolio_balance = await self._get_portfolio_balance()

        return positions_closed

    async def monitor_cycle(self) -> None:
        """
        Check all open positions for alerts and resolutions.
        """
        self._open_positions = self.db.get_open_positions(paper_trade=self.paper_mode)

        if not self._open_positions:
            return

        logger.debug(f"Monitoring {len(self._open_positions)} open positions")

        results = await self.monitor.monitor_positions(
            positions=self._open_positions,
            clob_client=self.clob_client,
            executor=self.executor,
            notifier=self.notifier,
            db=self.db,
            paper_mode=self.paper_mode,
        )

        # Update in-memory position list and paper balance
        if results.get("positions_closed") or results.get("positions_resolved"):
            self._open_positions = self.db.get_open_positions(paper_trade=self.paper_mode)
            if self.paper_mode:
                self.portfolio_balance = await self._get_portfolio_balance()

        for alert in results.get("alerts_triggered", []):
            level = alert.get("alert_level", "warning")
            pos_id = alert.get("position_id")
            price = alert.get("current_price", 0)
            self.dashboard.add_alert(
                level,
                f"Position {pos_id}: {level.upper()} at ${price:.4f}"
            )

        for closed in results.get("positions_closed", []):
            self.dashboard.add_log_entry(
                f"EXIT (auto): position {closed.get('position_id')} closed"
            )

        for resolved in results.get("positions_resolved", []):
            outcome = resolved.get("outcome", "Unknown")
            pos_id = resolved.get("position_id")
            self.dashboard.add_log_entry(f"RESOLVED: position {pos_id} → {outcome}")

    async def hourly_tasks(self) -> None:
        """Execute hourly maintenance tasks."""
        logger.info("Running hourly tasks")
        now = time.time()
        self._last_hourly_task = now

        # Clean up stale orders
        if self.clob_client:
            await self.monitor.cleanup_stale_orders(self.clob_client)

        # Refresh balance before snapshot so realized P&L is reflected
        self.portfolio_balance = await self._get_portfolio_balance()

        # Send hourly snapshot enriched with P&L data
        snapshot = self.dashboard.generate_hourly_snapshot(
            self._open_positions, self.portfolio_balance
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_stats_hourly = self.db.get_daily_stats(today)
        snapshot["realized_pnl_today"] = daily_stats_hourly.get("realized_pnl", 0.0)
        snapshot["all_time_pnl"] = self.db.get_all_time_stats(paper_trade=self.paper_mode).get("total_pnl", 0.0)
        await self.notifier.send_hourly_snapshot(snapshot)

        # Upsert today's running totals so performance_daily reflects real-time
        # state throughout the day (daily_tasks only fires at 00:00 UTC and would
        # otherwise leave today's row unwritten until tomorrow).
        unrealized_pnl = sum(
            ((p.get("_current_price") or p.get("entry_price", 0)) - p.get("entry_price", 0))
            * p.get("shares", 0)
            for p in self._open_positions
        )
        self.db.upsert_daily_performance(today, {
            **daily_stats_hourly,
            "unrealized_pnl": unrealized_pnl,
            "portfolio_balance": self.portfolio_balance,
            "total_deployed": sum(p.get("cost_basis", 0) for p in self._open_positions),
        })

        # V3: Fee-drag analysis
        all_time = self.db.get_all_time_stats(paper_trade=self.paper_mode)
        total_fees = all_time.get("total_fees", 0)
        total_pnl = all_time.get("total_pnl", 0)
        gross_edge = total_pnl + total_fees if total_pnl > 0 else 0
        if gross_edge > 0:
            fee_drag_pct = total_fees / gross_edge * 100
            logger.info(
                f"Fee drag: {fee_drag_pct:.1f}% of gross edge "
                f"(${total_fees:.2f} fees / ${gross_edge:.2f} gross)"
            )
            if fee_drag_pct > 40:
                logger.warning(
                    f"Fee drag at {fee_drag_pct:.0f}% — target <40%. "
                    f"Consider raising min_net_yield or reducing trade frequency."
                )

        self.dashboard.add_log_entry("Hourly tasks completed")

    async def daily_tasks(self) -> None:
        """Execute daily maintenance tasks."""
        logger.info("Running daily tasks")
        now = time.time()
        self._last_daily_task = now

        # Finalize YESTERDAY's performance row. daily_tasks fires at 00:00 UTC,
        # so "today" at this moment has no closed trades yet — the day that just
        # ended is the one that needs a final aggregation.
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        daily_stats = self.db.get_daily_stats(yesterday)

        # Unrealized P&L is a point-in-time snapshot; record it on yesterday's
        # row so the closing balance is captured alongside realized P&L.
        unrealized_pnl = 0.0
        for pos in self._open_positions:
            current = pos.get("_current_price") or pos.get("entry_price", 0)
            entry = pos.get("entry_price", 0)
            shares = pos.get("shares", 0)
            unrealized_pnl += (current - entry) * shares

        self.db.upsert_daily_performance(yesterday, {
            **daily_stats,
            "unrealized_pnl": unrealized_pnl,
            "portfolio_balance": self.portfolio_balance,
            "total_deployed": sum(p.get("cost_basis", 0) for p in self._open_positions),
        })

        # Send daily report with portfolio breakdown
        deployed = sum(p.get("cost_basis", 0) for p in self._open_positions)
        await self.notifier.send_daily_report({
            **daily_stats,
            "unrealized_pnl": unrealized_pnl,
            "portfolio_balance": self.portfolio_balance,
            "total_deployed": deployed,
        })

        # Generate report for dashboard
        report = self.dashboard.generate_daily_report(self.db, paper_trade=self.paper_mode)
        logger.info(f"Daily report: {report}")
        self.dashboard.add_log_entry(
            f"Daily report ({yesterday}): {daily_stats.get('trades_closed', 0)} closed, "
            f"P&L=${daily_stats.get('realized_pnl', 0):+.2f}"
        )

    async def performance_summary_task(self) -> None:
        """
        Send the lifetime performance summary notification.

        Distinct from daily_tasks — this sends an all-time aggregation (win
        rate, R:R, profit factor, streaks, drawdown, exit-reason breakdown).
        Fires once per UTC day; dedupe is tracked via _last_perf_summary_date
        so restarts mid-window don't re-send.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        logger.info("Running performance summary task for %s", today)
        summary = self.db.get_performance_summary(paper_trade=self.paper_mode)
        await self.notifier.send_performance_summary(summary)
        self._last_perf_summary_date = today
        self.dashboard.add_log_entry(
            f"Performance summary sent: {summary.get('closed_trades', 0)} closed, "
            f"win_rate={summary.get('win_rate', 0):.1f}%, "
            f"total_pnl=${summary.get('total_pnl', 0):+.2f}"
        )

    async def _graceful_shutdown(self, reason: str = "User requested") -> None:
        """
        Perform graceful shutdown: cancel orders, save state.

        Args:
            reason: Shutdown reason for logging/notification
        """
        logger.info(f"Initiating graceful shutdown: {reason}")
        self._running = False

        # Cancel all open orders (live mode only)
        if self.clob_client and not self.paper_mode:
            try:
                count = await self.executor.cancel_all_open_orders(self.clob_client)
                logger.info(f"Cancelled {count} open orders during shutdown")
            except Exception as e:
                logger.error(f"Error cancelling orders during shutdown: {e}")

        # Send shutdown notification
        try:
            await self.notifier.send_shutdown_notification(reason)
        except Exception:
            pass

        # Close connections
        await self.scanner.close()
        await self.detector.close()
        await self.monitor.close()
        await self.exit_engine.close()
        await self.notifier.close()

        self._shutdown_event.set()
        logger.info("Graceful shutdown complete")

    def _setup_signal_handlers(self) -> None:
        """Register SIGINT/SIGTERM handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()

        def handle_signal(signum, frame):
            logger.info(f"Received signal {signum}, initiating shutdown...")
            asyncio.create_task(self._graceful_shutdown(f"Signal {signum}"))

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(
                        self._graceful_shutdown(f"Signal {s.name}")
                    ),
                )
            except (NotImplementedError, RuntimeError):
                # Windows doesn't support add_signal_handler
                signal.signal(sig, handle_signal)

    async def run(self) -> None:
        """
        Main async event loop.

        Runs scan cycles, monitoring, and periodic tasks until shutdown.
        """
        self._running = True
        self._setup_signal_handlers()

        # Startup
        success = await self.startup()
        if not success:
            logger.error("Startup failed, exiting")
            return

        scan_interval = self.config.get("scanner", {}).get("scan_interval_seconds", 300)
        monitor_interval = 60  # seconds
        ob_monitor_interval = self.config.get("orderbook_monitor", {}).get(
            "orderbook_monitor_interval_seconds", 20
        )
        hourly_interval = 3600
        daily_interval = 86400

        last_scan = 0.0
        last_monitor = 0.0
        last_ob_monitor = 0.0

        # Dashboard state
        daily_stats: Dict[str, Any] = {}

        logger.info(
            f"Bot running. Scan interval={scan_interval}s, Monitor interval={monitor_interval}s"
        )

        # Start dashboard live display (non-blocking)
        dashboard_portfolio = {
            "balance": self.portfolio_balance,
            "deployed": 0.0,
            "available": self.portfolio_balance,
            "unrealized_pnl": 0.0,
            "open_positions": len(self._open_positions),
            "all_time_pnl": 0.0,
        }

        try:
            while self._running:
                # Check kill switch
                if is_halt_requested():
                    logger.warning("HALT file detected - initiating emergency shutdown")
                    await self._graceful_shutdown("HALT file detected")
                    break

                now = time.time()

                # Phase 1: Exit engine (runs every monitor cycle — before entries)
                if now - last_monitor >= monitor_interval:
                    last_monitor = now
                    exits = await self.exit_cycle()
                    if exits:
                        logger.info(f"Exit cycle: {exits} position(s) closed")
                    # Legacy monitor cycle for supplemental alert tracking
                    await self.monitor_cycle()

                # Phase 1.5: Orderbook monitor (live mode only, every 20s)
                if (
                    not self.paper_mode
                    and feature_enabled(self.config, "orderbook_monitor")
                    and now - last_ob_monitor >= ob_monitor_interval
                ):
                    last_ob_monitor = now
                    ob_signals = await self.orderbook_monitor.run_cycle(
                        self._open_positions, self.clob_client
                    )
                    for signal in ob_signals:
                        pos = next(
                            (p for p in self._open_positions if p.get("id") == signal["position_id"]),
                            None,
                        )
                        if pos:
                            success = await self.executor.close_position(
                                position=pos,
                                close_pct=1.0,
                                urgency="immediate",
                                reason="orderbook_imbalance_exit",
                                clob_client=self.clob_client,
                                paper_mode=False,
                                current_price=pos.get("_current_price"),
                                notifier=self.notifier,
                                db=self.db,
                            )
                            if success:
                                self.db.update_position(pos["id"], {"orderbook_exit_flag": 1})
                                self.orderbook_monitor.cleanup_position(pos["id"])
                                logger.warning(f"Orderbook imbalance exit: position {pos['id']}")

                # Phase 2: Scan for new entries
                if now - last_scan >= scan_interval:
                    last_scan = now
                    trades = await self.scan_cycle()
                    if trades > 0:
                        logger.info(f"Scan cycle complete: {trades} trades executed")

                # Hourly tasks
                if now - self._last_hourly_task >= hourly_interval:
                    await self.hourly_tasks()

                # Daily tasks (runs around midnight UTC)
                current_hour = datetime.now(timezone.utc).hour
                if now - self._last_daily_task >= daily_interval and current_hour == 0:
                    await self.daily_tasks()

                # Lifetime performance summary — fires once per UTC day at the
                # configured hour. Dedupe via _last_perf_summary_date so restarts
                # mid-window don't re-send.
                today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if (
                    current_hour == self._perf_summary_hour_utc
                    and self._last_perf_summary_date != today_utc
                ):
                    await self.performance_summary_task()

                # Update dashboard state
                deployed = sum(p.get("cost_basis", 0) for p in self._open_positions)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                daily_stats = self.db.get_daily_stats(today)

                dashboard_portfolio = {
                    "balance": self.portfolio_balance,
                    "deployed": deployed,
                    "available": self.portfolio_balance - deployed,
                    "unrealized_pnl": sum(
                        (p.get("_current_price", p.get("entry_price", 0)) - p.get("entry_price", 0))
                        * p.get("shares", 0)
                        for p in self._open_positions
                    ),
                    "open_positions": len(self._open_positions),
                    "all_time_pnl": self.db.get_all_time_stats(paper_trade=self.paper_mode).get("total_pnl", 0.0),
                }

                # Print summary every 60 seconds
                if int(now) % 60 == 0:
                    self.dashboard.print_summary(
                        dashboard_portfolio, self._open_positions, daily_stats
                    )

                await asyncio.sleep(1)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received")
            await self._graceful_shutdown("KeyboardInterrupt")
        except Exception as e:
            logger.error(f"Fatal error in main loop: {e}", exc_info=True)
            await self._graceful_shutdown(f"Fatal error: {e}")
        finally:
            logger.info("BondBot run() completed")


def parse_args() -> argparse.Namespace:
    """
    Parse command line arguments.

    Returns:
        Parsed argument namespace
    """
    parser = argparse.ArgumentParser(
        description="Polymarket Bond Strategy Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                     # Run in live mode
  python main.py --paper             # Run in paper trading mode
  python main.py --config my.yaml    # Use custom config file
  python main.py --paper --log-level DEBUG  # Debug paper mode
        """,
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        default=False,
        help="Run in paper trading mode (no real orders placed)",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        default=False,
        help="Run backtesting mode (see scripts/backtest.py)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override logging level from config",
    )
    return parser.parse_args()


async def main() -> None:
    """Async main entry point."""
    args = parse_args()

    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Override log level if specified
    if args.log_level:
        config.setdefault("logging", {})["level"] = args.log_level

    # Setup logging
    setup_logging(config)

    logger.info("=" * 60)
    logger.info("Polymarket Bond Strategy Bot v1.0.0")
    logger.info(f"Config: {args.config}")
    logger.info(f"Mode: {'PAPER' if args.paper else 'LIVE'}")
    logger.info("=" * 60)

    if args.backtest:
        logger.info("Backtesting mode requested. Run scripts/backtest.py instead.")
        print("Use: python scripts/backtest.py")
        return

    # Create and run the bot
    bot = BondBot(config=config, paper_mode=args.paper)

    try:
        await bot.run()
    except Exception as e:
        logger.critical(f"Unhandled exception: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
