"""
WX Sniper - Trading Logic (V2 - Correct Implementation)

Strategy:
1. Parse synoptic METAR for 6-hour max/min temps
2. Find Kalshi bracket containing that exact temperature
3. If bracket is priced < 90¢, BUY immediately (we have confirmation!)
4. Profit when it settles at $1.00

The 6-hour extremes ALREADY HAPPENED - we're buying certainty.
"""

import os
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional, List, Tuple
import re

from metar_parser import parse_metar, MetarTemps
from kalshi_client import KalshiClient
from config import STATIONS


@dataclass
class TradeSignal:
    """A signal to trade based on METAR data"""
    station: str           # ICAO code (KSFO)
    signal_type: str       # 'high' or 'low'
    temperature_f: int     # The confirmed temperature
    metar_time: datetime   # When the METAR was issued
    market_date: str       # Date string for market (29JAN28)


@dataclass 
class TradeResult:
    """Result of attempting a trade"""
    signal: Optional[TradeSignal]
    success: bool
    market_ticker: Optional[str] = None
    bracket_range: Optional[str] = None  # e.g., "60° to 61°"
    price_paid_cents: Optional[int] = None
    contracts: Optional[int] = None
    order_id: Optional[str] = None
    error: Optional[str] = None


class WXSniper:
    """
    Main trading bot for weather temperature markets.
    """
    
    def __init__(self, dry_run: bool = True, max_price_cents: int = 90):
        """
        Initialize the sniper.
        
        Args:
            dry_run: If True, don't execute real trades
            max_price_cents: Maximum price to pay (default 90¢)
        """
        self.dry_run = dry_run
        self.max_price_cents = max_price_cents
        self.kalshi = KalshiClient()
        
        print(f"[SNIPER] Initialized - dry_run={dry_run}, max_price={max_price_cents}¢")
    
    def process_metar(self, metar_text: str) -> List[TradeResult]:
        """
        Process a METAR and execute trades if opportunities exist.
        
        Args:
            metar_text: Raw METAR string
            
        Returns:
            List of TradeResult objects
        """
        results = []
        
        # Parse the METAR
        parsed = parse_metar(metar_text)
        
        if not parsed.station:
            return [TradeResult(
                signal=None,
                success=False,
                error="Could not parse station from METAR"
            )]
        
        # Check if this station is one we trade
        station_config = STATIONS.get(parsed.station)
        if not station_config:
            return [TradeResult(
                signal=None,
                success=False,
                error=f"Station {parsed.station} not in configured stations"
            )]
        
        # Get the market date (handle timezone!)
        market_date = self._get_market_date(parsed)
        
        print(f"[SNIPER] Processing {parsed.station} for date {market_date}")
        print(f"[SNIPER] 6hr max: {parsed.six_hour_max_f_rounded}°F, 6hr min: {parsed.six_hour_min_f_rounded}°F")
        
        # Check for HIGH temperature trade
        if parsed.six_hour_max_f_rounded is not None:
            high_ticker_base = station_config.get('kalshi_high_ticker')
            if high_ticker_base:
                result = self._attempt_trade(
                    station=parsed.station,
                    signal_type='high',
                    temperature_f=parsed.six_hour_max_f_rounded,
                    ticker_base=high_ticker_base,
                    market_date=market_date,
                    metar_time=parsed.observation_time
                )
                results.append(result)
        
        # Check for LOW temperature trade
        if parsed.six_hour_min_f_rounded is not None:
            low_ticker_base = station_config.get('kalshi_low_ticker')
            if low_ticker_base:
                result = self._attempt_trade(
                    station=parsed.station,
                    signal_type='low', 
                    temperature_f=parsed.six_hour_min_f_rounded,
                    ticker_base=low_ticker_base,
                    market_date=market_date,
                    metar_time=parsed.observation_time
                )
                results.append(result)
        
        if not results:
            results.append(TradeResult(
                signal=None,
                success=False,
                error="No 6-hour temperature groups found in METAR"
            ))
        
        return results
    
    def _get_market_date(self, parsed: MetarTemps) -> str:
        """
        Get the market date string (e.g., '29JAN26') for a METAR.
        
        CRITICAL: Kalshi markets are in LOCAL TIME, METARs are in UTC!
        We need to convert based on station timezone WITH DST AWARENESS.
        """
        try:
            from zoneinfo import ZoneInfo  # Python 3.9+
        except ImportError:
            from backports.zoneinfo import ZoneInfo  # Fallback
        
        if parsed.observation_time:
            # Make sure it's UTC aware
            obs_utc = parsed.observation_time.replace(tzinfo=ZoneInfo('UTC'))
        else:
            from datetime import datetime, timezone
            obs_utc = datetime.now(timezone.utc)
        
        # Station timezone mapping (IANA timezone names handle DST automatically)
        station_timezones = {
            'KSFO': 'America/Los_Angeles',
            'KLAS': 'America/Los_Angeles',
            'KSEA': 'America/Los_Angeles',
            'KLAX': 'America/Los_Angeles',
            'KDEN': 'America/Denver',
            'KAUS': 'America/Chicago',
            'KMDW': 'America/Chicago',
            'KMSY': 'America/Chicago',
            'KNYC': 'America/New_York',
            'KJFK': 'America/New_York',
            'KPHL': 'America/New_York',
            'KMIA': 'America/New_York',
            'KDCA': 'America/New_York',
        }
        
        tz_name = station_timezones.get(parsed.station, 'America/New_York')
        local_tz = ZoneInfo(tz_name)
        local_time = obs_utc.astimezone(local_tz)
        
        # Format as Kalshi expects: 29JAN26 (day + month + 2-digit year)
        return local_time.strftime("%d%b%y").upper()
    
    def _attempt_trade(
        self,
        station: str,
        signal_type: str,
        temperature_f: int,
        ticker_base: str,
        market_date: str,
        metar_time: datetime
    ) -> TradeResult:
        """
        Attempt to find and execute a trade for a confirmed temperature.
        
        NEW LOGIC: Consider BOTH YES and NO on ALL brackets!
        - If temp IS in bracket → YES wins at $1.00
        - If temp is NOT in bracket → NO wins at $1.00
        
        Buy whichever has the best edge (lowest price for confirmed winner)
        """
        signal = TradeSignal(
            station=station,
            signal_type=signal_type,
            temperature_f=temperature_f,
            metar_time=metar_time,
            market_date=market_date
        )
        
        print(f"[SNIPER] Looking for trades on {signal_type} with confirmed temp {temperature_f}°F")
        print(f"[SNIPER] Ticker base: {ticker_base}, date: {market_date}")
        
        try:
            # Find the BEST trade across ALL brackets
            best_trade = self._find_best_trade(
                ticker_base=ticker_base,
                market_date=market_date,
                temperature_f=temperature_f,
                signal_type=signal_type
            )
            
            if not best_trade:
                return TradeResult(
                    signal=signal,
                    success=False,
                    error=f"No profitable trade found for {temperature_f}°F"
                )
            
            market_ticker, bracket_range, side, price_cents, edge_cents = best_trade
            
            print(f"[SNIPER] Best trade: {side.upper()} on {market_ticker}")
            print(f"[SNIPER] Range: {bracket_range}, Price: {price_cents}¢, Edge: {edge_cents}¢")
            
            # Check if price is acceptable
            if price_cents > self.max_price_cents:
                return TradeResult(
                    signal=signal,
                    success=False,
                    market_ticker=market_ticker,
                    bracket_range=bracket_range,
                    price_paid_cents=price_cents,
                    error=f"Price {price_cents}¢ exceeds max {self.max_price_cents}¢"
                )
            
            # Execute the trade!
            if self.dry_run:
                print(f"[SNIPER] 🧪 DRY RUN - Would buy {side.upper()} on {market_ticker} at {price_cents}¢")
                return TradeResult(
                    signal=signal,
                    success=True,
                    market_ticker=market_ticker,
                    bracket_range=f"{bracket_range} ({side.upper()})",
                    price_paid_cents=price_cents,
                    contracts=1,
                    error=f"DRY RUN - {edge_cents}¢ edge"
                )
            else:
                # LIVE TRADE - Use limit order at ask price for predictable fills
                print(f"[SNIPER] 🔴 LIVE TRADE - Buying {side.upper()} on {market_ticker} at {price_cents}¢")
                order = self.kalshi.create_order(
                    ticker=market_ticker,
                    side=side,  # 'yes' or 'no'
                    action='buy',
                    count=1,
                    price_cents=price_cents,
                    order_type='limit'  # Use limit at ask for predictable fills
                )
                
                return TradeResult(
                    signal=signal,
                    success=True,
                    market_ticker=market_ticker,
                    bracket_range=f"{bracket_range} ({side.upper()})",
                    price_paid_cents=price_cents,
                    contracts=1,
                    order_id=order.get('order_id') if order else None,
                    error=None
                )
                
        except Exception as e:
            print(f"[SNIPER] Error: {e}")
            import traceback
            traceback.print_exc()
            return TradeResult(
                signal=signal,
                success=False,
                error=str(e)
            )
    
    def _find_best_trade(
        self,
        ticker_base: str,
        market_date: str,
        temperature_f: int,
        signal_type: str
    ) -> Optional[Tuple[str, str, str, int, int]]:
        """
        Find the BEST trade across all brackets for a confirmed temperature.
        
        For each bracket:
        - If temp IS in bracket → YES wins, check YES ask price
        - If temp is NOT in bracket → NO wins, check NO ask price
        
        Returns:
            Tuple of (market_ticker, bracket_range, side, price_cents, edge_cents) or None
            side is 'yes' or 'no'
        """
        event_ticker = f"{ticker_base}-{market_date}"
        
        print(f"[SNIPER] Fetching markets for event: {event_ticker}")
        
        try:
            markets = self.kalshi.get_markets(event_ticker=event_ticker)
            
            if not markets:
                print(f"[SNIPER] No markets found for {event_ticker}")
                return None
            
            print(f"[SNIPER] Found {len(markets)} brackets, analyzing all...")
            
            best_trade = None
            best_edge = -100  # Worst possible
            
            for market in markets:
                ticker = market.get('ticker', '')
                subtitle = market.get('yes_sub_title', '') or market.get('subtitle', '')
                
                # Get prices - handle different formats
                yes_ask = self._parse_price(market.get('yes_ask'), market.get('yes_ask_dollars'))
                no_ask = self._parse_price(market.get('no_ask'), market.get('no_ask_dollars'))
                
                # Determine if temp is IN this bracket
                temp_in_bracket = self._temp_in_bracket(subtitle, ticker, temperature_f)
                
                if temp_in_bracket:
                    # YES wins - check YES price
                    if yes_ask is not None and yes_ask <= self.max_price_cents:
                        edge = 100 - yes_ask  # Profit potential
                        print(f"  [YES] {ticker}: {subtitle} @ {yes_ask}¢ → {edge}¢ edge (temp IN bracket)")
                        if edge > best_edge:
                            best_edge = edge
                            best_trade = (ticker, subtitle, 'yes', yes_ask, edge)
                else:
                    # NO wins - check NO price
                    if no_ask is not None and no_ask <= self.max_price_cents:
                        edge = 100 - no_ask  # Profit potential
                        print(f"  [NO]  {ticker}: {subtitle} @ {no_ask}¢ → {edge}¢ edge (temp NOT in bracket)")
                        if edge > best_edge:
                            best_edge = edge
                            best_trade = (ticker, subtitle, 'no', no_ask, edge)
            
            if best_trade:
                print(f"[SNIPER] ✓ Best trade: {best_trade[2].upper()} on {best_trade[0]} @ {best_trade[3]}¢ ({best_trade[4]}¢ edge)")
            else:
                print(f"[SNIPER] No trade found under {self.max_price_cents}¢ threshold")
            
            return best_trade
            
        except Exception as e:
            print(f"[SNIPER] Error fetching markets: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _parse_price(self, price_raw, price_dollars) -> Optional[int]:
        """Parse price from various Kalshi formats to cents"""
        if price_dollars:
            try:
                return int(float(price_dollars) * 100)
            except:
                pass
        if price_raw is not None:
            try:
                if isinstance(price_raw, str):
                    return int(float(price_raw) * 100)
                elif price_raw < 2:  # Probably in dollars
                    return int(price_raw * 100)
                else:
                    return int(price_raw)
            except:
                pass
        return None
    
    def _temp_in_bracket(self, subtitle: str, ticker: str, temperature_f: int) -> bool:
        """
        Check if a temperature falls within a bracket.
        
        Bracket formats:
        - "60° to 61°" → temp 60 or 61 is IN
        - "57° or below" → temp <= 57 is IN  
        - "66° or above" → temp >= 66 is IN
        """
        subtitle_lower = subtitle.lower() if subtitle else ''
        
        # Range: "60° to 61°"
        range_match = re.search(r'(\d+)°?\s*to\s*(\d+)°?', subtitle_lower)
        if range_match:
            low = int(range_match.group(1))
            high = int(range_match.group(2))
            return low <= temperature_f <= high
        
        # Below: "57° or below"
        below_match = re.search(r'(\d+)°?\s*or\s*below', subtitle_lower)
        if below_match:
            threshold = int(below_match.group(1))
            return temperature_f <= threshold
        
        # Above: "66° or above"
        above_match = re.search(r'(\d+)°?\s*or\s*above', subtitle_lower)
        if above_match:
            threshold = int(above_match.group(1))
            return temperature_f >= threshold
        
        # Fallback: parse from ticker (e.g., -T60 means 60-61)
        ticker_match = re.search(r'-T?(\d+)$', ticker)
        if ticker_match:
            bracket_temp = int(ticker_match.group(1))
            return bracket_temp <= temperature_f <= bracket_temp + 1
        
        return False


# Global sniper instance
_sniper_instance = None

def get_sniper(dry_run: bool = True) -> WXSniper:
    """Get or create the global sniper instance"""
    global _sniper_instance
    if _sniper_instance is None:
        from config import MAX_BRACKET_PRICE_CENTS
        _sniper_instance = WXSniper(dry_run=dry_run, max_price_cents=MAX_BRACKET_PRICE_CENTS)
    return _sniper_instance


def metar_callback(metar_text: str) -> List[TradeResult]:
    """
    Callback function for processing METARs from any source.
    Used by SMS webhook and polling.
    """
    sniper = get_sniper()
    return sniper.process_metar(metar_text)
