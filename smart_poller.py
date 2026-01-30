#!/usr/bin/env python3
"""
WX Sniper v4.2 - Zero Strike Bug Fix
====================================

Changes from v4.1:
- Fixed parsing of floor_strike/cap_strike when value is 0 (was treating 0 as None)
  This caused brackets like "-1° to 0°" to have cap=None instead of cap=0

Changes from v4.0:
- Fixed metar_data reference bug (variable didn't exist in context)
- Added immediate METAR poll on startup so dashboard shows data right away
- Fixed price parsing to show 0¢ instead of 100¢ for missing data

STRATEGY SUMMARY:
We're a LATENCY SNIPER. When a METAR drops showing a new temperature reading,
we detect which brackets have TRANSITIONED to a resolved state before the 
market reprices.

BRACKET RESOLUTION RULES (CRITICAL - READ CAREFULLY):

=== HIGH MARKETS ===
The "observed high" can only go UP as the day progresses.

"X to Y" (between):
  - DEAD if observed_high > cap (exceeded the range, can never come back)
  - Can NEVER be locked (high could keep rising and exceed cap later)
  - Example: "21° to 22°" is DEAD once we see 23°F

"X or above" (greater, warm edge):
  - LOCKED YES once observed_high >= floor (we hit it, guaranteed win)
  - Can never be dead (high keeps getting higher, helps this bracket)
  - Example: "23° or above" is LOCKED YES once we see 23°F

"X or below" (less, cold edge):
  - DEAD if observed_high > cap (too warm, can never come back down)
  - Can NEVER be locked (high could keep rising)
  - Example: "18° or below" is DEAD once we see 19°F

=== LOW MARKETS ===
The "observed low" can only go DOWN as the day progresses.

"X to Y" (between):
  - DEAD if observed_low < floor (dropped below the range)
  - Can NEVER be locked (low could keep dropping)
  - Example: "5° to 6°" is DEAD once we see 4°F

"X or above" (greater, warm edge):
  - DEAD if observed_low < floor (dropped below, can never come back up)
  - Can NEVER be locked (low could still drop below floor later)
  - Example: "5° or above" is DEAD once we see 4°F

"X or below" (less, cold edge):
  - LOCKED YES once observed_low <= cap (we hit it, guaranteed win)  
  - Can never be dead (low keeps getting lower, helps this bracket)
  - Example: "4° or below" is LOCKED YES once we see 4°F

=== TRADING ACTIONS ===
- OPEN → DEAD: BUY NO (bracket can never win, NO will settle at $1)
- OPEN → LOCKED: BUY YES (bracket guaranteed to win, YES will settle at $1)
"""

import os
import re
import time
import json
import math
import threading
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================================
# CONFIGURATION
# ============================================================

STATIONS = {
    "KNYC": {
        "kalshi_high_ticker": "KXHIGHNY",
        "kalshi_low_ticker": "KXLOWTNYC",
        "timezone": "America/New_York",
        "name": "NYC"
    },
    "KPHL": {
        "kalshi_high_ticker": "KXHIGHPHIL",
        "kalshi_low_ticker": "KXLOWTPHIL",
        "timezone": "America/New_York",
        "name": "Philadelphia"
    },
    "KMDW": {
        "kalshi_high_ticker": "KXHIGHCHI",
        "kalshi_low_ticker": "KXLOWTCHI",
        "timezone": "America/Chicago",
        "name": "Chicago"
    },
    "KLAX": {
        "kalshi_high_ticker": "KXHIGHLAX",
        "kalshi_low_ticker": "KXLOWTLAX",
        "timezone": "America/Los_Angeles",
        "name": "Los Angeles"
    },
    "KMIA": {
        "kalshi_high_ticker": "KXHIGHMIA",
        "kalshi_low_ticker": "KXLOWTMIA",
        "timezone": "America/New_York",
        "name": "Miami"
    },
    "KAUS": {
        "kalshi_high_ticker": "KXHIGHAUS",
        "kalshi_low_ticker": "KXLOWTAUS",
        "timezone": "America/Chicago",
        "name": "Austin"
    },
    "KSFO": {
        "kalshi_high_ticker": "KXHIGHTSFO",
        "kalshi_low_ticker": None,
        "timezone": "America/Los_Angeles",
        "name": "San Francisco"
    },
    "KSEA": {
        "kalshi_high_ticker": "KXHIGHTSEA",
        "kalshi_low_ticker": None,
        "timezone": "America/Los_Angeles",
        "name": "Seattle"
    },
    "KDCA": {
        "kalshi_high_ticker": "KXHIGHTDC",
        "kalshi_low_ticker": None,
        "timezone": "America/New_York",
        "name": "Washington DC"
    },
    "KMSY": {
        "kalshi_high_ticker": "KXHIGHTNOLA",
        "kalshi_low_ticker": None,
        "timezone": "America/Chicago",
        "name": "New Orleans"
    },
    "KLAS": {
        "kalshi_high_ticker": "KXHIGHTLV",
        "kalshi_low_ticker": None,
        "timezone": "America/Los_Angeles",
        "name": "Las Vegas"
    },
    "KDEN": {
        "kalshi_high_ticker": "KXHIGHDEN",
        "kalshi_low_ticker": "KXLOWTDEN",
        "timezone": "America/Denver",
        "name": "Denver"
    }
}

# ============================================================
# METAR PARSING
# ============================================================

@dataclass
class ParsedMETAR:
    raw: str
    temp_c: Optional[float] = None
    temp_f: Optional[int] = None
    six_hr_max_c: Optional[float] = None
    six_hr_min_c: Optional[float] = None

def nws_round(temp_f: float) -> int:
    """NWS rounding: round half up (asymmetric)."""
    return math.floor(temp_f + 0.5)

def c_to_f_nws(temp_c: float) -> int:
    """Convert Celsius to Fahrenheit with NWS rounding."""
    return nws_round(temp_c * 9/5 + 32)

