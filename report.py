#!/usr/bin/env python3
"""
WX Sniper Trading Report

Quick summary of what the bot has been doing. Reads logs/snipes.jsonl and
prints a digest you can scan in 5 seconds. No Kalshi API calls; pure local
log analysis. Run anytime:

    python report.py             # full report
    python report.py --today     # just today
    python report.py --week      # last 7 days
    python report.py --recent 20 # last 20 snipes

Future work: add `--settle` flag that queries Kalshi for resolution status
of each ticker so we can compute realized P&L. For now, this just shows
what the bot tried to do, at what prices.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

LOG_PATH = Path(__file__).parent / "logs" / "snipes.jsonl"


def load_snipes() -> list[dict]:
    """Load all snipes from logs/snipes.jsonl. Skip malformed lines."""
    if not LOG_PATH.exists():
        print(f"⚠️  No snipes log at {LOG_PATH}")
        print("   Bot has never recorded a snipe. Either it hasn't fired any,")
        print("   or it's running in a different directory.")
        sys.exit(0)

    snipes = []
    with open(LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snipes.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return snipes


def filter_by_window(snipes: list[dict], hours: int | None) -> list[dict]:
    """Return only snipes from the last N hours (None = all)."""
    if hours is None:
        return snipes
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for s in snipes:
        try:
            ts = datetime.fromisoformat(s["time"])
            if ts >= cutoff:
                out.append(s)
        except (KeyError, ValueError):
            continue
    return out


def summarize(snipes: list[dict], label: str) -> None:
    """Print a concise summary of a snipe collection."""
    print(f"\n━━━ {label} ━━━")
    if not snipes:
        print("  (no snipes)")
        return

    # Counts
    total = len(snipes)
    successful = sum(1 for s in snipes if s.get("success"))
    failed = total - successful
    live = sum(1 for s in snipes if s.get("live"))
    dry = total - live

    print(f"  Total snipes:        {total}")
    print(f"    ✓ placed:          {successful}")
    print(f"    ✗ failed:          {failed}")
    print(f"    🔴 live:           {live}")
    print(f"    🧪 dry-run:        {dry}")

    # Side breakdown
    by_side = Counter(s.get("side", "?") for s in snipes)
    print(f"  Side breakdown:      NO={by_side.get('no', 0)}, YES={by_side.get('yes', 0)}")

    # Station breakdown
    by_station = Counter(s.get("station", "?") for s in snipes)
    if by_station:
        top = ", ".join(f"{k}={v}" for k, v in by_station.most_common(5))
        print(f"  Top stations:        {top}")

    # Original ask prices (lower = bigger edge captured)
    asks = [s.get("original_ask") for s in snipes if isinstance(s.get("original_ask"), (int, float))]
    if asks:
        avg_ask = sum(asks) / len(asks)
        print(f"  Original ask price:  min={min(asks)}¢ avg={avg_ask:.0f}¢ max={max(asks)}¢")
        print(f"    (lower = bigger theoretical edge before bot fired)")

    # Latency
    latencies = [s.get("latency_ms") for s in snipes if isinstance(s.get("latency_ms"), (int, float))]
    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        print(f"  Order latency:       avg={avg_latency:.0f}ms max={max(latencies):.0f}ms")

    # Theoretical max profit (if all settle in our favor)
    if asks:
        # Per-contract max profit = 100 - original_ask. We don't know quantity
        # filled or actual settlement here — this is BEST CASE.
        max_per_contract = sum(100 - a for a in asks)
        print(f"  Theoretical max P&L: ${max_per_contract / 100:.2f} (if every snipe wins, 1 contract each)")
        print(f"    (real P&L depends on fill prices + settlements — not yet tracked)")


def show_recent(snipes: list[dict], n: int) -> None:
    """Print the last N snipes in detail."""
    print(f"\n━━━ Last {n} snipes ━━━")
    recent = snipes[-n:] if len(snipes) >= n else snipes
    if not recent:
        print("  (none)")
        return
    for s in recent:
        ts = s.get("time", "?")
        station = s.get("station", "?")
        action = s.get("action", "?")
        ticker = s.get("ticker", "?")
        sub = s.get("subtitle", "?")
        ask = s.get("original_ask", "?")
        success = "✓" if s.get("success") else "✗"
        live = "🔴" if s.get("live") else "🧪"
        print(f"  {ts}  {live}{success}  {station}  {action:<8}  {sub:<14}  ask={ask}¢  ticker={ticker}")


def main():
    parser = argparse.ArgumentParser(description="WX Sniper trading report")
    parser.add_argument("--today", action="store_true", help="Last 24 hours only")
    parser.add_argument("--week", action="store_true", help="Last 7 days only")
    parser.add_argument("--recent", type=int, default=10, help="Show last N snipes (default 10)")
    args = parser.parse_args()

    all_snipes = load_snipes()

    print(f"\n📊 WX Sniper Report — {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"   Source: {LOG_PATH}")

    if args.today:
        summarize(filter_by_window(all_snipes, 24), "Last 24 hours")
    elif args.week:
        summarize(filter_by_window(all_snipes, 24 * 7), "Last 7 days")
    else:
        summarize(filter_by_window(all_snipes, 24), "Last 24 hours")
        summarize(filter_by_window(all_snipes, 24 * 7), "Last 7 days")
        summarize(all_snipes, f"Lifetime ({len(all_snipes)} snipes)")

    show_recent(all_snipes, args.recent)
    print()


if __name__ == "__main__":
    main()
