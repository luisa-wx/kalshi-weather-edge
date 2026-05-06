#!/usr/bin/env python3
"""
Morning Report — overnight health + activity at a glance.

Run when you wake up:
    python3 morning.py

Answers in one screen:
  ✅/❌  Was the bot alive all night?
  ✅/❌  Was NWWS-OI receiving data?
  📊    How many products + snipes happened?
  ⚠️    Any errors that need attention?
"""

import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
LOG_DIR = PROJECT_DIR / "logs"
SNIPES_LOG = LOG_DIR / "snipes.jsonl"
MONITOR_LOG = LOG_DIR / "monitor.log"
PRODUCTS_LOG = LOG_DIR / "cli_dsm_products.jsonl"
DECISIONS_LOG = LOG_DIR / "decisions.jsonl"

NOW = datetime.now(timezone.utc)
DAY_AGO = NOW - timedelta(hours=24)
HOUR_AGO = NOW - timedelta(hours=1)


# ------------- HEALTH CHECKS -------------

def bot_alive() -> tuple[bool, str]:
    """Check if nwws_monitor_server is running. Returns (alive, info_string)."""
    try:
        out = subprocess.check_output(["ps", "aux"], text=True)
        for line in out.splitlines():
            if "nwws_monitor_server.py" in line and "grep" not in line:
                parts = line.split()
                pid = parts[1]
                started = parts[8]
                return True, f"PID {pid} (started {started})"
    except Exception as e:
        return False, f"check failed: {e}"
    return False, "no process found"


def last_log_event_time() -> datetime | None:
    """Read the last timestamp from monitor.log."""
    if not MONITOR_LOG.exists():
        return None
    try:
        with open(MONITOR_LOG) as f:
            for line in reversed(f.readlines()[-200:]):
                m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                if m:
                    return datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return None


def count_log_lines(pattern: str, since: datetime | None = None) -> int:
    """Count lines in monitor.log matching pattern (regex), optionally only since a time."""
    if not MONITOR_LOG.exists():
        return 0
    n = 0
    try:
        with open(MONITOR_LOG) as f:
            for line in f:
                if since:
                    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if m:
                        ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
                        if ts < since:
                            continue
                if re.search(pattern, line):
                    n += 1
    except Exception:
        pass
    return n