def parse_metar(raw: str) -> ParsedMETAR:
    """Parse METAR string for temperature data."""
    result = ParsedMETAR(raw=raw)
    
    # T-group: T01061000 = +10.6°C temp, +0.0°C dewpoint
    t_match = re.search(r'\bT(\d)(\d{3})(\d)(\d{3})\b', raw)
    if t_match:
        temp_sign = -1 if t_match.group(1) == '1' else 1
        result.temp_c = temp_sign * int(t_match.group(2)) / 10.0
        result.temp_f = nws_round(result.temp_c * 9/5 + 32)
    
    # 6-hour max (1-group): 1snTTT where sn=sign, TTT=tenths C
    # Must be in remarks section and be exactly 5 digits starting with 1
    rmk_idx = raw.find('RMK')
    if rmk_idx > 0:
        remarks = raw[rmk_idx:]
        max_match = re.search(r'\b1([01])(\d{3})\b', remarks)
        if max_match:
            sign = -1 if max_match.group(1) == '1' else 1
            result.six_hr_max_c = sign * int(max_match.group(2)) / 10.0
        
        # 6-hour min (2-group)
        min_match = re.search(r'\b2([01])(\d{3})\b', remarks)
        if min_match:
            sign = -1 if min_match.group(1) == '1' else 1
            result.six_hr_min_c = sign * int(min_match.group(2)) / 10.0
    
    return result

# ============================================================
# BRACKET STATE - THE CORRECTED LOGIC
# ============================================================

class BracketState:
    """
    Tracks state of a single bracket.
    
    CRITICAL: The logic here determines whether we make or lose money!
    """
    
    def __init__(self, ticker: str, subtitle: str, floor_strike: Optional[int], 
                 cap_strike: Optional[int], strike_type: str, signal_type: str, station: str):
        self.ticker = ticker
        self.subtitle = subtitle
        self.floor_strike = floor_strike
        self.cap_strike = cap_strike
        self.strike_type = strike_type  # 'greater', 'less', 'between'
        self.signal_type = signal_type  # 'high' or 'low'
        self.station = station
        
        # Prices
        self.no_ask: int = 100
        self.yes_ask: int = 100
        
        # State
        self.status: str = 'open'  # 'open', 'dead', 'locked'
        self.traded: bool = False
    
    def check_status(self, observed_high: Optional[int], observed_low: Optional[int]) -> str:
        """
        Determine current status based on observations.
        
        Returns: 'open', 'dead', or 'locked'
        
        REMEMBER:
        - observed_high can only go UP (warmest seen so far)
        - observed_low can only go DOWN (coldest seen so far)
        """
        
        # ========== HIGH MARKETS ==========
        if self.signal_type == 'high':
            if observed_high is None:
                return 'open'
            
            # --- "X to Y" (between) ---
            # YES wins if floor <= final_high <= cap
            # NO wins if final_high < floor OR final_high > cap
            if self.strike_type == 'between':
                # DEAD: observed already exceeded cap (can't go back down)
                if self.cap_strike is not None and observed_high > self.cap_strike:
                    return 'dead'
                # Can NEVER be locked - high could still exceed cap later
                return 'open'
            
            # --- "X or above" (greater) ---
            # YES wins if final_high >= floor
            # NO wins if final_high < floor
            elif self.strike_type == 'greater':
                # LOCKED: we hit the threshold
                if self.floor_strike is not None and observed_high >= self.floor_strike:
                    return 'locked'
                # Can never be dead - high keeps rising, which helps
                return 'open'
            
            # --- "X or below" (less) ---
            # YES wins if final_high <= cap
            # NO wins if final_high > cap
            elif self.strike_type == 'less':
                # DEAD: observed already exceeded cap
                if self.cap_strike is not None and observed_high > self.cap_strike:
                    return 'dead'
                # Can NEVER be locked - high could still rise
                return 'open'
        
        # ========== LOW MARKETS ==========
        elif self.signal_type == 'low':
            if observed_low is None:
                return 'open'
            
            # --- "X to Y" (between) ---
            # YES wins if floor <= final_low <= cap
            # NO wins if final_low < floor OR final_low > cap
            if self.strike_type == 'between':
                # DEAD: observed dropped below floor (can't go back up)
                if self.floor_strike is not None and observed_low < self.floor_strike:
                    return 'dead'
                # Can NEVER be locked - low could still drop below floor
                return 'open'
            
            # --- "X or above" (greater) ---
            # YES wins if final_low >= floor
            # NO wins if final_low < floor
            elif self.strike_type == 'greater':
                # DEAD: observed dropped below floor (can't go back up)
                if self.floor_strike is not None and observed_low < self.floor_strike:
                    return 'dead'
                # Can NEVER be locked - low could still drop
                return 'open'
            
            # --- "X or below" (less) ---
            # YES wins if final_low <= cap
            # NO wins if final_low > cap
            elif self.strike_type == 'less':
                # LOCKED: observed_low <= cap means final_low will also be <= cap
                # (because low can only go DOWN, so if we're already at or below cap, we stay there)
                if self.cap_strike is not None and observed_low <= self.cap_strike:
                    return 'locked'
                # DEAD: observed_low > cap means we haven't hit it YET
                # BUT WAIT - the low could still DROP to hit it later!
                # So it's NOT dead, it's still open
                return 'open'
        
        return 'open'
    
    def describe_resolution(self, observed_high: Optional[int], observed_low: Optional[int]) -> str:
        """Get human-readable explanation of why bracket resolved."""
        status = self.check_status(observed_high, observed_low)
        
        if self.signal_type == 'high':
            if status == 'dead':
                return f"HIGH {observed_high}°F > cap {self.cap_strike}°F (exceeded, can't drop)"
            elif status == 'locked':
                return f"HIGH {observed_high}°F >= floor {self.floor_strike}°F (threshold hit)"
        else:  # low
            if status == 'dead':
                return f"LOW {observed_low}°F < floor {self.floor_strike}°F (dropped, can't rise)"
            elif status == 'locked':
                return f"LOW {observed_low}°F <= cap {self.cap_strike}°F (threshold hit)"
        
        return "Still open"


