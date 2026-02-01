#!/usr/bin/env python3
"""
WX Sniper v5.0 - ASOS Scout Module (wethr_integration.py)
=========================================================

This is a "Scout" module that uses wethr.net's high-frequency 5-minute ASOS data
to detect "Logical Locks" BEFORE the hourly METAR drops.

Key Concepts:
- Uses `lowest_probable` and `highest_probable` from wethr.net API
- These fields account for rounding ambiguity in 5-minute data
- When lowest_probable > bracket.cap_strike, the bracket is MATHEMATICALLY DEAD
- This allows entering positions at ~85¢ before the market reacts to the hourly METAR

Safety Features:
- CONFLICT_SHIELD: Goes dormant during smart_poller's hot window (50-59, 0-5 mins)
- Physics Gate: Rejects readings that jump >2°F from last observation
- Staleness Check: Ignores observations >15 minutes old
- Ejection Seat: If hourly METAR contradicts Scout position, immediately exits

Integration:
- Runs alongside smart_poller.py (import and call from main loop)
- Uses same BracketState and StationState objects
- Shares Kalshi client for order execution
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Optional, List, Any
from dataclasses import dataclass

# ============================================================
# CONFIGURATION
# ============================================================

# Trade quantity for Scout trades (keep low for testing)
TRADE_QUANTITY = int(os.environ.get('SCOUT_TRADE_QUANTITY', '1'))

# Scout bid price (we bid 85¢, not 99¢ - save 99¢ for METAR-confirmed kills)
BID_PRICE = int(os.environ.get('SCOUT_BID_PRICE', '99'))

# Wethr.net API configuration
WETHR_API_KEY = os.environ.get('WETHR_API_KEY', 'da0c8fe4607429123437c3d55cbfd5117652ac24506a9f8af45696a82e0fd652')
WETHR_BASE_URL = "https://wethr.net/api/v2/observations.php"

# Safety thresholds
MAX_TEMP_JUMP = 2.0  # Reject readings that jump more than 2°F
MAX_STALENESS_MINUTES = 15  # Reject observations older than 15 minutes

# Polling interval - 30s to catch 5-minute ASOS drops quickly
# Professional tier allows 60 req/min, so 12 stations x 2/min = 24 req/min is safe
SCOUT_POLL_INTERVAL_SECONDS = 30

logger = logging.getLogger('wx-sniper.scout')

# ============================================================
# SCOUT STATE TRACKING
# ============================================================

@dataclass
class ScoutPosition:
    """Tracks a position entered by the Scout."""
    ticker: str
    station: str
    side: str  # 'yes' or 'no'
    action: str  # 'BUY_YES' or 'BUY_NO'
    entry_price: int
    entry_time: datetime
    quantity: int
    reason: str
    wethr_temp: int  # Temperature that triggered the trade
    ejected: bool = False
    ejection_reason: Optional[str] = None


class ASOSScout:
    """
    The ASOS Scout monitors wethr.net's 5-minute data for early bracket locks.
    
    It detects when `lowest_probable` or `highest_probable` mathematically
    guarantees a bracket outcome, allowing trades before the hourly METAR.
    """
    
    def __init__(self, sniper: Any):
        """
        Initialize Scout with reference to main WXSniper instance.
        
        Args:
            sniper: The WXSniper instance (shares states, kalshi client)
        """
        self.sniper = sniper
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {WETHR_API_KEY}',
            'User-Agent': 'WXSniper-Scout/5.0'
        })
        
        # RAM-only temperature cache for Physics Gate
        self.last_temp: Dict[str, float] = {}
        
        # Track Scout positions for Ejection Seat
        self.positions: List[ScoutPosition] = []
        
        # Track last poll time
        self.last_poll: Optional[datetime] = None
        
        # Dormant mode flag
        self.dormant = False
        
        logger.info("[SCOUT] ASOS Scout initialized")
        logger.info(f"[SCOUT] Trade quantity: {TRADE_QUANTITY}")
    
    def is_conflict_window(self) -> bool:
        """
        Check if we're in smart_poller's hot window.
        
        Returns True during minutes 50-59 and 0-5 (METAR drop window).
        Scout goes DORMANT during this time to avoid conflicts.
        """
        minute = datetime.now(timezone.utc).minute
        return 50 <= minute <= 59 or minute <= 5
    
    def fetch_wethr_data(self, station: str) -> Optional[Dict]:
        """
        Fetch latest observation from wethr.net API.
        
        Args:
            station: ICAO code (e.g., 'KNYC')
            
        Returns:
            Dict with observation data or None on error
        """
        try:
            params = {
                'station_code': station,
                'mode': 'latest'
            }
            
            resp = self.session.get(WETHR_BASE_URL, params=params, timeout=10)
            resp.raise_for_status()
            
            data = resp.json()
            
            # Handle error responses
            if 'error' in data:
                logger.warning(f"[SCOUT] {station} API error: {data['error']}")
                return None
            
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[SCOUT] {station} fetch error: {e}")
            return None
        except Exception as e:
            logger.error(f"[SCOUT] {station} unexpected error: {e}")
            return None
    
    def fetch_wethr_high(self, station: str) -> Optional[Dict]:
        """
        Fetch day's running high/low from wethr.net API using wethr_high mode.
        
        This gives us the day's high/low with NWS rounding logic applied,
        allowing us to know bracket status BEFORE the hourly METAR.
        
        Args:
            station: ICAO code (e.g., 'KNYC')
            
        Returns:
            Dict with wethr_high, wethr_low, etc. or None on error
        """
        try:
            params = {
                'station_code': station,
                'mode': 'wethr_high',
                'logic': 'nws'
            }
            
            resp = self.session.get(WETHR_BASE_URL, params=params, timeout=10)
            resp.raise_for_status()
            
            data = resp.json()
            
            # Handle error responses
            if 'error' in data:
                logger.warning(f"[SCOUT] {station} wethr_high error: {data['error']}")
                return None
            
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[SCOUT] {station} wethr_high fetch error: {e}")
            return None
        except Exception as e:
            logger.error(f"[SCOUT] {station} wethr_high unexpected error: {e}")
            return None
    
    def seed_observed_temps(self):
        """
        Seed observed_high/low from wethr.net's wethr_high mode.
        
        Called on startup/rollover to populate initial high/low estimates
        so we don't get blindsided by the first METAR of the day.
        
        This ensures brackets that are already obviously dead (based on 5-min data)
        don't trigger trades when the hourly METAR merely confirms what we already knew.
        """
        logger.info("[SCOUT] Seeding observed temps from wethr.net...")
        
        for station, state in self.sniper.states.items():
            data = self.fetch_wethr_high(station)
            if not data:
                continue
            
            wethr_high = data.get('wethr_high')
            wethr_low = data.get('wethr_low')
            
            if wethr_high is not None:
                wethr_high = int(wethr_high)
                if state.observed_high is None or wethr_high > state.observed_high:
                    old = state.observed_high
                    state.observed_high = wethr_high
                    logger.info(f"[SCOUT] {station} observed_high: {old} → {wethr_high} (from wethr.net)")
            
            if wethr_low is not None:
                wethr_low = int(wethr_low)
                if state.observed_low is None or wethr_low < state.observed_low:
                    old = state.observed_low
                    state.observed_low = wethr_low
                    logger.info(f"[SCOUT] {station} observed_low: {old} → {wethr_low} (from wethr.net)")
    
    def parse_observation_time(self, time_str: str) -> Optional[datetime]:
        """Parse wethr.net observation_time string to datetime."""
        try:
            # Format: "2025-06-15 18:53:00"
            dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    
    def is_stale(self, obs_time: datetime) -> bool:
        """Check if observation is too old to trade on."""
        age = datetime.now(timezone.utc) - obs_time
        return age > timedelta(minutes=MAX_STALENESS_MINUTES)
    
    def physics_gate(self, station: str, current_temp: float) -> bool:
        """
        RAM-only sanity check to reject glitchy readings.
        
        Returns True if reading passes (is valid), False if it should be rejected.
        """
        if station in self.last_temp:
            last = self.last_temp[station]
            jump = abs(current_temp - last)
            if jump > MAX_TEMP_JUMP:
                logger.warning(f"[SCOUT] {station} Physics Gate REJECT: "
                              f"{last}°F -> {current_temp}°F (jump={jump:.1f}°F)")
                return False
        
        # Update cache
        self.last_temp[station] = current_temp
        return True
    
    def check_high_lock(self, station: str, lowest_probable: int, 
                        brackets: List[Any]) -> List[Dict]:
        """
        Check for HIGH bracket locks using lowest_probable.
        
        Kalshi convention:
          between: cap is actual cap. DEAD when lowest_probable > cap.
          less: cap = X+1, YES wins when final < cap. DEAD when lowest_probable >= cap.
        
        Returns list of trade signals.
        """
        signals = []
        
        for bracket in brackets:
            # Skip already-traded or non-open brackets
            if bracket.traded or bracket.status != 'open':
                continue
            
            # Only check brackets with a cap (between or less-than types)
            if bracket.cap_strike is None:
                continue
            
            is_dead = False
            if bracket.strike_type in ('less', 'less_or_equal'):
                # YES wins when final < cap. DEAD when lowest_probable >= cap.
                is_dead = lowest_probable >= bracket.cap_strike
            else:
                # between: DEAD when lowest_probable > cap
                is_dead = lowest_probable > bracket.cap_strike
            
            if is_dead:
                signals.append({
                    'bracket': bracket,
                    'action': 'BUY_NO',
                    'reason': f"SCOUT: lowest_probable {lowest_probable}°F vs cap {bracket.cap_strike}°F ({bracket.strike_type})",
                    'wethr_temp': lowest_probable
                })
                logger.info(f"[SCOUT] {station} HIGH-LOCK: {bracket.subtitle} "
                           f"(lowest_probable={lowest_probable} vs cap={bracket.cap_strike}, {bracket.strike_type})")
        
        return signals
    
    def check_low_lock(self, station: str, highest_probable: int,
                       brackets: List[Any]) -> List[Dict]:
        """
        Check for LOW bracket locks using highest_probable.
        
        Kalshi convention:
          between: floor is actual floor. DEAD when highest_probable < floor.
          greater: floor = X-1, YES wins when final > floor. DEAD when highest_probable <= floor.
        
        Returns list of trade signals.
        """
        signals = []
        
        for bracket in brackets:
            # Skip already-traded or non-open brackets
            if bracket.traded or bracket.status != 'open':
                continue
            
            # Only check brackets with a floor (between or greater-than types)
            if bracket.floor_strike is None:
                continue
            
            is_dead = False
            if bracket.strike_type in ('greater', 'greater_or_equal'):
                # YES wins when final > floor. DEAD when highest_probable <= floor.
                is_dead = highest_probable <= bracket.floor_strike
            else:
                # between: DEAD when highest_probable < floor
                is_dead = highest_probable < bracket.floor_strike
            
            if is_dead:
                signals.append({
                    'bracket': bracket,
                    'action': 'BUY_NO',
                    'reason': f"SCOUT: highest_probable {highest_probable}°F vs floor {bracket.floor_strike}°F ({bracket.strike_type})",
                    'wethr_temp': highest_probable
                })
                logger.info(f"[SCOUT] {station} LOW-LOCK: {bracket.subtitle} "
                           f"(highest_probable={highest_probable} vs floor={bracket.floor_strike}, {bracket.strike_type})")
        
        return signals
    
    def check_dsm_kill(self, station: str, dsm_high: Optional[int],
                       brackets: List[Any]) -> None:
        """
        Check for DSM invalidation (permanent bracket death).
        
        If dsm_high_display > bracket.cap_strike, the bracket is permanently dead.
        DSM is the Daily Summary Message - an official NWS product.
        """
        if dsm_high is None:
            return
        
        for bracket in brackets:
            if bracket.cap_strike is None:
                continue
            
            if dsm_high > bracket.cap_strike and bracket.status == 'open':
                logger.info(f"[SCOUT] {station} DSM-KILL: {bracket.subtitle} "
                           f"(dsm_high={dsm_high} > cap={bracket.cap_strike})")
                # Mark as dead but don't trade - let smart_poller handle it
                # This is just informational logging
    
    def execute_scout_trade(self, signal: Dict, station: str) -> bool:
        """
        Execute a Scout trade — BUY ONLY, hold to settlement.
        
        NO HEDGE. Settlement pays 100¢. Selling at 99¢ gives your profit away.
        Only the Ejection Seat should sell (market order if the position is wrong).
        
        Profit = (100 - entry_price)¢ per contract.
        At Scout's BID_PRICE of 99¢, that's 1¢/contract if correct.
        """
        bracket = signal['bracket']
        action = signal['action']
        side = 'no' if action == 'BUY_NO' else 'yes'
        
        # GLOBAL TICKER LOCK — prevents Scout + METAR sniper double-trade
        if bracket.ticker in self.sniper.processed_tickers:
            logger.info(f"[SCOUT] {bracket.ticker} already traded (global lock), skipping")
            return False
        
        # Check price
        price = bracket.no_ask if side == 'no' else bracket.yes_ask
        if price > self.sniper.max_price:
            logger.info(f"[SCOUT] {station} {bracket.subtitle}: Price {price}¢ > max {self.sniper.max_price}¢, skipping")
            return False
        
        logger.info(f"[SCOUT] EXECUTING: {action} {bracket.ticker} @ {BID_PRICE}¢ x{TRADE_QUANTITY}")
        
        if not self.sniper.live_mode:
            logger.info(f"[SCOUT] DRY RUN — holding to settlement (no hedge)")
            position = ScoutPosition(
                ticker=bracket.ticker,
                station=station,
                side=side,
                action=action,
                entry_price=price,
                entry_time=datetime.now(timezone.utc),
                quantity=TRADE_QUANTITY,
                reason=signal['reason'],
                wethr_temp=signal['wethr_temp']
            )
            self.positions.append(position)
            bracket.traded = True
            self.sniper.processed_tickers.add(bracket.ticker)
            return True
        
        try:
            # BUY ONLY — hold to settlement for (100 - BID_PRICE)¢ profit
            result = self.sniper.kalshi.create_order(
                ticker=bracket.ticker,
                side=side,
                action='buy',
                count=TRADE_QUANTITY,
                order_type='limit',
                price_cents=BID_PRICE
            )
            
            order_id = result.get('order', {}).get('order_id')
            logger.info(f"[SCOUT] Buy placed: {order_id} @ {BID_PRICE}¢ — holding to settlement")
            
            # Track position for Ejection Seat monitoring
            position = ScoutPosition(
                ticker=bracket.ticker,
                station=station,
                side=side,
                action=action,
                entry_price=BID_PRICE,
                entry_time=datetime.now(timezone.utc),
                quantity=TRADE_QUANTITY,
                reason=signal['reason'],
                wethr_temp=signal['wethr_temp']
            )
            self.positions.append(position)
            
            bracket.traded = True
            self.sniper.processed_tickers.add(bracket.ticker)
            return True
            
        except Exception as e:
            logger.error(f"[SCOUT] Trade execution failed: {e}")
            return False
    
    def check_ejection_seat(self, station: str, observed_high: Optional[int], 
                            observed_low: Optional[int], brackets: List[Any]):
        """
        The Ejection Seat: If hourly METAR contradicts Scout position, exit immediately.
        
        Called by smart_poller when it processes a new METAR.
        
        Logic:
        - For HIGH brackets where we bought NO (expecting bracket to die):
          If METAR observed_high <= bracket.cap_strike, the bracket is SAFE (we were wrong)
          
        - For LOW brackets where we bought NO (expecting bracket to die):
          If METAR observed_low >= bracket.floor_strike, the bracket is SAFE (we were wrong)
        
        Args:
            station: Station code
            observed_high: Current day's high from METAR
            observed_low: Current day's low from METAR
            brackets: List of BracketState objects to check against
        """
        # Build a lookup of brackets by ticker
        bracket_lookup = {b.ticker: b for b in brackets}
        
        for position in self.positions:
            if position.station != station or position.ejected:
                continue
            
            # Find the bracket for this position
            bracket = bracket_lookup.get(position.ticker)
            if not bracket:
                continue
            
            should_eject = False
            ejection_reason = None
            
            # Check if METAR contradicts our position
            if position.action == 'BUY_NO':
                # We bought NO expecting the bracket to be DEAD
                
                if bracket.signal_type == 'high' and observed_high is not None:
                    # For HIGH brackets: if observed_high <= cap, bracket is still SAFE
                    if bracket.cap_strike is not None and observed_high <= bracket.cap_strike:
                        should_eject = True
                        ejection_reason = (f"METAR HIGH {observed_high}°F <= cap {bracket.cap_strike}°F "
                                          f"(Scout predicted > cap)")
                
                elif bracket.signal_type == 'low' and observed_low is not None:
                    # For LOW brackets: if observed_low >= floor, bracket is still SAFE
                    if bracket.floor_strike is not None and observed_low >= bracket.floor_strike:
                        should_eject = True
                        ejection_reason = (f"METAR LOW {observed_low}°F >= floor {bracket.floor_strike}°F "
                                          f"(Scout predicted < floor)")
            
            if should_eject:
                logger.warning(f"[SCOUT] EJECTION TRIGGERED: {position.ticker}")
                logger.warning(f"[SCOUT] Reason: {ejection_reason}")
                
                position.ejected = True
                position.ejection_reason = ejection_reason
                
                # Execute market sell to exit position immediately
                if self.sniper.live_mode:
                    try:
                        eject_result = self.sniper.kalshi.create_order(
                            ticker=position.ticker,
                            side=position.side,
                            action='sell',
                            count=position.quantity,
                            order_type='market',  # Market order for immediate exit
                            price_cents=1  # Will fill at best available
                        )
                        logger.warning(f"[SCOUT] EJECTED: {eject_result.get('order', {}).get('order_id')}")
                    except Exception as e:
                        logger.error(f"[SCOUT] EJECTION FAILED: {e}")
                        # Try limit sell at 50¢ as backup
                        try:
                            backup_result = self.sniper.kalshi.create_order(
                                ticker=position.ticker,
                                side=position.side,
                                action='sell',
                                count=position.quantity,
                                order_type='limit',
                                price_cents=50  # Fire sale
                            )
                            logger.warning(f"[SCOUT] EJECTED (backup): {backup_result.get('order', {}).get('order_id')}")
                        except Exception as e2:
                            logger.error(f"[SCOUT] BACKUP EJECTION FAILED: {e2}")
                else:
                    logger.warning(f"[SCOUT] DRY RUN - would have ejected")
    
    def purge_stale_positions(self):
        """
        Purge positions from previous days.
        Called at the start of poll() to clean up old positions.
        """
        if not self.positions:
            return
        
        now = datetime.now(timezone.utc)
        stale_count = 0
        fresh_positions = []
        
        for pos in self.positions:
            # Get station's local timezone
            from smart_poller import STATIONS
            cfg = STATIONS.get(pos.station, {})
            tz_name = cfg.get('timezone', 'America/New_York')
            tz = ZoneInfo(tz_name)
            
            # Check if position is from today (local time)
            pos_local = pos.entry_time.astimezone(tz)
            now_local = now.astimezone(tz)
            
            if pos_local.date() < now_local.date():
                logger.info(f"[SCOUT] Purging stale position: {pos.ticker} (from {pos_local.date()})")
                stale_count += 1
            else:
                fresh_positions.append(pos)
        
        if stale_count > 0:
            logger.info(f"[SCOUT] Purged {stale_count} stale positions")
            self.positions = fresh_positions
    
    def poll(self):
        """
        Main Scout polling loop iteration.
        
        Should be called from smart_poller's main loop.
        """
        # Check for conflict window (METAR drop time)
        if self.is_conflict_window():
            if not self.dormant:
                logger.info("[SCOUT] Entering DORMANT mode (conflict window)")
                self.dormant = True
            return
        
        if self.dormant:
            logger.info("[SCOUT] Exiting DORMANT mode")
            self.dormant = False
        
        # Respect polling interval (30s)
        now = datetime.now(timezone.utc)
        if self.last_poll and (now - self.last_poll).total_seconds() < SCOUT_POLL_INTERVAL_SECONDS:
            return
        
        self.last_poll = now
        
        # Purge any stale positions from previous days
        self.purge_stale_positions()
        
        # Count active stations for logging
        active_stations = 0
        
        # Poll each active station
        for station, state in self.sniper.states.items():
            # Skip stations with no open brackets
            if not state.high_watchlist and not state.low_watchlist:
                continue
            
            active_stations += 1
            
            # Fetch wethr.net data
            data = self.fetch_wethr_data(station)
            if not data:
                continue
            
            # Parse observation time
            obs_time_str = data.get('observation_time')
            if obs_time_str:
                obs_time = self.parse_observation_time(obs_time_str)
                if obs_time and self.is_stale(obs_time):
                    logger.debug(f"[SCOUT] {station} observation stale, skipping")
                    continue
            
            # Get temperature fields
            temp_display = data.get('temperature_display')
            lowest_probable = data.get('lowest_probable')
            highest_probable = data.get('highest_probable')
            dsm_high = data.get('dsm_high_display')
            
            # Physics Gate
            if temp_display is not None:
                if not self.physics_gate(station, float(temp_display)):
                    continue
            
            # Check for HIGH bracket locks
            if lowest_probable is not None and state.high_watchlist:
                signals = self.check_high_lock(station, int(lowest_probable), state.high_watchlist)
                for signal in signals:
                    self.execute_scout_trade(signal, station)
            
            # Check for LOW bracket locks
            if highest_probable is not None and state.low_watchlist:
                signals = self.check_low_lock(station, int(highest_probable), state.low_watchlist)
                for signal in signals:
                    self.execute_scout_trade(signal, station)
            
            # Check DSM invalidation (informational)
            if dsm_high is not None and state.high_watchlist:
                self.check_dsm_kill(station, int(dsm_high), state.high_watchlist)
        
        logger.info(f"[SCOUT] Polled {active_stations} stations | Positions: {len(self.positions)}")


# ============================================================
# INTEGRATION HELPER
# ============================================================

def create_scout(sniper: Any) -> ASOSScout:
    """
    Factory function to create an ASOS Scout instance.
    
    Usage in smart_poller.py:
        from wethr_integration import create_scout
        
        # In WXSniper.__init__():
        self.scout = create_scout(self)
        
        # After init_watchlists/fetch_historical_temps:
        self.scout.seed_observed_temps()
        
        # In main loop:
        self.scout.poll()
        
        # After date rollover:
        self.scout.seed_observed_temps()
    """
    scout = ASOSScout(sniper)
    # NOTE: Don't seed here - fetch_historical_temps() will reset observed_high/low
    # Caller should call seed_observed_temps() AFTER fetch_historical_temps()
    return scout


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    # Test wethr.net API connectivity
    logging.basicConfig(level=logging.INFO)
    
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {WETHR_API_KEY}',
        'User-Agent': 'WXSniper-Scout/5.0-test'
    })
    
    test_stations = ['KNYC', 'KMDW', 'KLAX']
    
    print("Testing wethr.net API connectivity...\n")
    
    for station in test_stations:
        try:
            resp = session.get(WETHR_BASE_URL, params={
                'station_code': station,
                'mode': 'latest'
            }, timeout=10)
            
            data = resp.json()
            
            print(f"{station}:")
            print(f"  observation_time: {data.get('observation_time')}")
            print(f"  temperature_display: {data.get('temperature_display')}°F")
            print(f"  lowest_probable: {data.get('lowest_probable')}°F")
            print(f"  highest_probable: {data.get('highest_probable')}°F")
            print(f"  dsm_high_display: {data.get('dsm_high_display')}°F")
            print()
            
        except Exception as e:
            print(f"{station}: ERROR - {e}\n")
