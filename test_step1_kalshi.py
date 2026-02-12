#!/usr/bin/env python3
"""
QC Test: Step 1 — Kalshi Client + Bracket Loading + Opportunity Detection

Run on EC2:
    source venv/bin/activate
    python3 test_step1_kalshi.py

Tests:
    1. Kalshi API authentication (exchange status)
    2. Load brackets for one station (KSEA)
    3. Display all brackets with prices
    4. Simulate CLI high → find opportunities
    5. Dry-run snipe execution
"""

import os
import sys
import logging

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

from kalshi_client import (
    KalshiClient,
    Bracket,
    load_all_brackets,
    load_brackets_for_station,
    find_opportunities,
    execute_snipe,
    get_today_suffix,
)
from stations import STATIONS

print("=" * 70)
print("STEP 1 QC: Kalshi Client + Brackets + Opportunity Detection")
print("=" * 70)

# --- Test 1: Auth ---
print("\n[1] Testing Kalshi authentication...")
client = KalshiClient()

if not client.private_key:
    print("  ❌ No credentials. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY in .env")
    print("  (continuing with bracket loading test using mock data)\n")
else:
    try:
        status = client.get_exchange_status()
        active = status.get("exchange_active") or status.get("trading_active")
        print(f"  ✅ Exchange status: {status}")
        print(f"  Latency: {client.last_latency_ms:.0f}ms")
    except Exception as e:
        print(f"  ❌ Auth failed: {e}")

# --- Test 2: Load brackets for KSEA ---
print(f"\n[2] Loading brackets (today={get_today_suffix()})...")

if client.private_key:
    # Load just KSEA as a quick test
    test_station = "KSEA"
    cfg = STATIONS[test_station]
    brackets_high = load_brackets_for_station(
        client, test_station, cfg["high_ticker"], "high"
    )

    if brackets_high:
        print(f"\n  {test_station} HIGH brackets ({len(brackets_high)}):")
        print(f"  {'Subtitle':<30} {'Floor':>5} {'Cap':>5} {'Type':<10} {'YES Ask':>8} {'NO Ask':>8}")
        print(f"  {'-'*30} {'-'*5} {'-'*5} {'-'*10} {'-'*8} {'-'*8}")
        for b in sorted(brackets_high, key=lambda x: x.floor_strike if x.floor_strike is not None else -999):
            floor = str(b.floor_strike) if b.floor_strike is not None else "—"
            cap = str(b.cap_strike) if b.cap_strike is not None else "—"
            print(f"  {b.subtitle:<30} {floor:>5} {cap:>5} {b.strike_type:<10} {b.yes_ask:>7}¢ {b.no_ask:>7}¢")
    else:
        print(f"  No brackets found for {test_station} — market may not be open today")

    # --- Test 3: Load ALL stations ---
    print(f"\n[3] Loading all 19 stations...")
    all_brackets = load_all_brackets(client, STATIONS)

    total_high = sum(len(v["high"]) for v in all_brackets.values())
    total_low = sum(len(v["low"]) for v in all_brackets.values())
    print(f"  Total: {total_high} HIGH brackets + {total_low} LOW brackets")

    for station, data in all_brackets.items():
        h = len(data["high"])
        l = len(data["low"])
        city = STATIONS[station]["city"]
        if h + l > 0:
            print(f"  {station} ({city}): {h} HIGH, {l} LOW")

    # --- Test 4: Simulate opportunity detection ---
    print(f"\n[4] Simulating CLI drop for KSEA (HIGH=52°F)...")
    if all_brackets.get("KSEA", {}).get("high"):
        opps = find_opportunities(
            all_brackets,
            cli_high=52,
            station="KSEA",
            max_price=98,
        )
        if opps:
            print(f"  🎯 Found {len(opps)} opportunities:")
            for opp in opps:
                b = opp["bracket"]
                print(f"    {opp['action']} {b.subtitle} @ {opp['price']}¢ → edge={opp['edge_cents']}¢")
                print(f"      Reason: {opp['reason']}")
        else:
            print("  No opportunities below 98¢ threshold (all brackets at 99¢+ already)")
            # Show what WOULD resolve
            print("  Bracket resolutions at HIGH=52°F:")
            for b in all_brackets["KSEA"]["high"]:
                result = b.check_temp(52)
                marker = "✅" if result == "locked" else "❌" if result == "dead" else "⬜"
                print(f"    {marker} {b.subtitle} → {result} (yes={b.yes_ask}¢ no={b.no_ask}¢)")

    # --- Test 5: Dry-run snipe ---
    print(f"\n[5] Dry-run snipe execution...")
    if opps:
        record = execute_snipe(
            client,
            opps[0],
            order_size=1,
            live_mode=False,
            processed_tickers=set(),
        )
        print(f"  Result: {record}")
    else:
        print("  Skipped — no opportunities to test")

else:
    # No credentials — test with mock data
    print("\n  Testing bracket logic with mock data...")
    b = Bracket(
        ticker="KXHIGHTSEA-26FEB11-T52.5",
        subtitle="52° to 53°",
        floor_strike=52,
        cap_strike=53,
        strike_type="between",
        signal_type="high",
        station="KSEA",
    )
    b.yes_ask = 50
    b.no_ask = 50

    print(f"  Bracket: {b}")
    print(f"  check_temp(52) = {b.check_temp(52)}")  # Should be "locked"
    print(f"  check_temp(54) = {b.check_temp(54)}")  # Should be "dead"
    print(f"  check_temp(51) = {b.check_temp(51)}")  # Should be "dead"

    # Test greater_or_equal
    b2 = Bracket(
        ticker="KXHIGHTSEA-26FEB11-B55",
        subtitle="55° or above",
        floor_strike=54,
        cap_strike=None,
        strike_type="greater",
        signal_type="high",
        station="KSEA",
    )
    print(f"\n  Bracket: {b2}")
    print(f"  check_temp(55) = {b2.check_temp(55)}")  # Should be "locked"
    print(f"  check_temp(54) = {b2.check_temp(54)}")  # Should be "locked" (>= floor)
    print(f"  check_temp(53) = {b2.check_temp(53)}")  # Should be "dead"

    # Test less
    b3 = Bracket(
        ticker="KXHIGHTSEA-26FEB11-L48",
        subtitle="47° or below",
        floor_strike=None,
        cap_strike=48,
        strike_type="less",
        signal_type="high",
        station="KSEA",
    )
    print(f"\n  Bracket: {b3}")
    print(f"  check_temp(47) = {b3.check_temp(47)}")  # Should be "locked"
    print(f"  check_temp(48) = {b3.check_temp(48)}")  # Should be "dead"
    print(f"  check_temp(49) = {b3.check_temp(49)}")  # Should be "dead"

print("\n" + "=" * 70)
print("STEP 1 QC COMPLETE")
print("=" * 70)
