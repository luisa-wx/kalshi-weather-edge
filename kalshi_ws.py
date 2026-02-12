"""
Kalshi WebSocket Streaming Client.

Connects to wss://api.elections.kalshi.com/trade-api/ws/v2
Subscribes to ticker and orderbook_delta channels for all bracket tickers.
Updates bracket prices and orderbook state in memory in real-time.

Usage:
    from kalshi_ws import KalshiWebSocket
    
    ws = KalshiWebSocket(kalshi_client, brackets)
    asyncio.create_task(ws.run())  # runs forever, reconnects on failure
"""

import asyncio
import base64
import json
import logging
import time
from typing import Dict, List, Optional, Callable

logger = logging.getLogger("kalshi_ws")

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_URL_DEMO = "wss://demo-api.kalshi.co/trade-api/ws/v2"


class KalshiWebSocket:
    """Persistent websocket connection to Kalshi for real-time market data."""

    def __init__(
        self,
        kalshi_client,
        brackets: Dict,
        on_ticker_update: Optional[Callable] = None,
        on_orderbook_update: Optional[Callable] = None,
        demo: bool = False,
    ):
        self.client = kalshi_client
        self.brackets = brackets  # shared reference to BRACKETS dict
        self.ws_url = WS_URL_DEMO if demo else WS_URL
        self.on_ticker_update = on_ticker_update
        self.on_orderbook_update = on_orderbook_update

        self.ws = None
        self.msg_id = 1
        self.connected = False
        self.subscribed_tickers: List[str] = []

        # In-memory orderbook state: {ticker: {"yes": [...], "no": [...]}}
        self.orderbooks: Dict[str, dict] = {}

        # Stats
        self.stats = {
            "ticker_updates": 0,
            "orderbook_updates": 0,
            "reconnects": 0,
            "errors": 0,
        }

    def _get_all_tickers(self) -> List[str]:
        """Extract all market tickers from loaded brackets."""
        tickers = []
        for station, data in self.brackets.items():
            for signal_type in ("high", "low"):
                for b in data.get(signal_type, []):
                    if b.ticker:
                        tickers.append(b.ticker)
        return tickers

    def _get_auth_headers(self) -> dict:
        """Generate authentication headers for websocket connection."""
        if not self.client or not self.client.private_key:
            return {}

        timestamp_ms = int(time.time() * 1000)
        path = "/trade-api/ws/v2"
        signature = self.client._sign_request(timestamp_ms, "GET", path)

        return {
            "KALSHI-ACCESS-KEY": self.client.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        }

    def _find_bracket(self, ticker: str):
        """Find a Bracket object by ticker."""
        for station, data in self.brackets.items():
            for signal_type in ("high", "low"):
                for b in data.get(signal_type, []):
                    if b.ticker == ticker:
                        return b
        return None

    def _parse_price_cents(self, price_val) -> int:
        """Parse a Kalshi price value into cents."""
        if price_val is None:
            return 0
        try:
            if isinstance(price_val, str):
                return int(float(price_val) * 100)
            elif isinstance(price_val, (int, float)):
                if price_val < 2:  # It's in dollars (e.g. 0.95)
                    return int(price_val * 100)
                return int(price_val)  # Already cents
        except (ValueError, TypeError):
            return 0
        return 0

    async def _subscribe(self):
        """Subscribe to ticker and orderbook_delta for all bracket tickers."""
        tickers = self._get_all_tickers()
        if not tickers:
            logger.warning("No tickers to subscribe to")
            return

        self.subscribed_tickers = tickers
        logger.info(f"Subscribing to {len(tickers)} tickers...")

        # Subscribe to ticker (public, all markets)
        ticker_msg = {
            "id": self.msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["ticker"],
                "market_tickers": tickers,
            },
        }
        await self.ws.send(json.dumps(ticker_msg))
        self.msg_id += 1

        # Subscribe to orderbook_delta (private, per market)
        orderbook_msg = {
            "id": self.msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        }
        await self.ws.send(json.dumps(orderbook_msg))
        self.msg_id += 1

        logger.info(f"Subscribed to ticker + orderbook_delta for {len(tickers)} markets")

    def _handle_ticker(self, msg: dict):
        """Process a ticker update — update bracket yes_ask/no_ask in memory."""
        ticker = msg.get("market_ticker", "")
        bracket = self._find_bracket(ticker)
        if not bracket:
            return

        yes_bid = self._parse_price_cents(msg.get("yes_bid"))
        yes_ask = self._parse_price_cents(msg.get("yes_ask"))

        if yes_bid > 0:
            bracket.yes_bid = yes_bid
        if yes_ask > 0:
            bracket.yes_ask = yes_ask
        # No bid/ask are complements
        if yes_bid > 0:
            bracket.no_ask = 100 - yes_bid
        if yes_ask > 0:
            bracket.no_bid = 100 - yes_ask

        self.stats["ticker_updates"] += 1

        if self.on_ticker_update:
            self.on_ticker_update(ticker, bracket)

    def _handle_orderbook_snapshot(self, msg: dict):
        """Process full orderbook snapshot."""
        ticker = msg.get("market_ticker", "")

        # Store the full orderbook
        self.orderbooks[ticker] = {
            "yes": msg.get("yes", []),
            "no": msg.get("no", []),
            "yes_dollars": msg.get("yes_dollars", []),
            "no_dollars": msg.get("no_dollars", []),
        }

        # Update bracket best prices from orderbook
        self._update_bracket_from_orderbook(ticker)
        self.stats["orderbook_updates"] += 1

        if self.on_orderbook_update:
            self.on_orderbook_update(ticker, self.orderbooks[ticker])

    def _handle_orderbook_delta(self, msg: dict):
        """Process orderbook delta — apply changes to snapshot."""
        ticker = msg.get("market_ticker", "")

        if ticker not in self.orderbooks:
            # Got delta before snapshot — ignore
            return

        # Apply delta to stored orderbook
        price = msg.get("price")
        delta = msg.get("delta")
        side = msg.get("side")  # "yes" or "no"

        if price is not None and delta is not None and side:
            book = self.orderbooks[ticker]
            levels = book.get(side, [])

            # Find and update the price level
            found = False
            for i, level in enumerate(levels):
                if level[0] == price:
                    new_qty = level[1] + delta
                    if new_qty <= 0:
                        levels.pop(i)
                    else:
                        levels[i] = [price, new_qty]
                    found = True
                    break

            if not found and delta > 0:
                levels.append([price, delta])
                levels.sort(key=lambda x: x[0], reverse=True)

        self._update_bracket_from_orderbook(ticker)
        self.stats["orderbook_updates"] += 1

        if self.on_orderbook_update:
            self.on_orderbook_update(ticker, self.orderbooks.get(ticker, {}))

    def _update_bracket_from_orderbook(self, ticker: str):
        """Update bracket best ask prices from orderbook state."""
        bracket = self._find_bracket(ticker)
        if not bracket:
            return

        book = self.orderbooks.get(ticker, {})

        # Best YES ask = lowest ask on yes side
        yes_levels = book.get("yes", [])
        if yes_levels:
            # Levels are [price, count], sorted by price desc
            # Lowest ask = last entry
            best_yes_ask = min(level[0] for level in yes_levels if level[1] > 0) if yes_levels else 0
            if best_yes_ask > 0:
                bracket.yes_ask = self._parse_price_cents(best_yes_ask)

        # Best NO ask = lowest ask on no side
        no_levels = book.get("no", [])
        if no_levels:
            best_no_ask = min(level[0] for level in no_levels if level[1] > 0) if no_levels else 0
            if best_no_ask > 0:
                bracket.no_ask = self._parse_price_cents(best_no_ask)

    async def _process_messages(self):
        """Main message loop."""
        async for message in self.ws:
            try:
                data = json.loads(message)
                msg_type = data.get("type", "")

                if msg_type == "ticker":
                    self._handle_ticker(data.get("msg", {}))

                elif msg_type == "orderbook_snapshot":
                    self._handle_orderbook_snapshot(data.get("msg", {}))

                elif msg_type == "orderbook_delta":
                    self._handle_orderbook_delta(data.get("msg", {}))

                elif msg_type == "subscribed":
                    sid = data.get("msg", {}).get("sid")
                    channel = data.get("msg", {}).get("channel")
                    logger.info(f"✅ Subscribed to {channel} (sid={sid})")

                elif msg_type == "error":
                    code = data.get("msg", {}).get("code")
                    err_msg = data.get("msg", {}).get("msg")
                    logger.error(f"Kalshi WS error {code}: {err_msg}")
                    self.stats["errors"] += 1

            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from Kalshi WS: {message[:100]}")
            except Exception as e:
                logger.error(f"Error processing Kalshi WS message: {e}")
                self.stats["errors"] += 1

    async def run(self):
        """Main loop — connect, subscribe, process, reconnect on failure."""
        import websockets

        while True:
            try:
                headers = self._get_auth_headers()
                logger.info(f"Connecting to Kalshi WebSocket at {self.ws_url}...")

                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    logger.info("✅ Kalshi WebSocket connected")

                    await self._subscribe()
                    await self._process_messages()

            except Exception as e:
                logger.error(f"Kalshi WS connection error: {e}")
                self.stats["errors"] += 1

            self.connected = False
            self.stats["reconnects"] += 1
            wait = min(30, 2 ** min(self.stats["reconnects"], 5))
            logger.info(f"Reconnecting in {wait}s (attempt #{self.stats['reconnects']})...")
            await asyncio.sleep(wait)

    def get_orderbook(self, ticker: str) -> dict:
        """Get current orderbook for a ticker."""
        return self.orderbooks.get(ticker, {"yes": [], "no": []})

    def get_stats(self) -> dict:
        return {
            **self.stats,
            "connected": self.connected,
            "subscribed_count": len(self.subscribed_tickers),
            "orderbooks_cached": len(self.orderbooks),
        }
