"""
NWWS-OI WebSocket Monitor Server — v2 with Kalshi Integration

Runs the NWWS-OI XMPP client, loads Kalshi brackets on startup,
and when CLI/DSM products arrive, resolves brackets and broadcasts
opportunities to the dashboard.

Usage:
    python3 nwws_monitor_server.py
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional

import slixmpp
from slixmpp.xmlstream import ET

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nwws_monitor")

NWWS_ROOM = "nwws@conference.nwws-oi.weather.gov"
NWWS_OI_NS = "nwws-oi"

# Import station config and Kalshi client
from stations import STATIONS

CLI_SUFFIX_TO_STATION = {}
for k, v in STATIONS.items():
    suffix = k[1:]  # KNYC -> NYC
    CLI_SUFFIX_TO_STATION[suffix] = k

DSM_SUFFIX_TO_STATION = {k[1:]: k for k in STATIONS}
WATCHED_DSM_IDS = {f"DSM{k[1:]}" for k in STATIONS}

# ---------------------------------------------------------------------------
# Persistent product logging
# ---------------------------------------------------------------------------

PRODUCT_FILE = "logs/cli_dsm_products.jsonl"
os.makedirs("logs", exist_ok=True)


def save_product(product: dict):
    try:
        with open(PRODUCT_FILE, "a") as f:
            f.write(json.dumps(product) + "\n")
    except Exception as e:
        logger.error(f"Failed to save product: {e}")


def load_products(max_age_hours: int = 48) -> list:
    """Load products from JSONL, filtering to last max_age_hours."""
    products = []
    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)

    if os.path.exists(PRODUCT_FILE):
        kept = 0
        total = 0
        with open(PRODUCT_FILE, "r") as f:
            for line in f:
                total += 1
                try:
                    p = json.loads(line.strip())
                    # Parse timestamp and filter
                    ts = p.get("timestamp", "")
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if dt.timestamp() < cutoff:
                                continue
                        except (ValueError, TypeError):
                            pass
                    products.append(p)
                    kept += 1
                except Exception:
                    pass
        logger.info(f"Loaded {kept}/{total} products from {PRODUCT_FILE} (last {max_age_hours}h)")
    return products


def cleanup_product_file(max_age_hours: int = 48):
    """Rewrite JSONL file, removing products older than max_age_hours."""
    if not os.path.exists(PRODUCT_FILE):
        return

    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)
    kept = []

    with open(PRODUCT_FILE, "r") as f:
        for line in f:
            try:
                p = json.loads(line.strip())
                ts = p.get("timestamp", "")
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if dt.timestamp() < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass
                kept.append(line.strip())
            except Exception:
                pass

    with open(PRODUCT_FILE, "w") as f:
        for line in kept:
            f.write(line + "\n")

    logger.info(f"Cleaned product file: kept {len(kept)} records (last {max_age_hours}h)")


# ---------------------------------------------------------------------------
# Diagnostic logging — added 2026-05-06
# ---------------------------------------------------------------------------
# To diagnose "why didn't the bot snipe", we log:
#   1. decisions.jsonl — every CLI/DSM evaluation, whether or not it fired
#   2. prices.jsonl — periodic (5 min) snapshot of every bracket's prices
# Without these, "0 snipes" is ambiguous: was there no edge? wrong filter?
# bug? Now we can answer post-hoc by reading these logs.

DECISION_FILE = "logs/decisions.jsonl"
PRICES_FILE = "logs/prices.jsonl"


def write_decision(decision: dict):
    """Append a decision record (every CLI evaluation, snipe or not)."""
    decision.setdefault("time", datetime.now(timezone.utc).isoformat())
    try:
        with open(DECISION_FILE, "a") as f:
            f.write(json.dumps(decision, default=str) + "\n")
    except Exception as e:
        logger.error(f"Failed to write decision: {e}")


def write_price_snapshot(brackets_dict: dict):
    """Append a snapshot of all brackets' current prices (for replay)."""
    snapshot = {
        "time": datetime.now(timezone.utc).isoformat(),
        "stations": {},
    }
    for station, data in brackets_dict.items():
        sn = {"suffix": data.get("suffix"), "suffix_y": data.get("suffix_y"), "brackets": []}
        for key in ("high", "low", "high_y", "low_y"):
            for b in data.get(key, []):
                sn["brackets"].append({
                    "key": key,
                    "ticker": b.ticker,
                    "subtitle": b.subtitle,
                    "status": b.status,
                    "yes_ask": b.yes_ask,
                    "no_ask": b.no_ask,
                    "yes_bid": b.yes_bid,
                    "no_bid": b.no_bid,
                })
        snapshot["stations"][station] = sn
    try:
        with open(PRICES_FILE, "a") as f:
            f.write(json.dumps(snapshot, default=str) + "\n")
    except Exception as e:
        logger.error(f"Failed to write price snapshot: {e}")


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_cli(raw: str) -> dict:
    result = {
        "high": None, "low": None,
        "high_time": None, "low_time": None,
        "valid_as": None, "city_name": None,
    }

    city_match = re.search(r'\.\.\.THE (.+?) CLIMATE SUMMARY', raw)
    if city_match:
        result["city_name"] = city_match.group(1)

    # Extract the CLI date from "CLIMATE SUMMARY FOR FEBRUARY 11 2026"
    date_match = re.search(r'CLIMATE SUMMARY FOR\s+(\w+\s+\d+\s+\d{4})', raw, re.IGNORECASE)
    if date_match:
        result["valid_as"] = date_match.group(1)
    else:
        # Fallback: try "FOR TUESDAY FEBRUARY 11 2026" pattern
        date_match2 = re.search(r'FOR\s+\w+DAY\s+(\w+\s+\d+\s+\d{4})', raw, re.IGNORECASE)
        if date_match2:
            result["valid_as"] = date_match2.group(1)

    # Also check if it's preliminary — afternoon CLIs say "VALID TODAY AS OF"
    if re.search(r'VALID\s+TODAY\s+AS\s+OF', raw, re.IGNORECASE):
        result["is_preliminary"] = True
    else:
        result["is_preliminary"] = False

    temp_section = raw[raw.find("TEMPERATURE (F)"):] if "TEMPERATURE (F)" in raw else ""

    max_match = re.search(r'MAXIMUM\s+(-?\d+)\s+([\d:]+\s*[AP]M|MM)', temp_section)
    if max_match:
        result["high"] = int(max_match.group(1))
        result["high_time"] = max_match.group(2).strip()

    min_match = re.search(r'MINIMUM\s+(-?\d+)\s+([\d:]+\s*[AP]M|MM)', temp_section)
    if min_match:
        result["low"] = int(min_match.group(1))
        result["low_time"] = min_match.group(2).strip()

    return result


