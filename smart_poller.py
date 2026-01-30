#!/usr/bin/env python3
"""
WX Sniper - Smart Poller v3.2

NEW: Manual lock override UI - set cutoff times for when high/low is "settled"

Schedule:
- XX:48 → Build watchlist (check prices)
- XX:52-:02 → Poll METARs every 5s, execute trades
- Rest of time → Sleep

Synoptic METAR times (UTC): 05:53Z, 11:53Z, 17:53Z, 23:53Z
Hot windows: 05:52-06:02Z, 11:52-12:02Z, 17:52-18:02Z, 23:52-00:02Z
"""

import os
import sys
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aviation_weather import AviationWeatherPoller
from metar_parser import parse_metar, nws_round
from kalshi_client import KalshiClient
from config import STATIONS

# File to persist lock overrides
OVERRIDES_FILE = "lock_overrides.json"


def load_overrides() -> dict:
    """Load lock overrides from file."""
    if os.path.exists(OVERRIDES_FILE):
        try:
            with open(OVERRIDES_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_overrides(overrides: dict):
    """Save lock overrides to file."""
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
        
        saved_msg = ""
        if "saved=1" in self.path:
            saved_msg = '<div style="background:#004400;padding:10px;margin:10px 0;border-radius:5px;">✅ Overrides saved!</div>'
        
        html = f'''
        <html><head><meta charset="UTF-8"><title>WX Sniper - Lock Overrides</title>
        <style>
            body {{ font-family: monospace; background: #1a1a2e; color: #eee; padding: 20px; }}
            h1 {{ color: #00ff88; }} h2 {{ color: #00aaff; margin-top: 30px; }} a {{ color: #00aaff; }}
            table {{ border-collapse: collapse; margin: 10px 0; }}
            th, td {{ border: 1px solid #444; padding: 8px 12px; text-align: left; }} th {{ background: #333; }}
            input[type="time"] {{ background: #333; color: #eee; border: 1px solid #555; padding: 5px; font-family: monospace; }}
            input[type="submit"] {{ background: #00aa55; color: white; border: none; padding: 10px 20px; font-size: 16px; cursor: pointer; margin-top: 20px; }}
            .clear-btn {{ background: #aa5500; font-size: 12px; padding: 3px 8px; margin-left: 5px; cursor: pointer; }}
            .help {{ color: #888; font-size: 12px; }}
        </style>
        <script>function clearField(id) {{ document.getElementById(id).value = ''; }}</script>
        </head><body>
            <h1>⏰ Lock Override Config</h1>
            <p><a href="/">← Back to Status</a></p>
            <p class="help">Set LOCAL time after which high/low is "settled". Leave blank = strict math only.</p>
            {saved_msg}
            <h2>Overrides for {today}</h2>
            <form method="POST" action="/config">
            <table><tr><th>Station</th><th>TZ</th><th>High Locked After</th><th>Low Locked After</th></tr>
        '''
        
        for station, state in p.market_states.items():
            tz = state.timezone.split('/')[-1].replace('_', ' ')
            so = today_overrides.get(station, {})
            hv = so.get('high_locked_after') or ''
            lv = so.get('low_locked_after') or ''
            html += f'''<tr><td><strong>{station}</strong></td><td>{tz}</td>
                <td><input type="time" id="{station}_high" name="{station}_high" value="{hv}">
                    <button type="button" class="clear-btn" onclick="clearField('{station}_high')">✕</button></td>
                <td><input type="time" id="{station}_low" name="{station}_low" value="{lv}">
                    <button type="button" class="clear-btn" onclick="clearField('{station}_low')">✕</button></td></tr>'''
        
        html += '''</table><input type="submit" value="💾 Save Overrides"></form>
            <h2>How it works</h2>
            <p class="help"><strong>Without override:</strong> Bot only trades when observed temp > cap (hard lock).<br>
            <strong>With override:</strong> After specified time, observed == cap also triggers trade (soft lock).</p>
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
        s = p._get_synoptic_state()
        
        html = f'''<html><head><meta charset="UTF-8"><title>WX Sniper v3.2</title>
            <meta http-equiv="refresh" content="30">
            <style>
                body {{ font-family: monospace; background: #1a1a2e; color: #eee; padding: 20px; }}
                h1 {{ color: #00ff88; }} h2 {{ color: #00aaff; margin-top: 30px; }} a {{ color: #00aaff; }}
                .status {{ font-size: 24px; margin: 20px 0; }}
                .hot {{ color: #00ff88; }} .prep {{ color: #ffaa00; }} .waiting {{ color: #ff4444; }}
                table {{ border-collapse: collapse; margin: 10px 0; }}
                th, td {{ border: 1px solid #444; padding: 8px 12px; text-align: left; }} th {{ background: #333; }}
                .cheap {{ color: #00ff88; }} .override {{ color: #ffaa00; }}
            </style></head><body>
            <h1>🌡️ WX Sniper v3.2</h1>
            <p><a href="/config">⏰ Configure Lock Overrides</a></p>
            <div class="status">Mode: {'💰 LIVE' if not p.dry_run else '🧪 DRY RUN'} | Hourly: {'✅ ON' if p.hourly_mode else '❌ OFF'} | Max: {p.max_price_cents}¢</div>
            <div class="status">Current: {s['current_hour']:02d}:{s['current_minute']:02d}Z |
                <span class="{'hot' if s['is_hot_window'] else 'prep' if s['is_prep_window'] else 'waiting'}">
                {'🟢 HOT' if s['is_hot_window'] else '🟡 PREP' if s['is_prep_window'] else f"🔴 Next in {s['minutes_to_hot']}min"}</span></div>
            <div class="status">Trades: {len(p.executed_trades)}</div>'''
        
        # Active overrides
        overrides = load_overrides()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_overrides = overrides.get(today, {})
        active = []
        for st, cfg in today_overrides.items():
            if cfg.get('high_locked_after'): active.append(f"{st} H@{cfg['high_locked_after']}")
            if cfg.get('low_locked_after'): active.append(f"{st} L@{cfg['low_locked_after']}")
        if active:
            html += f'<div class="status override">⏰ {", ".join(active)}</div>'
        
        # Watchlists
        for station, state in p.market_states.items():
            if state.watchlist:
                html += f"<h2>{station}</h2><table><tr><th>Bracket</th><th>Type</th><th>NO</th><th>Floor</th><th>Cap</th></tr>"
                for b in state.watchlist:
                    html += f'<tr><td>{b.subtitle}</td><td>{b.signal_type.upper()}</td><td class="{"cheap" if b.no_ask<=70 else ""}">{b.no_ask}¢</td><td>{b.floor_strike or "-"}</td><td>{b.cap_strike or "-"}</td></tr>'
                html += "</table>"
        
        # METARs
        if p.latest_metars:
            html += "<h2>📡 METARs</h2><table><tr><th>Station</th><th>Temp</th><th>Raw</th></tr>"
            for st, metar in p.latest_metars.items():
                html += f'<tr><td>{st}</td><td>{p.latest_temps.get(st,"?")}°F</td><td style="font-size:11px">{metar[:60]}...</td></tr>'
            html += "</table>"
        
        # Trade log
        if p.trade_log:
            html += "<h2>💰 Trades</h2><table><tr><th>Time</th><th>Station</th><th>Bracket</th><th>Price</th><th>Status</th></tr>"
            for t in reversed(p.trade_log[-10:]):
                st = "✅" if t.get('success') else "❌"
                if t.get('dry_run'): st = "🧪"
                if t.get('soft_lock'): st += "⏰"
                html += f'<tr><td>{t.get("time","?")}</td><td>{t.get("station","?")}</td><td>{t.get("subtitle",t.get("ticker","?"))}</td><td>{t.get("price","?")}¢</td><td>{st}</td></tr>'
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
    strike_type: str
    signal_type: str
    no_ask: int
    station: str


@dataclass
class MarketState:
    station: str
    high_ticker_base: str
    low_ticker_base: str
    timezone: str
    watchlist: List[BracketInfo] = field(default_factory=list)
    observed_high: Optional[int] = None
    observed_low: Optional[int] = None


class SmartPoller:
    SYNOPTIC_HOURS = [5, 11, 17, 23]
    EXCLUDED_STATIONS_BY_HOUR = {
        11: ['KLAX', 'KSFO', 'KSEA', 'KLAS', 'KDEN'],
    }
    
    def __init__(self, dry_run: bool = True, max_price_cents: int = 93, hourly_mode: bool = False):
        self.dry_run = dry_run
        self.max_price_cents = max_price_cents
        self.hourly_mode = hourly_mode
        self.kalshi = KalshiClient()
        self.aviation = AviationWeatherPoller()
        self.running = False
        
        self.latest_metars: Dict[str, str] = {}
        self.latest_temps: Dict[str, int] = {}
        self.trade_log: List[dict] = []
        
        self.station_timezones = {
            'KNYC': 'America/New_York', 'KPHL': 'America/New_York',
            'KMDW': 'America/Chicago', 'KLAX': 'America/Los_Angeles',
            'KMIA': 'America/New_York', 'KAUS': 'America/Chicago',
            'KSFO': 'America/Los_Angeles', 'KSEA': 'America/Los_Angeles',
            'KDCA': 'America/New_York', 'KMSY': 'America/Chicago',
            'KLAS': 'America/Los_Angeles', 'KDEN': 'America/Denver',
        }
        
        self.market_states: Dict[str, MarketState] = {}
        for station, config in STATIONS.items():
            self.market_states[station] = MarketState(
                station=station,
                high_ticker_base=config.get('kalshi_high_ticker', ''),
                low_ticker_base=config.get('kalshi_low_ticker', ''),
                timezone=self.station_timezones.get(station, 'America/New_York')
            )
        
        self.executed_trades: set = set()
        print(f"[POLLER] Initialized - dry_run={dry_run}, max_price={max_price_cents}¢")
    
    def _get_local_time(self, tz_name: str) -> datetime:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    
    def _get_market_date(self, tz_name: str) -> str:
        return self._get_local_time(tz_name).strftime("%y%b%d").upper()
    
    def _get_synoptic_state(self) -> dict:
        now = datetime.now(timezone.utc)
        hour, minute = now.hour, now.minute
        
        if self.hourly_mode:
            is_prep = 48 <= minute <= 51
            is_hot = 52 <= minute <= 59 or minute <= 2
            mins_to_hot = (52 - minute) % 60 if minute < 52 else 0
            return {'now_utc': now, 'is_prep_window': is_prep, 'is_hot_window': is_hot, 
                    'minutes_to_hot': mins_to_hot, 'current_hour': hour, 'current_minute': minute}
        
        is_prep = 48 <= minute <= 51 and hour in self.SYNOPTIC_HOURS
        is_hot = (minute >= 52 and hour in self.SYNOPTIC_HOURS) or \
                 (minute <= 2 and (hour - 1) % 24 in self.SYNOPTIC_HOURS)
        
        next_hot = None
        for h in self.SYNOPTIC_HOURS:
            if h > hour or (h == hour and minute < 52):
                next_hot = h
                break
        if next_hot is None:
            next_hot = self.SYNOPTIC_HOURS[0]
        
        if is_hot:
            mins_to_hot = 0
        elif next_hot > hour:
            mins_to_hot = (next_hot - hour) * 60 + (52 - minute)
        else:
            mins_to_hot = (24 - hour + next_hot) * 60 + (52 - minute)
        
        return {'now_utc': now, 'is_prep_window': is_prep, 'is_hot_window': is_hot,
                'minutes_to_hot': mins_to_hot, 'current_hour': hour, 'current_minute': minute}
    
    def build_watchlist(self, state: MarketState) -> List[BracketInfo]:
        print(f"[WATCHLIST] Building for {state.station}...")
        watchlist = []
        date_str = self._get_market_date(state.timezone)
        
        for signal_type, ticker_base in [('high', state.high_ticker_base), ('low', state.low_ticker_base)]:
            if not ticker_base:
                continue
            event_ticker = f"{ticker_base}-{date_str}"
            try:
                time.sleep(0.5)
                markets = self.kalshi.get_markets(event_ticker=event_ticker)
                for m in markets:
                    no_ask_dollars = m.get('no_ask_dollars')
                    if not no_ask_dollars:
                        continue
                    no_ask = int(float(no_ask_dollars) * 100)
                    if no_ask <= self.max_price_cents:
                        bracket = BracketInfo(
                            ticker=m.get('ticker'), event_ticker=event_ticker,
                            subtitle=m.get('yes_sub_title', ''),
                            floor_strike=int(m['floor_strike']) if m.get('floor_strike') else None,
                            cap_strike=int(m['cap_strike']) if m.get('cap_strike') else None,
                            strike_type=m.get('strike_type', ''), signal_type=signal_type,
                            no_ask=no_ask, station=state.station
                        )
                        watchlist.append(bracket)
                        print(f"  ✓ {bracket.subtitle:<15} ({signal_type}) NO @ {no_ask}¢")
            except Exception as e:
                print(f"[WATCHLIST] Error: {e}")
        
        state.watchlist = watchlist
        print(f"[WATCHLIST] {state.station}: {len(watchlist)} brackets")
        return watchlist
    
    def _check_override_active(self, station: str, signal_type: str) -> bool:
        overrides = load_overrides()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        station_overrides = overrides.get(today, {}).get(station, {})
        override_time_str = station_overrides.get(f"{signal_type}_locked_after")
        
        if not override_time_str:
            return False
        
        try:
            override_hour, override_min = map(int, override_time_str.split(':'))
        except:
            return False
        
        local_now = self._get_local_time(self.station_timezones.get(station, 'America/New_York'))
        current_minutes = local_now.hour * 60 + local_now.minute
        override_minutes = override_hour * 60 + override_min
        return current_minutes >= override_minutes
    
    def _is_no_locked_for_high(self, observed: int, bracket: BracketInfo, soft: bool = False) -> bool:
        if bracket.strike_type == 'between':
            if bracket.cap_strike is None:
                return False
            if observed > bracket.cap_strike:
                return True
            if soft and observed == bracket.cap_strike:
                return True
            return False
        elif bracket.strike_type == 'less':
            return bracket.cap_strike is not None and observed >= bracket.cap_strike
        return False  # 'greater' can never lock for HIGH
    
    def _is_no_locked_for_low(self, observed: int, bracket: BracketInfo, soft: bool = False) -> bool:
        if bracket.strike_type == 'between':
            if bracket.floor_strike is None:
                return False
            if observed < bracket.floor_strike:
                return True
            if soft and observed == bracket.floor_strike:
                return True
            return False
        elif bracket.strike_type == 'greater':
            return bracket.floor_strike is not None and observed <= bracket.floor_strike
        return False  # 'less' can never lock for LOW
    
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
                    if bracket.signal_type == 'high':
                        if self._is_no_locked_for_high(current_temp, bracket, soft=high_override):
                            if bracket.ticker not in self.executed_trades:
                                is_soft = (bracket.cap_strike and current_temp == bracket.cap_strike and high_override)
                                results.append(self._execute_trade(bracket, soft_lock=is_soft))
                    elif bracket.signal_type == 'low':
                        if self._is_no_locked_for_low(current_temp, bracket, soft=low_override):
                            if bracket.ticker not in self.executed_trades:
                                is_soft = (bracket.floor_strike and current_temp == bracket.floor_strike and low_override)
                                results.append(self._execute_trade(bracket, soft_lock=is_soft))
            else:
                print(f"[SIGNAL] {state.station} NO T-GROUP - skipping")
            return results
        
        # SYNOPTIC MODE
        if parsed.six_hour_max_f_rounded is not None:
            observed = parsed.six_hour_max_f_rounded
            print(f"[SIGNAL] {state.station} 6hr MAX: {observed}°F")
            for bracket in state.watchlist:
                if bracket.signal_type == 'high':
                    if self._is_no_locked_for_high(observed, bracket, soft=high_override):
                        if bracket.ticker not in self.executed_trades:
                            is_soft = (bracket.cap_strike and observed == bracket.cap_strike and high_override)
                            results.append(self._execute_trade(bracket, soft_lock=is_soft))
        
        if parsed.six_hour_min_f_rounded is not None:
            observed = parsed.six_hour_min_f_rounded
            print(f"[SIGNAL] {state.station} 6hr MIN: {observed}°F")
            for bracket in state.watchlist:
                if bracket.signal_type == 'low':
                    if self._is_no_locked_for_low(observed, bracket, soft=low_override):
                        if bracket.ticker not in self.executed_trades:
                            is_soft = (bracket.floor_strike and observed == bracket.floor_strike and low_override)
                            results.append(self._execute_trade(bracket, soft_lock=is_soft))
        
        return results
    
    def _execute_trade(self, bracket: BracketInfo, soft_lock: bool = False) -> dict:
        lock_type = "⏰ SOFT" if soft_lock else "🎯 HARD"
        print(f"\n[EXECUTE] {lock_type} LOCK NO: {bracket.ticker}")
        print(f"[EXECUTE] {bracket.subtitle} @ {bracket.no_ask}¢")
        
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
        
        if self.dry_run:
            print(f"[EXECUTE] 🧪 DRY RUN - Would buy NO @ {bracket.no_ask}¢")
            self.executed_trades.add(bracket.ticker)
            result = {'success': True, 'dry_run': True, 'ticker': bracket.ticker, 'time': timestamp,
                      'price': bracket.no_ask, 'soft_lock': soft_lock, 'station': bracket.station, 'subtitle': bracket.subtitle}
            self.trade_log.append(result)
            return result
        
        try:
            order = self.kalshi.create_order(ticker=bracket.ticker, side='no', action='buy',
                                             count=1, price_cents=bracket.no_ask, order_type='limit')
            buy_id = order.get('order', {}).get('order_id')
            print(f"[EXECUTE] ✅ BUY: {buy_id}")
            
            hedge_id = None
            for attempt in range(3):
                try:
                    hedge = self.kalshi.create_order(ticker=bracket.ticker, side='no', action='sell',
                                                     count=1, price_cents=99, order_type='limit')
                    hedge_id = hedge.get('order', {}).get('order_id')
                    print(f"[EXECUTE] 🛡️ HEDGE: {hedge_id}")
                    break
                except Exception as e:
                    print(f"[EXECUTE] ⚠️ Hedge attempt {attempt+1} failed: {e}")
                    time.sleep(1)
            
            if not hedge_id:
                print(f"[EXECUTE] 🚨 HEDGE FAILED")
            
            self.executed_trades.add(bracket.ticker)
            result = {'success': True, 'ticker': bracket.ticker, 'buy': buy_id, 'hedge': hedge_id,
                      'time': timestamp, 'price': bracket.no_ask, 'station': bracket.station,
                      'subtitle': bracket.subtitle, 'soft_lock': soft_lock}
            self.trade_log.append(result)
            return result
        except Exception as e:
            print(f"[EXECUTE] ❌ FAILED: {e}")
            result = {'success': False, 'error': str(e), 'ticker': bracket.ticker, 'time': timestamp, 'soft_lock': soft_lock}
            self.trade_log.append(result)
            return result
    
    def run(self):
        HealthHandler.poller = self
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()
        
        self.running = True
        print("\n" + "="*60)
        print("WX SNIPER v3.2 - SMART POLLER")
        print("="*60)
        print(f"Mode: {'🧪 DRY RUN' if self.dry_run else '💰 LIVE'}")
        print(f"Hourly: {'✅ ON' if self.hourly_mode else '❌ OFF'}")
        print(f"Max NO price: {self.max_price_cents}¢")
        print("="*60 + "\n")
        
        print("[STARTUP] Building watchlists...")
        for state in self.market_states.values():
            self.build_watchlist(state)
            time.sleep(0.5)
        total = sum(len(st.watchlist) for st in self.market_states.values())
        print(f"[STARTUP] Done - {total} brackets\n")
        
        while self.running:
            try:
                s = self._get_synoptic_state()
                now_str = f"{s['current_hour']:02d}:{s['current_minute']:02d}Z"
                
                if s['is_prep_window']:
                    print(f"\n[{now_str}] 🟡 PREP")
                    for state in self.market_states.values():
                        self.build_watchlist(state)
                    time.sleep(60)
                    continue
                
                if s['is_hot_window']:
                    has_watchlist = any(st.watchlist for st in self.market_states.values())
                    current_synoptic = s['current_hour'] if s['current_minute'] >= 52 else (s['current_hour'] - 1) % 24
                    excluded = self.EXCLUDED_STATIONS_BY_HOUR.get(current_synoptic, [])
                    
                    if has_watchlist:
                        print(f"\n[{now_str}] 🟢 HOT")
                        for station, state in self.market_states.items():
                            if not state.watchlist or station in excluded:
                                continue
                            
                            resp = self.aviation.fetch_metar(station)
                            if resp and resp.raw_text:
                                self.latest_metars[station] = resp.raw_text
                                parsed = parse_metar(resp.raw_text)
                                if parsed.t_group_temp_c is not None:
                                    self.latest_temps[station] = nws_round(parsed.t_group_temp_c * 9/5 + 32)
                                
                                has_6hr = parsed.six_hour_max_f_rounded or parsed.six_hour_min_f_rounded
                                print(f"[METAR] {station} {'📊' if has_6hr else '⏳'} {resp.raw_text[:50]}...")
                                
                                if self.hourly_mode or has_6hr:
                                    self.check_and_trade(state, resp.raw_text)
                        time.sleep(5)
                    else:
                        time.sleep(30)
                    continue
                
                if self.hourly_mode:
                    sleep_mins = min(48 - s['current_minute'], 5) if s['current_minute'] < 48 else 1
                else:
                    sleep_mins = min(s['minutes_to_hot'], 5)
                
                print(f"[{now_str}] 🔴 Sleep {sleep_mins}min")
                time.sleep(sleep_mins * 60)
                
            except KeyboardInterrupt:
                print("\n[POLLER] Stopped")
                self.running = False
            except Exception as e:
                print(f"[POLLER] Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(60)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--hourly", action="store_true")
    parser.add_argument("--max-price", type=int, default=93)
    args = parser.parse_args()
    
    poller = SmartPoller(dry_run=not args.live, max_price_cents=args.max_price, hourly_mode=args.hourly)
    poller.run()


if __name__ == "__main__":
    main()
