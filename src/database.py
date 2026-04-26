"""
Database module for storing positions, scan logs, and performance data.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class Database:
    """SQLite database manager for the bond bot."""

    def __init__(self, db_path: str = "data/bond_bot.db"):
        """
        Initialize database connection.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        """Create all tables if they don't exist, then run migrations."""
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    market_question TEXT,
                    token_id TEXT,
                    entry_price REAL NOT NULL,
                    shares REAL NOT NULL,
                    cost_basis REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    expected_resolution TEXT,
                    status TEXT DEFAULT 'open',
                    exit_price REAL,
                    exit_time TEXT,
                    pnl REAL,
                    fees_paid REAL DEFAULT 0.0,
                    bond_score REAL,
                    paper_trade INTEGER DEFAULT 0,
                    order_id TEXT,
                    event_id TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_positions_status
                    ON positions(status);
                CREATE INDEX IF NOT EXISTS idx_positions_market_id
                    ON positions(market_id);

                CREATE TABLE IF NOT EXISTS scan_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_time TEXT NOT NULL,
                    markets_scanned INTEGER DEFAULT 0,
                    candidates_found INTEGER DEFAULT 0,
                    trades_executed INTEGER DEFAULT 0,
                    scan_duration_ms INTEGER DEFAULT 0,
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS performance_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL UNIQUE,
                    trades_opened INTEGER DEFAULT 0,
                    trades_closed INTEGER DEFAULT 0,
                    realized_pnl REAL DEFAULT 0.0,
                    unrealized_pnl REAL DEFAULT 0.0,
                    fees_paid REAL DEFAULT 0.0,
                    win_count INTEGER DEFAULT 0,
                    loss_count INTEGER DEFAULT 0,
                    total_deployed REAL DEFAULT 0.0,
                    portfolio_balance REAL DEFAULT 0.0
                );

                CREATE INDEX IF NOT EXISTS idx_performance_date
                    ON performance_daily(date);

                CREATE TABLE IF NOT EXISTS rejected_markets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    market_question TEXT,
                    rejection_time TEXT NOT NULL,
                    rejection_layer INTEGER,
                    rejection_reason TEXT,
                    yes_price REAL,
                    liquidity REAL,
                    days_to_resolution REAL
                );

                CREATE INDEX IF NOT EXISTS idx_rejected_market_id
                    ON rejected_markets(market_id);

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id INTEGER,
                    alert_time TEXT NOT NULL,
                    alert_level TEXT NOT NULL,
                    current_price REAL,
                    message TEXT,
                    acknowledged INTEGER DEFAULT 0,
                    FOREIGN KEY (position_id) REFERENCES positions(id)
                );

                CREATE TABLE IF NOT EXISTS blacklist_learning (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature_type TEXT NOT NULL,
                    feature_value TEXT NOT NULL,
                    loss_time TEXT NOT NULL,
                    market_id TEXT,
                    pnl REAL
                );

                CREATE INDEX IF NOT EXISTS idx_bl_learning_lookup
                    ON blacklist_learning(feature_type, feature_value, loss_time);

                CREATE TABLE IF NOT EXISTS pipeline_health (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_time TEXT NOT NULL,
                    candidates_fetched INTEGER NOT NULL,
                    candidates_passed_prefilter INTEGER NOT NULL,
                    candidates_passed_detector INTEGER NOT NULL,
                    candidates_passed_risk_engine INTEGER NOT NULL,
                    entries_executed INTEGER NOT NULL,
                    rejection_reasons_json TEXT,
                    mode TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pipeline_health_time
                    ON pipeline_health(scan_time);
            """)
        self._migrate_schema()
        logger.info(f"Database initialized at {self.db_path}")

    def _migrate_schema(self) -> None:
        """
        Apply schema migrations idempotently.
        Uses ALTER TABLE ADD COLUMN — safe to run on existing databases.
        """
        new_columns = [
            ("positions", "category", "TEXT DEFAULT 'unknown'"),
            ("positions", "event_group_id", "TEXT"),
            ("positions", "exit_reason", "TEXT"),
            ("positions", "partial_close_count", "INTEGER DEFAULT 0"),
            ("positions", "original_shares", "REAL"),
            ("positions", "risk_bucket", "TEXT DEFAULT 'other'"),
            ("positions", "high_water_mark", "REAL"),
            ("positions", "expected_exit_price", "REAL"),
            ("positions", "actual_exit_price", "REAL"),
            ("positions", "exit_slippage_pct", "REAL"),
            ("positions", "uma_dispute_detected_at", "TEXT"),
            ("positions", "capital_state", "TEXT DEFAULT 'deployed'"),
            ("positions", "redemption_initiated_at", "TEXT"),
            ("positions", "redemption_completed_at", "TEXT"),
            # P0: Teleportation + orderbook monitor
            ("positions", "teleportation_flag", "INTEGER DEFAULT 0"),
            ("positions", "orderbook_exit_flag", "INTEGER DEFAULT 0"),
            # P1: Binary catalyst
            ("positions", "catalyst_type", "TEXT DEFAULT 'unknown'"),
            ("positions", "binary_catalyst_score", "REAL DEFAULT 0.0"),
            # P2: Re-validation
            ("positions", "last_revalidation_time", "TEXT"),
            # V4 1.2: Dynamic fee model
            ("positions", "fee_schedule_json", "TEXT"),
            ("positions", "estimated_entry_fee", "REAL DEFAULT 0"),
            ("positions", "estimated_exit_fee", "REAL DEFAULT 0"),
            ("positions", "actual_entry_fee", "REAL"),
            ("positions", "actual_exit_fee", "REAL"),
            # V4 Phase 2.3: Holding rewards (4% APY on eligible positions).
            ("positions", "holding_rewards_enabled", "INTEGER DEFAULT 0"),
            ("positions", "holding_rewards_apr", "REAL DEFAULT 0"),
            ("positions", "estimated_holding_rewards", "REAL DEFAULT 0"),
            ("positions", "actual_holding_rewards", "REAL"),
            # V4 Phase 2.5: LP rewards (ranking signal; not auto-claimed).
            ("positions", "lp_rewards_enabled", "INTEGER DEFAULT 0"),
            ("positions", "lp_rewards_daily_rate", "REAL DEFAULT 0"),
            # Rejected markets catalyst columns
            ("rejected_markets", "catalyst_type", "TEXT"),
            ("rejected_markets", "binary_catalyst_score", "REAL"),
            # Scan log volume tracking
            ("scan_log", "volume_24h", "REAL"),
            ("scan_log", "market_id", "TEXT"),
        ]

        with self._get_connection() as conn:
            for table, column, col_def in new_columns:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
                    logger.debug(f"Added column {table}.{column}")
                except Exception:
                    # Column already exists — expected on subsequent startups
                    pass

    def execute_write(self, sql: str, params: tuple = ()) -> None:
        """Execute a write (INSERT/UPDATE/DELETE) query."""
        with self._get_connection() as conn:
            conn.execute(sql, params)

    def execute_read(self, sql: str, params: tuple = ()) -> List[Dict]:
        """Execute a read query and return list of row dicts."""
        with self._get_connection() as conn:
            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def save_position(self, position: Dict[str, Any]) -> int:
        """
        Save a new position to the database.

        Args:
            position: Position dictionary with all fields

        Returns:
            Row ID of the inserted position
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO positions (
                    market_id, market_question, token_id, entry_price, shares,
                    cost_basis, entry_time, expected_resolution, status,
                    fees_paid, bond_score, paper_trade, order_id, event_id,
                    category, event_group_id, risk_bucket, original_shares,
                    capital_state, fee_schedule_json,
                    estimated_entry_fee, estimated_exit_fee
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.get("market_id", ""),
                    position.get("market_question", ""),
                    position.get("token_id", ""),
                    position.get("entry_price", 0.0),
                    position.get("shares", 0.0),
                    position.get("cost_basis", 0.0),
                    position.get("entry_time", datetime.now(timezone.utc).isoformat()),
                    position.get("expected_resolution", ""),
                    position.get("status", "open"),
                    position.get("fees_paid", 0.0),
                    position.get("bond_score", 0.0),
                    1 if position.get("paper_trade", False) else 0,
                    position.get("order_id", ""),
                    position.get("event_id", ""),
                    position.get("category", "unknown"),
                    position.get("event_group_id", position.get("event_id", "")),
                    position.get("risk_bucket", "other"),
                    position.get("shares", 0.0),  # original_shares = initial shares
                    position.get("capital_state", "deployed"),
                    position.get("fee_schedule_json"),
                    position.get("estimated_entry_fee", 0.0),
                    position.get("estimated_exit_fee", 0.0),
                ),
            )
            row_id = cursor.lastrowid
            logger.info(f"Saved position {row_id} for market {position.get('market_id')}")
            return row_id

    def update_position(self, position_id: int, updates: Dict[str, Any]) -> bool:
        """
        Update an existing position.

        Args:
            position_id: Database row ID
            updates: Dictionary of fields to update

        Returns:
            True if updated successfully
        """
        allowed_fields = {
            "status", "exit_price", "exit_time", "pnl", "fees_paid",
            "order_id", "shares", "cost_basis", "exit_reason",
            "partial_close_count", "high_water_mark",
            "expected_exit_price", "actual_exit_price", "exit_slippage_pct",
            "uma_dispute_detected_at", "capital_state",
            "redemption_initiated_at", "redemption_completed_at",
            "teleportation_flag", "orderbook_exit_flag",
            "catalyst_type", "binary_catalyst_score",
            "last_revalidation_time",
            "fee_schedule_json", "estimated_entry_fee", "estimated_exit_fee",
            "actual_entry_fee", "actual_exit_fee",
            # V4 Phase 2.3/2.5: rewards columns.
            "holding_rewards_enabled", "holding_rewards_apr",
            "estimated_holding_rewards", "actual_holding_rewards",
            "lp_rewards_enabled", "lp_rewards_daily_rate",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed_fields}
        if not filtered:
            return False

        set_clause = ", ".join(f"{k} = ?" for k in filtered.keys())
        values = list(filtered.values()) + [position_id]

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"UPDATE positions SET {set_clause} WHERE id = ?",
                values,
            )
            success = cursor.rowcount > 0
            if success:
                logger.debug(f"Updated position {position_id}: {filtered}")
            return success

    def get_open_positions(self, paper_trade: Optional[bool] = None) -> List[Dict]:
        """
        Retrieve all open positions.

        Args:
            paper_trade: If set, filter by paper trade flag

        Returns:
            List of position dictionaries
        """
        with self._get_connection() as conn:
            if paper_trade is None:
                cursor = conn.execute(
                    "SELECT * FROM positions WHERE status = 'open' ORDER BY entry_time DESC"
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM positions WHERE status = 'open' AND paper_trade = ? "
                    "ORDER BY entry_time DESC",
                    (1 if paper_trade else 0,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_resolution_date_exposure(
        self,
        resolution_time_iso: str,
        window_hours: float = 24.0,
        paper_trade: Optional[bool] = None,
    ) -> float:
        """V4 Phase 2.4: sum cost_basis of open positions whose
        `expected_resolution` falls within ±window_hours of the given time.

        Used by the resolution-date cluster check to prevent >25% of deployed
        capital from resolving in any single 24h window (same-catalyst
        correlation risk). Positions with unparseable or missing resolution
        times are skipped (logged upstream).
        """
        from datetime import datetime, timedelta, timezone

        try:
            target = datetime.fromisoformat(resolution_time_iso.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return 0.0
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)

        half = timedelta(hours=window_hours / 2.0)
        lo, hi = target - half, target + half

        positions = self.get_open_positions(paper_trade=paper_trade)
        exposure = 0.0
        for pos in positions:
            iso = pos.get("expected_resolution") or ""
            if not iso:
                continue
            try:
                t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if lo <= t <= hi:
                exposure += float(pos.get("cost_basis") or 0.0)
        return exposure

    def get_all_positions(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict]:
        """
        Retrieve positions with optional status filter.

        Args:
            status: Filter by status ('open', 'closed', 'cancelled')
            limit: Maximum number of results
            offset: Pagination offset

        Returns:
            List of position dictionaries
        """
        with self._get_connection() as conn:
            if status:
                cursor = conn.execute(
                    "SELECT * FROM positions WHERE status = ? "
                    "ORDER BY entry_time DESC LIMIT ? OFFSET ?",
                    (status, limit, offset),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM positions ORDER BY entry_time DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_position_by_id(self, position_id: int) -> Optional[Dict]:
        """
        Get a single position by ID.

        Args:
            position_id: Database row ID

        Returns:
            Position dictionary or None
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM positions WHERE id = ?", (position_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_position_by_market(self, market_id: str, status: str = "open") -> Optional[Dict]:
        """
        Get a position by market ID and status.

        Args:
            market_id: Polymarket market ID
            status: Position status to filter by

        Returns:
            Position dictionary or None
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM positions WHERE market_id = ? AND status = ? "
                "ORDER BY entry_time DESC LIMIT 1",
                (market_id, status),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def log_rejection(
        self,
        market_id: str,
        market_question: str,
        layer: int,
        reason: str,
        yes_price: float = 0.0,
        liquidity: float = 0.0,
        days_to_resolution: float = 0.0,
    ) -> None:
        """
        Log a rejected market for analysis.

        Args:
            market_id: Market identifier
            market_question: Market question text
            layer: Detection layer that rejected (1-5)
            reason: Human-readable rejection reason
            yes_price: Current YES price at time of rejection
            liquidity: Available liquidity
            days_to_resolution: Days until resolution
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO rejected_markets (
                    market_id, market_question, rejection_time,
                    rejection_layer, rejection_reason, yes_price,
                    liquidity, days_to_resolution
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    market_id,
                    market_question,
                    datetime.now(timezone.utc).isoformat(),
                    layer,
                    reason,
                    yes_price,
                    liquidity,
                    days_to_resolution,
                ),
            )

    def get_daily_stats(self, date: Optional[str] = None) -> Dict[str, Any]:
        """
        Get performance statistics for a specific date.

        Always computes fresh from the positions table so that stats
        reflect trades closed since the last performance_daily upsert.

        Args:
            date: Date string (YYYY-MM-DD), defaults to today

        Returns:
            Performance statistics dictionary
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT
                    COUNT(CASE WHEN status = 'closed' AND DATE(exit_time) = ? THEN 1 END) AS trades_closed,
                    COUNT(CASE WHEN status = 'open' AND DATE(entry_time) = ? THEN 1 END) AS trades_opened,
                    COALESCE(SUM(CASE WHEN status = 'closed' AND DATE(exit_time) = ? THEN pnl ELSE 0 END), 0) AS realized_pnl,
                    COALESCE(SUM(CASE WHEN status = 'closed' AND DATE(exit_time) = ? AND pnl > 0 THEN 1 ELSE 0 END), 0) AS win_count,
                    COALESCE(SUM(CASE WHEN status = 'closed' AND DATE(exit_time) = ? AND pnl < 0 THEN 1 ELSE 0 END), 0) AS loss_count,
                    COALESCE(SUM(CASE WHEN status = 'closed' AND DATE(exit_time) = ? THEN fees_paid ELSE 0 END), 0) AS fees_paid
                FROM positions
                """,
                (date, date, date, date, date, date),
            )
            stats = dict(cursor.fetchone())
            stats["date"] = date
            return stats

    def log_scan(
        self,
        markets_scanned: int,
        candidates_found: int,
        trades_executed: int,
        scan_duration_ms: int,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Log a scan cycle's results.

        Args:
            markets_scanned: Total markets examined
            candidates_found: Candidates that passed filters
            trades_executed: Trades actually placed
            scan_duration_ms: Duration of scan in milliseconds
            error_message: Any error that occurred
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO scan_log (
                    scan_time, markets_scanned, candidates_found,
                    trades_executed, scan_duration_ms, error_message
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    markets_scanned,
                    candidates_found,
                    trades_executed,
                    scan_duration_ms,
                    error_message,
                ),
            )

    def upsert_daily_performance(self, date: str, data: Dict[str, Any]) -> None:
        """
        Insert or update daily performance record.

        Args:
            date: Date string (YYYY-MM-DD)
            data: Performance metrics to store
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO performance_daily (
                    date, trades_opened, trades_closed, realized_pnl,
                    unrealized_pnl, fees_paid, win_count, loss_count,
                    total_deployed, portfolio_balance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    trades_opened = excluded.trades_opened,
                    trades_closed = excluded.trades_closed,
                    realized_pnl = excluded.realized_pnl,
                    unrealized_pnl = excluded.unrealized_pnl,
                    fees_paid = excluded.fees_paid,
                    win_count = excluded.win_count,
                    loss_count = excluded.loss_count,
                    total_deployed = excluded.total_deployed,
                    portfolio_balance = excluded.portfolio_balance
                """,
                (
                    date,
                    data.get("trades_opened", 0),
                    data.get("trades_closed", 0),
                    data.get("realized_pnl", 0.0),
                    data.get("unrealized_pnl", 0.0),
                    data.get("fees_paid", 0.0),
                    data.get("win_count", 0),
                    data.get("loss_count", 0),
                    data.get("total_deployed", 0.0),
                    data.get("portfolio_balance", 0.0),
                ),
            )

    def log_alert(
        self,
        position_id: int,
        alert_level: str,
        current_price: float,
        message: str,
    ) -> None:
        """
        Log a position alert.

        Args:
            position_id: Database position ID
            alert_level: Alert severity ('yellow', 'orange', 'red')
            current_price: Price at time of alert
            message: Alert description
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO alerts (position_id, alert_time, alert_level, current_price, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    datetime.now(timezone.utc).isoformat(),
                    alert_level,
                    current_price,
                    message,
                ),
            )

    def get_recent_closed_positions(self, limit: int = 10) -> List[Dict]:
        """
        Get recently closed positions for loss tracking.

        Args:
            limit: Number of recent positions to return

        Returns:
            List of closed position dictionaries
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM positions WHERE status = 'closed' "
                "ORDER BY exit_time DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_all_time_stats(
        self, paper_trade: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Calculate all-time performance statistics.

        Args:
            paper_trade: If True, only paper trades; if False, only live;
                         if None, all trades.

        Returns:
            Dictionary with aggregated stats
        """
        with self._get_connection() as conn:
            if paper_trade is None:
                cursor = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total_trades,
                        COUNT(CASE WHEN status = 'closed' THEN 1 END) AS closed_trades,
                        COUNT(CASE WHEN status = 'open' THEN 1 END) AS open_trades,
                        COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl ELSE 0 END), 0) AS total_pnl,
                        COALESCE(SUM(CASE WHEN status = 'closed' AND pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                        COALESCE(SUM(CASE WHEN status = 'closed' AND pnl < 0 THEN 1 ELSE 0 END), 0) AS losses,
                        COALESCE(AVG(CASE WHEN status = 'closed' THEN pnl END), 0) AS avg_pnl,
                        COALESCE(SUM(fees_paid), 0) AS total_fees
                    FROM positions
                    """
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total_trades,
                        COUNT(CASE WHEN status = 'closed' THEN 1 END) AS closed_trades,
                        COUNT(CASE WHEN status = 'open' THEN 1 END) AS open_trades,
                        COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl ELSE 0 END), 0) AS total_pnl,
                        COALESCE(SUM(CASE WHEN status = 'closed' AND pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                        COALESCE(SUM(CASE WHEN status = 'closed' AND pnl < 0 THEN 1 ELSE 0 END), 0) AS losses,
                        COALESCE(AVG(CASE WHEN status = 'closed' THEN pnl END), 0) AS avg_pnl,
                        COALESCE(SUM(fees_paid), 0) AS total_fees
                    FROM positions
                    WHERE paper_trade = ?
                    """,
                    (1 if paper_trade else 0,),
                )
            return dict(cursor.fetchone())

    def get_performance_summary(
        self, paper_trade: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Calculate comprehensive lifetime performance metrics.

        Includes win rate, R:R, profit factor, expectancy, streaks, drawdown,
        avg hold time, and exit-reason breakdown. Computed at call-time from
        the positions table so results are always accurate.

        Args:
            paper_trade: If True, only paper trades; if False, only live;
                         if None, all trades.

        Returns:
            Dictionary of aggregated metrics. All numeric fields default to 0
            so callers can safely format them even in the zero-trades case.
        """
        with self._get_connection() as conn:
            if paper_trade is None:
                cursor = conn.execute(
                    "SELECT * FROM positions ORDER BY exit_time ASC, id ASC"
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM positions WHERE paper_trade = ? "
                    "ORDER BY exit_time ASC, id ASC",
                    (1 if paper_trade else 0,),
                )
            rows = [dict(r) for r in cursor.fetchall()]

        closed = [r for r in rows if r.get("status") == "closed"]
        open_pos = [r for r in rows if r.get("status") == "open"]
        wins = [r for r in closed if (r.get("pnl") or 0) > 0]
        losses = [r for r in closed if (r.get("pnl") or 0) < 0]

        summary: Dict[str, Any] = {
            "total_trades": len(rows),
            "closed_trades": len(closed),
            "open_trades": len(open_pos),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "total_cost_basis": 0.0,
            "roi_on_deployed": 0.0,
            "fees_paid": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "max_win": 0.0,
            "max_loss": 0.0,
            "profit_factor": 0.0,
            "rr_ratio": 0.0,
            "expectancy": 0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "peak_cum_pnl": 0.0,
            "max_drawdown_from_peak": 0.0,
            "avg_hold_hours": 0.0,
            "exit_reason_breakdown": {},
        }

        if not closed:
            return summary

        # Basic P&L
        total_pnl = sum((r.get("pnl") or 0) for r in closed)
        total_cost = sum((r.get("cost_basis") or 0) for r in closed)
        fees_paid = sum((r.get("fees_paid") or 0) for r in closed)
        summary["total_pnl"] = float(total_pnl)
        summary["total_cost_basis"] = float(total_cost)
        summary["fees_paid"] = float(fees_paid)
        summary["roi_on_deployed"] = (
            (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0
        )
        summary["win_rate"] = len(wins) / len(closed) * 100.0

        # Win / loss stats
        if wins:
            win_pnls = [r["pnl"] for r in wins]
            summary["avg_win"] = sum(win_pnls) / len(win_pnls)
            summary["max_win"] = max(win_pnls)
            win_pcts = [
                (r["pnl"] / r["cost_basis"] * 100.0)
                for r in wins
                if r.get("cost_basis")
            ]
            if win_pcts:
                summary["avg_win_pct"] = sum(win_pcts) / len(win_pcts)

        if losses:
            loss_pnls = [r["pnl"] for r in losses]
            summary["avg_loss"] = sum(loss_pnls) / len(loss_pnls)
            summary["max_loss"] = min(loss_pnls)
            loss_pcts = [
                (r["pnl"] / r["cost_basis"] * 100.0)
                for r in losses
                if r.get("cost_basis")
            ]
            if loss_pcts:
                summary["avg_loss_pct"] = sum(loss_pcts) / len(loss_pcts)

        # Risk/reward, expectancy, profit factor
        if wins and losses:
            avg_win = summary["avg_win"]
            avg_loss_abs = abs(summary["avg_loss"])
            summary["rr_ratio"] = (avg_win / avg_loss_abs) if avg_loss_abs else 0.0
            win_rate_frac = len(wins) / len(closed)
            summary["expectancy"] = (
                win_rate_frac * avg_win - (1.0 - win_rate_frac) * avg_loss_abs
            )
            gross_wins = sum(r["pnl"] for r in wins)
            gross_losses = abs(sum(r["pnl"] for r in losses))
            summary["profit_factor"] = (
                (gross_wins / gross_losses) if gross_losses > 0 else 0.0
            )

        # Streaks + equity curve / drawdown
        cur_w = cur_l = max_w = max_l = 0
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in closed:
            pnl = r.get("pnl") or 0
            if pnl > 0:
                cur_w += 1
                cur_l = 0
                max_w = max(max_w, cur_w)
            elif pnl < 0:
                cur_l += 1
                cur_w = 0
                max_l = max(max_l, cur_l)
            cum += pnl
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        summary["max_consecutive_wins"] = max_w
        summary["max_consecutive_losses"] = max_l
        summary["peak_cum_pnl"] = float(peak)
        summary["max_drawdown_from_peak"] = float(max_dd)

        # Avg holding duration
        durations: List[float] = []
        for r in closed:
            entry = r.get("entry_time")
            exit_ = r.get("exit_time")
            if not entry or not exit_:
                continue
            try:
                e = datetime.fromisoformat(str(entry).replace("Z", "+00:00"))
                x = datetime.fromisoformat(str(exit_).replace("Z", "+00:00"))
                durations.append((x - e).total_seconds() / 3600.0)
            except (ValueError, TypeError):
                continue
        if durations:
            summary["avg_hold_hours"] = sum(durations) / len(durations)

        # Exit reason breakdown
        breakdown: Dict[str, int] = {}
        for r in closed:
            reason = r.get("exit_reason") or "unknown"
            breakdown[reason] = breakdown.get(reason, 0) + 1
        summary["exit_reason_breakdown"] = breakdown

        return summary

    def get_category_exposure(
        self, category: str, paper_trade: Optional[bool] = None
    ) -> float:
        """Sum of cost_basis for open positions in the given category."""
        with self._get_connection() as conn:
            if paper_trade is None:
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(cost_basis), 0) FROM positions "
                    "WHERE status = 'open' AND LOWER(category) = LOWER(?)",
                    (category,),
                )
            else:
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(cost_basis), 0) FROM positions "
                    "WHERE status = 'open' AND LOWER(category) = LOWER(?) AND paper_trade = ?",
                    (category, 1 if paper_trade else 0),
                )
            return float(cursor.fetchone()[0] or 0.0)

    def get_event_group_exposure(
        self, event_group_id: str, paper_trade: Optional[bool] = None
    ) -> float:
        """Sum of cost_basis for open positions in the given event group."""
        if not event_group_id:
            return 0.0
        with self._get_connection() as conn:
            if paper_trade is None:
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(cost_basis), 0) FROM positions "
                    "WHERE status = 'open' AND (event_group_id = ? OR event_id = ?)",
                    (event_group_id, event_group_id),
                )
            else:
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(cost_basis), 0) FROM positions "
                    "WHERE status = 'open' AND (event_group_id = ? OR event_id = ?) "
                    "AND paper_trade = ?",
                    (event_group_id, event_group_id, 1 if paper_trade else 0),
                )
            return float(cursor.fetchone()[0] or 0.0)

    def get_risk_bucket_exposure(
        self, bucket_name: str, paper_trade: Optional[bool] = None
    ) -> float:
        """Sum of cost_basis for open positions in the given risk bucket."""
        with self._get_connection() as conn:
            if paper_trade is None:
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(cost_basis), 0) FROM positions "
                    "WHERE status = 'open' AND risk_bucket = ?",
                    (bucket_name,),
                )
            else:
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(cost_basis), 0) FROM positions "
                    "WHERE status = 'open' AND risk_bucket = ? AND paper_trade = ?",
                    (bucket_name, 1 if paper_trade else 0),
                )
            return float(cursor.fetchone()[0] or 0.0)

    def get_todays_realized_pnl(
        self, paper_trade: Optional[bool] = None
    ) -> float:
        """Sum of pnl for positions closed today (UTC)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            if paper_trade is None:
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(pnl), 0) FROM positions "
                    "WHERE status = 'closed' AND DATE(exit_time) = ?",
                    (today,),
                )
            else:
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(pnl), 0) FROM positions "
                    "WHERE status = 'closed' AND DATE(exit_time) = ? AND paper_trade = ?",
                    (today, 1 if paper_trade else 0),
                )
            return float(cursor.fetchone()[0] or 0.0)

    def get_consecutive_losses(self, paper_trade: Optional[bool] = None) -> int:
        """
        Count the number of consecutive losses from the most recent closed positions.
        Stops counting at the first win.
        """
        with self._get_connection() as conn:
            if paper_trade is None:
                cursor = conn.execute(
                    "SELECT pnl FROM positions WHERE status = 'closed' "
                    "ORDER BY exit_time DESC LIMIT 20"
                )
            else:
                cursor = conn.execute(
                    "SELECT pnl FROM positions WHERE status = 'closed' AND paper_trade = ? "
                    "ORDER BY exit_time DESC LIMIT 20",
                    (1 if paper_trade else 0,),
                )
            rows = cursor.fetchall()

        count = 0
        for row in rows:
            pnl = row[0]
            if pnl is not None and pnl < 0:
                count += 1
            else:
                break
        return count

    def get_total_deployed(self, paper_trade: Optional[bool] = None) -> float:
        """Sum of cost_basis for all open positions."""
        with self._get_connection() as conn:
            if paper_trade is None:
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(cost_basis), 0) FROM positions WHERE status = 'open'"
                )
            else:
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(cost_basis), 0) FROM positions "
                    "WHERE status = 'open' AND paper_trade = ?",
                    (1 if paper_trade else 0,),
                )
            return float(cursor.fetchone()[0] or 0.0)

    def update_high_water_mark(self, position_id: int, high_water_mark: float) -> bool:
        """Update the high-water mark price for a position."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE positions SET high_water_mark = ? WHERE id = ?",
                (high_water_mark, position_id),
            )
            return cursor.rowcount > 0

    def get_avg_trade_pnl(
        self, wins_only: bool = False, losses_only: bool = False,
        paper_trade: Optional[bool] = None
    ) -> float:
        """Average P&L per closed trade, optionally filtered to wins or losses."""
        with self._get_connection() as conn:
            base = "SELECT COALESCE(AVG(pnl), 0) FROM positions WHERE status = 'closed'"
            conditions = []
            params: list = []
            if wins_only:
                conditions.append("pnl > 0")
            if losses_only:
                conditions.append("pnl < 0")
            if paper_trade is not None:
                conditions.append("paper_trade = ?")
                params.append(1 if paper_trade else 0)
            if conditions:
                base += " AND " + " AND ".join(conditions)
            cursor = conn.execute(base, params)
            return float(cursor.fetchone()[0] or 0.0)

    def get_bucket_statistics(
        self, bucket: str, paper_trade: Optional[bool] = None
    ) -> Dict[str, Any]:
        """Get closed-trade stats for a specific risk bucket."""
        with self._get_connection() as conn:
            base = (
                "SELECT pnl FROM positions WHERE status = 'closed' AND risk_bucket = ?"
            )
            params: list = [bucket]
            if paper_trade is not None:
                base += " AND paper_trade = ?"
                params.append(1 if paper_trade else 0)
            cursor = conn.execute(base, params)
            pnls = [dict(r)["pnl"] for r in cursor.fetchall() if dict(r).get("pnl") is not None]
        return {
            "closed_count": len(pnls),
            "total_pnl": sum(pnls) if pnls else 0.0,
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
            "win_count": sum(1 for p in pnls if p > 0),
            "loss_count": sum(1 for p in pnls if p <= 0),
        }

    def get_trailing_avg_loss(
        self,
        exclude_id: Optional[int] = None,
        lookback: int = 10,
        paper_trade: Optional[bool] = None,
    ) -> float:
        """Average loss amount across the last N losing trades, optionally excluding one."""
        with self._get_connection() as conn:
            query = "SELECT pnl FROM positions WHERE pnl < 0 AND status = 'closed'"
            params: list = []
            if exclude_id is not None:
                query += " AND id != ?"
                params.append(exclude_id)
            if paper_trade is not None:
                query += " AND paper_trade = ?"
                params.append(1 if paper_trade else 0)
            query += " ORDER BY exit_time DESC LIMIT ?"
            params.append(lookback)
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        if not rows:
            return 0.0
        return sum(dict(r)["pnl"] for r in rows) / len(rows)

    def reclassify_open_positions(self, classifier: Any) -> int:
        """
        Retroactively reclassify open positions that are stuck in the 'other' bucket.

        Safe to call on every startup — only touches positions that are still 'other'.
        Returns the number of positions updated.

        Args:
            classifier: A RiskBucketClassifier instance

        Returns:
            Number of positions reclassified away from 'other'
        """
        updated = 0
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id, market_question, category FROM positions "
                "WHERE status = 'open' AND (risk_bucket = 'other' OR risk_bucket IS NULL)"
            )
            rows = cursor.fetchall()

            for row in rows:
                pos_id = row["id"]
                question = row["market_question"] or ""
                category = row["category"] or ""
                new_bucket = classifier.classify(category, question)
                if new_bucket != "other":
                    conn.execute(
                        "UPDATE positions SET risk_bucket = ? WHERE id = ?",
                        (new_bucket, pos_id),
                    )
                    logger.info(
                        f"Reclassified position {pos_id} "
                        f"'{question[:50]}' → bucket '{new_bucket}'"
                    )
                    updated += 1

        if updated:
            logger.info(f"Reclassified {updated} open position(s) from 'other' to correct bucket")
        return updated

    def get_win_rate(self, paper_trade: Optional[bool] = None) -> float:
        """Fraction of closed trades that were profitable."""
        with self._get_connection() as conn:
            if paper_trade is None:
                cursor = conn.execute(
                    "SELECT "
                    "  COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins, "
                    "  COUNT(*) AS total "
                    "FROM positions WHERE status = 'closed'"
                )
            else:
                cursor = conn.execute(
                    "SELECT "
                    "  COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins, "
                    "  COUNT(*) AS total "
                    "FROM positions WHERE status = 'closed' AND paper_trade = ?",
                    (1 if paper_trade else 0,),
                )
            row = cursor.fetchone()
            wins, total = row[0], row[1]
            return wins / total if total > 0 else 0.0
