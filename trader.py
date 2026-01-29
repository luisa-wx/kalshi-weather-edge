"""
WX Sniper - Trading Logic (V3 - NO-Only Safe Strategy)

Strategy:
1. Parse synoptic METAR for 6-hour max/min temps
2. Fetch ALL brackets from Kalshi API with floor_strike/cap_strike/strike_type
3. For each bracket, determine if NO is LOCKED based on observed temp
4. Buy NO on locked brackets where price < threshold
5. Immediately place 99¢ sell limit as hedge

KEY INSIGHT: We only trade NOs because:
- For HIGH markets: once temp exceeds a bracket's cap, it can't go back down
- For LOW markets: once temp drops below a bracket's floor, it can't go back up
- We do NOT trade YES because the final high/low isn't known until day end
"""

import os
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict
import re

from metar_parser import parse_metar, MetarTemps
from kalshi_client import KalshiClient
from config import STATIONS


@dataclass
class TradeSignal:
    """A signal to trade based on METAR data"""
    station: str           # ICAO code (KSFO)
    signal_type: str       # 'high' or 'low'
    temperature_f: int     # The observed 6-hour max/min
    metar_time: datetime   # When the METAR was issued
    market_date: str       # Date string for market (26JAN29)


@dataclass 
class TradeResult:
    """Result of attempting a trade"""
    signal: Optional[TradeSignal]
    success: bool
    market_ticker: Optional[str] = None
    bracket_range: Optional[str] = None  # e.g., "8° to 9°"
    price_paid_cents: Optional[int] = None
    contracts: Optional[int] = None
    order_id: Optional[str] = None
    error: Optional[str] = None


