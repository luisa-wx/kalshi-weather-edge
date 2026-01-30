#!/usr/bin/env python3
"""
WX Sniper - Smart Poller v3.5

NEW IN v3.5:
- Completely redesigned lock override system
- Per-city LOCK buttons that persist until midnight LOCAL time
- Clear visual feedback on lock status
- Locks auto-clear at midnight in each city's timezone

FIXES from v3.4:
- Config key names: kalshi_high_ticker / kalshi_low_ticker
- Handles None tickers (stations without LOW markets)
"""

import os
import sys
import time
import json
import math
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, field

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aviation_weather import AviationWeatherPoller
from metar_parser import parse_metar
from kalshi_client import KalshiClient
from config import STATIONS

OVERRIDES_FILE = "lock_overrides.json"


def nws_round(value: float) -> int:
    """NWS-style rounding: round half away from zero."""
    if value >= 0:
        return int(math.floor(value + 0.5))
    else:
        return int(math.ceil(value - 0.5))


def get_local_date(station: str) -> str:
    """Get current date in station's local timezone (YYYY-MM-DD)."""
    tz_name = STATIONS.get(station, {}).get("timezone", "America/New_York")
    tz = ZoneInfo(tz_name)
    return datetime.now(tz).strftime("%Y-%m-%d")


def get_local_time_str(station: str) -> str:
    """Get current time string in station's local timezone."""
    tz_name = STATIONS.get(station, {}).get("timezone", "America/New_York")
    tz = ZoneInfo(tz_name)
    return datetime.now(tz).strftime("%H:%M %Z")


