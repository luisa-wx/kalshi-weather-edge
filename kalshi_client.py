"""
Kalshi API Client for WX Sniper v2.

Handles RSA-PSS authentication, market data retrieval, bracket loading,
and order execution. Extracted from v1 smart_poller.py.

Environment variables:
    KALSHI_API_KEY_ID   — API key identifier
    KALSHI_PRIVATE_KEY  — RSA private key (PEM format)
    LIVE_MODE           — "true" for real trading
"""

import base64
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger("kalshi_client")

# ---------------------------------------------------------------------------
# Kalshi REST Client
# ---------------------------------------------------------------------------

class KalshiClient:
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self):
        self.api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
        self.private_key_str = os.environ.get("KALSHI_PRIVATE_KEY", "")

        # Optimised HTTP session (persistent connections)
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=0)
        self.session.mount("https://", adapter)

        self.last_latency_ms: float = 0
        self.private_key = None

        if self.api_key_id and self.private_key_str:
            self.private_key = self._load_private_key()
            logger.info("Kalshi credentials loaded (key_id=%s…)", self.api_key_id[:8])
        else:
            logger.warning("No Kalshi API credentials — dry-run only")

    # -- key loading ----------------------------------------------------------

    def _load_private_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend

        key_str = self.private_key_str.strip().replace("\\n", "\n").replace("\\r", "")

        if "\n" not in key_str and "PRIVATE KEY" in key_str:
            match = re.search(
                r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)\s*"
                r"([A-Za-z0-9+/=\s]+?)\s*"
                r"(-----END [A-Z ]*PRIVATE KEY-----)",
                key_str,
            )
            if match:
                header, content, footer = match.groups()
                content = "".join(content.split())
                lines = [content[i : i + 64] for i in range(0, len(content), 64)]
                key_str = header + "\n" + "\n".join(lines) + "\n" + footer + "\n"

        if not key_str.endswith("\n"):
            key_str += "\n"

        return serialization.load_pem_private_key(
            key_str.encode(), password=None, backend=default_backend()
        )

    # -- request signing ------------------------------------------------------

    def _sign_request(self, timestamp_ms: int, method: str, path: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        message = f"{timestamp_ms}{method}{path}"
        signature = self.private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    # -- HTTP -----------------------------------------------------------------

    def _make_request(
        self, method: str, endpoint: str, params: Dict = None, data: Dict = None
    ) -> Dict:
        start = time.perf_counter()

        timestamp_ms = int(time.time() * 1000)
        sign_path = f"/trade-api/v2{endpoint}"
        url = f"{self.BASE_URL}{endpoint}"

        if params:
            query_string = "&".join(f"{k}={v}" for k, v in params.items())
            sign_path = f"/trade-api/v2{endpoint}?{query_string}"
            url = f"{self.BASE_URL}{endpoint}?{query_string}"

        signature = self._sign_request(timestamp_ms, method.upper(), sign_path)

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "Content-Type": "application/json",
        }

        if method.upper() == "GET":
            resp = self.session.get(url, headers=headers, timeout=10)
        elif method.upper() == "POST":
            resp = self.session.post(url, headers=headers, json=data, timeout=10)
        else:
            raise ValueError(f"Unsupported method: {method}")

        self.last_latency_ms = (time.perf_counter() - start) * 1000
        resp.raise_for_status()
        return resp.json()

    # -- public API methods ---------------------------------------------------

    def get_exchange_status(self) -> Dict:
        return self._make_request("GET", "/exchange/status")

    def get_event(self, event_ticker: str) -> Dict:
        return self._make_request(
            "GET", f"/events/{event_ticker}", params={"with_nested_markets": "true"}
        )

    def get_market(self, ticker: str) -> Dict:
        return self._make_request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 5) -> Dict:
        """Get the live orderbook for a market. Returns yes/no bid/ask levels."""
        return self._make_request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": str(depth)}
        )

    def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        order_type: str = "limit",
        price_cents: int = None,
    ) -> Dict:
        data = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }
        if order_type == "limit" and price_cents is not None:
            if side == "yes":
                data["yes_price"] = price_cents
            else:
                data["no_price"] = price_cents

        logger.info("ORDER: %s %s %s x%d @ %s¢", action, side, ticker, count, price_cents)
        result = self._make_request("POST", "/portfolio/orders", data=data)
        logger.info("ORDER OK: latency=%dms", self.last_latency_ms)
        return result


# ---------------------------------------------------------------------------
# Bracket State
# ---------------------------------------------------------------------------

