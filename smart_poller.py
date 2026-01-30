#!/usr/bin/env python3
"""
WX Sniper v3.8 - Latency Sniper
===============================
The whole point: detect state TRANSITIONS and snipe before market reprices.

WATCHLIST MODEL:
- At startup, all brackets are "open" (could go either way)
- As METARs come in, brackets transition: OPEN → DEAD or OPEN → LOCKED
- The INSTANT a transition happens, execute the trade
- Remove from watchlist (no more API calls needed for that bracket)

STATE TRANSITIONS:
- OPEN → DEAD: Buy NO (bracket can never win now)
- OPEN → LOCKED: Buy YES (bracket is guaranteed to win now)

This minimizes API calls by only watching what matters.
"""

import os
import re
import time
import json
import threading
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Set
from http.server import HTTPServer, BaseHTTPRequestHandler

from config import STATIONS
from kalshi_client import KalshiClient
from aviation_weather import AviationWeatherPoller

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
    """NWS rounding: round half up."""
    import math
    return math.floor(temp_f + 0.5)

def c_to_f_nws(temp_c: float) -> int:
    """Convert Celsius to Fahrenheit with NWS rounding."""
    return nws_round(temp_c * 9/5 + 32)

def parse_metar(raw: str) -> ParsedMETAR:
    """Parse METAR string."""
    result = ParsedMETAR(raw=raw)
    
    # T-group: T01061000 = +10.6°C
    t_match = re.search(r'\bT(\d)(\d{3})(\d)(\d{3})\b', raw)
    if t_match:
        temp_sign = -1 if t_match.group(1) == '1' else 1
        result.temp_c = temp_sign * int(t_match.group(2)) / 10.0
        result.temp_f = nws_round(result.temp_c * 9/5 + 32)
    
    # 6-hour max (1-group)
    max_match = re.search(r'\b1(\d)(\d{3})\b', raw)
    if max_match:
        sign = -1 if max_match.group(1) == '1' else 1
        result.six_hr_max_c = sign * int(max_match.group(2)) / 10.0
    
    # 6-hour min (2-group)
    min_match = re.search(r'\b2(\d)(\d{3})\b', raw)
    if min_match:
        sign = -1 if min_match.group(1) == '1' else 1
        result.six_hr_min_c = sign * int(min_match.group(2)) / 10.0
    
    return result

# ============================================================
# BRACKET STATE
# ============================================================

class BracketState:
    """Tracks state of a single bracket."""
    
    def __init__(self, ticker: str, subtitle: str, floor_strike: Optional[int], 
                 cap_strike: Optional[int], strike_type: str, signal_type: str, station: str):
        self.ticker = ticker
        self.subtitle = subtitle
        self.floor_strike = floor_strike
        self.cap_strike = cap_strike
        self.strike_type = strike_type  # 'greater', 'less', 'between'
        self.signal_type = signal_type  # 'high' or 'low'
        self.station = station
        
        # Prices (updated periodically)
        self.no_ask: int = 100
        self.yes_ask: int = 100
        
        # State tracking
        self.status: str = 'open'  # 'open', 'dead', 'locked'
        self.traded: bool = False  # Have we already traded this?
    
    @property
    def is_edge(self) -> bool:
        return self.strike_type in ('greater', 'less')
    
    def check_status(self, observed_high: Optional[int], observed_low: Optional[int]) -> str:
        """
        Determine current status based on observations.
        Returns: 'open', 'dead', or 'locked'
        """
        if self.signal_type == 'high':
            if observed_high is None:
                return 'open'
            
            # HIGH "X to Y" or "X or below": dead if exceeded cap
            if self.cap_strike is not None and observed_high > self.cap_strike:
                return 'dead'
            
            # HIGH "X or above": locked if we hit it
            if self.is_edge and self.floor_strike is not None and observed_high >= self.floor_strike:
                return 'locked'
            
            return 'open'
        
        else:  # LOW
            if observed_low is None:
                return 'open'
            
            # LOW "X or below" (cold edge): dead if low > cap (too warm, locked out)
            if self.floor_strike is None and self.cap_strike is not None:
                if observed_low > self.cap_strike:
                    return 'dead'
                if observed_low <= self.cap_strike:
                    return 'locked'  # We hit it!
            
            # LOW "X or above" (warm edge): dead if low < floor
            if self.cap_strike is None and self.floor_strike is not None:
                if observed_low < self.floor_strike:
                    return 'dead'
                # Can't be locked - low could still drop
                return 'open'
            
            # LOW "X to Y" (range): dead if low < floor
            if self.floor_strike is not None and self.cap_strike is not None:
                if observed_low < self.floor_strike:
                    return 'dead'
                return 'open'
            
            return 'open'


