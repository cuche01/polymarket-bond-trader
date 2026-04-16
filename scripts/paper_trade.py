#!/usr/bin/env python3
"""
Paper trading wrapper for the Polymarket Bond Strategy Bot.

Starts the bot in paper (simulation) mode, runs scan cycles,
and generates detailed summaries after each cycle without placing
any real orders.

Usage:
    python scripts/paper_trade.py [--cycles N] [--config path]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src.database import Database
from src.dashboard import Dashboard
from src.detector import PseudoCertaintyDetector
from src.executor import OrderExecutor
from src.monitor import PositionMonitor
from src.notifications import Notifier
from src.risk_manager import RiskManager
from src.scanner import MarketScanner
from src.utils import (
    format_currency,
    load_config,
    setup_logging,
)

logger = logging.getLogger("paper_trade")


class PaperTradingRunner:
    """
    Runs the bond bot in paper mode and generates cycle summaries.
    """

    def __init__(self, config: dict, initial_balance: float = 10000.0):
        """
        Initialize paper trading runner.

        Args:
            config: Configuration dictionary
            initial_balance: Starting simulated portfolio balance
        """
        self.config = config
        self.balance = initial_balance
        self.cycle_count = 0
        self.cycle_summaries = []

        # Initialize modules in paper mode
        log_cfg = config.get("logging", {})
        db_path = "data/paper_trade.db"  # Separate DB for paper trades

        self.db = Database(db_path)
        self.scanner = MarketScanner(config)
        self.detector = PseudoCertaintyDetector(config)
        self.risk_manager = RiskManager(config)
        self.executor = OrderExecutor(config)
        self.monitor = PositionMonitor(config)
        self.dashboard = Dashboard(config)

    async def run_cycle(self) -> dict:
        """
        Run one paper trading scan cycle.

        Returns:
            Cycle summary dictionary
        """
        self.cycle_count += 1
        cycle_start = datetime.now(timezone.utc)
        trades_this_cycle = 0
        skipped = 0

        logger.info(f"\n{'=' * 60}")
        logger.info(f"Paper Trade Cycle #{self.cycle_count}")
        logger.info(f"Time: {cycle_start.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info(f"Balance: {format_currency(self.balance)}")
        logger.info("=" * 60)

        # Get open positions
        open_positions = self.db.get_open_positions(paper_trade=True)
        deployed = sum(p.get("cost_basis", 0) for p in open_positions)
        available = self.balance - deployed

        logger.info(f"Open positions: {len(open_positions)} | Deployed: {format_currency(deployed)} | Available: {format_currency(available)}")

        # Scan for candidates
        candidates = await self.scanner.run_scan_cycle()
        logger.info(f"Found {len(candidates)} bond candidates")

        # Process candidates
        entries_attempted = 0
        for market in candidates[:20]:  # Limit to top 20 per cycle
            market_id = market.get("id") or market.get("conditionId", "")

            # Skip existing positions
            existing = self.db.get_position_by_market(market_id, "open")
            if existing:
                continue

            # Validate
            position_estimate = self.risk_manager.calculate_position_size(
                market, self.balance, open_positions, market.get("_liquidity", 0)
            )

            is_valid, reason = await self.detector.is_valid_opportunity(
                market, None, position_estimate  # No CLOB in paper mode
            )

            if not is_valid:
                logger.debug(f"Rejected: {reason[:60]}")
                skipped += 1
                continue

            # Size position
            position_size = self.risk_manager.calculate_position_size(
                market, self.balance, open_positions, market.get("_liquidity", 0)
            )

            # Risk check
            approved, approval_reason = self.risk_manager.validate_entry(
                market=market,
                position_size=position_size,
                portfolio_balance=self.balance,
                existing_positions=open_positions,
                daily_pnl=0.0,
            )

            if not approved:
                logger.debug(f"Risk check failed: {approval_reason}")
                skipped += 1
                continue

            entries_attempted += 1

            # Simulate entry
            position = await self.executor.execute_entry(
                market=market,
                position_size=position_size,
                clob_client=None,
                paper_mode=True,
            )

            if position:
                pos_id = self.db.save_position(position)
                position["id"] = pos_id
                open_positions.append(position)
                trades_this_cycle += 1
                deployed += position.get("cost_basis", 0)

                logger.info(
                    f"[PAPER] ENTRY: {market.get('question', '')[:50][:50]} | "
                    f"Price=${position['entry_price']:.4f} | "
                    f"Size=${position['cost_basis']:.2f} | "
                    f"Score={market.get('_bond_score', 0):.6f}"
                )

        # Monitor existing positions
        if open_positions:
            logger.info(f"\nMonitoring {len(open_positions)} open positions...")
            for pos in open_positions:
                # Simulate position health check
                entry_price = pos.get("entry_price", 0)
                # In paper mode, we simulate stable prices (no price drops for demo)
                current_price = entry_price  # Would normally fetch from API
                pos["_current_price"] = current_price

                alert = self.monitor.check_position_health(pos, current_price)
                if alert:
                    logger.warning(
                        f"ALERT ({alert.upper()}): {pos.get('market_question', '')[:40]} "
                        f"@ ${current_price:.4f}"
                    )

        # Generate cycle summary
        cycle_duration = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        summary = self._generate_cycle_summary(
            cycle_num=self.cycle_count,
            candidates_found=len(candidates),
            trades_entered=trades_this_cycle,
            entries_attempted=entries_attempted,
            skipped=skipped,
            open_positions=open_positions,
            cycle_duration_secs=cycle_duration,
        )

        self.cycle_summaries.append(summary)
        self._print_cycle_summary(summary)
        return summary

    def _generate_cycle_summary(
        self,
        cycle_num: int,
        candidates_found: int,
        trades_entered: int,
        entries_attempted: int,
        skipped: int,
        open_positions: list,
        cycle_duration_secs: float,
    ) -> dict:
        """Generate a summary dict for this cycle."""
        deployed = sum(p.get("cost_basis", 0) for p in open_positions)
        deployed_pct = (deployed / self.balance * 100) if self.balance > 0 else 0

        # Calculate unrealized P&L
        unrealized_pnl = sum(
            (p.get("_current_price", p.get("entry_price", 0)) - p.get("entry_price", 0))
            * p.get("shares", 0)
            for p in open_positions
        )

        return {
            "cycle": cycle_num,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_secs": round(cycle_duration_secs, 2),
            "candidates_found": candidates_found,
            "entries_attempted": entries_attempted,
            "trades_entered": trades_entered,
            "skipped": skipped,
            "portfolio": {
                "balance": self.balance,
                "deployed": deployed,
                "available": self.balance - deployed,
                "deployed_pct": round(deployed_pct, 1),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "open_positions": len(open_positions),
            },
        }

    def _print_cycle_summary(self, summary: dict) -> None:
        """Print a formatted cycle summary."""
        portfolio = summary["portfolio"]
        print(f"\n{'─' * 50}")
        print(f"  Cycle #{summary['cycle']} Summary ({summary['duration_secs']:.1f}s)")
        print(f"{'─' * 50}")
        print(f"  Candidates found: {summary['candidates_found']}")
        print(f"  Entries attempted: {summary['entries_attempted']}")
        print(f"  Trades entered: {summary['trades_entered']}")
        print(f"  Skipped/rejected: {summary['skipped']}")
        print(f"{'─' * 50}")
        print(f"  Portfolio:")
        print(f"    Balance:     {format_currency(portfolio['balance'])}")
        print(f"    Deployed:    {format_currency(portfolio['deployed'])} ({portfolio['deployed_pct']:.1f}%)")
        print(f"    Available:   {format_currency(portfolio['available'])}")
        print(f"    Unrealized:  {'+' if portfolio['unrealized_pnl'] >= 0 else ''}"
              f"{format_currency(portfolio['unrealized_pnl'])}")
        print(f"    Open positions: {portfolio['open_positions']}")
        print(f"{'─' * 50}\n")

    def print_final_report(self) -> None:
        """Print final report after all cycles complete."""
        if not self.cycle_summaries:
            print("No cycles completed.")
            return

        total_trades = sum(c["trades_entered"] for c in self.cycle_summaries)
        total_candidates = sum(c["candidates_found"] for c in self.cycle_summaries)
        last_summary = self.cycle_summaries[-1]

        print(f"\n{'=' * 60}")
        print(f"  Paper Trading Final Report")
        print(f"{'=' * 60}")
        print(f"  Cycles completed:      {len(self.cycle_summaries)}")
        print(f"  Total candidates seen: {total_candidates}")
        print(f"  Total trades entered:  {total_trades}")
        print(f"  Final portfolio state:")
        portfolio = last_summary["portfolio"]
        print(f"    Balance:   {format_currency(portfolio['balance'])}")
        print(f"    Deployed:  {format_currency(portfolio['deployed'])}")
        print(f"    Available: {format_currency(portfolio['available'])}")
        print(f"    Open pos:  {portfolio['open_positions']}")
        print(f"{'=' * 60}")

        # Get DB stats
        try:
            all_time = self.db.get_all_time_stats(paper_trade=True)
            closed = all_time.get("closed_trades", 0)
            if closed > 0:
                total_pnl = all_time.get("total_pnl", 0)
                wins = all_time.get("wins", 0)
                win_rate = (wins / closed * 100) if closed > 0 else 0
                print(f"\n  All-time paper trade stats:")
                print(f"    Closed trades: {closed}")
                print(f"    Win rate:      {win_rate:.1f}%")
                print(f"    Total P&L:     {format_currency(total_pnl)}")
        except Exception as e:
            logger.debug(f"Could not fetch DB stats: {e}")

        print()


async def run_paper_trading(
    config: dict,
    num_cycles: int = 3,
    initial_balance: float = 10000.0,
    cycle_delay_seconds: float = 5.0,
) -> None:
    """
    Run paper trading for specified number of cycles.

    Args:
        config: Configuration dictionary
        num_cycles: Number of scan cycles to run
        initial_balance: Starting simulated balance
        cycle_delay_seconds: Delay between cycles
    """
    runner = PaperTradingRunner(config, initial_balance)

    logger.info("Starting paper trading session...")
    logger.info(f"Initial balance: {format_currency(initial_balance)}")
    logger.info(f"Cycles to run: {num_cycles}")

    try:
        for cycle in range(num_cycles):
            if cycle > 0:
                logger.info(f"Waiting {cycle_delay_seconds}s before next cycle...")
                await asyncio.sleep(cycle_delay_seconds)

            await runner.run_cycle()

    except KeyboardInterrupt:
        logger.info("Paper trading interrupted by user")
    except Exception as e:
        logger.error(f"Paper trading error: {e}", exc_info=True)
    finally:
        runner.print_final_report()
        await runner.scanner.close()
        await runner.detector.close()
        await runner.monitor.close()


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run Polymarket Bond Bot in paper trading mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/paper_trade.py                     # 3 cycles with $10,000 balance
  python scripts/paper_trade.py --cycles 10         # 10 cycles
  python scripts/paper_trade.py --balance 50000     # $50,000 simulated balance
  python scripts/paper_trade.py --cycles 5 --delay 60  # 5 cycles, 60s delay
        """,
    )
    parser.add_argument(
        "--cycles", type=int, default=3,
        help="Number of scan cycles to run (default: 3)"
    )
    parser.add_argument(
        "--balance", type=float, default=10000.0,
        help="Initial simulated balance in USD (default: 10000)"
    )
    parser.add_argument(
        "--delay", type=float, default=5.0,
        help="Seconds between cycles (default: 5)"
    )
    parser.add_argument(
        "--config", type=str, default="config.yaml",
        help="Config file path (default: config.yaml)"
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)"
    )
    return parser.parse_args()