class Bracket:
    """Tracks a single Kalshi bracket (market)."""

    def __init__(
        self,
        ticker: str,
        subtitle: str,
        floor_strike: Optional[int],
        cap_strike: Optional[int],
        strike_type: str,
        signal_type: str,   # 'high' or 'low'
        station: str,
    ):
        self.ticker = ticker
        self.subtitle = subtitle
        self.floor_strike = floor_strike
        self.cap_strike = cap_strike
        self.strike_type = strike_type
        self.signal_type = signal_type
        self.station = station

        self.yes_ask: int = 0
        self.no_ask: int = 0
        self.yes_bid: int = 0
        self.no_bid: int = 0
        self.status: str = "open"    # open / locked / dead
        self.traded: bool = False

    # Alias: default check_temp uses intraday (elimination only)
    def check_temp(self, temp: int) -> str:
        return self.check_temp_intraday(temp)

    def check_temp_intraday(self, temp: int) -> str:
        """
        ELIMINATION ONLY. Given an intraday observation, determine if this
        bracket is provably DEAD (can never win).
        
        Returns 'dead' or 'open'. NEVER returns 'locked' — we don't assert
        winners from intraday data. Winners are inferred by exclusion 
        (last-man-standing) at a higher level.
        
        HIGH markets (observed_high can only go UP):
          - between: DEAD when observed > cap (high already past ceiling)
          - greater: NEVER dead (high might still reach floor)
          - less: DEAD when observed >= cap (high already too high)
        
        LOW markets (observed_low can only go DOWN):
          - between: DEAD when observed < floor (low already below floor)
          - greater: DEAD when observed <= floor (low already at/below floor)
          - less: NEVER dead (low dropping helps it)
        """
        if self.signal_type == "high":
            return self._eliminate_high(temp)
        elif self.signal_type == "low":
            return self._eliminate_low(temp)
        return "open"

    def _eliminate_high(self, observed_high: int) -> str:
        """Can we prove this HIGH bracket is dead given observed high?"""
        if self.strike_type == "between":
            # "72° to 73°" cap=73: dead if high already > 73
            if self.cap_strike is not None and observed_high > self.cap_strike:
                return "dead"
        elif self.strike_type in ("greater", "greater_or_equal"):
            # "80° or above" floor=79: NEVER dead — high might still rise
            pass
        elif self.strike_type in ("less", "less_or_equal"):
            # "70° or below" cap=71: dead if high already >= 71
            if self.cap_strike is not None and observed_high >= self.cap_strike:
                return "dead"
        return "open"

    def _eliminate_low(self, observed_low: int) -> str:
        """Can we prove this LOW bracket is dead given observed low?"""
        if self.strike_type == "between":
            # "35° to 36°" floor=35: dead if low already < 35
            if self.floor_strike is not None and observed_low < self.floor_strike:
                return "dead"
        elif self.strike_type in ("greater", "greater_or_equal"):
            # "37° or above" floor=36: dead if low already <= 36
            if self.floor_strike is not None and observed_low <= self.floor_strike:
                return "dead"
        elif self.strike_type in ("less", "less_or_equal"):
            # "34° or below" cap=35: NEVER dead — low dropping helps it
            pass
        return "open"

    def check_temp_final(self, temp: int) -> str:
        """
        Given a FINAL confirmed temperature, determine definitive outcome.
        Only used when we're 100% certain the value won't change (e.g. 
        last-man-standing, or manual override).
        """
        if self.signal_type == "high":
            return self._resolve_final_high(temp)
        elif self.signal_type == "low":
            return self._resolve_final_low(temp)
        return "open"

    def _resolve_final_high(self, high: int) -> str:
        """Resolve HIGH bracket given final high temperature."""
        if self.strike_type == "between":
            if self.floor_strike is not None and self.cap_strike is not None:
                if self.floor_strike <= high <= self.cap_strike:
                    return "locked"
                return "dead"
        elif self.strike_type in ("greater", "greater_or_equal"):
            if self.floor_strike is not None:
                return "locked" if high > self.floor_strike else "dead"
        elif self.strike_type in ("less", "less_or_equal"):
            if self.cap_strike is not None:
                return "locked" if high < self.cap_strike else "dead"
        return "open"

    def _resolve_final_low(self, low: int) -> str:
        """Resolve LOW bracket given final low temperature."""
        if self.strike_type == "between":
            if self.floor_strike is not None and self.cap_strike is not None:
                if self.floor_strike <= low <= self.cap_strike:
                    return "locked"
                return "dead"
        elif self.strike_type in ("greater", "greater_or_equal"):
            if self.floor_strike is not None:
                return "locked" if low > self.floor_strike else "dead"
        elif self.strike_type in ("less", "less_or_equal"):
            if self.cap_strike is not None:
                return "locked" if low < self.cap_strike else "dead"
        return "open"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "subtitle": self.subtitle,
            "floor": self.floor_strike,
            "cap": self.cap_strike,
            "strike_type": self.strike_type,
            "signal_type": self.signal_type,
            "station": self.station,
            "yes_ask": self.yes_ask,
            "no_ask": self.no_ask,
            "yes_bid": self.yes_bid,
            "no_bid": self.no_bid,
            "status": self.status,
        }

    def __repr__(self):
        return f"<Bracket {self.ticker} {self.subtitle} yes={self.yes_ask}¢ status={self.status}>"


