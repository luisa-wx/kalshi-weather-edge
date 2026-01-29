#!/usr/bin/env python3
"""
WX Sniper - Smart Poller v3.1

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
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


# Simple health check server for DigitalOcean with status page
class HealthHandler(BaseHTTPRequestHandler):
    poller = None  # Will be set after poller is created
    
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        
        if HealthHandler.poller is None:
            self.wfile.write(b'<h1>WX Sniper - Starting...</h1>')
            return
        
        p = HealthHandler.poller
        s = p._get_synoptic_state()
        
        # Build HTML
        html = f"""
        <html>
        <head>
            <meta charset="UTF-8">
            <title>WX Sniper v3.1</title>
            <meta http-equiv="refresh" content="30">
            <style>
                body {{ font-family: monospace; background: #1a1a2e; color: #eee; padding: 20px; }}
                h1 {{ color: #00ff88; }}
                h2 {{ color: #00aaff; margin-top: 30px; }}
                .status {{ font-size: 24px; margin: 20px 0; }}
                .hot {{ color: #00ff88; }}
                .prep {{ color: #ffaa00; }}
                .waiting {{ color: #ff4444; }}
                table {{ border-collapse: collapse; margin: 10px 0; }}
                th, td {{ border: 1px solid #444; padding: 8px 12px; text-align: left; }}
                th {{ background: #333; }}
                .locked {{ background: #004400; }}
                .cheap {{ color: #00ff88; }}
            </style>
        </head>
        <body>
            <h1>🌡️ WX Sniper v3.1</h1>
            <div class="status">
                Mode: {'💰 LIVE' if not p.dry_run else '🧪 DRY RUN'} | 
                Hourly: {'✅ ON' if p.hourly_mode else '❌ OFF'} |
                Max Price: {p.max_price_cents}¢
            </div>
            <div class="status">
                Current: {s['current_hour']:02d}:{s['current_minute']:02d}Z |
                Status: <span class="{'hot' if s['is_hot_window'] else 'prep' if s['is_prep_window'] else 'waiting'}">
                    {'🟢 HOT WINDOW' if s['is_hot_window'] else '🟡 PREP WINDOW' if s['is_prep_window'] else f"🔴 Next hot in {s['minutes_to_hot']}min"}
                </span>
            </div>
            <div class="status">
                Trades executed this session: {len(p.executed_trades)}
            </div>
        """
        
        # Show watchlists for each station
        for station, state in p.market_states.items():
            if state.watchlist:
                html += f"<h2>{station}</h2>"
                html += "<table><tr><th>Bracket</th><th>Type</th><th>NO Ask</th><th>Floor</th><th>Cap</th><th>Ticker</th></tr>"
                for b in state.watchlist:
                    price_class = "cheap" if b.no_ask <= 70 else ""
                    html += f"""<tr>
                        <td>{b.subtitle}</td>
                        <td>{b.signal_type.upper()}</td>
                        <td class="{price_class}">{b.no_ask}¢</td>
                        <td>{b.floor_strike or '-'}</td>
                        <td>{b.cap_strike or '-'}</td>
                        <td>{b.ticker}</td>
                    </tr>"""
                html += "</table>"
        
        if not any(st.watchlist for st in p.market_states.values()):
            html += "<h2>No watchlist yet</h2><p>Watchlist builds at :48 each hour</p>"
        
        # Show latest METARs
        if p.latest_metars:
            html += "<h2>📡 Latest METARs</h2>"
            html += "<table><tr><th>Station</th><th>Temp</th><th>Raw METAR</th></tr>"
            for station, metar in p.latest_metars.items():
                temp = p.latest_temps.get(station, '?')
                html += f"<tr><td>{station}</td><td>{temp}°F</td><td style='font-size:11px'>{metar[:80]}...</td></tr>"
            html += "</table>"
        
        # Show trade log (most recent first)
        if p.trade_log:
            html += "<h2>💰 Trade Log</h2>"
            html += "<table><tr><th>Time</th><th>Station</th><th>Bracket</th><th>Price</th><th>Status</th></tr>"
            for t in reversed(p.trade_log[-20:]):  # Last 20 trades
                status = "✅" if t.get('success') else "❌"
                if t.get('dry_run'):
                    status = "🧪 DRY"
                html += f"""<tr>
                    <td>{t.get('time', '?')}</td>
                    <td>{t.get('station', '?')}</td>
                    <td>{t.get('subtitle', t.get('ticker', '?'))}</td>
                    <td>{t.get('price', '?')}¢</td>
                    <td>{status}</td>
                </tr>"""
            html += "</table>"
        
        # Show executed tickers (simple list)
        if p.executed_trades:
            html += f"<h2>Executed Tickers ({len(p.executed_trades)})</h2><p style='font-size:11px'>"
            html += ", ".join(sorted(p.executed_trades))
            html += "</p>"
        
        html += """
            <br><br>
            <small>Auto-refreshes every 30s</small>
        </body>
        </html>
        """
        
        self.wfile.write(html.encode())
    
    def log_message(self, format, *args):
        pass  # Suppress logs


def start_health_server():
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    print(f"[HEALTH] Server on port {port}")
    server.serve_forever()
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aviation_weather import AviationWeatherPoller
from metar_parser import parse_metar
from kalshi_client import KalshiClient
from config import STATIONS


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


class SmartPoller:
    # METARs drop at :53 of these hours (UTC)
    SYNOPTIC_HOURS = [5, 11, 17, 23]  # SORTED for loop logic
    
    # At certain synoptic times, some markets span midnight and we can't trade them
    # because the 6hr min/max might include previous day's temps
    # 
    # 11Z (6:53 AM EST) - 6hr period is ~06Z-12Z:
    #   EST (UTC-5): 1AM-7AM = same day ✅
    #   CST (UTC-6): 12AM-6AM = same day ✅
    #   MST (UTC-7): 11PM-5AM = spans midnight ❌
    #   PST (UTC-8): 10PM-4AM = spans midnight ❌
    #
    # For now, only restrict 11Z. Other windows need analysis for DST.
    EXCLUDED_STATIONS_BY_HOUR = {
        11: ['KLAX', 'KSFO', 'KSEA', 'KLAS', 'KDEN'],  # PST + MST
    }
    
    def __init__(self, dry_run: bool = True, max_price_cents: int = 93, hourly_mode: bool = False):
        self.dry_run = dry_run
        self.max_price_cents = max_price_cents
        self.hourly_mode = hourly_mode  # Use hourly T-group instead of 6-hour data
        self.kalshi = KalshiClient()
        self.aviation = AviationWeatherPoller()
        self.running = False
        
        # Tracking for UI
        self.latest_metars: Dict[str, str] = {}  # station -> raw METAR text
        self.latest_temps: Dict[str, int] = {}   # station -> temp in F
        self.trade_log: List[dict] = []          # list of trade results with timestamps
        
        self.station_timezones = {
            'KNYC': 'America/New_York',
            'KPHL': 'America/New_York',
            'KMDW': 'America/Chicago',
            'KLAX': 'America/Los_Angeles',
            'KMIA': 'America/New_York',
            'KAUS': 'America/Chicago',
            'KSFO': 'America/Los_Angeles',
            'KSEA': 'America/Los_Angeles',
            'KDCA': 'America/New_York',
            'KMSY': 'America/Chicago',
            'KLAS': 'America/Los_Angeles',
            'KDEN': 'America/Denver',
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
        print(f"[POLLER] Stations: {list(self.market_states.keys())}")
    
    def _get_local_time(self, tz_name: str) -> datetime:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    
    def _get_market_date(self, tz_name: str) -> str:
        local = self._get_local_time(tz_name)
        return local.strftime("%y%b%d").upper()
    
    def _get_synoptic_state(self) -> dict:
        now = datetime.now(timezone.utc)
        hour = now.hour
        minute = now.minute
        
        # In hourly mode, every hour is a hot hour
        if self.hourly_mode:
            is_prep_window = 48 <= minute <= 51
            is_hot_window = 52 <= minute <= 59 or minute <= 2
            minutes_to_hot = (52 - minute) % 60 if minute < 52 else 0
            
            return {
                'now_utc': now,
                'is_prep_window': is_prep_window,
                'is_hot_window': is_hot_window,
                'minutes_to_hot': minutes_to_hot,
                'current_hour': hour,
                'current_minute': minute
            }
        
        # Synoptic mode - only specific hours
        # Check if in prep window (XX:48-51 of a synoptic hour)
        is_prep_window = 48 <= minute <= 51 and hour in self.SYNOPTIC_HOURS
        
        # Check if in hot window (XX:52-59 of synoptic hour OR XX:00-02 of next hour)
        is_hot_window = False
        if minute >= 52 and hour in self.SYNOPTIC_HOURS:
            is_hot_window = True
        elif minute <= 2:
            prev_hour = (hour - 1) % 24
            if prev_hour in self.SYNOPTIC_HOURS:
                is_hot_window = True
        
        # Find next hot window
        next_hot_utc = None
        for h in self.SYNOPTIC_HOURS:
            if h > hour or (h == hour and minute < 52):
                next_hot_utc = h
                break
        if next_hot_utc is None:
            next_hot_utc = self.SYNOPTIC_HOURS[0]  # wrap to tomorrow
        
        # Calculate minutes to next hot window
        if is_hot_window:
            minutes_to_hot = 0
        else:
            if next_hot_utc > hour:
                minutes_to_hot = (next_hot_utc - hour) * 60 + (52 - minute)
            else:
                minutes_to_hot = (24 - hour + next_hot_utc) * 60 + (52 - minute)
        
        return {
            'now_utc': now,
            'is_prep_window': is_prep_window,
            'is_hot_window': is_hot_window,
            'minutes_to_hot': minutes_to_hot,
            'current_hour': hour,
            'current_minute': minute
        }
    
    def build_watchlist(self, state: MarketState) -> List[BracketInfo]:
        print(f"[WATCHLIST] Building for {state.station}...")
        
        watchlist = []
        date_str = self._get_market_date(state.timezone)
        
        for signal_type, ticker_base in [('high', state.high_ticker_base), ('low', state.low_ticker_base)]:
            if not ticker_base:
                continue
            
            event_ticker = f"{ticker_base}-{date_str}"
            
            try:
                time.sleep(0.5)  # Rate limit protection - 500ms between calls
                markets = self.kalshi.get_markets(event_ticker=event_ticker)
                
                for m in markets:
                    no_ask_dollars = m.get('no_ask_dollars')
                    if not no_ask_dollars:
                        continue
                    
                    no_ask = int(float(no_ask_dollars) * 100)
                    
                    if no_ask <= self.max_price_cents:
                        floor = m.get('floor_strike')
                        cap = m.get('cap_strike')
                        
                        bracket = BracketInfo(
                            ticker=m.get('ticker'),
                            event_ticker=event_ticker,
                            subtitle=m.get('yes_sub_title', ''),
                            floor_strike=int(floor) if floor else None,
                            cap_strike=int(cap) if cap else None,
                            strike_type=m.get('strike_type', ''),
                            signal_type=signal_type,
                            no_ask=no_ask,
                            station=state.station
                        )
                        watchlist.append(bracket)
                        print(f"  ✓ {bracket.subtitle:<15} ({signal_type}) NO @ {no_ask}¢")
                        
            except Exception as e:
                print(f"[WATCHLIST] Error: {e}")
        
        state.watchlist = watchlist
        print(f"[WATCHLIST] {state.station}: {len(watchlist)} brackets ≤ {self.max_price_cents}¢")
        return watchlist
    
    def _is_no_locked_for_high(self, observed_temp: int, bracket: BracketInfo) -> bool:
        if bracket.strike_type == 'between':
            return bracket.cap_strike is not None and observed_temp > bracket.cap_strike
        elif bracket.strike_type == 'less':
            return bracket.cap_strike is not None and observed_temp >= bracket.cap_strike
        return False  # 'greater' never locked for HIGH
    
    def _is_no_locked_for_low(self, observed_temp: int, bracket: BracketInfo) -> bool:
        if bracket.strike_type == 'between':
            return bracket.floor_strike is not None and observed_temp < bracket.floor_strike
        elif bracket.strike_type == 'greater':
            return bracket.floor_strike is not None and observed_temp <= bracket.floor_strike
        return False  # 'less' never locked for LOW
    
    def check_and_trade(self, state: MarketState, metar_text: str) -> List[dict]:
        results = []
        parsed = parse_metar(metar_text)
        
        if not parsed.station:
            return results
        
        # HOURLY MODE: Use current temp (T-group) for both high and low signals
        if self.hourly_mode:
            # Convert current_temp_c to F and round
            if parsed.current_temp_c is not None:
                current_temp = round(parsed.current_temp_c * 9/5 + 32)
                print(f"[SIGNAL] {state.station} HOURLY TEMP: {current_temp}°F (from {parsed.current_temp_c}°C)")
                
                # For HIGH markets: current temp proves high is AT LEAST this value
                # Lock NOs where cap_strike < current_temp (brackets already exceeded)
                for bracket in state.watchlist:
                    if bracket.signal_type == 'high':
                        if self._is_no_locked_for_high(current_temp, bracket):
                            if bracket.ticker not in self.executed_trades:
                                result = self._execute_trade(bracket)
                                results.append(result)
                
                # For LOW markets: current temp proves low is AT MOST this value
                # Lock NOs where floor_strike > current_temp (brackets already undercut)
                for bracket in state.watchlist:
                    if bracket.signal_type == 'low':
                        if self._is_no_locked_for_low(current_temp, bracket):
                            if bracket.ticker not in self.executed_trades:
                                result = self._execute_trade(bracket)
                                results.append(result)
            return results
        
        # SYNOPTIC MODE: Use 6-hour min/max data
        # HIGH markets - check 6hr max
        if parsed.six_hour_max_f_rounded is not None:
            observed = parsed.six_hour_max_f_rounded
            print(f"[SIGNAL] {state.station} 6hr MAX: {observed}°F")
            
            for bracket in state.watchlist:
                if bracket.signal_type != 'high':
                    continue
                if self._is_no_locked_for_high(observed, bracket):
                    if bracket.ticker not in self.executed_trades:
                        result = self._execute_trade(bracket)
                        results.append(result)
        
        # LOW markets - check 6hr min
        if parsed.six_hour_min_f_rounded is not None:
            observed = parsed.six_hour_min_f_rounded
            print(f"[SIGNAL] {state.station} 6hr MIN: {observed}°F")
            
            for bracket in state.watchlist:
                if bracket.signal_type != 'low':
                    continue
                if self._is_no_locked_for_low(observed, bracket):
                    if bracket.ticker not in self.executed_trades:
                        result = self._execute_trade(bracket)
                        results.append(result)
        
        return results
    
    def _execute_trade(self, bracket: BracketInfo) -> dict:
        print(f"\n[EXECUTE] 🎯 LOCKED NO: {bracket.ticker}")
        print(f"[EXECUTE] {bracket.subtitle} @ {bracket.no_ask}¢")
        
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
        
        if self.dry_run:
            print(f"[EXECUTE] 🧪 DRY RUN - Would buy NO @ {bracket.no_ask}¢, hedge @ 99¢")
            self.executed_trades.add(bracket.ticker)
            result = {'success': True, 'dry_run': True, 'ticker': bracket.ticker, 'time': timestamp, 'price': bracket.no_ask}
            self.trade_log.append(result)
            return result
        
        try:
            # Buy NO
            order = self.kalshi.create_order(
                ticker=bracket.ticker,
                side='no',
                action='buy',
                count=1,
                price_cents=bracket.no_ask,
                order_type='limit'
            )
            buy_id = order.get('order', {}).get('order_id')
            print(f"[EXECUTE] ✅ BUY: {buy_id}")
            
            # Hedge at 99¢ - RETRY UP TO 3x
            hedge_id = None
            for attempt in range(3):
                try:
                    hedge = self.kalshi.create_order(
                        ticker=bracket.ticker,
                        side='no',
                        action='sell',
                        count=1,
                        price_cents=99,
                        order_type='limit'
                    )
                    hedge_id = hedge.get('order', {}).get('order_id')
                    print(f"[EXECUTE] 🛡️ HEDGE: {hedge_id}")
                    break
                except Exception as e:
                    print(f"[EXECUTE] ⚠️ Hedge attempt {attempt+1} failed: {e}")
                    time.sleep(1)
            
            if not hedge_id:
                print(f"[EXECUTE] 🚨 HEDGE FAILED - MANUAL INTERVENTION NEEDED")
            
            self.executed_trades.add(bracket.ticker)
            result = {
                'success': True, 
                'ticker': bracket.ticker, 
                'buy': buy_id, 
                'hedge': hedge_id,
                'time': timestamp,
                'price': bracket.no_ask,
                'station': bracket.station,
                'subtitle': bracket.subtitle
            }
            self.trade_log.append(result)
            return result
            
        except Exception as e:
            print(f"[EXECUTE] ❌ FAILED: {e}")
            result = {'success': False, 'error': str(e), 'ticker': bracket.ticker, 'time': timestamp}
            self.trade_log.append(result)
            return result
    
    def run(self):
        # Start health check server in background thread
        HealthHandler.poller = self  # Give health handler access to poller state
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()
        
        self.running = True
        print("\n" + "="*60)
        print("WX SNIPER v3.1 - SMART POLLER")
        print("="*60)
        print(f"Mode: {'🧪 DRY RUN' if self.dry_run else '💰 LIVE'}")
        print(f"Hourly mode: {'✅ ON (polling every hour)' if self.hourly_mode else '❌ OFF (synoptic only)'}")
        print(f"Max NO price: {self.max_price_cents}¢")
        print(f"Synoptic hours (UTC): {self.SYNOPTIC_HOURS}")
        print("="*60 + "\n")
        
        # BUILD WATCHLISTS ON STARTUP so we don't miss first window
        print("[STARTUP] Building watchlists immediately...")
        for state in self.market_states.values():
            self.build_watchlist(state)
            time.sleep(0.5)  # Extra delay between stations on startup
        total = sum(len(st.watchlist) for st in self.market_states.values())
        print(f"[STARTUP] Done - {total} total brackets watching\n")
        
        while self.running:
            try:
                s = self._get_synoptic_state()
                now_str = f"{s['current_hour']:02d}:{s['current_minute']:02d}Z"
                
                if s['is_prep_window']:
                    print(f"\n[{now_str}] 🟡 PREP - Building watchlists")
                    for state in self.market_states.values():
                        self.build_watchlist(state)
                    time.sleep(60)
                    continue
                
                if s['is_hot_window']:
                    has_watchlist = any(st.watchlist for st in self.market_states.values())
                    
                    # Determine which synoptic hour we're in
                    current_synoptic = s['current_hour'] if s['current_minute'] >= 52 else (s['current_hour'] - 1) % 24
                    excluded = self.EXCLUDED_STATIONS_BY_HOUR.get(current_synoptic, [])
                    
                    if has_watchlist:
                        print(f"\n[{now_str}] 🟢 HOT - Polling METARs")
                        if excluded:
                            print(f"[{now_str}] ⚠️ Excluding {excluded} (midnight span)")
                        
                        for station, state in self.market_states.items():
                            if not state.watchlist:
                                continue
                            
                            # Skip stations that span midnight at this synoptic time
                            if station in excluded:
                                print(f"[METAR] {station} ⏭️ SKIPPED (midnight span)")
                                continue
                            
                            resp = self.aviation.fetch_metar(station)
                            if resp and resp.raw_text:
                                # Store for UI
                                self.latest_metars[station] = resp.raw_text
                                parsed = parse_metar(resp.raw_text)
                                if parsed.current_temp_c is not None:
                                    self.latest_temps[station] = round(parsed.current_temp_c * 9/5 + 32)
                                
                                has_6hr = parsed.six_hour_max_f_rounded or parsed.six_hour_min_f_rounded
                                icon = "📊" if has_6hr else "⏳"
                                print(f"[METAR] {station} {icon} {resp.raw_text[:50]}...")
                                
                                # In hourly mode, always check. In synoptic mode, only if has 6hr data
                                if self.hourly_mode or has_6hr:
                                    self.check_and_trade(state, resp.raw_text)
                        
                        time.sleep(5)
                    else:
                        print(f"[{now_str}] Hot window but no watchlist")
                        time.sleep(30)
                    continue
                
                # Outside windows - sleep, but not past prep window
                if self.hourly_mode:
                    # In hourly mode, prep is at :48 every hour
                    minute = s['current_minute']
                    if minute < 48:
                        sleep_mins = min(48 - minute, 5)
                    else:
                        # We're past :48 but not in prep/hot? Shouldn't happen, but sleep short
                        sleep_mins = 1
                else:
                    sleep_mins = min(s['minutes_to_hot'], 5)
                
                print(f"[{now_str}] 🔴 Next hot in {s['minutes_to_hot']}min, sleeping {sleep_mins}min")
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
    parser.add_argument("--live", action="store_true", help="Enable live trading")
    parser.add_argument("--hourly", action="store_true", help="Use hourly T-group temps (not just synoptic 6hr)")
    parser.add_argument("--max-price", type=int, default=93, help="Max NO price in cents")
    args = parser.parse_args()
    
    poller = SmartPoller(dry_run=not args.live, max_price_cents=args.max_price, hourly_mode=args.hourly)
    poller.run()


if __name__ == "__main__":
    main()
