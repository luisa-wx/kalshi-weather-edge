"""
WX Sniper - Main Trading Bot

This is the core trading logic that:
1. Receives METAR data (via SMS webhook or polling)
2. Extracts 6-hour max/min temperatures
3. Compares against tracked daily extremes
4. Finds appropriate Kalshi brackets
5. Executes trades when edge exists
"""

import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass

from config import (
    STATIONS,
    MAX_TRADE_AMOUNT_CENTS,
    MAX_BRACKET_PRICE_CENTS,
    SYNOPTIC_HOURS_UTC
)
from metar_parser import parse_metar, MetarTemps, format_metar_summary
from kalshi_client import KalshiClient, find_temperature_bracket, format_market_info
from state_manager import StateManager

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('wx_sniper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    """Represents a potential trade opportunity"""
    station: str
    bracket_type: str  # "high" or "low"
    temperature_f: int
    source: str  # "6hr_max", "6hr_min", "t_group"
    confidence: str  # "locked" (6hr group) or "indicated" (other)
    raw_temp_c: float
    raw_temp_f: float


@dataclass  
class TradeResult:
    """Result of a trade attempt"""
    success: bool
    signal: TradeSignal
    market_ticker: Optional[str] = None
    price_paid_cents: Optional[int] = None
    contracts: int = 0
    error: Optional[str] = None


class WXSniper:
    """Main trading bot"""
    
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.kalshi = KalshiClient()
        self.state = StateManager()
        
        # Cache of event/market data
        self._market_cache: Dict[str, Dict] = {}
        self._cache_time: Dict[str, datetime] = {}
        
        logger.info(f"WX Sniper initialized (dry_run={dry_run})")
    
    def process_metar(self, metar_text: str) -> List[TradeResult]:
        """
        Process a METAR and execute any trades
        
        Returns list of trade results
        """
        logger.info(f"Processing METAR: {metar_text[:60]}...")
        
        # Parse the METAR
        parsed = parse_metar(metar_text)
        logger.info(f"\n{format_metar_summary(parsed)}")
        
        if not parsed.station:
            logger.warning("Could not determine station from METAR")
            return []
        
        if parsed.station not in STATIONS:
            logger.warning(f"Station {parsed.station} not in configured stations")
            return []
        
        # Generate trade signals
        signals = self._generate_signals(parsed)
        
        if not signals:
            logger.info("No trade signals generated")
            return []
        
        logger.info(f"Generated {len(signals)} trade signals")
        
        # Execute trades
        results = []
        for signal in signals:
            result = self._execute_trade(signal)
            results.append(result)
        
        return results
    
    def _generate_signals(self, parsed: MetarTemps) -> List[TradeSignal]:
        """Generate trade signals from parsed METAR"""
        signals = []
        station = parsed.station
        
        # Check 6-hour maximum (most reliable - this is our primary signal)
        if parsed.six_hour_max_c is not None:
            temp_f_rounded = parsed.six_hour_max_f_rounded
            
            # Check if this sets a new daily high
            is_new_high = self.state.update_high(
                station, 
                temp_f_rounded, 
                source="6hr_max"
            )
            
            if is_new_high:
                # Check if we've already traded this bracket
                if not self.state.is_bracket_traded(station, temp_f_rounded, "high"):
                    signals.append(TradeSignal(
                        station=station,
                        bracket_type="high",
                        temperature_f=temp_f_rounded,
                        source="6hr_max",
                        confidence="locked",
                        raw_temp_c=parsed.six_hour_max_c,
                        raw_temp_f=parsed.six_hour_max_f
                    ))
                    logger.info(f"SIGNAL: {station} HIGH {temp_f_rounded}°F LOCKED by 6hr max")
                else:
                    logger.info(f"Already traded {station} high bracket {temp_f_rounded}°F")
        
        # Check 6-hour minimum
        if parsed.six_hour_min_c is not None:
            temp_f_rounded = parsed.six_hour_min_f_rounded
            
            is_new_low = self.state.update_low(
                station,
                temp_f_rounded,
                source="6hr_min"
            )
            
            if is_new_low:
                if not self.state.is_bracket_traded(station, temp_f_rounded, "low"):
                    signals.append(TradeSignal(
                        station=station,
                        bracket_type="low",
                        temperature_f=temp_f_rounded,
                        source="6hr_min", 
                        confidence="locked",
                        raw_temp_c=parsed.six_hour_min_c,
                        raw_temp_f=parsed.six_hour_min_f
                    ))
                    logger.info(f"SIGNAL: {station} LOW {temp_f_rounded}°F LOCKED by 6hr min")
                else:
                    logger.info(f"Already traded {station} low bracket {temp_f_rounded}°F")
        
        return signals
    
    def _get_event_ticker(self, station: str, bracket_type: str) -> Optional[str]:
        """Get the current day's event ticker for a station"""
        station_config = STATIONS.get(station, {})
        
        if bracket_type == "high":
            series_ticker = station_config.get("kalshi_high_ticker")
        else:
            series_ticker = station_config.get("kalshi_low_ticker")
        
        if not series_ticker:
            logger.warning(f"No Kalshi ticker configured for {station} {bracket_type}")
            return None
        
        # Get today's date in format used by Kalshi (e.g., 26JAN28)
        # This appears to be DDmmmYY format
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%d%b%y").upper()  # e.g., "28JAN26"
        
        event_ticker = f"{series_ticker}-{date_str}"
        return event_ticker
    
    def _get_markets_for_event(self, event_ticker: str) -> List[Dict]:
        """Get markets for an event, with caching"""
        cache_key = event_ticker
        
        # Check cache (valid for 60 seconds)
        if cache_key in self._market_cache:
            cache_age = (datetime.now(timezone.utc) - self._cache_time[cache_key]).total_seconds()
            if cache_age < 60:
                return self._market_cache[cache_key]
        
        # Fetch from API
        try:
            event_data = self.kalshi.get_event(event_ticker)
            markets = event_data.get("event", {}).get("markets", [])
            
            # Also try the separate markets field
            if not markets:
                markets = event_data.get("markets", [])
            
            self._market_cache[cache_key] = markets
            self._cache_time[cache_key] = datetime.now(timezone.utc)
            
            logger.info(f"Fetched {len(markets)} markets for {event_ticker}")
            return markets
            
        except Exception as e:
            logger.error(f"Error fetching markets for {event_ticker}: {e}")
            return []
    
    def _find_bracket_market(self, event_ticker: str, temp_f: int, bracket_type: str) -> Optional[Dict]:
        """Find the specific market for a temperature bracket"""
        markets = self._get_markets_for_event(event_ticker)
        
        if not markets:
            return None
        
        # Log available markets for debugging
        logger.info(f"Looking for {temp_f}°F bracket in {len(markets)} markets:")
        for m in markets[:10]:  # Show first 10
            logger.debug(f"  {m.get('ticker')}: {m.get('yes_sub_title')} (yes_ask: {m.get('yes_ask')})")
        
        return find_temperature_bracket(markets, temp_f, bracket_type)
    
    def _execute_trade(self, signal: TradeSignal) -> TradeResult:
        """Execute a trade based on signal"""
        logger.info(f"\n{'='*50}")
        logger.info(f"EXECUTING TRADE: {signal.station} {signal.bracket_type.upper()} {signal.temperature_f}°F")
        logger.info(f"Source: {signal.source}, Confidence: {signal.confidence}")
        logger.info(f"Raw temp: {signal.raw_temp_c}°C = {signal.raw_temp_f:.2f}°F")
        
        # Get event ticker
        event_ticker = self._get_event_ticker(signal.station, signal.bracket_type)
        if not event_ticker:
            return TradeResult(
                success=False,
                signal=signal,
                error="Could not determine event ticker"
            )
        
        logger.info(f"Event ticker: {event_ticker}")
        
        # Find the bracket market
        market = self._find_bracket_market(event_ticker, signal.temperature_f, signal.bracket_type)
        
        if not market:
            return TradeResult(
                success=False,
                signal=signal,
                error=f"Could not find market for {signal.temperature_f}°F bracket"
            )
        
        market_ticker = market.get("ticker")
        yes_ask = market.get("yes_ask")  # Price to buy YES
        yes_bid = market.get("yes_bid")
        
        logger.info(f"Found market: {market_ticker}")
        logger.info(f"  Yes Bid: {yes_bid}¢ | Yes Ask: {yes_ask}¢")
        
        # Check if price is favorable (< 90 cents)
        if yes_ask is None:
            return TradeResult(
                success=False,
                signal=signal,
                market_ticker=market_ticker,
                error="No ask price available"
            )
        
        if yes_ask > MAX_BRACKET_PRICE_CENTS:
            logger.info(f"Price {yes_ask}¢ > {MAX_BRACKET_PRICE_CENTS}¢ threshold, skipping")
            return TradeResult(
                success=False,
                signal=signal,
                market_ticker=market_ticker,
                price_paid_cents=yes_ask,
                error=f"Price too high ({yes_ask}¢ > {MAX_BRACKET_PRICE_CENTS}¢)"
            )
        
        # Calculate position size
        contracts = MAX_TRADE_AMOUNT_CENTS // yes_ask
        if contracts < 1:
            contracts = 1
        
        cost_cents = contracts * yes_ask
        potential_profit = (100 - yes_ask) * contracts  # Profit if bracket hits
        
        logger.info(f"Trade plan: BUY {contracts} YES @ {yes_ask}¢ = ${cost_cents/100:.2f}")
        logger.info(f"Potential profit: ${potential_profit/100:.2f}")
        
        if self.dry_run:
            logger.info("[DRY RUN] Would execute trade, but dry_run=True")
            self.state.mark_bracket_traded(signal.station, signal.temperature_f, signal.bracket_type)
            return TradeResult(
                success=True,
                signal=signal,
                market_ticker=market_ticker,
                price_paid_cents=yes_ask,
                contracts=contracts,
                error="DRY RUN"
            )
        
        # Execute the trade!
        try:
            order_result = self.kalshi.create_order(
                ticker=market_ticker,
                side="yes",
                action="buy",
                count=contracts,
                order_type="market"
            )
            
            logger.info(f"ORDER PLACED: {order_result}")
            
            # Mark as traded
            self.state.mark_bracket_traded(signal.station, signal.temperature_f, signal.bracket_type)
            
            return TradeResult(
                success=True,
                signal=signal,
                market_ticker=market_ticker,
                price_paid_cents=yes_ask,
                contracts=contracts
            )
            
        except Exception as e:
            logger.error(f"Order failed: {e}")
            return TradeResult(
                success=False,
                signal=signal,
                market_ticker=market_ticker,
                error=str(e)
            )


# =========== Callback for SMS webhook ===========

_sniper_instance: Optional[WXSniper] = None

def get_sniper(dry_run: bool = True) -> WXSniper:
    """Get or create sniper instance"""
    global _sniper_instance
    if _sniper_instance is None:
        _sniper_instance = WXSniper(dry_run=dry_run)
    return _sniper_instance

def metar_callback(metar_text: str, parsed: MetarTemps):
    """Callback for SMS webhook"""
    sniper = get_sniper()
    results = sniper.process_metar(metar_text)
    
    for result in results:
        if result.success:
            logger.info(f"✓ Trade executed: {result.market_ticker} x{result.contracts} @ {result.price_paid_cents}¢")
        else:
            logger.info(f"✗ Trade failed: {result.error}")


# =========== Test/Demo ===========

if __name__ == "__main__":
    print("=" * 60)
    print("WX SNIPER TEST")
    print("=" * 60)
    
    # Create sniper in dry run mode
    sniper = WXSniper(dry_run=True)
    
    # Test METAR with 6-hour groups
    test_metar = "KSFO 281853Z 29012KT 10SM FEW020 SCT200 17/08 A3012 RMK AO2 SLP203 T01720083 10189 20156 58010"
    
    print(f"\n[Test METAR]")
    print(f"{test_metar}")
    
    print(f"\n[Processing...]")
    results = sniper.process_metar(test_metar)
    
    print(f"\n[Results]")
    for result in results:
        print(f"  Success: {result.success}")
        print(f"  Signal: {result.signal.bracket_type} {result.signal.temperature_f}°F")
        print(f"  Market: {result.market_ticker}")
        print(f"  Error: {result.error}")
    
    print(f"\n[State Summary]")
    print(sniper.state.get_summary())
    
    print("\n" + "=" * 60)
    print("TICKER FORMAT DISCOVERY")
    print("=" * 60)
    
    print("\nAttempting to fetch real market data to discover ticker format...")
    
    try:
        # Try to get SFO events
        events = sniper.kalshi.get_events(series_ticker="KXHIGHTSFO", limit=3)
        print(f"\nFound {len(events)} KXHIGHTSFO events:")
        
        for event in events:
            print(f"\n  Event: {event.get('event_ticker')}")
            print(f"  Title: {event.get('title')}")
            
            markets = event.get('markets', [])
            print(f"  Markets ({len(markets)}):")
            
            for m in markets[:8]:
                print(f"    TICKER: {m.get('ticker')}")
                print(f"      Title: {m.get('yes_sub_title')}")
                print(f"      Bid/Ask: {m.get('yes_bid')}¢ / {m.get('yes_ask')}¢")
                print()
                
    except Exception as e:
        print(f"Error fetching from Kalshi: {e}")
        print("\nThis is expected if credentials aren't set up yet.")
