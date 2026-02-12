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

    valid_match = re.search(r'VALID.*?AS OF (\d{4} [AP]M) LOCAL TIME', raw, re.IGNORECASE)
    if valid_match:
        result["valid_as"] = valid_match.group(1)

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
            BRACKETS = load_all_brackets(KALSHI_CLIENT, STATIONS)
            total_h = sum(len(v["high"]) for v in BRACKETS.values())
            total_l = sum(len(v["low"]) for v in BRACKETS.values())
            logger.info(f"Kalshi: {total_h} HIGH + {total_l} LOW brackets loaded")
        else:
            logger.warning("Kalshi: no credentials, bracket loading skipped")
    except Exception as e:
        logger.error(f"Kalshi init failed: {e}")
        KALSHI_CLIENT = None


def check_cli_opportunities(station: str, cli_high: int = None, cli_low: int = None) -> list:
    """When a CLI/DSM arrives, resolve ALL brackets and find cheap opportunities."""
    global OPPORTUNITIES

    if not BRACKETS or station not in BRACKETS:
        return []

    try:
        from kalshi_client import find_opportunities, execute_snipe

        # Step 1: Mark ALL brackets as locked/dead based on CLI temps
        station_data = BRACKETS[station]
        resolved_count = 0

        if cli_high is not None:
            for b in station_data.get("high", []):
                new_status = b.check_temp(cli_high)
                if new_status in ("locked", "dead") and b.status == "open":
                    b.status = new_status
                    resolved_count += 1

        if cli_low is not None:
            for b in station_data.get("low", []):
                new_status = b.check_temp(cli_low)
                if new_status in ("locked", "dead") and b.status == "open":
                    b.status = new_status
                    resolved_count += 1

        if resolved_count > 0:
            logger.info(f"📊 {station}: {resolved_count} brackets resolved (H={cli_high} L={cli_low})")

        # Step 2: Broadcast updated brackets to all dashboards
        asyncio.ensure_future(broadcast({
            "type": "brackets_updated",
            "brackets": get_brackets_summary(),
        }))

        # Step 3: Find cheap opportunities (below threshold)
        opps = find_opportunities(
            BRACKETS,
            cli_high=cli_high,
            cli_low=cli_low,
            station=station,
            max_price=MAX_PRICE,
        )

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
                )
                SNIPE_LOG.append(record)

                try:
                    with open("logs/snipes.jsonl", "a") as f:
                        f.write(json.dumps(record) + "\n")
                except Exception:
                    pass

            OPPORTUNITIES.extend(opps)
            if len(OPPORTUNITIES) > 100:
                OPPORTUNITIES = OPPORTUNITIES[-100:]

        return opps

    except Exception as e:
        logger.error(f"Opportunity check failed: {e}")
        return []


def get_brackets_summary() -> list:
    summary = []
    for station, data in BRACKETS.items():
        cfg = STATIONS.get(station, {})
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
        self.add_event_handler("session_start", self._on_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("disconnected", self._on_disconnected)
        self.add_event_handler("connection_failed", self._on_conn_failed)
        self.stats = {"total": 0, "cli": 0, "dsm": 0}

    async def _on_start(self, event):
        logger.info("NWWS-OI session started, joining room...")
        await self.get_roster()
        self.send_presence()
        pres = self.make_presence(pto=f"{self.room}/{self.nick}")
        ET.SubElement(pres.xml, '{http://jabber.org/protocol/muc}x')
        pres.send()
        logger.info("Joined NWWS-OI chatroom")
        asyncio.ensure_future(broadcast({
            "type": "status",
            "connected": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))

    def _on_disconnected(self, event):
        logger.warning("Disconnected from NWWS-OI, will reconnect...")
        asyncio.ensure_future(broadcast({
            "type": "status",
            "connected": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))

    def _on_conn_failed(self, event):
        logger.error("NWWS-OI connection failed")

    def _on_message(self, msg):
        if msg['type'] != 'groupchat':
            return

        self.stats["total"] += 1
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
                "watched": station is not None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "issue": issue,
                "raw_excerpt": raw[:500],
            }
            self.stats["cli"] += 1
            self._emit(product)

            if station and (parsed["high"] is not None or parsed["low"] is not None):
                opps = check_cli_opportunities(station, cli_high=parsed["high"], cli_low=parsed["low"])
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
                opps = check_cli_opportunities(station, cli_high=parsed["high"], cli_low=parsed["low"])
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

    # Replay CLI resolutions from historical products so brackets show correct status
    if BRACKETS:
        replayed = 0
        for p in PRODUCT_LOG:
            if p.get("watched") and p.get("station") and p["station"] in BRACKETS:
                station = p["station"]
                cli_high = p.get("high")
                cli_low = p.get("low")
                if cli_high is not None or cli_low is not None:
                    station_data = BRACKETS[station]
                    if cli_high is not None:
                        for b in station_data.get("high", []):
                            new_status = b.check_temp(cli_high)
                            if new_status in ("locked", "dead") and b.status == "open":
                                b.status = new_status
                                replayed += 1
                    if cli_low is not None:
                        for b in station_data.get("low", []):
                            new_status = b.check_temp(cli_low)
                            if new_status in ("locked", "dead") and b.status == "open":
                                b.status = new_status
                                replayed += 1
        if replayed:
            logger.info(f"Replayed {replayed} bracket resolutions from historical products")

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

    asyncio.ensure_future(refresh_loop())

    try:
        await asyncio.Future()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
        client.disconnect()
        ws_server.close()


if __name__ == "__main__":
    asyncio.run(main())
