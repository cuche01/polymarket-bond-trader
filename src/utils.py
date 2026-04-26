"""
Utility functions and classes for the Polymarket Bond Bot.
"""

import asyncio
import logging
import logging.handlers
import math
import os
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional

import yaml


logger = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket rate limiter for API calls."""

    def __init__(self, rate: float, burst: Optional[float] = None):
        """
        Initialize rate limiter.

        Args:
            rate: Tokens per second (e.g., 100/60 for 100 req/min)
            burst: Maximum burst size (defaults to rate * 2)
        """
        self.rate = rate
        self.burst = burst if burst is not None else rate * 2
        self._tokens = self.burst
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        added = elapsed * self.rate
        self._tokens = min(self.burst, self._tokens + added)
        self._last_refill = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """
        Acquire tokens, waiting if necessary.

        Args:
            tokens: Number of tokens to consume
        """
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                # Calculate how long to wait
                deficit = tokens - self._tokens
                wait_time = deficit / self.rate
                await asyncio.sleep(wait_time)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """
        Try to acquire tokens without waiting.

        Args:
            tokens: Number of tokens to consume

        Returns:
            True if tokens were acquired, False otherwise
        """
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False


def retry_with_backoff(
    max_attempts: int = 3,
    delays: Optional[list] = None,
    exceptions: tuple = (Exception,),
) -> Callable:
    """
    Decorator factory for retrying functions with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts
        delays: List of delay values in seconds between retries
        exceptions: Tuple of exception types to catch and retry

    Returns:
        Decorator function
    """
    if delays is None:
        delays = [2, 4, 8]

    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs) -> Any:
                last_exception = None
                for attempt in range(max_attempts):
                    try:
                        return await func(*args, **kwargs)
                    except exceptions as e:
                        last_exception = e
                        if attempt < max_attempts - 1:
                            delay = delays[min(attempt, len(delays) - 1)]
                            logger.warning(
                                f"Attempt {attempt + 1}/{max_attempts} failed for "
                                f"{func.__name__}: {e}. Retrying in {delay}s..."
                            )
                            await asyncio.sleep(delay)
                        else:
                            logger.error(
                                f"All {max_attempts} attempts failed for {func.__name__}: {e}"
                            )
                raise last_exception
            return async_wrapper
        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs) -> Any:
                last_exception = None
                for attempt in range(max_attempts):
                    try:
                        return func(*args, **kwargs)
                    except exceptions as e:
                        last_exception = e
                        if attempt < max_attempts - 1:
                            delay = delays[min(attempt, len(delays) - 1)]
                            logger.warning(
                                f"Attempt {attempt + 1}/{max_attempts} failed for "
                                f"{func.__name__}: {e}. Retrying in {delay}s..."
                            )
                            time.sleep(delay)
                        else:
                            logger.error(
                                f"All {max_attempts} attempts failed for {func.__name__}: {e}"
                            )
                raise last_exception
            return sync_wrapper

    return decorator


def load_config(path: str = "config.yaml") -> dict:
    """
    Load configuration from YAML file.

    Args:
        path: Path to the config file

    Returns:
        Configuration dictionary
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    logger.debug(f"Loaded configuration from {path}")
    return config


def setup_logging(config: dict) -> logging.Logger:
    """
    Configure logging based on config settings.

    Args:
        config: Configuration dictionary with 'logging' section

    Returns:
        Root logger
    """
    log_config = config.get("logging", {})
    level_str = log_config.get("level", "INFO")
    level = getattr(logging, level_str.upper(), logging.INFO)
    log_file = log_config.get("file", "logs/bond_bot.log")
    max_size_mb = log_config.get("max_size_mb", 50)
    backup_count = log_config.get("backup_count", 5)

    # Ensure log directory exists
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console handler with rich-style formatting
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_fmt)

    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)8s] %(name)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logger.info(f"Logging configured: level={level_str}, file={log_file}")
    return root_logger


