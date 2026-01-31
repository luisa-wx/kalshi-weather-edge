#!/usr/bin/env python3
"""
QA Check: Compare wethr.net readings against Kalshi brackets
Run this to see if any Scout locks SHOULD be triggering
"""

import os
import requests
import hashlib
import base64
import time
from datetime import datetime
from zoneinfo import ZoneInfo

WETHR_API_KEY = "da0c8fe4607429123437c3d55cbfd5117652ac24506a9f8af45696a82e0fd652"
WETHR_BASE_URL = "https://wethr.net/api/v2/observations.php"
KALSHI_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"

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

class KalshiClient:
    def __init__(self):
        self.api_key_id = os.environ.get('KALSHI_API_KEY_ID', '')
        self.private_key_str = os.environ.get('KALSHI_PRIVATE_KEY', '')
        self.session = requests.Session()
    
    def _sign_request(self, method, path, timestamp):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend
        
        message = f"{timestamp}{method}{path}".encode('utf-8')
        key_bytes = self.private_key_str.encode('utf-8')
        private_key = serialization.load_pem_private_key(key_bytes, password=None, backend=default_backend())
        signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode('utf-8')
    
    def get_event(self, event_ticker):
        path = f"/events/{event_ticker}"
        timestamp = str(int(time.time() * 1000))
        signature = self._sign_request("GET", path, timestamp)
        
        headers = {
            'KALSHI-ACCESS-KEY': self.api_key_id,
            'KALSHI-ACCESS-SIGNATURE': signature,
            'KALSHI-ACCESS-TIMESTAMP': timestamp,
        }
        
        resp = self.session.get(f"{KALSHI_BASE_URL}{path}", headers=headers, timeout=10)
        return resp.json()

def get_today_suffix(tz_name):
    tz = ZoneInfo(tz_name)
    return datetime.now(tz).strftime('%y%b%d').upper()

print("=" * 80)
print("WETHR.NET vs KALSHI BRACKET QA CHECK")
print(f"Time: {datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %I:%M:%S %p ET')}")
print("=" * 80)

# Initialize Kalshi client
kalshi = None
try:
    kalshi = KalshiClient()
    if not kalshi.api_key_id:
        print("\n⚠️  KALSHI_API_KEY_ID not set - skipping bracket fetch")
        kalshi = None
except Exception as e:
    print(f"\n⚠️  Kalshi client init failed: {e}")

for station, cfg in STATIONS.items():
    tz = ZoneInfo(cfg['tz'])
    local_time = datetime.now(tz).strftime('%I:%M %p')
    today_suffix = get_today_suffix(cfg['tz'])
    
    print(f"\n{'='*80}")
    print(f"{cfg['name']} ({station}) - Local: {local_time}")
    print("=" * 80)
    
    # Get latest observation (5-min data with probable range)
    latest = get_wethr_data(station)
    if 'error' in latest:
        print(f"  ❌ Wethr API error: {latest['error']}")
        continue
    
    temp = latest.get('temperature_display', '?')
    lowest = latest.get('lowest_probable')
    highest = latest.get('highest_probable')
    obs_time = latest.get('observation_time', '?')
    
    print(f"\n📡 WETHR.NET DATA:")
    print(f"   Current Temp:      {temp}°F")
    print(f"   Lowest Probable:   {lowest}°F")
    print(f"   Highest Probable:  {highest}°F")
    print(f"   Observation Time:  {obs_time}")
    
    if not kalshi:
        continue
    
    # Get HIGH brackets
    print(f"\n📈 HIGH BRACKETS ({cfg['high']}-{today_suffix}):")
    try:
        event_data = kalshi.get_event(f"{cfg['high']}-{today_suffix}")
        markets = event_data.get('event', event_data).get('markets', [])
        
        locks_found = []
        for m in markets:
            cap = m.get('cap_strike')
            floor = m.get('floor_strike')
            subtitle = m.get('yes_sub_title', m.get('subtitle', ''))
            no_ask = m.get('no_ask', 0)
            yes_ask = m.get('yes_ask', 0)
            
            # Check if Scout would lock this
            would_lock = False
            if cap is not None and lowest is not None and int(lowest) > int(cap):
                would_lock = True
                locks_found.append(f"   🔒 SCOUT LOCK: {subtitle} (lowest_probable {lowest} > cap {cap}) - NO @ {int(no_ask*100)}¢")
            
            if would_lock:
                print(f"   🔒 {subtitle}: cap={cap} | NO={int(no_ask*100)}¢ YES={int(yes_ask*100)}¢ ← LOCK!")
            else:
                print(f"      {subtitle}: cap={cap} | NO={int(no_ask*100)}¢ YES={int(yes_ask*100)}¢")
        
        if locks_found:
            print(f"\n   ⚡ SCOUT SHOULD TRIGGER {len(locks_found)} TRADES!")
        else:
            print(f"\n   ✓ No Scout locks (lowest_probable {lowest} not > any cap)")
            
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    # Get LOW brackets
    print(f"\n📉 LOW BRACKETS ({cfg['low']}-{today_suffix}):")
    try:
        event_data = kalshi.get_event(f"{cfg['low']}-{today_suffix}")
        markets = event_data.get('event', event_data).get('markets', [])
        
        locks_found = []
        for m in markets:
            cap = m.get('cap_strike')
            floor = m.get('floor_strike')
            subtitle = m.get('yes_sub_title', m.get('subtitle', ''))
            no_ask = m.get('no_ask', 0)
            yes_ask = m.get('yes_ask', 0)
            
            # Check if Scout would lock this
            would_lock = False
            if floor is not None and highest is not None and int(highest) < int(floor):
                would_lock = True
                locks_found.append(f"   🔒 SCOUT LOCK: {subtitle} (highest_probable {highest} < floor {floor}) - NO @ {int(no_ask*100)}¢")
            
            if would_lock:
                print(f"   🔒 {subtitle}: floor={floor} | NO={int(no_ask*100)}¢ YES={int(yes_ask*100)}¢ ← LOCK!")
            else:
                print(f"      {subtitle}: floor={floor} | NO={int(no_ask*100)}¢ YES={int(yes_ask*100)}¢")
        
        if locks_found:
            print(f"\n   ⚡ SCOUT SHOULD TRIGGER {len(locks_found)} TRADES!")
        else:
            print(f"\n   ✓ No Scout locks (highest_probable {highest} not < any floor)")
            
    except Exception as e:
        print(f"   ❌ Error: {e}")

print("\n" + "=" * 80)
print("LEGEND:")
print("  🔒 = Scout SHOULD lock this bracket (buy NO at 85¢)")
print("  ✓  = No action needed")
print("=" * 80)