def parse_dsm(raw: str) -> dict:
    result = {
        "high": None, "low": None,
        "high_time": None, "low_time": None,
        "station_in_dsm": None,
    }

    ds_match = re.search(
        r'([A-Z]{4})\s+DS\s+(\d{4})\s+(\d{2}/\d{2})\s+'
        r'(\d{2,3})(\d{4})/\s*'
        r'(\d{2,3})(\d{4})?/?',
        raw
    )
    if ds_match:
        result["station_in_dsm"] = ds_match.group(1)
        result["high"] = int(ds_match.group(4))
        hh, mm = ds_match.group(5)[:2], ds_match.group(5)[2:]
        result["high_time"] = f"{int(hh)}:{mm}"
        result["low"] = int(ds_match.group(6))
        if ds_match.group(7):
            lh, lm = ds_match.group(7)[:2], ds_match.group(7)[2:]
            result["low_time"] = f"{int(lh)}:{lm}"

    return result


def resolve_cli_station(awipsid: str) -> Optional[str]:
    suffix = awipsid[3:].upper()
    return CLI_SUFFIX_TO_STATION.get(suffix)


def resolve_dsm_station(awipsid: str) -> Optional[str]:
    suffix = awipsid[3:].upper()
    return DSM_SUFFIX_TO_STATION.get(suffix)


# ---------------------------------------------------------------------------
# Kalshi Bracket State
# ---------------------------------------------------------------------------

BRACKETS = {}
KALSHI_CLIENT = None
KALSHI_WS = None
OPPORTUNITIES = []
SNIPE_LOG = []
PROCESSED_TICKERS = set()

LIVE_MODE = os.environ.get("LIVE_MODE", "false").lower() == "true"
ORDER_SIZE = int(os.environ.get("ORDER_SIZE", "10"))
MAX_PRICE = int(os.environ.get("MAX_PRICE", "98"))


def init_kalshi():
    global KALSHI_CLIENT, BRACKETS

    try:
        from kalshi_client import KalshiClient, load_all_brackets
        KALSHI_CLIENT = KalshiClient()

        if KALSHI_CLIENT.private_key:
            # Connection warming: pre-establish TLS + auth on startup so the
            # first real snipe doesn't pay the ~150ms handshake cost.
            try:
                KALSHI_CLIENT.get_exchange_status()
                logger.info(f"Kalshi: connection warm ({KALSHI_CLIENT.last_latency_ms:.0f}ms)")
            except Exception as e:
                logger.warning(f"Kalshi: connection warming failed: {e}")

            BRACKETS = load_all_brackets(KALSHI_CLIENT, STATIONS)
            total_h = sum(len(v["high"]) for v in BRACKETS.values())
            total_l = sum(len(v["low"]) for v in BRACKETS.values())
            logger.info(f"Kalshi: {total_h} HIGH + {total_l} LOW brackets loaded")
        else:
            logger.warning("Kalshi: no credentials, bracket loading skipped")
    except Exception as e:
        logger.error(f"Kalshi init failed: {e}")
        KALSHI_CLIENT = None


