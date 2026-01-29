"""
Smart Poller - Pre-scan Kalshi, then target specific stations

Strategy:
1. At :50 - Scan ALL Kalshi markets for opportunities (brackets ≤ threshold)
2. At :52 - Start polling ONLY stations with opportunities  
3. Stop polling a station once we get its synoptic METAR
4. At :02 - Stop all polling

This minimizes aviationweather.gov API calls (100/min limit)
"""

import time
import requests
from datetime import datetime, timezone
from typing import Dict, List, Set, Optional, Tuple
from dataclasses import dataclass

from config import STATIONS, SYNOPTIC_HOURS_UTC, MAX_BRACKET_PRICE_CENTS
from kalshi_client import KalshiClient
from metar_parser import parse_metar


@dataclass
class Opportunity:
    """A potential trading opportunity"""
    station: str
    market_type: str  # 'high' or 'low'
    series_ticker: str
    best_bracket: str
    best_price: int
    side: str  # 'yes' or 'no'


@dataclass  
class ScanResult:
    """Result of pre-scanning Kalshi"""
    opportunities: List[Opportunity]
    stations_to_watch: Set[str]
    scan_time: datetime


class SmartPoller:
    """
    Intelligent poller that:
    1. Pre-scans Kalshi for opportunities
    2. Only polls aviationweather for stations with opportunities
    3. Stops polling each station once synoptic METAR received
    """
    
    def __init__(self, kalshi: KalshiClient, threshold_cents: int = 93):
        self.kalshi = kalshi
        self.threshold = threshold_cents
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "WXSniper/1.0 (weather-trading-bot)"
        })
        
        # Track which stations have received synoptic METARs this cycle
        self.stations_done: Set[str] = set()
        
        # Track last observation time per station
        self.last_obs_time: Dict[str, datetime] = {}
        
    def pre_scan_kalshi(self) -> ScanResult:
        """
        Scan all Kalshi markets to find opportunities.
        Returns list of opportunities and set of stations to watch.
        """
        print(f"\n[SCAN] 🔍 Pre-scanning Kalshi markets...")
        opportunities = []
        stations_to_watch = set()
        
        # Get today's date for each timezone
        now_utc = datetime.now(timezone.utc)
        
        for station, config in STATIONS.items():
            # Check HIGH market
            high_ticker = config.get('kalshi_high_ticker')
            if high_ticker:
                opp = self._check_series_for_opportunity(
                    station, high_ticker, 'high', config['timezone']
                )
                if opp:
                    opportunities.append(opp)
                    stations_to_watch.add(station)
            
            # Check LOW market
            low_ticker = config.get('kalshi_low_ticker')
            if low_ticker:
                opp = self._check_series_for_opportunity(
                    station, low_ticker, 'low', config['timezone']
                )
                if opp:
                    opportunities.append(opp)
                    stations_to_watch.add(station)
        
        print(f"[SCAN] Found {len(opportunities)} opportunities across {len(stations_to_watch)} stations")
        for opp in opportunities:
            print(f"[SCAN]   {opp.station} {opp.market_type.upper()}: {opp.best_bracket} @ {opp.best_price}¢ ({opp.side})")
        
        return ScanResult(
            opportunities=opportunities,
            stations_to_watch=stations_to_watch,
            scan_time=now_utc
        )
    
    def _check_series_for_opportunity(
        self, 
        station: str, 
        series_ticker: str, 
        market_type: str,
        tz_name: str
    ) -> Optional[Opportunity]:
        """Check if a series has any brackets under threshold"""
        try:
            from zoneinfo import ZoneInfo
            
            # Get local date for market filtering
            now_utc = datetime.now(timezone.utc)
            local_tz = ZoneInfo(tz_name)
            local_time = now_utc.astimezone(local_tz)
            date_str = local_time.strftime("%y%b%d").upper()  # e.g., "26JAN29"
            
            # Query Kalshi
            markets = self.kalshi.get_markets(series_ticker=series_ticker, status='open')
            
            if not markets:
                return None
            
            # Filter to today's markets
            today_markets = [m for m in markets if date_str in m.get('ticker', '')]
            
            if not today_markets:
                return None
            
            # Find best opportunity (lowest price that could win)
            best_opp = None
            best_price = 100
            
            for market in today_markets:
                ticker = market.get('ticker', '')
                yes_ask = market.get('yes_ask', 100)
                no_ask = market.get('no_ask', 100)
                
                # Check YES side
                if yes_ask and yes_ask <= self.threshold and yes_ask < best_price:
                    best_price = yes_ask
                    best_opp = Opportunity(
                        station=station,
                        market_type=market_type,
                        series_ticker=series_ticker,
                        best_bracket=ticker,
                        best_price=yes_ask,
                        side='yes'
                    )
                
                # Check NO side
                if no_ask and no_ask <= self.threshold and no_ask < best_price:
                    best_price = no_ask
                    best_opp = Opportunity(
                        station=station,
                        market_type=market_type,
                        series_ticker=series_ticker,
                        best_bracket=ticker,
                        best_price=no_ask,
                        side='no'
                    )
            
            return best_opp
            
        except Exception as e:
            print(f"[SCAN] Error checking {series_ticker}: {e}")
            return None
    
    def fetch_metar(self, station: str) -> Optional[Tuple[str, datetime]]:
        """
        Fetch single station METAR from aviationweather.gov
        Returns (raw_text, obs_time) or None
        """
        try:
            url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=raw"
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            raw_text = response.text.strip()
            if not raw_text or 'error' in raw_text.lower():
                return None
            
            # Parse observation time
            obs_time = self._parse_obs_time(raw_text)
            
            return (raw_text, obs_time)
            
        except Exception as e:
            print(f"[POLL] Error fetching {station}: {e}")
            return None
    
    def _parse_obs_time(self, metar_text: str) -> Optional[datetime]:
        """Parse observation time from METAR"""
        try:
            parts = metar_text.split()
            if len(parts) < 2:
                return None
            
            time_part = parts[1]  # e.g., "281853Z"
            if not time_part.endswith('Z') or len(time_part) != 7:
                return None
            
            day = int(time_part[0:2])
            hour = int(time_part[2:4])
            minute = int(time_part[4:6])
            
            now = datetime.now(timezone.utc)
            return now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
            
        except:
            return None
    
    def is_synoptic_metar(self, metar_text: str) -> bool:
        """Check if METAR contains 6-hour temperature groups"""
        import re
        # Look for T-group: T followed by 8 digits (temp + dewpoint in tenths C)
        # AND 6-hour groups: 10xxx (max) or 20xxx (min)
        has_t_group = bool(re.search(r'\bT\d{8}\b', metar_text))
        has_6hr_group = bool(re.search(r'\b[12]0\d{3}\b', metar_text))
        return has_t_group and has_6hr_group
    
    def poll_targeted_stations(
        self, 
        stations: Set[str], 
        callback,
        poll_interval: int = 5,
        max_duration: int = 600  # 10 minutes max
    ):
        """
        Poll only specified stations until all have synoptic METARs
        
        Args:
            stations: Set of station codes to poll
            callback: Function to call with (station, metar_text) when synoptic found
            poll_interval: Seconds between polls (default 5)
            max_duration: Maximum polling duration in seconds
        """
        self.stations_done = set()
        remaining = stations.copy()
        start_time = time.time()
        
        print(f"\n[POLL] 🎯 Targeting {len(remaining)} stations: {', '.join(remaining)}")
        
        while remaining and (time.time() - start_time) < max_duration:
            now = datetime.now(timezone.utc)
            print(f"[POLL] {now.strftime('%H:%M:%S')}Z | Checking {len(remaining)} stations...")
            
            for station in list(remaining):
                result = self.fetch_metar(station)
                
                if result:
                    raw_text, obs_time = result
                    
                    # Check if this is a NEW metar
                    last_time = self.last_obs_time.get(station)
                    if last_time and obs_time and obs_time <= last_time:
                        continue  # Already seen this one
                    
                    if obs_time:
                        self.last_obs_time[station] = obs_time
                    
                    # Check if it's synoptic
                    if self.is_synoptic_metar(raw_text):
                        print(f"[POLL] ✅ SYNOPTIC METAR: {station}")
                        remaining.discard(station)
                        self.stations_done.add(station)
                        
                        # Call the callback
                        try:
                            callback(station, raw_text)
                        except Exception as e:
                            print(f"[POLL] Callback error for {station}: {e}")
            
            if remaining:
                time.sleep(poll_interval)
        
        if remaining:
            print(f"[POLL] ⏰ Timeout - missing synoptics from: {', '.join(remaining)}")
        else:
            print(f"[POLL] ✅ All {len(self.stations_done)} synoptic METARs received!")
    
    def reset_cycle(self):
        """Reset for new synoptic cycle"""
        self.stations_done = set()


