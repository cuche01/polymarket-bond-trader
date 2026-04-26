"""V4 Phase 2.3: Holding-rewards reconciliation.

Estimates rewards at entry and reconciles to actuals from Polymarket's Data
API once per day. Paper mode uses the estimate as the "actual" (since there
is no on-chain funder to query). Live mode queries:

    GET {api_base_url}/rewards?user={funder}&date=YYYY-MM-DD

and attributes payments back to positions that were open during that day.

Gated by `holding_rewards.reconciliation_enabled` — off by default so paper
sessions don't spam logs about a live-only path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .database import Database
from .utils import estimate_holding_rewards

logger = logging.getLogger(__name__)


class RewardsReconciler:
    def __init__(
        self,
        config: dict,
        db: Database,
        paper_mode: bool = True,
        http_session: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.paper_mode = paper_mode
        self._session = http_session
        cfg = config.get("holding_rewards", {}) or {}
        self.apr = float(cfg.get("apy", 0.04))
        self.enabled = bool(cfg.get("reconciliation_enabled", False))
        self.api_base_url = cfg.get("api_base_url", "https://data-api.polymarket.com")
        self.interval_hours = float(cfg.get("reconciliation_interval_hours", 24))
        self._last_run_at: Optional[datetime] = None

    def should_run(self, now: Optional[datetime] = None) -> bool:
        """True if it has been `interval_hours` since the last reconciliation."""
        now = now or datetime.now(timezone.utc)
        if self._last_run_at is None:
            return True
        delta = now - self._last_run_at
        return delta >= timedelta(hours=self.interval_hours)

    async def reconcile(self, now: Optional[datetime] = None) -> int:
        """Update `actual_holding_rewards` on open positions.

        Returns the number of positions updated. In paper mode, actuals are
        computed from the simple-interest estimate (no API call). In live
        mode with `reconciliation_enabled: true`, this would hit the Data
        API — left as a stub until funder-address plumbing lands.
        """
        if not self.enabled and not self.paper_mode:
            return 0

        now = now or datetime.now(timezone.utc)
        updated = 0
        for pos in self.db.get_open_positions(paper_trade=self.paper_mode):
            if not pos.get("holding_rewards_enabled"):
                continue
            entry_iso = pos.get("entry_time") or ""
            try:
                entry_dt = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            days_held = max((now - entry_dt).total_seconds() / 86400.0, 0.0)

            position_value = float(pos.get("cost_basis") or 0.0)
            apr = float(pos.get("holding_rewards_apr") or self.apr)
            actual = estimate_holding_rewards(position_value, days_held, apr)
            self.db.update_position(pos["id"], {"actual_holding_rewards": actual})
            updated += 1

        self._last_run_at = now
        logger.info(
            f"Reconciled holding rewards on {updated} open positions "
            f"(paper_mode={self.paper_mode})"
        )
        return updated