# ---------------------------------------------------------------------------
# Bracket Loader — fetches all brackets for all stations from Kalshi
# ---------------------------------------------------------------------------

def _parse_price(price_raw, price_dollars) -> int:
    """Parse Kalshi API price into cents."""
    if price_dollars:
        try:
            val = int(float(price_dollars) * 100)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    if price_raw is not None:
        try:
            if isinstance(price_raw, str):
                val = int(float(price_raw) * 100)
            elif price_raw < 2:
                val = int(price_raw * 100)
            else:
                val = int(price_raw)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    return 0


def get_today_suffix() -> str:
    """Kalshi event date suffix like '26FEB11' in UTC. Use get_station_suffix for accuracy."""
    return datetime.now(timezone.utc).strftime("%y%b%d").upper()


def get_station_suffix(tz_name: str) -> str:
    """Kalshi event date suffix using station's LOCAL date.
    
    Kalshi weather markets settle on local calendar date, so the event
    ticker must match the station's local date, not UTC.
    E.g. at 11:30 PM PT on Feb 11, UTC is Feb 12, but the Kalshi market
    is still for Feb 11.
    """
    tz = ZoneInfo(tz_name)
    local_now = datetime.now(tz)
    return local_now.strftime("%y%b%d").upper()


def get_station_local_info(tz_name: str) -> dict:
    """Get station's local time, date, and suffix for display."""
    tz = ZoneInfo(tz_name)
    local_now = datetime.now(tz)
    return {
        "local_time": local_now.strftime("%I:%M %p"),
        "local_date": local_now.strftime("%m/%d"),
        "suffix": local_now.strftime("%y%b%d").upper(),
        "date_iso": local_now.strftime("%Y-%m-%d"),
    }


def load_brackets_for_station(
    client: KalshiClient,
    station: str,
    ticker_base: str,
    signal_type: str,
    today_suffix: str = None,
) -> List[Bracket]:
    """Load all brackets for one event (e.g. KXHIGHNY-26FEB11)."""
    if today_suffix is None:
        today_suffix = get_today_suffix()

    event_ticker = f"{ticker_base}-{today_suffix}"
    brackets = []

    try:
        event_data = client.get_event(event_ticker)
        event_obj = event_data.get("event", event_data)
        markets = event_obj.get("markets", [])

        for m in markets:
            floor_val = m.get("floor_strike")
            cap_val = m.get("cap_strike")
            b = Bracket(
                ticker=m.get("ticker", ""),
                subtitle=m.get("yes_sub_title", m.get("subtitle", "")),
                floor_strike=int(floor_val) if floor_val is not None else None,
                cap_strike=int(cap_val) if cap_val is not None else None,
                strike_type=m.get("strike_type", "between"),
                signal_type=signal_type,
                station=station,
            )
            b.yes_ask = _parse_price(m.get("yes_ask"), m.get("yes_ask_dollars"))
            b.no_ask = _parse_price(m.get("no_ask"), m.get("no_ask_dollars"))
            b.yes_bid = _parse_price(m.get("yes_bid"), m.get("yes_bid_dollars"))
            b.no_bid = _parse_price(m.get("no_bid"), m.get("no_bid_dollars"))
            brackets.append(b)

        logger.info(
            "%s %s: %d brackets loaded (event=%s)",
            station,
            signal_type.upper(),
            len(brackets),
            event_ticker,
        )
    except Exception as e:
        logger.error("%s %s: failed to load — %s", station, signal_type.upper(), e)

    return brackets


