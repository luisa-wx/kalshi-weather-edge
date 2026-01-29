#!/usr/bin/env python3
"""
QC Test for NO-Only Trading Strategy

Tests the bracket logic against LIVE Kalshi market data with simulated temperatures.
Run this BEFORE going live to verify the logic is correct.
"""

import sys
import os

# Add parent dir if running from wx-sniper folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kalshi_client import KalshiClient
from trader import WXSniper
import requests
import time
import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding


def fetch_markets_direct(event_ticker: str) -> list:
    """Fetch markets directly from API (bypassing any caching)"""
    c = KalshiClient()
    ts = int(time.time() * 1000)
    path = f"/trade-api/v2/markets?event_ticker={event_ticker}"
    msg = f"{ts}GET{path}"
    sig = c.private_key.sign(
        msg.encode(), 
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), 
        hashes.SHA256()
    )
    r = requests.get(
        f"https://api.elections.kalshi.com{path}",
        headers={
            "KALSHI-ACCESS-KEY": c.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": str(ts)
        }
    )
    return r.json().get('markets', [])


def display_market_analysis(markets: list, observed_temp: int, signal_type: str):
    """Display which brackets have locked NOs"""
    sniper = WXSniper(dry_run=True)
    
    print(f"\n{'='*80}")
    print(f"ANALYSIS: {signal_type.upper()} market, observed temp = {observed_temp}°F")
    print(f"{'='*80}")
    print(f"{'Bracket':<20} {'floor':>6} {'cap':>6} {'type':<10} {'NO_ask':>8} {'Locked?':<10} {'Reason'}")
    print(f"{'-'*80}")
    
    for m in sorted(markets, key=lambda x: (x.get('floor_strike') or 0, x.get('cap_strike') or 999)):
        ticker = m.get('ticker', '')
        floor = m.get('floor_strike')
        cap = m.get('cap_strike')
        strike_type = m.get('strike_type', '')
        yes_sub = m.get('yes_sub_title', '')
        no_ask = sniper._parse_price(m.get('no_ask'), m.get('no_ask_dollars'))
        
        # Test the _is_no_locked function
        is_locked, reason = sniper._is_no_locked(
            signal_type=signal_type,
            observed_temp=observed_temp,
            floor_strike=floor,
            cap_strike=cap,
            strike_type=strike_type
        )
        
        lock_str = "✅ YES" if is_locked else "❌ NO"
        price_str = f"{no_ask}¢" if no_ask else "N/A"
        
        # Would we trade this?
        would_trade = ""
        if is_locked and no_ask and no_ask <= 90:
            would_trade = f" → WOULD BUY @ {no_ask}¢ (edge: {100-no_ask}¢)"
        elif is_locked and no_ask and no_ask > 90:
            would_trade = f" → TOO EXPENSIVE ({no_ask}¢ > 90¢)"
        
        print(f"{yes_sub:<20} {str(floor):>6} {str(cap):>6} {strike_type:<10} {price_str:>8} {lock_str:<10} {reason[:25]}{would_trade}")


def test_scenario(event_ticker: str, temps_to_test: list, signal_type: str):
    """Test multiple temperature scenarios against live market data"""
    print(f"\n{'#'*80}")
    print(f"# FETCHING LIVE DATA: {event_ticker}")
    print(f"{'#'*80}")
    
    markets = fetch_markets_direct(event_ticker)
    
    if not markets:
        print(f"ERROR: No markets found for {event_ticker}")
        return
    
    print(f"Found {len(markets)} brackets")
    
    # Show raw market data first
    print(f"\nRAW MARKET DATA:")
    print(f"{'-'*80}")
    for m in sorted(markets, key=lambda x: (x.get('floor_strike') or 0, x.get('cap_strike') or 999)):
        yes_sub = m.get('yes_sub_title', '')
        floor = m.get('floor_strike')
        cap = m.get('cap_strike')
        strike_type = m.get('strike_type', '')
        yes_ask = m.get('yes_ask')
        no_ask = m.get('no_ask')
        print(f"  {yes_sub:<20} floor={floor}, cap={cap}, type={strike_type}, YES={yes_ask}, NO={no_ask}")
    
    # Test each temperature scenario
    for temp in temps_to_test:
        display_market_analysis(markets, temp, signal_type)


