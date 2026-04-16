"""
Market scanner that fetches and filters Polymarket opportunities.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .utils import (
    RateLimiter,
    calculate_bond_score,
    feature_enabled,
    get_days_to_resolution,
    retry_with_backoff,
    safe_json_parse,
)

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


class MarketScanner:
    """Scans Polymarket for bond strategy opportunities."""

    def __init__(self, config: dict):
        """
        Initialize scanner with configuration.

        Args:
            config: Full configuration dictionary
        """
        self.config = config
        scanner_cfg = config.get("scanner", {})
        self.scan_interval = scanner_cfg.get("scan_interval_seconds", 300)
        self.min_entry_price = scanner_cfg.get("min_entry_price", 0.95)
        self.max_entry_price = scanner_cfg.get("max_entry_price", 0.99)
        self.max_days = scanner_cfg.get("max_days_to_resolution", 14)
        self.preferred_resolution_hours = scanner_cfg.get("preferred_resolution_hours", 72)
        self.min_liquidity = scanner_cfg.get("min_liquidity", 10000)
        self.min_volume_24h = scanner_cfg.get("min_volume_24h", 5000)
        self.max_volatility_1d = scanner_cfg.get("max_price_volatility_1d", 0.03)
        self.excluded_categories = scanner_cfg.get("excluded_categories", [])

        # P1: Volume trend filter
        self.volume_trend_min_ratio = scanner_cfg.get("volume_trend_min_ratio", 0.70)

        # P3: Time-of-day liquidity filter
        self.time_filter_weekend_multiplier = scanner_cfg.get("time_filter_weekend_multiplier", 2.0)
        self.time_filter_offpeak_multiplier = scanner_cfg.get("time_filter_offpeak_multiplier", 1.5)

        # Rate limiter: 100 req/min = ~1.67/sec
        self.rate_limiter = RateLimiter(rate=1.5, burst=10)

        self._session: Optional[aiohttp.ClientSession] = None
        self._last_scan_time: float = 0
        self._cached_candidates: List[Dict] = []

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120),
                headers={"User-Agent": "PolymarketBondBot/1.0"},
            )
        return self._session

    async def close(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _fetch_markets_page(
        self,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Dict]:
        """
        Fetch one page of active markets from Gamma API.

        Args:
            limit: Number of markets per page
            offset: Pagination offset

        Returns:
            List of market dictionaries
        """
        await self.rate_limiter.acquire()
        url = (
            f"{GAMMA_API_BASE}/markets"
            f"?closed=false&active=true&enableOrderBook=true"
            f"&limit={limit}&offset={offset}"
        )
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"Gamma API returned {resp.status} at offset={offset}")
                    return []
                data = await resp.json()
                # API may return a list or dict with results
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict):
                    return data.get("markets", data.get("results", []))
                return []
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error fetching markets page (offset={offset}): {e}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error for markets page (offset={offset}): {e}")
            return []

    async def scan_markets(self) -> List[Dict]:
        """
        Fetch all active markets from Gamma API with pagination.

        Returns:
            List of all active market dictionaries
        """
        all_markets = []
        offset = 0
        limit = 500

        logger.info("Starting market scan...")
        while True:
            page = await self._fetch_markets_page(limit=limit, offset=offset)
            if not page:
                break
            all_markets.extend(page)
            logger.debug(f"Fetched {len(page)} markets at offset={offset}")

            # If we got fewer than the limit, we've reached the end
            if len(page) < limit:
                break
            offset += limit

        logger.info(f"Fetched {len(all_markets)} total markets")
        return all_markets

    def _parse_market_prices(self, market: Dict) -> Tuple[Optional[float], Optional[float]]:
        """
        Parse YES and NO prices from market data.

        Args:
            market: Market dictionary from Gamma API

        Returns:
            Tuple of (yes_price, no_price), either may be None
        """
        outcome_prices = safe_json_parse(market.get("outcomePrices"))
        if outcome_prices and isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
            try:
                yes_price = float(outcome_prices[0])
                no_price = float(outcome_prices[1])
                return yes_price, no_price
            except (ValueError, TypeError):
                pass

        # Fallback to bestAsk/bestBid style fields
        yes_price = market.get("lastTradePrice") or market.get("price")
        if yes_price:
            try:
                return float(yes_price), None
            except (ValueError, TypeError):
                pass

        return None, None

    def _parse_token_ids(self, market: Dict) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse YES and NO token IDs from market data.

        Args:
            market: Market dictionary

        Returns:
            Tuple of (yes_token_id, no_token_id)
        """
        token_ids = safe_json_parse(market.get("clobTokenIds"))
        if token_ids and isinstance(token_ids, list) and len(token_ids) >= 2:
            return str(token_ids[0]), str(token_ids[1])
        elif token_ids and isinstance(token_ids, list) and len(token_ids) == 1:
            return str(token_ids[0]), None
        return None, None

    def _check_volume_trend(self, market: Dict) -> bool:
        """
        P1: Reject markets where 24h volume is declining relative to longer-term data.
        Uses volume_num (lifetime) as a proxy for average volume when 7d data is unavailable.
        Returns True if volume trend is acceptable.
        """
        volume_24h = float(market.get("volume24hr") or market.get("volume") or 0)
        # Gamma API provides 'volume' as lifetime total volume
        total_volume = float(market.get("volume") or 0)

        if total_volume <= 0 or volume_24h <= 0:
            return volume_24h >= self.min_volume_24h

        # Estimate daily average from lifetime volume and market age
        # If the market has been live for at least a few days, this is meaningful
        end_date = market.get("endDate") or market.get("end_date_iso")
        if not end_date:
            return True
        days_to_res = get_days_to_resolution(end_date)
        # Assume market has been live for roughly (max_days - days_to_res) days
        # This is an approximation; true creation date isn't always available
        estimated_age_days = max(self.max_days - days_to_res, 3)
        estimated_daily_avg = total_volume / max(estimated_age_days, 1)

        if estimated_daily_avg <= 0:
            return True

        ratio = volume_24h / estimated_daily_avg
        if ratio < self.volume_trend_min_ratio:
            return False

        return True

    def _get_time_adjusted_min_liquidity(self) -> float:
        """
        P3: Increase minimum liquidity requirement during low-activity hours.

        Peak hours:  14:00–22:00 UTC (US market hours) -> base min_liquidity
        Off-peak:    22:00–14:00 UTC weekdays -> 1.5x
        Weekends:    All day Sat/Sun -> 2.0x
        """
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)

        if now_utc.weekday() >= 5:  # Saturday=5, Sunday=6
            return self.min_liquidity * self.time_filter_weekend_multiplier

        hour = now_utc.hour
        if 14 <= hour < 22:  # Peak US hours
            return self.min_liquidity
        else:
            return self.min_liquidity * self.time_filter_offpeak_multiplier

    def filter_candidates(self, markets: List[Dict]) -> List[Dict]:
        """
        Apply all filters to find bond strategy candidates.

        Args:
            markets: List of market dictionaries from API

        Returns:
            List of markets that pass all filters
        """
        candidates = []
        rejected_counts = {
            "no_price": 0,
            "price_range": 0,
            "no_end_date": 0,
            "expired": 0,
            "too_far": 0,
            "low_liquidity": 0,
            "low_volume": 0,
            "volume_trend": 0,
            "high_volatility": 0,
            "excluded_category": 0,
            "no_token_id": 0,
        }

        for market in markets:
            # Check if market is actually active
            if not market.get("active", False):
                continue
            if market.get("closed", False):
                continue

            # Parse prices
            yes_price, no_price = self._parse_market_prices(market)
            if yes_price is None:
                rejected_counts["no_price"] += 1
                continue

            # Price range filter
            if not (self.min_entry_price <= yes_price <= self.max_entry_price):
                rejected_counts["price_range"] += 1
                continue

            # End date filter
            end_date = market.get("endDate") or market.get("end_date_iso")
            if not end_date:
                rejected_counts["no_end_date"] += 1
                continue

            days_to_resolution = get_days_to_resolution(end_date)
            if days_to_resolution <= 0:
                rejected_counts["expired"] += 1
                continue
            if days_to_resolution > self.max_days:
                rejected_counts["too_far"] += 1
                continue

            # Liquidity filter (P3: time-of-day adjusted)
            liquidity = float(market.get("liquidityClob") or market.get("liquidity") or 0)
            if feature_enabled(self.config, "time_of_day_filter"):
                min_liq = self._get_time_adjusted_min_liquidity()
            else:
                min_liq = self.min_liquidity
            if liquidity < min_liq:
                rejected_counts["low_liquidity"] += 1
                continue

            # Volume filter
            volume_24h = float(market.get("volume24hr") or market.get("volume") or 0)
            if volume_24h < self.min_volume_24h:
                rejected_counts["low_volume"] += 1
                continue

            # P1: Volume trend filter
            if feature_enabled(self.config, "volume_trend_filter"):
                if not self._check_volume_trend(market):
                    rejected_counts["volume_trend"] += 1
                    continue

            # Volatility filter
            price_change_1d = float(market.get("oneDayPriceChange") or 0)
            if abs(price_change_1d) > self.max_volatility_1d:
                rejected_counts["high_volatility"] += 1
                continue

            # Category exclusion filter
            category = market.get("category") or market.get("marketType") or ""
            if any(
                excl.lower() in category.lower()
                for excl in self.excluded_categories
            ):
                rejected_counts["excluded_category"] += 1
                continue

            # Ensure we have token IDs for order placement
            yes_token_id, no_token_id = self._parse_token_ids(market)
            if not yes_token_id:
                rejected_counts["no_token_id"] += 1
                continue

            # Enrich market with parsed data
            market["_yes_price"] = yes_price
            market["_no_price"] = no_price
            market["_yes_token_id"] = yes_token_id
            market["_no_token_id"] = no_token_id
            market["_days_to_resolution"] = days_to_resolution
            market["_liquidity"] = liquidity
            market["_volume_24h"] = volume_24h
            market["_price_change_1d"] = price_change_1d

            candidates.append(market)
            logger.debug(
                f"Candidate: '{market.get('question', '')[:60]}' "
                f"→ category='{market.get('category', 'MISSING')}'"
            )

        logger.info(
            f"Filtered {len(markets)} markets → {len(candidates)} candidates. "
            f"Rejections: {rejected_counts}"
        )
        return candidates

    def score_candidate(self, market: Dict) -> float:
        """
        Calculate bond score for a market candidate.

        Args:
            market: Enriched market dictionary (must have _yes_price etc.)

        Returns:
            Bond score (higher is better)
        """
        yes_price = market.get("_yes_price", 0)
        days = market.get("_days_to_resolution", 1)
        liquidity = market.get("_liquidity", 0)
        price_change_1d = market.get("_price_change_1d", 0)

        if yes_price <= 0 or days <= 0:
            return 0.0

        score = calculate_bond_score(
            entry_price=yes_price,
            days_to_resolution=days,
            liquidity_clob=liquidity,
            one_day_price_change=price_change_1d,
            catalyst_penalty=market.get("_catalyst_penalty", 1.0),
            blacklist_penalty=market.get("_blacklist_penalty", 1.0),
            config=self.config,
        )
        market["_bond_score"] = score
        return score

    def get_ranked_candidates(self, markets: Optional[List[Dict]] = None) -> List[Dict]:
        """
        Score and rank all candidates by bond score.

        Args:
            markets: Optional pre-filtered list; uses cache if None

        Returns:
            Candidates sorted by bond score (descending)
        """
        if markets is not None:
            candidates = markets
        else:
            candidates = self._cached_candidates

        # Score each candidate
        for market in candidates:
            self.score_candidate(market)

        # Sort by bond score descending
        ranked = sorted(candidates, key=lambda m: m.get("_bond_score", 0), reverse=True)
        return ranked

    async def get_market_details(self, condition_id: str) -> Optional[Dict]:
        """
        Fetch detailed info for a specific market.

        Args:
            condition_id: Market condition ID

        Returns:
            Market details dictionary or None
        """
        await self.rate_limiter.acquire()
        url = f"{GAMMA_API_BASE}/markets/{condition_id}"
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Market details returned {resp.status} for {condition_id}")
                return None
        except Exception as e:
            logger.error(f"Error fetching market details for {condition_id}: {e}")
            return None

    async def get_price_history(
        self,
        market_id: str,
        fidelity: int = 60,
        start_ts: Optional[int] = None,
    ) -> List[Dict]:
        """
        Fetch price history timeseries for a market.

        Args:
            market_id: Market condition ID or slug
            fidelity: Data point interval in minutes
            start_ts: Start timestamp (Unix seconds)

        Returns:
            List of price history data points
        """
        await self.rate_limiter.acquire()
        params = f"market={market_id}&fidelity={fidelity}"
        if start_ts:
            params += f"&startTs={start_ts}"
        url = f"{GAMMA_API_BASE}/prices-history?{params}"
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        return data
                    return data.get("history", [])
                return []
        except Exception as e:
            logger.error(f"Error fetching price history for {market_id}: {e}")
            return []

    async def run_scan_cycle(self) -> List[Dict]:
        """
        Run a complete scan cycle: fetch, filter, score, rank.

        Respects scan_interval to avoid redundant scans.

        Returns:
            Ranked list of bond candidates
        """
        now = time.time()
        time_since_last = now - self._last_scan_time

        if time_since_last < self.scan_interval and self._cached_candidates:
            logger.debug(
                f"Using cached candidates ({time_since_last:.0f}s < "
                f"{self.scan_interval}s interval)"
            )
            return self.get_ranked_candidates()

        start_time = time.time()
        try:
            markets = await self.scan_markets()
            candidates = self.filter_candidates(markets)
            ranked = self.get_ranked_candidates(candidates)
            self._cached_candidates = ranked
            self._last_scan_time = time.time()

            duration_ms = int((time.time() - start_time) * 1000)
            logger.info(
                f"Scan complete: {len(markets)} markets → {len(ranked)} candidates "
                f"({duration_ms}ms)"
            )
            return ranked

        except Exception as e:
            logger.error(f"Scan cycle failed: {e}", exc_info=True)
            return self._cached_candidates  # Return stale cache on error