def load_all_brackets(client: KalshiClient, stations: dict) -> Dict[str, Dict[str, List[Bracket]]]:
    """
    Load all brackets for all stations, using each station's LOCAL date
    for the event ticker suffix.

    Returns: {station_code: {"high": [Bracket, ...], "low": [Bracket, ...], "suffix": "26FEB11", "local_info": {...}}}
    """
    result = {}

    for station, cfg in stations.items():
        tz_name = cfg.get("tz_name", "America/New_York")
        suffix = get_station_suffix(tz_name)
        local_info = get_station_local_info(tz_name)

        result[station] = {"high": [], "low": [], "suffix": suffix, "local_info": local_info}

        high_ticker = cfg.get("high_ticker")
        if high_ticker:
            result[station]["high"] = load_brackets_for_station(
                client, station, high_ticker, "high", suffix
            )

        low_ticker = cfg.get("low_ticker")
        if low_ticker:
            result[station]["low"] = load_brackets_for_station(
                client, station, low_ticker, "low", suffix
            )

    return result


# ---------------------------------------------------------------------------
# Opportunity detection
# ---------------------------------------------------------------------------

def find_opportunities(
    brackets: Dict[str, Dict[str, List[Bracket]]],
    cli_high: int = None,
    cli_low: int = None,
    station: str = None,
    max_price: int = 98,
) -> List[dict]:
    """
    ELIMINATION-BASED opportunity detection.

    Strategy:
    1. BUY NO on dead brackets priced below max_price (safe: bracket provably can't win)
    2. LAST-MAN-STANDING: if all but one bracket in a group are dead,
       the survivor wins by exclusion → BUY YES on it if priced below max_price

    We NEVER BUY YES on a bracket just because the CLI temp falls in its range.
    That would be gambling on the temp not changing further.
    """
    opportunities = []

    if station and station in brackets:
        station_brackets = brackets[station]
    else:
        return opportunities

    # Process each signal type (high, low) separately
    for signal_type, temp in [("high", cli_high), ("low", cli_low)]:
        if temp is None:
            continue

        group = station_brackets.get(signal_type, [])
        if not group:
            continue

        # Run elimination on all brackets
        alive = []
        dead_list = []
        for b in group:
            result = b.check_temp_intraday(temp)
            if result == "dead":
                dead_list.append(b)
            else:
                alive.append(b)

        # Opportunity 1: BUY NO on dead brackets that aren't already at 99-100¢
        for b in dead_list:
            if b.no_ask > 0 and b.no_ask <= max_price:
                opportunities.append({
                    "action": "BUY_NO",
                    "bracket": b,
                    "price": b.no_ask,
                    "reason": (
                        f"CLI {signal_type.upper()}={temp}°F eliminates "
                        f"{b.subtitle} → DEAD (no_ask={b.no_ask}¢)"
                    ),
                    "edge_cents": 100 - b.no_ask,
                })

        # Opportunity 2: LAST-MAN-STANDING
        # If exactly 1 bracket survives elimination, it MUST win
        if len(alive) == 1:
            survivor = alive[0]
            if survivor.yes_ask > 0 and survivor.yes_ask <= max_price:
                opportunities.append({
                    "action": "BUY_YES",
                    "bracket": survivor,
                    "price": survivor.yes_ask,
                    "reason": (
                        f"LAST-MAN-STANDING: CLI {signal_type.upper()}={temp}°F "
                        f"eliminated {len(dead_list)} brackets, only "
                        f"{survivor.subtitle} survives (yes_ask={survivor.yes_ask}¢)"
                    ),
                    "edge_cents": 100 - survivor.yes_ask,
                })

    return sorted(opportunities, key=lambda x: x["edge_cents"], reverse=True)


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

