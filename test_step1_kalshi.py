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
    print("\n  Testing bracket logic with mock data (per reference doc)...")
    
    # HIGH between: "64° to 65°" → floor=64, cap=65
    b = Bracket(
        ticker="TEST-B64.5", subtitle="64° to 65°",
        floor_strike=64, cap_strike=65,
        strike_type="between", signal_type="high", station="KLAX",
    )
    print(f"\n  HIGH between: {b.subtitle} (floor={b.floor_strike}, cap={b.cap_strike})")
    print(f"    check_temp(64) = {b.check_temp(64)} (expect locked)")
    print(f"    check_temp(65) = {b.check_temp(65)} (expect locked)")
    print(f"    check_temp(63) = {b.check_temp(63)} (expect dead)")
    print(f"    check_temp(66) = {b.check_temp(66)} (expect dead)")
    assert b.check_temp(64) == "locked"
    assert b.check_temp(65) == "locked"
    assert b.check_temp(63) == "dead"
    assert b.check_temp(66) == "dead"
    print("    ✅ All correct")

    # HIGH greater: "72° or above" → floor=71 (offset by 1), YES wins when final > 71
    b2 = Bracket(
        ticker="TEST-G72", subtitle="72° or above",
        floor_strike=71, cap_strike=None,
        strike_type="greater", signal_type="high", station="KLAX",
    )
    print(f"\n  HIGH greater: {b2.subtitle} (floor={b2.floor_strike})")
    print(f"    check_temp(72) = {b2.check_temp(72)} (expect locked, 72>71)")
    print(f"    check_temp(71) = {b2.check_temp(71)} (expect dead, 71>71 is false)")
    print(f"    check_temp(80) = {b2.check_temp(80)} (expect locked)")
    assert b2.check_temp(72) == "locked"
    assert b2.check_temp(71) == "dead"
    assert b2.check_temp(80) == "locked"
    print("    ✅ All correct")

    # HIGH less: "61° or below" → cap=62 (offset by 1), YES wins when final < 62
    b3 = Bracket(
        ticker="TEST-L61", subtitle="61° or below",
        floor_strike=None, cap_strike=62,
        strike_type="less", signal_type="high", station="KLAX",
    )
    print(f"\n  HIGH less: {b3.subtitle} (cap={b3.cap_strike})")
    print(f"    check_temp(61) = {b3.check_temp(61)} (expect locked, 61<62)")
    print(f"    check_temp(62) = {b3.check_temp(62)} (expect dead, 62<62 is false)")
    print(f"    check_temp(50) = {b3.check_temp(50)} (expect locked)")
    assert b3.check_temp(61) == "locked"
    assert b3.check_temp(62) == "dead"
    assert b3.check_temp(50) == "locked"
    print("    ✅ All correct")

    # LOW between: "35° to 36°" → floor=35, cap=36
    b4 = Bracket(
        ticker="TEST-LB35", subtitle="35° to 36°",
        floor_strike=35, cap_strike=36,
        strike_type="between", signal_type="low", station="KNYC",
    )
    print(f"\n  LOW between: {b4.subtitle} (floor={b4.floor_strike}, cap={b4.cap_strike})")
    print(f"    check_temp(35) = {b4.check_temp(35)} (expect locked)")
    print(f"    check_temp(36) = {b4.check_temp(36)} (expect locked)")
    print(f"    check_temp(34) = {b4.check_temp(34)} (expect dead)")
    print(f"    check_temp(37) = {b4.check_temp(37)} (expect dead)")
    assert b4.check_temp(35) == "locked"
    assert b4.check_temp(36) == "locked"
    assert b4.check_temp(34) == "dead"
    assert b4.check_temp(37) == "dead"
    print("    ✅ All correct")

    # LOW greater: "37° or above" → floor=36, YES wins when final > 36
    b5 = Bracket(
        ticker="TEST-LG37", subtitle="37° or above",
        floor_strike=36, cap_strike=None,
        strike_type="greater", signal_type="low", station="KNYC",
    )
    print(f"\n  LOW greater: {b5.subtitle} (floor={b5.floor_strike})")
    print(f"    check_temp(37) = {b5.check_temp(37)} (expect locked, 37>36)")
    print(f"    check_temp(36) = {b5.check_temp(36)} (expect dead, 36>36 is false)")
    assert b5.check_temp(37) == "locked"
    assert b5.check_temp(36) == "dead"
    print("    ✅ All correct")

    # LOW less: "34° or below" → cap=35, YES wins when final < 35
    b6 = Bracket(
        ticker="TEST-LL34", subtitle="34° or below",
        floor_strike=None, cap_strike=35,
        strike_type="less", signal_type="low", station="KNYC",
    )
    print(f"\n  LOW less: {b6.subtitle} (cap={b6.cap_strike})")
    print(f"    check_temp(34) = {b6.check_temp(34)} (expect locked, 34<35)")
    print(f"    check_temp(35) = {b6.check_temp(35)} (expect dead, 35<35 is false)")
    assert b6.check_temp(34) == "locked"
    assert b6.check_temp(35) == "dead"
    print("    ✅ All correct")

print("\n" + "=" * 70)
print("STEP 1 QC COMPLETE")
print("=" * 70)
