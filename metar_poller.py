"""
METAR Poller — aviationweather.gov ingestion with smart polling.

Polls aviationweather.gov for METAR data on all configured stations and
feeds new observations into the bot's bracket-elimination flow.

Smart polling pattern (adapted from past CELL v14 smart_poller architecture):
  - Normal: 60s polling
  - Hot window (:50-:10 around synoptic hours 00/06/12/18 UTC): 200ms polling
  - Persistent HTTP session (TLS handshake amortized once)
  - Batch request for all stations in a single API call

Why this matters even at ~5 min total lag:
  - METARs deliver hourly cadence vs CLI's once-daily
  - 6-hour MAX/MIN in synoptic METARs (00/06/12/18Z) at :53 → ~5-min lag
    is still BEFORE afternoon prelim CLIs typically drop
  - T-group precision (tenths °C) — different category of edge from CLI rounding
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter

from parsers import parse_metar

logger = logging.getLogger("metar_poller")

# Synoptic hours when 6-hour MAX/MIN appear in METAR remarks (UTC).
SYNOPTIC_HOURS_UTC = {0, 6, 12, 18}

# Hot window: poll aggressively from :50 of synoptic hour through :10 of next hour
HOT_WINDOW_START_MINUTE = 50
HOT_WINDOW_END_MINUTE = 10  # next hour

# Polling intervals
HOT_POLL_INTERVAL_SECONDS = 0.2   # 200ms during hot window
NORMAL_POLL_INTERVAL_SECONDS = 60  # 60s normally

# AviationWeather.gov endpoint
AVWX_URL = "https://aviationweather.gov/api/data/metar"


class AviationWeatherPoller:
    """Polls aviationweather.gov for new METARs across all configured stations."""

    def __init__(self, stations: list[str], on_new_metar: Callable):
        """
        Args:
            stations: List of ICAO codes to monitor (e.g. ['KNYC', 'KPHL', ...])
            on_new_metar: Callback for each new METAR seen.
                          Signature: (station: str, raw: str, obs_time: datetime, parsed: ParsedMETAR) -> None
        """
        self.stations = stations
        self.on_new_metar = on_new_metar

        # Persistent session — TLS handshake done once, reused across requests
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": "WXSniper/2.0 (weather trading bot)",
        })

        # Track last obsTime per station to detect new METARs (de-dup)
        self.last_obs_time: dict[str, datetime] = {}

        # Stats
        self.stats = {
            "total_polls": 0,
            "new_metars": 0,
            "errors": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        # Lifecycle
        self.running = True

    def is_hot_window(self, now: Optional[datetime] = None) -> bool:
        """True if currently in :50-:10 around a synoptic hour."""
        if now is None:
            now = datetime.now(timezone.utc)
        h, m = now.hour, now.minute
        # Synoptic hour minute >= 50: e.g. 5:50-5:59
        if m >= HOT_WINDOW_START_MINUTE and h in SYNOPTIC_HOURS_UTC:
            return True
        # Hour after synoptic, minute <= 10: e.g. 6:00-6:10
        if m <= HOT_WINDOW_END_MINUTE and ((h - 1) % 24) in SYNOPTIC_HOURS_UTC:
            return True
        return False

    def fetch_all_stations(self) -> list[dict]:
        """Single batch GET for all stations. Returns list of METAR dicts."""
        ids = ",".join(self.stations)
        url = f"{AVWX_URL}?ids={ids}&format=json"
        try:
            resp = self.session.get(url, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return []
            return data
        except Exception as e:
            self.stats["errors"] += 1
            logger.warning("METAR fetch failed: %s", e)
            return []

    def process_metars(self, metars: list[dict]) -> int:
        """Process a batch of METARs from API response. Returns count of NEW METARs handled."""
        new_count = 0
        for m in metars:
            try:
                station = m.get("icaoId", "")
                if station not in self.stations:
                    continue

                # obsTime is unix epoch seconds from aviationweather.gov
                obs_epoch = m.get("obsTime")
                if not obs_epoch:
                    continue
                obs_time = datetime.fromtimestamp(obs_epoch, tz=timezone.utc)

                # Skip if we've already seen this obs_time for this station
                last_seen = self.last_obs_time.get(station)
                if last_seen is not None and obs_time <= last_seen:
                    continue

                self.last_obs_time[station] = obs_time

                raw = m.get("rawOb", "")
                if not raw:
                    continue

                # Parse with shared parser (T-group, 6hr max/min, etc.)
                parsed = None
                try:
                    parsed = parse_metar(raw)
                except Exception as e:
                    logger.warning("parse_metar failed for %s: %s", station, e)

                # Hand off to the bot
                try:
                    self.on_new_metar(station, raw, obs_time, parsed)
                    new_count += 1
                    self.stats["new_metars"] += 1
                except Exception as e:
                    logger.error("on_new_metar callback failed for %s: %s", station, e)
            except Exception as e:
                logger.warning("Failed to process METAR record: %s", e)
                continue
        return new_count

    async def run(self):
        """Main polling loop. Run as asyncio task."""
        logger.info(
            "📡 AviationWeather poller starting — %d stations, hot window %d:%02d-:%02d at %dms",
            len(self.stations), HOT_WINDOW_START_MINUTE, HOT_WINDOW_END_MINUTE,
            HOT_WINDOW_END_MINUTE, int(HOT_POLL_INTERVAL_SECONDS * 1000),
        )

        # Warm the connection on startup
        try:
            self.fetch_all_stations()
            logger.info("📡 AviationWeather connection warm")
        except Exception:
            pass

        last_hot_log = -1
        while self.running:
            try:
                hot = self.is_hot_window()
                now = datetime.now(timezone.utc)

                # Log when hot window starts/ends (once per minute change)
                if hot and now.minute != last_hot_log:
                    logger.info("🔥 METAR hot window @ %s", now.strftime("%H:%M:%SZ"))
                    last_hot_log = now.minute

                # Poll
                metars = self.fetch_all_stations()
                self.stats["total_polls"] += 1
                new_count = self.process_metars(metars)
                if new_count > 0:
                    logger.info("📡 %d new METAR(s) processed", new_count)

                # Sleep based on mode
                if hot:
                    await asyncio.sleep(HOT_POLL_INTERVAL_SECONDS)
                else:
                    await asyncio.sleep(NORMAL_POLL_INTERVAL_SECONDS)

            except asyncio.CancelledError:
                self.running = False
                break
            except Exception as e:
                logger.error("METAR poller loop error: %s", e)
                await asyncio.sleep(5)

    def stop(self):
        self.running = False
