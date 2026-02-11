"""
NWWS-OI XMPP Client for real-time NWS product ingestion.

Connects to the NWWS-OI (NOAA Weather Wire Service - Open Interface) via XMPP,
joins the single broadcast chatroom, and filters/parses incoming NWS products.

Architecture:
    NWWS-OI server (MUC chatroom)
        -> slixmpp client joins room
        -> receives <message type="groupchat"> stanzas
        -> extracts <x xmlns="nwws-oi"> payload with raw product text
        -> filters by cccc (WFO) and awipsid (product type)
        -> routes to appropriate parser
        -> emits parsed results via callback

Connection details:
    Servers: nwws-oi-bldr.weather.gov (Boulder) or nwws-oi-cprk.weather.gov (College Park)
    Room: nwws@nwws-oi.weather.gov
    Credentials: NWS-issued user_ID and password (case-sensitive, single session only)
"""

import asyncio
import logging
import signal
import sys
import json
import os
from datetime import datetime, timezone
from typing import Callable, Any
from xml.etree import ElementTree

import slixmpp
from slixmpp.xmlstream import ET

from parsers import (
    ProductParseResult, ProductType, parse_product, classify_product,
    DataSource
)
from stations import (
    STATIONS, DSM_TO_STATION, METAR_TO_STATION, WATCHED_WFOS
)

logger = logging.getLogger(__name__)

# NWWS-OI configuration
NWWS_SERVERS = [
    "nwws-oi-bldr.weather.gov",     # Boulder (primary after 2/4/2026 transition)
    "nwws-oi-cprk.weather.gov",     # College Park (fallback)
]
NWWS_ROOM = "nwws@conference.nwws-oi.weather.gov"
NWWS_OI_NAMESPACE = "nwws-oi"

# Products we care about for temperature resolution
INTERESTING_PRODUCT_PREFIXES = {"DSM", "CLI", "MTR", "SPE"}

# AWIPS IDs we specifically watch (built from station config)
WATCHED_DSM_IDS = set(DSM_TO_STATION.keys())