def round_to_tick(price: float, tick_size: float = 0.01) -> float:
    """
    Round a price to the nearest tick size.

    Args:
        price: The price to round
        tick_size: The minimum price increment

    Returns:
        Price rounded to nearest tick
    """
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")
    ticks = round(price / tick_size)
    return round(ticks * tick_size, 10)


def calculate_time_factor(days_to_resolution: float, is_bond: bool = False) -> float:
    """
    Calculate position size time factor based on days to resolution.

    For bonds, the curve is inverted: closer to resolution = more certain = larger position.
    For non-bonds, the original conservative curve is used.

    Args:
        days_to_resolution: Number of days until market resolves
        is_bond: Whether this is a bond position (entry >= $0.95)

    Returns:
        Time factor multiplier (0.2 to 1.0)
    """
    hours_to_resolution = days_to_resolution * 24

    if is_bond:
        # Bond: closer to resolution = higher certainty of payout
        if hours_to_resolution <= 24:
            return 1.0
        elif hours_to_resolution <= 72:
            return 0.9
        elif hours_to_resolution <= 168:  # 7 days
            return 0.7
        else:
            return 0.5
    else:
        # Non-bond: original conservative sizing
        if hours_to_resolution <= 6:
            return 1.0
        elif hours_to_resolution <= 24:
            return 0.8
        elif hours_to_resolution <= 72:
            return 0.6
        elif hours_to_resolution <= 168:  # 7 days
            return 0.4
        else:
            return 0.2


def feature_enabled(config: dict, flag_name: str) -> bool:
    """Check if a feature flag is enabled in config."""
    return config.get("feature_flags", {}).get(flag_name, False)


def shadow_enabled(config: dict, flag_name: str) -> bool:
    """Check if a filter's shadow variant is enabled (log verdict without enforcing).

    V4 Phase 2 (spec §0.2): new filters land with `shadow_{flag}` true and the real
    `{flag}` false. The detector/scanner/risk-engine runs the filter and records
    verdicts in pipeline_health for operator review, but the reject is not enforced.
    After 24h of acceptable shadow data, operator flips shadow off and real flag on.
    """
    return config.get("feature_flags", {}).get(f"shadow_{flag_name}", False)


def resolution_proximity_weight(days_to_resolution: float, decay_rate: float = 0.3) -> float:
    """
    Exponential proximity weight: e^(-decay_rate * days).
    Heavily favors short-duration markets vs linear 1/days.

    Examples (decay_rate=0.3):
        1 day  -> 0.74
        2 days -> 0.55
        7 days -> 0.12
        14 days -> 0.014
    """
    return math.exp(-decay_rate * max(days_to_resolution, 0.01))


def is_halt_requested() -> bool:
    """
    Check if a HALT file exists in the working directory.

    Returns:
        True if HALT file exists, False otherwise
    """
    halt_path = Path("HALT")
    return halt_path.exists()


def format_currency(amount: float, symbol: str = "$") -> str:
    """
    Format a numeric amount as currency string.

    Args:
        amount: The amount to format
        symbol: Currency symbol prefix

    Returns:
        Formatted currency string (e.g., "$1,234.56")
    """
    if amount < 0:
        return f"-{symbol}{abs(amount):,.2f}"
    return f"{symbol}{amount:,.2f}"


def format_percentage(value: float, decimal_places: int = 2) -> str:
    """
    Format a decimal value as a percentage string.

    Args:
        value: Decimal value (e.g., 0.05 for 5%)
        decimal_places: Number of decimal places to show

    Returns:
        Formatted percentage string (e.g., "5.00%")
    """
    return f"{value * 100:.{decimal_places}f}%"


