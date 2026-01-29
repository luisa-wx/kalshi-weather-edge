#!/usr/bin/env python3
"""
Live Trade Test Script

This script tests the trading mechanics WITHOUT automated METAR triggers.
It allows you to:
1. Find a cheap market to test with
2. Execute a small BUY order
3. Immediately place a SELL order at 99¢ (hedge)
4. Optionally cancel both orders

USAGE:
  python live_trade_test.py --dry-run          # Just show what would happen
  python live_trade_test.py --execute          # Actually execute the trade
  python live_trade_test.py --cancel ORDER_ID  # Cancel a specific order

REQUIREMENTS:
  - KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH env vars set
  - Small amount of funds available
"""

import os
import sys
import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Settings
from kalshi_client import KalshiClient

# Colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"


def find_test_market(client: KalshiClient, max_price: int = 20) -> dict:
    """
    Find a cheap market suitable for testing.
    We want something with low YES price so we can test buying cheap.
    """
    print(f"\n{CYAN}Looking for test markets with YES price <= {max_price}¢...{RESET}")
    
    # Try a few different series
    test_series = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX"]
    
    for series in test_series:
        try:
            print(f"  Checking {series}...")
            resp = client._request("GET", "/markets", params={
                "series_ticker": series,
                "status": "open",
                "limit": 20
            })
            
            markets = resp.get("markets", [])
            
            for m in markets:
                yes_bid = m.get("yes_bid", 0)
                yes_ask = m.get("yes_ask", 100)
                ticker = m.get("ticker", "")
                
                # Look for markets with low YES price and some liquidity
                if yes_ask and yes_ask <= max_price and yes_bid and yes_bid > 0:
                    print(f"  {GREEN}Found: {ticker}{RESET}")
                    print(f"    YES bid/ask: {yes_bid}¢ / {yes_ask}¢")
                    return m
                    
        except Exception as e:
            print(f"  {YELLOW}Error checking {series}: {e}{RESET}")
            continue
    
    return None


def show_balance(client: KalshiClient):
    """Show current account balance"""
    try:
        balance = client.get_balance()
        print(f"\n{CYAN}Account Balance:{RESET}")
        print(f"  Available: ${balance.get('balance', 0) / 100:.2f}")
        print(f"  Portfolio: ${balance.get('portfolio_value', 0) / 100:.2f}")
    except Exception as e:
        print(f"{RED}Error getting balance: {e}{RESET}")


def execute_test_trade(client: KalshiClient, market: dict, contracts: int = 1) -> tuple:
    """
    Execute a test trade:
    1. BUY 1 contract at market YES ask price
    2. Immediately place SELL limit at 99¢
    
    Returns (buy_order_id, sell_order_id) or (None, None) on failure
    """
    ticker = market.get("ticker")
    yes_ask = market.get("yes_ask", 0)
    
    if not ticker or not yes_ask:
        print(f"{RED}Invalid market data{RESET}")
        return None, None
    
    print(f"\n{CYAN}Executing test trade on {ticker}...{RESET}")
    print(f"  BUY {contracts} YES @ {yes_ask}¢")
    
    # Step 1: Buy
    try:
        buy_result = client.place_order(
            ticker=ticker,
            side="yes",
            action="buy",
            count=contracts,
            price_cents=yes_ask,
            order_type="limit"
        )
        
        buy_order = buy_result.get("order", {})
        buy_order_id = buy_order.get("order_id")
        buy_status = buy_order.get("status")
        fill_count = buy_order.get("fill_count", 0)
        
        print(f"  {GREEN}BUY order placed:{RESET}")
        print(f"    Order ID: {buy_order_id}")
        print(f"    Status: {buy_status}")
        print(f"    Filled: {fill_count}")
        
        if fill_count == 0:
            print(f"  {YELLOW}Warning: Order not filled. May need to adjust price.{RESET}")
        
    except Exception as e:
        print(f"  {RED}BUY failed: {e}{RESET}")
        return None, None
    
    # Step 2: Place hedge SELL at 99¢
    print(f"\n  Placing hedge SELL @ 99¢...")
    
    try:
        sell_result = client.place_order(
            ticker=ticker,
            side="yes",
            action="sell",
            count=contracts,
            price_cents=99,
            order_type="limit"
        )
        
        sell_order = sell_result.get("order", {})
        sell_order_id = sell_order.get("order_id")
        sell_status = sell_order.get("status")
        
        print(f"  {GREEN}SELL order placed:{RESET}")
        print(f"    Order ID: {sell_order_id}")
        print(f"    Status: {sell_status}")
        
    except Exception as e:
        print(f"  {RED}SELL failed: {e}{RESET}")
        sell_order_id = None
    
    return buy_order_id, sell_order_id


def cancel_order(client: KalshiClient, order_id: str):
    """Cancel a specific order"""
    print(f"\n{CYAN}Canceling order {order_id}...{RESET}")
    
    try:
        result = client.cancel_order(order_id)
        print(f"  {GREEN}Order canceled successfully{RESET}")
        return True
    except Exception as e:
        print(f"  {RED}Cancel failed: {e}{RESET}")
        return False


