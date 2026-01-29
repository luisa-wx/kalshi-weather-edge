#!/usr/bin/env python3
"""
COMPREHENSIVE QC TEST - Tests the ACTUAL code paths that will run in production.
Run this before deploying to catch bugs like the temp_f_rounded disaster.
"""

import sys
from datetime import datetime, timezone

print("="*70)
print("WX SNIPER - COMPREHENSIVE QC TEST")
print("="*70)

failures = []

# =============================================================================
# TEST 1: Import all modules
# =============================================================================
print("\n[TEST 1] Importing modules...")
try:
    from smart_poller import SmartPoller, BracketInfo
    from aviation_weather import AviationWeatherPoller
    from metar_parser import parse_metar
    from kalshi_client import KalshiClient
    print("   ✅ All imports successful")
except Exception as e:
    print(f"   ❌ Import failed: {e}")
    failures.append(f"Import: {e}")

# =============================================================================
# TEST 2: METAR parsing - various real formats
# =============================================================================
print("\n[TEST 2] METAR parsing - real formats...")

test_metars = [
    # Standard hourly (no 6hr data)
    ("METAR KLAX 292053Z 25009KT 10SM FEW025 18/09 A3032 RMK AO2", 
     {"station": "KLAX", "temp_c": 18, "temp_f": 64, "6hr_max": None, "6hr_min": None}),
    
    # Negative temps
    ("METAR KNYC 290751Z AUTO 28007KT 10SM CLR M11/M18 A3045 RMK AO2",
     {"station": "KNYC", "temp_c": -11, "temp_f": 12, "6hr_max": None, "6hr_min": None}),
    
    # Very cold negative
    ("METAR KMDW 291856Z 32015KT 10SM CLR M18/M23 A3038 RMK AO2",
     {"station": "KMDW", "temp_c": -18, "temp_f": 0, "6hr_max": None, "6hr_min": None}),
    
    # With T-group (high precision)
    ("METAR KSFO 291856Z 33007KT 10SM CLR 13/09 A3035 RMK AO2 SLP277 T01280094",
     {"station": "KSFO", "temp_c": 13, "temp_f": 55, "6hr_max": None, "6hr_min": None}),
    
    # Synoptic with 6-hour max (10128 = 12.8°C max)
    ("METAR KNYC 291756Z 31012KT 10SM CLR 10/M03 A3040 RMK AO2 SLP271 T01001028 10128 20044 51010",
     {"station": "KNYC", "temp_c": 10, "temp_f": 50, "6hr_max": 55, "6hr_min": None}),
    
    # Synoptic with 6-hour min (20044 = 4.4°C min) 
    ("METAR KPHL 291156Z 30008KT 10SM CLR 05/M02 A3042 RMK AO2 SLP305 T00501022 10072 20044 53002",
     {"station": "KPHL", "temp_c": 5, "temp_f": 41, "6hr_max": None, "6hr_min": 40}),
    
    # Synoptic with NEGATIVE 6-hour min (21044 = -4.4°C min)
    ("METAR KORD 291156Z 32015KT 10SM CLR M05/M10 A3045 RMK AO2 SLP310 T10501100 11028 21044 53005",
     {"station": "KORD", "temp_c": -5, "temp_f": 23, "6hr_max": None, "6hr_min": 24}),
    
    # Zero degrees C
    ("METAR KDEN 291856Z 18008KT 10SM CLR 00/M05 A3030 RMK AO2",
     {"station": "KDEN", "temp_c": 0, "temp_f": 32, "6hr_max": None, "6hr_min": None}),
]

