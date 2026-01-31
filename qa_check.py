#!/usr/bin/env python3
"""
QA Check: Compare wethr.net readings against Kalshi brackets
Run this to see if any Scout locks SHOULD be triggering
"""

import requests
from datetime import datetime
from zoneinfo import ZoneInfo

WETHR_API_KEY = "da0c8fe4607429123437c3d55cbfd5117652ac24506a9f8af45696a82e0fd652"
WETHR_BASE_URL = "https://wethr.net/api/v2/observations.php"

STATIONS = {
    'KNYC': {'name': 'NYC', 'tz': 'America/New_York', 'high': 'KXHIGHNYC', 'low': 'KXLOWNYC'},
    'KMDW': {'name': 'Chicago', 'tz': 'America/Chicago', 'high': 'KXHIGHORD', 'low': 'KXLOWORD'},
    'KLAX': {'name': 'Los Angeles', 'tz': 'America/Los_Angeles', 'high': 'KXHIGHLAX', 'low': 'KXLOWLAX'},
    'KAUS': {'name': 'Austin', 'tz': 'America/Chicago', 'high': 'KXHIGHAUS', 'low': 'KXLOWAUS'},
    'KMIA': {'name': 'Miami', 'tz': 'America/New_York', 'high': 'KXHIGHMIA', 'low': 'KXLOWMIA'},
    'KDCA': {'name': 'DC', 'tz': 'America/New_York', 'high': 'KXHIGHDCA', 'low': 'KXLOWDCA'},
}

def get_wethr_data(station):
    """Fetch latest observation from wethr.net"""
    try:
        resp = requests.get(WETHR_BASE_URL, params={
            'station_code': station,
            'mode': 'latest'
        }, headers={
            'Authorization': f'Bearer {WETHR_API_KEY}'
        }, timeout=10)
        return resp.json()
    except Exception as e:
        return {'error': str(e)}

def get_wethr_high(station):
    """Fetch wethr_high (day's running high/low) from wethr.net"""
    try:
        resp = requests.get(WETHR_BASE_URL, params={
            'station_code': station,
            'mode': 'wethr_high',
            'logic': 'nws'
        }, headers={
            'Authorization': f'Bearer {WETHR_API_KEY}'
        }, timeout=10)
        return resp.json()
    except Exception as e:
        return {'error': str(e)}

print("=" * 70)
print("WETHR.NET QA CHECK")
print(f"Time: {datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %I:%M:%S %p ET')}")
print("=" * 70)

for station, cfg in STATIONS.items():
    tz = ZoneInfo(cfg['tz'])
    local_time = datetime.now(tz).strftime('%I:%M %p')
    
    print(f"\n{cfg['name']} ({station}) - Local: {local_time}")
    print("-" * 50)
    
    # Get latest observation (5-min data with probable range)
    latest = get_wethr_data(station)
    if 'error' not in latest:
        temp = latest.get('temperature_display', '?')
        lowest = latest.get('lowest_probable', '?')
        highest = latest.get('highest_probable', '?')
        obs_time = latest.get('observation_time', '?')
        print(f"  Latest Obs:     {temp}°F (range: {lowest}-{highest}°F)")
        print(f"  Obs Time:       {obs_time}")
    else:
        print(f"  Latest Obs:     ERROR - {latest['error']}")
    
    # Get day's running high/low
    wethr_high = get_wethr_high(station)
    if 'error' not in wethr_high:
        day_high = wethr_high.get('wethr_high', '?')
        day_low = wethr_high.get('wethr_low', '?')
        print(f"  Day's High:     {day_high}°F")
        print(f"  Day's Low:      {day_low}°F")
    else:
        print(f"  Day's High/Low: ERROR - {wethr_high.get('error', 'unknown')}")
    
    # Scout lock analysis
    if 'error' not in latest and lowest != '?':
        print(f"\n  SCOUT ANALYSIS:")
        print(f"    lowest_probable={lowest}°F - would kill HIGH brackets with cap < {lowest}")
        print(f"    highest_probable={highest}°F - would kill LOW brackets with floor > {highest}")

print("\n" + "=" * 70)
print("To see Kalshi brackets, check the dashboard at http://localhost:8080")
print("=" * 70)
