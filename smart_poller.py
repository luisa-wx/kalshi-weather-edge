#!/usr/bin/env python3
"""
WX Sniper - Smart Poller (V3 - Efficient Scheduling)

Schedule:
1. 12:30 AM local time → Ingest brackets for each market (once per day)
2. XX:48 (8 min before synoptic) → Check prices, build watchlist
3. XX:52-:02 (hot window) → Poll METARs every 5s, execute trades
4. Rest of time → Sleep, no API calls

Synoptic times (UTC): 00Z, 06Z, 12Z, 18Z
- METARs with 6-hour temps drop around :53 past the hour
"""

import os
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, field

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aviation_weather import AviationWeatherPoller
from metar_parser import parse_metar
from kalshi_client import KalshiClient
from config import STATIONS


@dataclass
class BracketInfo:
    """Info about a tradeable bracket"""
    ticker: str
    event_ticker: str
    subtitle: str  # "8° to 9°"
    floor_strike: Optional[int]
    cap_strike: Optional[int]
    strike_type: str  # 'between', 'greater', 'less'
    signal_type: str  # 'high' or 'low'
    no_ask: int  # Current NO price in cents
    station: str  # ICAO code


@dataclass
class MarketState:
    """State for a single station's markets"""
    station: str
    high_ticker_base: str
    low_ticker_base: str
    timezone: str
    brackets_ingested: bool = False
    last_bracket_ingest: Optional[datetime] = None
    watchlist: List[BracketInfo] = field(default_factory=list)


