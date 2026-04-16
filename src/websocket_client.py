"""
WebSocket client for real-time Polymarket market data.
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, AsyncGenerator

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Reconnect delays in seconds
RECONNECT_DELAYS = [1, 2, 4, 8, 16, 30, 60]
# Fallback to REST polling after this many seconds of WS unavailability
WS_FALLBACK_TIMEOUT = 60


class PolymarketWebSocket:
    """WebSocket client for real-time Polymarket price and book updates."""

    def __init__(self, config: Optional[dict] = None):
        """
        Initialize WebSocket client.

        Args:
            config: Optional configuration dictionary
        """
        self.config = config or {}
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._subscribed_condition_ids: List[str] = []
        self._subscribed_token_ids: List[str] = []
        self._price_callbacks: List[Callable] = []
        self._book_callbacks: List[Callable] = []
        self._trade_callbacks: List[Callable] = []
        self._reconnect_attempt = 0
        self._last_message_time: float = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._fallback_active = False
        self._latest_prices: Dict[str, float] = {}  # condition_id -> price

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session for REST fallback."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    def on_price_update(self, callback: Callable) -> None:
        """Register a callback for price updates."""
        self._price_callbacks.append(callback)

    def on_book_update(self, callback: Callable) -> None:
        """Register a callback for orderbook updates."""
        self._book_callbacks.append(callback)

    def on_trade(self, callback: Callable) -> None:
        """Register a callback for trade events."""
        self._trade_callbacks.append(callback)

    async def connect(self) -> bool:
        """
        Connect to the WebSocket server.

        Returns:
            True if connected successfully
        """
        try:
            logger.info(f"Connecting to WebSocket: {WS_URL}")
            self._ws = await websockets.connect(
                WS_URL,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
                max_size=10 * 1024 * 1024,  # 10MB max message
            )
            self._connected = True
            self._reconnect_attempt = 0
            self._last_message_time = time.time()
            self._fallback_active = False
            logger.info("WebSocket connected successfully")
            return True
        except (WebSocketException, OSError, asyncio.TimeoutError) as e:
            logger.error(f"WebSocket connection failed: {e}")
            self._connected = False
            return False

    async def subscribe_to_markets(
        self,
        condition_ids: List[str],
        token_ids: Optional[List[str]] = None,
    ) -> None:
        """
        Subscribe to market updates for given condition and token IDs.

        Args:
            condition_ids: List of market condition IDs
            token_ids: Optional list of token IDs to subscribe to
        """
        self._subscribed_condition_ids = condition_ids
        self._subscribed_token_ids = token_ids or []

        if not self._connected or not self._ws:
            logger.warning("Cannot subscribe: WebSocket not connected")
            return

        # Subscribe to market price updates
        if condition_ids:
            subscribe_msg = {
                "type": "subscribe",
                "channel": "market",
                "markets": condition_ids,
            }
            try:
                await self._ws.send(json.dumps(subscribe_msg))
                logger.info(f"Subscribed to {len(condition_ids)} markets")
            except Exception as e:
                logger.error(f"Failed to send subscription message: {e}")

        # Subscribe to orderbook updates if token IDs provided
        if self._subscribed_token_ids:
            book_msg = {
                "type": "subscribe",
                "channel": "book",
                "assets_ids": self._subscribed_token_ids,
            }
            try:
                await self._ws.send(json.dumps(book_msg))
                logger.info(f"Subscribed to {len(self._subscribed_token_ids)} token orderbooks")
            except Exception as e:
                logger.error(f"Failed to send book subscription: {e}")

    async def listen(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Async generator that yields market events.

        Yields:
            Event dictionaries with 'type' and relevant data fields
        """
        self._running = True

        while self._running:
            if not self._connected:
                connected = await self.connect()
                if not connected:
                    # Start fallback mode
                    logger.warning("WebSocket unavailable, using REST fallback")
                    self._fallback_active = True
                    async for event in self._rest_fallback_generator():
                        if not self._running:
                            return
                        yield event
                    continue

                # Re-subscribe after reconnect
                if self._subscribed_condition_ids:
                    await self.subscribe_to_markets(
                        self._subscribed_condition_ids,
                        self._subscribed_token_ids,
                    )

            try:
                message = await asyncio.wait_for(self._ws.recv(), timeout=45.0)
                self._last_message_time = time.time()

                event = self._parse_message(message)
                if event:
                    # Update internal price cache
                    if event.get("type") in ("price_update", "last_trade"):
                        market_id = event.get("market_id") or event.get("condition_id")
                        price = event.get("price")
                        if market_id and price:
                            self._latest_prices[market_id] = float(price)

                    # Dispatch to registered callbacks
                    await self._dispatch_event(event)
                    yield event

            except asyncio.TimeoutError:
                # No message received in 45s, check if connection alive
                time_since = time.time() - self._last_message_time
                if time_since > WS_FALLBACK_TIMEOUT:
                    logger.warning(
                        f"No WS message for {time_since:.0f}s, switching to REST fallback"
                    )
                    self._connected = False
                else:
                    # Send keepalive ping
                    try:
                        if self._ws and not self._ws.closed:
                            await self._ws.ping()
                    except Exception:
                        self._connected = False

            except ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
                self._connected = False
                await self.reconnect_with_backoff()

            except WebSocketException as e:
                logger.error(f"WebSocket error: {e}")
                self._connected = False
                await self.reconnect_with_backoff()

            except Exception as e:
                logger.error(f"Unexpected WebSocket error: {e}", exc_info=True)
                self._connected = False
                await asyncio.sleep(5)

    def _parse_message(self, raw: str) -> Optional[Dict[str, Any]]:
        """
        Parse a raw WebSocket message string.

        Args:
            raw: Raw message string

        Returns:
            Parsed event dictionary or None
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.debug(f"Failed to parse WS message: {e}")
            return None

        if isinstance(data, list):
            # Handle batch updates
            if data:
                return {"type": "batch", "events": data}
            return None

        if not isinstance(data, dict):
            return None

        msg_type = data.get("type") or data.get("event_type")

        # Normalize different message types
        if msg_type in ("price_change", "price_update", "market"):
            return {
                "type": "price_update",
                "market_id": data.get("market_id") or data.get("asset_id"),
                "condition_id": data.get("condition_id"),
                "price": data.get("price") or data.get("outcome_prices"),
                "timestamp": data.get("timestamp"),
            }
        elif msg_type in ("book", "orderbook"):
            return {
                "type": "book_update",
                "asset_id": data.get("asset_id"),
                "bids": data.get("bids", []),
                "asks": data.get("asks", []),
                "timestamp": data.get("timestamp"),
            }
        elif msg_type == "trade":
            return {
                "type": "trade",
                "market_id": data.get("market_id"),
                "price": data.get("price"),
                "size": data.get("size"),
                "side": data.get("side"),
                "timestamp": data.get("timestamp"),
            }
        elif msg_type in ("last_trade_price", "last_trade"):
            return {
                "type": "last_trade",
                "market_id": data.get("market_id") or data.get("asset_id"),
                "price": data.get("price"),
                "timestamp": data.get("timestamp"),
            }

        return {"type": "unknown", "raw": data}

    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        """Dispatch an event to registered callbacks."""
        event_type = event.get("type")
        try:
            if event_type in ("price_update", "last_trade"):
                for cb in self._price_callbacks:
                    await cb(event) if asyncio.iscoroutinefunction(cb) else cb(event)
            elif event_type == "book_update":
                for cb in self._book_callbacks:
                    await cb(event) if asyncio.iscoroutinefunction(cb) else cb(event)
            elif event_type == "trade":
                for cb in self._trade_callbacks:
                    await cb(event) if asyncio.iscoroutinefunction(cb) else cb(event)
        except Exception as e:
            logger.error(f"Error in event callback: {e}")

    async def _rest_fallback_generator(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Fallback REST polling generator when WebSocket is unavailable.

        Polls subscribed markets every 10 seconds via Gamma API.

        Yields:
            Synthetic price update events
        """
        logger.info("Starting REST fallback polling")
        session = await self._get_http_session()

        while not self._connected and self._running and self._subscribed_condition_ids:
            for market_id in self._subscribed_condition_ids:
                try:
                    url = f"{GAMMA_API_BASE}/markets/{market_id}"
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            from .utils import safe_json_parse
                            prices = safe_json_parse(data.get("outcomePrices"))
                            if prices and len(prices) >= 1:
                                price = float(prices[0])
                                self._latest_prices[market_id] = price
                                yield {
                                    "type": "price_update",
                                    "market_id": market_id,
                                    "price": price,
                                    "source": "rest_fallback",
                                    "timestamp": int(time.time()),
                                }
                except Exception as e:
                    logger.debug(f"REST fallback error for {market_id}: {e}")

            # Wait before next polling cycle (try to reconnect WS in background)
            await asyncio.sleep(10)

            # Attempt to reconnect WebSocket
            if await self.connect():
                logger.info("WebSocket reconnected from REST fallback")
                break

    async def reconnect_with_backoff(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        delay = RECONNECT_DELAYS[min(self._reconnect_attempt, len(RECONNECT_DELAYS) - 1)]
        logger.info(
            f"Reconnecting WebSocket in {delay}s "
            f"(attempt {self._reconnect_attempt + 1})"
        )
        self._reconnect_attempt += 1
        await asyncio.sleep(delay)
        await self.connect()

    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        self._running = False
        self._connected = False
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("WebSocket disconnected")

    def get_latest_price(self, market_id: str) -> Optional[float]:
        """
        Get the most recent cached price for a market.

        Args:
            market_id: Market condition ID

        Returns:
            Latest price or None if not cached
        """
        return self._latest_prices.get(market_id)

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is currently connected."""
        return self._connected and self._ws is not None and not self._ws.closed