def _cli_date_matches_brackets(station: str, valid_as: str) -> str | None:
    """Check if CLI date matches today's OR yesterday's bracket date for this station.

    valid_as is like 'FEBRUARY 11 2026' from CLI parser.
    Brackets have a 'suffix' (today, e.g. '26MAY06') and 'suffix_y' (yesterday).

    Returns:
      'today' if CLI matches today's bracket date → use BRACKETS[station]['high'/'low']
      'yesterday' if CLI matches yesterday's date → use BRACKETS[station]['high_y'/'low_y']
      None if neither matches → skip the CLI

    The 'yesterday' case is critical: overnight settlement CLIs drop early
    AM local time (typically 12:30-5 AM) and report on the PREVIOUS day's
    high/low. A bot that just rolled over to today's brackets would miss
    these without yesterday's brackets also loaded.
    """
    if not valid_as or station not in BRACKETS:
        return None

    bracket_suffix = BRACKETS[station].get("suffix", "")
    bracket_suffix_y = BRACKETS[station].get("suffix_y", "")
    if not bracket_suffix:
        return "today"  # No suffix info, fall back to old assume-match behavior

    try:
        from datetime import datetime as dt_cls
        cli_date = dt_cls.strptime(valid_as.strip(), "%B %d %Y")
        cli_suffix = cli_date.strftime("%y%b%d").upper()

        if cli_suffix == bracket_suffix:
            return "today"
        if cli_suffix == bracket_suffix_y:
            logger.info(f"📅 {station}: CLI for yesterday ({cli_suffix}), applying to yesterday's brackets")
            return "yesterday"

        logger.info(
            f"⏭️ {station}: CLI date {valid_as} ({cli_suffix}) matches "
            f"neither today ({bracket_suffix}) nor yesterday ({bracket_suffix_y}), skipping"
        )
        return None
    except (ValueError, AttributeError) as e:
        logger.warning(f"Could not parse CLI date '{valid_as}': {e}")
        return "today"  # Can't parse, fall back to today's brackets