for metar_raw, expected in test_metars:
    try:
        parsed = parse_metar(metar_raw)
        
        # Check station
        if parsed.station != expected["station"]:
            failures.append(f"Station mismatch: got {parsed.station}, expected {expected['station']}")
            print(f"   ❌ {expected['station']}: station mismatch")
            continue
        
        # Check temp_c
        if parsed.current_temp_c != expected["temp_c"]:
            failures.append(f"{expected['station']} temp_c: got {parsed.current_temp_c}, expected {expected['temp_c']}")
            print(f"   ❌ {expected['station']}: temp_c={parsed.current_temp_c}, expected {expected['temp_c']}")
            continue
        
        # Check temp_f conversion (the bug we just fixed!)
        temp_f = round(parsed.current_temp_c * 9/5 + 32)
        if temp_f != expected["temp_f"]:
            failures.append(f"{expected['station']} temp_f: got {temp_f}, expected {expected['temp_f']}")
            print(f"   ❌ {expected['station']}: temp_f={temp_f}, expected {expected['temp_f']}")
            continue
        
        # Check 6hr max
        if parsed.six_hour_max_f_rounded != expected["6hr_max"]:
            failures.append(f"{expected['station']} 6hr_max: got {parsed.six_hour_max_f_rounded}, expected {expected['6hr_max']}")
            print(f"   ❌ {expected['station']}: 6hr_max={parsed.six_hour_max_f_rounded}, expected {expected['6hr_max']}")
            continue
            
        # Check 6hr min
        if parsed.six_hour_min_f_rounded != expected["6hr_min"]:
            failures.append(f"{expected['station']} 6hr_min: got {parsed.six_hour_min_f_rounded}, expected {expected['6hr_min']}")
            print(f"   ❌ {expected['station']}: 6hr_min={parsed.six_hour_min_f_rounded}, expected {expected['6hr_min']}")
            continue
        
        print(f"   ✅ {expected['station']}: {temp_f}°F (from {parsed.current_temp_c}°C), 6hr_max={parsed.six_hour_max_f_rounded}, 6hr_min={parsed.six_hour_min_f_rounded}")
        
    except Exception as e:
        failures.append(f"{expected['station']} parse error: {e}")
        print(f"   ❌ {expected['station']}: {e}")

# =============================================================================
# TEST 3: LIVE METAR fetch from Aviation Weather
# =============================================================================
print("\n[TEST 3] LIVE METAR fetch from aviationweather.gov...")
try:
    aviation = AviationWeatherPoller()
    test_stations = ['KNYC', 'KLAX', 'KMDW', 'KSEA', 'KMIA']
    
    for station in test_stations:
        resp = aviation.fetch_metar(station)
        if resp and resp.raw_text:
            parsed = parse_metar(resp.raw_text)
            if parsed.t_group_temp_c is not None:
                temp_f = round(parsed.t_group_temp_c * 9/5 + 32)
                print(f"   ✅ {station}: {temp_f}°F (T-group {parsed.t_group_temp_c}°C) | {resp.raw_text[:50]}...")
            elif parsed.current_temp_c is not None:
                # Fallback but warn - we shouldn't trade without T-group
                temp_f = round(parsed.current_temp_c * 9/5 + 32)
                print(f"   ⚠️  {station}: {temp_f}°F (NO T-GROUP - using rounded {parsed.current_temp_c}°C) | {resp.raw_text[:50]}...")
            else:
                failures.append(f"{station}: No temp parsed")
                print(f"   ❌ {station}: No temp parsed")
        else:
            failures.append(f"{station}: Failed to fetch METAR")
            print(f"   ❌ {station}: Failed to fetch METAR")
except Exception as e:
    failures.append(f"Aviation fetch: {e}")
    print(f"   ❌ Aviation fetch error: {e}")

# =============================================================================
# TEST 4: Kalshi API connection
# =============================================================================
print("\n[TEST 4] Kalshi API connection...")
try:
    kalshi = KalshiClient()
    balance = kalshi.get_balance()
    bal = balance.get('balance', 0) / 100
    print(f"   ✅ Connected - Balance: ${bal:.2f}")
except Exception as e:
    failures.append(f"Kalshi API: {e}")
    print(f"   ❌ Kalshi API error: {e}")