def execute_snipe(
    client: KalshiClient,
    opportunity: dict,
    order_size: int = 10,
    live_mode: bool = False,
    processed_tickers: set = None,
    kalshi_ws=None,
) -> dict:
    """
    Execute a snipe trade for an opportunity.
    
    If kalshi_ws is provided, checks the live orderbook for actual liquidity
    before firing. Only trades if there's real volume to fill against.
    
    For BUY_NO on dead brackets: looks for YES bids (we're taking the other side)
    For BUY_YES on last-man-standing: looks for YES asks below 99¢
    """
    bracket = opportunity["bracket"]

    if processed_tickers and bracket.ticker in processed_tickers:
        logger.warning("SKIP: %s already traded", bracket.ticker)
        return {"success": False, "reason": "duplicate"}

    side = "yes" if opportunity["action"] == "BUY_YES" else "no"
    
    # Check live orderbook for real liquidity
    book_info = {}
    execution_price = 99  # Default: max bid to guarantee fill
    available_qty = 0
    
    if kalshi_ws:
        book = kalshi_ws.get_orderbook(bracket.ticker)
        
        if opportunity["action"] == "BUY_NO":
            # We want to buy NO. Check:
            # 1. YES bids = people willing to buy YES on a dead bracket (we sell NO into them)
            # 2. NO asks = people offering to sell NO contracts to us
            yes_bids = book.get("yes", [])
            no_asks = book.get("no", [])
            
            # Count available YES bid liquidity (free money — someone bidding YES on dead bracket)
            for level in yes_bids:
                price, qty = level[0], level[1]
                price_cents = int(float(price) * 100) if isinstance(price, str) else (int(price * 100) if price < 2 else int(price))
                if price_cents > 1:  # Any YES bid > 1¢ on a dead bracket is profit
                    available_qty += int(float(qty)) if isinstance(qty, str) else int(qty)
            
            # Also check NO asks
            best_no_ask = None
            for level in no_asks:
                price, qty = level[0], level[1]
                price_cents = int(float(price) * 100) if isinstance(price, str) else (int(price * 100) if price < 2 else int(price))
                if best_no_ask is None or price_cents < best_no_ask:
                    best_no_ask = price_cents
                    
            book_info = {
                "yes_bid_levels": len(yes_bids),
                "yes_bid_qty": available_qty,
                "best_no_ask": best_no_ask,
            }
            
            if best_no_ask and best_no_ask < 99:
                execution_price = min(99, best_no_ask)  # Match the ask
                
        elif opportunity["action"] == "BUY_YES":
            # Last-man-standing: buy YES
            yes_asks = book.get("yes", [])  # These are ask levels
            
            best_yes_ask = None
            for level in yes_asks:
                price, qty = level[0], level[1]
                price_cents = int(float(price) * 100) if isinstance(price, str) else (int(price * 100) if price < 2 else int(price))
                if best_yes_ask is None or price_cents < best_yes_ask:
                    best_yes_ask = price_cents
                    available_qty += int(float(qty)) if isinstance(qty, str) else int(qty)
                    
            book_info = {
                "best_yes_ask": best_yes_ask,
                "available_qty": available_qty,
            }
            
            if best_yes_ask and best_yes_ask < 99:
                execution_price = min(99, best_yes_ask)

    # Determine actual order quantity
    actual_qty = min(order_size, available_qty) if available_qty > 0 else order_size

    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "station": bracket.station,
        "ticker": bracket.ticker,
        "subtitle": bracket.subtitle,
        "action": opportunity["action"],
        "side": side,
        "price": execution_price,
        "quantity": actual_qty,
        "original_ask": opportunity["price"],
        "edge_cents": 100 - execution_price,
        "reason": opportunity["reason"],
        "book_info": book_info,
        "live": live_mode,
        "success": False,
    }

    # Skip if no liquidity found
    if kalshi_ws and available_qty == 0 and not live_mode:
        record["reason"] += " [NO LIQUIDITY — skipped]"
        logger.info("📭 NO LIQUIDITY: %s %s %s — orderbook empty", opportunity["action"], bracket.subtitle, bracket.ticker)
        return record

    if live_mode and client.private_key:
        try:
            result = client.create_order(
                ticker=bracket.ticker,
                side=side,
                action="buy",
                count=actual_qty,
                order_type="limit",
                price_cents=execution_price,
            )
            record["success"] = True
            record["order_id"] = result.get("order", {}).get("order_id")
            record["latency_ms"] = client.last_latency_ms
            logger.info(
                "🎯 FILLED: %s %s @ %d¢ x%d (edge=%d¢)",
                side, bracket.ticker, execution_price, actual_qty, 100 - execution_price,
            )
        except Exception as e:
            record["error"] = str(e)
            logger.error("❌ ORDER FAILED: %s — %s", bracket.ticker, e)
    else:
        record["success"] = True
        logger.info(
            "🧪 DRY RUN: %s %s %s @ %d¢ x%d (book: %s)",
            opportunity["action"], side, bracket.ticker, execution_price, actual_qty,
            json.dumps(book_info) if book_info else "no ws",
        )

    bracket.traded = True
    if processed_tickers is not None:
        processed_tickers.add(bracket.ticker)

    return record
