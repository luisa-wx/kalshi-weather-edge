"""
Kalshi API Client

Handles authentication and trading operations for the Kalshi exchange.
Uses RSA-PSS signatures for API authentication.
"""

import time
import json
import base64
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

from config import KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY, KALSHI_BASE_URL


class KalshiClient:
    def __init__(self):
        self.api_key_id = KALSHI_API_KEY_ID
        self.base_url = KALSHI_BASE_URL
        self.private_key = self._load_private_key()
        
    def _load_private_key(self):
        """Load RSA private key from PEM string"""
        # Handle escaped newlines from environment variables
        key_str = KALSHI_PRIVATE_KEY
        if '\\n' in key_str:
            key_str = key_str.replace('\\n', '\n')
        return serialization.load_pem_private_key(
            key_str.encode(),
            password=None,
            backend=default_backend()
        )
    
    def _sign_request(self, timestamp_ms: int, method: str, path: str) -> str:
        """
        Create RSA-PSS signature for API authentication
        
        The signature is over: timestamp + method + path
        """
        message = f"{timestamp_ms}{method}{path}"
        signature = self.private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode()
    
    def _make_request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        """Make authenticated request to Kalshi API"""
        timestamp_ms = int(time.time() * 1000)
        path = endpoint
        
        # Build full URL
        url = f"{self.base_url}{endpoint}"
        
        # Add query params to path for signature if present
        if params:
            query_string = "&".join(f"{k}={v}" for k, v in params.items())
            path = f"{endpoint}?{query_string}"
            url = f"{self.base_url}{path}"
        
        # Create signature
        signature = self._sign_request(timestamp_ms, method.upper(), path)
        
        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "Content-Type": "application/json"
        }
        
        try:
            if method.upper() == "GET":
                response = requests.get(url, headers=headers, timeout=10)
            elif method.upper() == "POST":
                response = requests.post(url, headers=headers, json=data, timeout=10)
            elif method.upper() == "DELETE":
                response = requests.delete(url, headers=headers, timeout=10)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            print(f"[KALSHI ERROR] {method} {endpoint}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"[KALSHI ERROR] Response: {e.response.text}")
            raise
    
    # =========== Account Methods ===========
    
    def get_balance(self) -> Dict:
        """Get account balance"""
        return self._make_request("GET", "/portfolio/balance")
    
    # =========== Market Discovery ===========
    
    def get_events(self, series_ticker: str = None, status: str = "open", limit: int = 100) -> List[Dict]:
        """
        Get events (containers for markets)
        
        For temperature markets, events are daily (e.g., KXHIGHTSFO-26JAN28)
        """
        params = {"limit": limit, "with_nested_markets": "true"}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
            
        result = self._make_request("GET", "/events", params=params)
        return result.get("events", [])
    
    def get_event(self, event_ticker: str) -> Dict:
        """Get single event with its markets"""
        result = self._make_request("GET", f"/events/{event_ticker}", 
                                    params={"with_nested_markets": "true"})
        return result
    
    def get_markets(self, event_ticker: str = None, status: str = "open", limit: int = 200) -> List[Dict]:
        """
        Get markets, optionally filtered by event
        
        For temperature, each market is a temperature bracket (e.g., "71°F or higher")
        """
        params = {"limit": limit}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
            
        result = self._make_request("GET", "/markets", params=params)
        return result.get("markets", [])
    
    def get_market(self, ticker: str) -> Dict:
        """Get single market by ticker"""
        result = self._make_request("GET", f"/markets/{ticker}")
        return result.get("market", {})
    
    def get_market_orderbook(self, ticker: str, depth: int = 10) -> Dict:
        """Get orderbook for a market"""
        return self._make_request("GET", f"/markets/{ticker}/orderbook", 
                                  params={"depth": depth})
    
    # =========== Trading ===========
    
    def create_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,  # Number of contracts
        order_type: str = "market",  # "market" or "limit"
        yes_price: int = None,  # Price in cents (1-99) for limit orders
        client_order_id: str = None
    ) -> Dict:
        """
        Create an order
        
        For our strategy:
        - We BUY YES on temperature brackets when we know the temp will hit
        - side="yes", action="buy"
        - For market orders, no price needed
        - For limit orders, set yes_price (aggressive = near ask)
        """
        data = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type
        }
        
        if order_type == "limit" and yes_price is not None:
            data["yes_price"] = yes_price
            
        if client_order_id:
            data["client_order_id"] = client_order_id
            
        return self._make_request("POST", "/portfolio/orders", data=data)
    
    def get_positions(self, event_ticker: str = None) -> List[Dict]:
        """Get current positions"""
        params = {}
        if event_ticker:
            params["event_ticker"] = event_ticker
        result = self._make_request("GET", "/portfolio/positions", params=params)
        return result.get("market_positions", [])
    
    def get_orders(self, status: str = None, ticker: str = None) -> List[Dict]:
        """Get orders"""
        params = {}
        if status:
            params["status"] = status
        if ticker:
            params["ticker"] = ticker
        result = self._make_request("GET", "/portfolio/orders", params=params)
        return result.get("orders", [])


