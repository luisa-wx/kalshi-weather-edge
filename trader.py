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

from metar_parser import parse_metar, ParsedMETAR
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
    
    def _get_market_date(self, parsed: ParsedMETAR) -> str:
        """
        Get the market date string (e.g., '29JAN26') for a METAR.
        
        CRITICAL: Kalshi markets are in LOCAL TIME, METARs are in UTC!
        We need to convert based on station timezone.
        """
        if parsed.observation_time:
            obs_utc = parsed.observation_time
        else:
            obs_utc = datetime.now(timezone.utc)
        
        # Get station timezone offset (simplified - should use proper tz)
        # Most US stations are UTC-5 to UTC-8
        station_offsets = {
            'KSFO': -8,  # PST
            'KLAS': -8,  # PST  
            'KSEA': -8,  # PST
            'KLAX': -8,  # PST
            'KDEN': -7,  # MST
            'KAUS': -6,  # CST
            'KMDW': -6,  # CST
            'KMSY': -6,  # CST
            'KNYC': -5,  # EST (Note: usually KJFK, KLGA, KNYC)
            'KJFK': -5,  # EST
            'KPHL': -5,  # EST
            'KMIA': -5,  # EST
            'KDCA': -5,  # EST
        }
        
        offset_hours = station_offsets.get(parsed.station, -5)  # Default EST
        local_time = obs_utc + timedelta(hours=offset_hours)
        
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
        
        Args:
            station: ICAO code
            signal_type: 'high' or 'low'
            temperature_f: Confirmed temperature in F
            ticker_base: Base ticker (e.g., 'KXHIGHTSFO')
            market_date: Date string (e.g., '29JAN26')
            metar_time: METAR observation time
        """
        signal = TradeSignal(
            station=station,
            signal_type=signal_type,
            temperature_f=temperature_f,
            metar_time=metar_time,
            market_date=market_date
        )
        
        print(f"[SNIPER] Looking for {signal_type} bracket containing {temperature_f}°F")
        print(f"[SNIPER] Ticker base: {ticker_base}, date: {market_date}")
        
        try:
            # Find the bracket that contains this temperature
            bracket = self._find_bracket_for_temp(
                ticker_base=ticker_base,
                market_date=market_date,
                temperature_f=temperature_f,
                signal_type=signal_type
            )
            
            if not bracket:
                return TradeResult(
                    signal=signal,
                    success=False,
                    error=f"No bracket found containing {temperature_f}°F"
                )
            
            market_ticker, bracket_range, yes_ask = bracket
            
            print(f"[SNIPER] Found bracket: {market_ticker}")
            print(f"[SNIPER] Range: {bracket_range}, Ask: {yes_ask}¢")
            
            # Check if price is acceptable
            if yes_ask is None:
                return TradeResult(
                    signal=signal,
                    success=False,
                    market_ticker=market_ticker,
                    bracket_range=bracket_range,
                    error="No ask price available"
                )
            
            if yes_ask > self.max_price_cents:
                return TradeResult(
                    signal=signal,
                    success=False,
                    market_ticker=market_ticker,
                    bracket_range=bracket_range,
                    price_paid_cents=yes_ask,
                    error=f"Price {yes_ask}¢ exceeds max {self.max_price_cents}¢"
                )
            
            # Execute the trade!
            if self.dry_run:
                print(f"[SNIPER] 🧪 DRY RUN - Would buy {market_ticker} at {yes_ask}¢")
                return TradeResult(
                    signal=signal,
                    success=True,
                    market_ticker=market_ticker,
                    bracket_range=bracket_range,
                    price_paid_cents=yes_ask,
                    contracts=1,
                    error="DRY RUN - no actual trade"
                )
            else:
                # LIVE TRADE
                print(f"[SNIPER] 🔴 LIVE TRADE - Buying {market_ticker} at {yes_ask}¢")
                order = self.kalshi.create_order(
                    ticker=market_ticker,
                    side='yes',
                    action='buy',
                    count=1,
                    price_cents=yes_ask,
                    order_type='market'  # Take whatever is available
                )
                
                return TradeResult(
                    signal=signal,
                    success=True,
                    market_ticker=market_ticker,
                    bracket_range=bracket_range,
                    price_paid_cents=yes_ask,
                    contracts=1,
                    order_id=order.get('order_id')
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
    
    def _find_bracket_for_temp(
        self,
        ticker_base: str,
        market_date: str,
        temperature_f: int,
        signal_type: str
    ) -> Optional[Tuple[str, str, int]]:
        """
        Find the Kalshi bracket that contains the given temperature.
        
        Returns:
            Tuple of (market_ticker, bracket_range, yes_ask_cents) or None
        """
        # Construct event ticker (e.g., KXHIGHTSFO-29JAN26)
        event_ticker = f"{ticker_base}-{market_date}"
        
        print(f"[SNIPER] Fetching markets for event: {event_ticker}")
        
        try:
            # Get all markets for this event
            markets = self.kalshi.get_markets(event_ticker=event_ticker)
            
            if not markets:
                print(f"[SNIPER] No markets found for {event_ticker}")
                # Try to list what events ARE available
                try:
                    all_markets = self.kalshi.get_markets(series_ticker=ticker_base, limit=10)
                    if all_markets:
                        print(f"[SNIPER] Available events for {ticker_base}:")
                        seen = set()
                        for m in all_markets:
                            evt = m.get('event_ticker', '')
                            if evt and evt not in seen:
                                print(f"  - {evt}")
                                seen.add(evt)
                except:
                    pass
                return None
            
            print(f"[SNIPER] Found {len(markets)} brackets")
            
            # Find the bracket containing our temperature
            for market in markets:
                ticker = market.get('ticker', '')
                subtitle = market.get('yes_sub_title', '') or market.get('subtitle', '')
                
                # Parse the bracket range from subtitle or ticker
                bracket_match = self._parse_bracket(subtitle, ticker, temperature_f, signal_type)
                
                if bracket_match:
                    # Get the ask price
                    yes_ask = market.get('yes_ask')
                    yes_ask_dollars = market.get('yes_ask_dollars')
                    
                    # Handle different formats
                    if yes_ask_dollars:
                        # It's in dollars like "0.98"
                        yes_ask = int(float(yes_ask_dollars) * 100)
                    elif yes_ask is not None:
                        if isinstance(yes_ask, str):
                            yes_ask = int(float(yes_ask) * 100)
                        elif yes_ask < 2:  # Probably in dollars
                            yes_ask = int(yes_ask * 100)
                    
                    print(f"[SNIPER] ✓ Match! {ticker} - {subtitle} @ {yes_ask}¢")
                    return (ticker, subtitle or bracket_match, yes_ask)
            
            print(f"[SNIPER] No bracket found containing {temperature_f}°F")
            print(f"[SNIPER] Available brackets:")
            for m in markets[:10]:
                print(f"  - {m.get('ticker')}: {m.get('yes_sub_title', m.get('subtitle', ''))}")
            
            return None
            
        except Exception as e:
            print(f"[SNIPER] Error fetching markets: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _parse_bracket(
        self,
        subtitle: str,
        ticker: str,
        temperature_f: int,
        signal_type: str
    ) -> Optional[str]:
        """
        Check if a bracket contains the given temperature.
        
        Bracket formats from Kalshi:
        - "60° to 61°" (temp is 60 or 61)
        - "60° or below" (temp <= 60)
        - "66° or above" (temp >= 66)
        
        For HIGHS: we want the bracket where the temp falls within range
        For LOWS: same logic
        """
        subtitle_lower = subtitle.lower() if subtitle else ''
        
        # Check for range bracket: "60° to 61°" or "60 to 61"
        range_match = re.search(r'(\d+)°?\s*to\s*(\d+)°?', subtitle_lower)
        if range_match:
            low = int(range_match.group(1))
            high = int(range_match.group(2))
            # Temperature falls within this range
            if low <= temperature_f <= high:
                return f"{low}° to {high}°"
        
        # Check for "X or below" / "X° or below"
        below_match = re.search(r'(\d+)°?\s*or\s*below', subtitle_lower)
        if below_match:
            threshold = int(below_match.group(1))
            if temperature_f <= threshold:
                return f"{threshold}° or below"
        
        # Check for "X or above" / "X° or above"
        above_match = re.search(r'(\d+)°?\s*or\s*above', subtitle_lower)
        if above_match:
            threshold = int(above_match.group(1))
            if temperature_f >= threshold:
                return f"{threshold}° or above"
        
        # Try to parse from ticker (e.g., KXHIGHTSFO-29JAN26-T60)
        # The bracket temp indicates the LOW end of the range
        ticker_match = re.search(r'-T?(\d+)$', ticker)
        if ticker_match:
            bracket_temp = int(ticker_match.group(1))
            # Bracket "60" typically means "60 to 61" (2-degree ranges)
            if bracket_temp <= temperature_f <= bracket_temp + 1:
                return f"{bracket_temp}° to {bracket_temp + 1}°"
        
        return None


# Global sniper instance
_sniper_instance = None

def get_sniper(dry_run: bool = True) -> WXSniper:
    """Get or create the global sniper instance"""
    global _sniper_instance
    if _sniper_instance is None:
        _sniper_instance = WXSniper(dry_run=dry_run)
    return _sniper_instance


def metar_callback(metar_text: str) -> List[TradeResult]:
    """
    Callback function for processing METARs from any source.
    Used by SMS webhook and polling.
    """
    sniper = get_sniper()
    return sniper.process_metar(metar_text)
