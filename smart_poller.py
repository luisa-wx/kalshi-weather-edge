#!/usr/bin/env python3
"""
WX Sniper - Smart Poller v3.4

NEW IN v3.4:
- YES logic for edge brackets (lowest "X or below", highest "X or above")
- When temp hits the edge bracket, YES is locked because there's nowhere else to go

FIXES from v3.3:
- Corrected lock logic for 'greater' type on LOW markets
- Use nws_round() instead of Python round()

Edge bracket examples:
- LOW "6° or below" at 6°F → YES locked (can't go lower than lowest bracket)
- HIGH "85° or above" at 85°F → YES locked (can't go higher than highest bracket)
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


def load_overrides() -> dict:
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


class HealthHandler(BaseHTTPRequestHandler):
    poller = None
    
    def do_GET(self):
        parsed_url = urlparse(self.path)
        if parsed_url.path == '/config':
            self._serve_config_page()
        else:
            self._serve_status_page()
    
    def do_POST(self):
        if self.path == '/config':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length).decode('utf-8')
            params = parse_qs(post_data)
            
            overrides = load_overrides()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today not in overrides:
                overrides[today] = {}
            
            for station in HealthHandler.poller.market_states.keys():
                high_val = params.get(f"{station}_high", [''])[0].strip()
                low_val = params.get(f"{station}_low", [''])[0].strip()
                
                if station not in overrides[today]:
                    overrides[today][station] = {}
                
                overrides[today][station]['high_locked_after'] = high_val if high_val else None
                overrides[today][station]['low_locked_after'] = low_val if low_val else None
            
            save_overrides(overrides)
            self.send_response(303)
            self.send_header('Location', '/config?saved=1')
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
    
    def _serve_config_page(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        
        if HealthHandler.poller is None:
            self.wfile.write(b'<h1>WX Sniper - Starting...</h1>')
            return
        
        p = HealthHandler.poller
        overrides = load_overrides()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_overrides = overrides.get(today, {})
        saved = '?saved=1' in self.path
        
        html = '''<!DOCTYPE html><html><head><title>WX Sniper Config</title>
<style>
body { font-family: monospace; background: #1a1a1a; color: #e0e0e0; padding: 20px; }
h1, h2 { color: #00ff88; }
table { border-collapse: collapse; margin: 10px 0; }
th, td { border: 1px solid #444; padding: 8px; text-align: left; }
th { background: #333; }
input[type="text"] { width: 80px; background: #333; color: #fff; border: 1px solid #555; padding: 4px; }
button { background: #00aa55; color: white; border: none; padding: 10px 20px; cursor: pointer; margin-top: 10px; }
.saved { color: #00ff88; font-weight: bold; }
.help { color: #888; font-size: 12px; }
</style></head><body>
<h1>⚙️ WX Sniper Lock Overrides</h1>
'''
        if saved:
            html += '<p class="saved">✅ Settings saved!</p>'
        
        html += '''
<div class="help">
<p><strong>Lock Override Times:</strong> Enter HH:MM (UTC) to mark when a high/low is "settled".</p>
<p>Leave blank = only hard locks from observed temps will trigger trades.</p>
</div>
<form method="POST">
<table>
<tr><th>Station</th><th>High Override (UTC)</th><th>Low Override (UTC)</th><th>Current High</th><th>Current Low</th></tr>
'''
        for station, state in p.market_states.items():
            st_overrides = today_overrides.get(station, {})
            high_val = st_overrides.get('high_locked_after') or ''
            low_val = st_overrides.get('low_locked_after') or ''
            obs_high = state.observed_high if state.observed_high else '-'
            obs_low = state.observed_low if state.observed_low else '-'
            
            html += f'''<tr>
<td>{station}</td>
<td><input type="text" name="{station}_high" value="{high_val}" placeholder="HH:MM"></td>
<td><input type="text" name="{station}_low" value="{low_val}" placeholder="HH:MM"></td>
<td>{obs_high}°F</td>
<td>{obs_low}°F</td>
</tr>'''
        
        html += '''
</table>
<button type="submit">💾 Save Overrides</button>
</form>
<br><a href="/" style="color: #00ff88;">← Back to Status</a>
</body></html>'''
        self.wfile.write(html.encode())
    
    def _serve_status_page(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        
        if HealthHandler.poller is None:
            self.wfile.write(b'<h1>WX Sniper - Starting...</h1>')
            return
        
        p = HealthHandler.poller
        now = datetime.now(timezone.utc)
        
        html = f'''<!DOCTYPE html><html><head><title>WX Sniper</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: monospace; background: #1a1a1a; color: #e0e0e0; padding: 20px; }}
h1, h2 {{ color: #00ff88; }}
table {{ border-collapse: collapse; margin: 10px 0; }}
th, td {{ border: 1px solid #444; padding: 8px; text-align: left; }}
th {{ background: #333; }}
.cheap {{ color: #ffaa00; }}
.yes {{ color: #00ff88; }}
.no {{ color: #ff4444; }}
</style></head><body>
<h1>🎯 WX Sniper v3.4 (YES+NO)</h1>
<p>Mode: {"LIVE 💰" if p.live_mode else "DRY RUN 🧪"} | Hourly: {"YES" if p.hourly_mode else "NO"} | Max: {p.max_no_price}¢</p>
<p>Time: {now.strftime("%Y-%m-%d %H:%M:%SZ")}</p>
<p><a href="/config" style="color: #00ff88;">⚙️ Configure Lock Overrides</a></p>
'''
        
        html += "<h2>📋 Watchlist</h2>"
        for station, state in p.market_states.items():
            if not state.watchlist:
                continue
            html += f"<h3>{station}</h3><table><tr><th>Bracket</th><th>Type</th><th>NO Ask</th><th>YES Ask</th><th>Floor</th><th>Cap</th><th>Edge?</th></tr>"
            for b in state.watchlist:
                edge = "🎯" if b.is_edge_bracket else ""
                html += f'<tr><td>{b.subtitle}</td><td>{b.signal_type.upper()}</td><td class="no">{b.no_ask}¢</td><td class="yes">{b.yes_ask}¢</td><td>{b.floor_strike or "-"}</td><td>{b.cap_strike or "-"}</td><td>{edge}</td></tr>'
            html += "</table>"
        
        if p.latest_metars:
            html += "<h2>📡 METARs</h2><table><tr><th>Station</th><th>Temp</th><th>Raw</th></tr>"
            for st, metar in p.latest_metars.items():
                html += f'<tr><td>{st}</td><td>{p.latest_temps.get(st,"?")}°F</td><td style="font-size:11px">{metar[:60]}...</td></tr>'
            html += "</table>"
        
        if p.trade_log:
            html += "<h2>💰 Trades</h2><table><tr><th>Time</th><th>Station</th><th>Bracket</th><th>Side</th><th>Price</th><th>Status</th></tr>"
            for t in reversed(p.trade_log[-10:]):
                st = "✅" if t.get('success') else "❌"
                if t.get('dry_run'): st = "🧪"
                if t.get('soft_lock'): st += "⏰"
                side_class = "yes" if t.get('side') == 'yes' else "no"
                html += f'<tr><td>{t.get("time","?")}</td><td>{t.get("station","?")}</td><td>{t.get("subtitle",t.get("ticker","?"))}</td><td class="{side_class}">{t.get("side","?").upper()}</td><td>{t.get("price","?")}¢</td><td>{st}</td></tr>'
            html += "</table>"
        
        html += '<br><small>Refreshes every 30s</small></body></html>'
        self.wfile.write(html.encode())
    
    def log_message(self, format, *args):
        pass


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
    is_edge_bracket: bool = False  # True if this is the lowest/highest bracket


@dataclass
class MarketState:
    station: str
    high_ticker_base: str
    low_ticker_base: str
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
                high_ticker_base=cfg['high_ticker'],
                low_ticker_base=cfg['low_ticker']
            )
    
    def build_watchlist(self):
        today = datetime.now(timezone.utc).strftime("%y%b%d").upper()
        
        for station, state in self.market_states.items():
            print(f"[WATCHLIST] Building for {station}...")
            state.watchlist = []
            
            # HIGH markets
            high_event = f"{state.high_ticker_base}-{today}"
            high_brackets = []
            try:
                markets = self.kalshi.get_markets_for_event(high_event)
                for m in markets:
                    if m.get('status') != 'active':
                        continue
                    
                    ticker = m.get('ticker', '')
                    subtitle = m.get('yes_sub_title', m.get('subtitle', ''))
                    floor = m.get('floor_strike')
                    cap = m.get('cap_strike')
                    strike_type = m.get('strike_type', 'between')
                    
                    orderbook = self.kalshi.get_orderbook(ticker)
                    no_asks = orderbook.get('no', {}).get('asks', [])
                    yes_asks = orderbook.get('yes', {}).get('asks', [])
                    no_ask = min([a[0] for a in no_asks]) if no_asks else 100
                    yes_ask = min([a[0] for a in yes_asks]) if yes_asks else 100
                    
                    bracket = BracketInfo(
                        ticker=ticker, event_ticker=high_event, subtitle=subtitle,
                        floor_strike=int(floor) if floor else None,
                        cap_strike=int(cap) if cap else None,
                        strike_type=strike_type, signal_type='high',
                        no_ask=no_ask, yes_ask=yes_ask, station=station
                    )
                    high_brackets.append(bracket)
                
                # Mark edge brackets for HIGH (the "X or above" bracket is the edge)
                for b in high_brackets:
                    if b.strike_type == 'greater':
                        b.is_edge_bracket = True
                    # Add to watchlist if price is good for either side
                    if b.no_ask <= self.max_no_price or (b.is_edge_bracket and b.yes_ask <= self.max_yes_price):
                        state.watchlist.append(b)
                        edge_str = " 🎯 EDGE" if b.is_edge_bracket else ""
                        print(f"  ✓ {b.subtitle:<18} (high) NO@{b.no_ask}¢ YES@{b.yes_ask}¢{edge_str}")
                        
            except Exception as e:
                print(f"[ERROR] {station} high: {e}")
            
            # LOW markets
            low_event = f"{state.low_ticker_base}-{today}"
            low_brackets = []
            try:
                markets = self.kalshi.get_markets_for_event(low_event)
                for m in markets:
                    if m.get('status') != 'active':
                        continue
                    
                    ticker = m.get('ticker', '')
                    subtitle = m.get('yes_sub_title', m.get('subtitle', ''))
                    floor = m.get('floor_strike')
                    cap = m.get('cap_strike')
                    strike_type = m.get('strike_type', 'between')
                    
                    orderbook = self.kalshi.get_orderbook(ticker)
                    no_asks = orderbook.get('no', {}).get('asks', [])
                    yes_asks = orderbook.get('yes', {}).get('asks', [])
                    no_ask = min([a[0] for a in no_asks]) if no_asks else 100
                    yes_ask = min([a[0] for a in yes_asks]) if yes_asks else 100
                    
                    bracket = BracketInfo(
                        ticker=ticker, event_ticker=low_event, subtitle=subtitle,
                        floor_strike=int(floor) if floor else None,
                        cap_strike=int(cap) if cap else None,
                        strike_type=strike_type, signal_type='low',
                        no_ask=no_ask, yes_ask=yes_ask, station=station
                    )
                    low_brackets.append(bracket)
                
                # Mark edge brackets for LOW (the "X or below" bracket is the edge)
                for b in low_brackets:
                    if b.strike_type == 'less':
                        b.is_edge_bracket = True
                    # Add to watchlist if price is good for either side
                    if b.no_ask <= self.max_no_price or (b.is_edge_bracket and b.yes_ask <= self.max_yes_price):
                        state.watchlist.append(b)
                        edge_str = " 🎯 EDGE" if b.is_edge_bracket else ""
                        print(f"  ✓ {b.subtitle:<18} (low) NO@{b.no_ask}¢ YES@{b.yes_ask}¢{edge_str}")
                        
            except Exception as e:
                print(f"[ERROR] {station} low: {e}")
            
            print(f"[WATCHLIST] {station}: {len(state.watchlist)} brackets")
    
    def _check_override_active(self, station: str, lock_type: str) -> bool:
        overrides = load_overrides()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        station_overrides = overrides.get(today, {}).get(station, {})
        cutoff_str = station_overrides.get(f'{lock_type}_locked_after')
        
        if not cutoff_str:
            return False
        
        try:
            cutoff_hour, cutoff_min = map(int, cutoff_str.split(':'))
            now = datetime.now(timezone.utc)
            cutoff_time = now.replace(hour=cutoff_hour, minute=cutoff_min, second=0, microsecond=0)
            return now >= cutoff_time
        except:
            return False
    
    # ============ NO LOCK LOGIC ============
    
    def _is_no_locked_for_high(self, observed: int, bracket: BracketInfo, soft: bool = False) -> bool:
        """Check if NO is locked for a HIGH bracket."""
        if bracket.strike_type == 'between':
            if bracket.cap_strike is None:
                return False
            if observed > bracket.cap_strike:
                return True  # Hard lock
            if soft and observed == bracket.cap_strike:
                return True  # Soft lock
            return False
        elif bracket.strike_type == 'less':
            # "X or below" - NO locked if observed >= cap
            return bracket.cap_strike is not None and observed >= bracket.cap_strike
        return False  # 'greater' can never lock NO for HIGH
    
    def _is_no_locked_for_low(self, observed: int, bracket: BracketInfo, soft: bool = False) -> bool:
        """Check if NO is locked for a LOW bracket."""
        if bracket.strike_type == 'between':
            if bracket.floor_strike is None:
                return False
            if observed < bracket.floor_strike:
                return True  # Hard lock
            if soft and observed == bracket.floor_strike:
                return True  # Soft lock
            return False
        elif bracket.strike_type == 'greater':
            # "X or above" - NO locked if observed < floor
            if bracket.floor_strike is None:
                return False
            if observed < bracket.floor_strike:
                return True  # Hard lock
            if soft and observed == bracket.floor_strike:
                return True  # Soft lock
            return False
        return False  # 'less' can never lock NO for LOW
    
    # ============ YES LOCK LOGIC (NEW!) ============
    
    def _is_yes_locked_for_high(self, observed: int, bracket: BracketInfo, soft: bool = False) -> bool:
        """
        Check if YES is locked for a HIGH bracket.
        
        YES locks on edge brackets when temp hits/exceeds the threshold:
        - "85° or above" (greater): YES locked if observed >= 85 (it's the highest bracket)
        
        For edge brackets, hitting exactly the threshold IS a hard lock because
        there's no higher bracket - YES wins regardless of further rises.
        """
        if not bracket.is_edge_bracket:
            return False
        
        if bracket.strike_type == 'greater':
            # "X or above" - YES locked if observed >= floor (highest bracket, can't go higher)
            if bracket.floor_strike is None:
                return False
            if observed >= bracket.floor_strike:
                return True  # Hard lock - temp is at or above threshold, nowhere else to go
            return False
        
        return False
    
    def _is_yes_locked_for_low(self, observed: int, bracket: BracketInfo, soft: bool = False) -> bool:
        """
        Check if YES is locked for a LOW bracket.
        
        YES locks on edge brackets when temp hits/drops to the threshold:
        - "6° or below" (less): YES locked if observed <= 6 (it's the lowest bracket)
        
        For edge brackets, hitting exactly the threshold IS a hard lock because
        there's no lower bracket - YES wins regardless of further drops.
        """
        if not bracket.is_edge_bracket:
            return False
        
        if bracket.strike_type == 'less':
            # "X or below" - YES locked if observed <= cap (lowest bracket, can't go lower)
            if bracket.cap_strike is None:
                return False
            if observed <= bracket.cap_strike:
                return True  # Hard lock - temp is at or below threshold, nowhere else to go
            return False
        
        return False
    
    def check_and_trade(self, state: MarketState, metar_text: str) -> List[dict]:
        results = []
        parsed = parse_metar(metar_text)
        if not parsed.station:
            return results
        
        high_override = self._check_override_active(state.station, 'high')
        low_override = self._check_override_active(state.station, 'low')
        
        if high_override:
            print(f"[SIGNAL] {state.station} ⏰ HIGH override active")
        if low_override:
            print(f"[SIGNAL] {state.station} ⏰ LOW override active")
        
        if self.hourly_mode:
            if parsed.t_group_temp_c is not None:
                temp_c = parsed.t_group_temp_c
                current_temp = nws_round(temp_c * 9/5 + 32)
                print(f"[SIGNAL] {state.station} HOURLY: {current_temp}°F (T-group {temp_c}°C)")
                
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
                            if self._is_no_locked_for_high(current_temp, bracket, soft=high_override):
                                results.append(self._execute_trade(bracket, current_temp, state, side='no', soft_lock=high_override))
                                continue
                        # Check YES lock (edge brackets only)
                        if bracket.is_edge_bracket and bracket.yes_ask <= self.max_yes_price:
                            if self._is_yes_locked_for_high(current_temp, bracket, soft=high_override):
                                results.append(self._execute_trade(bracket, current_temp, state, side='yes', soft_lock=high_override))
                    else:  # low
                        # Check NO lock
                        if bracket.no_ask <= self.max_no_price:
                            if self._is_no_locked_for_low(current_temp, bracket, soft=low_override):
                                results.append(self._execute_trade(bracket, current_temp, state, side='no', soft_lock=low_override))
                                continue
                        # Check YES lock (edge brackets only)
                        if bracket.is_edge_bracket and bracket.yes_ask <= self.max_yes_price:
                            if self._is_yes_locked_for_low(current_temp, bracket, soft=low_override):
                                results.append(self._execute_trade(bracket, current_temp, state, side='yes', soft_lock=low_override))
        
        # 6-hourly max/min
        if parsed.six_hour_max_c is not None:
            observed = nws_round(parsed.six_hour_max_c * 9/5 + 32)
            print(f"[SIGNAL] {state.station} 6HR MAX: {observed}°F")
            
            for bracket in state.watchlist:
                if bracket.ticker in state.traded_tickers:
                    continue
                if bracket.signal_type == 'high':
                    if bracket.no_ask <= self.max_no_price:
                        if self._is_no_locked_for_high(observed, bracket, soft=high_override):
                            results.append(self._execute_trade(bracket, observed, state, side='no', soft_lock=high_override))
                            continue
                    if bracket.is_edge_bracket and bracket.yes_ask <= self.max_yes_price:
                        if self._is_yes_locked_for_high(observed, bracket, soft=high_override):
                            results.append(self._execute_trade(bracket, observed, state, side='yes', soft_lock=high_override))
        
        if parsed.six_hour_min_c is not None:
            observed = nws_round(parsed.six_hour_min_c * 9/5 + 32)
            print(f"[SIGNAL] {state.station} 6HR MIN: {observed}°F")
            
            for bracket in state.watchlist:
                if bracket.ticker in state.traded_tickers:
                    continue
                if bracket.signal_type == 'low':
                    if bracket.no_ask <= self.max_no_price:
                        if self._is_no_locked_for_low(observed, bracket, soft=low_override):
                            results.append(self._execute_trade(bracket, observed, state, side='no', soft_lock=low_override))
                            continue
                    if bracket.is_edge_bracket and bracket.yes_ask <= self.max_yes_price:
                        if self._is_yes_locked_for_low(observed, bracket, soft=low_override):
                            results.append(self._execute_trade(bracket, observed, state, side='yes', soft_lock=low_override))
        
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
                result = self.kalshi.place_order(
                    ticker=bracket.ticker,
                    side=side,
                    action='buy',
                    count=1,
                    order_type='market'
                )
                trade_info['success'] = True
                trade_info['order_id'] = result.get('order_id')
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
        
        print(f"\n[{now.strftime('%H:%MZ')}] 🟢 HOT")
        
        for station in self.market_states.keys():
            try:
                metar = self.weather.fetch_metar(station)
                if metar:
                    self.latest_metars[station] = metar
                    print(f"[METAR] {station} ⏳ {metar[:70]}...")
                    
                    parsed = parse_metar(metar)
                    if parsed.t_group_temp_c is not None:
                        temp_f = nws_round(parsed.t_group_temp_c * 9/5 + 32)
                        self.latest_temps[station] = temp_f
                    
                    state = self.market_states[station]
                    self.check_and_trade(state, metar)
            except Exception as e:
                print(f"[ERROR] {station}: {e}")
    
    def run(self):
        print(f"[START] WX Sniper v3.4 (YES+NO)")
        print(f"[CONFIG] Live: {self.live_mode} | Hourly: {self.hourly_mode}")
        print(f"[CONFIG] Max NO: {self.max_no_price}¢ | Max YES: {self.max_yes_price}¢")
        print(f"[CONFIG] Stations: {list(self.market_states.keys())}")
        
        HealthHandler.poller = self
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()
        
        self.build_watchlist()
        last_watchlist_build = datetime.now(timezone.utc)
        
        while True:
            now = datetime.now(timezone.utc)
            
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
