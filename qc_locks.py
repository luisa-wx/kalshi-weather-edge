#!/usr/bin/env python3
"""
WX Sniper - Lock Override QC Tool

Tests how lock overrides interact with brackets using:
a) Real hourly METARs (pulled live)
b) Simulated 6-hour METARs

Run: python qc_locks.py
"""

import os
import sys
import json
import math
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple

# ============================================================
# CONFIGURATION
# ============================================================

# Run this script from the same directory as smart_poller.py
# so it can read the lock_overrides.json file
OVERRIDES_FILE = "lock_overrides.json"

STATION_TIMEZONES = {
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

# Sample brackets (you can modify these to match your watchlist)
SAMPLE_BRACKETS = {
    'KNYC': {
        'high': [
            {'subtitle': '19° to 20°', 'floor': 19, 'cap': 20, 'type': 'between', 'no_ask': 76},
            {'subtitle': '17° to 18°', 'floor': 17, 'cap': 18, 'type': 'between', 'no_ask': 40},
            {'subtitle': '15° to 16°', 'floor': 15, 'cap': 16, 'type': 'between', 'no_ask': 91},
        ],
        'low': [
            {'subtitle': '4° or below', 'floor': None, 'cap': 4, 'type': 'less', 'no_ask': 91},
            {'subtitle': '9° to 10°', 'floor': 9, 'cap': 10, 'type': 'between', 'no_ask': 77},
            {'subtitle': '7° to 8°', 'floor': 7, 'cap': 8, 'type': 'between', 'no_ask': 82},
            {'subtitle': '5° to 6°', 'floor': 5, 'cap': 6, 'type': 'between', 'no_ask': 87},
            {'subtitle': '11° to 12°', 'floor': 11, 'cap': 12, 'type': 'between', 'no_ask': 84},
        ]
    },
    'KDCA': {
        'high': [
            {'subtitle': '26° to 27°', 'floor': 26, 'cap': 27, 'type': 'between', 'no_ask': 85},
            {'subtitle': '24° to 25°', 'floor': 24, 'cap': 25, 'type': 'between', 'no_ask': 72},
            {'subtitle': '22° to 23°', 'floor': 22, 'cap': 23, 'type': 'between', 'no_ask': 55},
            {'subtitle': '20° to 21°', 'floor': 20, 'cap': 21, 'type': 'between', 'no_ask': 91},
        ],
        'low': []
    },
    'KDEN': {
        'high': [
            {'subtitle': '47° to 48°', 'floor': 47, 'cap': 48, 'type': 'between', 'no_ask': 1},
        ],
        'low': [
            {'subtitle': '21° or below', 'floor': None, 'cap': 21, 'type': 'less', 'no_ask': 1},
        ]
    },
    'KMIA': {
        'high': [
            {'subtitle': '73° to 74°', 'floor': 73, 'cap': 74, 'type': 'between', 'no_ask': 90},
            {'subtitle': '71° to 72°', 'floor': 71, 'cap': 72, 'type': 'between', 'no_ask': 73},
            {'subtitle': '69° to 70°', 'floor': 69, 'cap': 70, 'type': 'between', 'no_ask': 63},
        ],
        'low': [
            {'subtitle': '62° to 63°', 'floor': 62, 'cap': 63, 'type': 'between', 'no_ask': 90},
            {'subtitle': '60° to 61°', 'floor': 60, 'cap': 61, 'type': 'between', 'no_ask': 70},
        ]
    }
}

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def nws_round(value: float) -> int:
    """NWS round half up asymmetric"""
    return math.floor(value + 0.5)


def load_overrides() -> dict:
    """Load lock overrides from file."""
    if os.path.exists(OVERRIDES_FILE):
        try:
            with open(OVERRIDES_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def get_local_time(tz_name: str) -> datetime:
    """Get current local time for a timezone."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz_name))


def check_override_active(station: str, signal_type: str) -> Tuple[bool, str]:
    """
    Check if a lock override is active for a station/signal.
    Returns (is_active, reason)
    """
    overrides = load_overrides()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    station_overrides = overrides.get(today, {}).get(station, {})
    override_time_str = station_overrides.get(f"{signal_type}_locked_after")
    
    if not override_time_str:
        return False, "No override set"
    
    try:
        override_hour, override_min = map(int, override_time_str.split(':'))
    except:
        return False, f"Invalid override format: {override_time_str}"
    
    tz = STATION_TIMEZONES.get(station, 'America/New_York')
    local_now = get_local_time(tz)
    current_minutes = local_now.hour * 60 + local_now.minute
    override_minutes = override_hour * 60 + override_min
    
    if current_minutes >= override_minutes:
        return True, f"Override active: {override_time_str} local, now {local_now.strftime('%H:%M')} local"
    else:
        mins_until = override_minutes - current_minutes
        return False, f"Override set for {override_time_str} local ({mins_until} mins away)"


def is_no_locked_for_high(observed: int, floor: int, cap: int, strike_type: str, soft: bool = False) -> Tuple[bool, str]:
    """Check if NO is locked for a HIGH bracket."""
    if strike_type == 'between':
        if cap is None:
            return False, "No cap defined"
        if observed > cap:
            return True, f"HARD LOCK: observed {observed} > cap {cap}"
        if soft and observed == cap:
            return True, f"SOFT LOCK: observed {observed} == cap {cap} (override active)"
        return False, f"Not locked: observed {observed} <= cap {cap}"
    elif strike_type == 'less':
        if cap is None:
            return False, "No cap defined"
        if observed >= cap:
            return True, f"HARD LOCK: observed {observed} >= cap {cap}"
        return False, f"Not locked: observed {observed} < cap {cap}"
    elif strike_type == 'greater':
        return False, "Never locks for HIGH (temp can keep rising)"
    return False, f"Unknown strike_type: {strike_type}"


def is_no_locked_for_low(observed: int, floor: int, cap: int, strike_type: str, soft: bool = False) -> Tuple[bool, str]:
    """Check if NO is locked for a LOW bracket."""
    if strike_type == 'between':
        if floor is None:
            return False, "No floor defined"
        if observed < floor:
            return True, f"HARD LOCK: observed {observed} < floor {floor}"
        if soft and observed == floor:
            return True, f"SOFT LOCK: observed {observed} == floor {floor} (override active)"
        return False, f"Not locked: observed {observed} >= floor {floor}"
    elif strike_type == 'greater':
        if floor is None:
            return False, "No floor defined"
        if observed <= floor:
            return True, f"HARD LOCK: observed {observed} <= floor {floor}"
        return False, f"Not locked: observed {observed} > floor {floor}"
    elif strike_type == 'less':
        return False, "Never locks for LOW (temp can keep dropping)"
    return False, f"Unknown strike_type: {strike_type}"


def fetch_metar(station: str) -> Optional[str]:
    """Fetch current METAR from aviationweather.gov"""
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=raw"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.read().decode('utf-8').strip()
    except Exception as e:
        print(f"  ⚠️  Could not fetch METAR for {station}: {e}")
        return None


def parse_t_group(metar: str) -> Optional[float]:
    """Extract T-group temperature from METAR (in Celsius)."""
    import re
    match = re.search(r'T(\d)(\d{3})(\d)(\d{3})', metar)
    if match:
        temp_sign = int(match.group(1))
        temp_tenths = int(match.group(2))
        temp_c = temp_tenths / 10.0
        if temp_sign == 1:
            temp_c = -temp_c
        return temp_c
    return None


def parse_6hr_groups(metar: str) -> Tuple[Optional[float], Optional[float]]:
    """Extract 6-hour max/min from METAR (in Celsius)."""
    import re
    max_c = None
    min_c = None
    
    # 6-hour max: 1snTTT
    match = re.search(r'\s1(\d)(\d{3})(?:\s|$)', metar)
    if match:
        sign = int(match.group(1))
        tenths = int(match.group(2))
        max_c = tenths / 10.0
        if sign == 1:
            max_c = -max_c
    
    # 6-hour min: 2snTTT
    match = re.search(r'\s2(\d)(\d{3})(?:\s|$)', metar)
    if match:
        sign = int(match.group(1))
        tenths = int(match.group(2))
        min_c = tenths / 10.0
        if sign == 1:
            min_c = -min_c
    
    return max_c, min_c


# ============================================================
# MAIN QC FUNCTIONS
# ============================================================

def show_current_overrides():
    """Display all current lock overrides."""
    print("\n" + "=" * 70)
    print("CURRENT LOCK OVERRIDES")
    print("=" * 70)
    
    overrides = load_overrides()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_overrides = overrides.get(today, {})
    
    if not today_overrides:
        print(f"\nNo overrides set for {today}")
        print("Set them via the web UI at http://localhost:8080/config")
        return
    
    print(f"\nOverrides for {today}:")
    print(f"{'Station':<10} {'TZ':<20} {'High Override':<15} {'Low Override':<15}")
    print("-" * 70)
    
    for station, settings in today_overrides.items():
        tz = STATION_TIMEZONES.get(station, 'America/New_York').split('/')[-1]
        high = settings.get('high_locked_after') or '-'
        low = settings.get('low_locked_after') or '-'
        print(f"{station:<10} {tz:<20} {high:<15} {low:<15}")


def test_with_real_metars():
    """Test lock behavior with real METARs."""
    print("\n" + "=" * 70)
    print("TESTING WITH REAL HOURLY METARS")
    print("=" * 70)
    
    stations_to_test = ['KNYC', 'KDCA', 'KDEN', 'KMIA']
    
    for station in stations_to_test:
        if station not in SAMPLE_BRACKETS:
            continue
            
        print(f"\n{'─' * 70}")
        print(f"STATION: {station}")
        print(f"{'─' * 70}")
        
        # Fetch real METAR
        metar = fetch_metar(station)
        if not metar:
            continue
        
        print(f"METAR: {metar[:80]}...")
        
        # Parse T-group
        temp_c = parse_t_group(metar)
        if temp_c is None:
            print("  ⚠️  No T-group found in METAR")
            continue
        
        temp_f = nws_round(temp_c * 9/5 + 32)
        print(f"T-group: {temp_c}°C → {temp_f}°F")
        
        # Check 6-hour groups
        max_c, min_c = parse_6hr_groups(metar)
        if max_c:
            max_f = nws_round(max_c * 9/5 + 32)
            print(f"6hr MAX: {max_c}°C → {max_f}°F")
        if min_c:
            min_f = nws_round(min_c * 9/5 + 32)
            print(f"6hr MIN: {min_c}°C → {min_f}°F")
        
        # Check override status
        high_override, high_reason = check_override_active(station, 'high')
        low_override, low_reason = check_override_active(station, 'low')
        
        print(f"\nHigh override: {'🟢 ACTIVE' if high_override else '⚪ inactive'} - {high_reason}")
        print(f"Low override:  {'🟢 ACTIVE' if low_override else '⚪ inactive'} - {low_reason}")
        
        # Test HIGH brackets
        if SAMPLE_BRACKETS[station].get('high'):
            print(f"\n  HIGH BRACKETS (observed temp = {temp_f}°F as minimum daily high):")
            for b in SAMPLE_BRACKETS[station]['high']:
                locked, reason = is_no_locked_for_high(
                    temp_f, b['floor'], b['cap'], b['type'], soft=high_override
                )
                status = "🔒 TRADE" if locked else "⏳ wait"
                print(f"    {status} {b['subtitle']:<15} NO@{b['no_ask']:>2}¢  {reason}")
        
        # Test LOW brackets
        if SAMPLE_BRACKETS[station].get('low'):
            print(f"\n  LOW BRACKETS (observed temp = {temp_f}°F as maximum daily low):")
            for b in SAMPLE_BRACKETS[station]['low']:
                locked, reason = is_no_locked_for_low(
                    temp_f, b['floor'], b['cap'], b['type'], soft=low_override
                )
                status = "🔒 TRADE" if locked else "⏳ wait"
                print(f"    {status} {b['subtitle']:<15} NO@{b['no_ask']:>2}¢  {reason}")


def test_with_simulated_6hr():
    """Test lock behavior with simulated 6-hour groups."""
    print("\n" + "=" * 70)
    print("TESTING WITH SIMULATED 6-HOUR METARS")
    print("=" * 70)
    
    # Simulate scenarios
    scenarios = [
        {
            'station': 'KNYC',
            'desc': 'Morning 6hr report - cold night',
            '6hr_max_f': 22,  # Overnight "high" was 22°F
            '6hr_min_f': 12,  # Overnight low was 12°F
        },
        {
            'station': 'KNYC',
            'desc': 'Afternoon 6hr report - warmed up',
            '6hr_max_f': 35,  # Afternoon high reached 35°F
            '6hr_min_f': 18,  # Morning low was 18°F
        },
        {
            'station': 'KDCA',
            'desc': 'Midday 6hr report',
            '6hr_max_f': 28,  # High so far is 28°F
            '6hr_min_f': 20,  # Low so far is 20°F
        },
        {
            'station': 'KMIA',
            'desc': 'Warm day in Miami',
            '6hr_max_f': 75,  # High reached 75°F
            '6hr_min_f': 65,  # Low was 65°F
        },
    ]
    
    for scenario in scenarios:
        station = scenario['station']
        if station not in SAMPLE_BRACKETS:
            continue
        
        print(f"\n{'─' * 70}")
        print(f"SCENARIO: {scenario['desc']}")
        print(f"Station: {station}")
        print(f"6hr MAX: {scenario['6hr_max_f']}°F, 6hr MIN: {scenario['6hr_min_f']}°F")
        print(f"{'─' * 70}")
        
        # Check override status
        high_override, high_reason = check_override_active(station, 'high')
        low_override, low_reason = check_override_active(station, 'low')
        
        print(f"High override: {'🟢 ACTIVE' if high_override else '⚪ inactive'}")
        print(f"Low override:  {'🟢 ACTIVE' if low_override else '⚪ inactive'}")
        
        # Test HIGH brackets with 6hr MAX
        max_f = scenario['6hr_max_f']
        if SAMPLE_BRACKETS[station].get('high'):
            print(f"\n  HIGH BRACKETS (6hr max = {max_f}°F):")
            for b in SAMPLE_BRACKETS[station]['high']:
                locked, reason = is_no_locked_for_high(
                    max_f, b['floor'], b['cap'], b['type'], soft=high_override
                )
                status = "🔒 TRADE" if locked else "⏳ wait"
                print(f"    {status} {b['subtitle']:<15} NO@{b['no_ask']:>2}¢  {reason}")
        
        # Test LOW brackets with 6hr MIN
        min_f = scenario['6hr_min_f']
        if SAMPLE_BRACKETS[station].get('low'):
            print(f"\n  LOW BRACKETS (6hr min = {min_f}°F):")
            for b in SAMPLE_BRACKETS[station]['low']:
                locked, reason = is_no_locked_for_low(
                    min_f, b['floor'], b['cap'], b['type'], soft=low_override
                )
                status = "🔒 TRADE" if locked else "⏳ wait"
                print(f"    {status} {b['subtitle']:<15} NO@{b['no_ask']:>2}¢  {reason}")


def explain_soft_locks():
    """Explain how soft locks (overrides) work."""
    print("\n" + "=" * 70)
    print("HOW SOFT LOCKS (OVERRIDES) WORK")
    print("=" * 70)
    print("""
HARD LOCK (no override needed):
  - HIGH bracket: observed > cap  →  NO is locked
  - LOW bracket:  observed < floor →  NO is locked
  
SOFT LOCK (requires override to be active):
  - HIGH bracket: observed == cap AND override active  →  NO is locked
  - LOW bracket:  observed == floor AND override active →  NO is locked

EXAMPLE - KNYC HIGH bracket "19° to 20°" (cap=20):
  
  Without override:
    - Temp 21°F: HARD LOCK (21 > 20) → Trade NO
    - Temp 20°F: NOT locked (20 is not > 20) → Don't trade
    - Temp 19°F: NOT locked → Don't trade
  
  With override active (e.g., set to 14:00 and it's now 15:00):
    - Temp 21°F: HARD LOCK (21 > 20) → Trade NO
    - Temp 20°F: SOFT LOCK (20 == 20 + override) → Trade NO
    - Temp 19°F: NOT locked (19 < 20, not ==) → Don't trade

WHY USE SOFT LOCKS?
  
  At 14:00, if the temp is exactly 20°F:
  - Without override: Bot won't trade (20 is not > 20)
  - But you KNOW the high won't go higher (it's late afternoon)
  - So you set override to 14:00 to trigger trade on == case
  
  This is useful when you're confident the daily extreme is "settled"
  but the temp is sitting exactly on the boundary.

RISK OF SOFT LOCKS:
  
  If you set override too early:
  - Temp is 20°F at 12:00, override triggers trade
  - But temp rises to 21°F at 14:00
  - Your trade was correct! (final high > cap, NO wins)
  
  But what if:
  - Temp is 20°F at 12:00, override triggers trade
  - Temp stays at 20°F all day (final high == 20)
  - Bracket settles to YES (20 is within 19-20 range)
  - Your NO loses!

RECOMMENDATION:
  - Only set overrides for times when you're CONFIDENT the extreme is locked
  - HIGH: Set override for after typical peak (2-4 PM local)
  - LOW: Set override for after typical trough (6-8 AM local)
""")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("=" * 70)
    print("WX SNIPER - LOCK OVERRIDE QC TOOL")
    print("=" * 70)
    print(f"Current time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    
    # Show current overrides
    show_current_overrides()
    
    # Explain soft locks
    explain_soft_locks()
    
    # Test with real METARs
    test_with_real_metars()
    
    # Test with simulated 6-hour groups
    test_with_simulated_6hr()
    
    print("\n" + "=" * 70)
    print("QC COMPLETE")
    print("=" * 70)
