#!/usr/bin/env python3
"""
WX Sniper v5.0 - ASOS Scout Module (wethr_integration.py)
=========================================================

This is a "Scout" module that uses wethr.net's high-frequency 5-minute ASOS data
to detect "Logical Locks" BEFORE the hourly METAR drops.

Key Concepts:
- Uses `lowest_probable` and `highest_probable` from wethr.net API
- These fields account for rounding ambiguity in 5-minute data
- When lowest_probable > bracket.cap_strike, the bracket is MATHEMATICALLY DEAD
- This allows entering positions at ~85¢ before the market reacts to the hourly METAR

Safety Features:
- CONFLICT_SHIELD: Goes dormant during smart_poller's hot window (50-59, 0-5 mins)
- Physics Gate: Rejects readings that jump >2°F from last observation
- Staleness Check: Ignores observations >15 minutes old
- Ejection Seat: If hourly METAR contradicts Scout position, immediately exits

Integration:
- Runs alongside smart_poller.py (import and call from main loop)
- Uses same BracketState and StationState objects
- Shares Kalshi client for order execution
"""

import os
import time
import logging
import requests
import subprocess
import signal as _signal
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Optional, List, Any
from dataclasses import dataclass

# ============================================================
# CONFIGURATION
# ============================================================

# Trade quantity for Scout trades (keep low for testing)
TRADE_QUANTITY = int(os.environ.get('SCOUT_TRADE_QUANTITY', '1'))

# Scout bid price (we bid 85¢, not 99¢ - save 99¢ for METAR-confirmed kills)
BID_PRICE = int(os.environ.get('SCOUT_BID_PRICE', '99'))

# Wethr.net API configuration
WETHR_API_KEY = os.environ.get('WETHR_API_KEY', 'da0c8fe4607429123437c3d55cbfd5117652ac24506a9f8af45696a82e0fd652')
WETHR_BASE_URL = "https://wethr.net/api/v2/observations.php"
WETHR_FORECAST_URL = "https://wethr.net/api/v2/nws_forecasts.php"

# Safety thresholds
MAX_TEMP_JUMP = 2.0  # Reject readings that jump more than 2°F
MAX_STALENESS_MINUTES = 15  # Reject observations older than 15 minutes

# Polling interval - 30s to catch 5-minute ASOS drops quickly
# Professional tier allows 60 req/min, so 12 stations x 2/min = 24 req/min is safe
SCOUT_POLL_INTERVAL_SECONDS = 30

# ── Phase 2: Cadence + Proximity ──
# Variable polling intervals per phase (seconds)
CADENCE_IDLE = 900       # 15 min — far from windows
CADENCE_WATCHING = 300   # 5 min  — near/in a forecast window
CADENCE_HOT = 120        # 2 min  — close to a bracket boundary

# Proximity thresholds (Section 9E Q2)
BASE_PROXIMITY_F = 3     # Default gap threshold in °F
FAST_PROXIMITY_F = 4     # Widen threshold on fast-moving fronts
VELOCITY_FAST_THRESHOLD = 0.1  # °F per minute → "fast front"

# temp_history ring buffer size
TEMP_HISTORY_MAX = 10

# Phone-enabled stations (Section 9 orchestrator)
PHONE_STATIONS = {
    'KPHL': '+12154929617',
    'KAUS': '+15123697881',
    'KLAS': '+17025297334',
    'KSEA': '+12062142592',
}

# ── Phase 5: Phone Deployment ──
# Per-station WebSocket port for Twilio → stream_sniper communication
SNIPER_WS_PORTS = {
    'KPHL': 8766,
    'KAUS': 8767,
    'KLAS': 8768,
    'KSEA': 8769,
}

# WS URL base — Twilio calls back to this address
# Set via env var; falls back to server's public IP with nip.io for TLS
SNIPER_WS_BASE = os.environ.get('SNIPER_WS_BASE', 'wss://54-91-7-11.nip.io')

# Deployment behavior
SNIPER_LINGER_GAP_F = 3         # Stay on line if next bracket within this gap
SNIPER_COOLDOWN_SECONDS = 180   # Don't re-deploy within 3 min of hangup
SNIPER_MAX_RUNTIME_MIN = 60     # Max call session length (safety cap)
SNIPER_BID_PRICE = 99           # Phone sniper bids 99¢ (high-confidence kills)
SNIPER_TRADE_QTY = int(os.environ.get('SNIPER_TRADE_QTY', '1'))

logger = logging.getLogger('wx-sniper.scout')

# ============================================================
# FORECAST STATE (Section 9B)
# ============================================================

@dataclass
class ForecastWindow:
    """A time window where an extreme (high or low) might be set."""
    signal: str          # 'high' or 'low'
    peak_hour_lst: int   # Hour in LST (0-23) when extreme is forecast
    forecast_temp: int   # Forecast temperature in °F
    window_start_lst: int  # peak_hour - 3, clamped to 0-23
    window_end_lst: int    # peak_hour + 3, clamped to 0-23

@dataclass
class StationForecast:
    """Forecast state for one station, one day."""
    station: str
    forecast_date: str          # YYYY-MM-DD (LST)
    version: int                # NWS forecast version number
    hourly_temps: list          # 24-element array, index = LST hour
    forecast_high: Optional[int]
    forecast_low: Optional[int]
    high_windows: List[ForecastWindow]  # 1-2 windows for high
    low_windows: List[ForecastWindow]   # 1-2 windows for low
    fetched_at: datetime        # When we fetched this
    
    @property
    def age_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.fetched_at).total_seconds() / 60
    
    @property
    def is_stale(self) -> bool:
        """Forecast is stale if we haven't refreshed in 45 min."""
        return self.age_minutes > 45

# ============================================================
# SCOUT STATE TRACKING
# ============================================================

@dataclass
class ScoutPosition:
    """Tracks a position entered by the Scout."""
    ticker: str
    station: str
    side: str  # 'yes' or 'no'
    action: str  # 'BUY_YES' or 'BUY_NO'
    entry_price: int
    entry_time: datetime
    quantity: int
    reason: str
    wethr_temp: int  # Temperature that triggered the trade
    ejected: bool = False
    ejection_reason: Optional[str] = None


