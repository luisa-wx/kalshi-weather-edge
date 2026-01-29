#!/usr/bin/env python3
"""
Execute and Flip a Real Trade

This script:
1. Finds the CHEAPEST NO contract in any NYC market
2. Buys 1 contract
3. Immediately places a sell order at the same price (to flip/cancel)
4. Reports results

Use this to verify the full trading flow works before going live.
"""

import sys
import os
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kalshi_client import KalshiClient


def get_balance(client: KalshiClient) -> dict:
    """Get current balance"""
    return client.get_balance()


def find_cheapest_no(client: KalshiClient, event_ticker: str) -> dict:
    """Find the cheapest NO contract in an event"""
    markets = client.get_markets(event_ticker=event_ticker)
    
    if not markets:
        return None
    
    cheapest = None
    cheapest_price = 999
    
    for m in markets:
        no_ask_dollars = m.get('no_ask_dollars')
        if no_ask_dollars:
            price = int(float(no_ask_dollars) * 100)
            if price < cheapest_price and price > 0:
                cheapest_price = price
                cheapest = {
                    'ticker': m.get('ticker'),
                    'subtitle': m.get('yes_sub_title'),
                    'no_ask': price,
                    'no_bid': int(float(m.get('no_bid_dollars', '0')) * 100)
                }
    
    return cheapest


def execute_trade_test(dry_run: bool = True):
    """Execute a test trade"""
    print("="*70)
    print("TRADE EXECUTION TEST")
    print("="*70)
    
    client = KalshiClient()
    
    # Get balance first
    print("\n1. CHECKING BALANCE...")
    balance = get_balance(client)
    available = balance.get('balance', 0) / 100
    portfolio = balance.get('portfolio_value', 0) / 100
    print(f"   Available: ${available:.2f}")
    print(f"   Portfolio: ${portfolio:.2f}")
    
    # Get today's date
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    
    now_et = datetime.now(ZoneInfo('America/New_York'))
    date_str = now_et.strftime("%y%b%d").upper()
    
    # Find cheapest NO in NYC LOW market
    print(f"\n2. FINDING CHEAPEST NO CONTRACT...")
    event_ticker = f"KXLOWTNYC-{date_str}"
    print(f"   Searching: {event_ticker}")
    
    cheapest = find_cheapest_no(client, event_ticker)
    
    if not cheapest:
        print("   ERROR: No markets found!")
        return False
    
    print(f"   Found: {cheapest['subtitle']}")
    print(f"   Ticker: {cheapest['ticker']}")
    print(f"   NO Ask: {cheapest['no_ask']}¢")
    print(f"   NO Bid: {cheapest['no_bid']}¢")
    
    if cheapest['no_ask'] > 50:
        print("\n   WARNING: Cheapest NO is > 50¢ - looking for HIGH market instead...")
        event_ticker = f"KXHIGHNY-{date_str}"
        cheapest = find_cheapest_no(client, event_ticker)
        if cheapest:
            print(f"   Found: {cheapest['subtitle']}")
            print(f"   Ticker: {cheapest['ticker']}")
            print(f"   NO Ask: {cheapest['no_ask']}¢")
    
    if dry_run:
        print("\n3. DRY RUN - Would execute:")
        print(f"   BUY 1 NO @ {cheapest['no_ask']}¢")
        print(f"   Then SELL 1 NO @ {cheapest['no_ask']}¢ (to exit)")
        print("\n   Run with --execute to do it for real!")
        return True
    
    # REAL TRADE
    print(f"\n3. EXECUTING BUY ORDER...")
    print(f"   Buying 1 NO on {cheapest['ticker']} @ {cheapest['no_ask']}¢")
    
    try:
        buy_order = client.create_order(
            ticker=cheapest['ticker'],
            side='no',
            action='buy',
            count=1,
            price_cents=cheapest['no_ask'],
            order_type='limit'
        )
        
        buy_order_id = buy_order.get('order', {}).get('order_id')
        buy_status = buy_order.get('order', {}).get('status')
        
        print(f"   ✅ Buy order placed!")
        print(f"   Order ID: {buy_order_id}")
        print(f"   Status: {buy_status}")
        
    except Exception as e:
        print(f"   ❌ Buy failed: {e}")
        return False
    
    # Wait a moment
    print("\n   Waiting 2 seconds...")
    time.sleep(2)
    
    # Check if we have a position
    print(f"\n4. CHECKING POSITION...")
    positions = client.get_positions()
    
    our_position = None
    for mp in positions.get('market_positions', []):
        if mp.get('ticker') == cheapest['ticker']:
            our_position = mp
            break
    
    if our_position:
        print(f"   Position: {our_position.get('position')} contracts")
    else:
        print(f"   No position found (order may not have filled yet)")
    
    # Place sell order to exit
    print(f"\n5. PLACING SELL ORDER TO EXIT...")
    
    # Sell at the current ask (to exit quickly) or slightly below
    sell_price = cheapest['no_ask']  # Same price we bought at
    
    try:
        sell_order = client.create_order(
            ticker=cheapest['ticker'],
            side='no',
            action='sell',
            count=1,
            price_cents=sell_price,
            order_type='limit'
        )
        
        sell_order_id = sell_order.get('order', {}).get('order_id')
        sell_status = sell_order.get('order', {}).get('status')
        
        print(f"   ✅ Sell order placed!")
        print(f"   Order ID: {sell_order_id}")
        print(f"   Status: {sell_status}")
        print(f"   Price: {sell_price}¢")
        
    except Exception as e:
        print(f"   ❌ Sell failed: {e}")
        print(f"   You may need to manually sell your position!")
        return False
    
    # Final balance check
    print("\n6. FINAL BALANCE CHECK...")
    time.sleep(1)
    balance = get_balance(client)
    available = balance.get('balance', 0) / 100
    portfolio = balance.get('portfolio_value', 0) / 100
    print(f"   Available: ${available:.2f}")
    print(f"   Portfolio: ${portfolio:.2f}")
    
    print("\n" + "="*70)
    print("TEST COMPLETE")
    print("="*70)
    print("""
NOTES:
- If buy filled but sell is resting, you now have a position
- The sell order will exit at breakeven if someone takes it
- Check Kalshi UI to see your orders/positions
- You can cancel the sell order if you want to hold the position
""")
    
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test trade execution")
    parser.add_argument("--execute", action="store_true", 
                       help="Actually execute trades (default is dry run)")
    args = parser.parse_args()
    
    success = execute_trade_test(dry_run=not args.execute)
    sys.exit(0 if success else 1)
