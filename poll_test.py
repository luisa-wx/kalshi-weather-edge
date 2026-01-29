#!/usr/bin/env python3
"""
Aviation Weather Poller Test

This script:
1. Fetches LIVE METARs from aviationweather.gov
2. Parses 6-hour temps if present
3. Shows what the bot would see at synoptic times
"""

import sys
import os
import requests
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from metar_parser import parse_metar
from config import STATIONS


def fetch_metar(station: str) -> str:
    """Fetch latest METAR from aviationweather.gov"""
    url = "https://aviationweather.gov/api/data/metar"
    params = {
        "ids": station,
        "format": "raw",
        "hours": 2  # Last 2 hours
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.text.strip()
    except Exception as e:
        return f"ERROR: {e}"


def check_synoptic_time() -> dict:
    """Check if we're in a synoptic window"""
    now = datetime.now(timezone.utc)
    minute = now.minute
    hour = now.hour
    
    # Synoptic times are 00Z, 06Z, 12Z, 18Z
    is_synoptic_hour = hour in [0, 6, 12, 18]
    
    # Hot window is :52 to :02
    is_hot_window = minute >= 52 or minute <= 2
    
    return {
        'utc_time': now.strftime("%H:%M:%S UTC"),
        'hour': hour,
        'minute': minute,
        'is_synoptic_hour': is_synoptic_hour,
        'is_hot_window': is_hot_window,
        'next_synoptic': f"{((hour // 6) + 1) * 6 % 24:02d}:00Z"
    }


def main():
    print("="*70)
    print("AVIATION WEATHER POLLER TEST")
    print("="*70)
    
    # Check timing
    timing = check_synoptic_time()
    print(f"\nCurrent UTC: {timing['utc_time']}")
    print(f"Is synoptic hour (00/06/12/18Z)? {timing['is_synoptic_hour']}")
    print(f"Is hot window (:52-:02)? {timing['is_hot_window']}")
    print(f"Next synoptic time: {timing['next_synoptic']}")
    
    # Fetch METARs for all configured stations
    print("\n" + "-"*70)
    print("FETCHING LIVE METARS")
    print("-"*70)
    
    for station, config in STATIONS.items():
        print(f"\n📡 {station} ({config.get('name', 'Unknown')})")
        
        metar_text = fetch_metar(station)
        
        if metar_text.startswith("ERROR"):
            print(f"   {metar_text}")
            continue
        
        # May have multiple lines (multiple METARs)
        lines = [l.strip() for l in metar_text.split('\n') if l.strip()]
        
        if not lines:
            print("   No METAR data")
            continue
        
        # Take the most recent (first line usually)
        latest = lines[0]
        print(f"   Raw: {latest[:80]}{'...' if len(latest) > 80 else ''}")
        
        # Parse it
        parsed = parse_metar(latest)
        
        print(f"   Station: {parsed.station}")
        print(f"   Time: {parsed.observation_time}")
        print(f"   Current temp: {parsed.temp_c}°C / {parsed.temp_f}°F")
        
        if parsed.six_hour_max_c is not None:
            print(f"   🔥 6-HR MAX: {parsed.six_hour_max_c}°C → {parsed.six_hour_max_f_rounded}°F (rounded)")
        else:
            print(f"   6-hr max: Not present")
            
        if parsed.six_hour_min_c is not None:
            print(f"   ❄️ 6-HR MIN: {parsed.six_hour_min_c}°C → {parsed.six_hour_min_f_rounded}°F (rounded)")
        else:
            print(f"   6-hr min: Not present")
    
    # Show when 6-hour temps are expected
    print("\n" + "-"*70)
    print("WHEN TO EXPECT 6-HOUR TEMPS")
    print("-"*70)
    print("""
6-hour max/min temps are reported at SYNOPTIC times:
  - 00Z (7 PM EST / 8 PM EDT)  → Reports max/min from previous 6 hours
  - 06Z (1 AM EST / 2 AM EDT)  → Reports max/min from previous 6 hours  
  - 12Z (7 AM EST / 8 AM EDT)  → Reports max/min from previous 6 hours
  - 18Z (1 PM EST / 2 PM EDT)  → Reports max/min from previous 6 hours

METARs are issued ~:53 past the hour, so:
  - 6-hr temps appear in METARs around :53 of synoptic hours
  - Bot should poll heavily from :52 to :02

For LOW markets:
  - Best trading time is after 06Z/12Z (overnight lows captured)
  
For HIGH markets:
  - Best trading time is after 18Z/00Z (afternoon highs captured)
""")
    
    print("\n" + "="*70)
    print("TEST COMPLETE")
    print("="*70)


if __name__ == "__main__":
    main()
