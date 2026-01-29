"""
AviationWeather.gov API Poller

Polls the free aviationweather.gov API for METARs.
This is the automated fallback when SMS isn't available.

Updates typically appear within a few minutes of observation time.
"""

import time
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, List
from dataclasses import dataclass

from config import (
    STATIONS,
    AVIATIONWEATHER_API_URL,
    AVIATIONWEATHER_USER_AGENT
)


@dataclass
class MetarResponse:
    """Response from aviationweather.gov API"""
    station: str
    raw_text: str
    observation_time: datetime
    fetched_at: datetime


class AviationWeatherPoller:
    """Polls aviationweather.gov for METAR data"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": AVIATIONWEATHER_USER_AGENT
        })
        
        # Track last observation time per station to detect new METARs
        self.last_obs_time: Dict[str, datetime] = {}
    
    def fetch_metar(self, station: str) -> Optional[MetarResponse]:
        """
        Fetch latest METAR for a station
        
        Returns MetarResponse or None if fetch failed
        """
        try:
            url = f"{AVIATIONWEATHER_API_URL}?ids={station}&format=raw"
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            raw_text = response.text.strip()
            
            if not raw_text or "error" in raw_text.lower():
                return None
            
            # Parse observation time from METAR (format: STATION DDHHMMZ ...)
            obs_time = self._parse_obs_time(raw_text)
            
            return MetarResponse(
                station=station,
                raw_text=raw_text,
                observation_time=obs_time,
                fetched_at=datetime.now(timezone.utc)
            )
            
        except Exception as e:
            print(f"[AVWX] Error fetching {station}: {e}")
            return None
    
    def fetch_all_stations(self) -> List[MetarResponse]:
        """Fetch METARs for all configured stations"""
        results = []
        
        stations = list(STATIONS.keys())
        
        try:
            # Fetch all at once (more efficient)
            ids = ",".join(stations)
            url = f"{AVIATIONWEATHER_API_URL}?ids={ids}&format=raw"
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            
            # Response has one METAR per line
            for line in response.text.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                    
                # Extract station from start of METAR
                parts = line.split()
                if parts and len(parts[0]) == 4:
                    station = parts[0]
                    obs_time = self._parse_obs_time(line)
                    
                    results.append(MetarResponse(
                        station=station,
                        raw_text=line,
                        observation_time=obs_time,
                        fetched_at=datetime.now(timezone.utc)
                    ))
                    
        except Exception as e:
            print(f"[AVWX] Error fetching all stations: {e}")
        
        return results
    
    def check_for_new_metars(self) -> List[MetarResponse]:
        """
        Fetch all stations and return only NEW METARs
        (where observation time is newer than last seen)
        """
        new_metars = []
        
        all_metars = self.fetch_all_stations()
        
        for metar in all_metars:
            station = metar.station
            obs_time = metar.observation_time
            
            if obs_time is None:
                continue
            
            last_time = self.last_obs_time.get(station)
            
            if last_time is None or obs_time > last_time:
                # This is a new METAR!
                self.last_obs_time[station] = obs_time
                new_metars.append(metar)
                print(f"[AVWX] NEW METAR: {station} @ {obs_time.strftime('%H:%M')}Z")
        
        return new_metars
    
    def _parse_obs_time(self, metar_text: str) -> Optional[datetime]:
        """
        Parse observation time from METAR text
        
        Format: STATION DDHHMMZ ...
        Example: KSFO 281853Z ...
        """
        try:
            parts = metar_text.split()
            if len(parts) < 2:
                return None
            
            time_part = parts[1]  # e.g., "281853Z"
            
            if not time_part.endswith('Z') or len(time_part) != 7:
                return None
            
            day = int(time_part[0:2])
            hour = int(time_part[2:4])
            minute = int(time_part[4:6])
            
            # Build datetime (assume current month/year)
            now = datetime.now(timezone.utc)
            
            # Handle month boundary (if day > current day, it's last month)
            year = now.year
            month = now.month
            
            if day > now.day + 1:  # More than 1 day in future = last month
                month -= 1
                if month < 1:
                    month = 12
                    year -= 1
            
            return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            
        except (ValueError, IndexError):
            return None
    
    def is_synoptic_metar(self, metar: MetarResponse) -> bool:
        """
        Check if this METAR is from a synoptic time (00Z, 06Z, 12Z, 18Z)
        
        Synoptic METARs have 6-hour max/min groups in remarks.
        They're issued at :53 past the synoptic hour.
        """
        if metar.observation_time is None:
            return False
        
        hour = metar.observation_time.hour
        minute = metar.observation_time.minute
        
        # Synoptic hours are 00, 06, 12, 18
        # METARs are issued around :53-:56
        if hour in [0, 6, 12, 18] and 50 <= minute <= 59:
            return True
        
        # Also check for 6-hour groups in the text
        if " 1" in metar.raw_text and any(f" 1{d}" in metar.raw_text for d in "01"):
            # Has 1-group (6hr max)
            return True
        
        return False


# =========== Test ===========

if __name__ == "__main__":
    print("=" * 60)
    print("AVIATIONWEATHER.GOV POLLER TEST")
    print("=" * 60)
    
    poller = AviationWeatherPoller()
    
    print("\n[1] Fetching single station (KSFO)...")
    metar = poller.fetch_metar("KSFO")
    if metar:
        print(f"  Station: {metar.station}")
        print(f"  Obs Time: {metar.observation_time}")
        print(f"  Raw: {metar.raw_text[:80]}...")
        print(f"  Is Synoptic: {poller.is_synoptic_metar(metar)}")
    else:
        print("  Failed to fetch")
    
    print("\n[2] Fetching all configured stations...")
    all_metars = poller.fetch_all_stations()
    print(f"  Fetched {len(all_metars)} METARs")
    
    for m in all_metars:
        synoptic = "📊" if poller.is_synoptic_metar(m) else "  "
        print(f"  {synoptic} {m.station}: {m.observation_time.strftime('%d %H:%MZ') if m.observation_time else 'N/A'}")
    
    print("\n[3] Checking for new METARs...")
    new_metars = poller.check_for_new_metars()
    print(f"  Found {len(new_metars)} new METARs")
    
    print("\n[4] Checking again (should find 0 new)...")
    new_metars = poller.check_for_new_metars()
    print(f"  Found {len(new_metars)} new METARs")
