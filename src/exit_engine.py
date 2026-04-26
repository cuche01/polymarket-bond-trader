"""
Exit Engine: active position exit manager.

Evaluates every open position each cycle and returns ExitDecisions.
Runs BEFORE entry scanning so capital is freed and risk limits enforced
before new positions are considered.

Exit priority order (highest first):
  1. Market resolved              → always close
  2. UMA dispute detected (A2)   → lock tracking, block category
  3. Stop-loss breached          → immediate close
  4. Trailing stop triggered (A6)→ immediate close if activated
  5. Time limit exceeded         → normal close
  6. Take-profit reached         → normal close (bond-specific logic)
  7. Portfolio drawdown          → handled portfolio-wide, not per-position
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .portfolio_manager import PortfolioManager
from .utils import (
    calculate_taker_fee,
    feature_enabled,
    get_days_to_resolution,
    parse_iso_datetime,
    safe_json_parse,
)

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


@dataclass
class ExitDecision:
    """Result of evaluating a single position."""
    action: str      # "hold", "close_full", "close_partial"
    reason: str      # "resolution_win", "resolution_loss", "stop_loss",
                     # "trailing_stop", "time_exit", "take_profit",
                     # "drawdown_reduction", "manual"
    close_pct: float  # 0.0 to 1.0
    urgency: str     # "immediate" (FOK) or "normal" (limit at best bid)


_HOLD = ExitDecision(action="hold", reason="", close_pct=0.0, urgency="normal")


class ExitEngine:
    """
    Evaluates all open positions every cycle and returns exit decisions.
    """

    def __init__(
        self,
        config: dict,
        portfolio: PortfolioManager,
        risk_engine: Any,  # RiskEngine — for UMA dispute category blocking
        notifier: Optional[Any] = None,
    ):
        self.config = config
        self.portfolio = portfolio
        self.risk_engine = risk_engine
        self.notifier = notifier

        exits_cfg = config.get("exits", {})
        alerts_cfg = config.get("alerts", {})

        self.stop_loss_pct = exits_cfg.get("stop_loss_pct", 0.07)
        self.max_holding_days = exits_cfg.get("max_holding_days", 5)
        self.stale_resolution_hours = exits_cfg.get("stale_resolution_hours", 48)
        self.bond_take_profit_price = exits_cfg.get("bond_take_profit_price", 0.99)
        self.bond_take_profit_min_hours = exits_cfg.get(
            "bond_take_profit_min_hours_to_resolution", 48
        )
        self.generic_take_profit_pct = exits_cfg.get("generic_take_profit_pct", 0.10)
        self.partial_scaling_enabled = exits_cfg.get("partial_scaling_enabled", False)
        self.partial_close_trigger_pct = exits_cfg.get("partial_close_trigger_pct", 0.08)
        self.partial_close_pct = exits_cfg.get("partial_close_pct", 0.50)
        self.portfolio_drawdown_alert_pct = exits_cfg.get("portfolio_drawdown_alert_pct", 0.03)
        self.portfolio_drawdown_critical_pct = exits_cfg.get("portfolio_drawdown_critical_pct", 0.05)

        self.yellow_loss_pct = alerts_cfg.get("yellow_loss_pct", 0.03)
        self.orange_loss_pct = alerts_cfg.get("orange_loss_pct", 0.05)

        # A6: trailing stop
        self.trailing_stop_activation_price = exits_cfg.get(
            "trailing_stop_activation_price", 0.995
        )
        self.trailing_stop_distance_pct = exits_cfg.get("trailing_stop_distance_pct", 0.005)

        # R2: tiered stop-loss by entry price
        self.tiered_stop_loss = exits_cfg.get("tiered_stop_loss", [])
        # Sort descending by min_entry so we match highest tier first
        self.tiered_stop_loss.sort(key=lambda t: t.get("min_entry", 0), reverse=True)

        # V4 P1.5: Grace period before stop-loss / trailing-stop can fire.
        # Teleportation, revalidation_failed, resolution exits are *not*
        # suppressed — only price-action exits driven by noise are deferred.
        self.entry_grace_period_hours = exits_cfg.get("entry_grace_period_hours", 0)

        # P0: Teleportation slippage protection
        teleport_cfg = config.get("teleportation", {})
        self.teleportation_max_loss_pct = teleport_cfg.get("teleportation_max_loss_pct", 0.50)
        self.teleportation_detection_multiplier = teleport_cfg.get(
            "teleportation_detection_multiplier", 2.0
        )
        self.teleportation_exit_slippage_pct = teleport_cfg.get(
            "teleportation_exit_slippage_pct", 0.10
        )

        # P2: Post-entry re-validation
        self.revalidation_interval_hours = exits_cfg.get("revalidation_interval_hours", 4)

        # P3: Exit fee optimization (legacy flat rate, used as fallback only)
        self._fee_rate = 0.002
        # V4 1.2: Dynamic fees
        self._fees_cfg = config.get("fees", {}) or {}

        self._session: Optional[aiohttp.ClientSession] = None

        # Alert deduplication: {(position_id, alert_level): last_sent_timestamp}
        self._alert_cooldowns: Dict[tuple, float] = {}
        self._alert_cooldown_seconds = 3600  # 1 hour between repeat alerts

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ─── Market data helper (R11) ────────────────────────────────────────────

    async def _fetch_market_data(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Fetch market data from Gamma API once for multiple checks."""
        if not market_id:
            return None
        try:
            session = await self._get_session()
            async with session.get(f"{GAMMA_API_BASE}/markets/{market_id}") as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.debug(f"Market data fetch failed for {market_id}: {e}")
        return None

    # ─── Main evaluation ──────────────────────────────────────────────────────

    async def evaluate_all_positions(
        self,
        positions: List[Dict[str, Any]],
        clob_client: Optional[Any] = None,
    ) -> List[Tuple[Dict[str, Any], ExitDecision]]:
        """
        For each open position, run exit checks in priority order.
        Returns list of (position, decision) pairs where action != "hold".
        """
        tasks = [
            self._evaluate_single(pos, clob_client)
            for pos in positions
            if pos.get("status") == "open"
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        decisions = []
        for pos, result in zip(positions, results):
            if isinstance(result, Exception):
                logger.error(f"Exit evaluation error for position {pos.get('id')}: {result}")
                continue
            if result.action != "hold":
                decisions.append((pos, result))
        return decisions

    async def _evaluate_single(
        self,
        position: Dict[str, Any],
        clob_client: Optional[Any] = None,
    ) -> ExitDecision:
        """Evaluate a single position through all exit checks in priority order."""
        # Fetch current price and store on position for downstream use
        current_price = await self._get_current_price(position, clob_client)
        if current_price is not None:
            position["_current_price"] = current_price

        # R11: Fetch market data once for resolution + UMA checks
        market_id = position.get("market_id", "")
        market_data = await self._fetch_market_data(market_id)

        # 1. Market resolved
        decision = await self.check_resolution(position, market_data=market_data)
        if decision:
            return decision

        # 2. UMA dispute (A2)
        decision = await self.check_uma_dispute(position, market_data=market_data)
        if decision:
            return decision

        if current_price is None:
            return _HOLD

        # 2.5. Teleportation detection (P0) — gap-down past stop-loss
        if feature_enabled(self.config, "teleportation_detection"):
            decision = self.check_teleportation(position, current_price)
            if decision:
                return decision

        # 3. Stop-loss
        decision = self.check_stop_loss(position, current_price)
        if decision:
            return decision

        # 4. Trailing stop (A6)
        decision = self.check_trailing_stop(position, current_price)
        if decision:
            return decision

        # 5. Time exit
        decision = self.check_time_exit(position)
        if decision:
            return decision

        # 6. Take-profit (with P3 fee optimization)
        decision = self.check_take_profit(position, current_price)
        if decision:
            return decision

        # 7.5. Post-entry re-validation (P2)
        if feature_enabled(self.config, "post_entry_revalidation"):
            decision = await self.check_revalidation(position, market_data)
            if decision:
                return decision

        # Non-exit: notification-only alerts
        self._check_alerts(position, current_price)

        return _HOLD

    # ─── Individual exit checks ───────────────────────────────────────────────

    async def check_resolution(
        self, position: Dict[str, Any], market_data: Optional[Dict[str, Any]] = None
    ) -> Optional[ExitDecision]:
        """Poll Gamma API for market closure. Highest-priority exit."""
        market_id = position.get("market_id", "")
        if not market_id:
            return None

        data = market_data
        if data is None:
            data = await self._fetch_market_data(market_id)
        if data is None:
            return None

        try:
            logger.debug(
                f"Resolution check for '{position.get('market_question', market_id)[:50]}': "
                f"closed={data.get('closed')}, resolved={data.get('resolved')}, "
                f"uma_status='{data.get('umaResolutionStatus', '')}'"
            )

            if data.get("resolved") or data.get("closed"):
                winner = data.get("winner") or data.get("resolvedOutcome")
                if winner:
                    outcome = "YES" if str(winner).upper() in ("YES", "1", "TRUE") else "NO"
                    reason = "resolution_win" if outcome == "YES" else "resolution_loss"
                    return ExitDecision(
                        action="close_full", reason=reason,
                        close_pct=1.0, urgency="normal"
                    )

                outcome_prices = safe_json_parse(data.get("outcomePrices"))
                if outcome_prices:
                    yes_price = float(outcome_prices[0])
                    if yes_price >= 0.99:
                        return ExitDecision(
                            action="close_full", reason="resolution_win",
                            close_pct=1.0, urgency="normal"
                        )
                    elif yes_price <= 0.01:
                        return ExitDecision(
                            action="close_full", reason="resolution_loss",
                            close_pct=1.0, urgency="normal"
                        )

            # Flag stale (past end date but not resolved)
            end_date = data.get("endDate") or data.get("end_date_iso")
            if end_date:
                days_past = -get_days_to_resolution(end_date)
                stale_days = self.stale_resolution_hours / 24
                if days_past > stale_days:
                    logger.warning(
                        f"Position {position.get('id')} market {market_id} "
                        f"past end date by {days_past:.1f}d, awaiting resolution"
                    )

        except Exception as e:
            logger.debug(f"Resolution check failed for {market_id}: {e}")

        return None

    async def check_uma_dispute(
        self, position: Dict[str, Any], market_data: Optional[Dict[str, Any]] = None
    ) -> Optional[ExitDecision]:
        """
        A2: Detect UMA dispute status. Capital is locked — cannot exit.
        Side effect: marks position as 'disputed' and blocks category for new entries.
        Returns None (hold) since disputes cannot be exited.
        """
        market_id = position.get("market_id", "")
        if not market_id:
            return None

        data = market_data
        if data is None:
            data = await self._fetch_market_data(market_id)
        if data is None:
            return None

        try:
            uma_status = data.get("umaResolutionStatus", "")

            if uma_status and "dispute" in uma_status.lower():
                if position.get("status") != "disputed":
                    position_id = position.get("id")
                    if position_id:
                        self.portfolio.update_position_status(position_id, "disputed")
                        self.portfolio.db.update_position(position_id, {
                            "uma_dispute_detected_at": datetime.now(timezone.utc).isoformat()
                        })

                    category = position.get("category", "")
                    if category and self.risk_engine:
                        self.risk_engine.add_temporary_category_block(
                            category,
                            f"UMA dispute in market {market_id}: {uma_status}",
                        )

                    msg = (
                        f"UMA Dispute: {position.get('market_question', market_id)[:80]}\n"
                        f"Status: {uma_status}\nCapital locked — cannot exit."
                    )
                    if self.notifier:
                        await self.notifier.send_warning(msg, level="red")
                    logger.critical(
                        f"UMA DISPUTE detected for position {position.get('id')} "
                        f"market {market_id}: {uma_status}"
                    )

        except Exception as e:
            logger.debug(f"UMA dispute check failed for {market_id}: {e}")

        return None  # Cannot exit during dispute; just track

    def _get_tiered_stop_loss(self, entry_price: float) -> float:
        """R2: Get the applicable stop-loss percentage based on entry price tier."""
        for tier in self.tiered_stop_loss:
            if entry_price >= tier.get("min_entry", 0):
                return tier.get("stop_loss_pct", self.stop_loss_pct)
        return self.stop_loss_pct  # fallback to default

    def _in_grace_period(self, position: Dict[str, Any]) -> bool:
        """V4 P1.5: True if position entered < entry_grace_period_hours ago."""
        if self.entry_grace_period_hours <= 0:
            return False
        entry_time_str = position.get("entry_time", "")
        if not entry_time_str:
            return False
        entry_time = parse_iso_datetime(entry_time_str)
        if not entry_time:
            return False
        now = datetime.now(timezone.utc)
        held_hours = (now - entry_time).total_seconds() / 3600
        return held_hours < self.entry_grace_period_hours

    def check_teleportation(
        self, position: Dict[str, Any], current_price: float
    ) -> Optional[ExitDecision]:
        """
        P0: Detect price teleportation — a gap-down that skips past the stop-loss.
        If the price has dropped more than 2x the stop-loss tier in a single cycle,
        this is a teleportation event requiring emergency exit.
        """
        entry_price = position.get("entry_price", 0)
        if entry_price <= 0:
            return None

        stop_loss_pct = self._get_tiered_stop_loss(entry_price)
        stop_trigger = entry_price * (1 - stop_loss_pct)

        # Only fires if price is already below the stop-loss level
        if current_price >= stop_trigger:
            return None

        drop_pct = (entry_price - current_price) / entry_price

        # Check if this is a teleportation (drop > multiplier * stop tier)
        if drop_pct <= stop_loss_pct * self.teleportation_detection_multiplier:
            return None  # Normal stop-loss territory, let check_stop_loss handle it

        logger.critical(
            f"TELEPORTATION detected for position {position.get('id')}: "
            f"entry=${entry_price:.4f}, current=${current_price:.4f}, "
            f"drop={drop_pct:.2%} >> stop_loss={stop_loss_pct:.2%}"
        )

        if self.notifier:
            try:
                asyncio.create_task(
                    self.notifier.send_teleportation_alert(
                        position, entry_price, current_price, drop_pct
                    )
                )
            except RuntimeError:
                pass  # No running event loop (e.g., in tests)

        if drop_pct >= self.teleportation_max_loss_pct:
            # Catastrophic — exit at any price
            return ExitDecision(
                action="close_full", reason="teleportation_catastrophic",
                close_pct=1.0, urgency="immediate"
            )
        else:
            # Survivable teleportation — use wider slippage band
            return ExitDecision(
                action="close_full", reason="teleportation_detected",
                close_pct=1.0, urgency="immediate"
            )

    def check_stop_loss(
        self, position: Dict[str, Any], current_price: float
    ) -> Optional[ExitDecision]:
        """
        R2: Tiered stop-loss — tighter stops for higher-entry bonds.
        Falls back to default stop_loss_pct if no tier matches.
        V4 P1.5: Suppressed during the initial grace period to avoid
        transient-liquidity / spread-widening exits on bond entries.
        """
        entry_price = position.get("entry_price", 0)
        if entry_price <= 0:
            return None

        if self._in_grace_period(position):
            return None

        applicable_stop = self._get_tiered_stop_loss(entry_price)
        loss_pct = (entry_price - current_price) / entry_price
        if loss_pct >= applicable_stop:
            logger.warning(
                f"STOP-LOSS triggered for position {position.get('id')}: "
                f"entry=${entry_price:.4f}, current=${current_price:.4f}, "
                f"loss={loss_pct:.2%} >= {applicable_stop:.2%} (tiered)"
            )
            return ExitDecision(
                action="close_full", reason="stop_loss",
                close_pct=1.0, urgency="immediate"
            )
        return None

    def check_trailing_stop(
        self, position: Dict[str, Any], current_price: float
    ) -> Optional[ExitDecision]:
        """
        A6: Trailing stop — activates once price >= trailing_stop_activation_price.
        Once active, tracks high-water mark and exits if price drops
        trailing_stop_distance_pct below the high-water mark.
        V4 P1.5: Suppressed during the initial grace period.
        """
        if self._in_grace_period(position):
            return None

        high_water = position.get("high_water_mark") or 0.0

        # Only check activation threshold if no high-water mark has been set yet.
        # Once the trailing stop is activated (high_water >= activation_price),
        # it stays active even if price falls back below the activation level.
        if high_water < self.trailing_stop_activation_price:
            if current_price < self.trailing_stop_activation_price:
                return None
        if current_price > high_water:
            high_water = current_price
            position["high_water_mark"] = high_water
            pos_id = position.get("id")
            if pos_id:
                self.portfolio.update_high_water_mark(pos_id, high_water)

        trail_price = high_water * (1 - self.trailing_stop_distance_pct)
        if current_price <= trail_price:
            logger.info(
                f"TRAILING STOP triggered for position {position.get('id')}: "
                f"high_water=${high_water:.4f}, current=${current_price:.4f}, "
                f"trail_price=${trail_price:.4f}"
            )
            return ExitDecision(
                action="close_full", reason="trailing_stop",
                close_pct=1.0, urgency="immediate"
            )
        return None

    def check_time_exit(
        self, position: Dict[str, Any]
    ) -> Optional[ExitDecision]:
        """
        R3: Dynamic max holding — extends to expected_resolution + 2 days,
        capped at 14 days absolute. Falls back to config max_holding_days.
        """
        entry_time_str = position.get("entry_time", "")
        if not entry_time_str:
            return None

        try:
            entry_time = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            held_days = (now - entry_time).total_seconds() / 86400

            # R3: Calculate dynamic max from expected resolution date
            dynamic_max = self.max_holding_days
            expected = position.get("expected_resolution", "")
            if expected:
                days_to_res = get_days_to_resolution(expected)
                if days_to_res > 0:
                    # Allow holding until resolution + 2 day buffer
                    days_since_entry = held_days
                    total_needed = days_since_entry + days_to_res + 2
                    dynamic_max = max(total_needed, self.max_holding_days)
                    dynamic_max = min(dynamic_max, 14)  # absolute cap

            if held_days >= dynamic_max:
                logger.warning(
                    f"TIME EXIT for position {position.get('id')}: "
                    f"held {held_days:.1f}d >= max {dynamic_max:.1f}d"
                )
                return ExitDecision(
                    action="close_full", reason="time_exit",
                    close_pct=1.0, urgency="normal"
                )
        except Exception as e:
            logger.debug(f"Time exit check failed: {e}")

        return None

    def check_take_profit(
        self, position: Dict[str, Any], current_price: float
    ) -> Optional[ExitDecision]:
        """
        Bond-specific take-profit logic:
        - Bond positions (entry >= $0.95): early exit only if price >= $0.99
          AND market resolution is > 48 hours away. If resolution is close, hold.
        - Non-bond positions (entry < $0.95): standard 10% take-profit.
        """
        entry_price = position.get("entry_price", 0)
        if entry_price <= 0:
            return None

        is_bond = entry_price >= 0.95

        if is_bond:
            if current_price < self.bond_take_profit_price:
                return None

            # Check hours to resolution
            expected = position.get("expected_resolution", "")
            if expected:
                days_remaining = get_days_to_resolution(expected)
                hours_remaining = days_remaining * 24
                if hours_remaining <= self.bond_take_profit_min_hours:
                    return None  # Close to resolution — hold for full $1.00

            # P3: Exit fee optimization — compare sell EV vs hold EV
            if feature_enabled(self.config, "exit_fee_optimization"):
                verdict = self.should_take_profit_or_hold(position, current_price)
                if verdict == "hold":
                    return None  # EV says hold is better than selling

            logger.info(
                f"TAKE-PROFIT for bond position {position.get('id')}: "
                f"price=${current_price:.4f} >= ${self.bond_take_profit_price:.4f}, "
                f"resolution > {self.bond_take_profit_min_hours}h away"
            )
            return ExitDecision(
                action="close_full", reason="take_profit",
                close_pct=1.0, urgency="normal"
            )
        else:
            # Non-bond: standard percentage take-profit
            profit_pct = (current_price - entry_price) / entry_price
            if profit_pct >= self.generic_take_profit_pct:
                logger.info(
                    f"TAKE-PROFIT for position {position.get('id')}: "
                    f"profit={profit_pct:.2%} >= {self.generic_take_profit_pct:.2%}"
                )
                if (
                    self.partial_scaling_enabled
                    and profit_pct >= self.partial_close_trigger_pct
                ):
                    return ExitDecision(
                        action="close_partial", reason="take_profit",
                        close_pct=self.partial_close_pct, urgency="normal"
                    )
                return ExitDecision(
                    action="close_full", reason="take_profit",
                    close_pct=1.0, urgency="normal"
                )

        return None

    # ─── P2: Post-entry re-validation ──────────────────────────────────────────

    async def check_revalidation(
        self, position: Dict[str, Any], market_data: Optional[Dict[str, Any]] = None
    ) -> Optional[ExitDecision]:
        """
        P2: Re-run validation checks against current market data for open positions.
        Only runs every revalidation_interval_hours. Checks price stability,
        spread health, and volume sanity on the live market.
        """
        last_reval = position.get("last_revalidation_time")
        if last_reval:
            last_dt = parse_iso_datetime(last_reval)
            if last_dt:
                now = datetime.now(timezone.utc)
                hours_since = (now - last_dt).total_seconds() / 3600
                if hours_since < self.revalidation_interval_hours:
                    return None

        if market_data is None:
            market_data = await self._fetch_market_data(position.get("market_id", ""))
        if market_data is None:
            return None

        # Update revalidation timestamp regardless of outcome
        pos_id = position.get("id")
        if pos_id:
            self.portfolio.db.update_position(pos_id, {
                "last_revalidation_time": datetime.now(timezone.utc).isoformat()
            })

        # Check: large 1-day price change (instability)
        price_change_1d = abs(float(market_data.get("oneDayPriceChange") or 0))
        if price_change_1d > 0.15:
            logger.warning(
                f"Re-validation FAILED for position {pos_id}: "
                f"1d price change {price_change_1d:.2%}"
            )
            return ExitDecision(
                action="close_full", reason="revalidation_failed",
                close_pct=1.0, urgency="normal"
            )

        # Check: spread blew out
        best_ask = float(market_data.get("bestAsk") or 0)
        best_bid = float(market_data.get("bestBid") or 0)
        if best_ask > 0 and best_bid > 0:
            spread = best_ask - best_bid
            if spread > 0.10:
                logger.warning(
                    f"Re-validation FAILED for position {pos_id}: "
                    f"spread {spread:.4f} > 0.10"
                )
                return ExitDecision(
                    action="close_full", reason="revalidation_failed",
                    close_pct=1.0, urgency="normal"
                )

        # Check: suspicious volume/liquidity ratio
        volume_24h = float(market_data.get("volume24hr") or 0)
        liquidity = float(market_data.get("liquidityClob") or market_data.get("liquidity") or 0)
        if volume_24h > 0 and liquidity > 0 and (volume_24h / liquidity) > 10:
            logger.warning(
                f"Re-validation FAILED for position {pos_id}: "
                f"volume/liquidity ratio {volume_24h / liquidity:.1f}"
            )
            return ExitDecision(
                action="close_full", reason="revalidation_failed",
                close_pct=1.0, urgency="normal"
            )

        return None

    # ─── P3: Exit fee optimization ───────────────────────────────────────────

    def should_take_profit_or_hold(
        self, position: Dict[str, Any], current_price: float
    ) -> str:
        """
        P3: Compare EV of selling now vs holding to resolution.
        Returns "sell" or "hold".
        """
        entry_price = position.get("entry_price", 0)
        shares = position.get("shares", 0)
        if entry_price <= 0 or shares <= 0:
            return "sell"

        # Option 1: Sell now (V4 1.2: dynamic fee if schedule available)
        fee_schedule = None
        fs_json = position.get("fee_schedule_json")
        if fs_json:
            try:
                import json as _json
                fee_schedule = _json.loads(fs_json) if isinstance(fs_json, str) else fs_json
            except Exception:
                fee_schedule = None
        if fee_schedule and self._fees_cfg.get("use_dynamic_fees", True):
            sell_fee = calculate_taker_fee(current_price, shares, fee_schedule)
        else:
            sell_fee = current_price * shares * self._fee_rate
        sell_pnl = (current_price - entry_price) * shares - sell_fee

        # Option 2: Hold to resolution
        win_prob = current_price  # Market-implied probability
        win_pnl = (1.00 - entry_price) * shares  # No exit fee on resolution win
        loss_pnl = -entry_price * shares * 0.50  # Rough loss estimate
        hold_ev = win_prob * win_pnl + (1 - win_prob) * loss_pnl

        return "sell" if sell_pnl > hold_ev else "hold"

    # ─── V3: Fluke loss detection ──────────────────────────────────────────────

    def check_for_fluke_loss(self, position: Dict[str, Any], pnl: float) -> None:
        """
        V3: Called after a stop-loss exit. If this loss is >= 3× the trailing
        average loss (excluding this trade), flag as a fluke and pause the bucket.
        """
        avg_loss = self.portfolio.get_trailing_avg_loss(
            exclude_id=position.get("id"), lookback=10
        )
        if avg_loss == 0:
            return  # No prior losses to compare

        if abs(pnl) >= abs(avg_loss) * 3:
            bucket = position.get("risk_bucket", "unknown")
            logger.critical(
                f"FLUKE LOSS DETECTED: #{position.get('id')} lost ${abs(pnl):.2f} "
                f"(3× avg loss of ${abs(avg_loss):.2f}). "
                f"Pausing bucket '{bucket}' for 24h."
            )
            if self.notifier:
                try:
                    asyncio.create_task(
                        self.notifier.send_warning(
                            f"Outsized loss: #{position.get('id')} "
                            f"'{(position.get('market_question') or '')[:50]}' "
                            f"lost ${abs(pnl):.2f} (3× avg). "
                            f"Bucket '{bucket}' paused 24h for review.",
                            level="red",
                        )
                    )
                except RuntimeError:
                    pass
            if self.risk_engine:
                self.risk_engine.add_temporary_category_block(
                    bucket,
                    reason=f"Fluke loss: #{position.get('id')} lost ${abs(pnl):.2f} (3× avg)",
                    duration_hours=24,
                )

    # ─── Portfolio-level drawdown check ───────────────────────────────────────

    def check_portfolio_drawdown(
        self, open_positions: List[Dict[str, Any]]
    ) -> List[Tuple[Dict[str, Any], ExitDecision]]:
        """
        Portfolio-wide drawdown reduction:
        - At -3%: close the single worst-performing position
        - At -5%: close the bottom 3 positions
        Returns list of (position, decision) pairs.
        """
        drawdown = self.portfolio.get_portfolio_drawdown_pct(open_positions)

        if drawdown >= 0 or abs(drawdown) < self.portfolio_drawdown_alert_pct:
            return []

        if abs(drawdown) >= self.portfolio_drawdown_critical_pct:
            n_to_close = 3
            level = "CRITICAL"
        else:
            n_to_close = 1
            level = "ALERT"

        weakest = self.portfolio.get_weakest_positions(n_to_close, open_positions)
        if not weakest:
            return []

        logger.warning(
            f"PORTFOLIO DRAWDOWN {level}: {drawdown:.2%} — "
            f"closing {len(weakest)} weakest position(s)"
        )

        return [
            (
                pos,
                ExitDecision(
                    action="close_full", reason="drawdown_reduction",
                    close_pct=1.0, urgency="normal"
                ),
            )
            for pos in weakest
        ]

    # ─── Alert system (notification-only, does NOT trigger exits) ─────────────

    def _check_alerts(
        self, position: Dict[str, Any], current_price: float
    ) -> None:
        """
        Graduated alert notifications. Does not trigger exits — the stop-loss
        handles that at -7%. These alerts give the operator early warning.

        Deduplicates alerts: each (position_id, level) pair is only sent once
        per _alert_cooldown_seconds (default 1 hour).
        """
        entry_price = position.get("entry_price", 0)
        if entry_price <= 0:
            return

        loss_pct = (entry_price - current_price) / entry_price
        position_id = position.get("id", "unknown")
        now = time.time()

        alert_level = None
        if loss_pct >= self.orange_loss_pct:
            alert_level = "orange"
        elif loss_pct >= self.yellow_loss_pct:
            alert_level = "yellow"

        if alert_level and self.notifier:
            key = (position_id, alert_level)
            last_sent = self._alert_cooldowns.get(key, 0)
            if now - last_sent >= self._alert_cooldown_seconds:
                self._alert_cooldowns[key] = now
                asyncio.create_task(
                    self.notifier.send_position_alert(position, alert_level, current_price)
                )

    # ─── Price fetching ───────────────────────────────────────────────────────

    async def _get_current_price(
        self,
        position: Dict[str, Any],
        clob_client: Optional[Any] = None,
    ) -> Optional[float]:
        """Fetch current YES price via CLOB (preferred) or Gamma API fallback."""
        token_id = position.get("token_id", "")
        market_id = position.get("market_id", "")

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
                logger.debug(f"CLOB price fetch failed for {token_id}: {e}")

        if not market_id:
            return None

        try:
            session = await self._get_session()
            async with session.get(f"{GAMMA_API_BASE}/markets/{market_id}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    outcome_prices = safe_json_parse(data.get("outcomePrices"))
                    if outcome_prices and len(outcome_prices) >= 1:
                        return float(outcome_prices[0])
                    last = data.get("lastTradePrice") or data.get("price")
                    if last:
                        return float(last)
        except Exception as e:
            logger.debug(f"Gamma price fetch failed for {market_id}: {e}")

        return None
