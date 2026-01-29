#!/usr/bin/env python3
"""
QC Check - Verify smart_poller.py is working correctly
Run this after deploying to confirm everything is good.
"""

import sys
from datetime import datetime, timezone

print("="*60)
print("WX SNIPER - DEPLOYMENT QC CHECK")
print("="*60)

# 1. Check imports work
print("\n1. Checking imports...")
try:
    from smart_poller import SmartPoller
    print("   ✅ smart_poller imports OK")
except Exception as e:
    print(f"   ❌ FAILED: {e}")
    sys.exit(1)

# 2. Check timing logic
print("\n2. Checking timing logic...")
try:
    p = SmartPoller(dry_run=True)
    s = p._get_synoptic_state()
    
    now_utc = f"{s['current_hour']:02d}:{s['current_minute']:02d}Z"
    now_est = f"{(s['current_hour']-5)%24}:{s['current_minute']:02d} EST"
    
    print(f"   Current time: {now_utc} ({now_est})")
    print(f"   Prep window: {s['is_prep_window']}")
    print(f"   Hot window: {s['is_hot_window']}")
    print(f"   Minutes to next hot: {s['minutes_to_hot']}")
    
    # Find next hot in EST
    for h in [5, 11, 17, 23]:
        if h > s['current_hour'] or (h == s['current_hour'] and s['current_minute'] < 52):
            next_hot_utc = h
            break
    else:
        next_hot_utc = 5
    
    next_hot_est = (next_hot_utc - 5) % 24
    print(f"   Next hot window: {next_hot_utc}:52Z = {next_hot_est}:52 EST")
    print("   ✅ Timing logic OK")
except Exception as e:
    print(f"   ❌ FAILED: {e}")
    sys.exit(1)

# 3. Check Kalshi API
print("\n3. Checking Kalshi API...")
try:
    balance = p.kalshi.get_balance()
    bal = balance.get('balance', 0) / 100
    print(f"   Balance: ${bal:.2f}")
    print("   ✅ Kalshi API OK")
except Exception as e:
    print(f"   ❌ FAILED: {e}")
    sys.exit(1)

# 4. Check Aviation Weather API
print("\n4. Checking Aviation Weather API...")
try:
    resp = p.aviation.fetch_metar('KNYC')
    if resp and resp.raw_text:
        print(f"   METAR: {resp.raw_text[:50]}...")
        print("   ✅ Aviation API OK")
    else:
        print("   ❌ No METAR returned")
        sys.exit(1)
except Exception as e:
    print(f"   ❌ FAILED: {e}")
    sys.exit(1)

# 5. Check watchlist building
print("\n5. Checking watchlist build...")
try:
    state = list(p.market_states.values())[0]  # First station
    watchlist = p.build_watchlist(state)
    print(f"   ✅ Built watchlist: {len(watchlist)} brackets")
except Exception as e:
    print(f"   ❌ FAILED: {e}")
    sys.exit(1)

# 6. Summary
print("\n" + "="*60)
print("✅ ALL CHECKS PASSED - POLLER IS READY")
print("="*60)
print(f"\nNext hot window: {next_hot_utc}:52Z = {next_hot_est}:52 EST")
print(f"Mode: {'🧪 DRY RUN' if p.dry_run else '💰 LIVE TRADING'}")
print(f"Max price: {p.max_price_cents}¢")
print("\nThe poller will:")
print(f"  - Wake up at {next_hot_utc}:48Z ({(next_hot_utc-5)%24}:48 EST) to build watchlists")
print(f"  - Poll METARs every 5s from {next_hot_utc}:52Z-{(next_hot_utc+1)%24}:02Z")
print(f"  - Execute trades on any locked NOs ≤ {p.max_price_cents}¢")
print("="*60)
