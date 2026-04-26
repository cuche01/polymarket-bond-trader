"""
V4 1.1: Pipeline Health monitor.

Records per-scan funnel metrics (candidates fetched -> prefilter -> detector ->
risk engine -> entries) and detects starvation conditions. Per V4 §0.1, this
must be installed before any other V4 change — it is the guardrail against
filter-induced pipeline starvation.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PipelineHealth:
    """
    Tracks scan-funnel health and exposes starvation detection.

    severity codes:
      OK          — recent entries or entries too few to worry
      WARNING     — dry period exceeded warning_dry_period_hours
      CRITICAL    — dry period exceeded critical_dry_period_hours
      STARVATION  — dry period exceeded starvation_dry_period_hours; auto-relax
                    protocol should be considered (operator opt-in)
    """

    def __init__(self, config: dict, db):
        health_cfg = config.get("pipeline_health", {}) or {}
        self.enabled = health_cfg.get("enabled", True)
        self.warning_hours = health_cfg.get("warning_dry_period_hours", 12)
        self.critical_hours = health_cfg.get("critical_dry_period_hours", 18)
        self.starvation_hours = health_cfg.get("starvation_dry_period_hours", 36)
        self.min_acceptance_rate_24h = health_cfg.get("min_acceptance_rate_24h", 0.005)
        self.auto_relax_cfg = health_cfg.get("auto_relaxation", {}) or {}
        self.db = db

    def record_scan(self, metrics: Dict) -> None:
        """Persist a single scan's funnel metrics to pipeline_health."""
        if not self.enabled:
            return
        rejection_reasons = metrics.get("rejection_reasons") or {}
        row = (
            metrics.get("scan_time") or datetime.now(timezone.utc).isoformat(),
            int(metrics.get("candidates_fetched", 0)),
            int(metrics.get("candidates_passed_prefilter", 0)),
            int(metrics.get("candidates_passed_detector", 0)),
            int(metrics.get("candidates_passed_risk_engine", 0)),
            int(metrics.get("entries_executed", 0)),
            json.dumps(rejection_reasons),
            metrics.get("mode", "paper"),
        )
        self.db.execute_write(
            "INSERT INTO pipeline_health "
            "(scan_time, candidates_fetched, candidates_passed_prefilter, "
            " candidates_passed_detector, candidates_passed_risk_engine, "
            " entries_executed, rejection_reasons_json, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )

    def get_acceptance_rate(self, hours: int = 24) -> float:
        """entries_executed / candidates_fetched over the last N hours."""
        rows = self.db.execute_read(
            "SELECT SUM(candidates_fetched) AS fetched, SUM(entries_executed) AS entries "
            "FROM pipeline_health "
            "WHERE scan_time >= datetime('now', ?)",
            (f"-{int(hours)} hours",),
        )
        if not rows or not rows[0].get("fetched"):
            return 0.0
        fetched = rows[0]["fetched"] or 0
        entries = rows[0]["entries"] or 0
        if fetched == 0:
            return 0.0
        return entries / fetched

    def get_dry_period_hours(self) -> float:
        """Hours since the most recent entry, across all modes."""
        rows = self.db.execute_read(
            "SELECT scan_time FROM pipeline_health "
            "WHERE entries_executed > 0 "
            "ORDER BY id DESC LIMIT 1"
        )
        if not rows:
            rows = self.db.execute_read(
                "SELECT scan_time FROM pipeline_health ORDER BY id ASC LIMIT 1"
            )
            if not rows:
                return 0.0
        last = datetime.fromisoformat(rows[0]["scan_time"])
        now = datetime.now(timezone.utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        delta = now - last
        return delta.total_seconds() / 3600.0

    def get_top_rejection_reasons(
        self, limit: int = 5, hours: int = 24
    ) -> List[Tuple[str, int]]:
        """Aggregate rejection reason counts over the window."""
        rows = self.db.execute_read(
            "SELECT rejection_reasons_json FROM pipeline_health "
            "WHERE scan_time >= datetime('now', ?)",
            (f"-{int(hours)} hours",),
        )
        tally: Dict[str, int] = {}
        for row in rows:
            raw = row.get("rejection_reasons_json")
            if not raw:
                continue
            try:
                reasons = json.loads(raw)
            except Exception:
                continue
            for k, v in reasons.items():
                tally[k] = tally.get(k, 0) + int(v)
        ordered = sorted(tally.items(), key=lambda x: x[1], reverse=True)
        return ordered[:limit]

    def check_starvation(self) -> Tuple[str, Optional[str]]:
        """
        Returns (severity, action).

        severity ∈ {OK, WARNING, CRITICAL, STARVATION}
        action ∈ {None, 'alert', 'auto_relax'}
        """
        if not self.enabled:
            return ("OK", None)
        dry = self.get_dry_period_hours()
        if dry >= self.starvation_hours:
            auto = bool(self.auto_relax_cfg.get("enabled", False))
            return ("STARVATION", "auto_relax" if auto else "alert")
        if dry >= self.critical_hours:
            return ("CRITICAL", "alert")
        if dry >= self.warning_hours:
            return ("WARNING", "alert")
        return ("OK", None)

    def get_24h_summary(self) -> Dict:
        """Summary used by dashboard and daily Discord report."""
        rows = self.db.execute_read(
            "SELECT COUNT(*) AS scans, "
            "       SUM(candidates_fetched) AS fetched, "
            "       SUM(candidates_passed_prefilter) AS prefilter, "
            "       SUM(candidates_passed_detector) AS detector, "
            "       SUM(candidates_passed_risk_engine) AS risk, "
            "       SUM(entries_executed) AS entries "
            "FROM pipeline_health "
            "WHERE scan_time >= datetime('now', '-24 hours')"
        )
        r = rows[0] if rows else {}
        fetched = r.get("fetched") or 0
        entries = r.get("entries") or 0
        return {
            "scans": r.get("scans") or 0,
            "fetched": fetched,
            "prefilter": r.get("prefilter") or 0,
            "detector": r.get("detector") or 0,
            "risk": r.get("risk") or 0,
            "entries": entries,
            "acceptance_rate": (entries / fetched) if fetched else 0.0,
            "dry_period_hours": self.get_dry_period_hours(),
            "top_rejections": self.get_top_rejection_reasons(),
        }
