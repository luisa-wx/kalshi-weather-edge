#!/usr/bin/env python3
"""
WX Sniper v3.6 - Clean Rebuild
==============================
Simplified logic, correct bracket handling.

Core Logic:
- HIGH markets: "What will the daily HIGH be?"
  - If observed temp > bracket's cap → bracket is DEAD → buy NO
  - If edge bracket ("X or above") not yet hit → buy YES
  
- LOW markets: "What will the daily LOW be?"
  - If observed temp < bracket's floor → bracket is DEAD → buy NO
  - If edge bracket ("X or below") not yet hit → buy YES

Polling:
- METARs: Fetch on startup, then poll :52-:02 window
- Prices: Fetch every 10 min (or on startup)
- Watchlist: Build at :45, refresh prices until :02
"""

import os
import re
import time
import json
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from http.server import HTTPServer, BaseHTTPRequestHandler

# Imports from your existing modules
from config import STATIONS, KALSHI_SERIES_TICKERS
from kalshi_client import KalshiClient
from weather import WeatherClient

# ============================================================
# METAR PARSING
# ============================================================

@dataclass
class ParsedMETAR:
    raw: str
    temp_c: Optional[float] = None
    temp_f: Optional[int] = None
    obs_time: Optional[datetime] = None
    is_synoptic: bool = False  # True if this is a synoptic METAR (has 6-hr extremes)
    six_hr_max_c: Optional[float] = None
    six_hr_min_c: Optional[float] = None

def parse_metar(raw: str) -> ParsedMETAR:
    """Parse METAR string, extract T-group temp and 6-hr extremes if present."""
    result = ParsedMETAR(raw=raw)
    
    # Extract T-group (high precision temp): T01061000 = +10.6°C temp, +10.0°C dewpoint
    t_match = re.search(r'\bT(\d)(\d{3})(\d)(\d{3})\b', raw)
    if t_match:
        temp_sign = -1 if t_match.group(1) == '1' else 1
        temp_val = int(t_match.group(2)) / 10.0
        result.temp_c = temp_sign * temp_val
        result.temp_f = nws_round(result.temp_c * 9/5 + 32)
    
    # Extract 6-hour max (1-group): 10170 = max 17.0°C
    max_match = re.search(r'\b1(\d)(\d{3})\b', raw)
    if max_match:
        sign = -1 if max_match.group(1) == '1' else 1
        result.six_hr_max_c = sign * int(max_match.group(2)) / 10.0
        result.is_synoptic = True
    
    # Extract 6-hour min (2-group): 20046 = min 4.6°C
    min_match = re.search(r'\b2(\d)(\d{3})\b', raw)
    if min_match:
        sign = -1 if min_match.group(1) == '1' else 1
        result.six_hr_min_c = sign * int(min_match.group(2)) / 10.0
        result.is_synoptic = True
    
    return result

def nws_round(temp_f: float) -> int:
    """NWS rounding: round half up (away from zero for negative)."""
    if temp_f >= 0:
        return int(temp_f + 0.5)
    else:
        return int(temp_f - 0.5)

def c_to_f_nws(temp_c: float) -> int:
    """Convert Celsius to Fahrenheit with NWS rounding."""
    return nws_round(temp_c * 9/5 + 32)

# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class Bracket:
    ticker: str
    subtitle: str
    floor_strike: Optional[int]  # Lower bound (or None for "X or below")
    cap_strike: Optional[int]    # Upper bound (or None for "X or above")
    strike_type: str             # 'greater', 'less', 'between'
    signal_type: str             # 'high' or 'low'
    station: str
    no_ask: int = 100
    yes_ask: int = 100
    
    @property
    def is_edge(self) -> bool:
        """Edge brackets are 'X or above' (high) or 'X or below' (low)."""
        return self.strike_type in ('greater', 'less')
    
    def is_dead(self, observed_high: Optional[int], observed_low: Optional[int]) -> bool:
        """
        Check if this bracket is DEAD (cannot settle YES).
        
        For HIGH brackets: dead if observed_high > cap
        For LOW brackets: dead if observed_low < floor
        """
        if self.signal_type == 'high':
            if observed_high is None:
                return False
            if self.cap_strike is not None:
                return observed_high > self.cap_strike
            return False  # Edge bracket with no cap
        else:  # low
            if observed_low is None:
                return False
            if self.floor_strike is not None:
                return observed_low < self.floor_strike
            return False  # Edge bracket with no floor
    
    def is_edge_opportunity(self, observed_high: Optional[int], observed_low: Optional[int]) -> bool:
        """
        Check if this is an edge bracket that hasn't been hit yet.
        
        For HIGH "X or above": opportunity if observed_high < floor
        For LOW "X or below": opportunity if observed_low > cap
        """
        if not self.is_edge:
            return False
        
        if self.signal_type == 'high':
            # "X or above" - opportunity if we haven't hit X yet
            if observed_high is None or self.floor_strike is None:
                return False
            return observed_high < self.floor_strike
        else:  # low
            # "X or below" - opportunity if we haven't gone below X yet
            if observed_low is None or self.cap_strike is None:
                return False
            return observed_low > self.cap_strike

@dataclass 
class StationState:
    station: str
    observed_high: Optional[int] = None
    observed_low: Optional[int] = None
    latest_metar: Optional[str] = None
    latest_temp_f: Optional[int] = None
    metar_time: Optional[datetime] = None
    high_brackets: List[Bracket] = field(default_factory=list)
    low_brackets: List[Bracket] = field(default_factory=list)

# ============================================================
# MAIN POLLER
# ============================================================