class ASOSScout:
    """
    The ASOS Scout monitors wethr.net's 5-minute data for early bracket locks.
    
    It detects when `lowest_probable` or `highest_probable` mathematically
    guarantees a bracket outcome, allowing trades before the hourly METAR.
    """
    
    def __init__(self, sniper: Any):
        """
        Initialize Scout with reference to main WXSniper instance.
        
        Args:
            sniper: The WXSniper instance (shares states, kalshi client)
        """
        self.sniper = sniper
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {WETHR_API_KEY}',
            'User-Agent': 'WXSniper-Scout/5.0'
        })
        
        # RAM-only temperature cache for Physics Gate
        self.last_temp: Dict[str, float] = {}
        
        # Track Scout positions for Ejection Seat
        self.positions: List[ScoutPosition] = []
        
        # Track last poll time
        self.last_poll: Optional[datetime] = None
        
        # Dormant mode flag
        self.dormant = False
        
        # ── Section 9 Forecast State ──
        # Today and tomorrow forecasts per station
        self.forecasts: Dict[str, StationForecast] = {}    # key: "KPHL_today"
        self.tomorrow_forecasts: Dict[str, StationForecast] = {}  # key: "KPHL_tomorrow"
        self.last_forecast_poll: Optional[datetime] = None
        
        # Latest observation cache for trend tracking (Section 9E/Q4)
        # Key: station, Value: list of (timestamp, temp_f) tuples, max TEMP_HISTORY_MAX
        self.temp_history: Dict[str, List[tuple]] = {}
        
        # ── Phase 2: Per-station polling + proximity state ──
        # Per-station last poll times (variable cadence per phase)
        self.station_last_obs_poll: Dict[str, datetime] = {}
        self.station_last_wethr_high_poll: Dict[str, datetime] = {}
        
        # Cached wethr_high/low per station (refreshed on WATCHING/HOT cadence)
        self.wethr_high_cache: Dict[str, Dict] = {}  # station -> {wethr_high, wethr_low, ...}
        
        # Q2 results per station for dashboard display
        # Key: station, Value: dict with gap info
        self.q2_results: Dict[str, Dict] = {}
        
        # ── Phase 5: Phone Deployment State ──
        # Active sniper subprocesses: station -> SniperProcess info
        self.active_snipers: Dict[str, Dict] = {}
        # Trade events detected from sniper log files
        self.sniper_trades: List[Dict] = []
        # Cooldown: don't re-deploy within N seconds of hangup
        self.sniper_cooldown: Dict[str, datetime] = {}
        
        logger.info("[SCOUT] ASOS Scout initialized")
        logger.info(f"[SCOUT] Trade quantity: {TRADE_QUANTITY}")
    
    # ================================================================
    # SECTION 9B: FORECAST SYNC
    # ================================================================
    
    def fetch_forecast(self, station: str, date: Optional[str] = None) -> Optional[StationForecast]:
        """
        Fetch NWS hourly forecast from wethr.net for a station/date.
        
        Args:
            station: ICAO code (e.g., 'KPHL')
            date: YYYY-MM-DD in LST, or None for today
            
        Returns:
            StationForecast or None on error
        """
        try:
            params = {'station_code': station, 'mode': 'latest'}
            if date:
                params['date'] = date
            
            resp = self.session.get(WETHR_FORECAST_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            if 'error' in data:
                logger.warning(f"[FORECAST] {station} API error: {data['error']}")
                return None
            
            hourly = data.get('hourly_temps', [])
            if not hourly or len(hourly) < 24:
                # Pad to 24 if short
                hourly = hourly + [None] * (24 - len(hourly))
            
            # Extract windows (Section 9C: bimodal for both highs and lows)
            high_windows = self._extract_high_windows(station, hourly)
            low_windows = self._extract_low_windows(station, hourly)
            
            # Get forecast high/low from non-null values
            valid_temps = [t for t in hourly if t is not None]
            forecast_high = max(valid_temps) if valid_temps else None
            forecast_low = min(valid_temps) if valid_temps else None
            
            fc = StationForecast(
                station=station,
                forecast_date=data.get('forecast_date', date or '?'),
                version=data.get('version', 0),
                hourly_temps=hourly[:24],
                forecast_high=forecast_high,
                forecast_low=forecast_low,
                high_windows=high_windows,
                low_windows=low_windows,
                fetched_at=datetime.now(timezone.utc),
            )
            
            logger.info(
                f"[FORECAST] {station} {fc.forecast_date} v{fc.version}: "
                f"H={forecast_high}°F L={forecast_low}°F | "
                f"High windows: {[(w.peak_hour_lst, w.forecast_temp) for w in high_windows]} | "
                f"Low windows: {[(w.peak_hour_lst, w.forecast_temp) for w in low_windows]}"
            )
            return fc
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[FORECAST] {station} fetch error: {e}")
            return None
        except Exception as e:
            logger.error(f"[FORECAST] {station} unexpected error: {e}")
            return None
    
    def _extract_high_windows(self, station: str, hourly: list) -> List[ForecastWindow]:
        """
        Extract 1-2 high windows from hourly temps (Section 9C).
        
        Split day into first half (0-11 LST) and second half (12-23 LST).
        Each half gets a window if its max is within 4°F of the overall max.
        """
        first_half = [(i, t) for i, t in enumerate(hourly[:12]) if t is not None]
        second_half = [(i, t) for i, t in enumerate(hourly[12:24], start=12) if t is not None]
        
        windows = []
        
        first_max = max(first_half, key=lambda x: x[1]) if first_half else None
        second_max = max(second_half, key=lambda x: x[1]) if second_half else None
        
        # Determine primary (higher max) and secondary
        candidates = []
        if first_max:
            candidates.append(first_max)
        if second_max:
            candidates.append(second_max)
        
        if not candidates:
            return windows
        
        overall_max = max(c[1] for c in candidates)
        
        for hour, temp in candidates:
            if temp >= overall_max - 4:
                windows.append(ForecastWindow(
                    signal='high',
                    peak_hour_lst=hour,
                    forecast_temp=temp,
                    window_start_lst=max(0, hour - 3),
                    window_end_lst=min(23, hour + 3),
                ))
        
        return windows
    
    def _extract_low_windows(self, station: str, hourly: list) -> List[ForecastWindow]:
        """
        Extract 1-2 low windows from hourly temps (Section 9C).
        
        Same bimodal logic as highs but looking for minima.
        """
        first_half = [(i, t) for i, t in enumerate(hourly[:12]) if t is not None]
        second_half = [(i, t) for i, t in enumerate(hourly[12:24], start=12) if t is not None]
        
        windows = []
        
        first_min = min(first_half, key=lambda x: x[1]) if first_half else None
        second_min = min(second_half, key=lambda x: x[1]) if second_half else None
        
        candidates = []
        if first_min:
            candidates.append(first_min)
        if second_min:
            candidates.append(second_min)
        
        if not candidates:
            return windows
        
        overall_min = min(c[1] for c in candidates)
        
        for hour, temp in candidates:
            if temp <= overall_min + 4:
                windows.append(ForecastWindow(
                    signal='low',
                    peak_hour_lst=hour,
                    forecast_temp=temp,
                    window_start_lst=max(0, hour - 3),
                    window_end_lst=min(23, hour + 3),
                ))
        
        return windows
    
    def sync_forecasts(self):
        """
        Fetch/refresh forecasts for ALL stations (forecast data is free).
        
        Called every 30 min but only reprocesses if version changed.
        After 10 PM local, also fetches tomorrow's forecast.
        """
        from smart_poller import STATIONS
        
        now = datetime.now(timezone.utc)
        
        for station in STATIONS:
            cfg = STATIONS.get(station, {})
            tz = ZoneInfo(cfg.get('timezone', 'America/New_York'))
            now_local = now.astimezone(tz)
            today_str = now_local.strftime('%Y-%m-%d')
            
            # ── Today's forecast ──
            key = f"{station}_today"
            existing = self.forecasts.get(key)
            
            # Fetch if: no forecast yet, or stale (>45 min), or different date
            if not existing or existing.is_stale or existing.forecast_date != today_str:
                fc = self.fetch_forecast(station, today_str)
                if fc:
                    # Only update if version changed or first fetch
                    if not existing or fc.version != existing.version or existing.forecast_date != today_str:
                        self.forecasts[key] = fc
                        logger.info(f"[FORECAST] {station} today updated: v{fc.version}")
                    else:
                        # Same version, just update fetched_at
                        existing.fetched_at = now
            
            # ── Tomorrow's forecast (after 10 PM local) ──
            if now_local.hour >= 22:
                tomorrow_local = now_local + timedelta(days=1)
                tomorrow_str = tomorrow_local.strftime('%Y-%m-%d')
                tkey = f"{station}_tomorrow"
                existing_t = self.tomorrow_forecasts.get(tkey)
                
                if not existing_t or existing_t.is_stale or existing_t.forecast_date != tomorrow_str:
                    fc = self.fetch_forecast(station, tomorrow_str)
                    if fc:
                        if not existing_t or fc.version != existing_t.version or existing_t.forecast_date != tomorrow_str:
                            self.tomorrow_forecasts[tkey] = fc
                            logger.info(f"[FORECAST] {station} TOMORROW updated: v{fc.version}")
                        else:
                            existing_t.fetched_at = now
        
        self.last_forecast_poll = now
    
    def get_forecast_for_station(self, station: str, which: str = 'today') -> Optional[StationForecast]:
        """Get the current forecast for a station. which='today' or 'tomorrow'."""
        if which == 'tomorrow':
            return self.tomorrow_forecasts.get(f"{station}_tomorrow")
        return self.forecasts.get(f"{station}_today")
    
    def is_in_forecast_window(self, station: str) -> Optional[str]:
        """
        Check if current time is in any ACTIVE forecast window for this station.
        A window is only "active" if we haven't clearly passed the peak.
        
        Returns:
            'high', 'low', 'both', or None
        """
        from smart_poller import STATIONS
        cfg = STATIONS.get(station, {})
        tz = ZoneInfo(cfg.get('timezone', 'America/New_York'))
        current_lst_hour = datetime.now(tz).hour
        
        fc = self.get_forecast_for_station(station)
        state = self.sniper.states.get(station)
        if not fc:
            return None
        
        def window_is_active(w):
            """A window is active if we're inside it AND haven't passed peak by 2+hr."""
            if not (w.window_start_lst <= current_lst_hour <= w.window_end_lst):
                return False
            hours_past_peak = current_lst_hour - w.peak_hour_lst
            if hours_past_peak < 0:
                hours_past_peak += 24
            # Past peak by 2+ hours (but not wrapped around) = window is done
            if hours_past_peak > 2 and hours_past_peak < 20:
                return False
            return True
        
        in_high = any(window_is_active(w) for w in fc.high_windows)
        in_low = any(window_is_active(w) for w in fc.low_windows)
        
        if in_high and in_low:
            return 'both'
        elif in_high:
            return 'high'
        elif in_low:
            return 'low'
        return None
    
    def get_current_phase(self, station: str) -> tuple:
        """
        Determine the current polling phase for a station (Section 9D).
        
        Returns: (phase: str, reason: str)
            phase: 'DORMANT', 'IDLE', 'WATCHING', 'HOT'
            reason: human-readable explanation for dashboard
            
        Phase names describe SYSTEM BEHAVIOR, not temperature direction:
            IDLE     = far from any window, polling every 15 min
            WATCHING = near or inside a window, polling every 5 min
            HOT      = close to a bracket boundary, polling every 2 min
        """
        from smart_poller import STATIONS
        cfg = STATIONS.get(station, {})
        tz = ZoneInfo(cfg.get('timezone', 'America/New_York'))
        current_lst_hour = datetime.now(tz).hour
        
        fc = self.get_forecast_for_station(station)
        state = self.sniper.states.get(station)
        
        if not fc:
            return ('IDLE', 'no forecast loaded')
        
        # ── DIRECTIONAL OVERRIDE ──
        # Only fire HOT if the deviation HELPS set an extreme:
        #   - Near a HIGH window AND latest > forecast+2 → climbing faster than expected
        #   - Near a LOW window AND latest < forecast-2 → dropping faster than expected
        # Being warm during a LOW window is uninteresting (means low not set yet).
        
        if state and state.latest_temp_f is not None and fc.hourly_temps[current_lst_hour] is not None:
            forecast_now = fc.hourly_temps[current_lst_hour]
            latest = state.latest_temp_f
            
            # Check HIGH windows: are we running hotter than forecast?
            for w in fc.high_windows:
                hours_past = current_lst_hour - w.peak_hour_lst
                if hours_past < 0:
                    hours_past += 24
                if hours_past <= 3 or hours_past >= 21:  # near this window
                    if latest > forecast_now + 2:
                        return ('HOT', f'running hot: {latest}°F vs fcst {forecast_now}°F (high peak @{w.peak_hour_lst:02d}h)')
            
            # Check LOW windows: are we dropping faster than forecast?
            for w in fc.low_windows:
                hours_past = current_lst_hour - w.peak_hour_lst
                if hours_past < 0:
                    hours_past += 24
                if hours_past <= 3 or hours_past >= 21:
                    if latest < forecast_now - 2:
                        return ('HOT', f'dropping fast: {latest}°F vs fcst {forecast_now}°F (low trough @{w.peak_hour_lst:02d}h)')
        
        # ── Inside an active window? ──
        window = self.is_in_forecast_window(station)
        if window:
            return ('WATCHING', f'in {window.upper()} window')
        
        # ── Approaching a FUTURE window? (only look forward, not backward) ──
        all_windows = (fc.high_windows or []) + (fc.low_windows or [])
        for w in all_windows:
            hours_until_start = w.window_start_lst - current_lst_hour
            if hours_until_start < -12:
                hours_until_start += 24  # wrap
            
            # Only future windows
            if 0 < hours_until_start <= 3:
                return ('WATCHING', f'{w.signal.upper()} window in ~{hours_until_start}hr')
        
        return ('IDLE', 'outside all windows')

    def is_conflict_window(self) -> bool:
        """
        Check if we're in smart_poller's hot window.
        
        Returns True during minutes 50-59 and 0-5 (METAR drop window).
        Scout goes DORMANT during this time to avoid conflicts.
        """
        minute = datetime.now(timezone.utc).minute
        return 50 <= minute <= 59 or minute <= 5
    
    def fetch_wethr_data(self, station: str) -> Optional[Dict]:
        """
        Fetch latest observation from wethr.net API.
        
        Args:
            station: ICAO code (e.g., 'KNYC')
            
        Returns:
            Dict with observation data or None on error
        """
        try:
            params = {
                'station_code': station,
                'mode': 'latest'
            }
            
            resp = self.session.get(WETHR_BASE_URL, params=params, timeout=10)
            resp.raise_for_status()
            
            data = resp.json()
            
            # Handle error responses
            if 'error' in data:
                logger.warning(f"[SCOUT] {station} API error: {data['error']}")
                return None
            
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[SCOUT] {station} fetch error: {e}")
            return None
        except Exception as e:
            logger.error(f"[SCOUT] {station} unexpected error: {e}")
            return None
    
    def fetch_wethr_high(self, station: str) -> Optional[Dict]:
        """
        Fetch day's running high/low from wethr.net API using wethr_high mode.
        
        This gives us the day's high/low with NWS rounding logic applied,
        allowing us to know bracket status BEFORE the hourly METAR.
        
        Args:
            station: ICAO code (e.g., 'KNYC')
            
        Returns:
            Dict with wethr_high, wethr_low, etc. or None on error
        """
        try:
            params = {
                'station_code': station,
                'mode': 'wethr_high',
                'logic': 'nws'
            }
            
            resp = self.session.get(WETHR_BASE_URL, params=params, timeout=10)
            resp.raise_for_status()
            
            data = resp.json()
            
            # Handle error responses
            if 'error' in data:
                logger.warning(f"[SCOUT] {station} wethr_high error: {data['error']}")
                return None
            
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[SCOUT] {station} wethr_high fetch error: {e}")
            return None
        except Exception as e:
            logger.error(f"[SCOUT] {station} wethr_high unexpected error: {e}")
            return None
    
    def seed_observed_temps(self):
        """
        Seed observed_high/low from wethr.net's wethr_high mode.
        
        Called on startup/rollover to populate initial high/low estimates
        so we don't get blindsided by the first METAR of the day.
        
        This ensures brackets that are already obviously dead (based on 5-min data)
        don't trigger trades when the hourly METAR merely confirms what we already knew.
        """
        logger.info("[SCOUT] Seeding observed temps from wethr.net...")
        
        for station, state in self.sniper.states.items():
            data = self.fetch_wethr_high(station)
            if not data:
                continue
            
            wethr_high = data.get('wethr_high')
            wethr_low = data.get('wethr_low')
            
            if wethr_high is not None:
                wethr_high = int(wethr_high)
                if state.observed_high is None or wethr_high > state.observed_high:
                    old = state.observed_high
                    state.observed_high = wethr_high
                    logger.info(f"[SCOUT] {station} observed_high: {old} → {wethr_high} (from wethr.net)")
            
            if wethr_low is not None:
                wethr_low = int(wethr_low)
                if state.observed_low is None or wethr_low < state.observed_low:
                    old = state.observed_low
                    state.observed_low = wethr_low
                    logger.info(f"[SCOUT] {station} observed_low: {old} → {wethr_low} (from wethr.net)")
    
    # ================================================================
    # PHASE 2: TEMP HISTORY + VELOCITY + GAP + Q2
    # ================================================================
    
    def record_temp(self, station: str, temp_f: int, obs_time: Optional[datetime] = None):
        """
        Record a temperature reading for trend/velocity tracking.
        
        Called every time we get a valid mode=latest reading.
        Stores (timestamp, temp_f) tuples, max TEMP_HISTORY_MAX per station.
        Deduplicates: skips if same temp and <60s since last record.
        """
        ts = obs_time or datetime.now(timezone.utc)
        
        if station not in self.temp_history:
            self.temp_history[station] = []
        
        history = self.temp_history[station]
        
        # Deduplicate: skip if same temp and recent
        if history:
            last_ts, last_temp = history[-1]
            if last_temp == temp_f and (ts - last_ts).total_seconds() < 60:
                return  # Same temp, too soon
        
        history.append((ts, temp_f))
        
        # Trim to max size
        if len(history) > TEMP_HISTORY_MAX:
            self.temp_history[station] = history[-TEMP_HISTORY_MAX:]
    
    def get_velocity(self, station: str) -> Optional[float]:
        """
        Compute temperature velocity from the last 3 readings.
        
        Returns: °F per minute (positive = warming, negative = cooling)
                 None if insufficient data.
        
        Uses reading[-1] vs reading[-3] per Section 9E spec.
        """
        history = self.temp_history.get(station, [])
        if len(history) < 3:
            return None
        
        ts_recent, temp_recent = history[-1]
        ts_old, temp_old = history[-3]
        
        minutes = (ts_recent - ts_old).total_seconds() / 60
        if minutes <= 0:
            return None
        
        return (temp_recent - temp_old) / minutes
    
    def get_cadence_seconds(self, station: str) -> int:
        """
        Get the polling interval for a station based on its current phase.
        
        Returns seconds between polls:
            IDLE     → 900s  (15 min)
            WATCHING → 300s  (5 min)
            HOT      → 120s  (2 min)
            DORMANT  → no poll (returns a large number)
        """
        phase, _ = self.get_current_phase(station)
        
        if phase == 'HOT':
            return CADENCE_HOT
        elif phase == 'WATCHING':
            return CADENCE_WATCHING
        elif phase == 'DORMANT':
            return 9999  # effectively don't poll
        else:
            return CADENCE_IDLE
    
    def should_poll_station(self, station: str) -> bool:
        """
        Check if enough time has passed to poll mode=latest for this station.
        
        Per Section 9D:
            IDLE     → latest NOT polled (return False)
            WATCHING → every 5 min
            HOT      → every 2 min
        """
        phase, _ = self.get_current_phase(station)
        if phase in ('IDLE', 'DORMANT'):
            return False  # Don't poll latest during IDLE — no trend tracking needed
        
        now = datetime.now(timezone.utc)
        last = self.station_last_obs_poll.get(station)
        
        if last is None:
            return True  # First poll
        
        cadence = self.get_cadence_seconds(station)
        elapsed = (now - last).total_seconds()
        
        return elapsed >= cadence
    
    def should_poll_wethr_high(self, station: str) -> bool:
        """
        Check if we should poll wethr_high for this station.
        
        Per Section 9D:
            IDLE     → every 15 min (track running high/low for bracket state)
            WATCHING → every 5 min
            HOT      → every 2 min
        """
        now = datetime.now(timezone.utc)
        last = self.station_last_wethr_high_poll.get(station)
        
        if last is None:
            return True
        
        cadence = self.get_cadence_seconds(station)
        elapsed = (now - last).total_seconds()
        
        return elapsed >= cadence
    
    def compute_gap_to_bracket(self, station: str) -> Dict:
        """
        Compute distance from current wethr_high/low to the nearest open bracket boundary.
        
        Returns dict with:
            high_gap: int or None — degrees from wethr_high to next open HIGH bracket floor
            low_gap: int or None — degrees from wethr_low to next open LOW bracket cap
            high_next_floor: int or None — the floor of the nearest open HIGH bracket
            low_next_cap: int or None — the cap of the nearest open LOW bracket
            high_next_subtitle: str — bracket label
            low_next_subtitle: str — bracket label
        """
        state = self.sniper.states.get(station)
        cached = self.wethr_high_cache.get(station, {})
        
        result = {
            'high_gap': None, 'low_gap': None,
            'high_next_floor': None, 'low_next_cap': None,
            'high_next_subtitle': '', 'low_next_subtitle': '',
            'wethr_high': None, 'wethr_low': None,
        }
        
        if not state:
            return result
        
        wethr_high = cached.get('wethr_high')
        wethr_low = cached.get('wethr_low')
        
        if wethr_high is not None:
            result['wethr_high'] = int(wethr_high)
        if wethr_low is not None:
            result['wethr_low'] = int(wethr_low)
        
        # ── HIGH gap: find lowest floor of still-OPEN HIGH brackets ──
        # "Next open bracket boundary" = the lowest strike that would trigger
        # action on a still-OPEN bracket (for highs).
        # For between: floor is the lower edge of the bracket range. When wethr_high
        #   reaches floor, the temp is IN the bracket range. Gap = floor - wethr_high.
        #   (Spec example: brackets "76-77", wethr_high=75, gap = 76-75 = 1)
        # For greater: floor is X-1 (offset). Bracket LOCKS when obs > floor.
        # For less: bracket DIES when obs >= cap.
        if wethr_high is not None and state.high_watchlist:
            wh = int(wethr_high)
            open_boundaries = []
            for b in state.high_watchlist:
                if b.status != 'open':
                    continue
                if b.strike_type == 'between' and b.floor_strike is not None:
                    # Between: use floor (lower edge of bracket range)
                    open_boundaries.append((b.floor_strike, b.subtitle))
                elif b.strike_type in ('greater', 'greater_or_equal') and b.floor_strike is not None:
                    # Greater: locks when obs > floor (floor is X-1)
                    open_boundaries.append((b.floor_strike, b.subtitle))
                elif b.strike_type in ('less', 'less_or_equal') and b.cap_strike is not None:
                    # Less: dies when obs >= cap
                    open_boundaries.append((b.cap_strike, b.subtitle))
            
            if open_boundaries:
                # Sort by boundary: nearest actionable boundary first
                open_boundaries.sort(key=lambda x: x[0])
                # Find the nearest boundary ABOVE current wethr_high
                for boundary, subtitle in open_boundaries:
                    gap = boundary - wh
                    if gap >= 0:  # Only look at boundaries we haven't passed
                        result['high_gap'] = gap
                        result['high_next_floor'] = boundary
                        result['high_next_subtitle'] = subtitle
                        break
        
        # ── LOW gap: find highest boundary of still-OPEN LOW brackets ──
        # For lows, "next open bracket boundary" = the highest boundary that
        # the temp needs to drop to in order to trigger action.
        # For between: cap is the upper edge of the bracket range. When wethr_low
        #   drops to cap, the temp enters the bracket range. Gap = wethr_low - cap.
        #   (Spec example: bracket "26-27", wethr_low=29, gap = 29-27 = 2)
        # For greater: bracket DIES when obs <= floor. Gap = wethr_low - floor.
        # For less: bracket LOCKS when obs < cap. Gap = wethr_low - cap.
        if wethr_low is not None and state.low_watchlist:
            wl = int(wethr_low)
            open_boundaries = []
            for b in state.low_watchlist:
                if b.status != 'open':
                    continue
                if b.strike_type == 'between' and b.cap_strike is not None:
                    # Between: use cap (upper edge of bracket range)
                    open_boundaries.append((b.cap_strike, b.subtitle))
                elif b.strike_type in ('greater', 'greater_or_equal') and b.floor_strike is not None:
                    # Greater: dies when obs <= floor
                    open_boundaries.append((b.floor_strike, b.subtitle))
                elif b.strike_type in ('less', 'less_or_equal') and b.cap_strike is not None:
                    # Less: locks when obs < cap
                    open_boundaries.append((b.cap_strike, b.subtitle))
            
            if open_boundaries:
                # Sort descending: nearest boundary BELOW current wethr_low
                open_boundaries.sort(key=lambda x: x[0], reverse=True)
                for boundary, subtitle in open_boundaries:
                    gap = wl - boundary
                    if gap >= 0:  # Only look at boundaries we haven't passed
                        result['low_gap'] = gap
                        result['low_next_cap'] = boundary
                        result['low_next_subtitle'] = subtitle
                        break
        
        return result
    
    def evaluate_q2(self, station: str) -> Dict:
        """
        Q2: Are we close (velocity-adjusted)?
        
        Returns dict:
            passed: bool
            high_result: str — human-readable for dashboard
            low_result: str — human-readable for dashboard
            velocity: float or None — °F/min
            proximity_threshold: int — 3 or 4
        """
        velocity = self.get_velocity(station)
        
        if velocity is not None and abs(velocity) > VELOCITY_FAST_THRESHOLD:
            proximity_threshold = FAST_PROXIMITY_F
        else:
            proximity_threshold = BASE_PROXIMITY_F
        
        gap_info = self.compute_gap_to_bracket(station)
        
        high_passed = False
        low_passed = False
        high_result = ''
        low_result = ''
        
        if gap_info['high_gap'] is not None:
            if gap_info['high_gap'] <= proximity_threshold:
                high_passed = True
                high_result = f"✅ {gap_info['high_gap']}°F to {gap_info['high_next_subtitle']} (≤{proximity_threshold})"
            else:
                high_result = f"⏳ {gap_info['high_gap']}°F to {gap_info['high_next_subtitle']} (>{proximity_threshold})"
        else:
            high_result = "— no open HIGH brackets"
        
        if gap_info['low_gap'] is not None:
            if gap_info['low_gap'] <= proximity_threshold:
                low_passed = True
                low_result = f"✅ {gap_info['low_gap']}°F to {gap_info['low_next_subtitle']} (≤{proximity_threshold})"
            else:
                low_result = f"⏳ {gap_info['low_gap']}°F to {gap_info['low_next_subtitle']} (>{proximity_threshold})"
        else:
            low_result = "— no open LOW brackets"
        
        result = {
            'passed': high_passed or low_passed,
            'high_passed': high_passed,
            'low_passed': low_passed,
            'high_result': high_result,
            'low_result': low_result,
            'velocity': velocity,
            'proximity_threshold': proximity_threshold,
            'gap_info': gap_info,
        }
        
        # Cache for dashboard
        self.q2_results[station] = result
        
        return result
    
    # ================================================================
    # SECTION 9E: Q1, Q3, Q4 + DEPLOYMENT EVALUATOR
    # ================================================================
    
    def evaluate_q1(self, station: str, signal_type: str) -> str:
        """
        Q1: Is there stale edge to capture?
        
        ⚠️ TESTING MODE: Always returns PASS.
        We want to observe deployment decisions without the liquidity filter
        masking them. Re-enable once Q2-Q4 are validated against real data.
        """
        return 'PASS(test)'
    
    def evaluate_q3(self, station: str, signal_type: str) -> str:
        """
        Q3: Does the forecast support continued movement?
        
        Compares observed trajectory vs forecast to detect:
        - AHEAD OF FORECAST: observed beating forecast by 2°F+ → PASS(ahead)
        - PEAKED: 2+ hours past peak AND declining → FAIL(peaked)
        - FORECAST BUSTED: past peak but still climbing → PASS(busted)
        - NORMAL: within forecast expectations → PASS(normal)
        """
        state = self.sniper.states.get(station)
        if not state or state.latest_temp_f is None:
            return 'PASS(no obs)'
        
        fc = self.get_forecast_for_station(station)
        if not fc or not fc.hourly_temps:
            return 'PASS(no fcst)'
        
        from smart_poller import STATIONS
        cfg = STATIONS.get(station, {})
        tz = ZoneInfo(cfg.get('timezone', 'America/New_York'))
        current_lst_hour = datetime.now(tz).hour
        
        forecast_now = fc.hourly_temps[current_lst_hour]
        if forecast_now is None:
            return 'PASS(no fcst hr)'
        
        latest = state.latest_temp_f
        
        if signal_type == 'high':
            # Ahead of forecast? (hotter than expected → market mispriced)
            if latest > forecast_now + 2:
                return f'PASS(ahead +{latest - forecast_now}°)'
            
            # Past peak check — for each high window
            for w in fc.high_windows:
                hours_past = current_lst_hour - w.peak_hour_lst
                if hours_past < 0:
                    hours_past += 24
                
                if hours_past > 2 and hours_past < 12:  # 2+ hours past, not wrapped
                    history = self.temp_history.get(station, [])
                    if len(history) >= 3:
                        last_3 = [h[1] for h in history[-3:]]
                        if last_3[0] > last_3[1] > last_3[2]:
                            return 'FAIL(peaked)'
                        elif last_3[2] > last_3[0]:
                            return 'PASS(busted)'  # Past peak but still climbing
            
            return 'PASS(normal)'
        
        else:  # low
            # Ahead of forecast? (colder than expected)
            if latest < forecast_now - 2:
                return f'PASS(ahead -{forecast_now - latest}°)'
            
            # Past trough check
            for w in fc.low_windows:
                hours_past = current_lst_hour - w.peak_hour_lst
                if hours_past < 0:
                    hours_past += 24
                
                if hours_past > 2 and hours_past < 12:
                    history = self.temp_history.get(station, [])
                    if len(history) >= 3:
                        last_3 = [h[1] for h in history[-3:]]
                        if last_3[0] < last_3[1] < last_3[2]:
                            return 'FAIL(peaked)'  # Warming after trough
                        elif last_3[2] < last_3[0]:
                            return 'PASS(busted)'
            
            return 'PASS(normal)'
    
    def evaluate_q4(self, station: str, signal_type: str) -> str:
        """
        Q4: Are we still moving toward the boundary?
        
        Check last 3 readings — 2 of 3 must be moving in the right direction.
        For highs: temp going UP. For lows: temp going DOWN.
        Edge case: if only 2 readings, require both to show movement.
        """
        history = self.temp_history.get(station, [])
        
        if len(history) < 2:
            return 'WAIT(need data)'
        
        if len(history) == 2:
            t1, t2 = history[-2][1], history[-1][1]
            if signal_type == 'high':
                return 'PASS(2/2↑)' if t2 > t1 else 'WAIT(flat/↓)'
            else:
                return 'PASS(2/2↓)' if t2 < t1 else 'WAIT(flat/↑)'
        
        # 3+ readings: check last 3
        temps = [h[1] for h in history[-3:]]
        
        if signal_type == 'high':
            moves = sum(1 for i in range(2) if temps[i+1] > temps[i])
            return f'PASS({moves}/3↑)' if moves >= 2 else f'WAIT({moves}/3↑)'
        else:
            moves = sum(1 for i in range(2) if temps[i+1] < temps[i])
            return f'PASS({moves}/3↓)' if moves >= 2 else f'WAIT({moves}/3↓)'
    
    def evaluate_deployment(self, station: str) -> Dict:
        """
        Run the full Q1-Q4 deployment decision for a station.
        
        Evaluates both HIGH and LOW signals. Stores results in q2_results
        for dashboard rendering. Returns a dict with all results.
        
        This is the master evaluator — call after compute_gap_to_bracket.
        """
        state = self.sniper.states.get(station)
        if not state:
            return {}
        
        q2_info = self.q2_results.get(station, {})
        gap_info = q2_info.get('gap_info', {})
        velocity = q2_info.get('velocity')
        
        results = {
            'high': {'q1': '—', 'q2': '—', 'q3': '—', 'q4': '—', 'signal': '—'},
            'low':  {'q1': '—', 'q2': '—', 'q3': '—', 'q4': '—', 'signal': '—'},
        }
        
        # ── HIGH signal evaluation ──
        if gap_info.get('high_gap') is not None:
            q1 = self.evaluate_q1(station, 'high')
            q3 = self.evaluate_q3(station, 'high')
            q4 = self.evaluate_q4(station, 'high')
            
            # Q2 from the already-computed gap
            h_gap = gap_info['high_gap']
            prox = q2_info.get('proximity_threshold', BASE_PROXIMITY_F)
            q2 = f'PASS(gap={h_gap})' if h_gap <= prox else f'WAIT(gap={h_gap})'
            
            all_pass = all(r.startswith('PASS') for r in [q1, q2, q3, q4])
            if all_pass:
                signal = 'DEPLOY'
            else:
                failing = [f'Q{i+1}' for i, r in enumerate([q1, q2, q3, q4]) 
                          if not r.startswith('PASS')]
                signal = f'WAIT({",".join(failing)})'
            
            results['high'] = {'q1': q1, 'q2': q2, 'q3': q3, 'q4': q4, 'signal': signal}
        
        # ── LOW signal evaluation ──
        if gap_info.get('low_gap') is not None:
            q1 = self.evaluate_q1(station, 'low')
            q3 = self.evaluate_q3(station, 'low')
            q4 = self.evaluate_q4(station, 'low')
            
            l_gap = gap_info['low_gap']
            prox = q2_info.get('proximity_threshold', BASE_PROXIMITY_F)
            q2 = f'PASS(gap={l_gap})' if l_gap <= prox else f'WAIT(gap={l_gap})'
            
            all_pass = all(r.startswith('PASS') for r in [q1, q2, q3, q4])
            if all_pass:
                signal = 'DEPLOY'
            else:
                failing = [f'Q{i+1}' for i, r in enumerate([q1, q2, q3, q4]) 
                          if not r.startswith('PASS')]
                signal = f'WAIT({",".join(failing)})'
            
            results['low'] = {'q1': q1, 'q2': q2, 'q3': q3, 'q4': q4, 'signal': signal}
        
        # Store in q2_results for dashboard (extending the existing dict)
        if station in self.q2_results:
            self.q2_results[station]['deployment'] = results
        
        return results
    
    # ================================================================
    # PHASE 5: PHONE DEPLOYMENT ORCHESTRATOR
    # ================================================================
    
    def _is_phone_station(self, station: str) -> bool:
        """Check if station is phone-enabled."""
        return station in PHONE_STATIONS and station in SNIPER_WS_PORTS
    
    def _sniper_is_active(self, station: str) -> bool:
        """Check if a sniper subprocess is running for this station."""
        info = self.active_snipers.get(station)
        if not info:
            return False
        proc = info.get('process')
        if proc is None:
            return False
        # poll() returns None if still running, returncode if exited
        return proc.poll() is None
    
    def _in_cooldown(self, station: str) -> bool:
        """Check if station is in post-hangup cooldown."""
        cd = self.sniper_cooldown.get(station)
        if not cd:
            return False
        elapsed = (datetime.now(timezone.utc) - cd).total_seconds()
        return elapsed < SNIPER_COOLDOWN_SECONDS
    
    def deploy_sniper(self, station: str, signal_type: str = 'both'):
        """
        Launch stream_sniper.py as a subprocess for a phone-enabled station.
        
        Called when evaluate_deployment() returns DEPLOY and no sniper is active.
        
        Args:
            station: ICAO code (must be in PHONE_STATIONS)
            signal_type: 'high', 'low', or 'both'
        """
        if not self._is_phone_station(station):
            return
        
        if self._sniper_is_active(station):
            logger.debug(f"[PHONE] {station} sniper already active, skipping deploy")
            return
        
        if self._in_cooldown(station):
            logger.debug(f"[PHONE] {station} in cooldown, skipping deploy")
            return
        
        port = SNIPER_WS_PORTS[station]
        ws_url = f"{SNIPER_WS_BASE}:{port}/stream"
        
        cmd = [
            'python3', 'stream_sniper.py',
            '--station', station,
            '--ws-url', ws_url,
            '--signal', signal_type,
            '--port', str(port),
            '--bid', str(SNIPER_BID_PRICE),
            '--qty', str(SNIPER_TRADE_QTY),
            '--run-minutes', str(SNIPER_MAX_RUNTIME_MIN),
        ]
        
        # Add --live if the main sniper is in live mode
        if self.sniper.live_mode:
            cmd.append('--live')
        
        try:
            log_file = f'/tmp/{station.lower()}_phone_deploy.log'
            log_fh = open(log_file, 'a')
            
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                # Don't let child die when parent gets signals
                preexec_fn=os.setpgrp,
            )
            
            self.active_snipers[station] = {
                'process': proc,
                'pid': proc.pid,
                'signal_type': signal_type,
                'started_at': datetime.now(timezone.utc),
                'log_file': log_file,
                'log_fh': log_fh,
                'trade_count': 0,
                'last_trade_check': 0,
            }
            
            logger.warning(f"[PHONE] 📞 DEPLOYED {station} sniper (pid={proc.pid}, "
                         f"signal={signal_type}, port={port}, ws={ws_url})")
            
        except Exception as e:
            logger.error(f"[PHONE] {station} deploy FAILED: {e}")
    
    def hangup_sniper(self, station: str, reason: str = 'gap too wide'):
        """
        Terminate a running sniper subprocess.
        
        Called when:
        - Deployment evaluation goes from DEPLOY to WAIT
        - Gap to next bracket grows past linger threshold
        - All brackets resolved
        - Safety timeout
        """
        info = self.active_snipers.get(station)
        if not info:
            return
        
        proc = info.get('process')
        if proc is None:
            return
        
        pid = proc.pid
        
        try:
            # Graceful: SIGTERM first
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Force kill if it doesn't stop
                proc.kill()
                proc.wait(timeout=3)
        except Exception as e:
            logger.error(f"[PHONE] {station} hangup error: {e}")
        
        # Close log file handle
        log_fh = info.get('log_fh')
        if log_fh:
            try:
                log_fh.close()
            except:
                pass
        
        duration_s = (datetime.now(timezone.utc) - info['started_at']).total_seconds()
        duration_min = duration_s / 60
        est_cost = (info.get('trade_count', 0) * 0.02) + (duration_min * 0.011)
        
        logger.warning(f"[PHONE] 📴 HANGUP {station} (pid={pid}, reason={reason}, "
                      f"duration={duration_min:.1f}min, ~${est_cost:.2f})")
        
        # Set cooldown
        self.sniper_cooldown[station] = datetime.now(timezone.utc)
        
        # Clean up
        del self.active_snipers[station]
    
    def check_sniper_health(self):
        """
        Check all active snipers — detect crashes, read trade logs.
        
        Called on every poll cycle.
        """
        dead_stations = []
        
        for station, info in self.active_snipers.items():
            proc = info.get('process')
            if proc is None:
                dead_stations.append(station)
                continue
            
            # Check if process has exited
            retcode = proc.poll()
            if retcode is not None:
                duration_s = (datetime.now(timezone.utc) - info['started_at']).total_seconds()
                if retcode == 0:
                    logger.info(f"[PHONE] {station} sniper exited normally "
                              f"(duration={duration_s/60:.1f}min)")
                else:
                    logger.warning(f"[PHONE] {station} sniper exited with code {retcode} "
                                 f"(duration={duration_s/60:.1f}min)")
                dead_stations.append(station)
                continue
            
            # Read trade log for new trades
            self._check_sniper_trades(station, info)
        
        # Clean up dead snipers
        for station in dead_stations:
            info = self.active_snipers.get(station)
            if info:
                log_fh = info.get('log_fh')
                if log_fh:
                    try:
                        log_fh.close()
                    except:
                        pass
                self.sniper_cooldown[station] = datetime.now(timezone.utc)
                del self.active_snipers[station]
    
    def _check_sniper_trades(self, station: str, info: Dict):
        """Check the sniper's CSV log for new trade events."""
        csv_file = f'/tmp/{station.lower()}_stream_sniper.csv'
        try:
            if not os.path.exists(csv_file):
                return
            
            # Only re-read if file has been modified
            mtime = os.path.getmtime(csv_file)
            if mtime <= info.get('last_trade_check', 0):
                return
            info['last_trade_check'] = mtime
            
            # Read last few lines looking for trades
            with open(csv_file, 'r') as f:
                lines = f.readlines()
            
            for line in lines[-20:]:
                # CSV format: timestamp,zulu,temp_c,candidates,changed,trade,error
                parts = line.strip().split(',')
                if len(parts) >= 6 and parts[5] and parts[5] != 'trade':
                    trade_str = parts[5]
                    # Avoid double-counting — check if we've already seen this
                    trade_key = f"{station}:{parts[0]}:{trade_str}"
                    already_seen = any(t.get('key') == trade_key for t in self.sniper_trades)
                    if not already_seen:
                        self.sniper_trades.append({
                            'key': trade_key,
                            'station': station,
                            'time': parts[0],
                            'trade': trade_str,
                            'source': 'phone',
                        })
                        info['trade_count'] = info.get('trade_count', 0) + 1
                        logger.warning(f"[PHONE] 🎯 {station} TRADE: {trade_str}")
        except Exception as e:
            logger.debug(f"[PHONE] {station} trade log read error: {e}")
    
    def evaluate_and_deploy(self, station: str):
        """
        Master Phase 5 decision: deploy, linger, or hangup.
        
        Called after evaluate_deployment() on every poll cycle for phone stations.
        
        Decision tree:
        1. If DEPLOY signal AND no active sniper AND not in cooldown → deploy
        2. If active sniper AND WAIT signal AND gap > linger threshold → hangup
        3. If active sniper AND DEPLOY signal → linger (keep running)
        4. If active sniper AND all brackets resolved → hangup
        """
        if not self._is_phone_station(station):
            return
        
        deployment = self.q2_results.get(station, {}).get('deployment', {})
        gap_info = self.q2_results.get(station, {}).get('gap_info', {})
        
        # Determine if either HIGH or LOW says DEPLOY
        high_deploy = deployment.get('high', {}).get('signal') == 'DEPLOY'
        low_deploy = deployment.get('low', {}).get('signal') == 'DEPLOY'
        any_deploy = high_deploy or low_deploy
        
        # Determine the signal type for deployment
        if high_deploy and low_deploy:
            deploy_signal = 'both'
        elif high_deploy:
            deploy_signal = 'high'
        elif low_deploy:
            deploy_signal = 'low'
        else:
            deploy_signal = None
        
        sniper_active = self._sniper_is_active(station)
        
        if sniper_active:
            # ── Sniper is running — should we linger or hangup? ──
            
            # Check if all brackets are resolved
            state = self.sniper.states.get(station)
            if state:
                all_high_resolved = not state.high_watchlist or all(
                    b.status != 'open' for b in state.high_watchlist)
                all_low_resolved = not state.low_watchlist or all(
                    b.status != 'open' for b in state.low_watchlist)
                if all_high_resolved and all_low_resolved:
                    self.hangup_sniper(station, 'all brackets resolved')
                    return
            
            # Check linger gap
            h_gap = gap_info.get('high_gap')
            l_gap = gap_info.get('low_gap')
            
            # If we have gap data, check if we're still close enough to linger
            nearest_gap = None
            if h_gap is not None and l_gap is not None:
                nearest_gap = min(h_gap, l_gap)
            elif h_gap is not None:
                nearest_gap = h_gap
            elif l_gap is not None:
                nearest_gap = l_gap
            
            if nearest_gap is not None and nearest_gap > SNIPER_LINGER_GAP_F and not any_deploy:
                self.hangup_sniper(station, f'gap={nearest_gap}°F > linger threshold, WAIT')
                return
            
            # Check safety timeout
            info = self.active_snipers.get(station, {})
            started_at = info.get('started_at')
            if started_at:
                runtime = (datetime.now(timezone.utc) - started_at).total_seconds()
                if runtime > SNIPER_MAX_RUNTIME_MIN * 60:
                    self.hangup_sniper(station, f'safety timeout ({SNIPER_MAX_RUNTIME_MIN}min)')
                    return
            
            # Otherwise: linger (keep running)
            
        else:
            # ── No sniper running — should we deploy? ──
            if any_deploy:
                self.deploy_sniper(station, deploy_signal)
    
    def parse_observation_time(self, time_str: str) -> Optional[datetime]:
        """Parse wethr.net observation_time string to datetime."""
        try:
            # Format: "2025-06-15 18:53:00"
            dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    
    def is_stale(self, obs_time: datetime) -> bool:
        """Check if observation is too old to trade on."""
        age = datetime.now(timezone.utc) - obs_time
        return age > timedelta(minutes=MAX_STALENESS_MINUTES)
    
    def physics_gate(self, station: str, current_temp: float) -> bool:
        """
        RAM-only sanity check to reject glitchy readings.
        
        Returns True if reading passes (is valid), False if it should be rejected.
        """
        if station in self.last_temp:
            last = self.last_temp[station]
            jump = abs(current_temp - last)
            if jump > MAX_TEMP_JUMP:
                logger.warning(f"[SCOUT] {station} Physics Gate REJECT: "
                              f"{last}°F -> {current_temp}°F (jump={jump:.1f}°F)")
                return False
        
        # Update cache
        self.last_temp[station] = current_temp
        return True
    
    def check_high_lock(self, station: str, lowest_probable: int, 
                        brackets: List[Any]) -> List[Dict]:
        """
        Check for HIGH bracket locks using lowest_probable.
        
        Kalshi convention:
          between: cap is actual cap. DEAD when lowest_probable > cap.
          less: cap = X+1, YES wins when final < cap. DEAD when lowest_probable >= cap.
        
        Returns list of trade signals.
        """
        signals = []
        
        for bracket in brackets:
            # Skip already-traded or non-open brackets
            if bracket.traded or bracket.status != 'open':
                continue
            
            # Only check brackets with a cap (between or less-than types)
            if bracket.cap_strike is None:
                continue
            
            is_dead = False
            if bracket.strike_type in ('less', 'less_or_equal'):
                # YES wins when final < cap. DEAD when lowest_probable >= cap.
                is_dead = lowest_probable >= bracket.cap_strike
            else:
                # between: DEAD when lowest_probable > cap
                is_dead = lowest_probable > bracket.cap_strike
            
            if is_dead:
                signals.append({
                    'bracket': bracket,
                    'action': 'BUY_NO',
                    'reason': f"SCOUT: lowest_probable {lowest_probable}°F vs cap {bracket.cap_strike}°F ({bracket.strike_type})",
                    'wethr_temp': lowest_probable
                })
                logger.info(f"[SCOUT] {station} HIGH-LOCK: {bracket.subtitle} "
                           f"(lowest_probable={lowest_probable} vs cap={bracket.cap_strike}, {bracket.strike_type})")
        
        return signals
    
    def check_low_lock(self, station: str, highest_probable: int,
                       brackets: List[Any]) -> List[Dict]:
        """
        Check for LOW bracket locks using highest_probable.
        
        Kalshi convention:
          between: floor is actual floor. DEAD when highest_probable < floor.
          greater: floor = X-1, YES wins when final > floor. DEAD when highest_probable <= floor.
        
        Returns list of trade signals.
        """
        signals = []
        
        for bracket in brackets:
            # Skip already-traded or non-open brackets
            if bracket.traded or bracket.status != 'open':
                continue
            
            # Only check brackets with a floor (between or greater-than types)
            if bracket.floor_strike is None:
                continue
            
            is_dead = False
            if bracket.strike_type in ('greater', 'greater_or_equal'):
                # YES wins when final > floor. DEAD when highest_probable <= floor.
                is_dead = highest_probable <= bracket.floor_strike
            else:
                # between: DEAD when highest_probable < floor
                is_dead = highest_probable < bracket.floor_strike
            
            if is_dead:
                signals.append({
                    'bracket': bracket,
                    'action': 'BUY_NO',
                    'reason': f"SCOUT: highest_probable {highest_probable}°F vs floor {bracket.floor_strike}°F ({bracket.strike_type})",
                    'wethr_temp': highest_probable
                })
                logger.info(f"[SCOUT] {station} LOW-LOCK: {bracket.subtitle} "
                           f"(highest_probable={highest_probable} vs floor={bracket.floor_strike}, {bracket.strike_type})")
        
        return signals
    
    def check_dsm_kill(self, station: str, dsm_high: Optional[int],
                       brackets: List[Any]) -> None:
        """
        Check for DSM invalidation (permanent bracket death).
        
        If dsm_high_display > bracket.cap_strike, the bracket is permanently dead.
        DSM is the Daily Summary Message - an official NWS product.
        """
        if dsm_high is None:
            return
        
        for bracket in brackets:
            if bracket.cap_strike is None:
                continue
            
            if dsm_high > bracket.cap_strike and bracket.status == 'open':
                logger.info(f"[SCOUT] {station} DSM-KILL: {bracket.subtitle} "
                           f"(dsm_high={dsm_high} > cap={bracket.cap_strike})")
                # Mark as dead but don't trade - let smart_poller handle it
                # This is just informational logging
    
    def execute_scout_trade(self, signal: Dict, station: str) -> bool:
        """
        Execute a Scout trade — BUY ONLY, hold to settlement.
        
        NO HEDGE. Settlement pays 100¢. Selling at 99¢ gives your profit away.
        Only the Ejection Seat should sell (market order if the position is wrong).
        
        Profit = (100 - entry_price)¢ per contract.
        At Scout's BID_PRICE of 99¢, that's 1¢/contract if correct.
        """
        bracket = signal['bracket']
        action = signal['action']
        side = 'no' if action == 'BUY_NO' else 'yes'
        
        # GLOBAL TICKER LOCK — prevents Scout + METAR sniper double-trade
        if bracket.ticker in self.sniper.processed_tickers:
            logger.info(f"[SCOUT] {bracket.ticker} already traded (global lock), skipping")
            return False
        
        # Check price
        price = bracket.no_ask if side == 'no' else bracket.yes_ask
        if price > self.sniper.max_price:
            logger.info(f"[SCOUT] {station} {bracket.subtitle}: Price {price}¢ > max {self.sniper.max_price}¢, skipping")
            return False
        
        logger.info(f"[SCOUT] EXECUTING: {action} {bracket.ticker} @ {BID_PRICE}¢ x{TRADE_QUANTITY}")
        
        if not self.sniper.live_mode:
            logger.info(f"[SCOUT] DRY RUN — holding to settlement (no hedge)")
            position = ScoutPosition(
                ticker=bracket.ticker,
                station=station,
                side=side,
                action=action,
                entry_price=price,
                entry_time=datetime.now(timezone.utc),
                quantity=TRADE_QUANTITY,
                reason=signal['reason'],
                wethr_temp=signal['wethr_temp']
            )
            self.positions.append(position)
            bracket.traded = True
            self.sniper.processed_tickers.add(bracket.ticker)
            return True
        
        try:
            # BUY ONLY — hold to settlement for (100 - BID_PRICE)¢ profit
            result = self.sniper.kalshi.create_order(
                ticker=bracket.ticker,
                side=side,
                action='buy',
                count=TRADE_QUANTITY,
                order_type='limit',
                price_cents=BID_PRICE
            )
            
            order_id = result.get('order', {}).get('order_id')
            logger.info(f"[SCOUT] Buy placed: {order_id} @ {BID_PRICE}¢ — holding to settlement")
            
            # Track position for Ejection Seat monitoring
            position = ScoutPosition(
                ticker=bracket.ticker,
                station=station,
                side=side,
                action=action,
                entry_price=BID_PRICE,
                entry_time=datetime.now(timezone.utc),
                quantity=TRADE_QUANTITY,
                reason=signal['reason'],
                wethr_temp=signal['wethr_temp']
            )
            self.positions.append(position)
            
            bracket.traded = True
            self.sniper.processed_tickers.add(bracket.ticker)
            return True
            
        except Exception as e:
            logger.error(f"[SCOUT] Trade execution failed: {e}")
            return False
    
    def check_ejection_seat(self, station: str, observed_high: Optional[int], 
                            observed_low: Optional[int], brackets: List[Any]):
        """
        The Ejection Seat: If hourly METAR contradicts Scout position, exit immediately.
        
        Called by smart_poller when it processes a new METAR.
        
        Logic:
        - For HIGH brackets where we bought NO (expecting bracket to die):
          If METAR observed_high <= bracket.cap_strike, the bracket is SAFE (we were wrong)
          
        - For LOW brackets where we bought NO (expecting bracket to die):
          If METAR observed_low >= bracket.floor_strike, the bracket is SAFE (we were wrong)
        
        Args:
            station: Station code
            observed_high: Current day's high from METAR
            observed_low: Current day's low from METAR
            brackets: List of BracketState objects to check against
        """
        # Build a lookup of brackets by ticker
        bracket_lookup = {b.ticker: b for b in brackets}
        
        for position in self.positions:
            if position.station != station or position.ejected:
                continue
            
            # Find the bracket for this position
            bracket = bracket_lookup.get(position.ticker)
            if not bracket:
                continue
            
            should_eject = False
            ejection_reason = None
            
            # Check if METAR contradicts our position
            if position.action == 'BUY_NO':
                # We bought NO expecting the bracket to be DEAD
                
                if bracket.signal_type == 'high' and observed_high is not None:
                    # For HIGH brackets: if observed_high <= cap, bracket is still SAFE
                    if bracket.cap_strike is not None and observed_high <= bracket.cap_strike:
                        should_eject = True
                        ejection_reason = (f"METAR HIGH {observed_high}°F <= cap {bracket.cap_strike}°F "
                                          f"(Scout predicted > cap)")
                
                elif bracket.signal_type == 'low' and observed_low is not None:
                    # For LOW brackets: if observed_low >= floor, bracket is still SAFE
                    if bracket.floor_strike is not None and observed_low >= bracket.floor_strike:
                        should_eject = True
                        ejection_reason = (f"METAR LOW {observed_low}°F >= floor {bracket.floor_strike}°F "
                                          f"(Scout predicted < floor)")
            
            if should_eject:
                logger.warning(f"[SCOUT] EJECTION TRIGGERED: {position.ticker}")
                logger.warning(f"[SCOUT] Reason: {ejection_reason}")
                
                position.ejected = True
                position.ejection_reason = ejection_reason
                
                # Execute market sell to exit position immediately
                if self.sniper.live_mode:
                    try:
                        eject_result = self.sniper.kalshi.create_order(
                            ticker=position.ticker,
                            side=position.side,
                            action='sell',
                            count=position.quantity,
                            order_type='market',  # Market order for immediate exit
                            price_cents=1  # Will fill at best available
                        )
                        logger.warning(f"[SCOUT] EJECTED: {eject_result.get('order', {}).get('order_id')}")
                    except Exception as e:
                        logger.error(f"[SCOUT] EJECTION FAILED: {e}")
                        # Try limit sell at 50¢ as backup
                        try:
                            backup_result = self.sniper.kalshi.create_order(
                                ticker=position.ticker,
                                side=position.side,
                                action='sell',
                                count=position.quantity,
                                order_type='limit',
                                price_cents=50  # Fire sale
                            )
                            logger.warning(f"[SCOUT] EJECTED (backup): {backup_result.get('order', {}).get('order_id')}")
                        except Exception as e2:
                            logger.error(f"[SCOUT] BACKUP EJECTION FAILED: {e2}")
                else:
                    logger.warning(f"[SCOUT] DRY RUN - would have ejected")
    
    def purge_stale_positions(self):
        """
        Purge positions from previous days.
        Called at the start of poll() to clean up old positions.
        """
        if not self.positions:
            return
        
        now = datetime.now(timezone.utc)
        stale_count = 0
        fresh_positions = []
        
        for pos in self.positions:
            # Get station's local timezone
            from smart_poller import STATIONS
            cfg = STATIONS.get(pos.station, {})
            tz_name = cfg.get('timezone', 'America/New_York')
            tz = ZoneInfo(tz_name)
            
            # Check if position is from today (local time)
            pos_local = pos.entry_time.astimezone(tz)
            now_local = now.astimezone(tz)
            
            if pos_local.date() < now_local.date():
                logger.info(f"[SCOUT] Purging stale position: {pos.ticker} (from {pos_local.date()})")
                stale_count += 1
            else:
                fresh_positions.append(pos)
        
        if stale_count > 0:
            logger.info(f"[SCOUT] Purged {stale_count} stale positions")
            self.positions = fresh_positions
    
    def poll(self):
        """
        Main Scout polling loop iteration.
        
        Phase 2: Variable cadence per station based on forecast phase.
            IDLE     → poll mode=latest every 15 min
            WATCHING → poll mode=latest every 5 min + mode=wethr_high every 5 min
            HOT      → poll both every 2 min
        
        Also: populates temp_history, computes Q2 gap/velocity for dashboard.
        """
        now = datetime.now(timezone.utc)
        
        # ── Forecast sync (every 30 min, or first run) ──
        if not self.last_forecast_poll or (now - self.last_forecast_poll).total_seconds() > 1800:
            try:
                self.sync_forecasts()
            except Exception as e:
                logger.error(f"[FORECAST] Sync error: {e}")
        
        # Check for conflict window (METAR drop time)
        if self.is_conflict_window():
            if not self.dormant:
                logger.info("[SCOUT] Entering DORMANT mode (conflict window)")
                self.dormant = True
            return
        
        if self.dormant:
            logger.info("[SCOUT] Exiting DORMANT mode")
            self.dormant = False
        
        # Purge any stale positions from previous days
        self.purge_stale_positions()
        
        # Count active stations for logging
        active_stations = 0
        skipped_cadence = 0
        
        # Poll each active station (variable cadence per station)
        for station, state in self.sniper.states.items():
            # KLAS is handled by klas_phone_sniper.py — skip in Scout
            # KSFO is handled by stream sniper — skip in Scout
            if station in ('KLAS', 'KSFO'):
                continue
            
            # Skip stations with no open brackets
            if not state.high_watchlist and not state.low_watchlist:
                continue
            
            # ── mode=latest: only during WATCHING/HOT ──
            # IDLE skips latest (no trend tracking needed far from windows)
            poll_latest = self.should_poll_station(station)
            
            if poll_latest:
                active_stations += 1
                self.station_last_obs_poll[station] = now
                
                data = self.fetch_wethr_data(station)
                if data:
                    # Parse observation time
                    obs_time_str = data.get('observation_time')
                    obs_time = None
                    if obs_time_str:
                        obs_time = self.parse_observation_time(obs_time_str)
                        if obs_time and self.is_stale(obs_time):
                            logger.debug(f"[SCOUT] {station} observation stale, skipping latest")
                            data = None  # Mark as stale but don't skip the whole station
                    
                    if data:
                        temp_display = data.get('temperature_display')
                        lowest_probable = data.get('lowest_probable')
                        highest_probable = data.get('highest_probable')
                        dsm_high = data.get('dsm_high_display')
                        
                        # Physics Gate (BEFORE recording — don't corrupt velocity)
                        physics_ok = True
                        if temp_display is not None:
                            if not self.physics_gate(station, float(temp_display)):
                                physics_ok = False
                        
                        if physics_ok:
                            # Record to temp_history (Phase 2)
                            if temp_display is not None:
                                try:
                                    self.record_temp(station, int(round(float(temp_display))), obs_time)
                                except (ValueError, TypeError):
                                    pass
                            
                            # Check for HIGH bracket locks
                            if lowest_probable is not None and state.high_watchlist:
                                signals = self.check_high_lock(station, int(lowest_probable), state.high_watchlist)
                                for signal in signals:
                                    self.execute_scout_trade(signal, station)
                            
                            # Check for LOW bracket locks
                            if highest_probable is not None and state.low_watchlist:
                                signals = self.check_low_lock(station, int(highest_probable), state.low_watchlist)
                                for signal in signals:
                                    self.execute_scout_trade(signal, station)
                            
                            # Check DSM invalidation (informational)
                            if dsm_high is not None and state.high_watchlist:
                                self.check_dsm_kill(station, int(dsm_high), state.high_watchlist)
            else:
                skipped_cadence += 1
            
            # ── mode=wethr_high: running day's high/low (Phase 2) ──
            # Polls during ALL phases (IDLE/WATCHING/HOT) — cadence varies
            if self.should_poll_wethr_high(station):
                self.station_last_wethr_high_poll[station] = now
                wh_data = self.fetch_wethr_high(station)
                if wh_data:
                    self.wethr_high_cache[station] = wh_data
                    
                    # Also update observed_high/low from wethr_high
                    wh = wh_data.get('wethr_high')
                    wl = wh_data.get('wethr_low')
                    if wh is not None:
                        wh = int(wh)
                        if state.observed_high is None or wh > state.observed_high:
                            state.observed_high = wh
                    if wl is not None:
                        wl = int(wl)
                        if state.observed_low is None or wl < state.observed_low:
                            state.observed_low = wl
            
            # ── Q2 evaluation (Phase 2) ──
            # Only compute if we have wethr_high data cached
            if station in self.wethr_high_cache:
                self.evaluate_q2(station)
                
                # ── Q1-Q4 full deployment evaluation ──
                self.evaluate_deployment(station)
                
                # ── Phase 5: Phone deployment decision ──
                self.evaluate_and_deploy(station)
        
        # ── Phase 5: Check health of all active snipers ──
        self.check_sniper_health()
        
        if active_stations > 0 or skipped_cadence > 0:
            n_snipers = sum(1 for s in self.active_snipers if self._sniper_is_active(s))
            sniper_str = f" | 📞 {n_snipers} active" if n_snipers > 0 else ""
            logger.info(f"[SCOUT] Polled {active_stations} stations, {skipped_cadence} skipped (cadence) | Positions: {len(self.positions)}{sniper_str}")