class WXSniper:
    """
    Main trading bot for weather temperature markets.
    
    NO-ONLY SAFE STRATEGY:
    - Only buy NO on brackets where the outcome is LOCKED
    - For HIGH markets: NO is locked when observed temp > cap_strike
    - For LOW markets: NO is locked when observed temp < floor_strike
    """
    
    def __init__(self, dry_run: bool = True, max_price_cents: int = 90):
        """
        Initialize the sniper.
        
        Args:
            dry_run: If True, don't execute real trades
            max_price_cents: Maximum price to pay for NO (default 90¢)
        """
        self.dry_run = dry_run
        self.max_price_cents = max_price_cents
        self.kalshi = KalshiClient()
        
        print(f"[SNIPER] Initialized - dry_run={dry_run}, max_price={max_price_cents}¢")
        print(f"[SNIPER] Strategy: NO-only safe trades (buy locked NOs)")
    
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
        
        # Check for HIGH temperature trades (NO on brackets exceeded by 6hr max)
        if parsed.six_hour_max_f_rounded is not None:
            high_ticker_base = station_config.get('kalshi_high_ticker')
            if high_ticker_base:
                high_results = self._find_locked_no_trades(
                    station=parsed.station,
                    signal_type='high',
                    observed_temp_f=parsed.six_hour_max_f_rounded,
                    ticker_base=high_ticker_base,
                    market_date=market_date,
                    metar_time=parsed.observation_time
                )
                results.extend(high_results)
        
        # Check for LOW temperature trades (NO on brackets below 6hr min)
        if parsed.six_hour_min_f_rounded is not None:
            low_ticker_base = station_config.get('kalshi_low_ticker')
            if low_ticker_base:
                low_results = self._find_locked_no_trades(
                    station=parsed.station,
                    signal_type='low', 
                    observed_temp_f=parsed.six_hour_min_f_rounded,
                    ticker_base=low_ticker_base,
                    market_date=market_date,
                    metar_time=parsed.observation_time
                )
                results.extend(low_results)
        
        if not results:
            results.append(TradeResult(
                signal=None,
                success=False,
                error="No 6-hour temperature groups found in METAR or no locked NO trades available"
            ))
        
        return results
    
    def _get_market_date(self, parsed: MetarTemps) -> str:
        """
        Get the market date string (e.g., '26JAN29') for a METAR.
        
        CRITICAL: Kalshi markets are in LOCAL TIME, METARs are in UTC!
        """
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        
        if parsed.observation_time:
            obs_utc = parsed.observation_time.replace(tzinfo=ZoneInfo('UTC'))
        else:
            obs_utc = datetime.now(timezone.utc)
        
        # Station timezone mapping (IANA names handle DST automatically)
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
        
        # Format as Kalshi expects: 26JAN29 (uppercase)
        return local_time.strftime("%y%b%d").upper()
    
    def _find_locked_no_trades(
        self,
        station: str,
        signal_type: str,  # 'high' or 'low'
        observed_temp_f: int,
        ticker_base: str,
        market_date: str,
        metar_time: datetime
    ) -> List[TradeResult]:
        """
        Find all brackets where NO is LOCKED and execute trades.
        
        For HIGH markets (observed temp is 6hr max so far):
            - NO is locked on brackets where observed_temp > cap_strike
            - Because temp can't go back DOWN
            
        For LOW markets (observed temp is 6hr min so far):
            - NO is locked on brackets where observed_temp < floor_strike
            - Because temp can't go back UP
        
        Returns list of TradeResults for all executed trades.
        """
        results = []
        
        signal = TradeSignal(
            station=station,
            signal_type=signal_type,
            temperature_f=observed_temp_f,
            metar_time=metar_time,
            market_date=market_date
        )
        
        print(f"\n[SNIPER] === Finding locked NO trades for {signal_type.upper()} ===")
        print(f"[SNIPER] Observed temp: {observed_temp_f}°F")
        print(f"[SNIPER] Ticker base: {ticker_base}, date: {market_date}")
        
        try:
            # Fetch all markets for this event
            markets = self._fetch_markets_for_date(ticker_base, market_date)
            
            if not markets:
                return [TradeResult(
                    signal=signal,
                    success=False,
                    error=f"No markets found for {ticker_base} on {market_date}"
                )]
            
            print(f"[SNIPER] Found {len(markets)} brackets, checking for locked NOs...")
            
            # Analyze each bracket
            locked_trades = []
            
            for market in markets:
                ticker = market.get('ticker', '')
                floor_strike = market.get('floor_strike')
                cap_strike = market.get('cap_strike')
                strike_type = market.get('strike_type', '')
                yes_sub = market.get('yes_sub_title', '')
                no_ask = self._parse_price(market.get('no_ask'), market.get('no_ask_dollars'))
                
                # Determine if NO is locked
                no_locked, reason = self._is_no_locked(
                    signal_type=signal_type,
                    observed_temp=observed_temp_f,
                    floor_strike=floor_strike,
                    cap_strike=cap_strike,
                    strike_type=strike_type
                )
                
                if no_locked:
                    edge = 100 - no_ask if no_ask else 0
                    print(f"  ✅ LOCKED: {yes_sub:<15} | NO @ {no_ask}¢ | Edge: {edge}¢ | {reason}")
                    
                    if no_ask and no_ask <= self.max_price_cents:
                        locked_trades.append({
                            'ticker': ticker,
                            'subtitle': yes_sub,
                            'no_ask': no_ask,
                            'edge': edge,
                            'reason': reason
                        })
                    elif no_ask and no_ask > self.max_price_cents:
                        print(f"      → Price {no_ask}¢ exceeds max {self.max_price_cents}¢, skipping")
                else:
                    print(f"  ❌ NOT LOCKED: {yes_sub:<15} | {reason}")
            
            # Execute trades on locked brackets (best edge first)
            locked_trades.sort(key=lambda x: x['edge'], reverse=True)
            
            for trade in locked_trades:
                result = self._execute_no_trade(
                    signal=signal,
                    ticker=trade['ticker'],
                    subtitle=trade['subtitle'],
                    price_cents=trade['no_ask']
                )
                results.append(result)
            
            if not locked_trades:
                results.append(TradeResult(
                    signal=signal,
                    success=False,
                    error=f"No locked NO trades under {self.max_price_cents}¢"
                ))
            
        except Exception as e:
            print(f"[SNIPER] Error: {e}")
            import traceback
            traceback.print_exc()
            results.append(TradeResult(
                signal=signal,
                success=False,
                error=str(e)
            ))
        
        return results
    
    def _is_no_locked(
        self,
        signal_type: str,  # 'high' or 'low'
        observed_temp: int,
        floor_strike: Optional[float],
        cap_strike: Optional[float],
        strike_type: str
    ) -> Tuple[bool, str]:
        """
        Determine if NO is locked for a given bracket.
        
        Uses floor_strike, cap_strike, and strike_type from Kalshi API.
        
        Returns:
            Tuple of (is_locked: bool, reason: str)
        """
        # Convert strikes to int for comparison (they come as float)
        floor = int(floor_strike) if floor_strike is not None else None
        cap = int(cap_strike) if cap_strike is not None else None
        
        if signal_type == 'high':
            # HIGH market: YES wins if final high meets condition
            # NO is locked when observed temp has ALREADY exceeded what YES needs
            
            if strike_type == 'between':
                # YES wins if floor <= high <= cap
                # NO wins if high < floor OR high > cap
                # NO is locked if observed > cap (already above range, can't go down)
                if cap is not None and observed_temp > cap:
                    return True, f"observed {observed_temp}°F > cap {cap}°F, can't go down"
                else:
                    return False, f"could still land in {floor}-{cap} range"
                    
            elif strike_type == 'less':
                # YES wins if high < cap
                # NO wins if high >= cap
                # NO is locked if observed >= cap (already at/above threshold)
                if cap is not None and observed_temp >= cap:
                    return True, f"observed {observed_temp}°F >= cap {cap}°F"
                else:
                    return False, f"high could still stay below {cap}"
                    
            elif strike_type == 'greater':
                # YES wins if high > floor
                # NO wins if high <= floor
                # For HIGH markets, if current temp already > floor, YES might win
                # NO is NEVER locked for 'greater' on HIGH (temp could keep rising)
                return False, f"temp could still exceed {floor}"
                
        elif signal_type == 'low':
            # LOW market: YES wins if final low meets condition
            # NO is locked when observed temp has ALREADY gone below what YES needs
            
            if strike_type == 'between':
                # YES wins if floor <= low <= cap
                # NO wins if low < floor OR low > cap
                # NO is locked if observed < floor (already below range, can't go up)
                if floor is not None and observed_temp < floor:
                    return True, f"observed {observed_temp}°F < floor {floor}°F, can't go up"
                else:
                    return False, f"could still land in {floor}-{cap} range"
                    
            elif strike_type == 'greater':
                # YES wins if low > floor (i.e., low >= floor+1)
                # NO wins if low <= floor
                # NO is locked if observed <= floor (already at/below threshold, can't go up)
                if floor is not None and observed_temp <= floor:
                    return True, f"observed {observed_temp}°F <= floor {floor}°F, can't go up"
                else:
                    return False, f"low could still stay above {floor}"
                    
            elif strike_type == 'less':
                # YES wins if low < cap
                # NO wins if low >= cap
                # For LOW markets, if current temp already < cap, YES might win
                # NO is NEVER locked for 'less' on LOW (temp could keep dropping)
                return False, f"temp could still drop below {cap}"
        
        return False, "unknown strike_type or signal_type"
    
    def _fetch_markets_for_date(self, ticker_base: str, market_date: str) -> List[Dict]:
        """
        Fetch all markets for a given ticker base and date.
        
        Args:
            ticker_base: e.g., 'KXLOWTNYC' or 'KXHIGHNY'
            market_date: e.g., '26JAN29'
            
        Returns:
            List of market dicts from Kalshi API
        """
        # Build event ticker: KXLOWTNYC-26JAN29
        event_ticker = f"{ticker_base}-{market_date}"
        
        print(f"[SNIPER] Fetching markets for event: {event_ticker}")
        
        try:
            markets = self.kalshi.get_markets(event_ticker=event_ticker)
            return markets if markets else []
        except Exception as e:
            print(f"[SNIPER] Error fetching markets: {e}")
            return []
    
    def _execute_no_trade(
        self,
        signal: TradeSignal,
        ticker: str,
        subtitle: str,
        price_cents: int
    ) -> TradeResult:
        """
        Execute a NO trade on a locked bracket.
        
        1. Buy NO at ask price
        2. Immediately place 99¢ sell limit as hedge
        """
        print(f"\n[SNIPER] 🎯 EXECUTING NO TRADE: {ticker}")
        print(f"[SNIPER] Bracket: {subtitle}, Price: {price_cents}¢")
        
        if self.dry_run:
            print(f"[SNIPER] 🧪 DRY RUN - Would buy NO on {ticker} at {price_cents}¢")
            return TradeResult(
                signal=signal,
                success=True,
                market_ticker=ticker,
                bracket_range=f"{subtitle} (NO)",
                price_paid_cents=price_cents,
                contracts=1,
                order_id="DRY_RUN",
                error="Dry run - no real trade"
            )
        
        try:
            # BUY NO
            print(f"[SNIPER] 💰 Buying NO at {price_cents}¢...")
            order = self.kalshi.create_order(
                ticker=ticker,
                side='no',
                action='buy',
                count=1,
                price_cents=price_cents,
                order_type='limit'
            )
            
            buy_order_id = order.get('order_id') if order else None
            print(f"[SNIPER] ✓ Buy order placed: {buy_order_id}")
            
            # HEDGE: Place 99¢ sell limit
            sell_order_id = None
            if buy_order_id:
                try:
                    print(f"[SNIPER] 🛡️ Placing hedge sell at 99¢...")
                    sell_order = self.kalshi.create_order(
                        ticker=ticker,
                        side='no',
                        action='sell',
                        count=1,
                        price_cents=99,
                        order_type='limit'
                    )
                    sell_order_id = sell_order.get('order_id') if sell_order else None
                    print(f"[SNIPER] ✓ Hedge order placed: {sell_order_id}")
                except Exception as hedge_err:
                    print(f"[SNIPER] ⚠️ Failed to place hedge: {hedge_err}")
            
            return TradeResult(
                signal=signal,
                success=True,
                market_ticker=ticker,
                bracket_range=f"{subtitle} (NO)",
                price_paid_cents=price_cents,
                contracts=1,
                order_id=buy_order_id,
                error=f"Hedge: {sell_order_id}" if sell_order_id else "No hedge"
            )
            
        except Exception as e:
            print(f"[SNIPER] ❌ Trade failed: {e}")
            return TradeResult(
                signal=signal,
                success=False,
                market_ticker=ticker,
                bracket_range=f"{subtitle} (NO)",
                price_paid_cents=price_cents,
                error=str(e)
            )
    
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