def run_smart_cycle(sniper, dry_run: bool = True):
    """
    Run a complete smart polling cycle:
    1. Pre-scan Kalshi at :50
    2. Poll targeted stations :52 to :02
    3. Execute trades as synoptic METARs arrive
    """
    from trader import WXSniper
    
    kalshi = KalshiClient()
    poller = SmartPoller(kalshi)
    
    # Pre-scan
    scan = poller.pre_scan_kalshi()
    
    if not scan.stations_to_watch:
        print("[SMART] No opportunities found - skipping this cycle")
        return
    
    # Define callback for when synoptic METAR arrives
    def on_synoptic_metar(station: str, metar_text: str):
        print(f"\n[SMART] Processing {station}...")
        results = sniper.process_metar(metar_text)
        for r in results:
            if r.success:
                print(f"[SMART] ✅ TRADE: {r.market_ticker} @ {r.price_paid_cents}¢")
            else:
                print(f"[SMART] ⏭️ No trade: {r.error}")
    
    # Poll targeted stations
    poller.poll_targeted_stations(
        stations=scan.stations_to_watch,
        callback=on_synoptic_metar,
        poll_interval=5,
        max_duration=600  # 10 minutes
    )


if __name__ == "__main__":
    # Test the pre-scan
    from kalshi_client import KalshiClient
    
    kalshi = KalshiClient()
    poller = SmartPoller(kalshi, threshold_cents=97)
    
    scan = poller.pre_scan_kalshi()
    print(f"\nStations to watch: {scan.stations_to_watch}")