# ============================================================
# INTEGRATION HELPER
# ============================================================

def create_scout(sniper: Any) -> ASOSScout:
    """
    Factory function to create an ASOS Scout instance.
    
    Usage in smart_poller.py:
        from wethr_integration import create_scout
        
        # In WXSniper.__init__():
        self.scout = create_scout(self)
        
        # After init_watchlists/fetch_historical_temps:
        self.scout.seed_observed_temps()
        
        # In main loop:
        self.scout.poll()
        
        # After date rollover:
        self.scout.seed_observed_temps()
    """
    scout = ASOSScout(sniper)
    # NOTE: Don't seed here - fetch_historical_temps() will reset observed_high/low
    # Caller should call seed_observed_temps() AFTER fetch_historical_temps()
    return scout


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    # Test wethr.net API connectivity
    logging.basicConfig(level=logging.INFO)
    
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {WETHR_API_KEY}',
        'User-Agent': 'WXSniper-Scout/5.0-test'
    })
    
    test_stations = ['KNYC', 'KMDW', 'KLAX']
    
    print("Testing wethr.net API connectivity...\n")
    
    for station in test_stations:
        try:
            resp = session.get(WETHR_BASE_URL, params={
                'station_code': station,
                'mode': 'latest'
            }, timeout=10)
            
            data = resp.json()
            
            print(f"{station}:")
            print(f"  observation_time: {data.get('observation_time')}")
            print(f"  temperature_display: {data.get('temperature_display')}°F")
            print(f"  lowest_probable: {data.get('lowest_probable')}°F")
            print(f"  highest_probable: {data.get('highest_probable')}°F")
            print(f"  dsm_high_display: {data.get('dsm_high_display')}°F")
            print()
            
        except Exception as e:
            print(f"{station}: ERROR - {e}\n")
