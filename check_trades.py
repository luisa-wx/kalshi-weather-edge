#!/usr/bin/env python3
"""
Quick script to check trade timestamps for a specific market.
Run in DO console with: python3 check_trades.py
"""

import os
import time
import requests
import base64
from datetime import datetime, timezone

# Get credentials from environment
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_STR = os.environ.get("KALSHI_PRIVATE_KEY", "")

def load_private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    
    key_str = PRIVATE_KEY_STR.strip()
    if '\\n' in key_str:
        key_str = key_str.replace('\\n', '\n')
    
    if '\n' not in key_str and 'PRIVATE KEY' in key_str:
        import re
        match = re.search(
            r'(-----BEGIN [A-Z ]*PRIVATE KEY-----)\s*([A-Za-z0-9+/=\s]+?)\s*(-----END [A-Z ]*PRIVATE KEY-----)',
            key_str
        )
        if match:
            header, content, footer = match.groups()
            content = ''.join(content.split())
            lines = [content[i:i+64] for i in range(0, len(content), 64)]
            key_str = header + '\n' + '\n'.join(lines) + '\n' + footer + '\n'
    
    if not key_str.endswith('\n'):
        key_str += '\n'
    
    return serialization.load_pem_private_key(
        key_str.encode(),
        password=None,
        backend=default_backend()
    )

def sign_request(private_key, timestamp_ms, method, path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    
    message = f"{timestamp_ms}{method}{path}"
    signature = private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode()

def get_trades(ticker):
    private_key = load_private_key()
    
    timestamp_ms = int(time.time() * 1000)
    endpoint = f"/markets/trades"
    params = f"ticker={ticker}&limit=50"
    sign_path = f"/trade-api/v2{endpoint}?{params}"
    
    signature = sign_request(private_key, timestamp_ms, "GET", sign_path)
    
    headers = {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
    }
    
    url = f"https://api.elections.kalshi.com/trade-api/v2{endpoint}?{params}"
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

if __name__ == "__main__":
    # Las Vegas 65-66 bracket from today
    ticker = "KXHIGHTLV-26JAN30-B65.5"
    
    print(f"Fetching trades for {ticker}...\n")
    
    try:
        data = get_trades(ticker)
        trades = data.get('trades', [])
        
        print(f"Found {len(trades)} recent trades:\n")
        print(f"{'Time (UTC)':<28} {'Price':>8} {'Count':>8} {'Taker':>8}")
        print("-" * 56)
        
        for t in trades:
            # Check what timestamp fields exist
            ts = t.get('created_time') or t.get('ts') or t.get('timestamp')
            price = t.get('yes_price') or t.get('no_price') or t.get('price')
            count = t.get('count', 1)
            taker = t.get('taker_side', '?')
            
            if ts:
                if isinstance(ts, int):
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                else:
                    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                time_str = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            else:
                time_str = "unknown"
            
            print(f"{time_str:<28} {price:>8} {count:>8} {taker:>8}")
        
        # Also print raw first trade to see all fields
        if trades:
            print(f"\n--- Raw first trade object ---")
            for k, v in trades[0].items():
                print(f"  {k}: {v}")
                
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