def check_cli_opportunities(station: str, cli_high: int = None, cli_low: int = None, valid_as: str = None) -> list:
    """When a CLI/DSM arrives, eliminate dead brackets and find opportunities.

    Uses ELIMINATION ONLY:
      - Marks brackets as dead if the observed temp proves they can't win
      - BUY NO on dead brackets below max_price
      - If all but one bracket eliminated → last-man-standing BUY YES

    Never asserts a bracket has won just because the temp is in its range.

    Routes to TODAY's or YESTERDAY's bracket set based on the CLI's valid_as
    date — critical for catching overnight settlement CLIs that report on the
    previous day's data.
    """
    global OPPORTUNITIES

    # Build decision record up-front so every code path writes one.
    decision = {
        "station": station,
        "cli_high": cli_high,
        "cli_low": cli_low,
        "valid_as": valid_as,
        "max_price": MAX_PRICE,
    }

    if not BRACKETS or station not in BRACKETS:
        decision["match"] = None
        decision["skip_reason"] = "station_not_loaded"
        decision["snipes_fired"] = 0
        write_decision(decision)
        return []

    # Determine which bracket set this CLI applies to.
    match = _cli_date_matches_brackets(station, valid_as) if valid_as else "today"
    decision["match"] = match
    if not match:
        decision["skip_reason"] = "cli_date_mismatch"
        decision["snipes_fired"] = 0
        write_decision(decision)
        return []

    high_key = "high" if match == "today" else "high_y"
    low_key = "low" if match == "today" else "low_y"

    # Snapshot bracket state BEFORE elimination — for retroactive analysis.
    decision["brackets_before"] = []
    for sig_type, key in [("high", high_key), ("low", low_key)]:
        for b in BRACKETS[station].get(key, []):
            decision["brackets_before"].append({
                "ticker": b.ticker,
                "subtitle": b.subtitle,
                "signal": sig_type,
                "status": b.status,
                "no_ask": b.no_ask,
                "yes_ask": b.yes_ask,
            })

    try:
        from kalshi_client import find_opportunities, execute_snipe

        station_data = BRACKETS[station]
        resolved_count = 0

        # Eliminate HIGH brackets (today's or yesterday's based on CLI date)
        if cli_high is not None:
            for b in station_data.get(high_key, []):
                new_status = b.check_temp_intraday(cli_high)
                if new_status == "dead" and b.status == "open":
                    b.status = "dead"
                    resolved_count += 1

        # Eliminate LOW brackets
        if cli_low is not None:
            for b in station_data.get(low_key, []):
                new_status = b.check_temp_intraday(cli_low)
                if new_status == "dead" and b.status == "open":
                    b.status = "dead"
                    resolved_count += 1

        # Check for last-man-standing — same bracket set as elimination
        for sig_type, key in (("high", high_key), ("low", low_key)):
            group = station_data.get(key, [])
            alive = [b for b in group if b.status != "dead"]
            if len(alive) == 1 and alive[0].status == "open":
                alive[0].status = "locked"
                resolved_count += 1
                logger.info(f"🏆 {station} {sig_type.upper()} ({match}): LAST-MAN-STANDING → {alive[0].subtitle}")

        if resolved_count > 0:
            logger.info(f"📊 {station}: {resolved_count} brackets resolved (H={cli_high} L={cli_low})")

        # Broadcast updated brackets
        asyncio.ensure_future(broadcast({
            "type": "brackets_updated",
            "brackets": get_brackets_summary(),
        }))

        # Find cheap opportunities — pass the right bracket set.
        # find_opportunities reads brackets[station]["high"/"low"], so when
        # the CLI is for yesterday, build a temp dict that puts yesterday's
        # brackets in the standard positions for this station.
        if match == "yesterday":
            brackets_for_search = dict(BRACKETS)
            brackets_for_search[station] = {
                **BRACKETS[station],
                "high": BRACKETS[station].get("high_y", []),
                "low": BRACKETS[station].get("low_y", []),
            }
        else:
            brackets_for_search = BRACKETS

        opps = find_opportunities(
            brackets_for_search,
            cli_high=cli_high,
            cli_low=cli_low,
            station=station,
            max_price=MAX_PRICE,
        )

        decision["resolved_count"] = resolved_count
        decision["opportunities_found"] = len(opps)

        snipes_fired = 0
        if opps:
            logger.info(f"🎯 {station}: {len(opps)} opportunities found!")
            for opp in opps:
                b = opp["bracket"]
                logger.info(f"  {opp['action']} {b.subtitle} @ {opp['price']}¢ (edge={opp['edge_cents']}¢)")

                record = execute_snipe(
                    KALSHI_CLIENT,
                    opp,
                    order_size=ORDER_SIZE,
                    live_mode=LIVE_MODE,
                    processed_tickers=PROCESSED_TICKERS,
                    kalshi_ws=KALSHI_WS,
                )
                SNIPE_LOG.append(record)
                if record.get("success"):
                    snipes_fired += 1

                try:
                    with open("logs/snipes.jsonl", "a") as f:
                        f.write(json.dumps(record) + "\n")
                except Exception:
                    pass

            OPPORTUNITIES.extend(opps)
            if len(OPPORTUNITIES) > 100:
                OPPORTUNITIES = OPPORTUNITIES[-100:]

        decision["snipes_fired"] = snipes_fired

        # Diagnose why we didn't snipe even when transitions occurred.
        # If brackets were resolved but opps==0, it's the price filter rejecting them.
        if resolved_count > 0 and len(opps) == 0:
            decision["skip_reason"] = "price_filter_or_no_active_bid"
            # Capture which brackets transitioned and at what price
            decision["resolved_brackets_no_opp"] = []
            for sig_type, key in [("high", high_key), ("low", low_key)]:
                for b in BRACKETS[station].get(key, []):
                    if b.status in ("dead", "locked"):
                        decision["resolved_brackets_no_opp"].append({
                            "ticker": b.ticker,
                            "subtitle": b.subtitle,
                            "signal": sig_type,
                            "status": b.status,
                            "no_ask": b.no_ask,
                            "yes_ask": b.yes_ask,
                            "above_max_price": (
                                (b.no_ask is not None and b.no_ask > MAX_PRICE)
                                if b.status == "dead"
                                else (b.yes_ask is not None and b.yes_ask > MAX_PRICE)
                            ),
                        })
        elif resolved_count == 0:
            decision["skip_reason"] = "no_transitions"
        elif snipes_fired == 0 and len(opps) > 0:
            decision["skip_reason"] = "opps_found_but_snipes_failed"

        write_decision(decision)
        return opps

    except Exception as e:
        logger.error(f"Opportunity check failed: {e}")
        decision["error"] = str(e)
        write_decision(decision)
        return []


def get_brackets_summary() -> list:
    summary = []
    for station, data in BRACKETS.items():
        cfg = STATIONS.get(station, {})
        local_info = data.get("local_info", {})
        suffix = data.get("suffix", "")
        for signal_type in ("high", "low"):
            for b in data.get(signal_type, []):
                summary.append({
                    "station": station,
                    "city": cfg.get("city", station),
                    "signal_type": signal_type,
                    "ticker": b.ticker,
                    "subtitle": b.subtitle,
                    "floor": b.floor_strike,
                    "cap": b.cap_strike,
                    "yes_ask": b.yes_ask,
                    "no_ask": b.no_ask,
                    "yes_bid": b.yes_bid,
                    "no_bid": b.no_bid,
                    "status": b.status,
                    "traded": b.traded,
                    "local_time": local_info.get("local_time", ""),
                    "local_date": local_info.get("local_date", ""),
                    "suffix": suffix,
                })
    return summary


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------

CLIENTS = set()
PRODUCT_LOG = []
MAX_LOG = 2000  # ~48h worth at typical volume