async def main() -> None:
    """Main entry point for paper trading."""
    args = parse_args()

    # Setup basic logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        logger.warning(f"Config {args.config} not found, using defaults")
        config = {
            "scanner": {
                "scan_interval_seconds": 1,  # Immediate scan for testing
                "min_entry_price": 0.95,
                "max_entry_price": 0.99,
                "max_days_to_resolution": 14,
                "min_liquidity": 10000,
                "min_volume_24h": 5000,
                "max_price_volatility_1d": 0.03,
                "excluded_categories": ["15-min Crypto", "1-hr Crypto", "Live Sports"],
            },
            "risk": {
                "max_single_market_pct": 0.10,
                "max_correlated_pct": 0.15,
                "max_deployed_pct": 0.70,
                "max_absolute_position": 5000,
                "max_daily_loss_pct": 0.02,
                "consecutive_loss_halt": 3,
                "min_net_yield": 0.01,
            },
            "exits": {
                "auto_exit_enabled": True,
                "yellow_alert_threshold": 0.92,
                "orange_alert_threshold": 0.88,
                "red_alert_exit_threshold": 0.80,
                "order_timeout_seconds": 300,
            },
            "orderbook": {
                "min_bid_depth_multiplier": 5,
                "max_spread": 0.03,
            },
            "notifications": {"enabled": False},
            "logging": {"level": args.log_level, "db_path": "data/paper_trade.db"},
        }

    # Override scan interval to be faster for testing
    config.setdefault("scanner", {})["scan_interval_seconds"] = 1

    print(f"\n{'=' * 60}")
    print("  Polymarket Bond Strategy Bot - Paper Trading Mode")
    print(f"{'=' * 60}")
    print(f"  Balance: {format_currency(args.balance)}")
    print(f"  Cycles:  {args.cycles}")
    print(f"  Delay:   {args.delay}s between cycles")
    print(f"{'=' * 60}\n")

    await run_paper_trading(
        config=config,
        num_cycles=args.cycles,
        initial_balance=args.balance,
        cycle_delay_seconds=args.delay,
    )


if __name__ == "__main__":
    asyncio.run(main())