def count_recent_products() -> tuple[int, int, dict]:
    """Return (total_products, last_24h_products, by_type_24h_breakdown)."""
    if not PRODUCTS_LOG.exists():
        return 0, 0, {}
    total, recent = 0, 0
    by_type = Counter()
    try:
        with open(PRODUCTS_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    p = json.loads(line)
                    ts_str = p.get("timestamp", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str)
                        if ts >= DAY_AGO:
                            recent += 1
                            by_type[p.get("product_type", "?")] += 1
                except Exception:
                    continue
    except Exception:
        pass
    return total, recent, dict(by_type)


def load_recent_snipes() -> list[dict]:
    if not SNIPES_LOG.exists():
        return []
    out = []
    try:
        with open(SNIPES_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    s = json.loads(line)
                    ts = datetime.fromisoformat(s["time"])
                    if ts >= DAY_AGO:
                        out.append(s)
                except Exception:
                    continue
    except Exception:
        pass
    return out


def load_recent_decisions() -> list[dict]:
    """Load decisions.jsonl entries from the last 24h."""
    if not DECISIONS_LOG.exists():
        return []
    out = []
    try:
        with open(DECISIONS_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    ts_str = d.get("time", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str)
                        if ts >= DAY_AGO:
                            out.append(d)
                except Exception:
                    continue
    except Exception:
        pass
    return out


# ------------- RENDER -------------

def fmt_age(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    delta = NOW - dt
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60}m ago"
    return f"{s // 86400}d ago"


def status(ok: bool) -> str:
    return "✅" if ok else "❌"


def main():
    print()
    print("━" * 60)
    print(f"  🌅 WX SNIPER — MORNING REPORT")
    print(f"  {NOW.strftime('%Y-%m-%d %H:%M UTC')}")
    print("━" * 60)

    # Health
    print("\n📡 HEALTH")
    alive, info = bot_alive()
    print(f"  {status(alive)} Bot process: {info if alive else 'NOT RUNNING'}")

    last_log = last_log_event_time()
    log_recent = last_log is not None and (NOW - last_log).total_seconds() < 3600
    print(f"  {status(log_recent)} Last log entry: {fmt_age(last_log)}")

    # NWWS-OI activity (proxy: did products arrive in last hour?)
    _, recent_products, by_type = count_recent_products()
    last_hour_count = sum(1 for s in load_recent_snipes() if datetime.fromisoformat(s["time"]) >= HOUR_AGO)

    nwws_ok = recent_products > 0
    print(f"  {status(nwws_ok)} NWWS-OI receiving: {recent_products} products in last 24h")

    # Errors / watchdog firings
    errors_24h = count_log_lines(r"\[ERROR\]", since=DAY_AGO)
    watchdog_fires = count_log_lines(r"WATCHDOG.*stale", since=DAY_AGO)
    keepalive_traces = count_log_lines(r"Whitespace Keepalive", since=DAY_AGO)
    reconnects = count_log_lines(r"Auto-reconnect engaged", since=DAY_AGO)

    print(f"  {status(errors_24h == 0)} Errors in last 24h: {errors_24h}")
    if watchdog_fires:
        print(f"  ⚠️  Watchdog forced reconnect: {watchdog_fires} time(s)")
    if reconnects:
        print(f"  ℹ️  NWWS-OI reconnected: {reconnects} time(s) (normal)")
    if keepalive_traces:
        print(f"  ⚠️  Whitespace Keepalive traces: {keepalive_traces} (slixmpp race — investigate)")

    # Activity
    print("\n📊 ACTIVITY (last 24h)")
    print(f"  NWS products received: {recent_products}")
    if by_type:
        for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"    • {t}: {c}")

    snipes = load_recent_snipes()
    print(f"  Snipes fired: {len(snipes)}")

    # Snipe breakdown
    if snipes:
        live = sum(1 for s in snipes if s.get("live"))
        dry = len(snipes) - live
        success = sum(1 for s in snipes if s.get("success"))
        by_side = Counter(s.get("side", "?") for s in snipes)
        by_station = Counter(s.get("station", "?") for s in snipes)
        asks = [s.get("original_ask") for s in snipes if isinstance(s.get("original_ask"), (int, float))]

        print(f"    • Live: {live}, Dry-run: {dry}")
        print(f"    • Order placed successfully: {success}/{len(snipes)}")
        print(f"    • Side: NO={by_side.get('no', 0)}, YES={by_side.get('yes', 0)}")
        if by_station:
            top = ", ".join(f"{k}={v}" for k, v in by_station.most_common(5))
            print(f"    • Stations: {top}")
        if asks:
            avg_ask = sum(asks) / len(asks)
            est_max = sum(100 - a for a in asks) / 100
            print(f"    • Original ask: min={min(asks)}¢ avg={avg_ask:.0f}¢ max={max(asks)}¢")
            print(f"    • Theoretical max P&L if all win: ${est_max:.2f} (1 contract each)")

        print(f"\n  Recent snipes:")
        for s in snipes[-10:]:
            ts = s.get("time", "?")[:19]
            station = s.get("station", "?")
            action = s.get("action", "?")
            sub = s.get("subtitle", "?")
            ask = s.get("original_ask", "?")
            mark = "🔴" if s.get("live") else "🧪"
            ok = "✓" if s.get("success") else "✗"
            print(f"    {ts}  {mark}{ok}  {station:<5} {action:<8} {sub:<14} ask={ask}¢")
    else:
        print("    (none — see DECISIONS section below for why.)")

    # ----- DECISIONS BREAKDOWN (the new diagnostic data) -----
    decisions = load_recent_decisions()
    print(f"\n🔍 DECISIONS (last 24h): {len(decisions)} CLI evaluations")

    if decisions:
        # Bucket by skip_reason
        skip_counts = Counter(d.get("skip_reason") or ("ok" if d.get("snipes_fired", 0) > 0 else "no_snipe") for d in decisions)
        for reason, n in skip_counts.most_common():
            label = {
                "ok": "✅ snipes fired",
                "no_snipe": "⚠️  no snipe (unknown reason)",
                "cli_date_mismatch": "  CLI date didn't match brackets",
                "no_transitions": "  No bracket transitions",
                "price_filter_or_no_active_bid": "  Brackets dead but already at 100¢ (market converged before us)",
                "station_not_loaded": "  Station not in BRACKETS dict",
                "opps_found_but_snipes_failed": "❌ opportunities found but order failed",
            }.get(reason, f"  {reason}")
            print(f"    {n}× {label}")

        # The KEY metric: any rejected brackets with no_ask < 90 → real edge existed
        edge_moments = []
        for d in decisions:
            for b in d.get("resolved_brackets_no_opp", []):
                no_ask = b.get("no_ask")
                yes_ask = b.get("yes_ask")
                # For dead brackets: low no_ask = edge existed
                # For locked: low yes_ask = edge existed
                price = no_ask if b.get("status") == "dead" else yes_ask
                if isinstance(price, (int, float)) and 1 < price < 90:
                    edge_moments.append({
                        "time": d.get("time", "")[:19],
                        "station": d.get("station"),
                        "subtitle": b.get("subtitle"),
                        "status": b.get("status"),
                        "price": price,
                        "edge": 100 - price,
                    })

        if edge_moments:
            print(f"\n  ✨ {len(edge_moments)} REJECTED BRACKETS HAD REAL EDGE (no_ask < 90)")
            print(f"     These would have been profitable trades had MAX_PRICE been higher:")
            for m in edge_moments[:10]:
                print(f"       {m['time']}  {m['station']:<5} {m['subtitle']:<14} {m['status']:<6} @ {m['price']}¢ (edge {m['edge']}¢)")
            if len(edge_moments) > 10:
                print(f"       ... and {len(edge_moments) - 10} more")
        else:
            print(f"\n  📊 Of all rejected brackets, NONE had no_ask < 90.")
            print(f"     Market was fully converged (≥90¢) every time the bot saw a CLI.")

    # Bottom line
    print()
    print("━" * 60)
    overall_ok = alive and log_recent and errors_24h < 5

    edge_count = len([d for d in decisions for b in d.get("resolved_brackets_no_opp", [])
                      if isinstance(b.get("no_ask"), (int, float)) and 1 < b["no_ask"] < 90])

    if overall_ok and len(snipes) > 0:
        print("  🎯 BOTTOM LINE: bot ran and FIRED snipes. Audit them above.")
    elif overall_ok and edge_count > 0:
        print(f"  ✨ BOTTOM LINE: bot ran cleanly, no snipes, BUT {edge_count} rejected brackets")
        print(f"     had real edge (no_ask < 90). Tune MAX_PRICE upward to capture them.")
    elif overall_ok and len(decisions) > 0 and recent_products > 0:
        print(f"  ✅ BOTTOM LINE: bot ran cleanly. {len(decisions)} CLI evaluations, 0 snipes.")
        print(f"     Every rejected bracket was at ≥90¢ — market converges before our CLI.")
        print(f"     Strategy may need faster data (METARs) or different game.")
    elif overall_ok and recent_products > 0:
        print("  ✅ BOTTOM LINE: bot ran cleanly. No CLIs hit our stations yet today.")
    elif overall_ok and recent_products == 0:
        print("  ⚠️  BOTTOM LINE: bot is alive but received no NWS products.")
        print("     NWWS-OI may be silently stalling. Check `tail logs/monitor.log`.")
    else:
        print("  ❌ BOTTOM LINE: something's broken. Check logs immediately.")
    print("━" * 60)
    print()


if __name__ == "__main__":
    main()