def safe_json_parse(value: Any, default: Any = None) -> Any:
    """
    Safely parse a JSON string, returning default on failure.

    Args:
        value: Value to parse (string or already parsed)
        default: Default value if parsing fails

    Returns:
        Parsed value or default
    """
    import json

    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def parse_iso_datetime(dt_str: str) -> Optional[Any]:
    """
    Parse an ISO datetime string into a datetime object.

    Args:
        dt_str: ISO format datetime string

    Returns:
        datetime object or None if parsing fails
    """
    from datetime import datetime, timezone

    if not dt_str:
        return None

    # Handle various ISO formats
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f+00:00",
        "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(dt_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Try dateutil as fallback
    try:
        from datetime import timezone as tz
        import re
        # Simple Z suffix handler
        clean = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except Exception:
        logger.warning(f"Could not parse datetime: {dt_str}")
        return None


def get_days_to_resolution(end_date_str: str) -> float:
    """
    Calculate days remaining until market resolution.

    Args:
        end_date_str: ISO format end date string

    Returns:
        Float number of days (can be negative if already past)
    """
    from datetime import datetime, timezone

    end_dt = parse_iso_datetime(end_date_str)
    if end_dt is None:
        return float("inf")

    now = datetime.now(timezone.utc)
    delta = end_dt - now
    return delta.total_seconds() / 86400


# V4 1.2: Dynamic fee model (Polymarket fee = C × Θ × p × (1-p))
# Category peak rates per Polymarket 2026 schedule (used as fallback when
# market.feeSchedule is missing). Keys are lowercased category names.
_CATEGORY_PEAK_TAKER_RATE = {
    "geopolitics": 0.0,
    "politics": 0.010,
    "sports": 0.0075,
    "finance": 0.010,
    "tech": 0.010,
    "culture": 0.0125,
    "weather": 0.0125,
    "economics": 0.015,
    "crypto": 0.018,
    "mentions": 0.0156,
}


def _taker_rate_at(price: float, fee_schedule: Optional[dict]) -> float:
    """Return the effective taker fee rate at a given price, scaled by p(1-p).

    Θ is the peak taker-fee coefficient (per-unit-of-shares rate at p=0.5).
    At p=0.5 the p(1-p) factor = 0.25, so effective rate at p=0.5 is Θ/4.
    The formula `fee = C × Θ × p × (1-p)` returns fee in USDC for C shares,
    so fee/notional = Θ × p × (1-p) / p = Θ × (1-p) — but the utility here
    returns the share-space rate (fee per share) for composition with shares.
    We keep the canonical p(1-p) form; callers multiply by shares.
    """
    if not fee_schedule or not fee_schedule.get("feesEnabled", True):
        return 0.0
    theta = fee_schedule.get("takerFeeCoefficient")
    if theta is None:
        bps = fee_schedule.get("takerFeeBps")
        if bps is not None:
            theta = float(bps) / 10000.0
    if theta is None:
        theta = fee_schedule.get("peakTakerFeeRate", 0.0)
    theta = float(theta or 0.0)
    p = max(0.01, min(0.99, float(price)))
    return theta * p * (1.0 - p)


def calculate_taker_fee(
    price: float,
    shares: float,
    fee_schedule: Optional[dict],
) -> float:
    """Compute taker fee in USDC for a trade at `price` for `shares` shares.

    Polymarket formula: fee = C × Θ × p × (1-p). Returns 0 if fees disabled,
    no schedule provided, or Θ resolves to 0 (e.g. Geopolitics).
    """
    rate = _taker_rate_at(price, fee_schedule)
    return float(shares) * rate


def calculate_maker_fee(
    price: float,
    shares: float,
    fee_schedule: Optional[dict],
) -> float:
    """Makers pay no fees on Polymarket. Kept as symmetric API."""
    return 0.0


def fee_schedule_from_category(
    category: Optional[str],
    fallback_taker_rate: float = 0.002,
) -> dict:
    """Synthesize a fee_schedule dict from a category name when Gamma omits one.

    Uses the published 2026 peak rates. Unknown categories fall back to
    `fallback_taker_rate` (treated as Θ, i.e. per-share rate at p=0.5).
    """
    cat = (category or "").strip().lower()
    theta = _CATEGORY_PEAK_TAKER_RATE.get(cat, fallback_taker_rate)
    return {
        "feesEnabled": theta > 0.0,
        "takerFeeCoefficient": theta,
        "source": "category_fallback",
        "category": cat,
    }