# ============================================================
# STATION STATE
# ============================================================

@dataclass
class StationState:
    station: str
    observed_high: Optional[int] = None
    observed_low: Optional[int] = None
    latest_metar: Optional[str] = None
    latest_temp_f: Optional[int] = None  # Current temp from latest METAR
    metar_time: Optional[datetime] = None
    current_local_date: Optional[str] = None
    
    high_watchlist: List[BracketState] = field(default_factory=list)
    low_watchlist: List[BracketState] = field(default_factory=list)
    resolved_brackets: List[BracketState] = field(default_factory=list)


# ============================================================
# KALSHI CLIENT (simplified, inline)
# ============================================================

class KalshiClient:
    def __init__(self):
        self.api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
        self.private_key_str = os.environ.get("KALSHI_PRIVATE_KEY", "")
        self.base_url = "https://api.elections.kalshi.com/trade-api/v2"
        
        if self.api_key_id and self.private_key_str:
            self.private_key = self._load_private_key()
        else:
            self.private_key = None
            print("[KALSHI] WARNING: No API credentials found in environment")
    
    def _load_private_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        
        key_str = self.private_key_str.strip()
        
        # Debug: show what we received
        has_newlines = '\n' in key_str
        has_escaped_n = '\\n' in key_str
        print(f"[KALSHI] Raw key length: {len(key_str)}, contains newlines: {has_newlines}, contains backslash-n: {has_escaped_n}")
        
        # Handle literal \n from environment variables (very common in DO/Heroku)
        # This handles both \\n (escaped) and cases where shell passed literal backslash-n
        key_str = key_str.replace('\\n', '\n')
        key_str = key_str.replace('\\r', '')
        
        # Also try replacing space-separated PEM (sometimes happens with copy/paste)
        # e.g., "-----BEGIN RSA PRIVATE KEY----- MIIE... -----END RSA PRIVATE KEY-----"
        
        # If still no newlines and it looks like a PEM, reconstruct it
        if '\n' not in key_str and 'PRIVATE KEY' in key_str:
            import re
            
            # Try to extract header, content, footer
            # Handle both RSA PRIVATE KEY and PRIVATE KEY formats
            match = re.search(
                r'(-----BEGIN [A-Z ]*PRIVATE KEY-----)\s*'  # Header
                r'([A-Za-z0-9+/=\s]+?)\s*'                   # Base64 content (allow spaces)
                r'(-----END [A-Z ]*PRIVATE KEY-----)',       # Footer
                key_str
            )
            
            if match:
                header = match.group(1)
                content = match.group(2)
                footer = match.group(3)
                
                # Remove ALL whitespace from base64 content
                content = ''.join(content.split())
                
                # Split base64 into 64-char lines (PEM standard)
                lines = [content[i:i+64] for i in range(0, len(content), 64)]
                
                key_str = header + '\n' + '\n'.join(lines) + '\n' + footer + '\n'
                print(f"[KALSHI] Reconstructed PEM with {len(lines)} base64 lines")
            else:
                print(f"[KALSHI] WARNING: Could not parse PEM structure")
                print(f"[KALSHI] Key preview: {key_str[:80]}...")
        
        # Ensure ends with newline
        if not key_str.endswith('\n'):
            key_str += '\n'
        
        try:
            return serialization.load_pem_private_key(
                key_str.encode(),
                password=None,
                backend=default_backend()
            )
        except Exception as e:
            print(f"[KALSHI] Key parse FAILED: {e}")
            print(f"[KALSHI] Key starts: {repr(key_str[:60])}")
            print(f"[KALSHI] Key ends: {repr(key_str[-60:])}")
            raise
    
    def _sign_request(self, timestamp_ms: int, method: str, path: str) -> str:
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        
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
        import time
        
        timestamp_ms = int(time.time() * 1000)
        sign_path = f"/trade-api/v2{endpoint}"
        url = f"{self.base_url}{endpoint}"
        
        if params:
            query_string = "&".join(f"{k}={v}" for k, v in params.items())
            sign_path = f"/trade-api/v2{endpoint}?{query_string}"
            url = f"{self.base_url}{endpoint}?{query_string}"
        
        signature = self._sign_request(timestamp_ms, method.upper(), sign_path)
        
        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "Content-Type": "application/json"
        }
        
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, timeout=10)
        elif method.upper() == "POST":
            response = requests.post(url, headers=headers, json=data, timeout=10)
        else:
            raise ValueError(f"Unsupported method: {method}")
        
        response.raise_for_status()
        return response.json()
    
    def get_event(self, event_ticker: str) -> Dict:
        return self._make_request("GET", f"/events/{event_ticker}", 
                                  params={"with_nested_markets": "true"})
    
    def create_order(self, ticker: str, side: str, action: str, count: int, 
                     order_type: str = "limit", price_cents: int = None) -> Dict:
        data = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type
        }
        if order_type == "limit" and price_cents is not None:
            if side == "yes":
                data["yes_price"] = price_cents
            else:
                data["no_price"] = price_cents
        
        print(f"[KALSHI] Creating order: {data}")
        return self._make_request("POST", "/portfolio/orders", data=data)


# ============================================================
# MAIN SNIPER
# ============================================================

