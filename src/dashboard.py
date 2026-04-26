"""
Terminal dashboard using Rich for live display of bot status and positions.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align

logger = logging.getLogger(__name__)

console = Console()


class Dashboard:
    """Rich terminal dashboard for the bond bot."""

    def __init__(self, config: Optional[dict] = None):
        """
        Initialize dashboard.

        Args:
            config: Optional configuration dictionary
        """
        self.config = config or {}
        self._live: Optional[Live] = None
        self._layout = Layout()
        self._log_entries: List[str] = []
        self._max_log_entries = 15
        self._alerts: List[Dict] = []

    def _make_layout(self) -> Layout:
        """Create the dashboard layout structure."""
        layout = Layout(name="root")
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="right", ratio=2),
        )
        layout["left"].split_column(
            Layout(name="portfolio", size=10),
            Layout(name="positions"),
        )
        layout["right"].split_column(
            Layout(name="watchlist"),
            Layout(name="logs"),
        )
        return layout

    def _make_header(self) -> Panel:
        """Create the header panel."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        title = Text("🤖 Polymarket Bond Strategy Bot", style="bold white")
        subtitle = Text(f" ⏰ {now}", style="dim cyan")
        return Panel(
            Align.center(title + subtitle),
            style="bold blue",
            box=box.DOUBLE,
        )

    def _make_portfolio_panel(
        self,
        portfolio: Dict[str, Any],
        daily_stats: Dict[str, Any],
    ) -> Panel:
        """Create the portfolio summary panel."""
        balance = portfolio.get("balance", 0.0)
        deployed = portfolio.get("deployed", 0.0)
        available = portfolio.get("available", balance - deployed)
        unrealized_pnl = portfolio.get("unrealized_pnl", 0.0)
        deployed_pct = (deployed / balance * 100) if balance > 0 else 0.0

        realized_pnl = daily_stats.get("realized_pnl", 0.0)
        total_pnl_today = realized_pnl + unrealized_pnl
        wins = daily_stats.get("win_count", 0)
        losses = daily_stats.get("loss_count", 0)
        total_trades = wins + losses
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

        all_time_pnl = portfolio.get("all_time_pnl", 0.0)

        table = Table(box=None, padding=(0, 1), show_header=False)
        table.add_column("Metric", style="dim")
        table.add_column("Value", style="bold")
        table.add_column("Metric2", style="dim")
        table.add_column("Value2", style="bold")

        def pnl_style(val: float) -> str:
            return "bold green" if val >= 0 else "bold red"

        table.add_row(
            "Portfolio Balance:", f"${balance:,.2f}",
            "Today's P&L:", f"[{pnl_style(total_pnl_today)}]${total_pnl_today:+,.2f}[/]",
        )
        table.add_row(
            "Deployed:", f"${deployed:,.2f} ({deployed_pct:.1f}%)",
            "Realized P&L:", f"[{pnl_style(realized_pnl)}]${realized_pnl:+,.2f}[/]",
        )
        table.add_row(
            "Available:", f"${available:,.2f}",
            "Unrealized P&L:", f"[{pnl_style(unrealized_pnl)}]${unrealized_pnl:+,.2f}[/]",
        )
        table.add_row(
            "Open Positions:", str(portfolio.get("open_positions", 0)),
            "All-Time P&L:", f"[{pnl_style(all_time_pnl)}]${all_time_pnl:+,.2f}[/]",
        )
        table.add_row(
            "Win Rate:", f"{win_rate:.1f}% ({wins}W / {losses}L)",
            "Total Trades:", str(total_trades),
        )

        # V4 Phase 2.3: cumulative holding rewards (right column only to
        # preserve the two-col layout).
        holding_rewards = portfolio.get("holding_rewards_earned")
        if holding_rewards is not None:
            table.add_row(
                "", "",
                "Holding Rewards:", f"${holding_rewards:,.2f}",
            )

        return Panel(table, title="[bold cyan]Portfolio Summary[/bold cyan]", box=box.ROUNDED)

    def _make_positions_table(self, positions: List[Dict[str, Any]]) -> Panel:
        """Create the open positions table."""
        table = Table(
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold magenta",
            row_styles=["", "dim"],
        )
        table.add_column("Market", style="cyan", max_width=35, no_wrap=True)
        table.add_column("Entry $", justify="right", style="white")
        table.add_column("Current $", justify="right")
        table.add_column("Shares", justify="right", style="dim")
        table.add_column("Cost", justify="right", style="dim")
        table.add_column("P&L %", justify="right")
        table.add_column("Resolution", justify="center", style="dim")
        table.add_column("Alert", justify="center")

        for pos in positions:
            if pos.get("status") != "open":
                continue

            question = (pos.get("market_question") or "Unknown")[:35]
            entry_price = pos.get("entry_price", 0)
            current_price = pos.get("_current_price") or entry_price
            shares = pos.get("shares", 0)
            cost = pos.get("cost_basis", 0)
            end_date = pos.get("expected_resolution", "")

            # Calculate P&L
            unrealized = (current_price - entry_price) * shares
            pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

            # Resolution date
            if end_date:
                try:
                    from .utils import parse_iso_datetime, get_days_to_resolution
                    days = get_days_to_resolution(end_date)
                    if days < 1:
                        res_str = f"{days * 24:.0f}h"
                    else:
                        res_str = f"{days:.1f}d"
                except Exception:
                    res_str = end_date[:10]
            else:
                res_str = "Unknown"

            # Alert status
            alert_level = pos.get("_alert_level")
            if alert_level == "red":
                alert_str = "[bold red]🔴 RED[/bold red]"
            elif alert_level == "orange":
                alert_str = "[bold yellow]🟠 ORANGE[/bold yellow]"
            elif alert_level == "yellow":
                alert_str = "[yellow]🟡 YELLOW[/yellow]"
            else:
                alert_str = "[green]✓[/green]"

            # P&L color
            pnl_color = "green" if pnl_pct >= 0 else "red"
            pnl_str = f"[{pnl_color}]{pnl_pct:+.2f}%[/{pnl_color}]"

            # Current price color
            price_color = "green" if current_price > entry_price else "red" if current_price < entry_price else "white"
            current_str = f"[{price_color}]${current_price:.4f}[/{price_color}]"

            table.add_row(
                question,
                f"${entry_price:.4f}",
                current_str,
                f"{shares:.1f}",
                f"${cost:.2f}",
                pnl_str,
                res_str,
                alert_str,
            )

        if not any(p.get("status") == "open" for p in positions):
            table.add_row(
                "[dim]No open positions[/dim]", "", "", "", "", "", "", ""
            )

        return Panel(
            table,
            title="[bold cyan]Open Positions[/bold cyan]",
            box=box.ROUNDED,
        )

    def _make_watchlist_panel(self, candidates: List[Dict[str, Any]]) -> Panel:
        """Create the top candidates watchlist panel."""
        table = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold blue",
        )
        table.add_column("#", style="dim", width=3)
        table.add_column("Market", max_width=30, no_wrap=True)
        table.add_column("Price", justify="right")
        table.add_column("Yield", justify="right", style="green")
        table.add_column("Days", justify="right", style="dim")
        table.add_column("Score", justify="right")

        top = candidates[:10]
        for i, market in enumerate(top, 1):
            question = (market.get("question") or market.get("title") or "Unknown")[:30]
            price = market.get("_yes_price", 0)
            days = market.get("_days_to_resolution", 0)
            score = market.get("_bond_score", 0)
            yield_pct = ((1.0 - price) / price * 100) if price > 0 else 0

            table.add_row(
                str(i),
                question,
                f"${price:.4f}",
                f"{yield_pct:.2f}%",
                f"{days:.1f}",
                f"{score:.5f}",
            )

        if not top:
            table.add_row("", "[dim]No candidates found[/dim]", "", "", "", "")

        return Panel(
            table,
            title="[bold blue]Top Bond Candidates[/bold blue]",
            box=box.ROUNDED,
        )

    def _make_pipeline_health_panel(self, health_summary: Dict[str, Any]) -> Panel:
        """V4 1.4: Render pipeline funnel + top rejection reasons.

        `health_summary` mirrors PipelineHealth.get_24h_summary().
        """
        fetched = int(health_summary.get("fetched") or 0)
        prefilter = int(health_summary.get("prefilter") or 0)
        detector = int(health_summary.get("detector") or 0)
        risk = int(health_summary.get("risk") or 0)
        entries = int(health_summary.get("entries") or 0)
        accept_rate = float(health_summary.get("acceptance_rate") or 0.0) * 100
        dry = float(health_summary.get("dry_period_hours") or 0.0)

        def pct(n: int) -> str:
            return f"({(n / fetched * 100):.2f}%)" if fetched else ""

        body = Text()
        body.append(
            f"Scans: {int(health_summary.get('scans') or 0)}    "
            f"Acceptance rate: {accept_rate:.2f}%\n"
        )
        body.append(f"Fetched: {fetched:,}   Prefilter: {prefilter:,} {pct(prefilter)}\n")
        body.append(f"Detector: {detector:,} {pct(detector)}  RiskEng: {risk:,} {pct(risk)}\n")
        body.append(f"Entries: {entries}    Dry period: {dry:.1f}h\n\n")
        body.append("Top rejection reasons:\n", style="bold")
        top = health_summary.get("top_rejections") or []
        if top:
            for i, (reason, count) in enumerate(top, 1):
                body.append(f" {i}. {reason:<32} {count:,}\n", style="dim")
        else:
            body.append(" (none recorded)\n", style="dim")

        return Panel(
            body,
            title="[bold cyan]Pipeline Health (24h)[/bold cyan]",
            box=box.ROUNDED,
        )

    def _make_fee_attribution_panel(self, fee_stats: Dict[str, Any]) -> Panel:
        """V4 1.4: Lifetime fee attribution — legacy flat vs dynamic-model.

        `fee_stats` keys: position_count, gross_revenue, legacy_fees,
        actual_fees, savings.
        """
        count = int(fee_stats.get("position_count") or 0)
        gross = float(fee_stats.get("gross_revenue") or 0.0)
        legacy = float(fee_stats.get("legacy_fees") or 0.0)
        actual = float(fee_stats.get("actual_fees") or 0.0)
        savings = legacy - actual

        def pct_of_gross(v: float) -> str:
            return f"({(v / gross * 100):.1f}%)" if gross > 0 else ""

        body = Text()
        body.append(f"Position count: {count}\n")
        body.append(f"Gross revenue: ${gross:,.2f}\n")
        body.append(f"Estimated fees (V3 flat): ${legacy:,.2f}  {pct_of_gross(legacy)}\n")
        body.append(f"Actual fees (V4 dynamic): ${actual:,.2f}  {pct_of_gross(actual)}\n")
        body.append(f"Savings from maker execution: ${savings:,.2f}\n", style="bold green")

        return Panel(
            body,
            title="[bold magenta]Fee Attribution (lifetime)[/bold magenta]",
            box=box.ROUNDED,
        )

    def _make_alerts_panel(self) -> Panel:
        """Create the active alerts panel."""
        if not self._alerts:
            content = Text("No active alerts", style="dim green")
        else:
            content = Text()
            for alert in self._alerts[-5:]:  # Show last 5 alerts
                level = alert.get("level", "info")
                msg = alert.get("message", "")
                ts = alert.get("time", "")
                color = {"yellow": "yellow", "orange": "dark_orange", "red": "red"}.get(
                    level, "white"
                )
                content.append(f"[{ts}] ", style="dim")
                content.append(f"{msg}\n", style=color)

        return Panel(content, title="[bold red]Active Alerts[/bold red]", box=box.ROUNDED)

    def _make_logs_panel(self) -> Panel:
        """Create the recent log entries panel."""
        log_text = Text()
        for entry in self._logs_to_display():
            log_text.append(entry + "\n", style="dim")

        if not log_text.plain.strip():
            log_text.append("No recent log entries", style="dim")

        return Panel(log_text, title="[bold]Recent Logs[/bold]", box=box.ROUNDED)

    def _logs_to_display(self) -> List[str]:
        """Get recent log entries formatted for display."""
        return self._log_entries[-self._max_log_entries:]

    def _make_footer(self) -> Panel:
        """Create the footer status bar."""
        mode_text = Text()
        mode_text.append("Controls: ", style="dim")
        mode_text.append("Ctrl+C", style="bold yellow")
        mode_text.append(" to stop | Create ", style="dim")
        mode_text.append("HALT", style="bold red")
        mode_text.append(" file for kill switch", style="dim")
        return Panel(Align.center(mode_text), style="dim", box=box.SIMPLE)

    def add_log_entry(self, message: str) -> None:
        """Add a message to the log display."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._log_entries.append(f"[{now}] {message}")
        if len(self._log_entries) > 100:
            self._log_entries = self._log_entries[-100:]

    def add_alert(self, level: str, message: str) -> None:
        """Add an alert to the alerts display."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._alerts.append({
            "level": level,
            "message": message,
            "time": now,
        })
        if len(self._alerts) > 50:
            self._alerts = self._alerts[-50:]

    def render(
        self,
        portfolio: Dict[str, Any],
        positions: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
        daily_stats: Dict[str, Any],
    ) -> Layout:
        """
        Render the full dashboard layout.

        Args:
            portfolio: Portfolio summary dictionary
            positions: List of all positions
            candidates: List of top bond candidates
            daily_stats: Today's performance statistics

        Returns:
            Rendered layout object
        """
        layout = self._make_layout()

        layout["header"].update(self._make_header())
        layout["portfolio"].update(self._make_portfolio_panel(portfolio, daily_stats))
        layout["positions"].update(self._make_positions_table(positions))
        layout["watchlist"].update(self._make_watchlist_panel(candidates))
        layout["logs"].update(self._make_logs_panel())
        layout["footer"].update(self._make_footer())

        return layout

    def start_live(
        self,
        portfolio: Dict[str, Any],
        positions: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
        daily_stats: Dict[str, Any],
        refresh_per_second: float = 1.0,
    ) -> Live:
        """
        Start a live-updating terminal display.

        Args:
            portfolio: Initial portfolio data
            positions: Initial positions list
            candidates: Initial candidates list
            daily_stats: Initial daily stats
            refresh_per_second: How often to refresh

        Returns:
            Rich Live context manager
        """
        layout = self.render(portfolio, positions, candidates, daily_stats)
        self._live = Live(
            layout,
            console=console,
            refresh_per_second=refresh_per_second,
            screen=True,
        )
        return self._live

    def update_live(
        self,
        portfolio: Dict[str, Any],
        positions: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
        daily_stats: Dict[str, Any],
    ) -> None:
        """
        Update the live display with new data.

        Args:
            portfolio: Updated portfolio data
            positions: Updated positions list
            candidates: Updated candidates list
            daily_stats: Updated daily stats
        """
        if self._live and self._live.is_started:
            layout = self.render(portfolio, positions, candidates, daily_stats)
            self._live.update(layout)

    def generate_daily_report(self, db: Any, paper_trade: Optional[bool] = None) -> Dict[str, Any]:
        """
        Generate a daily performance report from database stats.

        Args:
            db: Database instance
            paper_trade: Filter by trade mode

        Returns:
            Report dictionary with all metrics
        """
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_stats = db.get_daily_stats(today)
        all_time = db.get_all_time_stats(paper_trade=paper_trade)
        open_positions = db.get_open_positions(paper_trade=paper_trade)

        wins = daily_stats.get("win_count", 0)
        losses = daily_stats.get("loss_count", 0)
        total = wins + losses
        win_rate = (wins / total * 100) if total > 0 else 0

        all_wins = all_time.get("wins", 0)
        all_losses = all_time.get("losses", 0)
        all_total = all_wins + all_losses
        all_win_rate = (all_wins / all_total * 100) if all_total > 0 else 0

        return {
            "date": today,
            "daily": {
                "trades_opened": daily_stats.get("trades_opened", 0),
                "trades_closed": total,
                "realized_pnl": daily_stats.get("realized_pnl", 0.0),
                "fees_paid": daily_stats.get("fees_paid", 0.0),
                "win_rate": win_rate,
                "wins": wins,
                "losses": losses,
            },
            "all_time": {
                "total_trades": all_time.get("closed_trades", 0),
                "win_rate": all_win_rate,
                "total_pnl": all_time.get("total_pnl", 0.0),
                "avg_pnl": all_time.get("avg_pnl", 0.0),
                "total_fees": all_time.get("total_fees", 0.0),
            },
            "current": {
                "open_positions": len(open_positions),
                "deployed": sum(p.get("cost_basis", 0) for p in open_positions),
            },
        }

    def generate_hourly_snapshot(
        self,
        positions: List[Dict[str, Any]],
        balance: float,
    ) -> Dict[str, Any]:
        """
        Generate an hourly portfolio snapshot.

        Args:
            positions: List of open positions
            balance: Current portfolio balance

        Returns:
            Snapshot dictionary
        """
        open_positions = [p for p in positions if p.get("status") == "open"]
        deployed = sum(p.get("cost_basis", 0) for p in open_positions)
        available = balance - deployed

        unrealized_pnl = 0.0
        for pos in open_positions:
            current = pos.get("_current_price") or pos.get("entry_price", 0)
            entry = pos.get("entry_price", 0)
            shares = pos.get("shares", 0)
            unrealized_pnl += (current - entry) * shares

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "balance": balance,
            "deployed": deployed,
            "available": available,
            "unrealized_pnl": unrealized_pnl,
            "open_positions": len(open_positions),
            "deployed_pct": (deployed / balance * 100) if balance > 0 else 0,
        }

    def print_summary(
        self,
        portfolio: Dict[str, Any],
        positions: List[Dict[str, Any]],
        daily_stats: Dict[str, Any],
    ) -> None:
        """
        Print a simple summary to console without Live mode.

        Args:
            portfolio: Portfolio data
            positions: Position list
            daily_stats: Daily stats
        """
        console.print(self._make_portfolio_panel(portfolio, daily_stats))
        console.print(self._make_positions_table(positions))
