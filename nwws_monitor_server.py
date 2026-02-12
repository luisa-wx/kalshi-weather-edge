"""
NWWS-OI WebSocket Monitor Server

Runs the NWWS-OI XMPP client and broadcasts parsed CLI/DSM products
to connected web dashboard clients via WebSocket.

Usage:
    python3 nwws_monitor_server.py

Serves:
    - ws://0.0.0.0:8766  — WebSocket for real-time product feed
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

# Our 19 watched stations
STATIONS = {
    "KNYC": {"city": "NYC", "tz": "ET", "cli_suffix": "NYC"},
    "KPHL": {"city": "Philadelphia", "tz": "ET", "cli_suffix": "PHL"},
    "KMDW": {"city": "Chicago", "tz": "CT", "cli_suffix": "MDW"},
    "KLAX": {"city": "Los Angeles", "tz": "PT", "cli_suffix": "LAX"},
    "KMIA": {"city": "Miami", "tz": "ET", "cli_suffix": "MIA"},
    "KAUS": {"city": "Austin", "tz": "CT", "cli_suffix": "AUS"},
    "KDEN": {"city": "Denver", "tz": "MT", "cli_suffix": "DEN"},
    "KSFO": {"city": "San Francisco", "tz": "PT", "cli_suffix": "SFO"},
    "KSEA": {"city": "Seattle", "tz": "PT", "cli_suffix": "SEA"},
    "KDCA": {"city": "Washington DC", "tz": "ET", "cli_suffix": "DCA"},
    "KMSY": {"city": "New Orleans", "tz": "CT", "cli_suffix": "MSY"},
    "KLAS": {"city": "Las Vegas", "tz": "PT", "cli_suffix": "LAS"},
    "KDFW": {"city": "Dallas", "tz": "CT", "cli_suffix": "DFW"},
    "KHOU": {"city": "Houston", "tz": "CT", "cli_suffix": "HOU"},
    "KBOS": {"city": "Boston", "tz": "ET", "cli_suffix": "BOS"},
    "KMSP": {"city": "Minneapolis", "tz": "CT", "cli_suffix": "MSP"},
    "KSAT": {"city": "San Antonio", "tz": "CT", "cli_suffix": "SAT"},
    "KOKC": {"city": "Oklahoma City", "tz": "CT", "cli_suffix": "OKC"},
    "KPHX": {"city": "Phoenix", "tz": "MST", "cli_suffix": "PHX"},
}

# Reverse lookups
CLI_SUFFIX_TO_STATION = {v["cli_suffix"]: k for k, v in STATIONS.items()}
DSM_SUFFIX_TO_STATION = {k[1:]: k for k in STATIONS}
WATCHED_DSM_IDS = {f"DSM{k[1:]}" for k in STATIONS}

# ---------------------------------------------------------------------------
# Persistent product logging
# ---------------------------------------------------------------------------

PRODUCT_FILE = "logs/cli_dsm_products.jsonl"
os.makedirs("logs", exist_ok=True)


def save_product(product: dict):
    """Append product to persistent JSONL file."""
    try:
        with open(PRODUCT_FILE, "a") as f:
            f.write(json.dumps(product) + "\n")
    except Exception as e:
        logger.error(f"Failed to save product: {e}")


def load_products() -> list:
    """Load all products from persistent file."""
    products = []
    if os.path.exists(PRODUCT_FILE):
        with open(PRODUCT_FILE, "r") as f:
            for line in f:
                try:
                    products.append(json.loads(line.strip()))
                except Exception:
                    pass
    logger.info(f"Loaded {len(products)} historical products from {PRODUCT_FILE}")
    return products


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_cli(raw: str) -> dict:
    """Parse a CLI (Climate Report) for high/low temps."""
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
    """Parse a DSM (Daily Summary Message) for high/low temps."""
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
# WebSocket broadcast
# ---------------------------------------------------------------------------

CLIENTS = set()
PRODUCT_LOG = []
MAX_LOG = 500


async def broadcast(message: dict):
    """Send a JSON message to all connected WebSocket clients."""
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
    """Handle a new WebSocket connection — send history then stream."""
    CLIENTS.add(websocket)
    logger.info(f"Dashboard connected ({len(CLIENTS)} total)")

    try:
        # Send full history on connect
        await websocket.send(json.dumps({
            "type": "history",
            "data": PRODUCT_LOG[-200:],
        }))

        # Listen for injected test products
        async for msg in websocket:
            try:
                data = json.loads(msg)
                if data.get("type") == "product":
                    product = data["data"]
                    PRODUCT_LOG.append(product)
                    save_product(product)
                    await broadcast(data)
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

    def _emit(self, product):
        """Log, persist, and broadcast a parsed product."""
        tag = "🟢" if product["high"] is not None else "⚪"
        w = "★" if product["watched"] else " "
        logger.info(
            f"{tag}{w} {product['product_type']} {product['awipsid']} | "
            f"station={product['station']} | "
            f"H={product['high']}°F L={product['low']}°F | "
            f"valid={product['valid_as']}"
        )

        # Always save to file (watched and unwatched)
        save_product(product)

        # Always add to in-memory log
        PRODUCT_LOG.append(product)
        if len(PRODUCT_LOG) > MAX_LOG:
            PRODUCT_LOG.pop(0)

        # Broadcast to dashboards
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

    # Load historical products
    PRODUCT_LOG.extend(load_products())
    logger.info(f"Product log has {len(PRODUCT_LOG)} entries")

    # Start WebSocket server
    try:
        import websockets
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "websockets", "--break-system-packages", "-q"])
        import websockets

    ws_server = await websockets.serve(ws_handler, "0.0.0.0", 8766)
    logger.info("WebSocket server running on ws://0.0.0.0:8766")

    # Start NWWS-OI client
    client = NWWSMonitorClient(jid, password)
    client.connect((server, 5222))

    logger.info(f"Connecting to NWWS-OI as {jid}...")
    logger.info(f"Watching {len(STATIONS)} stations for CLI/DSM products")
    logger.info(f"Dashboard: http://YOUR_EC2_IP:8080/nwws_monitor.html")

    try:
        await asyncio.Future()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
        client.disconnect()
        ws_server.close()


if __name__ == "__main__":
    asyncio.run(main())
