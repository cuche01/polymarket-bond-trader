"""
Pseudo-certainty detector for validating market opportunities through multiple layers.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore

from .risk_buckets import RiskBucketClassifier
from .utils import (
    estimate_round_trip_fee_rate,
    feature_enabled,
    get_days_to_resolution,
    resolve_fee_schedule,
    safe_json_parse,
    shadow_enabled,
)

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


class PseudoCertaintyDetector:
    """
    Multi-layer validator for pseudo-certainty market opportunities.

    Layers:
    1. Category exclusion
    2. Price behavior analysis
    3. Orderbook health check
    4. Resolution source check
    5. Blacklist check
    """

    def __init__(self, config: dict):
        """
        Initialize detector with configuration.

        Args:
            config: Full configuration dictionary
        """
        self.config = config
        scanner_cfg = config.get("scanner", {})
        self.excluded_categories = scanner_cfg.get("excluded_categories", [])
        self.max_volatility = scanner_cfg.get("max_price_volatility_1d", 0.03)
        self.min_entry_price = scanner_cfg.get("min_entry_price", 0.95)
        self.max_entry_price = scanner_cfg.get("max_entry_price", 0.99)

        orderbook_cfg = config.get("orderbook", {})
        self.min_bid_depth_multiplier = orderbook_cfg.get("min_bid_depth_multiplier", 5)
        self.max_spread = orderbook_cfg.get("max_spread", 0.03)
        self.max_bid_volume_decline = orderbook_cfg.get("max_bid_volume_decline_pct", 0.30)

        risk_cfg = config.get("risk", {})
        self.min_net_yield = risk_cfg.get("min_net_yield", 0.01)
        # V4 Phase 2.1: Category-specific min net yield map.
        self.min_net_yield_by_category = risk_cfg.get("min_net_yield_by_category", {}) or {}

        # Live-event gate: reject sports markets that resolve within this window
        # to prevent in-play entries (which are exposed to unrecoverable gap-downs).
        self.sports_min_days_to_resolution = scanner_cfg.get(
            "sports_min_days_to_resolution", 0.25
        )
        self._bucket_classifier = RiskBucketClassifier(config.get("risk_buckets"))

        # P1: Binary catalyst configuration
        catalyst_cfg = config.get("binary_catalyst", {})
        self.binary_reject_threshold = catalyst_cfg.get("binary_catalyst_reject_threshold", 0.85)
        self.binary_penalize_threshold = catalyst_cfg.get("binary_catalyst_penalize_threshold", 0.50)
        self.binary_penalty_factor = catalyst_cfg.get("binary_catalyst_penalty_factor", 0.60)

        # P1: Price drop cool-down
        detector_cfg = config.get("detector", {})
        self.price_drop_cooldown_threshold = detector_cfg.get("price_drop_cooldown_threshold", -0.03)
        self.price_drop_recovery_ratio = detector_cfg.get("price_drop_recovery_ratio", 0.80)

        # P2: YES+NO parity
        self.min_parity_sum = detector_cfg.get("min_parity_sum", 0.96)
        self.max_parity_sum = detector_cfg.get("max_parity_sum", 1.04)

        # Load blacklist
        self._blacklist = self._load_blacklist()
        self._session: Optional[aiohttp.ClientSession] = None

    def _load_blacklist(self) -> Dict:
        """Load blacklist from data/blacklist.json."""
        blacklist_path = Path("data/blacklist.json")
        if not blacklist_path.exists():
            logger.warning("Blacklist file not found, using empty blacklist")
            return {"market_ids": [], "slugs": [], "keyword_patterns": [], "categories": []}
        try:
            with open(blacklist_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load blacklist: {e}")
            return {"market_ids": [], "slugs": [], "keyword_patterns": [], "categories": []}

    def _resolve_category_min_yield(self, category: str) -> Optional[float]:
        """V4 Phase 2.1: look up the category-specific min net yield.

        Returns the category override, the `_unknown` default, or None when no
        map is configured. Overrides are clamped at `min_net_yield - 0.005` —
        a hard floor to prevent misconfiguration from accepting junk yields.
        """
        if not self.min_net_yield_by_category:
            return None
        value = self.min_net_yield_by_category.get(category)
        if value is None:
            value = self.min_net_yield_by_category.get("_unknown")
        if value is None:
            return None
        hard_floor = self.min_net_yield - 0.005
        return max(float(value), hard_floor)

    async def _get_session(self):
        """Get or create HTTP session."""
        if aiohttp is None:
            raise ImportError("aiohttp is required for HTTP requests. Install it with: pip install aiohttp")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def close(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _check_category_exclusion(self, market: Dict) -> Tuple[bool, str]:
        """
        Layer 1: Check if market category is excluded.

        Args:
            market: Market dictionary

        Returns:
            (passed, reason) tuple
        """
        category = market.get("category") or market.get("marketType") or ""
        question = market.get("question") or market.get("title") or ""
        slug = market.get("slug") or market.get("conditionId") or ""

        # Polymarket sports markets often have no category field but always
        # carry sportsMarketType and/or a sports feeType.  Reject them here
        # so they never reach deeper (slower) validation layers.
        sports_market_type = market.get("sportsMarketType") or ""
        fee_type = market.get("feeType") or ""
        if sports_market_type:
            return False, f"Sports market detected via sportsMarketType: {sports_market_type}"
        if "sports" in fee_type.lower():
            return False, f"Sports market detected via feeType: {fee_type}"

        # Check against config excluded categories
        for excl in self.excluded_categories:
            if excl.lower() in category.lower():
                return False, f"Excluded category: {category}"

        # Check against blacklist categories
        for bl_cat in self._blacklist.get("categories", []):
            if bl_cat.lower() in category.lower():
                return False, f"Blacklisted category: {bl_cat}"

        # Check keyword patterns in question/slug
        for pattern in self._blacklist.get("keyword_patterns", []):
            if pattern.lower() in question.lower() or pattern.lower() in slug.lower():
                return False, f"Blacklisted keyword pattern: {pattern}"

        return True, "Category OK"

    def _check_price_behavior(self, market: Dict) -> Tuple[bool, str]:
        """
        Layer 2: Analyze price behavior for stability.

        Args:
            market: Market dictionary

        Returns:
            (passed, reason) tuple
        """
        yes_price = market.get("_yes_price") or float(
            safe_json_parse(market.get("outcomePrices"), [0])[0] or 0
        )

        # Check price is in bond range
        if not yes_price or yes_price < self.min_entry_price or yes_price > self.max_entry_price:
            return False, f"Price {yes_price:.4f} outside bond range [{self.min_entry_price}, {self.max_entry_price}]"

        # Check 1-day price change
        price_change_1d = abs(float(market.get("oneDayPriceChange") or 0))
        if price_change_1d > self.max_volatility:
            return False, f"1-day price change {price_change_1d:.4f} exceeds max {self.max_volatility}"

        # Check 1-week price change if available
        price_change_1w = abs(float(market.get("oneWeekPriceChange") or 0))
        if price_change_1w > 0.15:  # 15% weekly change is concerning
            return False, f"1-week price change {price_change_1w:.4f} exceeds 10% threshold"

        # Ensure price hasn't recently spiked from lower level
        # (indicates manipulation or fake certainty)
        best_ask = float(market.get("bestAsk") or yes_price)
        best_bid = float(market.get("bestBid") or yes_price)

        # Large spread is a red flag
        spread = best_ask - best_bid
        if spread > self.max_spread:
            return False, f"Spread {spread:.4f} exceeds max {self.max_spread}"

        # Check volume makes sense for liquidity
        volume_24h = float(market.get("volume24hr") or 0)
        liquidity = float(market.get("liquidityClob") or market.get("liquidity") or 0)
        if volume_24h > 0 and liquidity > 0:
            volume_to_liquidity = volume_24h / liquidity
            if volume_to_liquidity > 10:  # Volume 10x liquidity is suspicious
                return False, f"Suspicious volume/liquidity ratio: {volume_to_liquidity:.2f}"

        return True, "Price behavior stable"

    async def _check_orderbook_health(
        self,
        market: Dict,
        clob_client: Any,
        position_size: float = 1000.0,
    ) -> Tuple[bool, str]:
        """
        Layer 3: Validate orderbook depth and quality.

        Args:
            market: Market dictionary
            clob_client: CLOB client instance
            position_size: Intended position size in USD

        Returns:
            (passed, reason) tuple
        """
        yes_token_id = market.get("_yes_token_id")
        if not yes_token_id:
            token_ids = safe_json_parse(market.get("clobTokenIds"), [])
            if token_ids and len(token_ids) > 0:
                yes_token_id = str(token_ids[0])
            else:
                return False, "No YES token ID available"

        yes_price = market.get("_yes_price", 0.0)
        if yes_price <= 0:
            return False, "Invalid YES price"

        # Try to get orderbook from CLOB client
        if clob_client is None:
            # Paper mode or no client: fall back to market-level liquidity
            liquidity = float(market.get("liquidityClob") or market.get("liquidity") or 0)
            min_required = position_size * self.min_bid_depth_multiplier
            if liquidity < min_required:
                return (
                    False,
                    f"Insufficient market liquidity: ${liquidity:,.0f} < ${min_required:,.0f}",
                )
            return True, f"Liquidity check passed (no CLOB client): ${liquidity:,.0f}"

        try:
            orderbook = clob_client.get_order_book(yes_token_id)

            if not orderbook:
                return False, "No orderbook data available"

            bids = getattr(orderbook, "bids", []) or []
            asks = getattr(orderbook, "asks", []) or []

            if not bids:
                return False, "No bids in orderbook"
            if not asks:
                return False, "No asks in orderbook"

            # Calculate bid-side depth (USD)
            total_bid_size_usd = sum(
                float(getattr(b, "price", 0)) * float(getattr(b, "size", 0))
                for b in bids
            )

            # Ensure enough bid depth to exit our position
            min_required_depth = position_size * self.min_bid_depth_multiplier
            if total_bid_size_usd < min_required_depth:
                return (
                    False,
                    f"Insufficient bid depth: ${total_bid_size_usd:,.0f} < "
                    f"${min_required_depth:,.0f} required",
                )

            # Check spread
            best_bid = float(getattr(bids[0], "price", 0))
            best_ask = float(getattr(asks[0], "price", 0))
            if best_bid > 0 and best_ask > 0:
                spread = best_ask - best_bid
                if spread > self.max_spread:
                    return False, f"CLOB spread {spread:.4f} exceeds max {self.max_spread}"

            return True, f"Orderbook healthy: ${total_bid_size_usd:,.0f} bid depth"

        except Exception as e:
            logger.warning(f"Orderbook check failed for {yes_token_id}: {e}")
            # Fall back to market-level liquidity data
            liquidity = float(market.get("liquidityClob") or market.get("liquidity") or 0)
            min_required = position_size * self.min_bid_depth_multiplier
            if liquidity < min_required:
                return (
                    False,
                    f"Insufficient market liquidity: ${liquidity:,.0f} < ${min_required:,.0f}",
                )
            return True, f"Liquidity check passed (REST fallback): ${liquidity:,.0f}"

    def _check_resolution_source(self, market: Dict) -> Tuple[bool, str]:
        """
        Layer 4: Check that resolution source is objective and reliable.

        Args:
            market: Market dictionary

        Returns:
            (passed, reason) tuple
        """
        question = (market.get("question") or market.get("title") or "").lower()
        description = (market.get("description") or "").lower()

        # Red flags: subjective or hard-to-verify resolution criteria
        subjective_patterns = [
            "will the community",
            "popular opinion",
            "twitter poll",
            "social media",
            "based on user votes",
        ]
        for pattern in subjective_patterns:
            if pattern in question or pattern in description:
                return False, f"Subjective resolution criteria: '{pattern}'"

        # Check for clear resolution criteria
        # Markets with these typically resolve objectively
        objective_patterns = [
            "officially announced",
            "according to",
            "as reported by",
            "fda",
            "government",
            "officially",
            "confirmed",
            "election results",
            "final score",
            "closing price",
        ]

        # Check for unresolvable edge cases
        risky_patterns = [
            "within 24 hours",
            "next tweet",
            "before midnight",
        ]
        for pattern in risky_patterns:
            if pattern in question:
                return False, f"Risky time-dependent resolution: '{pattern}'"

        # Check end date is in the future
        end_date = market.get("endDate") or market.get("end_date_iso")
        if not end_date:
            return False, "No end date specified"

        days = get_days_to_resolution(end_date)
        if days <= 0:
            return False, f"Market already past end date ({days:.2f} days)"

        # Live-event gate: sports markets resolving within sports_min_days_to_resolution
        # are almost always in-play, where a single play can gap price down 20-30%
        # past the stop-loss in under a minute. Reject them pre-entry.
        category = market.get("category") or market.get("marketType") or ""
        bucket = self._bucket_classifier.classify(category, question)
        if bucket == "sports" and days < self.sports_min_days_to_resolution:
            return False, (
                f"Live-event gate: sports market resolves in {days:.2f}d "
                f"(< {self.sports_min_days_to_resolution}d) — gap-down risk"
            )

        return True, "Resolution source appears objective"

    # ── P1: Binary Catalyst Classifier (Layer 4.5) ───────────────────────────

    # V3: Commodity/stock direction patterns — apply bond score penalty
    DIRECTION_PENALTY_PATTERNS = [
        "close above", "close below",
        "settle above", "settle below",
        "finish above", "finish below",
    ]
    DIRECTION_PENALTY_FACTOR = 0.3  # 0.3× bond score for direction bets

    BINARY_CATALYST_PATTERNS = [
        # Court / Legal decisions
        r"\b(rul(?:e|ing)|verdict|decision|dismiss|overturn|uphold|acquit|convict|indict)\b",
        r"\b(supreme court|scotus|circuit court|judge|jury)\b",
        # Regulatory / Government actions
        r"\b(approv(?:e|al)|reject|sign(?:s|ed)?(?:\s+into\s+law)?|veto|executive order)\b",
        r"\b(fda|sec|ftc|epa|fcc)\s+(?:approv|reject|rul|decision)\b",
        # Election / Vote results
        r"\b(win(?:s)?|elect(?:ed)?|vote|ballot|runoff|primary)\b",
        r"\b(inaugurat|certif(?:y|ied|ication))\b",
        # Single-event sports
        r"\b(game\s*\d|match|bout|fight|race)\b",
        # Announcements / Releases
        r"\b(announce|reveal|launch|release|unveil|drop(?:s|ped)?)\b",
        r"\b(earnings|quarterly results|q[1-4]\s+report)\b",
        # Social media / Individual actions
        r"\b(tweet|post(?:s|ed)?|say(?:s)?|said|comment(?:s|ed)?|respond)\b",
        r"\b(resign|fire[ds]?|hire[ds]?|appoint(?:ed)?|step(?:s)?\s*down)\b",
    ]

    CONTINUOUS_DECAY_PATTERNS = [
        # Price thresholds over time
        r"\b(stay|remain|hold|maintain|keep)\s+(above|below|at|within)\b",
        r"\b(hit|reach|exceed|break|cross)\s+\$?\d+.*\b(by|before|in|during|through)\b",
        # Cumulative / Statistical
        r"\b(total|cumulative|average|median)\b",
        r"\b(more|fewer|at least|less than)\s+\d+.*\b(by|before|in)\b",
        # Duration / Sustained conditions
        r"\b(for\s+\d+\s+(?:day|week|month|consecutive))\b",
        r"\b(throughout|during|over the course of)\b",
    ]

    def _classify_catalyst_type(self, market_question: str, description: str = "") -> Dict:
        """
        P1: Classify a market's resolution catalyst as binary or continuous.

        Returns dict with catalyst_type, binary_score, recommendation.
        """
        text = f"{market_question} {description}".lower()

        binary_matches = [p for p in self.BINARY_CATALYST_PATTERNS if re.search(p, text, re.IGNORECASE)]
        continuous_matches = [p for p in self.CONTINUOUS_DECAY_PATTERNS if re.search(p, text, re.IGNORECASE)]

        total = len(binary_matches) + len(continuous_matches)
        if total == 0:
            binary_score = 0.6  # Unknown — slight binary lean (conservative)
        else:
            binary_score = len(binary_matches) / total

        if binary_score >= self.binary_reject_threshold:
            recommendation = "reject"
        elif binary_score >= self.binary_penalize_threshold:
            recommendation = "penalize"
        else:
            recommendation = "allow"

        catalyst_type = (
            "binary" if binary_score >= 0.7
            else "continuous" if binary_score <= 0.3
            else "mixed"
        )

        return {
            "catalyst_type": catalyst_type,
            "binary_score": binary_score,
            "matched_binary": len(binary_matches),
            "matched_continuous": len(continuous_matches),
            "recommendation": recommendation,
        }

    def _check_binary_catalyst(self, market: Dict) -> Tuple[bool, str]:
        """
        Layer 4.5: Binary Catalyst Filter.

        - reject:   binary_score >= 0.85 (pure binary catalyst)
        - penalize: binary_score >= 0.50 (mixed — reduce bond score)
        - allow:    binary_score < 0.50 (continuous decay — safe for bonds)
        """
        question = market.get("question") or market.get("title") or ""
        description = market.get("description") or ""

        result = self._classify_catalyst_type(question, description)

        if result["recommendation"] == "reject":
            return False, (
                f"Binary catalyst rejected (score={result['binary_score']:.2f}, "
                f"binary={result['matched_binary']}, continuous={result['matched_continuous']})"
            )

        if result["recommendation"] == "penalize":
            market["_catalyst_penalty"] = self.binary_penalty_factor
        else:
            market["_catalyst_penalty"] = 1.0

        market["_catalyst_type"] = result["catalyst_type"]
        market["_binary_catalyst_score"] = result["binary_score"]

        return True, f"Catalyst: {result['catalyst_type']} (score={result['binary_score']:.2f})"

    # ── P1: Price Drop Cool-Down ─────────────────────────────────────────────

    def _check_price_drop_cooldown(self, market: Dict) -> Tuple[bool, str]:
        """
        P1: If the market's price has dropped > threshold in the last hour, reject.
        Uses the 1-day price change as a proxy when hourly data is unavailable.
        """
        price_change = float(market.get("oneDayPriceChange") or 0)

        if price_change < self.price_drop_cooldown_threshold:
            return False, (
                f"Price drop cool-down: {price_change:.1%} recent decline "
                f"(threshold {self.price_drop_cooldown_threshold:.1%})"
            )

        return True, "No recent price drop"

    # ── P2: YES+NO Parity Check ──────────────────────────────────────────────

    def _check_yes_no_parity(self, market: Dict) -> Tuple[bool, str]:
        """
        P2: Validate that YES + NO prices sum close to $1.00.
        Deviation suggests stale orderbook, imminent dispute, or manipulation.
        """
        yes_price = market.get("_yes_price") or 0
        no_price = market.get("_no_price") or 0

        if no_price <= 0:
            return True, "No NO-side price data; parity check skipped"

        parity = yes_price + no_price
        if parity < self.min_parity_sum or parity > self.max_parity_sum:
            return False, (
                f"YES+NO parity violation: {yes_price:.3f} + {no_price:.3f} = {parity:.3f} "
                f"(expected {self.min_parity_sum:.2f}–{self.max_parity_sum:.2f})"
            )

        return True, f"Parity OK: {parity:.3f}"

    def _apply_direction_penalty(self, market: Dict) -> None:
        """
        V3: Apply bond score penalty for commodity/stock direction markets.
        "Close above $X" or "finish below $Y" are coin-flip bets masquerading
        as bonds. Penalizes bond score by 0.3× so they are deprioritized.
        """
        question = (market.get("question") or market.get("title") or "").lower()
        for pattern in self.DIRECTION_PENALTY_PATTERNS:
            if pattern in question:
                existing = market.get("_catalyst_penalty", 1.0)
                market["_catalyst_penalty"] = existing * self.DIRECTION_PENALTY_FACTOR
                logger.info(
                    f"Direction penalty applied: '{pattern}' in "
                    f"'{market.get('question', '')[:50]}' → 0.3× score"
                )
                return

    def _check_blacklist(self, market: Dict) -> Tuple[bool, str]:
        """
        Layer 5: Check if market is in the blacklist.

        Args:
            market: Market dictionary

        Returns:
            (passed, reason) tuple
        """
        market_id = market.get("id") or market.get("conditionId") or ""
        slug = market.get("slug") or ""
        question = market.get("question") or market.get("title") or ""

        # Check explicit market IDs
        if market_id in self._blacklist.get("market_ids", []):
            return False, f"Market ID {market_id} is blacklisted"

        # Check slugs
        if slug and slug in self._blacklist.get("slugs", []):
            return False, f"Market slug '{slug}' is blacklisted"

        # Already checked keyword_patterns in Layer 1, but double-check here
        for pattern in self._blacklist.get("keyword_patterns", []):
            if pattern.lower() in question.lower():
                return False, f"Blacklisted keyword in question: '{pattern}'"

        return True, "Not blacklisted"

    async def is_valid_opportunity(
        self,
        market: Dict,
        clob_client: Any,
        position_size: float = 1000.0,
    ) -> Tuple[bool, str]:
        """
        Run all validation layers on a market opportunity.

        Args:
            market: Market dictionary from Gamma API
            clob_client: Authenticated CLOB client
            position_size: Intended position size for orderbook check

        Returns:
            (is_valid, reason) tuple
        """
        # Layer 1: Category exclusion
        passed, reason = self._check_category_exclusion(market)
        if not passed:
            logger.debug(f"Layer 1 rejected: {reason}")
            return False, f"[L1] {reason}"

        # Layer 2: Price behavior
        passed, reason = self._check_price_behavior(market)
        if not passed:
            logger.debug(f"Layer 2 rejected: {reason}")
            return False, f"[L2] {reason}"

        # Layer 2.5: Price drop cool-down (P1)
        if feature_enabled(self.config, "price_drop_cooldown"):
            passed, reason = self._check_price_drop_cooldown(market)
            if not passed:
                logger.debug(f"Layer 2.5 rejected: {reason}")
                return False, f"[L2.5] {reason}"

        # Layer 2.6: YES+NO parity check (P2)
        if feature_enabled(self.config, "secondary_price_validation"):
            passed, reason = self._check_yes_no_parity(market)
            if not passed:
                logger.debug(f"Layer 2.6 rejected: {reason}")
                return False, f"[L2.6] {reason}"

        # Layer 3: Orderbook health
        passed, reason = await self._check_orderbook_health(market, clob_client, position_size)
        if not passed:
            logger.debug(f"Layer 3 rejected: {reason}")
            return False, f"[L3] {reason}"

        # Layer 4: Resolution source
        passed, reason = self._check_resolution_source(market)
        if not passed:
            logger.debug(f"Layer 4 rejected: {reason}")
            return False, f"[L4] {reason}"

        # Layer 4.5: Binary catalyst filter (P1)
        if feature_enabled(self.config, "binary_catalyst_filter"):
            passed, reason = self._check_binary_catalyst(market)
            if not passed:
                logger.debug(f"Layer 4.5 rejected: {reason}")
                return False, f"[L4.5] {reason}"

        # Layer 4.6: Direction-bet penalty (V3)
        self._apply_direction_penalty(market)

        # Layer 5: Blacklist
        passed, reason = self._check_blacklist(market)
        if not passed:
            logger.debug(f"Layer 5 rejected: {reason}")
            return False, f"[L5] {reason}"

        # Final yield check (V4 1.2: dynamic fees from feeSchedule)
        yes_price = market.get("_yes_price", 0)
        if yes_price > 0:
            gross_yield = (1.0 - yes_price) / yes_price
            fees_cfg = self.config.get("fees", {}) or {}
            fee_schedule = resolve_fee_schedule(market, fees_cfg)
            market["_fee_schedule"] = fee_schedule
            round_trip_fee = estimate_round_trip_fee_rate(
                entry_price=yes_price,
                exit_price=None,
                fee_schedule=fee_schedule,
                entry_is_maker=bool(fees_cfg.get("assume_entry_maker", True)),
                exit_is_taker=bool(fees_cfg.get("assume_exit_taker", True)),
                exit_is_resolution=False,  # conservative: assume early taker exit
            )
            net_yield = gross_yield - round_trip_fee
            market["_estimated_round_trip_fee_rate"] = round_trip_fee

            # V4 Phase 2.1: Category-specific min net yield.
            category = market.get("category") or market.get("marketType") or "_unknown"
            category_min = self._resolve_category_min_yield(category)
            enforce_category = feature_enabled(self.config, "category_min_yield")
            shadow_category = shadow_enabled(self.config, "category_min_yield")

            # Primary (global) gate always enforces.
            if net_yield < self.min_net_yield:
                return False, (
                    f"Net yield {net_yield:.4f} below minimum {self.min_net_yield} "
                    f"(fee_rate={round_trip_fee:.4f})"
                )

            # Category-specific gate: enforce only when the real flag is on.
            if category_min is not None and net_yield < category_min:
                if enforce_category:
                    return False, (
                        f"Net yield {net_yield:.4f} below {category} minimum "
                        f"{category_min:.4f} (fee_rate={round_trip_fee:.4f})"
                    )
                if shadow_category:
                    detail = (
                        f"category={category} min={category_min:.4f} "
                        f"actual={net_yield:.4f}"
                    )
                    market["_shadow_reject_category_min_yield"] = detail
                    logger.info(f"[SHADOW category_min_yield] would reject: {detail}")

        market_question = market.get("question") or market.get("title") or "Unknown"
        logger.info(f"Market PASSED all layers: {market_question[:60]}")
        return True, "All validation layers passed"
