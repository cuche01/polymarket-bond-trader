"""
Blacklist Learning Loop (P2)

After each losing trade, extracts features (category, risk_bucket, keyword bigrams)
and tracks them in a learning table. When a feature's loss count exceeds a threshold
within a rolling window, it applies a penalty multiplier to the bond score for markets
sharing that feature.

This does NOT auto-reject markets — it feeds into the bond score calculation
as a penalty multiplier, allowing the operator to review and promote entries
to the hard blacklist if warranted.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class BlacklistLearner:
    """Tracks losing trade features and computes penalty multipliers."""

    def __init__(self, database, config: dict):
        self.db = database
        self.config = config
        bl_cfg = config.get("blacklist_learner", {})
        self.enabled = bl_cfg.get("enabled", False)
        self.loss_threshold = bl_cfg.get("loss_threshold", 3)
        self.window_days = bl_cfg.get("window_days", 30)

    def record_loss(self, position: Dict) -> None:
        """Record loss features for learning after a losing trade."""
        if not self.enabled:
            return

        features = self._extract_features(position)
        market_id = position.get("market_id", "")
        pnl = position.get("pnl", 0)

        for feature_type, feature_value in features:
            try:
                self.db.execute_write(
                    """INSERT INTO blacklist_learning
                       (feature_type, feature_value, loss_time, market_id, pnl)
                       VALUES (?, ?, datetime('now'), ?, ?)""",
                    (feature_type, feature_value, market_id, pnl),
                )
            except Exception as e:
                logger.debug(f"Failed to record blacklist learning feature: {e}")

    def get_penalty(self, market: Dict) -> float:
        """
        Check if any features of this market have accumulated losses.
        Returns a penalty multiplier (1.0 = no penalty, 0.4 = max penalty).
        """
        if not self.enabled:
            return 1.0

        features = self._extract_features_from_market(market)
        max_penalty = 1.0

        for feature_type, feature_value in features:
            try:
                count = self.db.execute_read(
                    """SELECT COUNT(*) as cnt FROM blacklist_learning
                       WHERE feature_type = ? AND feature_value = ?
                       AND loss_time >= datetime('now', ?)""",
                    (feature_type, feature_value, f"-{self.window_days} days"),
                )
                if count and count[0]:
                    cnt = count[0].get("cnt", 0) if isinstance(count[0], dict) else count[0][0]
                    if cnt >= self.loss_threshold:
                        # Escalating penalty: 0.7 at threshold, 0.4 at 2x threshold
                        penalty = max(0.4, 1.0 - (cnt / self.loss_threshold) * 0.3)
                        max_penalty = min(max_penalty, penalty)
            except Exception as e:
                logger.debug(f"Blacklist learner penalty lookup failed: {e}")

        return max_penalty

    def _extract_features(self, position: Dict) -> List[Tuple[str, str]]:
        """Extract features from a closed position."""
        features = []
        if position.get("category"):
            features.append(("category", position["category"]))
        if position.get("risk_bucket"):
            features.append(("risk_bucket", position["risk_bucket"]))
        # Extract 2-word bigrams from market question
        question = position.get("market_question", "")
        words = re.findall(r"\b[a-z]{3,}\b", question.lower())
        for i in range(len(words) - 1):
            features.append(("keyword_bigram", f"{words[i]}_{words[i+1]}"))
        return features

    def _extract_features_from_market(self, market: Dict) -> List[Tuple[str, str]]:
        """Extract features from a candidate market (pre-entry)."""
        features = []
        category = market.get("category") or market.get("marketType") or ""
        if category:
            features.append(("category", category))
        risk_bucket = market.get("_risk_bucket") or market.get("risk_bucket") or ""
        if risk_bucket:
            features.append(("risk_bucket", risk_bucket))
        question = market.get("question") or market.get("title") or ""
        words = re.findall(r"\b[a-z]{3,}\b", question.lower())
        for i in range(len(words) - 1):
            features.append(("keyword_bigram", f"{words[i]}_{words[i+1]}"))
        return features