async def broadcast(message: dict):
    global CLIENTS
    payload = json.dumps(message)
    dead = set()
    for ws in CLIENTS:
        try:
            await ws.send(payload)
        except Exception:
            dead.add(ws)
    CLIENTS -= dead


async def ws_handler(websocket):
    CLIENTS.add(websocket)
    logger.info(f"Dashboard connected ({len(CLIENTS)} total)")

    try:
        # Build cliData from product history (latest CLI/DSM per watched station)
        cli_data = {}
        for p in PRODUCT_LOG:
            if p.get("watched") and p.get("station"):
                station = p["station"]
                # Always update with latest product for this station
                cli_data[station] = {
                    "high": p.get("high"),
                    "low": p.get("low"),
                    "high_time": p.get("high_time"),
                    "low_time": p.get("low_time"),
                    "product_type": p.get("product_type"),
                    "valid_as": p.get("valid_as"),
                    "timestamp": p.get("timestamp"),
                }

        await websocket.send(json.dumps({
            "type": "init",
            "products": PRODUCT_LOG,
            "brackets": get_brackets_summary(),
            "cliData": cli_data,
            "opportunities": [
                {
                    "action": o["action"],
                    "price": o["price"],
                    "edge_cents": o["edge_cents"],
                    "reason": o["reason"],
                    "station": o["bracket"].station,
                    "subtitle": o["bracket"].subtitle,
                }
                for o in OPPORTUNITIES[-50:]
            ],
            "snipes": SNIPE_LOG[-50:],
            "live_mode": LIVE_MODE,
            "station_count": len(STATIONS),
            "bracket_count": sum(
                len(v["high"]) + len(v["low"]) for v in BRACKETS.values()
            ),
        }))

        async for msg in websocket:
            try:
                data = json.loads(msg)
                if data.get("type") == "product":
                    product = data["data"]
                    PRODUCT_LOG.append(product)
                    save_product(product)
                    await broadcast(data)
                elif data.get("type") == "refresh_brackets":
                    init_kalshi()
                    await websocket.send(json.dumps({
                        "type": "brackets_updated",
                        "brackets": get_brackets_summary(),
                    }))
            except Exception:
                pass
    except Exception:
        pass
    finally:
        CLIENTS.discard(websocket)
        logger.info(f"Dashboard disconnected ({len(CLIENTS)} total)")


# ---------------------------------------------------------------------------
# NWWS-OI XMPP Client
# ---------------------------------------------------------------------------

