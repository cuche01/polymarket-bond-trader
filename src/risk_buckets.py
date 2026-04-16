"""
Risk bucket classification for correlation clustering.

Maps Polymarket markets to risk buckets based on category and question text.
This prevents correlated markets that Polymarket labels differently from
concentrating risk beyond safe limits.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_RISK_BUCKET_MAP = {
    "politics": {
        "category_matches": ["Politics", "US Politics", "Global Politics", "Elections"],
        "keyword_matches": [
            "president", "president of", "election", "congress", "senate", "governor",
            "prime minister", "prime minister of", "parliament", "vote", "impeach",
            "cabinet", "democrat", "republican", "trump", "biden", "minister",
            "head of state", "political party",
            # Electoral / legislative
            "redistricting", "referendum", "ballot", "special election",
            "seats", "constituency", "party win",
        ],
        "max_bucket_exposure_pct": 0.25,
    },
    "crypto": {
        "category_matches": ["Crypto", "Bitcoin", "Ethereum", "DeFi"],
        "keyword_matches": [
            "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
            "crypto", "token", "blockchain", "defi", "nft", "etf",
            "price of bitcoin", "price of ethereum", "price of solana",
            "xrp", "ripple", "cardano", "ada", "dogecoin", "doge",
        ],
        "max_bucket_exposure_pct": 0.20,
    },
    "macro": {
        "category_matches": ["Economics", "Finance", "Fed", "Macro", "Commodities"],
        "keyword_matches": [
            "fed", "interest rate", "inflation", "gdp", "recession",
            "unemployment", "tariff", "trade war", "sanctions",
            "treasury", "central bank", "crude oil", "oil price",
            "military action", "military strike", "war", "conflict",
            "invasion", "nuclear", "missile", "attack on",
            # Stock / equity markets
            "stock", "s&p", "nasdaq", "dow jones", "finish week",
            "close above", "close below", "market cap",
            "aapl", "msft", "goog", "amzn", "tsla", "nvda", "meta",
        ],
        "max_bucket_exposure_pct": 0.25,
    },
    "sports": {
        "category_matches": ["Sports", "NBA", "NFL", "MLB", "Soccer", "Tennis", "MMA", "Boxing"],
        "keyword_matches": [
            "game", "match", "championship", "playoff", "finals",
            "world cup", "super bowl", "mvp", "nba", "nfl", "mlb",
            "premier league", "champions league", "tournament",
            # Common "X vs. Y" matchup pattern for sports
            " vs. ", " vs ",
            # NBA teams
            "76ers", "celtics", "lakers", "bucks", "nuggets", "knicks",
            "heat", "cavaliers", "pacers", "wizards", "hawks", "nets",
            "bulls", "pistons", "raptors", "magic", "hornets", "thunder",
            "timberwolves", "pelicans", "spurs", "rockets", "mavericks",
            "warriors", "clippers", "suns", "kings", "grizzlies", "blazers",
            # NFL teams
            "patriots", "chiefs", "eagles", "cowboys", "49ers", "ravens",
            "bills", "bengals", "dolphins", "steelers", "chargers",
            # Tennis
            "alcaraz", "djokovic", "sinner", "medvedev", "monte carlo masters",
            "roland garros", "wimbledon", "us open tennis",
            # Asian basketball / other leagues
            "sharks", "sturgeons", "cba", "euroleague",
        ],
        "max_bucket_exposure_pct": 0.15,
    },
    "culture": {
        "category_matches": ["Pop Culture", "Entertainment", "Awards", "Media", "Music", "Film"],
        "keyword_matches": [
            "oscar", "grammy", "emmy", "box office", "streaming",
            "movie", "album", "celebrity", "netflix", "youtube",
            "mrbeast", "views", "subscribers", "one piece", "anime",
            "tv show", "series", "episode", "season finale",
        ],
        "max_bucket_exposure_pct": 0.15,
    },
    "science_tech": {
        "category_matches": ["Science", "Technology", "AI", "Space"],
        "keyword_matches": [
            "ai", "artificial intelligence", "spacex", "nasa",
            "climate", "fda", "vaccine", "patent",
            "gpt", "openai", "llm", "model", "launch",
        ],
        "max_bucket_exposure_pct": 0.20,
    },
}

FALLBACK_BUCKET = "other"
FALLBACK_MAX_EXPOSURE_PCT = 0.10


class RiskBucketClassifier:
    """
    Maps a Polymarket market to a risk bucket based on its category and question text.
    Priority: category_matches first, then keyword_matches on question text.

    The config's ``risk_buckets`` section may only contain ``max_bucket_exposure_pct``
    overrides. This class always starts from DEFAULT_RISK_BUCKET_MAP (which carries the
    full category_matches and keyword_matches rules) and merges in any config overrides.
    Passing a config dict that has no keyword/category rules will NOT break matching.
    """

    def __init__(self, bucket_map: Optional[dict] = None):
        # Always start with the full defaults so keyword/category matching rules are present
        self.bucket_map = {k: dict(v) for k, v in DEFAULT_RISK_BUCKET_MAP.items()}

        if bucket_map:
            for bucket_name, overrides in bucket_map.items():
                if bucket_name in self.bucket_map:
                    # Merge: only update keys that are present in the override
                    self.bucket_map[bucket_name].update(overrides)
                else:
                    # Brand-new bucket defined entirely in config
                    self.bucket_map[bucket_name] = overrides

        logger.debug(
            f"RiskBucketClassifier loaded {len(self.bucket_map)} buckets: "
            f"{list(self.bucket_map.keys())}"
        )

    def classify(self, polymarket_category: str, market_question: str) -> str:
        """
        Determine risk bucket for a market.

        Args:
            polymarket_category: Category field from Polymarket API
            market_question: Market question text

        Returns:
            Bucket name (e.g., 'politics', 'crypto', 'other')
        """
        category_lower = (polymarket_category or "").lower()
        question_lower = (market_question or "").lower()

        for bucket_name, rules in self.bucket_map.items():
            for cat_pattern in rules.get("category_matches", []):
                if cat_pattern.lower() in category_lower:
                    return bucket_name

            for keyword in rules.get("keyword_matches", []):
                # Use word-boundary matching to avoid false substring matches
                # (e.g. "etf" inside "netflix", "war" inside "award")
                pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
                if re.search(pattern, question_lower):
                    return bucket_name

        return FALLBACK_BUCKET

    def get_max_exposure(self, bucket_name: str) -> float:
        """Get the max portfolio exposure % for a given bucket."""
        if bucket_name in self.bucket_map:
            return self.bucket_map[bucket_name]["max_bucket_exposure_pct"]
        return FALLBACK_MAX_EXPOSURE_PCT