def show_open_orders(client: KalshiClient):
    """Show all open orders"""
    print(f"\n{CYAN}Open Orders:{RESET}")
    
    try:
        resp = client._request("GET", "/portfolio/orders", params={"status": "resting"})
        orders = resp.get("orders", [])
        
        if not orders:
            print("  No open orders")
            return
            
        for o in orders:
            ticker = o.get("ticker", "?")
            side = o.get("side", "?")
            action = o.get("action", "?")
            price = o.get("yes_price", 0) if side == "yes" else o.get("no_price", 0)
            count = o.get("remaining_count", 0)
            order_id = o.get("order_id", "?")
            
            print(f"  {order_id[:8]}... {action.upper()} {count} {side.upper()} @ {price}¢ on {ticker}")
            
    except Exception as e:
        print(f"  {RED}Error: {e}{RESET}")


def show_positions(client: KalshiClient):
    """Show current positions"""
    print(f"\n{CYAN}Positions:{RESET}")
    
    try:
        resp = client._request("GET", "/portfolio/positions", params={"limit": 100})
        positions = resp.get("market_positions", [])
        
        has_positions = False
        for p in positions:
            pos = p.get("position", 0)
            if pos != 0:
                has_positions = True
                ticker = p.get("ticker", "?")
                exposure = p.get("market_exposure", 0)
                side = "YES" if pos > 0 else "NO"
                print(f"  {ticker}: {abs(pos)} {side} (exposure: ${exposure/100:.2f})")
        
        if not has_positions:
            print("  No open positions")
            
    except Exception as e:
        print(f"  {RED}Error: {e}{RESET}")


def main():
    parser = argparse.ArgumentParser(description="Live trade test script")
    parser.add_argument("--dry-run", action="store_true", help="Just show what would happen")
    parser.add_argument("--execute", action="store_true", help="Actually execute the trade")
    parser.add_argument("--cancel", type=str, help="Cancel a specific order ID")
    parser.add_argument("--status", action="store_true", help="Show account status only")
    parser.add_argument("--max-price", type=int, default=15, help="Max price in cents for test market")
    parser.add_argument("--contracts", type=int, default=1, help="Number of contracts to trade")
    
    args = parser.parse_args()
    
    # Check for API credentials
    if not os.environ.get("KALSHI_API_KEY"):
        print(f"{RED}Error: KALSHI_API_KEY environment variable not set{RESET}")
        print("Set it with: export KALSHI_API_KEY=your_key_here")
        return 1
    
    if not os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        print(f"{RED}Error: KALSHI_PRIVATE_KEY_PATH environment variable not set{RESET}")
        print("Set it with: export KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem")
        return 1
    
    # Initialize client
    settings = Settings()
    client = KalshiClient(settings)
    
    print("\n" + "="*60)
    print(f" LIVE TRADE TEST - {datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("="*60)
    
    # Show current status
    show_balance(client)
    show_positions(client)
    show_open_orders(client)
    
    if args.status:
        return 0
    
    if args.cancel:
        cancel_order(client, args.cancel)
        return 0
    
    if args.dry_run or args.execute:
        # Find a test market
        market = find_test_market(client, max_price=args.max_price)
        
        if not market:
            print(f"\n{YELLOW}No suitable test market found with price <= {args.max_price}¢{RESET}")
            print("Try increasing --max-price or check that markets are open")
            return 1
        
        ticker = market.get("ticker")
        yes_bid = market.get("yes_bid", 0)
        yes_ask = market.get("yes_ask", 0)
        
        print(f"\n{CYAN}Test Market Selected:{RESET}")
        print(f"  Ticker: {ticker}")
        print(f"  YES bid/ask: {yes_bid}¢ / {yes_ask}¢")
        print(f"  Cost to buy {args.contracts}: ${args.contracts * yes_ask / 100:.2f}")
        
        if args.dry_run:
            print(f"\n{YELLOW}DRY RUN - No trades executed{RESET}")
            print(f"Would execute:")
            print(f"  1. BUY {args.contracts} YES @ {yes_ask}¢")
            print(f"  2. SELL {args.contracts} YES @ 99¢ (hedge)")
            return 0
        
        if args.execute:
            print(f"\n{RED}⚠️  LIVE EXECUTION - Real money will be used!{RESET}")
            confirm = input("Type 'yes' to confirm: ")
            
            if confirm.lower() != "yes":
                print("Canceled.")
                return 0
            
            buy_id, sell_id = execute_test_trade(client, market, args.contracts)
            
            if buy_id:
                print(f"\n{GREEN}Trade executed successfully!{RESET}")
                print(f"  BUY order:  {buy_id}")
                print(f"  SELL order: {sell_id or 'None'}")
                print(f"\nTo cancel these orders:")
                if buy_id:
                    print(f"  python live_trade_test.py --cancel {buy_id}")
                if sell_id:
                    print(f"  python live_trade_test.py --cancel {sell_id}")
            
            # Show updated status
            print("\n" + "-"*40)
            show_balance(client)
            show_positions(client)
            show_open_orders(client)
            
            return 0
    
    # Default: show help
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
