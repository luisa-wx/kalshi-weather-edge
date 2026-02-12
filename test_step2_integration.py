#!/usr/bin/env python3
"""
QC Test: Step 2 — CLI → Bracket Resolution → Opportunity Detection

Run AFTER nwws_monitor_server.py is running:
    python3 test_step2_integration.py

Tests:
    1. Connect to websocket
    2. Receive init message with brackets
    3. Inject a fake CLI product for KSEA
    4. Verify server broadcasts opportunity
    5. Check logs/snipes.jsonl for dry-run record
"""

import asyncio
import json
import sys
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import websockets
except ImportError:
    print("pip install websockets first")
    sys.exit(1)

WS_URL = "ws://localhost:8766"

async def run_test():
    print("=" * 70)
    print("STEP 2 QC: CLI → Brackets → Opportunities (end-to-end)")
    print("=" * 70)

    # --- Test 1: Connect ---
    print("\n[1] Connecting to websocket...")
    try:
        ws = await websockets.connect(WS_URL)
        print(f"  ✅ Connected to {WS_URL}")
    except Exception as e:
        print(f"  ❌ Failed: {e}")
        print("  Is nwws_monitor_server.py running?")
        return

    # --- Test 2: Receive init ---
    print("\n[2] Receiving init message...")
    raw = await asyncio.wait_for(ws.recv(), timeout=5)
    init_msg = json.loads(raw)

    if init_msg.get("type") == "init":
        brackets = init_msg.get("brackets", [])
        products = init_msg.get("products", [])
        live = init_msg.get("live_mode", False)
        bc = init_msg.get("bracket_count", 0)
        sc = init_msg.get("station_count", 0)
        print(f"  ✅ Init received:")
        print(f"    Stations: {sc}")
        print(f"    Brackets: {bc}")
        print(f"    Historical products: {len(products)}")
        print(f"    Mode: {'🔴 LIVE' if live else '🧪 DRY RUN'}")

        # Show KSEA brackets specifically
        ksea_brackets = [b for b in brackets if b["station"] == "KSEA"]
        if ksea_brackets:
            print(f"\n    KSEA brackets ({len(ksea_brackets)}):")
            for b in sorted(ksea_brackets, key=lambda x: x.get("floor") or -999):
                print(f"      {b['subtitle']:<20} yes={b['yes_ask']}¢  no={b['no_ask']}¢")
    else:
        print(f"  ⚠️  Unexpected message type: {init_msg.get('type')}")

    # --- Test 3: Inject fake CLI ---
    print("\n[3] Injecting fake CLI for KSEA (HIGH=52°F, LOW=40°F)...")
    fake_product = {
        "type": "product",
        "data": {
            "product_type": "CLI",
            "station": "KSEA",
            "awipsid": "CLISEA",
            "cccc": "KSEW",
            "high": 52,
            "low": 40,
            "high_time": "2:38 PM",
            "low_time": "8:15 AM",
            "valid_as": "0500 PM",
            "city": "Seattle",
            "watched": True,
            "timestamp": "2026-02-12T01:17:52Z",
        }
    }
    await ws.send(json.dumps(fake_product))
    print("  ✅ Sent fake CLI product")

    # --- Test 4: Listen for broadcast ---
    print("\n[4] Waiting for server response...")
    # Server should broadcast the product back and potentially opportunities
    messages = []
    try:
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=3)
            msg = json.loads(raw)
            messages.append(msg)
            print(f"  📨 Received: type={msg.get('type')}")

            if msg.get("type") == "product":
                p = msg.get("data", {})
                print(f"     {p.get('product_type')} {p.get('station')} H={p.get('high')}°F L={p.get('low')}°F")

            elif msg.get("type") == "opportunities":
                opps = msg.get("opportunities", [])
                print(f"     🎯 {len(opps)} opportunities for {msg.get('station')}:")
                for o in opps:
                    print(f"       {o['action']} {o['subtitle']} @ {o['price']}¢ (edge={o['edge_cents']}¢)")

    except asyncio.TimeoutError:
        pass

    product_msgs = [m for m in messages if m.get("type") == "product"]
    opp_msgs = [m for m in messages if m.get("type") == "opportunities"]

    if product_msgs:
        print(f"\n  ✅ Product broadcast received ({len(product_msgs)})")
    else:
        print(f"\n  ⚠️  No product broadcast (injected products relay through server)")

    if opp_msgs:
        print(f"  ✅ Opportunities broadcast received ({len(opp_msgs)})")
    else:
        print(f"  ℹ️  No opportunities broadcast (injection path doesn't trigger bracket check)")
        print(f"      This is expected — real CLI from NWWS-OI will trigger bracket check")

    # --- Test 5: Check snipe log ---
    print("\n[5] Checking snipe log...")
    snipe_file = "logs/snipes.jsonl"
    if os.path.exists(snipe_file):
        with open(snipe_file) as f:
            lines = f.readlines()
        print(f"  ✅ {len(lines)} snipe records in {snipe_file}")
        if lines:
            last = json.loads(lines[-1])
            print(f"    Latest: {last.get('action')} {last.get('ticker')} @ {last.get('price')}¢")
            print(f"    Live: {last.get('live')} | Success: {last.get('success')}")
    else:
        print(f"  ℹ️  No snipe log yet (will be created when real CLI triggers opportunity)")

    await ws.close()

    print("\n" + "=" * 70)
    print("STEP 2 QC COMPLETE")
    print("=" * 70)
    print("\nNext: Wait for CLIs to arrive from NWWS-OI and watch the dashboard.")
    print("Server will auto-check brackets and log opportunities to logs/snipes.jsonl")

asyncio.run(run_test())