def run_full_qc():
    """Run comprehensive QC tests"""
    print("="*80)
    print("WX SNIPER v10 - NO-ONLY STRATEGY QC TEST")
    print("="*80)
    print("\nThis test fetches LIVE market data and simulates various temperature scenarios")
    print("to verify our bracket logic is correct.\n")
    
    # Get today's date in Kalshi format
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    
    now_et = datetime.now(ZoneInfo('America/New_York'))
    date_str = now_et.strftime("%y%b%d").upper()  # e.g., "26JAN29"
    
    print(f"Today's date (ET): {date_str}")
    print(f"Current time (ET): {now_et.strftime('%H:%M:%S')}")
    
    # ========== NYC LOW TESTS ==========
    print("\n" + "="*80)
    print("TEST 1: NYC LOW MARKETS")
    print("="*80)
    
    # Test scenarios for LOW:
    # - If observed min is 7°F, brackets 8-9 and 10+ should have locked NOs
    # - If observed min is 5°F, brackets 6-7, 8-9, and 10+ should have locked NOs
    test_scenario(
        event_ticker=f"KXLOWTNYC-{date_str}",
        temps_to_test=[10, 8, 7, 5, 3],  # Various low scenarios
        signal_type='low'
    )
    
    # ========== NYC HIGH TESTS ==========
    print("\n" + "="*80)
    print("TEST 2: NYC HIGH MARKETS")
    print("="*80)
    
    # Test scenarios for HIGH:
    # - If observed max is 22°F, brackets 19-20 should have locked NOs
    # - If observed max is 25°F, brackets 19-20, 21-22, 23-24 should have locked NOs
    test_scenario(
        event_ticker=f"KXHIGHNY-{date_str}",
        temps_to_test=[20, 22, 24, 26, 28],  # Various high scenarios
        signal_type='high'
    )
    
    # ========== LOGIC UNIT TESTS ==========
    print("\n" + "="*80)
    print("TEST 3: UNIT TESTS FOR _is_no_locked()")
    print("="*80)
    
    sniper = WXSniper(dry_run=True)
    
    tests = [
        # (signal_type, observed, floor, cap, strike_type, expected_locked, description)
        
        # LOW market - between brackets
        ('low', 7, 8, 9, 'between', True, "LOW: 7°F < floor 8, NO locked"),
        ('low', 7, 6, 7, 'between', False, "LOW: 7°F in range 6-7, NOT locked"),
        ('low', 7, 4, 5, 'between', False, "LOW: 7°F > cap 5 but could drop, NOT locked"),
        
        # LOW market - greater (T9 style: "10° or above")
        ('low', 7, 9, None, 'greater', True, "LOW: 7°F <= floor 9, NO locked"),
        ('low', 10, 9, None, 'greater', False, "LOW: 10°F > floor 9, NOT locked"),
        
        # LOW market - less (T2 style: "1° or below")
        ('low', 7, None, 2, 'less', False, "LOW: temp could still drop below 2"),
        ('low', 0, None, 2, 'less', False, "LOW: even at 0°F, temp could drop more"),
        
        # HIGH market - between brackets
        ('high', 25, 21, 22, 'between', True, "HIGH: 25°F > cap 22, NO locked"),
        ('high', 22, 21, 22, 'between', False, "HIGH: 22°F in range, NOT locked"),
        ('high', 20, 21, 22, 'between', False, "HIGH: 20°F < floor, could still rise"),
        
        # HIGH market - less (T19 style: "18° or below")
        ('high', 22, None, 19, 'less', True, "HIGH: 22°F >= cap 19, NO locked"),
        ('high', 18, None, 19, 'less', False, "HIGH: 18°F < cap 19, NOT locked"),
        
        # HIGH market - greater (T26 style: "27° or above")
        ('high', 22, 26, None, 'greater', False, "HIGH: could still exceed 26"),
        ('high', 30, 26, None, 'greater', False, "HIGH: temp rising doesn't lock NO"),
    ]
    
    passed = 0
    failed = 0
    
    for signal_type, observed, floor, cap, strike_type, expected, desc in tests:
        is_locked, reason = sniper._is_no_locked(signal_type, observed, floor, cap, strike_type)
        
        if is_locked == expected:
            print(f"  ✅ PASS: {desc}")
            passed += 1
        else:
            print(f"  ❌ FAIL: {desc}")
            print(f"         Expected locked={expected}, got locked={is_locked}")
            print(f"         Reason: {reason}")
            failed += 1
    
    print(f"\n  Results: {passed} passed, {failed} failed")
    
    # ========== SUMMARY ==========
    print("\n" + "="*80)
    print("QC SUMMARY")
    print("="*80)
    print("""
KEY RULES VERIFIED:
1. For LOW markets: NO is locked when observed_temp < floor_strike
2. For HIGH markets: NO is locked when observed_temp > cap_strike
3. We NEVER lock NO on 'greater' for HIGH (temp could keep rising)
4. We NEVER lock NO on 'less' for LOW (temp could keep dropping)

NEXT STEPS:
1. Verify the locked brackets match your intuition
2. Check prices - are locked NOs reasonably priced?
3. If all looks good, test a real trade with --execute-trade
""")
    
    return failed == 0


if __name__ == "__main__":
    success = run_full_qc()
    sys.exit(0 if success else 1)
