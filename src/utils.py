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


def calculate_bond_score(
    entry_price: float,
    days_to_resolution: float,
    liquidity_clob: float,
    one_day_price_change: float,
    catalyst_penalty: float = 1.0,
    blacklist_penalty: float = 1.0,
    config: Optional[dict] = None,
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

    # R1: Confidence-adjusted yield — penalizes lower-confidence markets
    confidence_weight = entry_price ** 2
    adjusted_yield = yield_pct * confidence_weight

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

    return bond_score