def estimate_round_trip_fee_rate(
    entry_price: float,
    exit_price: Optional[float],
    fee_schedule: Optional[dict],
    entry_is_maker: bool = True,
    exit_is_taker: bool = True,
    exit_is_resolution: bool = False,
) -> float:
    """Return round-trip fee as a fraction of position notional.

    Used for net-yield gating. `fee_schedule` may be None — returns 0 in that
    case (caller is expected to have supplied fallback via
    `fee_schedule_from_category`).
    """
    if fee_schedule is None:
        return 0.0
    if not fee_schedule.get("feesEnabled", True):
        return 0.0

    entry_fee_rate = 0.0
    if not entry_is_maker:
        entry_fee_rate = _taker_rate_at(entry_price, fee_schedule)

    exit_fee_rate = 0.0
    if not exit_is_resolution and exit_is_taker:
        px = exit_price if exit_price is not None else 1.0
        exit_fee_rate = _taker_rate_at(px, fee_schedule)

    return entry_fee_rate + exit_fee_rate


def resolve_fee_schedule(market: dict, fees_cfg: dict) -> Optional[dict]:
    """Extract feeSchedule from a Gamma market dict with category fallback.

    Returns None when dynamic fees are disabled in config so callers collapse
    to legacy flat-rate behaviour.
    """
    if not fees_cfg.get("use_dynamic_fees", True):
        return None
    fs = market.get("feeSchedule") or market.get("fee_schedule")
    if isinstance(fs, dict) and fs:
        if "feesEnabled" not in fs:
            fs = {**fs, "feesEnabled": True}
        return fs
    category = market.get("category") or market.get("marketType")
    fallback = fees_cfg.get("fallback_taker_rate", 0.002)
    return fee_schedule_from_category(category, fallback_taker_rate=fallback)


# Canonical-underlying patterns. Ordered: first match wins; specific before general.
# Each entry is (underlying_key, regex). Used to cap concurrent exposure to any
# single asset across different strike prices / resolution dates.
_UNDERLYING_PATTERNS = [
    ("BTC", r"\b(bitcoin|btc)\b"),
    ("ETH", r"\b(ethereum|ether|eth)\b"),
    ("SOL", r"\b(solana|sol)\b"),
    ("XRP", r"\b(xrp|ripple)\b"),
    ("DOGE", r"\b(dogecoin|doge)\b"),
    ("ADA", r"\b(cardano|ada)\b"),
    ("WTI", r"\b(wti|crude oil|oil price)\b"),
    ("GOLD", r"\bgold\b"),
    ("SPX", r"\b(s&p\s*500|spx|s&p)\b"),
    ("NDX", r"\b(nasdaq)\b"),
    ("NVDA", r"\b(nvidia|nvda)\b"),
    ("AAPL", r"\b(apple|aapl)\b"),
    ("MSFT", r"\b(microsoft|msft)\b"),
    ("GOOG", r"\b(google|alphabet|goog|googl)\b"),
    ("AMZN", r"\b(amazon|amzn)\b"),
    ("TSLA", r"\b(tesla|tsla)\b"),
    ("META", r"\b(facebook|meta\b|metaplatforms)"),
    ("FED",  r"\b(fed rate|interest rate|fomc|federal reserve)\b"),
    ("CPI",  r"\b(cpi|inflation rate)\b"),
]


def classify_underlying(market_question: str) -> Optional[str]:
    """Canonicalise a market question to an underlying asset key (e.g. 'BTC').

    Returns None if no specific underlying is detected. Used to cap concurrent
    exposure and enforce cooldowns on assets that Polymarket lists as separate
    markets but that move in lockstep (strike variants, different dates).
    """
    import re as _re
    if not market_question:
        return None
    q = market_question.lower()
    for key, pattern in _UNDERLYING_PATTERNS:
        if _re.search(pattern, q):
            return key
    return None