class WXSniper:
    def __init__(self):
        self.kalshi = KalshiClient()
        self.weather = WeatherClient()
        
        # Config
        self.live_mode = os.environ.get('LIVE_MODE', 'false').lower() == 'true'
        self.max_no_price = int(os.environ.get('MAX_NO_PRICE', '15'))
        self.max_yes_price = int(os.environ.get('MAX_YES_PRICE', '10'))
        
        # State
        self.states: Dict[str, StationState] = {}
        for station in STATIONS.keys():
            self.states[station] = StationState(station=station)
        
        # Tracking
        self.last_metar_poll: Optional[datetime] = None
        self.last_price_poll: Optional[datetime] = None
        self.trade_log: List[dict] = []
        self.opportunities: List[dict] = []  # Current opportunities
        
    def get_local_date(self, station: str) -> str:
        """Get today's date in station's local timezone."""
        tz_name = STATIONS.get(station, {}).get('tz', 'America/New_York')
        tz = ZoneInfo(tz_name)
        return datetime.now(tz).strftime('%Y-%m-%d')
    
    def get_local_time_str(self, station: str) -> str:
        """Get current time string in station's local timezone."""
        tz_name = STATIONS.get(station, {}).get('tz', 'America/New_York')
        tz = ZoneInfo(tz_name)
        return datetime.now(tz).strftime('%H:%M %Z')

    # ============================================================
    # METAR FETCHING
    # ============================================================
    
    def fetch_all_metars(self):
        """Fetch METARs for all stations, update observed temps."""
        print(f"[METAR] Fetching all stations...")
        
        for station, state in self.states.items():
            try:
                metar = self.weather.fetch_metar(station)
                if metar:
                    raw = metar.raw_text if hasattr(metar, 'raw_text') else str(metar)
                    parsed = parse_metar(raw)
                    
                    state.latest_metar = raw
                    state.metar_time = datetime.now(timezone.utc)
                    
                    if parsed.temp_f is not None:
                        state.latest_temp_f = parsed.temp_f
                        
                        # Update observed high/low
                        if state.observed_high is None or parsed.temp_f > state.observed_high:
                            state.observed_high = parsed.temp_f
                        if state.observed_low is None or parsed.temp_f < state.observed_low:
                            state.observed_low = parsed.temp_f
                    
                    # Check for 6-hour extremes in synoptic METARs
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
                    
                    print(f"  {station}: {state.latest_temp_f}°F (high={state.observed_high}, low={state.observed_low})")
                    
            except Exception as e:
                print(f"  {station}: ERROR - {e}")
        
        self.last_metar_poll = datetime.now(timezone.utc)
        print(f"[METAR] Done\n")

    # ============================================================
    # PRICE FETCHING
    # ============================================================
    
    def fetch_all_prices(self):
        """Fetch current prices for all brackets."""
        print(f"[PRICES] Fetching all brackets...")
        now = datetime.now(timezone.utc)
        today_suffix = now.strftime('%y%b%d').upper()  # e.g., "26JAN30"
        
        for station, state in self.states.items():
            cfg = STATIONS.get(station, {})
            high_ticker_base = cfg.get('kalshi_high_ticker')
            low_ticker_base = cfg.get('kalshi_low_ticker')
            
            state.high_brackets = []
            state.low_brackets = []
            
            # Fetch HIGH brackets
            if high_ticker_base:
                try:
                    high_event = f"{high_ticker_base}-{today_suffix}"
                    event_data = self.kalshi.get_event(high_event)
                    markets = event_data.get('markets', [])
                    
                    for m in markets:
                        ticker = m.get('ticker', '')
                        subtitle = m.get('yes_sub_title', m.get('subtitle', ''))
                        floor = m.get('floor_strike')
                        cap = m.get('cap_strike')
                        strike_type = m.get('strike_type', 'between')
                        
                        no_ask = m.get('no_ask') or 100
                        yes_ask = m.get('yes_ask') or 100
                        
                        bracket = Bracket(
                            ticker=ticker, subtitle=subtitle,
                            floor_strike=int(floor) if floor else None,
                            cap_strike=int(cap) if cap else None,
                            strike_type=strike_type, signal_type='high',
                            station=station, no_ask=int(no_ask), yes_ask=int(yes_ask)
                        )
                        state.high_brackets.append(bracket)
                    
                    print(f"  {station} HIGH: {len(state.high_brackets)} brackets")
                except Exception as e:
                    print(f"  {station} HIGH: ERROR - {e}")
            
            # Fetch LOW brackets
            if low_ticker_base:
                try:
                    low_event = f"{low_ticker_base}-{today_suffix}"
                    event_data = self.kalshi.get_event(low_event)
                    markets = event_data.get('markets', [])
                    
                    for m in markets:
                        ticker = m.get('ticker', '')
                        subtitle = m.get('yes_sub_title', m.get('subtitle', ''))
                        floor = m.get('floor_strike')
                        cap = m.get('cap_strike')
                        strike_type = m.get('strike_type', 'between')
                        
                        no_ask = m.get('no_ask') or 100
                        yes_ask = m.get('yes_ask') or 100
                        
                        bracket = Bracket(
                            ticker=ticker, subtitle=subtitle,
                            floor_strike=int(floor) if floor else None,
                            cap_strike=int(cap) if cap else None,
                            strike_type=strike_type, signal_type='low',
                            station=station, no_ask=int(no_ask), yes_ask=int(yes_ask)
                        )
                        state.low_brackets.append(bracket)
                    
                    print(f"  {station} LOW: {len(state.low_brackets)} brackets")
                except Exception as e:
                    print(f"  {station} LOW: ERROR - {e}")
        
        self.last_price_poll = datetime.now(timezone.utc)
        print(f"[PRICES] Done\n")

    # ============================================================
    # OPPORTUNITY DETECTION
    # ============================================================
    
    def find_opportunities(self) -> List[dict]:
        """
        Find all trading opportunities based on current state.
        
        Returns list of opportunities with action (buy NO or buy YES).
        """
        opportunities = []
        
        for station, state in self.states.items():
            # Check HIGH brackets
            for b in state.high_brackets:
                # NO opportunity: bracket is DEAD (observed > cap)
                if b.is_dead(state.observed_high, state.observed_low):
                    if b.no_ask <= self.max_no_price:
                        opportunities.append({
                            'station': station,
                            'bracket': b,
                            'action': 'BUY_NO',
                            'price': b.no_ask,
                            'reason': f"HIGH dead: obs {state.observed_high}°F > cap {b.cap_strike}",
                        })
                
                # YES opportunity: edge bracket not yet hit
                elif b.is_edge_opportunity(state.observed_high, state.observed_low):
                    if b.yes_ask <= self.max_yes_price:
                        opportunities.append({
                            'station': station,
                            'bracket': b,
                            'action': 'BUY_YES',
                            'price': b.yes_ask,
                            'reason': f"HIGH edge: obs {state.observed_high}°F < floor {b.floor_strike}",
                        })
            
            # Check LOW brackets
            for b in state.low_brackets:
                # NO opportunity: bracket is DEAD (observed < floor)
                if b.is_dead(state.observed_high, state.observed_low):
                    if b.no_ask <= self.max_no_price:
                        opportunities.append({
                            'station': station,
                            'bracket': b,
                            'action': 'BUY_NO',
                            'price': b.no_ask,
                            'reason': f"LOW dead: obs {state.observed_low}°F < floor {b.floor_strike}",
                        })
                
                # YES opportunity: edge bracket not yet hit
                elif b.is_edge_opportunity(state.observed_high, state.observed_low):
                    if b.yes_ask <= self.max_yes_price:
                        opportunities.append({
                            'station': station,
                            'bracket': b,
                            'action': 'BUY_YES',
                            'price': b.yes_ask,
                            'reason': f"LOW edge: obs {state.observed_low}°F > cap {b.cap_strike}",
                        })
        
        self.opportunities = opportunities
        return opportunities

    # ============================================================
    # TRADING
    # ============================================================
    
    def execute_trade(self, opp: dict) -> bool:
        """Execute a trade for an opportunity."""
        bracket = opp['bracket']
        action = opp['action']
        side = 'no' if action == 'BUY_NO' else 'yes'
        price = opp['price']
        
        trade_record = {
            'time': datetime.now(timezone.utc).isoformat(),
            'station': opp['station'],
            'ticker': bracket.ticker,
            'subtitle': bracket.subtitle,
            'side': side,
            'price': price,
            'reason': opp['reason'],
            'dry_run': not self.live_mode,
            'success': False,
        }
        
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
                trade_record['success'] = True
                trade_record['order_id'] = result.get('order', {}).get('order_id')
                print(f"[TRADE] ✅ {action} {bracket.subtitle} @ {price}¢")
            except Exception as e:
                trade_record['error'] = str(e)
                print(f"[TRADE] ❌ {action} {bracket.subtitle} failed: {e}")
        else:
            trade_record['success'] = True
            print(f"[DRY RUN] 🧪 {action} {bracket.subtitle} @ {price}¢")
        
        self.trade_log.append(trade_record)
        return trade_record['success']

    # ============================================================
    # MAIN LOOP
    # ============================================================
    
    def poll_cycle(self):
        """Run one polling cycle based on current time."""
        now = datetime.now(timezone.utc)
        minute = now.minute
        
        # Prep window: :45-:51 - build watchlist
        if 45 <= minute <= 51:
            # Fetch prices at :45
            if minute == 45 and (self.last_price_poll is None or 
                (now - self.last_price_poll).total_seconds() > 300):
                self.fetch_all_prices()
                self.find_opportunities()
                print(f"[PREP] Found {len(self.opportunities)} opportunities")
        
        # Hot window: :52-:02 - poll METARs and trade
        elif minute >= 52 or minute <= 2:
            # Fetch METARs
            if self.last_metar_poll is None or (now - self.last_metar_poll).total_seconds() > 60:
                self.fetch_all_metars()
                
                # Re-evaluate opportunities after new METAR data
                self.find_opportunities()
                
                # Execute trades
                for opp in self.opportunities:
                    self.execute_trade(opp)
        
        # Outside hot window: refresh prices every 10 min
        else:
            if self.last_price_poll is None or (now - self.last_price_poll).total_seconds() > 600:
                self.fetch_all_prices()
                self.find_opportunities()
    
    def run(self):
        """Main entry point."""
        print(f"[START] WX Sniper v3.6")
        print(f"[CONFIG] Live: {self.live_mode}")
        print(f"[CONFIG] Max NO: {self.max_no_price}¢ | Max YES: {self.max_yes_price}¢")
        print(f"[CONFIG] Stations: {list(self.states.keys())}")
        
        # Start health server
        HealthHandler.poller = self
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()
        
        # Initial fetch
        self.fetch_all_metars()
        self.fetch_all_prices()
        self.find_opportunities()
        
        # Main loop
        while True:
            self.poll_cycle()
            time.sleep(10)

