"""
Risk Engine: portfolio-level gate for all trade entries.

No trade can be opened without passing through evaluate_entry().
Implements all 10 checks from the spec including Addenda A2, A4, A5.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from .portfolio_manager import PortfolioManager
from .risk_buckets import RiskBucketClassifier
from .utils import classify_underlying, feature_enabled, shadow_enabled

logger = logging.getLogger(__name__)


class RiskEngine:
    """
    Stateless risk evaluator. Called before every trade entry.
    Returns (approved: bool, reason: str, adjusted_size: float).

    Check order in evaluate_entry():
    1.  check_category_blocked     — temporary UMA dispute blocks
    2.  check_daily_loss_limit     — halt if today's losses >= 2%
    3.  check_consecutive_losses   — halt if last N closed were losses
    4.  check_deployment_limit     — max 70% deployed
    5.  check_category_exposure    — max 20% per category
    6.  check_event_group_exposure — max 15% per event group
    7.  check_risk_bucket_exposure — max per correlated bucket (A4)
    8.  check_position_size        — 4% hard cap, 3% target
    9.  check_volume_to_size       — max 2% of 24h volume (A5)
    10. check_liquidity            — slippage estimate (if CLOB available)
    """

    def __init__(self, config: dict, portfolio: PortfolioManager):
        self.config = config
        self.portfolio = portfolio
        risk_cfg = config.get("risk", {})

        self.max_single_market_pct = risk_cfg.get("max_single_market_pct", 0.04)
        self.target_position_pct = risk_cfg.get("target_position_pct", 0.03)
        self.max_correlated_pct = risk_cfg.get("max_correlated_pct", 0.15)
        self.max_category_exposure_pct = risk_cfg.get("max_category_exposure_pct", 0.20)
        # Bug 3 fix (2026-04-30): per-category exposure override map. Crypto cap
        # at 20% prevents BTC/ETH simultaneous-stop-out cascades.
        self.max_category_exposure_pct_by_category = risk_cfg.get(
            "max_category_exposure_pct_by_category", {}
        ) or {}
        self.max_deployed_pct = risk_cfg.get("max_deployed_pct", 0.70)
        # Bug 2 fix: while |portfolio drawdown| ≥ this, halt new entries entirely.
        exits_cfg = config.get("exits", {})
        self.drawdown_entry_halt_pct = exits_cfg.get("drawdown_entry_halt_pct", 0.0)
        self.max_daily_loss_pct = risk_cfg.get("max_daily_loss_pct", 0.02)
        self.consecutive_loss_halt = risk_cfg.get("consecutive_loss_halt", 3)
        self.max_slippage_pct = risk_cfg.get("max_slippage_pct", 0.02)
        self.volume_size_max_pct = risk_cfg.get("volume_size_max_pct", 0.02)
        self.min_viable_position = risk_cfg.get("min_viable_position", 50.0)

        self.consecutive_loss_cooldown_hours = risk_cfg.get(
            "consecutive_loss_cooldown_hours", 6
        )

        # V4 P1.5: Per-underlying concentration cap + post-stop-out cooldown.
        # Caps concurrent positions on the same canonical asset (BTC/ETH/etc.)
        # across strike/date variants. Cooldown is in-memory (resets on restart).
        self.max_positions_per_underlying = risk_cfg.get(
            "max_positions_per_underlying", 0
        )
        self.underlying_cooldown_hours = risk_cfg.get("underlying_cooldown_hours", 0)
        self._underlying_stopout_at: dict = {}  # {underlying: unix_ts}

        # V4 Phase 2.4: Same-catalyst resolution-date cluster cap.
        # Prevents >25% of deployed capital resolving within any 24h window
        # (e.g. Fed meeting day, election night) regardless of category.
        self.resolution_cluster_pct = risk_cfg.get(
            "resolution_date_cluster_pct", 0.25
        )
        self.resolution_cluster_window_hours = risk_cfg.get(
            "resolution_date_cluster_window_hours", 24
        )

        # A4: risk bucket classifier
        self.bucket_classifier = RiskBucketClassifier(config.get("risk_buckets"))

        # P2: Adaptive sizing
        adaptive_cfg = config.get("adaptive_sizing", {})
        self.adaptive_sizing_enabled = adaptive_cfg.get("enabled", False)
        self.adaptive_min_size_pct = adaptive_cfg.get("min_size_pct", 0.05)
        self.adaptive_max_size_pct = adaptive_cfg.get("max_size_pct", 0.10)

        # Scanner config refs for adaptive sizing
        scanner_cfg = config.get("scanner", {})
        self._min_entry_price = scanner_cfg.get("min_entry_price", 0.94)
        self._max_entry_price = scanner_cfg.get("max_entry_price", 0.99)
        self._preferred_resolution_hours = scanner_cfg.get("preferred_resolution_hours", 72)

        # A2: in-memory category blocks (resets on restart)
        self._blocked_categories: dict = {}

        # Circuit breaker cooldown: timestamp when the breaker first triggered
        self._circuit_breaker_triggered_at: Optional[float] = None

        # V4 Phase 2 fix: shadow-signal counters observed within a scan cycle.
        # main.py resets these at scan_cycle start and merges into pipeline_health
        # rejection_reasons at scan_cycle end.
        self.shadow_signals: dict = {}

    # ─── P2: Adaptive position sizing ───────────────────────────────────────

    def calculate_adaptive_size(
        self,
        entry_price: float,
        days_to_resolution: float,
        portfolio_balance: float,
    ) -> float:
        """
        P2: Confidence-weighted and theta-adjusted position sizing.

        Higher-probability markets (higher entry price) and shorter-duration
        markets get more capital. Lower-probability or long-duration get less.
        """
        # Confidence: linear scale from min_entry to max_entry
        entry_range = self._max_entry_price - self._min_entry_price + 0.01
        confidence_factor = (entry_price - self._min_entry_price + 0.01) / entry_range
        confidence_factor = max(0.0, min(1.0, confidence_factor))

        # Theta: inverse relationship with days to resolution
        preferred_days = self._preferred_resolution_hours / 24.0
        theta_factor = preferred_days / (preferred_days + max(days_to_resolution, 0.1))
        theta_factor = max(0.3, min(1.0, theta_factor))  # Floor at 0.3

        size_pct = self.adaptive_min_size_pct + (
            (self.adaptive_max_size_pct - self.adaptive_min_size_pct) * confidence_factor * theta_factor
        )

        return portfolio_balance * size_pct

    # ─── V3: Bucket confidence scaling ───────────────────────────────────────

    def apply_bucket_confidence_scaling(self, bucket: str, base_size: float) -> float:
        """
        V3: Scale position size based on the bucket's historical track record.
        - 0 closed trades: 0.5× (no data = cautious)
        - <3 closed trades: 0.7× (thin data)
        - Negative P&L: 0.5×
        - Positive P&L and 3+ trades: 1.0× (full confidence)
        """
        stats = self.portfolio.get_bucket_statistics(bucket)

        if stats["closed_count"] == 0:
            logger.info(f"Bucket '{bucket}' has no history — sizing at 0.5×")
            return base_size * 0.5

        if stats["closed_count"] < 3:
            logger.info(f"Bucket '{bucket}' thin data (n={stats['closed_count']}) — sizing at 0.7×")
            return base_size * 0.7

        if stats["total_pnl"] < 0:
            logger.info(f"Bucket '{bucket}' negative P&L (${stats['total_pnl']:.2f}) — sizing at 0.5×")
            return base_size * 0.5

        return base_size

    # ─── Main entry point ─────────────────────────────────────────────────────

    def evaluate_entry(
        self,
        market_id: str,
        category: str,
        event_group_id: str,
        requested_size: float,
        entry_price: float,
        market_question: str = "",
        market_volume_24h: float = 0,
        clob_client: Optional[Any] = None,
        token_id: Optional[str] = None,
        days_to_resolution: float = 7.0,
        market_resolution_time: Optional[str] = None,
        open_positions: Optional[list] = None,
    ) -> Tuple[bool, str, float]:
        """
        Run all risk checks in order. Return on first failure.
        Returns (approved, reason, adjusted_size).
        """
        balance = self.portfolio.get_portfolio_balance()

        # P2: Adaptive sizing overrides requested_size if enabled
        if self.adaptive_sizing_enabled and feature_enabled(self.config, "adaptive_sizing"):
            requested_size = self.calculate_adaptive_size(
                entry_price, days_to_resolution, balance
            )

        logger.debug(
            f"evaluate_entry: market={market_id} | "
            f"category='{category}' | question='{market_question[:80]}' | "
            f"bucket='{self.bucket_classifier.classify(category, market_question)}' | "
            f"size=${requested_size:.2f}"
        )

        # V4 Phase 2 fix: run resolution_date_cluster shadow *before* any
        # early-return check so the signal isn't masked by the underlying cap.
        # This only emits a log line — enforcement still happens at check 7.75.
        self._log_shadow_resolution_cluster(
            market_resolution_time, requested_size, balance
        )

        # 1. Category block (UMA dispute contagion) + V3: bucket block (fluke loss)
        ok, reason = self.check_category_blocked(category)
        if not ok:
            return False, reason, 0.0
        # Also check the risk bucket name itself (fluke filter blocks by bucket)
        bucket_name = self.bucket_classifier.classify(category, market_question)
        ok, reason = self.check_category_blocked(bucket_name)
        if not ok:
            return False, reason, 0.0

        # 1.5 Bug 2 fix (2026-04-30): halt new entries while in drawdown.
        # Without this, the scanner kept opening positions for the drawdown_reduction
        # loop to immediately close — 89 churn trades on 2026-04-30.
        if self.drawdown_entry_halt_pct > 0:
            dd = self.portfolio.get_portfolio_drawdown_pct(open_positions or [])
            if dd < 0 and abs(dd) >= self.drawdown_entry_halt_pct:
                return False, (
                    f"Portfolio drawdown {dd:.2%} ≥ halt threshold "
                    f"{self.drawdown_entry_halt_pct:.2%} — entries paused"
                ), 0.0

        # 2. Daily loss limit
        ok, reason = self.check_daily_loss_limit(balance)
        if not ok:
            return False, reason, 0.0

        # 3. Consecutive losses
        ok, reason = self.check_consecutive_losses()
        if not ok:
            return False, reason, 0.0

        # 4. Deployment limit
        ok, reason = self.check_deployment_limit(requested_size, balance)
        if not ok:
            return False, reason, 0.0

        # 5. Category exposure
        ok, reason = self.check_category_exposure(category, requested_size, balance)
        if not ok:
            return False, reason, 0.0

        # 6. Event group exposure
        ok, reason = self.check_event_group_exposure(event_group_id, requested_size, balance)
        if not ok:
            return False, reason, 0.0

        # 7. Risk bucket exposure (A4)
        ok, reason = self.check_risk_bucket_exposure(
            category, market_question, requested_size, balance
        )
        if not ok:
            return False, reason, 0.0

        # 7.25 V4 P1.5: Per-underlying concentration cap
        ok, reason = self.check_underlying_exposure(market_question)
        if not ok:
            return False, reason, 0.0

        # 7.5 V4 P1.5: Per-underlying post-stop-out cooldown
        ok, reason = self.check_underlying_cooldown(market_question)
        if not ok:
            return False, reason, 0.0

        # 7.75 V4 Phase 2.4: Resolution-date cluster check
        ok, reason = self.check_resolution_date_cluster(
            market_resolution_time, requested_size, balance
        )
        if not ok:
            return False, reason, 0.0

        # 8. Position size — may adjust downward
        ok, reason, adjusted_size = self.check_position_size(requested_size, balance)
        if not ok:
            return False, reason, 0.0

        # 8.5 V3: Bucket confidence scaling — reduce size for unproven buckets
        bucket = self.bucket_classifier.classify(category, market_question)
        adjusted_size = self.apply_bucket_confidence_scaling(bucket, adjusted_size)
        if adjusted_size < self.min_viable_position:
            return False, f"Bucket-scaled size ${adjusted_size:.2f} below minimum ${self.min_viable_position:.2f}", 0.0

        # 9. Volume-to-size check (A5)
        ok, reason, adjusted_size = self.check_volume_to_size(
            market_volume_24h, adjusted_size
        )
        if not ok:
            return False, reason, 0.0

        # 10. Liquidity / slippage (only if CLOB client available)
        if clob_client and token_id:
            ok, reason = self.check_liquidity(token_id, adjusted_size, clob_client)
            if not ok:
                return False, reason, 0.0

        return True, "Entry approved", adjusted_size

    # ─── Individual checks ────────────────────────────────────────────────────

    def check_category_blocked(self, category: str) -> Tuple[bool, str]:
        """A2/V3: Check if this category is temporarily blocked (UMA dispute or fluke loss)."""
        if not category:
            return True, ""

        # Check both the category name and the risk bucket name
        keys_to_check = [category.lower()]
        blocked = self._blocked_categories.get(category.lower())
        if blocked:
            # V3: Auto-expiry support
            expires_at = blocked.get("expires_at")
            if expires_at and datetime.now(timezone.utc) >= expires_at:
                del self._blocked_categories[category.lower()]
                logger.info(f"Bucket block expired: {category}")
                return True, ""
            remaining = ""
            if expires_at:
                remaining_h = (expires_at - datetime.now(timezone.utc)).total_seconds() / 3600
                remaining = f" ({remaining_h:.1f}h remaining)"
            return (
                False,
                f"Category '{category}' blocked{remaining}: {blocked['reason']}",
            )
        return True, ""

    def check_daily_loss_limit(self, portfolio_balance: float) -> Tuple[bool, str]:
        """Halt new entries if today's realized losses >= max_daily_loss_pct."""
        if portfolio_balance <= 0:
            return False, "Portfolio balance is zero"
        todays_pnl = self.portfolio.get_todays_realized_pnl()
        if todays_pnl >= 0:
            return True, ""
        loss_pct = abs(todays_pnl) / portfolio_balance
        if loss_pct >= self.max_daily_loss_pct:
            return (
                False,
                f"Daily loss limit reached: {loss_pct:.2%} >= {self.max_daily_loss_pct:.2%}",
            )
        return True, ""

    def check_consecutive_losses(self) -> Tuple[bool, str]:
        """Halt if the last N closed positions were all losses.

        Includes a time-based cooldown: after the breaker triggers, trading
        resumes once consecutive_loss_cooldown_hours have elapsed.
        """
        n = self.portfolio.get_consecutive_losses()
        if n >= self.consecutive_loss_halt:
            now = time.time()
            # Record when the breaker first triggered
            if self._circuit_breaker_triggered_at is None:
                self._circuit_breaker_triggered_at = now

            elapsed_hours = (now - self._circuit_breaker_triggered_at) / 3600
            if elapsed_hours >= self.consecutive_loss_cooldown_hours:
                logger.info(
                    f"Consecutive loss circuit breaker cooldown elapsed "
                    f"({elapsed_hours:.1f}h >= {self.consecutive_loss_cooldown_hours}h) "
                    f"— resuming trading despite {n} consecutive losses"
                )
                self._circuit_breaker_triggered_at = now  # reset for next cycle
                return True, ""

            remaining = self.consecutive_loss_cooldown_hours - elapsed_hours
            return (
                False,
                f"Consecutive loss circuit breaker: {n} losses in a row "
                f"(cooldown: {remaining:.1f}h remaining)",
            )

        # No longer in a losing streak — clear the trigger timestamp
        self._circuit_breaker_triggered_at = None
        return True, ""

    def check_deployment_limit(
        self, position_size: float, portfolio_balance: float
    ) -> Tuple[bool, str]:
        """Enforce max 70% total deployed capital."""
        if portfolio_balance <= 0:
            return False, "Portfolio balance is zero"
        deployed = self.portfolio.get_total_deployed()
        new_deployed_pct = (deployed + position_size) / portfolio_balance
        if new_deployed_pct > self.max_deployed_pct:
            return (
                False,
                f"Deployment limit: {new_deployed_pct:.1%} would exceed {self.max_deployed_pct:.0%}",
            )
        return True, ""

    def check_category_exposure(
        self, category: str, position_size: float, portfolio_balance: float
    ) -> Tuple[bool, str]:
        """Enforce per-category portfolio exposure cap.

        Uses the per-category override map first (e.g., Crypto: 0.20), falling
        back to the global ``max_category_exposure_pct``.
        """
        if not category or portfolio_balance <= 0:
            return True, ""
        cap = self.max_category_exposure_pct_by_category.get(
            category, self.max_category_exposure_pct
        )
        current = self.portfolio.get_category_exposure(category)
        new_pct = (current + position_size) / portfolio_balance
        if new_pct > cap:
            return (
                False,
                f"Category '{category}' exposure {new_pct:.1%} would exceed {cap:.0%}",
            )
        return True, ""

    def check_event_group_exposure(
        self, event_group_id: str, position_size: float, portfolio_balance: float
    ) -> Tuple[bool, str]:
        """Enforce max 15% portfolio exposure per event group (same event ID)."""
        if not event_group_id or portfolio_balance <= 0:
            return True, ""
        current = self.portfolio.get_event_group_exposure(event_group_id)
        new_pct = (current + position_size) / portfolio_balance
        if new_pct > self.max_correlated_pct:
            return (
                False,
                f"Event group '{event_group_id}' exposure {new_pct:.1%} would exceed "
                f"{self.max_correlated_pct:.0%}",
            )
        return True, ""

    def check_risk_bucket_exposure(
        self,
        category: str,
        market_question: str,
        position_size: float,
        portfolio_balance: float,
    ) -> Tuple[bool, str]:
        """
        A4: Classify market into a correlated risk bucket and check exposure.
        Separate from category check — catches markets Polymarket labels differently
        but that share macro-level risk (e.g., multiple political markets).
        """
        if portfolio_balance <= 0:
            return True, ""
        bucket = self.bucket_classifier.classify(category, market_question)
        max_exposure = self.bucket_classifier.get_max_exposure(bucket)
        current = self.portfolio.get_risk_bucket_exposure(bucket)
        new_pct = (current + position_size) / portfolio_balance
        if new_pct > max_exposure:
            return (
                False,
                f"Risk bucket '{bucket}' exposure {new_pct:.1%} would exceed {max_exposure:.0%}",
            )
        return True, ""

    # ─── V4 P1.5: Per-underlying concentration + cooldown ────────────────────

    def check_underlying_exposure(self, market_question: str) -> Tuple[bool, str]:
        """Cap concurrent open positions on the same canonical underlying.

        Counts open positions whose market_question maps to the same underlying
        (e.g. multiple 'Bitcoin above $X on April Y' markets all map to 'BTC').
        Skips the check when max_positions_per_underlying <= 0 or the market's
        underlying cannot be identified.
        """
        if self.max_positions_per_underlying <= 0:
            return True, ""
        underlying = classify_underlying(market_question)
        if not underlying:
            return True, ""
        try:
            open_positions = self.portfolio.db.get_open_positions()
        except Exception:
            return True, ""  # Fail open (avoid blocking trades on DB hiccup)
        n = sum(
            1 for pos in open_positions
            if classify_underlying(pos.get("market_question") or "") == underlying
        )
        if n >= self.max_positions_per_underlying:
            return (
                False,
                f"Underlying '{underlying}' has {n} open positions "
                f"(max {self.max_positions_per_underlying})",
            )
        return True, ""

    def check_underlying_cooldown(self, market_question: str) -> Tuple[bool, str]:
        """Reject entries on an underlying that recently suffered a stop-out."""
        if self.underlying_cooldown_hours <= 0:
            return True, ""
        underlying = classify_underlying(market_question)
        if not underlying:
            return True, ""
        last_ts = self._underlying_stopout_at.get(underlying)
        if not last_ts:
            return True, ""
        elapsed_hours = (time.time() - last_ts) / 3600
        if elapsed_hours >= self.underlying_cooldown_hours:
            # Cooldown expired — clear tracker
            self._underlying_stopout_at.pop(underlying, None)
            return True, ""
        remaining = self.underlying_cooldown_hours - elapsed_hours
        return (
            False,
            f"Underlying '{underlying}' on stop-out cooldown "
            f"({remaining:.1f}h remaining)",
        )

    def register_underlying_stopout(self, market_question: str) -> None:
        """Record a stop-out / teleportation on this underlying to start cooldown."""
        if self.underlying_cooldown_hours <= 0:
            return
        underlying = classify_underlying(market_question)
        if not underlying:
            return
        self._underlying_stopout_at[underlying] = time.time()
        logger.info(
            f"Registered stop-out cooldown for '{underlying}' "
            f"({self.underlying_cooldown_hours}h)"
        )

    def check_resolution_date_cluster(
        self,
        resolution_time_iso: Optional[str],
        requested_size: float,
        portfolio_balance: float,
    ) -> Tuple[bool, str]:
        """V4 Phase 2.4: reject if adding this position would push >25% of
        portfolio into any 24h resolution window (same-catalyst correlation).

        Gated by feature flag `resolution_date_cluster`. The shadow-mode log
        is emitted by :meth:`_log_shadow_resolution_cluster` earlier in
        evaluate_entry — this method only handles enforcement so it isn't
        masked by upstream early-returns.
        """
        enforce = feature_enabled(self.config, "resolution_date_cluster")
        if not enforce:
            return True, "ok"
        if not resolution_time_iso:
            return True, "ok"
        if portfolio_balance <= 0:
            return True, "ok"

        same_window = self.portfolio.get_resolution_date_exposure(
            resolution_time_iso,
            window_hours=self.resolution_cluster_window_hours,
        )
        total_after = same_window + requested_size
        pct_after = total_after / portfolio_balance

        if pct_after > self.resolution_cluster_pct:
            msg = (
                f"Resolution-date cluster: ${total_after:,.0f} "
                f"({pct_after:.1%}) would resolve within "
                f"{self.resolution_cluster_window_hours}h of {resolution_time_iso} "
                f"(max {self.resolution_cluster_pct:.0%})"
            )
            return False, msg
        return True, "ok"

    def _log_shadow_resolution_cluster(
        self,
        resolution_time_iso: Optional[str],
        requested_size: float,
        portfolio_balance: float,
    ) -> None:
        """V4 Phase 2 fix: emit the shadow-mode log *before* any early-return
        risk check can mask it. Pure log side-effect, no return value.
        """
        if not shadow_enabled(self.config, "resolution_date_cluster"):
            return
        if feature_enabled(self.config, "resolution_date_cluster"):
            return  # enforce path handles it downstream; avoid duplicate logs
        if not resolution_time_iso or portfolio_balance <= 0:
            return
        try:
            same_window = self.portfolio.get_resolution_date_exposure(
                resolution_time_iso,
                window_hours=self.resolution_cluster_window_hours,
            )
        except Exception as exc:
            logger.debug(f"shadow resolution_date_cluster lookup failed: {exc}")
            return
        total_after = same_window + requested_size
        pct_after = total_after / portfolio_balance
        if pct_after > self.resolution_cluster_pct:
            logger.info(
                f"[SHADOW resolution_date_cluster] would reject: "
                f"${total_after:,.0f} ({pct_after:.1%}) would resolve within "
                f"{self.resolution_cluster_window_hours}h of "
                f"{resolution_time_iso} (max {self.resolution_cluster_pct:.0%})"
            )
            self.shadow_signals["resolution_date_cluster"] = (
                self.shadow_signals.get("resolution_date_cluster", 0) + 1
            )

    def check_position_size(
        self, requested_size: float, portfolio_balance: float
    ) -> Tuple[bool, str, float]:
        """
        Enforce 4% hard cap, cap to 3% target if above target.
        Returns (ok, reason, adjusted_size).
        """
        if portfolio_balance <= 0:
            return False, "Portfolio balance is zero", 0.0

        max_size = portfolio_balance * self.max_single_market_pct
        target_size = portfolio_balance * self.target_position_pct

        if requested_size > max_size:
            return (
                False,
                f"Position size ${requested_size:.2f} exceeds hard cap "
                f"${max_size:.2f} ({self.max_single_market_pct:.0%})",
                0.0,
            )

        if requested_size > target_size:
            logger.debug(
                f"Position size ${requested_size:.2f} capped to target ${target_size:.2f}"
            )
            return True, f"Size capped to {self.target_position_pct:.0%} target", target_size

        return True, "", requested_size

    def check_volume_to_size(
        self, market_volume_24h: float, requested_size: float
    ) -> Tuple[bool, str, float]:
        """
        A5: Ensure position size is no more than volume_size_max_pct of 24h volume.
        If the market can't absorb the stop-loss sell within a day's volume, reject.
        """
        if market_volume_24h <= 0:
            return False, "No 24h volume data — rejecting entry", 0.0

        max_allowed = market_volume_24h * self.volume_size_max_pct
        if max_allowed < self.min_viable_position:
            return (
                False,
                f"Market 24h volume ${market_volume_24h:.0f} too low for any viable position",
                0.0,
            )

        if requested_size > max_allowed:
            logger.debug(
                f"Size ${requested_size:.2f} capped to volume limit ${max_allowed:.2f} "
                f"({self.volume_size_max_pct:.0%} of ${market_volume_24h:.0f} 24h vol)"
            )
            return True, "Size capped by 24h volume", max_allowed

        return True, "", requested_size

    def check_liquidity(
        self,
        token_id: str,
        position_size: float,
        clob_client: Any,
    ) -> Tuple[bool, str]:
        """
        Fetch orderbook and estimate fill slippage for the requested size.
        Reject if estimated slippage > max_slippage_pct.
        """
        try:
            orderbook = clob_client.get_order_book(token_id)
            if not orderbook:
                return True, ""  # Cannot check — allow through

            asks = getattr(orderbook, "asks", []) or []
            if not asks:
                return True, ""

            best_ask = float(getattr(asks[0], "price", 0))
            if best_ask <= 0:
                return True, ""

            # Estimate average fill price by walking the book
            total_cost = 0.0
            remaining = position_size
            for ask in asks:
                ask_price = float(getattr(ask, "price", 0))
                ask_size_usdc = float(getattr(ask, "size", 0)) * ask_price
                if ask_size_usdc <= 0:
                    continue
                fill = min(remaining, ask_size_usdc)
                total_cost += fill * (ask_price / best_ask)  # relative to best ask
                remaining -= fill
                if remaining <= 0:
                    break

            if remaining > 0:
                # Order book too thin to fill
                return (
                    False,
                    f"Orderbook too thin: ${remaining:.2f} unfillable at token {token_id}",
                )

            avg_fill_ratio = total_cost / position_size
            slippage = avg_fill_ratio - 1.0
            if slippage > self.max_slippage_pct:
                return (
                    False,
                    f"Estimated slippage {slippage:.2%} exceeds max {self.max_slippage_pct:.2%}",
                )

        except Exception as e:
            logger.warning(f"Liquidity check failed for {token_id}: {e}")
            return False, f"Liquidity check failed: {e}"

        return True, ""

    # ─── UMA dispute management (A2) ─────────────────────────────────────────

    def add_temporary_category_block(
        self, category: str, reason: str, duration_hours: Optional[int] = None
    ) -> None:
        """
        A2/V3: Temporarily block new entries in a category/bucket.
        If duration_hours is set, the block auto-expires. Otherwise permanent until restart.
        Stored in-memory — resets on bot restart.
        """
        if not category:
            return
        key = category.lower()
        block: dict = {
            "reason": reason,
            "blocked_at": datetime.now(timezone.utc),
        }
        if duration_hours is not None:
            from datetime import timedelta
            block["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
        self._blocked_categories[key] = block
        dur_str = f" for {duration_hours}h" if duration_hours else ""
        logger.warning(f"CATEGORY BLOCKED{dur_str}: '{category}' — {reason}")

    def remove_category_block(self, category: str) -> None:
        """Remove a temporary category block (e.g., after dispute resolves)."""
        self._blocked_categories.pop(category.lower(), None)
        logger.info(f"Category block removed for '{category}'")