def load_overrides() -> dict:
    """Load overrides, structure: {station: {date: {high_locked: bool, low_locked: bool, locked_at: str}}}"""
    if os.path.exists(OVERRIDES_FILE):
        try:
            with open(OVERRIDES_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_overrides(overrides: dict):
    with open(OVERRIDES_FILE, 'w') as f:
        json.dump(overrides, f, indent=2)


def is_locked(station: str, lock_type: str) -> bool:
    """
    Check if a lock is active for station/type.
    Locks are stored per-station per-LOCAL-date and auto-expire at midnight local.
    """
    overrides = load_overrides()
    local_date = get_local_date(station)
    
    station_data = overrides.get(station, {})
    date_data = station_data.get(local_date, {})
    
    return date_data.get(f'{lock_type}_locked', False)


def set_lock(station: str, lock_type: str, locked: bool):
    """Set or clear a lock for station/type."""
    overrides = load_overrides()
    local_date = get_local_date(station)
    
    if station not in overrides:
        overrides[station] = {}
    if local_date not in overrides[station]:
        overrides[station][local_date] = {}
    
    overrides[station][local_date][f'{lock_type}_locked'] = locked
    if locked:
        overrides[station][local_date][f'{lock_type}_locked_at'] = datetime.now(timezone.utc).isoformat()
    
    save_overrides(overrides)


class HealthHandler(BaseHTTPRequestHandler):
    poller = None
    
    def do_GET(self):
        parsed_url = urlparse(self.path)
        if parsed_url.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self._serve_dashboard()
    
    def do_POST(self):
        """Handle lock toggle requests."""
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length).decode('utf-8')
        params = parse_qs(post_data)
        
        action = params.get('action', [''])[0]
        station = params.get('station', [''])[0]
        lock_type = params.get('type', [''])[0]  # 'high' or 'low'
        
        if action == 'toggle_lock' and station and lock_type:
            current = is_locked(station, lock_type)
            set_lock(station, lock_type, not current)
            status = "LOCKED" if not current else "UNLOCKED"
            print(f"[LOCK] {station} {lock_type.upper()} -> {status}")
        
        # Redirect back to dashboard
        self.send_response(303)
        self.send_header('Location', '/')
        self.end_headers()
    
    def _serve_dashboard(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        
        if HealthHandler.poller is None:
            self.wfile.write(b'<h1>WX Sniper - Starting...</h1>')
            return
        
        p = HealthHandler.poller
        now = datetime.now(timezone.utc)
        
        html = f'''<!DOCTYPE html><html><head><title>WX Sniper v3.5</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, monospace; background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 1400px; margin: 0 auto; }}
h1, h2, h3 {{ color: #58a6ff; margin-top: 20px; }}
table {{ border-collapse: collapse; margin: 10px 0; width: 100%; }}
th, td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: left; }}
th {{ background: #161b22; }}
tr:hover {{ background: #161b22; }}
.yes {{ color: #3fb950; font-weight: bold; }}
.no {{ color: #f85149; font-weight: bold; }}
.locked {{ background: #238636 !important; color: white; }}
.unlocked {{ background: #30363d; color: #8b949e; }}
.lock-btn {{ 
    padding: 8px 16px; 
    border: none; 
    border-radius: 6px; 
    cursor: pointer; 
    font-weight: bold;
    font-size: 14px;
    min-width: 100px;
}}
.lock-btn.locked {{ background: #238636; color: white; }}
.lock-btn.unlocked {{ background: #21262d; color: #8b949e; border: 1px solid #30363d; }}
.lock-btn.active {{ background: #238636; color: white; }}
.lock-btn:hover {{ opacity: 0.8; }}
.station-card {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 16px;
    margin: 10px 0;
}}
.station-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
}}
.station-name {{ font-size: 18px; font-weight: bold; color: #58a6ff; }}
.station-time {{ color: #8b949e; font-size: 12px; }}
.lock-controls {{ display: flex; gap: 10px; }}
.temp-display {{ font-size: 24px; margin: 10px 0; }}
.status-badge {{
    display: inline-block;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: bold;
    margin-left: 8px;
}}
.status-badge.active {{ background: #238636; color: white; }}
.status-badge.inactive {{ background: #30363d; color: #8b949e; }}
.edge {{ color: #d29922; }}
.trade-row {{ background: #1c2128; }}
.trade-row.success {{ border-left: 3px solid #238636; }}
.trade-row.dry {{ border-left: 3px solid #d29922; }}
</style></head><body>
<h1>🎯 WX Sniper v3.5</h1>
<p>
    <span style="margin-right: 20px;">Mode: <strong>{"🔴 LIVE" if p.live_mode else "🧪 DRY RUN"}</strong></span>
    <span style="margin-right: 20px;">Hourly: <strong>{"YES" if p.hourly_mode else "NO"}</strong></span>
    <span>Max Price: <strong>{p.max_no_price}¢</strong></span>
</p>
<p style="color: #8b949e;">UTC: {now.strftime("%Y-%m-%d %H:%M:%S")}</p>

<h2>🏙️ Station Locks</h2>
<p style="color: #8b949e; font-size: 13px;">
    Click to toggle. Locks auto-clear at midnight in each city's local time.<br>
    🔒 = Soft lock active (treats current temp as final for day)
</p>
'''
        
        # Station lock cards
        for station, state in p.market_states.items():
            cfg = STATIONS.get(station, {})
            city_name = cfg.get('name', station)
            local_time = get_local_time_str(station)
            
            high_locked = is_locked(station, 'high')
            low_locked = is_locked(station, 'low')
            
            obs_high = f"{state.observed_high}°F" if state.observed_high else "—"
            obs_low = f"{state.observed_low}°F" if state.observed_low else "—"
            
            has_high = state.high_ticker_base is not None
            has_low = state.low_ticker_base is not None
            
            html += f'''
<div class="station-card">
    <div class="station-header">
        <div>
            <span class="station-name">{city_name}</span>
            <span style="color: #8b949e; margin-left: 10px;">({station})</span>
        </div>
        <div class="station-time">🕐 {local_time}</div>
    </div>
    <div style="display: flex; justify-content: space-between; align-items: center;">
        <div class="temp-display">
            📈 High: <strong>{obs_high}</strong> &nbsp;&nbsp;
            📉 Low: <strong>{obs_low}</strong>
        </div>
        <div class="lock-controls">
'''
            # HIGH lock button
            if has_high:
                high_class = "locked" if high_locked else "unlocked"
                high_text = "🔒 HIGH LOCKED" if high_locked else "🔓 Lock HIGH"
                html += f'''
            <form method="POST" style="display:inline;">
                <input type="hidden" name="action" value="toggle_lock">
                <input type="hidden" name="station" value="{station}">
                <input type="hidden" name="type" value="high">
                <button type="submit" class="lock-btn {high_class}">{high_text}</button>
            </form>
'''
            
            # LOW lock button
            if has_low:
                low_class = "locked" if low_locked else "unlocked"
                low_text = "🔒 LOW LOCKED" if low_locked else "🔓 Lock LOW"
                html += f'''
            <form method="POST" style="display:inline;">
                <input type="hidden" name="action" value="toggle_lock">
                <input type="hidden" name="station" value="{station}">
                <input type="hidden" name="type" value="low">
                <button type="submit" class="lock-btn {low_class}">{low_text}</button>
            </form>
'''
            
            html += '''
        </div>
    </div>
</div>
'''
        
        # Watchlist
        total_brackets = sum(len(s.watchlist) for s in p.market_states.values())
        html += f"<h2>📋 Watchlist ({total_brackets} brackets)</h2>"
        
        for station, state in p.market_states.items():
            if not state.watchlist:
                continue
            
            cfg = STATIONS.get(station, {})
            city_name = cfg.get('name', station)
            high_locked = is_locked(station, 'high')
            low_locked = is_locked(station, 'low')
            
            # Show observed temps
            obs_high = state.observed_high if state.observed_high else "?"
            obs_low = state.observed_low if state.observed_low else "?"
            
            html += f'''<h3>{city_name} ({len(state.watchlist)} brackets)
                <span style="margin-left:10px; color:#8b949e;">Obs: ↑{obs_high}°F ↓{obs_low}°F</span>
            </h3>
            <div style="margin-bottom:10px;">
                <form method="POST" style="display:inline;">
                    <input type="hidden" name="action" value="toggle_lock">
                    <input type="hidden" name="station" value="{station}">
                    <input type="hidden" name="type" value="high">
                    <button type="submit" class="lock-btn {"active" if high_locked else ""}">
                        {"🔒 HIGH LOCKED" if high_locked else "🔓 Lock HIGH"}
                    </button>
                </form>
                <form method="POST" style="display:inline;">
                    <input type="hidden" name="action" value="toggle_lock">
                    <input type="hidden" name="station" value="{station}">
                    <input type="hidden" name="type" value="low">
                    <button type="submit" class="lock-btn {"active" if low_locked else ""}">
                        {"🔒 LOW LOCKED" if low_locked else "🔓 Lock LOW"}
                    </button>
                </form>
            </div>'''
            html += '<table><tr><th>Bracket</th><th>Type</th><th>NO Ask</th><th>YES Ask</th><th>Floor</th><th>Cap</th><th>Status</th></tr>'
            
            for b in state.watchlist:
                edge = '🎯 EDGE' if b.is_edge_bracket else ""
                
                # Determine bracket status based on observed temps
                bracket_status = ""
                if b.signal_type == 'high':
                    if state.observed_high is not None:
                        # For HIGH "X or above" edge brackets: LOCKED if observed >= floor
                        if b.is_edge_bracket and b.floor_strike is not None:
                            if state.observed_high >= b.floor_strike:
                                bracket_status = '<span style="color:#238636;">✓ LOCKED</span>'
                            else:
                                bracket_status = f'<span style="color:#d29922;">{edge}</span>'
                        # For HIGH range brackets (floor to cap): IN RANGE if floor <= observed <= cap
                        elif b.floor_strike is not None and b.cap_strike is not None:
                            if b.floor_strike <= state.observed_high <= b.cap_strike:
                                bracket_status = '<span style="color:#f0883e;">⚠ IN RANGE</span>'
                            elif state.observed_high > b.cap_strike:
                                bracket_status = '<span style="color:#8b949e;">PASSED</span>'
                                
                elif b.signal_type == 'low':
                    if state.observed_low is not None:
                        # For LOW "X or below" edge brackets: LOCKED if observed <= cap
                        if b.is_edge_bracket and b.cap_strike is not None:
                            if state.observed_low <= b.cap_strike:
                                bracket_status = '<span style="color:#238636;">✓ LOCKED</span>'
                            else:
                                bracket_status = f'<span style="color:#d29922;">{edge}</span>'
                        # For LOW range brackets: IN RANGE if floor <= observed <= cap
                        elif b.floor_strike is not None and b.cap_strike is not None:
                            if b.floor_strike <= state.observed_low <= b.cap_strike:
                                bracket_status = '<span style="color:#f0883e;">⚠ IN RANGE</span>'
                            elif state.observed_low < b.floor_strike:
                                bracket_status = '<span style="color:#8b949e;">PASSED</span>'
                
                if not bracket_status:
                    bracket_status = f'<span style="color:#d29922;">{edge}</span>' if edge else "—"
                
                html += f'''<tr>
                    <td>{b.subtitle}</td>
                    <td>{b.signal_type.upper()}</td>
                    <td class="no">{b.no_ask}¢</td>
                    <td class="yes">{b.yes_ask}¢</td>
                    <td>{b.floor_strike or "—"}</td>
                    <td>{b.cap_strike or "—"}</td>
                    <td>{bracket_status}</td>
                </tr>'''
            html += "</table>"
        
        # Latest METARs
        if p.latest_metars:
            html += "<h2>📡 Latest METARs</h2>"
            html += '<table><tr><th>Station</th><th>Temp</th><th>Raw METAR</th></tr>'
            for st, metar in p.latest_metars.items():
                temp = p.latest_temps.get(st, "?")
                html += f'<tr><td>{st}</td><td><strong>{temp}°F</strong></td><td style="font-size:11px; color:#8b949e;">{metar[:80]}...</td></tr>'
            html += "</table>"
        
        # Trade log
        if p.trade_log:
            html += f"<h2>💰 Recent Trades ({len(p.trade_log)} total)</h2>"
            html += '<table><tr><th>Time</th><th>Station</th><th>Bracket</th><th>Side</th><th>Price</th><th>Temp</th><th>Status</th></tr>'
            for t in reversed(p.trade_log[-15:]):
                status = "✅" if t.get('success') else "❌"
                row_class = "trade-row"
                if t.get('dry_run'):
                    status = "🧪 DRY"
                    row_class += " dry"
                elif t.get('success'):
                    row_class += " success"
                if t.get('soft_lock'):
                    status += " ⏰"
                
                side_class = "yes" if t.get('side') == 'yes' else "no"
                html += f'''<tr class="{row_class}">
                    <td>{t.get("time", "?")}</td>
                    <td>{t.get("station", "?")}</td>
                    <td>{t.get("subtitle", t.get("ticker", "?"))}</td>
                    <td class="{side_class}">{t.get("side", "?").upper()}</td>
                    <td>{t.get("price", "?")}¢</td>
                    <td>{t.get("observed", "?")}°F</td>
                    <td>{status}</td>
                </tr>'''
            html += "</table>"
        
        html += '''
<br><br>
<p style="color: #30363d; font-size: 11px;">Auto-refreshes every 30 seconds</p>
</body></html>'''
        
        self.wfile.write(html.encode())
    
    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def start_health_server():
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    print(f"[HEALTH] Server on port {port}")
    server.serve_forever()


@dataclass
class BracketInfo:
    ticker: str
    event_ticker: str
    subtitle: str
    floor_strike: Optional[int]
    cap_strike: Optional[int]
    strike_type: str  # 'between', 'greater', 'less'
    signal_type: str  # 'high' or 'low'
    no_ask: int
    yes_ask: int
    station: str
    is_edge_bracket: bool = False


@dataclass
class MarketState:
    station: str
    high_ticker_base: Optional[str]
    low_ticker_base: Optional[str]
    watchlist: List[BracketInfo] = field(default_factory=list)
    observed_high: Optional[int] = None
    observed_low: Optional[int] = None
    traded_tickers: set = field(default_factory=set)


class SmartPoller:
    def __init__(self, live_mode=False, max_no_price=93, max_yes_price=93, hourly_mode=False):
        self.live_mode = live_mode
        self.max_no_price = max_no_price
        self.max_yes_price = max_yes_price
        self.hourly_mode = hourly_mode
        
        self.kalshi = KalshiClient()
        self.weather = AviationWeatherPoller()
        
        self.market_states: Dict[str, MarketState] = {}
        self.latest_metars: Dict[str, str] = {}
        self.latest_temps: Dict[str, int] = {}
        self.trade_log: List[dict] = []
        
        for station, cfg in STATIONS.items():
            self.market_states[station] = MarketState(
                station=station,
                high_ticker_base=cfg.get('kalshi_high_ticker'),
                low_ticker_base=cfg.get('kalshi_low_ticker')
            )
    
    def build_watchlist(self):
        today = datetime.now(timezone.utc).strftime("%y%b%d").upper()
        
        for station, state in self.market_states.items():
            print(f"[WATCHLIST] Building for {station}...")
            state.watchlist = []
            
            # HIGH markets
            if state.high_ticker_base:
                high_event = f"{state.high_ticker_base}-{today}"
                high_brackets = []
                try:
                    markets = self.kalshi.get_markets(event_ticker=high_event)
                    for m in markets:
                        if m.get('status') not in ['active', 'open']:
                            continue
                        
                        ticker = m.get('ticker', '')
                        subtitle = m.get('yes_sub_title', m.get('subtitle', ''))
                        floor = m.get('floor_strike')
                        cap = m.get('cap_strike')
                        strike_type = m.get('strike_type', 'between')
                        
                        # Get prices directly from market object (more reliable than orderbook)
                        no_ask = m.get('no_ask', 100)
                        yes_ask = m.get('yes_ask', 100)
                        # Handle None values
                        if no_ask is None: no_ask = 100
                        if yes_ask is None: yes_ask = 100
                        
                        bracket = BracketInfo(
                            ticker=ticker, event_ticker=high_event, subtitle=subtitle,
                            floor_strike=int(floor) if floor else None,
                            cap_strike=int(cap) if cap else None,
                            strike_type=strike_type, signal_type='high',
                            no_ask=int(no_ask), yes_ask=int(yes_ask), station=station
                        )
                        high_brackets.append(bracket)
                    
                    # Mark edge brackets for HIGH (the "X or above" bracket is the edge)
                    for b in high_brackets:
                        if b.strike_type == 'greater':
                            b.is_edge_bracket = True
                        if b.no_ask <= self.max_no_price or (b.is_edge_bracket and b.yes_ask <= self.max_yes_price):
                            state.watchlist.append(b)
                            edge_str = " 🎯 EDGE" if b.is_edge_bracket else ""
                            print(f"  ✓ {b.subtitle:<18} (high) NO@{b.no_ask}¢ YES@{b.yes_ask}¢{edge_str}")
                            
                except Exception as e:
                    print(f"[ERROR] {station} high: {e}")
            
            # LOW markets
            if state.low_ticker_base:
                low_event = f"{state.low_ticker_base}-{today}"
                low_brackets = []
                try:
                    markets = self.kalshi.get_markets(event_ticker=low_event)
                    for m in markets:
                        if m.get('status') not in ['active', 'open']:
                            continue
                        
                        ticker = m.get('ticker', '')
                        subtitle = m.get('yes_sub_title', m.get('subtitle', ''))
                        floor = m.get('floor_strike')
                        cap = m.get('cap_strike')
                        strike_type = m.get('strike_type', 'between')
                        
                        # Get prices directly from market object (more reliable than orderbook)
                        no_ask = m.get('no_ask', 100)
                        yes_ask = m.get('yes_ask', 100)
                        # Handle None values
                        if no_ask is None: no_ask = 100
                        if yes_ask is None: yes_ask = 100
                        
                        bracket = BracketInfo(
                            ticker=ticker, event_ticker=low_event, subtitle=subtitle,
                            floor_strike=int(floor) if floor else None,
                            cap_strike=int(cap) if cap else None,
                            strike_type=strike_type, signal_type='low',
                            no_ask=int(no_ask), yes_ask=int(yes_ask), station=station
                        )
                        low_brackets.append(bracket)
                    
                    # Mark edge brackets for LOW (the "X or below" bracket is the edge)
                    for b in low_brackets:
                        if b.strike_type == 'less':
                            b.is_edge_bracket = True
                        if b.no_ask <= self.max_no_price or (b.is_edge_bracket and b.yes_ask <= self.max_yes_price):
                            state.watchlist.append(b)
                            edge_str = " 🎯 EDGE" if b.is_edge_bracket else ""
                            print(f"  ✓ {b.subtitle:<18} (low) NO@{b.no_ask}¢ YES@{b.yes_ask}¢{edge_str}")
                            
                except Exception as e:
                    print(f"[ERROR] {station} low: {e}")
            
            print(f"[WATCHLIST] {station}: {len(state.watchlist)} brackets")
    
    # ============ NO LOCK LOGIC ============
    
    def _is_no_locked_for_high(self, observed: int, bracket: BracketInfo, soft: bool = False) -> bool:
        """Check if NO is locked for a HIGH bracket."""
        if bracket.strike_type == 'between':
            if bracket.cap_strike is None:
                return False
            if observed > bracket.cap_strike:
                return True  # Hard lock
            if soft and observed >= bracket.cap_strike:
                return True  # Soft lock at cap
            return False
        elif bracket.strike_type == 'less':
            # "X or below" - NO locked if observed >= cap (exceeded the max)
            return bracket.cap_strike is not None and observed >= bracket.cap_strike
        return False  # 'greater' can never lock NO for HIGH
    
    def _is_no_locked_for_low(self, observed: int, bracket: BracketInfo, soft: bool = False) -> bool:
        """Check if NO is locked for a LOW bracket."""
        if bracket.strike_type == 'between':
            if bracket.floor_strike is None:
                return False
            if observed < bracket.floor_strike:
                return True  # Hard lock
            if soft and observed <= bracket.floor_strike:
                return True  # Soft lock at floor
            return False
        elif bracket.strike_type == 'greater':
            # "X or above" - NO locked if observed < floor
            if bracket.floor_strike is None:
                return False
            if observed < bracket.floor_strike:
                return True  # Hard lock
            if soft and observed <= bracket.floor_strike:
                return True  # Soft lock at floor
            return False
        return False  # 'less' can never lock NO for LOW
    
    # ============ YES LOCK LOGIC ============
    
    def _is_yes_locked_for_high(self, observed: int, bracket: BracketInfo, soft: bool = False) -> bool:
        """YES locks on edge HIGH brackets when temp hits/exceeds the threshold."""
        if not bracket.is_edge_bracket:
            return False
        if bracket.strike_type == 'greater':
            if bracket.floor_strike is None:
                return False
            if observed >= bracket.floor_strike:
                return True  # Hard lock
            return False
        return False
    
    def _is_yes_locked_for_low(self, observed: int, bracket: BracketInfo, soft: bool = False) -> bool:
        """YES locks on edge LOW brackets when temp hits/drops to the threshold."""
        if not bracket.is_edge_bracket:
            return False
        if bracket.strike_type == 'less':
            if bracket.cap_strike is None:
                return False
            if observed <= bracket.cap_strike:
                return True  # Hard lock
            return False
        return False
    
    def check_and_trade(self, state: MarketState, metar_text: str) -> List[dict]:
        results = []
        parsed = parse_metar(metar_text)
        if not parsed.station:
            return results
        
        # Check soft locks (user-set via UI)
        high_soft = is_locked(state.station, 'high')
        low_soft = is_locked(state.station, 'low')
        
        if high_soft:
            print(f"[SIGNAL] {state.station} ⏰ HIGH soft lock ACTIVE")
        if low_soft:
            print(f"[SIGNAL] {state.station} ⏰ LOW soft lock ACTIVE")
        
        if self.hourly_mode:
            if parsed.t_group_temp_c is not None:
                temp_c = parsed.t_group_temp_c
                current_temp = nws_round(temp_c * 9/5 + 32)
                print(f"[SIGNAL] {state.station} HOURLY: {current_temp}°F (T-group {temp_c}°C)")
                
                # Update observed temps
                if state.observed_high is None or current_temp > state.observed_high:
                    state.observed_high = current_temp
                if state.observed_low is None or current_temp < state.observed_low:
                    state.observed_low = current_temp
                
                for bracket in state.watchlist:
                    if bracket.ticker in state.traded_tickers:
                        continue
                    
                    if bracket.signal_type == 'high':
                        # Check NO lock
                        if bracket.no_ask <= self.max_no_price:
                            if self._is_no_locked_for_high(current_temp, bracket, soft=high_soft):
                                results.append(self._execute_trade(bracket, current_temp, state, side='no', soft_lock=high_soft))
                                continue
                        # Check YES lock (edge brackets only)
                        if bracket.is_edge_bracket and bracket.yes_ask <= self.max_yes_price:
                            if self._is_yes_locked_for_high(current_temp, bracket, soft=high_soft):
                                results.append(self._execute_trade(bracket, current_temp, state, side='yes', soft_lock=high_soft))
                    else:  # low
                        # Check NO lock
                        if bracket.no_ask <= self.max_no_price:
                            if self._is_no_locked_for_low(current_temp, bracket, soft=low_soft):
                                results.append(self._execute_trade(bracket, current_temp, state, side='no', soft_lock=low_soft))
                                continue
                        # Check YES lock (edge brackets only)
                        if bracket.is_edge_bracket and bracket.yes_ask <= self.max_yes_price:
                            if self._is_yes_locked_for_low(current_temp, bracket, soft=low_soft):
                                results.append(self._execute_trade(bracket, current_temp, state, side='yes', soft_lock=low_soft))
        
        # 6-hourly max/min
        if parsed.six_hour_max_c is not None:
            observed = nws_round(parsed.six_hour_max_c * 9/5 + 32)
            print(f"[SIGNAL] {state.station} 6HR MAX: {observed}°F")
            
            if state.observed_high is None or observed > state.observed_high:
                state.observed_high = observed
            
            for bracket in state.watchlist:
                if bracket.ticker in state.traded_tickers:
                    continue
                if bracket.signal_type == 'high':
                    if bracket.no_ask <= self.max_no_price:
                        if self._is_no_locked_for_high(observed, bracket, soft=high_soft):
                            results.append(self._execute_trade(bracket, observed, state, side='no', soft_lock=high_soft))
                            continue
                    if bracket.is_edge_bracket and bracket.yes_ask <= self.max_yes_price:
                        if self._is_yes_locked_for_high(observed, bracket, soft=high_soft):
                            results.append(self._execute_trade(bracket, observed, state, side='yes', soft_lock=high_soft))
        
        if parsed.six_hour_min_c is not None:
            observed = nws_round(parsed.six_hour_min_c * 9/5 + 32)
            print(f"[SIGNAL] {state.station} 6HR MIN: {observed}°F")
            
            if state.observed_low is None or observed < state.observed_low:
                state.observed_low = observed
            
            for bracket in state.watchlist:
                if bracket.ticker in state.traded_tickers:
                    continue
                if bracket.signal_type == 'low':
                    if bracket.no_ask <= self.max_no_price:
                        if self._is_no_locked_for_low(observed, bracket, soft=low_soft):
                            results.append(self._execute_trade(bracket, observed, state, side='no', soft_lock=low_soft))
                            continue
                    if bracket.is_edge_bracket and bracket.yes_ask <= self.max_yes_price:
                        if self._is_yes_locked_for_low(observed, bracket, soft=low_soft):
                            results.append(self._execute_trade(bracket, observed, state, side='yes', soft_lock=low_soft))
        
        return results
    
    def _execute_trade(self, bracket: BracketInfo, observed: int, state: MarketState, side: str, soft_lock: bool = False) -> dict:
        now = datetime.now(timezone.utc)
        
        price = bracket.yes_ask if side == 'yes' else bracket.no_ask
        
        trade_info = {
            'time': now.strftime("%H:%M:%SZ"),
            'station': bracket.station,
            'ticker': bracket.ticker,
            'subtitle': bracket.subtitle,
            'side': side,
            'price': price,
            'observed': observed,
            'signal_type': bracket.signal_type,
            'soft_lock': soft_lock,
            'dry_run': not self.live_mode,
            'success': False
        }
        
        lock_type = "⏰ SOFT" if soft_lock else "🔒 HARD"
        side_emoji = "🟢" if side == 'yes' else "🔴"
        print(f"[TRADE] {lock_type} LOCK {side_emoji} {side.upper()}: {bracket.station} {bracket.subtitle} ({bracket.signal_type}) - Observed: {observed}°F - {side.upper()} @ {price}¢")
        
        if self.live_mode:
            try:
                result = self.kalshi.create_order(
                    ticker=bracket.ticker,
                    side=side,
                    action='buy',
                    count=1,
                    order_type='market'
                )
                trade_info['success'] = True
                trade_info['order_id'] = result.get('order', {}).get('order_id')
                print(f"[TRADE] ✅ Order placed: {result}")
            except Exception as e:
                print(f"[TRADE] ❌ Failed: {e}")
                trade_info['error'] = str(e)
        else:
            print(f"[TRADE] 🧪 DRY RUN - would buy {side.upper()} @ {price}¢")
            trade_info['success'] = True
        
        state.traded_tickers.add(bracket.ticker)
        self.trade_log.append(trade_info)
        return trade_info
    
    def poll_cycle(self):
        now = datetime.now(timezone.utc)
        minute = now.minute
        
        in_hot_window = (minute >= 52) or (minute <= 2)
        if not in_hot_window:
            return
        
        print(f"\n[{now.strftime('%H:%MZ')}] 🟢 HOT WINDOW")
        
        for station in self.market_states.keys():
            try:
                metar = self.weather.fetch_metar(station)
                if metar:
                    self.latest_metars[station] = metar.raw_text if hasattr(metar, 'raw_text') else str(metar)
                    raw_text = self.latest_metars[station]
                    print(f"[METAR] {station}: {raw_text[:70]}...")
                    
                    parsed = parse_metar(raw_text)
                    if parsed.t_group_temp_c is not None:
                        temp_f = nws_round(parsed.t_group_temp_c * 9/5 + 32)
                        self.latest_temps[station] = temp_f
                    
                    state = self.market_states[station]
                    self.check_and_trade(state, raw_text)
            except Exception as e:
                print(f"[ERROR] {station}: {e}")
    
    def initial_fetch(self):
        """Fetch METARs for all stations on startup to populate UI."""
        print(f"\n[STARTUP] Fetching initial METARs...")
        for station in self.market_states.keys():
            try:
                metar = self.weather.fetch_metar(station)
                if metar:
                    raw_text = metar.raw_text if hasattr(metar, 'raw_text') else str(metar)
                    self.latest_metars[station] = raw_text
                    
                    parsed = parse_metar(raw_text)
                    if parsed.t_group_temp_c is not None:
                        temp_f = nws_round(parsed.t_group_temp_c * 9/5 + 32)
                        self.latest_temps[station] = temp_f
                        
                        # Update observed temps
                        state = self.market_states[station]
                        if state.observed_high is None or temp_f > state.observed_high:
                            state.observed_high = temp_f
                        if state.observed_low is None or temp_f < state.observed_low:
                            state.observed_low = temp_f
                    
                    print(f"[STARTUP] {station}: {self.latest_temps.get(station, '?')}°F")
            except Exception as e:
                print(f"[STARTUP] {station} error: {e}")
        print(f"[STARTUP] Done - fetched {len(self.latest_metars)} METARs\n")
    
    def run(self):
        print(f"[START] WX Sniper v3.5")
        print(f"[CONFIG] Live: {self.live_mode} | Hourly: {self.hourly_mode}")
        print(f"[CONFIG] Max NO: {self.max_no_price}¢ | Max YES: {self.max_yes_price}¢")
        print(f"[CONFIG] Stations: {list(self.market_states.keys())}")
        
        HealthHandler.poller = self
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()
        
        self.build_watchlist()
        self.initial_fetch()  # Fetch METARs immediately on startup
        last_watchlist_build = datetime.now(timezone.utc)
        
        while True:
            now = datetime.now(timezone.utc)
            
            # Rebuild watchlist at :48 every hour
            if now.minute == 48 and (now - last_watchlist_build).total_seconds() > 300:
                self.build_watchlist()
                last_watchlist_build = now
            
            self.poll_cycle()
            time.sleep(5)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='WX Sniper Smart Poller')
    parser.add_argument('--live', action='store_true', help='Enable live trading')
    parser.add_argument('--max-no-price', type=int, default=93, help='Max NO price in cents')
    parser.add_argument('--max-yes-price', type=int, default=93, help='Max YES price in cents (edge brackets)')
    parser.add_argument('--hourly', action='store_true', help='Use hourly T-group temps')
    args = parser.parse_args()
    
    poller = SmartPoller(
        live_mode=args.live,
        max_no_price=args.max_no_price,
        max_yes_price=args.max_yes_price,
        hourly_mode=args.hourly
    )
    poller.run()


if __name__ == '__main__':
    main()