class WXSniper:
    def __init__(self):
        self.kalshi = KalshiClient()
        
        # Config from environment
        self.live_mode = os.environ.get('LIVE_MODE', 'false').lower() == 'true'
        self.max_price = int(os.environ.get('MAX_PRICE', '95'))
        
        # State per station
        self.states: Dict[str, StationState] = {}
        for station in STATIONS.keys():
            self.states[station] = StationState(station=station)
        
        # Tracking
        self.last_metar_poll: Optional[datetime] = None
        self.last_price_poll: Optional[datetime] = None
        self.snipes: List[dict] = []
    
    def get_local_date(self, station: str) -> str:
        tz_name = STATIONS.get(station, {}).get('timezone', 'America/New_York')
        return datetime.now(ZoneInfo(tz_name)).strftime('%Y-%m-%d')
    
    # ============================================================
    # INITIALIZATION
    # ============================================================
    
    def init_watchlists(self):
        """Initialize watchlists with all brackets from Kalshi."""
        print(f"[INIT] Building watchlists...")
        now = datetime.now(timezone.utc)
        today_suffix = now.strftime('%y%b%d').upper()
        
        for station, state in self.states.items():
            cfg = STATIONS.get(station, {})
            high_ticker = cfg.get('kalshi_high_ticker')
            low_ticker = cfg.get('kalshi_low_ticker')
            
            state.high_watchlist = []
            state.low_watchlist = []
            state.resolved_brackets = []
            
            # Fetch HIGH brackets
            if high_ticker:
                try:
                    event = f"{high_ticker}-{today_suffix}"
                    event_data = self.kalshi.get_event(event)
                    event_obj = event_data.get('event', event_data)
                    markets = event_obj.get('markets', [])
                    
                    for m in markets:
                        # Handle 0 as valid value (not None) - use explicit None check
                        floor_val = m.get('floor_strike')
                        cap_val = m.get('cap_strike')
                        b = BracketState(
                            ticker=m.get('ticker', ''),
                            subtitle=m.get('yes_sub_title', m.get('subtitle', '')),
                            floor_strike=int(floor_val) if floor_val is not None else None,
                            cap_strike=int(cap_val) if cap_val is not None else None,
                            strike_type=m.get('strike_type', 'between'),
                            signal_type='high',
                            station=station
                        )
                        b.no_ask = self._parse_price(m.get('no_ask'), m.get('no_ask_dollars'))
                        b.yes_ask = self._parse_price(m.get('yes_ask'), m.get('yes_ask_dollars'))
                        state.high_watchlist.append(b)
                    
                    print(f"  {station} HIGH: {len(state.high_watchlist)} brackets")
                except Exception as e:
                    print(f"  {station} HIGH: ERROR - {e}")
            
            # Fetch LOW brackets
            if low_ticker:
                try:
                    event = f"{low_ticker}-{today_suffix}"
                    event_data = self.kalshi.get_event(event)
                    event_obj = event_data.get('event', event_data)
                    markets = event_obj.get('markets', [])
                    
                    for m in markets:
                        # Handle 0 as valid value (not None) - use explicit None check
                        floor_val = m.get('floor_strike')
                        cap_val = m.get('cap_strike')
                        b = BracketState(
                            ticker=m.get('ticker', ''),
                            subtitle=m.get('yes_sub_title', m.get('subtitle', '')),
                            floor_strike=int(floor_val) if floor_val is not None else None,
                            cap_strike=int(cap_val) if cap_val is not None else None,
                            strike_type=m.get('strike_type', 'between'),
                            signal_type='low',
                            station=station
                        )
                        b.no_ask = self._parse_price(m.get('no_ask'), m.get('no_ask_dollars'))
                        b.yes_ask = self._parse_price(m.get('yes_ask'), m.get('yes_ask_dollars'))
                        state.low_watchlist.append(b)
                    
                    print(f"  {station} LOW: {len(state.low_watchlist)} brackets")
                except Exception as e:
                    print(f"  {station} LOW: ERROR - {e}")
        
        self.last_price_poll = datetime.now(timezone.utc)
    
    def _parse_price(self, price_raw, price_dollars) -> int:
        """Parse price from Kalshi API response."""
        if price_dollars:
            try:
                val = int(float(price_dollars) * 100)
                if val > 0:
                    return val
            except:
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
            except:
                pass
        # Return 0 to indicate "no data" - display will show 0¢
        return 0
    
    def fetch_historical_temps(self):
        """Fetch historical METARs to get accurate daily high/low."""
        print(f"[INIT] Fetching historical temps...")
        
        max_hours = 24
        
        try:
            ids_param = ','.join(self.states.keys())
            url = f"https://aviationweather.gov/api/data/metar?ids={ids_param}&format=json&hours={max_hours}"
            headers = {'User-Agent': 'WXSniper/4.0 (weather-trading-bot)'}
            
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                print(f"  [HIST] HTTP {resp.status_code}")
                return
            
            metars = resp.json()
            if not isinstance(metars, list):
                metars = [metars] if metars else []
            
            # Initialize all states
            for station, state in self.states.items():
                cfg = STATIONS.get(station, {})
                tz_name = cfg.get('timezone', 'America/New_York')
                tz = ZoneInfo(tz_name)
                state.current_local_date = datetime.now(tz).strftime('%Y-%m-%d')
                state.observed_high = None
                state.observed_low = None
            
            # Process all METARs
            for metar_data in metars:
                station = metar_data.get('icaoId') or metar_data.get('stationId')
                if not station or station not in self.states:
                    continue
                
                state = self.states[station]
                raw = metar_data.get('rawOb', '')
                parsed = parse_metar(raw)
                
                if parsed.temp_f is not None:
                    if state.observed_high is None or parsed.temp_f > state.observed_high:
                        state.observed_high = parsed.temp_f
                    if state.observed_low is None or parsed.temp_f < state.observed_low:
                        state.observed_low = parsed.temp_f
                
                if parsed.six_hr_max_c is not None:
                    max_f = c_to_f_nws(parsed.six_hr_max_c)
                    if state.observed_high is None or max_f > state.observed_high:
                        state.observed_high = max_f
                
                if parsed.six_hr_min_c is not None:
                    min_f = c_to_f_nws(parsed.six_hr_min_c)
                    if state.observed_low is None or min_f < state.observed_low:
                        state.observed_low = min_f
            
            for station, state in self.states.items():
                print(f"  [{station}] HIGH={state.observed_high}°F LOW={state.observed_low}°F")
            
        except Exception as e:
            print(f"  [HIST] Batch fetch error: {e}")
    
    def prune_watchlists(self):
        """Remove already-resolved brackets from watchlists."""
        print(f"[INIT] Pruning watchlists...")
        
        for station, state in self.states.items():
            # Check HIGH watchlist
            still_open = []
            for b in state.high_watchlist:
                status = b.check_status(state.observed_high, state.observed_low)
                if status == 'open':
                    still_open.append(b)
                else:
                    b.status = status
                    state.resolved_brackets.append(b)
            state.high_watchlist = still_open
            
            # Check LOW watchlist
            still_open = []
            for b in state.low_watchlist:
                status = b.check_status(state.observed_high, state.observed_low)
                if status == 'open':
                    still_open.append(b)
                else:
                    b.status = status
                    state.resolved_brackets.append(b)
            state.low_watchlist = still_open
            
            high_count = len(state.high_watchlist)
            low_count = len(state.low_watchlist)
            resolved = len(state.resolved_brackets)
            print(f"  [{station}] Watching: {high_count} HIGH, {low_count} LOW | Already resolved: {resolved}")
    
    # ============================================================
    # POLLING & SNIPE DETECTION
    # ============================================================
    
    def _fetch_metar_nws_txt(self, station: str) -> Optional[str]:
        """Fetch METAR from NWS TXT (often faster, no caching)."""
        try:
            url = f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{station}.TXT"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                lines = resp.text.strip().split('\n')
                # First line is timestamp, second line is METAR
                if len(lines) >= 2:
                    return lines[1].strip()
        except:
            pass
        return None
    
    def _fetch_metars_aviation_api(self, stations: List[str]) -> Dict[str, str]:
        """Fetch METARs from Aviation Weather API (batch, but can be cached)."""
        result = {}
        try:
            ids_param = ','.join(stations)
            # Add cache-buster
            url = f"https://aviationweather.gov/api/data/metar?ids={ids_param}&format=json&_t={int(time.time())}"
            headers = {'User-Agent': 'WXSniper/4.0'}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                metars = resp.json()
                if not isinstance(metars, list):
                    metars = [metars]
                for m in metars:
                    station = m.get('icaoId') or m.get('stationId')
                    raw = m.get('rawOb', '')
                    if station and raw:
                        result[station] = raw
        except:
            pass
        return result
    
    def poll_and_snipe(self):
        """Fetch METARs from multiple sources, detect transitions, execute snipes."""
        active_stations = [
            station for station, state in self.states.items()
            if state.high_watchlist or state.low_watchlist
        ]
        
        if not active_stations:
            print("[METAR] No active watchlists to poll")
            return
        
        print(f"[METAR] Polling {len(active_stations)} stations...")
        
        try:
            # Strategy: Try NWS TXT first (faster), fall back to Aviation API
            metars_found = {}
            
            # 1. Try NWS TXT for each station IN PARALLEL
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=12) as executor:
                futures = {executor.submit(self._fetch_metar_nws_txt, station): station 
                          for station in active_stations}
                for future in as_completed(futures, timeout=6):
                    station = futures[future]
                    try:
                        raw = future.result()
                        if raw:
                            metars_found[station] = raw
                    except:
                        pass
            
            nws_count = len(metars_found)
            
            # 2. Fall back to Aviation API for any missing
            missing = [s for s in active_stations if s not in metars_found]
            if missing:
                api_metars = self._fetch_metars_aviation_api(missing)
                metars_found.update(api_metars)
            
            api_count = len(metars_found) - nws_count
            print(f"  [METAR] Got {nws_count} from NWS TXT, {api_count} from API")
            
            # Process all METARs
            for station, raw in metars_found.items():
                if station not in self.states:
                    continue
                
                state = self.states[station]
                
                # Debug: log if METAR changed
                if raw != state.latest_metar:
                    print(f"  [{station}] NEW METAR: {raw[:80]}")
                
                parsed = parse_metar(raw)
                
                state.latest_metar = raw
                
                # Parse observation time from METAR string (e.g., "301853Z" = day 30, 18:53 UTC)
                # Match pattern like "301853Z" (DDHHMMZ)
                time_match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', raw)
                if time_match:
                    day = int(time_match.group(1))
                    hour = int(time_match.group(2))
                    minute = int(time_match.group(3))
                    # Use current year/month, adjust day
                    now_utc = datetime.now(timezone.utc)
                    try:
                        state.metar_time = now_utc.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
                    except ValueError:
                        # Day might be from previous month - use now as fallback
                        state.metar_time = now_utc
                else:
                    # No time found in METAR, use current time
                    state.metar_time = datetime.now(timezone.utc)
                
                # Track changes
                old_high = state.observed_high
                old_low = state.observed_low
                
                # Update latest temp (current reading from this METAR)
                if parsed.temp_f is not None:
                    state.latest_temp_f = parsed.temp_f
                
                # Update from current temp
                if parsed.temp_f is not None:
                    if state.observed_high is None or parsed.temp_f > state.observed_high:
                        state.observed_high = parsed.temp_f
                    if state.observed_low is None or parsed.temp_f < state.observed_low:
                        state.observed_low = parsed.temp_f
                
                # Update from 6-hour groups
                if parsed.six_hr_max_c is not None:
                    max_f = c_to_f_nws(parsed.six_hr_max_c)
                    if state.observed_high is None or max_f > state.observed_high:
                        state.observed_high = max_f
                        print(f"  [SYNOPTIC] {station} 6hr max: {max_f}°F")
                
                if parsed.six_hr_min_c is not None:
                    min_f = c_to_f_nws(parsed.six_hr_min_c)
                    if state.observed_low is None or min_f < state.observed_low:
                        state.observed_low = min_f
                        print(f"  [SYNOPTIC] {station} 6hr min: {min_f}°F")
                
                # Check for transitions
                if state.observed_high != old_high or state.observed_low != old_low:
                    print(f"  [{station}] Updated: HIGH={state.observed_high}°F LOW={state.observed_low}°F")
                    self._check_transitions(state)
        
        except Exception as e:
            print(f"  [METAR] Poll error: {e}")
        
        self.last_metar_poll = datetime.now(timezone.utc)
    
    def _check_transitions(self, state: StationState):
        """Check watchlists for OPEN → DEAD or OPEN → LOCKED transitions."""
        
        # Check HIGH watchlist
        still_watching = []
        for b in state.high_watchlist:
            new_status = b.check_status(state.observed_high, state.observed_low)
            
            if new_status == 'dead' and b.status == 'open':
                reason = b.describe_resolution(state.observed_high, state.observed_low)
                self._snipe(b, 'BUY_NO', b.no_ask, reason)
                b.status = 'dead'
                state.resolved_brackets.append(b)
            
            elif new_status == 'locked' and b.status == 'open':
                reason = b.describe_resolution(state.observed_high, state.observed_low)
                self._snipe(b, 'BUY_YES', b.yes_ask, reason)
                b.status = 'locked'
                state.resolved_brackets.append(b)
            
            else:
                still_watching.append(b)
        
        state.high_watchlist = still_watching
        
        # Check LOW watchlist
        still_watching = []
        for b in state.low_watchlist:
            new_status = b.check_status(state.observed_high, state.observed_low)
            
            if new_status == 'dead' and b.status == 'open':
                reason = b.describe_resolution(state.observed_high, state.observed_low)
                self._snipe(b, 'BUY_NO', b.no_ask, reason)
                b.status = 'dead'
                state.resolved_brackets.append(b)
            
            elif new_status == 'locked' and b.status == 'open':
                reason = b.describe_resolution(state.observed_high, state.observed_low)
                self._snipe(b, 'BUY_YES', b.yes_ask, reason)
                b.status = 'locked'
                state.resolved_brackets.append(b)
            
            else:
                still_watching.append(b)
        
        state.low_watchlist = still_watching
    
    def _snipe(self, bracket: BracketState, action: str, price: int, reason: str):
        """Execute a snipe trade."""
        side = 'no' if action == 'BUY_NO' else 'yes'
        
        # TESTING MODE: Always bid 99¢ to guarantee fill
        # TODO: Revert to actual price-based bidding once we confirm everything works
        execution_price = 99  # Always bid max to guarantee fill during testing
        
        snipe_record = {
            'time': datetime.now(timezone.utc).isoformat(),
            'station': bracket.station,
            'ticker': bracket.ticker,
            'subtitle': bracket.subtitle,
            'action': action,
            'side': side,
            'price': execution_price,
            'original_ask': price,  # Track what the ask was
            'reason': reason,
            'live': self.live_mode,
            'success': False,
        }
        
        print(f"  [SNIPE] {action} {bracket.subtitle} - bidding {execution_price}¢ (ask was {price}¢) - {reason}")
        
        if self.live_mode:
            try:
                result = self.kalshi.create_order(
                    ticker=bracket.ticker,
                    side=side,
                    action='buy',
                    count=1,
                    order_type='limit',
                    price_cents=execution_price
                )
                snipe_record['success'] = True
                snipe_record['order_id'] = result.get('order', {}).get('order_id')
                print(f"  [SNIPE] ✅ Order placed!")
            except Exception as e:
                snipe_record['error'] = str(e)
                print(f"  [SNIPE] ❌ Failed: {e}")
        else:
            snipe_record['success'] = True
            print(f"  [SNIPE] 🧪 DRY RUN")
        
        bracket.traded = True
        self.snipes.append(snipe_record)
    
    # ============================================================
    # PRICE REFRESH
    # ============================================================
    
    def refresh_prices(self):
        """Refresh prices for watched brackets."""
        print(f"[PRICES] Refreshing...")
        now = datetime.now(timezone.utc)
        today_suffix = now.strftime('%y%b%d').upper()
        
        for station, state in self.states.items():
            cfg = STATIONS.get(station, {})
            
            if state.high_watchlist:
                high_ticker = cfg.get('kalshi_high_ticker')
                if high_ticker:
                    try:
                        event = f"{high_ticker}-{today_suffix}"
                        event_data = self.kalshi.get_event(event)
                        event_obj = event_data.get('event', event_data)
                        markets = {m.get('ticker'): m for m in event_obj.get('markets', [])}
                        
                        for b in state.high_watchlist:
                            if b.ticker in markets:
                                m = markets[b.ticker]
                                b.no_ask = self._parse_price(m.get('no_ask'), m.get('no_ask_dollars'))
                                b.yes_ask = self._parse_price(m.get('yes_ask'), m.get('yes_ask_dollars'))
                    except Exception as e:
                        print(f"  {station} HIGH prices: {e}")
            
            if state.low_watchlist:
                low_ticker = cfg.get('kalshi_low_ticker')
                if low_ticker:
                    try:
                        event = f"{low_ticker}-{today_suffix}"
                        event_data = self.kalshi.get_event(event)
                        event_obj = event_data.get('event', event_data)
                        markets = {m.get('ticker'): m for m in event_obj.get('markets', [])}
                        
                        for b in state.low_watchlist:
                            if b.ticker in markets:
                                m = markets[b.ticker]
                                b.no_ask = self._parse_price(m.get('no_ask'), m.get('no_ask_dollars'))
                                b.yes_ask = self._parse_price(m.get('yes_ask'), m.get('yes_ask_dollars'))
                    except Exception as e:
                        print(f"  {station} LOW prices: {e}")
        
        self.last_price_poll = datetime.now(timezone.utc)
    
    # ============================================================
    # MAIN LOOP
    # ============================================================
    
    def run(self):
        """Main entry point."""
        print("=" * 60)
        print("WX SNIPER v4.2 - ZERO STRIKE BUG FIX")
        print("=" * 60)
        print(f"Mode: {'LIVE 🔴' if self.live_mode else 'DRY RUN 🧪'}")
        print(f"Max price: {self.max_price}¢")
        print("=" * 60)
        
        # Start dashboard
        HealthHandler.sniper = self
        dashboard_thread = threading.Thread(target=start_health_server, daemon=True)
        dashboard_thread.start()
        
        # Initialize
        self.init_watchlists()
        self.fetch_historical_temps()
        self.prune_watchlists()
        
        # Do an immediate METAR poll so dashboard has data right away
        print("\n[INIT] Initial METAR poll...")
        self.poll_and_snipe()
        
        print("\n[RUNNING] Starting main loop...")
        last_price_refresh = time.time()
        
        while True:
            now = datetime.now(timezone.utc)
            minute = now.minute
            
            # Hot window: :52 to :02
            is_hot = minute >= 50 or minute <= 5
            
            # Poll METARs
            self.poll_and_snipe()
            
            # Refresh prices every 5 minutes
            if time.time() - last_price_refresh > 300:
                self.refresh_prices()
                last_price_refresh = time.time()
            
            # Sleep
            if is_hot:
                time.sleep(10)  # Fast polling during hot window
            else:
                time.sleep(60)  # Normal polling