# ============================================================
# HEALTH / UI SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    poller: Optional[WXSniper] = None
    
    def log_message(self, format, *args):
        pass  # Suppress logging
    
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            html = self.build_dashboard()
            self.wfile.write(html.encode())
    
    def build_dashboard(self) -> str:
        p = HealthHandler.poller
        if not p:
            return "<html><body>Poller not initialized</body></html>"
        
        now = datetime.now(timezone.utc)
        
        html = f'''<!DOCTYPE html>
<html><head>
<title>WX Sniper v3.6</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, sans-serif; padding: 20px; }}
h1 {{ color: #58a6ff; }}
h2 {{ color: #8b949e; border-bottom: 1px solid #30363d; padding-bottom: 8px; margin-top: 30px; }}
h3 {{ color: #58a6ff; margin-top: 20px; }}
table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
th, td {{ padding: 8px 12px; text-align: left; border: 1px solid #30363d; }}
th {{ background: #161b22; }}
tr:hover {{ background: #161b22; }}
.no {{ color: #f85149; }}
.yes {{ color: #3fb950; }}
.dead {{ color: #f85149; font-weight: bold; }}
.edge {{ color: #d29922; }}
.opportunity {{ background: #1c3d1c; }}
.metar {{ font-family: monospace; font-size: 11px; color: #8b949e; }}
.time {{ color: #8b949e; font-size: 12px; }}
</style>
</head><body>
<h1>🎯 WX Sniper v3.6</h1>
<p>
    Mode: <strong>{"🔴 LIVE" if p.live_mode else "🧪 DRY RUN"}</strong> |
    Max NO: <strong>{p.max_no_price}¢</strong> |
    Max YES: <strong>{p.max_yes_price}¢</strong>
</p>
<p class="time">
    UTC: {now.strftime("%Y-%m-%d %H:%M:%S")} |
    Last METAR: {p.last_metar_poll.strftime("%H:%M:%S") if p.last_metar_poll else "Never"} |
    Last Prices: {p.last_price_poll.strftime("%H:%M:%S") if p.last_price_poll else "Never"}
</p>
'''
        
        # Opportunities section
        html += f"<h2>🎯 Opportunities ({len(p.opportunities)})</h2>"
        if p.opportunities:
            html += '<table><tr><th>Station</th><th>Bracket</th><th>Action</th><th>Price</th><th>Reason</th></tr>'
            for opp in p.opportunities:
                b = opp['bracket']
                action_class = "yes" if opp['action'] == 'BUY_YES' else "no"
                html += f'''<tr class="opportunity">
                    <td>{opp['station']}</td>
                    <td>{b.subtitle}</td>
                    <td class="{action_class}">{opp['action']}</td>
                    <td>{opp['price']}¢</td>
                    <td>{opp['reason']}</td>
                </tr>'''
            html += '</table>'
        else:
            html += '<p>No opportunities found at current prices.</p>'
        
        # Station data
        html += "<h2>📊 Station Data</h2>"
        
        for station, state in p.states.items():
            cfg = STATIONS.get(station, {})
            city = cfg.get('name', station)
            local_time = p.get_local_time_str(station)
            
            # Header with METAR
            metar_preview = state.latest_metar[:60] + "..." if state.latest_metar and len(state.latest_metar) > 60 else (state.latest_metar or "None")
            
            html += f'''<h3>{city} ({station}) 
                <span class="time">🕐 {local_time}</span>
            </h3>
            <p>
                <strong>Observed:</strong> HIGH={state.observed_high or "?"}°F, LOW={state.observed_low or "?"}°F |
                <strong>Latest:</strong> {state.latest_temp_f or "?"}°F
            </p>
            <p class="metar">METAR: {metar_preview}</p>
            '''
            
            # HIGH brackets
            if state.high_brackets:
                html += '<table><tr><th>HIGH Bracket</th><th>Floor</th><th>Cap</th><th>NO Ask</th><th>YES Ask</th><th>Status</th></tr>'
                for b in sorted(state.high_brackets, key=lambda x: x.floor_strike or 0, reverse=True):
                    status = ""
                    row_class = ""
                    
                    if b.is_dead(state.observed_high, state.observed_low):
                        status = '<span class="dead">DEAD</span>'
                        if b.no_ask <= p.max_no_price:
                            status += ' → BUY NO'
                            row_class = "opportunity"
                    elif b.is_edge:
                        if b.is_edge_opportunity(state.observed_high, state.observed_low):
                            status = '<span class="edge">EDGE - not hit</span>'
                            if b.yes_ask <= p.max_yes_price:
                                status += ' → BUY YES'
                                row_class = "opportunity"
                        else:
                            status = '<span class="yes">EDGE - HIT</span>'
                    else:
                        status = "Open"
                    
                    html += f'''<tr class="{row_class}">
                        <td>{b.subtitle}</td>
                        <td>{b.floor_strike or "—"}</td>
                        <td>{b.cap_strike or "—"}</td>
                        <td class="no">{b.no_ask}¢</td>
                        <td class="yes">{b.yes_ask}¢</td>
                        <td>{status}</td>
                    </tr>'''
                html += '</table>'
            
            # LOW brackets
            if state.low_brackets:
                html += '<table><tr><th>LOW Bracket</th><th>Floor</th><th>Cap</th><th>NO Ask</th><th>YES Ask</th><th>Status</th></tr>'
                for b in sorted(state.low_brackets, key=lambda x: x.cap_strike or 999):
                    status = ""
                    row_class = ""
                    
                    if b.is_dead(state.observed_high, state.observed_low):
                        status = '<span class="dead">DEAD</span>'
                        if b.no_ask <= p.max_no_price:
                            status += ' → BUY NO'
                            row_class = "opportunity"
                    elif b.is_edge:
                        if b.is_edge_opportunity(state.observed_high, state.observed_low):
                            status = '<span class="edge">EDGE - not hit</span>'
                            if b.yes_ask <= p.max_yes_price:
                                status += ' → BUY YES'
                                row_class = "opportunity"
                        else:
                            status = '<span class="yes">EDGE - HIT</span>'
                    else:
                        status = "Open"
                    
                    html += f'''<tr class="{row_class}">
                        <td>{b.subtitle}</td>
                        <td>{b.floor_strike or "—"}</td>
                        <td>{b.cap_strike or "—"}</td>
                        <td class="no">{b.no_ask}¢</td>
                        <td class="yes">{b.yes_ask}¢</td>
                        <td>{status}</td>
                    </tr>'''
                html += '</table>'
        
        # Trade log
        if p.trade_log:
            html += f"<h2>💰 Trade Log ({len(p.trade_log)})</h2>"
            html += '<table><tr><th>Time</th><th>Station</th><th>Bracket</th><th>Side</th><th>Price</th><th>Status</th></tr>'
            for t in reversed(p.trade_log[-20:]):
                status = "✅" if t.get('success') else "❌"
                if t.get('dry_run'):
                    status = "🧪 DRY"
                side_class = t.get('side', 'no')
                html += f'''<tr>
                    <td class="time">{t.get('time', '')[:19]}</td>
                    <td>{t.get('station', '')}</td>
                    <td>{t.get('subtitle', '')}</td>
                    <td class="{side_class}">{t.get('side', '').upper()}</td>
                    <td>{t.get('price', '')}¢</td>
                    <td>{status}</td>
                </tr>'''
            html += '</table>'
        
        html += "</body></html>"
        return html

def start_health_server():
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    print(f"[HTTP] Server on port {port}")
    server.serve_forever()

# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    sniper = WXSniper()
    sniper.run()