class NWWSOIClient(slixmpp.ClientXMPP):
    """
    XMPP client that connects to NWWS-OI and ingests NWS products.

    The NWWS-OI is a single MUC (Multi-User Chat) room where a bot user
    broadcasts ALL NWS products as they're issued. We join the room,
    listen for messages, and filter for products relevant to our stations.

    The key payload is in the <x xmlns="nwws-oi"> element within each
    <message> stanza, which contains:
        - cccc: Four-character issuing center (e.g., KOKX)
        - ttaaii: Six-character WMO product ID
        - awipsid: Six-character AWIPS ID / AFOS PIL (e.g., DSMNYC)
        - issue: ISO-8601 UTC datetime
        - id: Unique product identifier (PID.sequence)
        - Text content: The raw NWS product text
    """

    def __init__(self, jid: str, password: str,
                 on_product: Callable[[ProductParseResult], Any] | None = None,
                 watch_all: bool = False):
        """
        Args:
            jid: XMPP JID (your NWWS-OI user_ID@nwws-oi-bldr.weather.gov)
            password: NWWS-OI password (case-sensitive)
            on_product: Callback for parsed products with temperature data
            watch_all: If True, log ALL products (not just temperature-related)
        """
        super().__init__(jid, password)

        self.room = NWWS_ROOM
        self.nick = jid.split("@")[0]  # Use username as nickname
        self.on_product = on_product
        self.watch_all = watch_all

        # Stats
        self.stats = {
            "connected_at": None,
            "messages_received": 0,
            "products_filtered": 0,
            "products_parsed": 0,
            "products_with_data": 0,
            "errors": 0,
            "last_product_time": None,
            "reconnects": 0,
        }

        # Sequence tracking for gap detection
        self._last_seq_id: int | None = None
        self._last_pid: int | None = None

        # Register plugins
        self.register_plugin("xep_0199")  # XMPP Ping (keepalive)

        # Event handlers
        self.add_event_handler("session_start", self._on_session_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("disconnected", self._on_disconnected)
        self.add_event_handler("connection_failed", self._on_connection_failed)

    async def _on_session_start(self, event):
        """Called when XMPP session is established."""
        logger.info("NWWS-OI session started, sending presence...")
        await self.get_roster()
        self.send_presence()

        # Join the NWWS chatroom using raw presence stanza
        # (slixmpp's xep_0045 plugin has compatibility issues with this server)
        logger.info(f"Joining room: {self.room} as {self.nick}")
        pres = self.make_presence(pto=f"{self.room}/{self.nick}")
        x_elem = ET.SubElement(pres.xml, '{http://jabber.org/protocol/muc}x')
        pres.send()

        self.stats["connected_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("Successfully joined NWWS-OI chatroom. Listening for products...")

    def _on_disconnected(self, event):
        """Handle disconnection - NWWS-OI has known random disconnect issues."""
        self.stats["reconnects"] += 1
        logger.warning(
            f"Disconnected from NWWS-OI (reconnect #{self.stats['reconnects']}). "
            f"Will attempt to reconnect..."
        )

    def _on_connection_failed(self, event):
        """Handle connection failure."""
        logger.error("Connection to NWWS-OI failed. Will retry...")

    def _on_message(self, msg):
        """
        Handle all incoming messages, filtering for groupchat (MUC) messages.
        Since we join with raw presence instead of xep_0045, all messages
        come through the generic 'message' event.
        """
        if msg['type'] == 'groupchat':
            self._on_groupchat_message(msg)

    def _on_groupchat_message(self, msg):
        """
        Process incoming MUC messages from the NWWS-OI chatroom.

        Each message contains:
        - <body>: Human-readable summary (e.g., "KOKX issues DSM valid ...")
        - <x xmlns="nwws-oi">: The actual product payload with raw text
        """
        self.stats["messages_received"] += 1

        # Extract the nwws-oi payload from the XML
        try:
            raw_xml = str(msg)
            product_data = self._extract_nwws_payload(msg)
        except Exception as e:
            logger.error(f"Failed to extract NWWS payload: {e}")
            self.stats["errors"] += 1
            return

        if product_data is None:
            # Messages during server restart may lack the <x> payload
            return

        cccc = product_data.get("cccc", "")
        awipsid = product_data.get("awipsid", "")
        issue_str = product_data.get("issue", "")
        product_id = product_data.get("id", "")
        raw_text = product_data.get("text", "")
        ttaaii = product_data.get("ttaaii", "")

        # Track sequence for gap detection
        self._check_sequence(product_id)

        # Log all products if watch_all mode
        if self.watch_all:
            logger.debug(
                f"NWWS product: cccc={cccc} awipsid={awipsid} "
                f"ttaaii={ttaaii} issue={issue_str} id={product_id}"
            )

        # FILTER: Only process products we care about
        if not self._is_interesting(cccc, awipsid):
            return

        self.stats["products_filtered"] += 1
        self.stats["last_product_time"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            f">>> INTERESTING PRODUCT: cccc={cccc} awipsid={awipsid} "
            f"issue={issue_str} id={product_id}"
        )

        # Parse the issue time
        issue_time = None
        if issue_str:
            try:
                issue_time = datetime.fromisoformat(issue_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Determine station hint from awipsid
        station_hint = self._resolve_station(awipsid, cccc, raw_text)

        # Parse the product
        try:
            result = parse_product(
                raw_text=raw_text,
                awipsid=awipsid,
                cccc=cccc,
                station_hint=station_hint,
                issue_time=issue_time,
            )
            result.awipsid = awipsid
            result.cccc = cccc
            self.stats["products_parsed"] += 1

            if result.has_data:
                self.stats["products_with_data"] += 1
                logger.info(
                    f"  PARSED: {result.product_type.value} {result.station} -> "
                    f"{[(t.source.value, t.temp_f) for t in result.temperatures]}"
                )

                # Fire callback
                if self.on_product:
                    try:
                        self.on_product(result)
                    except Exception as e:
                        logger.error(f"Callback error: {e}")
            else:
                if result.parse_errors:
                    logger.debug(f"  Parse issues: {result.parse_errors}")

        except Exception as e:
            logger.error(f"Failed to parse product {awipsid}: {e}", exc_info=True)
            self.stats["errors"] += 1

    def _extract_nwws_payload(self, msg) -> dict | None:
        """
        Extract the <x xmlns="nwws-oi"> payload from a message stanza.

        The payload contains product metadata as attributes and the raw
        product text as the element's text content.

        Returns dict with keys: cccc, ttaaii, awipsid, issue, id, text
        Or None if no payload found.
        """
        # slixmpp gives us access to the underlying XML
        xml = msg.xml

        # Look for the <x> element with nwws-oi namespace
        x_elem = xml.find(f"{{{NWWS_OI_NAMESPACE}}}x")
        if x_elem is None:
            # Try without namespace (some messages during restart lack it)
            for child in xml:
                if child.tag.endswith("}x") or child.tag == "x":
                    ns = child.tag.split("}")[0].lstrip("{") if "}" in child.tag else ""
                    if "nwws" in ns.lower():
                        x_elem = child
                        break

        if x_elem is None:
            return None

        return {
            "cccc": x_elem.get("cccc", ""),
            "ttaaii": x_elem.get("ttaaii", ""),
            "awipsid": x_elem.get("awipsid", ""),
            "issue": x_elem.get("issue", ""),
            "id": x_elem.get("id", ""),
            "text": (x_elem.text or "").strip(),
        }

    def _is_interesting(self, cccc: str, awipsid: str) -> bool:
        """
        Check if this product is one we want to parse.

        We filter on:
        1. Product type (DSM, METAR, SPECI, CLI)
        2. Station relevance (from a watched WFO or contains a watched station)
        """
        if not awipsid:
            return False

        base = awipsid[:3].upper()

        # DSM: Check if it's for one of our watched stations
        if base == "DSM":
            return awipsid.upper() in WATCHED_DSM_IDS

        # CLI: Check the station suffix
        if base == "CLI":
            # CLI awipsids are like CLINEW (for KNYC/NYC area)
            # This needs more nuanced matching - for now, pass through
            # if from a watched WFO
            return cccc in WATCHED_WFOS

        # METAR/SPECI: Check if from a watched WFO
        # Note: METARs on NWWS-OI often come as SA/SP products
        # with ttaaii starting with SA or SP
        if base in ("MTR", "SPE", "SA ", "SP "):
            return cccc in WATCHED_WFOS

        # Also catch products where the awipsid contains a known station
        # suffix (e.g., last 3 chars match a station)
        suffix = awipsid[3:].upper()
        for station in STATIONS:
            if suffix == station[1:]:  # e.g., "NYC" matches KNYC
                return True

        return False

    def _resolve_station(self, awipsid: str, cccc: str, raw_text: str) -> str:
        """Try to determine which station this product is for."""
        # DSM: direct lookup
        if awipsid.upper() in DSM_TO_STATION:
            return DSM_TO_STATION[awipsid.upper()]

        # METAR/SPECI: Look for station in the raw text
        import re
        for station in STATIONS:
            if station in raw_text:
                return station

        # Fall back to WFO -> any station mapping
        for station, config in STATIONS.items():
            if config["wfo_cccc"] == cccc:
                return station

        return ""

    def _check_sequence(self, product_id: str):
        """Check for gaps in the product sequence."""
        if not product_id or "." not in product_id:
            return

        try:
            pid_str, seq_str = product_id.split(".", 1)
            pid = int(pid_str)
            seq = int(seq_str)

            if self._last_pid is not None and pid == self._last_pid:
                if self._last_seq_id is not None and seq > self._last_seq_id + 1:
                    gap = seq - self._last_seq_id - 1
                    logger.warning(
                        f"Sequence gap detected: missed {gap} products "
                        f"(last={self._last_seq_id}, current={seq})"
                    )
            elif self._last_pid is not None and pid != self._last_pid:
                logger.info(f"NWWS-OI process ID changed: {self._last_pid} -> {pid}")

            self._last_pid = pid
            self._last_seq_id = seq
        except (ValueError, IndexError):
            pass

    def get_stats(self) -> dict:
        """Return current connection and processing stats."""
        return dict(self.stats)


# ---------------------------------------------------------------------------
# PRODUCT LOG - writes parsed products to a JSONL file for review
# ---------------------------------------------------------------------------

class ProductLogger:
    """Logs parsed products to a JSONL file for review and debugging."""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self._file = None
        self._current_date = None

    def _ensure_file(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._file:
                self._file.close()
            path = os.path.join(self.log_dir, f"products_{today}.jsonl")
            self._file = open(path, "a")
            self._current_date = today

    def log(self, result: ProductParseResult):
        """Write a parsed product result to the log file."""
        self._ensure_file()
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "product_type": result.product_type.value,
            "station": result.station,
            "awipsid": result.awipsid,
            "cccc": result.cccc,
            "issue_time": result.issue_time.isoformat() if result.issue_time else None,
            "temperatures": [
                {
                    "temp_f": t.temp_f,
                    "temp_c": t.temp_c,
                    "source": t.source.value,
                    "precision": t.precision,
                    "time_of_occurrence": t.time_of_occurrence,
                }
                for t in result.temperatures
            ],
            "parse_errors": result.parse_errors,
        }
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()

    def close(self):
        if self._file:
            self._file.close()


# ---------------------------------------------------------------------------
# MAIN - standalone runner for testing
# ---------------------------------------------------------------------------

async def run_client(jid: str, password: str, watch_all: bool = False):
    """Run the NWWS-OI client as a standalone process."""

    product_logger = ProductLogger()

    def on_product(result: ProductParseResult):
        """Callback for parsed products."""
        product_logger.log(result)

        # Print to console for monitoring
        for temp in result.temperatures:
            print(
                f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                f"{result.product_type.value.upper():6s} | "
                f"{result.station:4s} | "
                f"{temp.source.value:15s} | "
                f"{temp.temp_f:4d}°F"
                f"{f' ({temp.temp_c:.1f}°C)' if temp.temp_c is not None else ''}"
                f"{f' at {temp.time_of_occurrence} LST' if temp.time_of_occurrence else ''}"
            )

    client = NWWSOIClient(
        jid=jid,
        password=password,
        on_product=on_product,
        watch_all=watch_all,
    )

    # Try connecting to NWWS-OI servers
    connected = False
    for server in NWWS_SERVERS:
        logger.info(f"Attempting connection to {server}...")
        try:
            client.connect((server, 5222))
            connected = True
            break
        except Exception as e:
            logger.warning(f"Failed to connect to {server}: {e}")

    if not connected:
        logger.error("Could not connect to any NWWS-OI server")
        return

    # Print startup info
    print("=" * 70)
    print("WX SNIPER V2 — NWWS-OI Product Ingestor")
    print("=" * 70)
    print(f"Watching {len(STATIONS)} stations for DSM/METAR/SPECI/CLI products")
    print(f"DSM IDs monitored: {sorted(WATCHED_DSM_IDS)}")
    print(f"WFOs monitored: {sorted(WATCHED_WFOS)}")
    print(f"Watch all products: {watch_all}")
    print(f"Product log: logs/products_YYYY-MM-DD.jsonl")
    print("-" * 70)
    print("Waiting for products...\n")

    # Run forever
    try:
        await asyncio.Future()  # Block forever
    except asyncio.CancelledError:
        pass
    finally:
        product_logger.close()
        client.disconnect()


def main():
    """Entry point for the NWWS-OI ingestor."""
    import argparse

    parser = argparse.ArgumentParser(description="NWWS-OI Product Ingestor for WX Sniper V2")
    parser.add_argument("--jid", required=True,
                        help="NWWS-OI JID (e.g., your_user_id@nwws-oi-bldr.weather.gov)")
    parser.add_argument("--password", required=True,
                        help="NWWS-OI password (case-sensitive)")
    parser.add_argument("--watch-all", action="store_true",
                        help="Log ALL products, not just temperature-related")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level")

    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.debug else getattr(logging, args.log_level)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet down slixmpp unless debugging
    if not args.debug:
        logging.getLogger("slixmpp").setLevel(logging.WARNING)

    # Run
    try:
        asyncio.run(run_client(args.jid, args.password, args.watch_all))
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    main()