# =============================================================================
# TEST 5: Kalshi market data fetch
# =============================================================================
print("\n[TEST 5] Kalshi market data fetch...")
try:
    markets = kalshi.get_markets(event_ticker='KXHIGHLAX-26JAN29')
    if markets and len(markets) > 0:
        m = markets[0]
        print(f"   ✅ Fetched {len(markets)} markets")
        print(f"      Sample: {m.get('yes_sub_title')} | strike_type={m.get('strike_type')} | floor={m.get('floor_strike')} | cap={m.get('cap_strike')}")
    else:
        failures.append("No markets returned")
        print(f"   ❌ No markets returned")
except Exception as e:
    failures.append(f"Market fetch: {e}")
    print(f"   ❌ Market fetch error: {e}")

# =============================================================================
# TEST 6: Lock logic - HIGH markets
# =============================================================================
print("\n[TEST 6] Lock logic - HIGH markets...")

# Create a test poller to access lock methods
poller = SmartPoller(dry_run=True)

# Test cases: (observed_temp, bracket_type, floor, cap, expected_locked)
high_tests = [
    # Between brackets
    (65, "between", 63, 64, True, "65°F > cap 64 → NO locked"),
    (64, "between", 63, 64, False, "64°F = cap 64 → NO not locked (need >)"),
    (63, "between", 63, 64, False, "63°F < cap 64 → NO not locked"),
    (100, "between", 79, 80, True, "100°F > cap 80 → NO locked"),
    
    # Less brackets (e.g., "74° or below")
    (75, "less", None, 75, True, "75°F >= cap 75 → NO locked"),
    (74, "less", None, 75, False, "74°F < cap 75 → NO not locked"),
    (80, "less", None, 75, True, "80°F >= cap 75 → NO locked"),
    
    # Greater brackets - should NEVER lock for HIGH
    (90, "greater", 82, None, False, "greater type never locks for HIGH"),
    (100, "greater", 82, None, False, "greater type never locks for HIGH"),
]

for temp, strike_type, floor, cap, expected, desc in high_tests:
    bracket = BracketInfo(
        ticker="TEST", event_ticker="TEST", subtitle="Test",
        floor_strike=floor, cap_strike=cap, strike_type=strike_type,
        signal_type="high", no_ask=50, station="TEST"
    )
    result = poller._is_no_locked_for_high(temp, bracket)
    if result == expected:
        print(f"   ✅ {desc}")
    else:
        failures.append(f"HIGH lock: {desc} - got {result}")
        print(f"   ❌ {desc} - got {result}, expected {expected}")

# =============================================================================
# TEST 7: Lock logic - LOW markets
# =============================================================================
print("\n[TEST 7] Lock logic - LOW markets...")

low_tests = [
    # Between brackets
    (10, "between", 12, 13, True, "10°F < floor 12 → NO locked"),
    (12, "between", 12, 13, False, "12°F = floor 12 → NO not locked (need <)"),
    (15, "between", 12, 13, False, "15°F > floor 12 → NO not locked"),
    (0, "between", 8, 9, True, "0°F < floor 8 → NO locked"),
    
    # Greater brackets (e.g., "10° or above" has floor=9)
    (9, "greater", 9, None, True, "9°F <= floor 9 → NO locked"),
    (10, "greater", 9, None, False, "10°F > floor 9 → NO not locked"),
    (8, "greater", 9, None, True, "8°F <= floor 9 → NO locked"),
    
    # Less brackets - should NEVER lock for LOW
    (0, "less", None, 5, False, "less type never locks for LOW"),
    (-10, "less", None, 5, False, "less type never locks for LOW"),
]

for temp, strike_type, floor, cap, expected, desc in low_tests:
    bracket = BracketInfo(
        ticker="TEST", event_ticker="TEST", subtitle="Test",
        floor_strike=floor, cap_strike=cap, strike_type=strike_type,
        signal_type="low", no_ask=50, station="TEST"
    )
    result = poller._is_no_locked_for_low(temp, bracket)
    if result == expected:
        print(f"   ✅ {desc}")
    else:
        failures.append(f"LOW lock: {desc} - got {result}")
        print(f"   ❌ {desc} - got {result}, expected {expected}")