# =========== Helper Functions ===========

def find_temperature_bracket(markets: List[Dict], target_temp_f: int, bracket_type: str = "high") -> Optional[Dict]:
    """
    Find the market for a specific temperature bracket
    
    For HIGH temps: we want the bracket that resolves YES if temp >= target
    For LOW temps: we want the bracket that resolves YES if temp <= target
    
    Returns the market dict or None if not found
    """
    for market in markets:
        # Parse the market title/subtitle to find temperature
        # Format varies but usually includes the temperature value
        title = market.get("title", "") + " " + market.get("yes_sub_title", "")
        
        # Look for temperature patterns like "71°" or "71 degrees" or "T71"
        import re
        temp_match = re.search(r'(\d+)\s*°?(?:F|degrees)?', title, re.IGNORECASE)
        if temp_match:
            market_temp = int(temp_match.group(1))
            if market_temp == target_temp_f:
                return market
                
        # Also check ticker for temperature encoding
        ticker = market.get("ticker", "")
        # Might be encoded like KXHIGHTSFO-26JAN28-B71 or -T71
        ticker_match = re.search(r'[BT](\d+)$', ticker)
        if ticker_match:
            market_temp = int(ticker_match.group(1))
            if market_temp == target_temp_f:
                return market
    
    return None


def format_market_info(market: Dict) -> str:
    """Format market info for display"""
    return (
        f"Ticker: {market.get('ticker')}\n"
        f"Title: {market.get('title')}\n"
        f"Yes Sub: {market.get('yes_sub_title')}\n"
        f"Status: {market.get('status')}\n"
        f"Yes Bid: {market.get('yes_bid')}¢ | Yes Ask: {market.get('yes_ask')}¢\n"
        f"Last: {market.get('last_price')}¢ | Volume: {market.get('volume')}"
    )


# =========== Test/Demo ===========

if __name__ == "__main__":
    print("=" * 60)
    print("KALSHI API CLIENT TEST")
    print("=" * 60)
    
    client = KalshiClient()
    
    # Test 1: Get balance
    print("\n[TEST 1] Getting account balance...")
    try:
        balance = client.get_balance()
        print(f"Balance: ${balance.get('balance', 0) / 100:.2f}")
        print(f"Portfolio Value: ${balance.get('portfolio_value', 0) / 100:.2f}")
    except Exception as e:
        print(f"Error: {e}")
    
    # Test 2: Find SFO temperature events
    print("\n[TEST 2] Finding SFO high temperature events...")
    try:
        # Try to find events for SFO high temps
        events = client.get_events(series_ticker="KXHIGHTSFO", limit=5)
        print(f"Found {len(events)} events")
        for event in events[:3]:
            print(f"\n  Event: {event.get('event_ticker')}")
            print(f"  Title: {event.get('title')}")
            markets = event.get('markets', [])
            print(f"  Markets: {len(markets)}")
            
            # Show first few markets
            for m in markets[:5]:
                print(f"    - {m.get('ticker')}: {m.get('yes_sub_title')} "
                      f"(bid:{m.get('yes_bid')}¢ ask:{m.get('yes_ask')}¢)")
    except Exception as e:
        print(f"Error: {e}")
    
    # Test 3: Find LAS temperature events
    print("\n[TEST 3] Finding Las Vegas high temperature events...")
    try:
        events = client.get_events(series_ticker="KXHIGHTLV", limit=5)
        print(f"Found {len(events)} events")
        for event in events[:3]:
            print(f"\n  Event: {event.get('event_ticker')}")
            print(f"  Title: {event.get('title')}")
            markets = event.get('markets', [])
            print(f"  Markets: {len(markets)}")
            
            for m in markets[:5]:
                print(f"    - {m.get('ticker')}: {m.get('yes_sub_title')} "
                      f"(bid:{m.get('yes_bid')}¢ ask:{m.get('yes_ask')}¢)")
    except Exception as e:
        print(f"Error: {e}")
    
    print("\n" + "=" * 60)
    print("TICKER FORMAT DISCOVERY")
    print("=" * 60)
    print("""
Based on the URL you provided: 
  https://kalshi.com/markets/kxhightsfo/san-francisco-high-temperature-daily/kxhightsfo-26jan28

The event ticker appears to be: KXHIGHTSFO-26JAN28 (base + date)

The individual market tickers (temperature brackets) will be shown above.
Look at the output to see the exact format for temperature brackets.
    """)