def estimate_holding_rewards(
    position_value_usd: float,
    days_held: float,
    holding_rewards_apr: float = 0.04,
) -> float:
    """V4 Phase 2.3: expected USDC rewards for holding the position.

    Polymarket emits rewards on unmatched YES balance at a fixed APY on
    reward-eligible markets. Formula: `value * apr * days / 365`. The estimate
    is simple-interest; daily reconciliation (live mode) populates actuals.
    """
    if position_value_usd <= 0 or days_held <= 0 or holding_rewards_apr <= 0:
        return 0.0
    return position_value_usd * holding_rewards_apr * (days_held / 365.0)


def calculate_bond_score(
    entry_price: float,
    days_to_resolution: float,
    liquidity_clob: float,
    one_day_price_change: float,
    catalyst_penalty: float = 1.0,
    blacklist_penalty: float = 1.0,
    config: Optional[dict] = None,
    holding_rewards_enabled: bool = False,
    holding_rewards_apr: float = 0.0,
    lp_rewards_boost: float = 1.0,
) -> float:
    """
    Calculate the bond score for a market opportunity.

    Args:
        entry_price: Current YES price (0.95-0.99)
        days_to_resolution: Days until market resolves
        liquidity_clob: CLOB liquidity in USD
        one_day_price_change: Absolute price change over past 24h
        catalyst_penalty: Penalty from binary catalyst classifier (0.0-1.0)
        blacklist_penalty: Penalty from blacklist learning loop (0.0-1.0)
        config: Optional config dict for scoring parameters

    Returns:
        Bond score (higher is better)
    """
    if days_to_resolution <= 0:
        return 0.0

    yield_pct = (1.00 - entry_price) / entry_price

    # V4 Phase 2.3: holding-reward yield accrues on YES balance at 4% APY on
    # eligible markets. Fold it into the yield term so shorter-duration
    # reward-eligible markets don't dominate (time-to-resolve already
    # normalizes below).
    if holding_rewards_enabled and holding_rewards_apr > 0:
        holding_reward_yield = holding_rewards_apr * (days_to_resolution / 365.0)
    else:
        holding_reward_yield = 0.0

    # R1: Confidence-adjusted yield — penalizes lower-confidence markets
    confidence_weight = entry_price ** 2
    adjusted_yield = (yield_pct + holding_reward_yield) * confidence_weight

    liquidity_weight = min(liquidity_clob / 50000.0, 1.0)

    abs_change = abs(one_day_price_change)
    if abs_change < 0.01:
        stability_weight = 1.0
    elif abs_change < 0.03:
        stability_weight = 0.8
    else:
        stability_weight = 0.5

    # P3: Exponential proximity weighting (if enabled)
    use_exp = False
    decay_rate = 0.3
    if config:
        scoring_cfg = config.get("scoring", {})
        use_exp = scoring_cfg.get("use_exponential_proximity", False) and feature_enabled(
            config, "exponential_proximity"
        )
        decay_rate = scoring_cfg.get("resolution_proximity_decay_rate", 0.3)

    if use_exp:
        time_weight = resolution_proximity_weight(days_to_resolution, decay_rate)
        bond_score = adjusted_yield * time_weight * liquidity_weight * stability_weight
    else:
        bond_score = (adjusted_yield / days_to_resolution) * liquidity_weight * stability_weight

    # Apply penalties from catalyst classifier and blacklist learner
    bond_score *= catalyst_penalty * blacklist_penalty

    # V4 Phase 2.5: LP-rewarded markets have deeper books / tighter spreads.
    # Caller passes 1.15 when the market qualifies, 1.0 otherwise.
    if lp_rewards_boost and lp_rewards_boost != 1.0:
        bond_score *= lp_rewards_boost

    return bond_score