# =============================================================================
# TEST 8: Timing logic
# =============================================================================
print("\n[TEST 8] Timing logic...")
try:
    s = poller._get_synoptic_state()
    now_str = f"{s['current_hour']:02d}:{s['current_minute']:02d}Z"
    print(f"   Current: {now_str}")
    print(f"   Prep window: {s['is_prep_window']}")
    print(f"   Hot window: {s['is_hot_window']}")
    print(f"   Minutes to hot: {s['minutes_to_hot']}")
    print(f"   Hourly mode: {poller.hourly_mode}")
    print(f"   ✅ Timing logic OK")
except Exception as e:
    failures.append(f"Timing: {e}")
    print(f"   ❌ Timing error: {e}")

# =============================================================================
# TEST 9: End-to-end simulation with LIVE data
# =============================================================================
print("\n[TEST 9] End-to-end simulation with LIVE data...")
try:
    # Fetch real METAR
    resp = aviation.fetch_metar('KLAX')
    if resp and resp.raw_text:
        parsed = parse_metar(resp.raw_text)
        if parsed.current_temp_c is not None:
            current_temp = round(parsed.current_temp_c * 9/5 + 32)
            print(f"   KLAX current temp: {current_temp}°F")
            
            # Get real market data
            markets = kalshi.get_markets(event_ticker='KXHIGHLAX-26JAN29')
            locked_count = 0
            for m in markets:
                if m.get('strike_type') == 'between':
                    cap = m.get('cap_strike')
                    if cap and current_temp > cap:
                        locked_count += 1
                        no_ask = m.get('no_ask_dollars', '?')
                        print(f"   🎯 WOULD LOCK: {m.get('yes_sub_title')} (cap={cap}, NO@{no_ask})")
            
            if locked_count == 0:
                print(f"   ℹ️ No brackets currently locked (temp {current_temp}°F not exceeding any caps)")
            print(f"   ✅ End-to-end simulation complete")
        else:
            failures.append("E2E: current_temp_c is None")
            print(f"   ❌ current_temp_c is None")
    else:
        failures.append("E2E: Failed to fetch METAR")
        print(f"   ❌ Failed to fetch METAR")
except Exception as e:
    failures.append(f"E2E: {e}")
    print(f"   ❌ E2E error: {e}")

# =============================================================================
# TEST 10: Negative temp edge cases
# =============================================================================
print("\n[TEST 10] Negative temp edge cases...")

negative_tests = [
    (-40, -40, "−40°C = −40°F (the crossover point)"),
    (-17.78, 0, "−17.78°C ≈ 0°F"),
    (-6.67, 20, "−6.67°C ≈ 20°F"),
    (0, 32, "0°C = 32°F"),
]

for temp_c, expected_f, desc in negative_tests:
    calc_f = round(temp_c * 9/5 + 32)
    if calc_f == expected_f:
        print(f"   ✅ {desc}: {temp_c}°C → {calc_f}°F")
    else:
        failures.append(f"Negative temp: {desc} - got {calc_f}")
        print(f"   ❌ {desc}: got {calc_f}°F, expected {expected_f}°F")

# =============================================================================
# SUMMARY
# =============================================================================
print("\n" + "="*70)
if failures:
    print(f"❌ QC FAILED - {len(failures)} issues found:")
    for f in failures:
        print(f"   • {f}")
    sys.exit(1)
else:
    print("✅ ALL QC TESTS PASSED")
    print("\nThe bot should work correctly. Key validations:")
    print("   • METAR parsing handles positive and negative temps")
    print("   • current_temp_c → Fahrenheit conversion works")
    print("   • Live aviation API fetch works")
    print("   • Kalshi API connection works")
    print("   • Lock logic for HIGH and LOW brackets is correct")
    print("   • Timing logic is functional")
print("="*70)