class SmartPoller:
    """
    Efficient polling scheduler for weather trading.
    
    Minimizes API calls by only polling when needed:
    - Bracket ingest: once per day at 12:30 AM local
    - Price check: 8 min before each synoptic time
    - METAR polling: only during hot windows with watchlist items
    """
    
    def __init__(self, dry_run: bool = True, max_price_cents: int = 93):
        self.dry_run = dry_run
        self.max_price_cents = max_price_cents
        self.kalshi = KalshiClient()
        self.aviation = AviationWeatherPoller()
        self.running = False
        
        # Station timezones
        self.station_timezones = {
            'KJFK': 'America/New_York',
            'KNYC': 'America/New_York',
            'KSFO': 'America/Los_Angeles',
            'KDEN': 'America/Denver',
            'KORD': 'America/Chicago',
            'KMDW': 'America/Chicago',
        }
        
        # Initialize market states
        self.market_states: Dict[str, MarketState] = {}
        for station, config in STATIONS.items():
            self.market_states[station] = MarketState(
                station=station,
                high_ticker_base=config.get('kalshi_high_ticker', ''),
                low_ticker_base=config.get('kalshi_low_ticker', ''),
                timezone=self.station_timezones.get(station, 'America/New_York')
            )
        
        # Track executed trades to avoid duplicates
        self.executed_trades: set = set()
        
        print(f"[POLLER] Initialized - dry_run={dry_run}, max_price={max_price_cents}¢")
        print(f"[POLLER] Stations: {list(self.market_states.keys())}")
    
    def _get_local_time(self, tz_name: str) -> datetime:
        """Get current time in a specific timezone"""
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    
    def _get_market_date(self, tz_name: str) -> str:
        """Get today's market date string in Kalshi format (26JAN29)"""
        local = self._get_local_time(tz_name)
        return local.strftime("%y%b%d").upper()
    
    def _is_bracket_ingest_time(self, tz_name: str) -> bool:
        """Check if it's 12:30 AM local time (±2 min window)"""
        local = self._get_local_time(tz_name)
        target_minutes = 12 * 60 + 30  # 12:30 AM = 30 minutes after midnight
        current_minutes = local.hour * 60 + local.minute
        return abs(current_minutes - target_minutes) <= 2
    
    def _get_synoptic_state(self) -> dict:
        """
        Determine current synoptic state.
        
        Returns dict with:
        - is_prep_window: True if XX:48-XX:51 (time to check prices)
        - is_hot_window: True if XX:52-XX:02 (time to poll METARs)
        - synoptic_hour: Which synoptic hour we're near (0, 6, 12, 18)
        - minutes_to_hot: Minutes until hot window starts
        """
        now = datetime.now(timezone.utc)
        minute = now.minute
        hour = now.hour
        
        synoptic_hours = [0, 6, 12, 18]
        
        # Prep window: XX:48-XX:51 of a synoptic hour
        is_prep_window = 48 <= minute <= 51 and hour in synoptic_hours
        
        # Hot window: XX:52-XX:59 of synoptic hour OR XX:00-XX:02 of next hour
        is_hot_window = False
        synoptic_hour = None
        
        if minute >= 52 and hour in synoptic_hours:
            is_hot_window = True
            synoptic_hour = hour
        elif minute <= 2:
            prev_hour = (hour - 1) % 24
            if prev_hour in synoptic_hours:
                is_hot_window = True
                synoptic_hour = prev_hour
        
        # Calculate minutes to next hot window
        if is_hot_window:
            minutes_to_hot = 0
        elif is_prep_window:
            minutes_to_hot = 52 - minute
        else:
            # Find next synoptic :52
            for h in synoptic_hours:
                if h > hour or (h == hour and minute < 48):
                    next_synoptic = h
                    break
            else:
                next_synoptic = synoptic_hours[0]  # Tomorrow's 00Z
            
            if next_synoptic > hour:
                minutes_to_hot = (next_synoptic - hour - 1) * 60 + (52 - minute)
            else:
                minutes_to_hot = (24 - hour + next_synoptic - 1) * 60 + (52 - minute)
            
            if minutes_to_hot < 0:
                minutes_to_hot += 24 * 60
        
        return {
            'now_utc': now,
            'is_prep_window': is_prep_window,
            'is_hot_window': is_hot_window,
            'synoptic_hour': synoptic_hour,
            'minutes_to_hot': minutes_to_hot,
            'current_hour': hour,
            'current_minute': minute
        }
    
    def ingest_brackets(self, state: MarketState) -> int:
        """
        Ingest all brackets for a station's markets.
        Called once per day at 12:30 AM local time.
        
        Returns number of brackets ingested.
        """
        print(f"\n[INGEST] Ingesting brackets for {state.station}...")
        
        date_str = self._get_market_date(state.timezone)
        count = 0
        
        # Ingest HIGH brackets
        if state.high_ticker_base:
            event_ticker = f"{state.high_ticker_base}-{date_str}"
            try:
                markets = self.kalshi.get_markets(event_ticker=event_ticker)
                print(f"[INGEST] {event_ticker}: {len(markets)} brackets")
                count += len(markets)
            except Exception as e:
                print(f"[INGEST] Error fetching {event_ticker}: {e}")
        
        # Ingest LOW brackets
        if state.low_ticker_base:
            event_ticker = f"{state.low_ticker_base}-{date_str}"
            try:
                markets = self.kalshi.get_markets(event_ticker=event_ticker)
                print(f"[INGEST] {event_ticker}: {len(markets)} brackets")
                count += len(markets)
            except Exception as e:
                print(f"[INGEST] Error fetching {event_ticker}: {e}")
        
        state.brackets_ingested = True
        state.last_bracket_ingest = datetime.now(timezone.utc)
        
        return count
    
    def build_watchlist(self, state: MarketState) -> List[BracketInfo]:
        """
        Build watchlist of brackets with NOs under our price threshold.
        Called at XX:48 before synoptic times.
        """
        print(f"\n[WATCHLIST] Building watchlist for {state.station}...")
        
        watchlist = []
        date_str = self._get_market_date(state.timezone)
        
        for signal_type, ticker_base in [('high', state.high_ticker_base), ('low', state.low_ticker_base)]:
            if not ticker_base:
                continue
                
            event_ticker = f"{ticker_base}-{date_str}"
            
            try:
                markets = self.kalshi.get_markets(event_ticker=event_ticker)
                
                for m in markets:
                    no_ask_dollars = m.get('no_ask_dollars')
                    if not no_ask_dollars:
                        continue
                    
                    no_ask = int(float(no_ask_dollars) * 100)
                    
                    # Only add to watchlist if NO is under our threshold
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
        print(f"[WATCHLIST] {state.station}: {len(watchlist)} brackets under {self.max_price_cents}¢")
        
        return watchlist
    
    def check_and_trade(self, state: MarketState, metar_text: str) -> List[dict]:
        """
        Parse METAR, check for locked NOs, execute trades.
        
        Returns list of trade results.
        """
        results = []
        parsed = parse_metar(metar_text)
        
        if not parsed.station:
            return results
        
        # Check 6-hour max for HIGH markets
        if parsed.six_hour_max_f_rounded is not None:
            observed = parsed.six_hour_max_f_rounded
            print(f"[TRADE] {state.station} 6hr MAX: {observed}°F")
            
            for bracket in state.watchlist:
                if bracket.signal_type != 'high':
                    continue
                
                # Check if NO is locked
                is_locked = self._is_no_locked_for_high(observed, bracket)
                
                if is_locked and bracket.ticker not in self.executed_trades:
                    result = self._execute_trade(bracket)
                    results.append(result)
        
        # Check 6-hour min for LOW markets
        if parsed.six_hour_min_f_rounded is not None:
            observed = parsed.six_hour_min_f_rounded
            print(f"[TRADE] {state.station} 6hr MIN: {observed}°F")
            
            for bracket in state.watchlist:
                if bracket.signal_type != 'low':
                    continue
                
                # Check if NO is locked
                is_locked = self._is_no_locked_for_low(observed, bracket)
                
                if is_locked and bracket.ticker not in self.executed_trades:
                    result = self._execute_trade(bracket)
                    results.append(result)
        
        return results
    
    def _is_no_locked_for_high(self, observed_temp: int, bracket: BracketInfo) -> bool:
        """Check if NO is locked for a HIGH market bracket"""
        if bracket.strike_type == 'between':
            # NO locked if observed > cap (already above range)
            return bracket.cap_strike is not None and observed_temp > bracket.cap_strike
        elif bracket.strike_type == 'less':
            # NO locked if observed >= cap
            return bracket.cap_strike is not None and observed_temp >= bracket.cap_strike
        elif bracket.strike_type == 'greater':
            # Never locked for HIGH (temp could keep rising)
            return False
        return False
    
    def _is_no_locked_for_low(self, observed_temp: int, bracket: BracketInfo) -> bool:
        """Check if NO is locked for a LOW market bracket"""
        if bracket.strike_type == 'between':
            # NO locked if observed < floor (already below range)
            return bracket.floor_strike is not None and observed_temp < bracket.floor_strike
        elif bracket.strike_type == 'greater':
            # NO locked if observed <= floor
            return bracket.floor_strike is not None and observed_temp <= bracket.floor_strike
        elif bracket.strike_type == 'less':
            # Never locked for LOW (temp could keep dropping)
            return False
        return False
    
    def _execute_trade(self, bracket: BracketInfo) -> dict:
        """Execute a NO trade on a locked bracket"""
        print(f"\n[EXECUTE] 🎯 LOCKED NO: {bracket.ticker}")
        print(f"[EXECUTE] {bracket.subtitle} @ {bracket.no_ask}¢")
        
        if self.dry_run:
            print(f"[EXECUTE] 🧪 DRY RUN - Would buy NO")
            self.executed_trades.add(bracket.ticker)
            return {'success': True, 'dry_run': True, 'ticker': bracket.ticker}
        
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
            print(f"[EXECUTE] ✅ Buy order: {buy_id}")
            
            # Hedge at 99¢
            hedge = self.kalshi.create_order(
                ticker=bracket.ticker,
                side='no',
                action='sell',
                count=1,
                price_cents=99,
                order_type='limit'
            )
            hedge_id = hedge.get('order', {}).get('order_id')
            print(f"[EXECUTE] 🛡️ Hedge order: {hedge_id}")
            
            self.executed_trades.add(bracket.ticker)
            
            return {
                'success': True,
                'ticker': bracket.ticker,
                'buy_order': buy_id,
                'hedge_order': hedge_id,
                'price': bracket.no_ask
            }
            
        except Exception as e:
            print(f"[EXECUTE] ❌ Failed: {e}")
            return {'success': False, 'error': str(e), 'ticker': bracket.ticker}
    
    def run(self):
        """Main polling loop"""
        self.running = True
        print("\n" + "="*70)
        print("WX SNIPER - SMART POLLER STARTED")
        print("="*70)
        print(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE TRADING'}")
        print(f"Max NO price: {self.max_price_cents}¢")
        print("="*70 + "\n")
        
        while self.running:
            try:
                synoptic = self._get_synoptic_state()
                
                # Status display
                status = "🔴 WAITING"
                if synoptic['is_hot_window']:
                    status = "🟢 HOT WINDOW - POLLING"
                elif synoptic['is_prep_window']:
                    status = "🟡 PREP - CHECKING PRICES"
                
                # Check for bracket ingest time (12:30 AM local for each station)
                for station, state in self.market_states.items():
                    if self._is_bracket_ingest_time(state.timezone):
                        if not state.brackets_ingested or (datetime.now(timezone.utc) - state.last_bracket_ingest).total_seconds() > 3600:
                            self.ingest_brackets(state)
                
                # Prep window: Build watchlists
                if synoptic['is_prep_window']:
                    print(f"\n[STATUS] {status} ({synoptic['current_hour']:02d}:{synoptic['current_minute']:02d}Z)")
                    for station, state in self.market_states.items():
                        if not state.watchlist or synoptic['current_minute'] == 48:
                            self.build_watchlist(state)
                    time.sleep(60)  # Check every minute during prep
                    continue
                
                # Hot window: Poll METARs
                if synoptic['is_hot_window']:
                    # Check if any station has watchlist items
                    has_watchlist = any(state.watchlist for state in self.market_states.values())
                    
                    if has_watchlist:
                        print(f"\n[STATUS] {status} ({synoptic['current_hour']:02d}:{synoptic['current_minute']:02d}Z)")
                        
                        for station, state in self.market_states.items():
                            if not state.watchlist:
                                continue
                            
                            # Fetch METAR
                            metar_response = self.aviation.fetch_metar(station)
                            if metar_response and metar_response.raw_text:
                                metar = metar_response.raw_text
                                print(f"[METAR] {station}: {metar[:60]}...")
                                self.check_and_trade(state, metar)
                        
                        time.sleep(5)  # Poll every 5s during hot window
                    else:
                        print(f"[STATUS] Hot window but no watchlist items, sleeping...")
                        time.sleep(30)
                    continue
                
                # Outside windows: Sleep until next event
                sleep_time = min(synoptic['minutes_to_hot'] * 60, 300)  # Max 5 min sleep
                print(f"[STATUS] {status} - Next hot window in {synoptic['minutes_to_hot']} min, sleeping {sleep_time}s")
                time.sleep(sleep_time)
                
            except KeyboardInterrupt:
                print("\n[POLLER] Shutting down...")
                self.running = False
            except Exception as e:
                print(f"[POLLER] Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(60)
    
    def stop(self):
        """Stop the poller"""
        self.running = False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="WX Sniper Smart Poller")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default is dry run)")
    parser.add_argument("--max-price", type=int, default=93, help="Max NO price in cents (default 93)")
    args = parser.parse_args()
    
    poller = SmartPoller(dry_run=not args.live, max_price_cents=args.max_price)
    poller.run()


if __name__ == "__main__":
    main()