# ============================================================
# STATION STATE
# ============================================================

@dataclass
class StationState:
    station: str
    observed_high: Optional[int] = None
    observed_low: Optional[int] = None
    latest_metar: Optional[str] = None
    latest_temp_f: Optional[int] = None
    metar_time: Optional[datetime] = None
    current_local_date: Optional[str] = None
    
    # Watchlist: brackets we're still monitoring
    high_watchlist: List[BracketState] = field(default_factory=list)
    low_watchlist: List[BracketState] = field(default_factory=list)
    
    # Resolved: brackets that are done (for display only)
    resolved_brackets: List[BracketState] = field(default_factory=list)


# ============================================================
# MAIN SNIPER
# ============================================================

class WXSniper:
    def __init__(self):
        self.kalshi = KalshiClient()
        self.weather = AviationWeatherPoller()
        
        # Config
        self.live_mode = os.environ.get('LIVE_MODE', 'true').lower() == 'true'
        self.max_price = int(os.environ.get('MAX_PRICE', '95'))  # Max price to pay
        
        # State per station
        self.states: Dict[str, StationState] = {}
        for station in STATIONS.keys():
            self.states[station] = StationState(station=station)
        
        # Tracking
        self.last_metar_poll: Optional[datetime] = None
        self.last_price_poll: Optional[datetime] = None
        self.trade_log: List[dict] = []
        self.snipes: List[dict] = []  # Recent snipes for display
        
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
                        b = BracketState(
                            ticker=m.get('ticker', ''),
                            subtitle=m.get('yes_sub_title', m.get('subtitle', '')),
                            floor_strike=int(m.get('floor_strike')) if m.get('floor_strike') else None,
                            cap_strike=int(m.get('cap_strike')) if m.get('cap_strike') else None,
                            strike_type=m.get('strike_type', 'between'),
                            signal_type='high',
                            station=station
                        )
                        b.no_ask = int(m.get('no_ask') or 100)
                        b.yes_ask = int(m.get('yes_ask') or 100)
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
                        b = BracketState(
                            ticker=m.get('ticker', ''),
                            subtitle=m.get('yes_sub_title', m.get('subtitle', '')),
                            floor_strike=int(m.get('floor_strike')) if m.get('floor_strike') else None,
                            cap_strike=int(m.get('cap_strike')) if m.get('cap_strike') else None,
                            strike_type=m.get('strike_type', 'between'),
                            signal_type='low',
                            station=station
                        )
                        b.no_ask = int(m.get('no_ask') or 100)
                        b.yes_ask = int(m.get('yes_ask') or 100)
                        state.low_watchlist.append(b)
                    
                    print(f"  {station} LOW: {len(state.low_watchlist)} brackets")
                except Exception as e:
                    print(f"  {station} LOW: ERROR - {e}")
        
        self.last_price_poll = datetime.now(timezone.utc)
    
    def fetch_historical_temps(self):
        """
        Fetch historical METARs to get accurate daily high/low.
        Uses batched request to minimize API calls.
        """
        print(f"[INIT] Fetching historical temps...")
        
        # Calculate hours needed (from midnight local to now)
        # Use max across all stations to be safe
        max_hours = 1
        for station in self.states.keys():
            cfg = STATIONS.get(station, {})
            tz_name = cfg.get('timezone', 'America/New_York')
            tz = ZoneInfo(tz_name)
            now_local = datetime.now(tz)
            hours = max(1, now_local.hour + 1)
            max_hours = max(max_hours, hours)
        
        # BATCHED REQUEST: All stations in one API call
        try:
            ids_param = ','.join(self.states.keys())
            url = f"https://aviationweather.gov/api/data/metar?ids={ids_param}&format=json&hours={max_hours}"
            headers = {'User-Agent': 'WXSniper/3.8 (weather-trading-bot)'}
            
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
            
            # Print results
            for station, state in self.states.items():
                print(f"  [{station}] HIGH={state.observed_high}°F LOW={state.observed_low}°F")
            
        except Exception as e:
            print(f"  [HIST] Batch fetch error: {e}")
    
    def prune_watchlists(self):
        """
        Check all watchlists against current observations.
        Remove already-dead/locked brackets (we missed them).
        """
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
    # METAR POLLING & SNIPE DETECTION
    # ============================================================
    
    def poll_and_snipe(self):
        """
        Fetch METARs, update observations, detect transitions, execute snipes.
        This is the hot loop.
        
        IMPORTANT: Uses batched request to stay under 100 req/min limit.
        One request fetches all stations at once.
        """
        # Get list of stations still being watched
        active_stations = [
            station for station, state in self.states.items()
            if state.high_watchlist or state.low_watchlist
        ]
        
        if not active_stations:
            return
        
        # BATCHED REQUEST: All stations in one API call
        try:
            ids_param = ','.join(active_stations)
            url = f"https://aviationweather.gov/api/data/metar?ids={ids_param}&format=json"
            headers = {'User-Agent': 'WXSniper/3.8 (weather-trading-bot)'}
            
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 204:
                return  # No data
            if resp.status_code != 200:
                print(f"  [METAR] HTTP {resp.status_code}")
                return
            
            metars = resp.json()
            if not isinstance(metars, list):
                metars = [metars]
            
            # Process each METAR
            for metar_data in metars:
                station = metar_data.get('icaoId') or metar_data.get('stationId')
                if not station or station not in self.states:
                    continue
                
                state = self.states[station]
                raw = metar_data.get('rawOb', '')
                
                parsed = parse_metar(raw)
                
                state.latest_metar = raw
                # Parse observation time
                obs_time_str = metar_data.get('obsTime') or metar_data.get('reportTime')
                if obs_time_str:
                    try:
                        state.metar_time = datetime.fromisoformat(obs_time_str.replace('Z', '+00:00'))
                    except:
                        state.metar_time = datetime.now(timezone.utc)
                else:
                    state.metar_time = datetime.now(timezone.utc)
                
                # Track if observations changed
                old_high = state.observed_high
                old_low = state.observed_low
                
                # Update from current temp
                if parsed.temp_f is not None:
                    state.latest_temp_f = parsed.temp_f
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
                
                # Did observations change?
                if state.observed_high != old_high or state.observed_low != old_low:
                    print(f"  [{station}] Updated: HIGH={state.observed_high}°F LOW={state.observed_low}°F")
                    
                    # Check for transitions and snipe!
                    self._check_transitions(state)
        
        except Exception as e:
            print(f"  [METAR] Batch poll error: {e}")
        
        self.last_metar_poll = datetime.now(timezone.utc)
    
    def _check_transitions(self, state: StationState):
        """Check watchlists for OPEN → DEAD or OPEN → LOCKED transitions."""
        
        # Check HIGH watchlist
        still_watching = []
        for b in state.high_watchlist:
            new_status = b.check_status(state.observed_high, state.observed_low)
            
            if new_status == 'dead' and b.status == 'open':
                # TRANSITION: OPEN → DEAD
                self._snipe(b, 'BUY_NO', b.no_ask, f"HIGH dead: {state.observed_high}°F > {b.cap_strike}°F")
                b.status = 'dead'
                state.resolved_brackets.append(b)
            
            elif new_status == 'locked' and b.status == 'open':
                # TRANSITION: OPEN → LOCKED
                self._snipe(b, 'BUY_YES', b.yes_ask, f"HIGH locked: {state.observed_high}°F >= {b.floor_strike}°F")
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
                # TRANSITION: OPEN → DEAD
                if b.floor_strike is None and b.cap_strike is not None:
                    reason = f"LOW dead: {state.observed_low}°F > {b.cap_strike}°F (too warm)"
                else:
                    reason = f"LOW dead: {state.observed_low}°F < {b.floor_strike}°F"
                self._snipe(b, 'BUY_NO', b.no_ask, reason)
                b.status = 'dead'
                state.resolved_brackets.append(b)
            
            elif new_status == 'locked' and b.status == 'open':
                # TRANSITION: OPEN → LOCKED
                self._snipe(b, 'BUY_YES', b.yes_ask, f"LOW locked: {state.observed_low}°F <= {b.cap_strike}°F")
                b.status = 'locked'
                state.resolved_brackets.append(b)
            
            else:
                still_watching.append(b)
        
        state.low_watchlist = still_watching
    
    def _snipe(self, bracket: BracketState, action: str, price: int, reason: str):
        """Execute a snipe trade."""
        side = 'no' if action == 'BUY_NO' else 'yes'
        
        # Check price threshold
        if price > self.max_price:
            print(f"  [SKIP] {action} {bracket.subtitle} @ {price}¢ > max {self.max_price}¢")
            return
        
        snipe_record = {
            'time': datetime.now(timezone.utc).isoformat(),
            'station': bracket.station,
            'ticker': bracket.ticker,
            'subtitle': bracket.subtitle,
            'action': action,
            'side': side,
            'price': price,
            'reason': reason,
            'live': self.live_mode,
            'success': False,
        }
        
        print(f"  [SNIPE] {action} {bracket.subtitle} @ {price}¢ - {reason}")
        
        if self.live_mode:
            try:
                result = self.kalshi.create_order(
                    ticker=bracket.ticker,
                    side=side,
                    action='buy',
                    count=1,
                    order_type='limit',
                    price_cents=price
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
        self.trade_log.append(snipe_record)

    # ============================================================
    # PRICE REFRESH
    # ============================================================
    
    def refresh_prices(self):
        """Refresh prices for watched brackets only."""
        print(f"[PRICES] Refreshing watched brackets...")
        now = datetime.now(timezone.utc)
        today_suffix = now.strftime('%y%b%d').upper()
        
        for station, state in self.states.items():
            cfg = STATIONS.get(station, {})
            
            # Only fetch if we have something to watch
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
                                b.no_ask = int(m.get('no_ask') or 100)
                                b.yes_ask = int(m.get('yes_ask') or 100)
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
                                b.no_ask = int(m.get('no_ask') or 100)
                                b.yes_ask = int(m.get('yes_ask') or 100)
                    except Exception as e:
                        print(f"  {station} LOW prices: {e}")
        
        self.last_price_poll = datetime.now(timezone.utc)

    # ============================================================
    # MAIN LOOP
    # ============================================================
    
    def run(self):
        """Main entry point."""
        print("=" * 60)
        print("WX SNIPER v3.8 - LATENCY SNIPER")
        print("=" * 60)
        print(f"Mode: {'LIVE 🔴' if self.live_mode else 'DRY RUN 🧪'}")
        print(f"Max price: {self.max_price}¢")
        print("=" * 60)
        
        # Start dashboard
        HealthHandler.sniper = self
        http_thread = threading.Thread(target=start_health_server, daemon=True)
        http_thread.start()
        
        # Initialize
        print("\n[STARTUP]")
        self.init_watchlists()
        time.sleep(1)  # Rate limit pause
        self.fetch_historical_temps()
        self.prune_watchlists()
        
        # Main loop
        # API limits: 100 req/min total, 1 req/min per endpoint recommended
        # Our batched approach: 1 METAR request for ALL stations
        # Hot window: poll every 10s = 6 req/min (safe)
        # Normal: poll every 60s = 1 req/min (safe)
        print("\n[RUNNING] Sniper active...")
        print("[RATE LIMITS] Using batched requests: ~6 req/min hot, ~1 req/min normal")
        
        while True:
            try:
                now = datetime.now(timezone.utc)
                minute = now.minute
                
                # Hot window: :52-:02 - poll every 10s (6 req/min)
                if minute >= 52 or minute <= 2:
                    self.poll_and_snipe()
                    time.sleep(10)
                
                # Prep window: :50-:51 - refresh prices
                elif minute in (50, 51):
                    if self.last_price_poll is None or (now - self.last_price_poll).total_seconds() > 60:
                        self.refresh_prices()
                    time.sleep(15)
                
                # Normal time - light polling (every 60s)
                else:
                    if self.last_metar_poll is None or (now - self.last_metar_poll).total_seconds() > 60:
                        self.poll_and_snipe()
                    if self.last_price_poll is None or (now - self.last_price_poll).total_seconds() > 300:
                        self.refresh_prices()
                    time.sleep(30)
                    
            except KeyboardInterrupt:
                print("\n[EXIT]")
                break
            except Exception as e:
                print(f"[ERROR] {e}")
                time.sleep(30)


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
        minute = now.minute
        is_hot = minute >= 52 or minute <= 2
        
        # Count total watchlist items
        total_watching = sum(len(st.high_watchlist) + len(st.low_watchlist) for st in s.states.values())
        total_resolved = sum(len(st.resolved_brackets) for st in s.states.values())
        
        html = f'''<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>WX Sniper v3.8</title>
<meta http-equiv="refresh" content="{'10' if is_hot else '30'}">
<style>
body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, sans-serif; padding: 20px; }}
h1 {{ color: #58a6ff; }}
h2 {{ color: #8b949e; border-bottom: 1px solid #30363d; padding-bottom: 8px; margin-top: 30px; }}
h3 {{ color: #58a6ff; margin-top: 20px; }}
table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
th, td {{ padding: 6px 10px; text-align: left; border: 1px solid #30363d; }}
th {{ background: #161b22; }}
.dead {{ color: #f85149; }}
.locked {{ color: #3fb950; }}
.open {{ color: #d29922; }}
.hot {{ background: #3d1c1c; padding: 5px 10px; border-radius: 4px; }}
.snipe {{ background: #1c3d1c; }}
.metar {{ font-family: monospace; font-size: 11px; color: #8b949e; }}
.time {{ color: #8b949e; font-size: 12px; }}
.stats {{ display: flex; gap: 20px; margin: 10px 0; }}
.stat {{ background: #161b22; padding: 10px 15px; border-radius: 6px; }}
.stat-value {{ font-size: 24px; font-weight: bold; color: #58a6ff; }}
.stat-label {{ font-size: 12px; color: #8b949e; }}
</style>
</head><body>
<h1>&#127919; WX Sniper v3.8</h1>
<p>
    Mode: <strong>{"LIVE &#128308;" if s.live_mode else "DRY RUN &#129514;"}</strong> |
    Max price: <strong>{s.max_price}&#162;</strong> |
    {"<span class='hot'>&#128293; HOT WINDOW</span>" if is_hot else "Normal polling"}
</p>

<div class="stats">
    <div class="stat">
        <div class="stat-value">{total_watching}</div>
        <div class="stat-label">Watching</div>
    </div>
    <div class="stat">
        <div class="stat-value">{total_resolved}</div>
        <div class="stat-label">Resolved</div>
    </div>
    <div class="stat">
        <div class="stat-value">{len(s.snipes)}</div>
        <div class="stat-label">Snipes</div>
    </div>
</div>

<p class="time">
    UTC: {now.strftime("%H:%M:%S")} |
    Last METAR: {s.last_metar_poll.strftime("%H:%M:%S") if s.last_metar_poll else "Never"} |
    Last Prices: {s.last_price_poll.strftime("%H:%M:%S") if s.last_price_poll else "Never"}
</p>
'''
        
        # Recent snipes
        if s.snipes:
            html += f"<h2>&#9889; Recent Snipes ({len(s.snipes)})</h2>"
            html += '<table><tr><th>Time</th><th>Station</th><th>Bracket</th><th>Action</th><th>Price</th><th>Reason</th><th>Status</th></tr>'
            for snipe in reversed(s.snipes[-10:]):
                status = "&#9989;" if snipe.get('success') else "&#10060;"
                if not snipe.get('live'):
                    status = "&#129514;"
                action_class = "locked" if snipe['action'] == 'BUY_YES' else "dead"
                html += f'''<tr class="snipe">
                    <td class="time">{snipe['time'][11:19]}</td>
                    <td>{snipe['station']}</td>
                    <td>{snipe['subtitle']}</td>
                    <td class="{action_class}">{snipe['action']}</td>
                    <td>{snipe['price']}&#162;</td>
                    <td>{snipe['reason']}</td>
                    <td>{status}</td>
                </tr>'''
            html += '</table>'
        
        # Station watchlists
        html += "<h2>Watchlists</h2>"
        
        for station, state in s.states.items():
            cfg = STATIONS.get(station, {})
            city = cfg.get('name', station)
            tz = ZoneInfo(cfg.get('timezone', 'America/New_York'))
            
            metar_time = state.metar_time.astimezone(tz).strftime('%H:%M') if state.metar_time else "?"
            
            watching_count = len(state.high_watchlist) + len(state.low_watchlist)
            
            html += f'''<h3>{city} ({station}) - {watching_count} watching</h3>
            <p>
                <strong>Observed:</strong> HIGH={state.observed_high or "?"}&#176;F, LOW={state.observed_low or "?"}&#176;F |
                METAR @ {metar_time}
            </p>'''
            
            # HIGH watchlist
            if state.high_watchlist:
                html += '<p><strong>HIGH Watchlist:</strong></p>'
                html += '<table><tr><th>Bracket</th><th>Floor</th><th>Cap</th><th>NO Ask</th><th>YES Ask</th></tr>'
                for b in sorted(state.high_watchlist, key=lambda x: x.floor_strike or 0, reverse=True):
                    html += f'''<tr>
                        <td>{b.subtitle}</td>
                        <td>{b.floor_strike or "&#8212;"}</td>
                        <td>{b.cap_strike or "&#8212;"}</td>
                        <td>{b.no_ask}&#162;</td>
                        <td>{b.yes_ask}&#162;</td>
                    </tr>'''
                html += '</table>'
            
            # LOW watchlist
            if state.low_watchlist:
                html += '<p><strong>LOW Watchlist:</strong></p>'
                html += '<table><tr><th>Bracket</th><th>Floor</th><th>Cap</th><th>NO Ask</th><th>YES Ask</th></tr>'
                for b in sorted(state.low_watchlist, key=lambda x: x.cap_strike or 999):
                    html += f'''<tr>
                        <td>{b.subtitle}</td>
                        <td>{b.floor_strike or "&#8212;"}</td>
                        <td>{b.cap_strike or "&#8212;"}</td>
                        <td>{b.no_ask}&#162;</td>
                        <td>{b.yes_ask}&#162;</td>
                    </tr>'''
                html += '</table>'
            
            # Show resolved brackets (collapsed by default)
            if state.resolved_brackets:
                html += f'<details><summary>Resolved ({len(state.resolved_brackets)})</summary>'
                html += '<table><tr><th>Bracket</th><th>Status</th><th>Traded?</th></tr>'
                for b in state.resolved_brackets[-20:]:
                    status_class = "locked" if b.status == 'locked' else "dead"
                    traded = "&#9989;" if b.traded else "&#8212;"
                    html += f'''<tr>
                        <td>{b.subtitle}</td>
                        <td class="{status_class}">{b.status.upper()}</td>
                        <td>{traded}</td>
                    </tr>'''
                html += '</table></details>'
        
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