# ============================================================
# DASHBOARD
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    sniper: Optional[WXSniper] = None
    
    def log_message(self, format, *args):
        pass
    
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode())
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(self.build_dashboard().encode('utf-8'))
    
    def build_dashboard(self) -> str:
        s = HealthHandler.sniper
        if not s:
            return "<html><body>Not initialized</body></html>"
        
        now = datetime.now(timezone.utc)
        now_et = datetime.now(ZoneInfo('America/New_York'))
        minute = now.minute
        is_hot = minute >= 50 or minute <= 5
        
        total_watching = sum(len(st.high_watchlist) + len(st.low_watchlist) for st in s.states.values())
        total_resolved = sum(len(st.resolved_brackets) for st in s.states.values())
        
        # Filter snipes to only show those from today (ET midnight cutoff)
        today_et = now_et.date()
        todays_snipes = []
        for snipe in s.snipes:
            try:
                snipe_time = datetime.fromisoformat(snipe['time'].replace('Z', '+00:00'))
                snipe_et = snipe_time.astimezone(ZoneInfo('America/New_York'))
                if snipe_et.date() == today_et:
                    todays_snipes.append(snipe)
            except:
                todays_snipes.append(snipe)  # Include if can't parse
        
        html = f'''<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>WX Sniper v4.2</title>
<meta http-equiv="refresh" content="{'10' if is_hot else '30'}">
<style>
body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, sans-serif; padding: 20px; }}
h1 {{ color: #58a6ff; }}
h2 {{ color: #8b949e; border-bottom: 1px solid #30363d; padding-bottom: 8px; margin-top: 30px; }}
h3 {{ color: #58a6ff; margin-top: 20px; }}
h4 {{ color: #8b949e; margin: 15px 0 5px 0; font-size: 14px; }}
table {{ border-collapse: collapse; width: 100%; max-width: 900px; margin: 10px 0; }}
th, td {{ padding: 6px 10px; text-align: left; border: 1px solid #30363d; }}
th {{ background: #161b22; }}
.dead {{ color: #f85149; }}
.locked {{ color: #3fb950; }}
.open {{ color: #58a6ff; font-weight: bold; }}
.hot {{ background: #3d1c1c; padding: 5px 10px; border-radius: 4px; }}
.snipe {{ background: #1c3d1c; }}
.time {{ color: #8b949e; font-size: 12px; }}
.stats {{ display: flex; gap: 20px; margin: 10px 0; flex-wrap: wrap; }}
.stat {{ background: #161b22; padding: 10px 15px; border-radius: 6px; }}
.stat-value {{ font-size: 24px; font-weight: bold; color: #58a6ff; }}
.stat-label {{ font-size: 12px; color: #8b949e; }}
.resolved-row {{ opacity: 0.6; }}
details {{ margin: 10px 0; }}
summary {{ cursor: pointer; color: #8b949e; }}
.metar-info {{ background: #161b22; padding: 8px 12px; border-radius: 4px; margin: 8px 0; font-size: 13px; }}
.metar-info strong {{ color: #58a6ff; }}
.current-temp {{ font-size: 18px; color: #f0f6fc; }}
</style>
</head><body>
<h1>&#127919; WX Sniper v4.2</h1>
<p>
    Mode: <strong>{"LIVE &#128308;" if s.live_mode else "DRY RUN &#129514;"}</strong> |
    Max price: <strong>{s.max_price}&#162;</strong> |
    {"<span class='hot'>&#128293; HOT WINDOW</span>" if is_hot else "Normal polling"}
</p>

<div class="stats">
    <div class="stat"><div class="stat-value">{total_watching}</div><div class="stat-label">Watching</div></div>
    <div class="stat"><div class="stat-value">{total_resolved}</div><div class="stat-label">Resolved</div></div>
    <div class="stat"><div class="stat-value">{len(todays_snipes)}</div><div class="stat-label">Trades Today</div></div>
</div>

<p class="time">
    ET: {now_et.strftime("%b %d, %Y %I:%M:%S %p")} |
    Last METAR: {s.last_metar_poll.astimezone(ZoneInfo('America/New_York')).strftime("%I:%M:%S %p") if s.last_metar_poll else "Never"} ET |
    Last Kalshi: {s.last_price_poll.astimezone(ZoneInfo('America/New_York')).strftime("%I:%M:%S %p") if s.last_price_poll else "Never"} ET
</p>
'''
        
        # Today's Trades section (persists until midnight ET)
        if todays_snipes:
            html += f"<h2>&#9889; Today's Trades ({len(todays_snipes)}) - resets at midnight ET</h2>"
            html += '<table><tr><th>Date</th><th>Time (ET)</th><th>Station</th><th>Bracket</th><th>Action</th><th>Bid</th><th>Reason</th><th>Status</th></tr>'
            for snipe in reversed(todays_snipes):
                try:
                    snipe_time = datetime.fromisoformat(snipe['time'].replace('Z', '+00:00'))
                    snipe_et = snipe_time.astimezone(ZoneInfo('America/New_York'))
                    date_str = snipe_et.strftime("%m/%d")
                    time_str = snipe_et.strftime("%I:%M %p")
                except:
                    date_str = "?"
                    time_str = snipe['time'][11:19]
                
                status = "&#9989;" if snipe.get('success') else "&#10060;"
                if not snipe.get('live'):
                    status = "&#129514; DRY"
                action_class = "locked" if snipe['action'] == 'BUY_YES' else "dead"
                original_ask = snipe.get('original_ask', snipe['price'])
                html += f'''<tr class="snipe">
                    <td class="time">{date_str}</td>
                    <td class="time">{time_str}</td>
                    <td>{snipe['station']}</td>
                    <td>{snipe['subtitle']}</td>
                    <td class="{action_class}">{snipe['action']}</td>
                    <td>{snipe['price']}&#162; (ask: {original_ask}&#162;)</td>
                    <td>{snipe['reason'][:50]}</td>
                    <td>{status}</td>
                </tr>'''
            html += '</table>'
        else:
            html += "<h2>&#9889; Today's Trades (0)</h2><p style='color:#8b949e'>No trades yet today (ET timezone)</p>"
        
        # Watchlists
        html += "<h2>Watchlists</h2>"
        
        for station, state in s.states.items():
            cfg = STATIONS.get(station, {})
            city = cfg.get('name', station)
            tz_name = cfg.get('timezone', 'America/New_York')
            tz = ZoneInfo(tz_name)
            
            watching_count = len(state.high_watchlist) + len(state.low_watchlist)
            
            # Format METAR time in local time (12-hour format)
            metar_local_str = "?"
            if state.metar_time:
                metar_local = state.metar_time.astimezone(tz)
                metar_local_str = metar_local.strftime("%I:%M %p")
            
            current_temp_str = f"{state.latest_temp_f}°F" if state.latest_temp_f is not None else "?"
            
            html += f'''<h3>{city} ({station}) - {watching_count} watching</h3>
            <div class="metar-info">
                <strong>Latest METAR:</strong> {metar_local_str} local &nbsp;&nbsp; <span class="current-temp">{current_temp_str}</span> &nbsp;&nbsp;|&nbsp;&nbsp;
                <strong>Day's Range:</strong> <strong>HIGH</strong> {state.observed_high or "?"}&#176;F &nbsp; <strong>LOW</strong> {state.observed_low or "?"}&#176;F
            </div>'''
            
            # HIGH watchlist
            if state.high_watchlist:
                html += '<h4>HIGH Watchlist</h4>'
                html += '<table><tr><th>Bracket</th><th>Floor</th><th>Cap</th><th>NO Ask</th><th>YES Ask</th><th>Status</th></tr>'
                for b in sorted(state.high_watchlist, key=lambda x: x.floor_strike if x.floor_strike is not None else -999, reverse=True):
                    floor_display = b.floor_strike if b.floor_strike is not None else "—"
                    cap_display = b.cap_strike if b.cap_strike is not None else "—"
                    html += f'''<tr>
                        <td>{b.subtitle}</td>
                        <td>{floor_display}</td>
                        <td>{cap_display}</td>
                        <td>{b.no_ask}&#162;</td>
                        <td>{b.yes_ask}&#162;</td>
                        <td class="open">OPEN</td>
                    </tr>'''
                html += '</table>'
            
            # LOW watchlist
            if state.low_watchlist:
                html += '<h4>LOW Watchlist</h4>'
                html += '<table><tr><th>Bracket</th><th>Floor</th><th>Cap</th><th>NO Ask</th><th>YES Ask</th><th>Status</th></tr>'
                for b in sorted(state.low_watchlist, key=lambda x: x.cap_strike if x.cap_strike is not None else 999):
                    floor_display = b.floor_strike if b.floor_strike is not None else "—"
                    cap_display = b.cap_strike if b.cap_strike is not None else "—"
                    html += f'''<tr>
                        <td>{b.subtitle}</td>
                        <td>{floor_display}</td>
                        <td>{cap_display}</td>
                        <td>{b.no_ask}&#162;</td>
                        <td>{b.yes_ask}&#162;</td>
                        <td class="open">OPEN</td>
                    </tr>'''
                html += '</table>'
            
            # Resolved - split into HIGH and LOW
            high_resolved = [b for b in state.resolved_brackets if b.signal_type == 'high']
            low_resolved = [b for b in state.resolved_brackets if b.signal_type == 'low']
            
            if high_resolved or low_resolved:
                total_resolved_count = len(high_resolved) + len(low_resolved)
                html += f'<details><summary>Resolved ({total_resolved_count})</summary>'
                
                if high_resolved:
                    html += '<h4>HIGH Resolved</h4>'
                    html += '<table><tr><th>Bracket</th><th>Status</th><th>Traded?</th></tr>'
                    for b in high_resolved[-15:]:
                        status_class = "locked" if b.status == 'locked' else "dead"
                        traded = "&#9989;" if b.traded else "—"
                        html += f'<tr class="resolved-row"><td>{b.subtitle}</td><td class="{status_class}">{b.status.upper()}</td><td>{traded}</td></tr>'
                    html += '</table>'
                
                if low_resolved:
                    html += '<h4>LOW Resolved</h4>'
                    html += '<table><tr><th>Bracket</th><th>Status</th><th>Traded?</th></tr>'
                    for b in low_resolved[-15:]:
                        status_class = "locked" if b.status == 'locked' else "dead"
                        traded = "&#9989;" if b.traded else "—"
                        html += f'<tr class="resolved-row"><td>{b.subtitle}</td><td class="{status_class}">{b.status.upper()}</td><td>{traded}</td></tr>'
                    html += '</table>'
                
                html += '</details>'
        
        html += "</body></html>"
        return html


def start_health_server():
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    print(f"[HTTP] Dashboard on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    sniper = WXSniper()
    sniper.run()