class NWWSMonitorClient(slixmpp.ClientXMPP):
    def __init__(self, jid, password):
        super().__init__(jid, password)
        self.room = NWWS_ROOM
        self.nick = jid.split("@")[0]
        self.register_plugin("xep_0199")

        # Auto-reconnect on socket-level disconnects (slixmpp built-in).
        self.auto_reconnect = True

        # Track connect params so _safe_reconnect() can re-establish.
        self._connect_params = None

        # Watchdog config — if no products received in this long, the
        # connection is treated as silently dead and force-disconnected.
        self.stale_threshold_seconds = 600  # 10 min

        self.add_event_handler("session_start", self._on_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("disconnected", self._on_disconnected)
        self.add_event_handler("connection_failed", self._on_conn_failed)

        self.stats = {
            "total": 0,
            "cli": 0,
            "dsm": 0,
            "last_product_time": None,
            "connected_at": None,
            "reconnects": 0,
        }

    def connect(self, address=None, **kwargs):
        """Override connect to remember address for reconnect."""
        if address is not None:
            self._connect_params = address
        return super().connect(address, **kwargs)

    def is_stale(self) -> bool:
        """True if connection appears stalled — no products in stale_threshold_seconds.

        Used by watchdog_loop in main() to detect silent NWWS-OI stalls
        (TCP alive but server stopped sending data).
        """
        if not self.stats["last_product_time"]:
            # 5-min grace period from connection time before declaring stale
            if self.stats["connected_at"]:
                connected_at = datetime.fromisoformat(self.stats["connected_at"])
                elapsed = (datetime.now(timezone.utc) - connected_at).total_seconds()
                return elapsed > 300
            return False
        last_product = datetime.fromisoformat(self.stats["last_product_time"])
        elapsed = (datetime.now(timezone.utc) - last_product).total_seconds()
        return elapsed > self.stale_threshold_seconds

    def _safe_reconnect(self):
        """Force a fresh reconnect using last known connection params."""
        if self._connect_params:
            try:
                logger.info(f"Reconnecting to NWWS-OI: {self._connect_params}")
                self.connect(self._connect_params)
            except Exception as e:
                logger.error(f"Reconnect failed: {e}, retrying in 60s...")
                asyncio.get_event_loop().call_later(60, self._safe_reconnect)

    async def _on_start(self, event):
        logger.info("NWWS-OI session started, joining room...")
        await self.get_roster()
        self.send_presence()
        pres = self.make_presence(pto=f"{self.room}/{self.nick}")
        ET.SubElement(pres.xml, '{http://jabber.org/protocol/muc}x')
        pres.send()
        logger.info("Joined NWWS-OI chatroom")
        self.stats["connected_at"] = datetime.now(timezone.utc).isoformat()
        asyncio.ensure_future(broadcast({
            "type": "status",
            "connected": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))

    def _on_disconnected(self, event):
        """Auto-reconnect engaged via self.auto_reconnect. Watchdog catches stalls."""
        self.stats["reconnects"] += 1
        logger.warning(
            f"Disconnected from NWWS-OI (reconnect #{self.stats['reconnects']}). "
            f"Auto-reconnect engaged."
        )
        asyncio.ensure_future(broadcast({
            "type": "status",
            "connected": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))

    def _on_conn_failed(self, event):
        """Connection failure — log only.

        With self.auto_reconnect = True, slixmpp handles the retry. Our
        previous version scheduled an additional manual reconnect via
        call_later, which raced with slixmpp's reconnect and produced
        "There is already a scheduled event: Whitespace Keepalive" errors.

        If slixmpp's auto-reconnect ever fails repeatedly, the watchdog
        catches it via is_stale() and force-disconnects to retry.
        """
        logger.error(
            f"NWWS-OI connection failed (auto_reconnect will retry; "
            f"connect_params={self._connect_params})"
        )

    def _on_message(self, msg):
        if msg['type'] != 'groupchat':
            return

        self.stats["total"] += 1
        # Update last_product_time so the watchdog knows we're alive.
        # This fires for ALL groupchat messages (including non-product noise),
        # which is what we want — the watchdog only needs to know that the
        # XMPP connection is still receiving data, not specifically NWS products.
        self.stats["last_product_time"] = datetime.now(timezone.utc).isoformat()

        x_elem = msg.xml.find(f'{{{NWWS_OI_NS}}}x')
        if x_elem is None:
            return

        cccc = x_elem.get('cccc', '')
        awipsid = x_elem.get('awipsid', '')
        issue = x_elem.get('issue', '')
        raw = x_elem.text or ''

        if not awipsid:
            return

        prefix = awipsid[:3].upper()

        if prefix == "CLI":
            station = resolve_cli_station(awipsid)
            parsed = parse_cli(raw)
            product = {
                "product_type": "CLI",
                "awipsid": awipsid,
                "cccc": cccc,
                "station": station,
                "city": parsed.get("city_name") or (STATIONS[station]["city"] if station else awipsid[3:]),
                "high": parsed["high"],
                "low": parsed["low"],
                "high_time": parsed["high_time"],
                "low_time": parsed["low_time"],
                "valid_as": parsed["valid_as"],
                "is_preliminary": parsed.get("is_preliminary", False),
                "watched": station is not None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "issue": issue,
                "raw_excerpt": raw[:500],
            }
            self.stats["cli"] += 1
            self._emit(product)

            if station and (parsed["high"] is not None or parsed["low"] is not None):
                opps = check_cli_opportunities(
                    station, cli_high=parsed["high"], cli_low=parsed["low"],
                    valid_as=parsed.get("valid_as"),
                )
                if opps:
                    asyncio.ensure_future(broadcast({
                        "type": "opportunities",
                        "station": station,
                        "cli_high": parsed["high"],
                        "cli_low": parsed["low"],
                        "opportunities": [
                            {
                                "action": o["action"],
                                "price": o["price"],
                                "edge_cents": o["edge_cents"],
                                "reason": o["reason"],
                                "subtitle": o["bracket"].subtitle,
                                "ticker": o["bracket"].ticker,
                            }
                            for o in opps
                        ],
                    }))

        elif prefix == "DSM":
            station = resolve_dsm_station(awipsid)
            parsed = parse_dsm(raw)
            product = {
                "product_type": "DSM",
                "awipsid": awipsid,
                "cccc": cccc,
                "station": station,
                "city": STATIONS[station]["city"] if station else awipsid[3:],
                "high": parsed["high"],
                "low": parsed["low"],
                "high_time": parsed["high_time"],
                "low_time": parsed["low_time"],
                "valid_as": None,
                "watched": station is not None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "issue": issue,
                "raw_excerpt": raw[:500],
            }
            self.stats["dsm"] += 1
            self._emit(product)

            if station and (parsed["high"] is not None or parsed["low"] is not None):
                opps = check_cli_opportunities(
                    station, cli_high=parsed["high"], cli_low=parsed["low"],
                )
                if opps:
                    asyncio.ensure_future(broadcast({
                        "type": "opportunities",
                        "station": station,
                        "cli_high": parsed["high"],
                        "cli_low": parsed["low"],
                        "opportunities": [
                            {
                                "action": o["action"],
                                "price": o["price"],
                                "edge_cents": o["edge_cents"],
                                "reason": o["reason"],
                                "subtitle": o["bracket"].subtitle,
                                "ticker": o["bracket"].ticker,
                            }
                            for o in opps
                        ],
                    }))

    def _emit(self, product):
        tag = "🟢" if product["high"] is not None else "⚪"
        w = "★" if product["watched"] else " "
        logger.info(
            f"{tag}{w} {product['product_type']} {product['awipsid']} | "
            f"station={product['station']} | "
            f"H={product['high']}°F L={product['low']}°F | "
            f"valid={product['valid_as']}"
        )

        save_product(product)
        PRODUCT_LOG.append(product)
        if len(PRODUCT_LOG) > MAX_LOG:
            PRODUCT_LOG.pop(0)

        asyncio.ensure_future(broadcast({
            "type": "product",
            "data": product,
        }))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    from dotenv import load_dotenv
    load_dotenv()

    user_id = os.getenv("NWWS_USER_ID")
    password = os.getenv("NWWS_PASSWORD")
    server = os.getenv("NWWS_SERVER", "nwws-oi-bldr.weather.gov")

    if not user_id or not password:
        logger.error("Set NWWS_USER_ID and NWWS_PASSWORD in .env")
        sys.exit(1)

    jid = f"{user_id}@{server}"

    PRODUCT_LOG.extend(load_products(max_age_hours=48))
    cleanup_product_file(max_age_hours=48)
    init_kalshi()

    # Replay CLI/DSM eliminations from historical products
    if BRACKETS:
        replayed = 0
        for p in PRODUCT_LOG:
            if p.get("watched") and p.get("station") and p["station"] in BRACKETS:
                station = p["station"]
                cli_high = p.get("high")
                cli_low = p.get("low")
                valid_as = p.get("valid_as")

                # Skip if CLI date doesn't match bracket date
                if valid_as and not _cli_date_matches_brackets(station, valid_as):
                    continue

                if cli_high is not None or cli_low is not None:
                    station_data = BRACKETS[station]

                    if cli_high is not None:
                        for b in station_data.get("high", []):
                            if b.status == "open" and b.check_temp_intraday(cli_high) == "dead":
                                b.status = "dead"
                                replayed += 1

                    if cli_low is not None:
                        for b in station_data.get("low", []):
                            if b.status == "open" and b.check_temp_intraday(cli_low) == "dead":
                                b.status = "dead"
                                replayed += 1

        # Check for last-man-standing after all eliminations
        for station, data in BRACKETS.items():
            for signal_type in ("high", "low"):
                group = data.get(signal_type, [])
                alive = [b for b in group if b.status != "dead"]
                if len(alive) == 1 and alive[0].status == "open":
                    alive[0].status = "locked"
                    replayed += 1
                    logger.info(f"🏆 {station} {signal_type.upper()}: LAST-MAN-STANDING → {alive[0].subtitle}")

        if replayed:
            logger.info(f"Replayed {replayed} bracket eliminations from historical products")

    try:
        import websockets
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "websockets", "--break-system-packages", "-q"])
        import websockets

    ws_server = await websockets.serve(ws_handler, "0.0.0.0", 8766)
    logger.info("WebSocket server running on ws://0.0.0.0:8766")

    client = NWWSMonitorClient(jid, password)
    client.connect((server, 5222))

    logger.info(f"Connecting to NWWS-OI as {jid}...")
    logger.info(f"Watching {len(STATIONS)} stations for CLI/DSM products")
    logger.info(f"Mode: {'🔴 LIVE' if LIVE_MODE else '🧪 DRY RUN'} | Max price: {MAX_PRICE}¢ | Order size: {ORDER_SIZE}")
    logger.info(f"Dashboard: http://YOUR_EC2_IP:8080/nwws_monitor.html")

    # Start Kalshi WebSocket for real-time orderbook/ticker data
    global KALSHI_WS
    kalshi_ws = None
    if KALSHI_CLIENT and KALSHI_CLIENT.private_key and BRACKETS:
        try:
            from kalshi_ws import KalshiWebSocket

            def on_ticker_update(ticker, bracket):
                """When a ticker updates, broadcast to dashboard."""
                # Only broadcast periodically to avoid flooding
                if kalshi_ws and kalshi_ws.stats["ticker_updates"] % 50 == 0:
                    asyncio.ensure_future(broadcast({
                        "type": "brackets_updated",
                        "brackets": get_brackets_summary(),
                    }))

            kalshi_ws = KalshiWebSocket(
                KALSHI_CLIENT, BRACKETS,
                on_ticker_update=on_ticker_update,
            )
            asyncio.ensure_future(kalshi_ws.run())
            KALSHI_WS = kalshi_ws
            logger.info("📡 Kalshi WebSocket streaming started")
        except Exception as e:
            logger.error(f"Failed to start Kalshi WebSocket: {e}")

    async def refresh_loop():
        while True:
            await asyncio.sleep(1800)
            logger.info("Refreshing Kalshi brackets...")
            init_kalshi()
            await broadcast({
                "type": "brackets_updated",
                "brackets": get_brackets_summary(),
            })
            # Cleanup old products every refresh cycle
            cleanup_product_file(max_age_hours=48)

    async def watchdog_loop():
        """Detect silent NWWS-OI stalls and force reconnect.

        slixmpp's auto-reconnect handles socket-level disconnects, but
        NWWS-OI is known to silently stop sending data while the TCP
        connection looks alive. This loop checks every minute whether
        the client has gone silent (no products in >stale_threshold) and
        force-disconnects, which triggers a fresh reconnect.

        This is what was broken Feb 18 → May 5 — bot ran "fine" for 78 days
        without ingesting a single product because nothing detected the stall.
        """
        await asyncio.sleep(120)  # initial 2-min grace period after startup
        while True:
            try:
                if client.is_stale():
                    last = client.stats.get("last_product_time", "never")
                    logger.warning(
                        f"🐶 WATCHDOG: NWWS-OI stale (last product: {last}). "
                        f"Forcing reconnect..."
                    )
                    try:
                        client.disconnect()
                    except Exception as e:
                        logger.error(f"Watchdog disconnect failed: {e}")
            except Exception as e:
                logger.error(f"Watchdog loop error: {e}")
            await asyncio.sleep(60)

    async def price_snapshot_loop():
        """Every 5 min, dump all bracket prices to logs/prices.jsonl.

        Lets us reconstruct the market timeline retrospectively to answer
        questions like 'when did Vegas LOW move from 70¢ to 100¢?' which
        is impossible without time-series data.
        """
        await asyncio.sleep(60)  # initial 1-min grace
        while True:
            try:
                if BRACKETS:
                    write_price_snapshot(BRACKETS)
            except Exception as e:
                logger.error(f"Price snapshot failed: {e}")
            await asyncio.sleep(300)  # 5 min

    # ---- METAR POLLER ----
    # Augments the CLI flow with hourly METAR signals. At synoptic times
    # (00/06/12/18 UTC) METARs include 6-hour MAX/MIN groups which give
    # mathematical eliminations earlier than waiting for the daily CLI.
    def on_new_metar(station, raw, obs_time, parsed):
        """Callback for each new METAR. Triggers bracket elimination if 6hr group present."""
        if parsed is None or not getattr(parsed, "temperatures", None):
            return
        try:
            from parsers import DataSource

            # SYNOPTIC GUARD: 6-hour MAX/MIN groups are ONLY present in METARs
            # at synoptic times (00/06/12/18 UTC, observed at :53-:59 of those hours).
            # If the parser claims to find 6hr data in a non-synoptic METAR, it's a
            # FALSE POSITIVE — usually wind data (e.g. "PK WND 28030/0106" matches
            # the 2xxxx regex and decodes as bogus +3.0°C → 37°F). Skip these to
            # avoid firing snipes on phantom temperature observations.
            is_synoptic = (obs_time.hour in {0, 6, 12, 18}) and (obs_time.minute >= 50)

            six_hr_max_f = None
            six_hr_min_f = None
            for t in parsed.temperatures:
                if t.source == DataSource.METAR_6HR_MAX and is_synoptic:
                    six_hr_max_f = t.temp_f
                elif t.source == DataSource.METAR_6HR_MIN and is_synoptic:
                    six_hr_min_f = t.temp_f
                # Non-synoptic 6hr matches are silently dropped (parser false positives)

            # V1: only act on synoptic METARs (those with 6-hour groups).
            # T-group precision logic (rounding-ambiguity edge) is a future enhancement.
            if six_hr_max_f is None and six_hr_min_f is None:
                return

            logger.info(
                "📡 [METAR %s] obs=%s 6hr_max=%s°F 6hr_min=%s°F → routing to bracket eval",
                station, obs_time.strftime("%H:%M:%SZ"),
                six_hr_max_f, six_hr_min_f,
            )

            # Route through existing check_cli_opportunities for today's brackets.
            # valid_as=None lets the function default to match='today'.
            check_cli_opportunities(
                station=station,
                cli_high=six_hr_max_f,
                cli_low=six_hr_min_f,
                valid_as=None,
            )
        except Exception as e:
            logger.error(f"on_new_metar failed for {station}: {e}")

    metar_poller = None
    try:
        from metar_poller import AviationWeatherPoller
        metar_poller = AviationWeatherPoller(
            stations=list(STATIONS.keys()),
            on_new_metar=on_new_metar,
        )
        logger.info("📡 METAR poller initialized for %d stations", len(STATIONS))
    except Exception as e:
        logger.error(f"Failed to initialize METAR poller: {e}")

    asyncio.ensure_future(refresh_loop())
    asyncio.ensure_future(watchdog_loop())
    asyncio.ensure_future(price_snapshot_loop())
    if metar_poller is not None:
        asyncio.ensure_future(metar_poller.run())

    try:
        await asyncio.Future()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
        client.disconnect()
        ws_server.close()


if __name__ == "__main__":
    asyncio.run(main())
