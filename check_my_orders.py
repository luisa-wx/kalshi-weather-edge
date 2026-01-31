#!/usr/bin/env python3
"""
Check your own order timestamps from Kalshi portfolio.
Run: python3 check_my_orders.py
"""

import os
import time
import requests
import base64
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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

def get_my_orders(ticker=None):
    private_key = load_private_key()
    
    timestamp_ms = int(time.time() * 1000)
    endpoint = "/portfolio/orders"
    params = "limit=100"
    if ticker:
        params += f"&ticker={ticker}"
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

def get_my_fills(ticker=None):
    private_key = load_private_key()
    
    timestamp_ms = int(time.time() * 1000)
    endpoint = "/portfolio/fills"
    params = "limit=100"
    if ticker:
        params += f"&ticker={ticker}"
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
    ticker = "KXHIGHTLV-26JAN30-B65.5"
    pt = ZoneInfo('America/Los_Angeles')
    
    print(f"=== YOUR ORDERS for {ticker} ===\n")
    
    try:
        data = get_my_orders(ticker)
        orders = data.get('orders', [])
        
        for o in orders:
            created = o.get('created_time')
            if created:
                dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                dt_pt = dt.astimezone(pt)
                time_str = dt_pt.strftime("%I:%M:%S.") + dt_pt.strftime("%f")[:3] + " PT"
            else:
                time_str = "?"
            
            print(f"Order ID: {o.get('order_id', '?')[:20]}...")
            print(f"  Created:  {time_str}")
            print(f"  Side:     {o.get('side')} / {o.get('action')}")
            print(f"  Status:   {o.get('status')}")
            print(f"  Price:    YES {o.get('yes_price')}¢ / NO {o.get('no_price')}¢")
            print(f"  Count:    {o.get('initial_count')} (filled: {o.get('fill_count')})")
            print()
            
    except Exception as e:
        print(f"Orders error: {e}")
    
    print(f"\n=== YOUR FILLS for {ticker} ===\n")
    
    try:
        data = get_my_fills(ticker)
        fills = data.get('fills', [])
        
        for f in fills:
            created = f.get('created_time')
            if created:
                dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                dt_pt = dt.astimezone(pt)
                time_str = dt_pt.strftime("%I:%M:%S.") + dt_pt.strftime("%f")[:3] + " PT"
            else:
                time_str = "?"
            
            print(f"Fill ID: {f.get('fill_id', '?')[:20]}...")
            print(f"  Time:     {time_str}")
            print(f"  Side:     {f.get('side')} / {f.get('action')}")
            print(f"  Price:    YES {f.get('yes_price')}¢ / NO {f.get('no_price')}¢")
            print(f"  Count:    {f.get('count')}")
            print(f"  Taker:    {f.get('is_taker')}")
            print()
            
        # Raw dump of first fill
        if fills:
            print("--- Raw first fill ---")
            for k, v in fills[0].items():
                print(f"  {k}: {v}")
                
    except Exception as e:
        print(f"Fills error: {e}")
        import traceback
        traceback.print_exc()
