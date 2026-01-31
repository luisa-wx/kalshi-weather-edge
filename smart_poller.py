#!/usr/bin/env python3
"""
WX Sniper v4.6 - AWS Migration + Latency Optimizations
========================================================

Changes from v4.5:
- GRACEFUL SHUTDOWN: Handle SIGTERM/SIGINT for ECS/systemd
- CONNECTION WARMING: Pre-establish TCP/TLS on startup
- LATENCY TRACKING: Log request latencies for optimization
- AWS SECRETS MANAGER: Optional integration for credentials
- STRUCTURED LOGGING: JSON-formatted logs for CloudWatch
- PRE-COMPUTED DATE SUFFIX: Avoid repeated datetime formatting

DEPLOYMENT:
- EC2: Use systemd service with RestartSec=5
- ECS: Use health check on /health endpoint
- Environment: AWS_REGION=us-east-1 for lowest Kalshi latency

See AWS_MIGRATION_GUIDE.md for full deployment instructions.
"""

import os
import re
import sys
import time
import json
import math
import signal
import logging
import threading
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from http.server import HTTPServer, BaseHTTPRequestHandler
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# LOGGING SETUP (CloudWatch-friendly)
# ============================================================

# Configure structured logging for CloudWatch
LOG_FORMAT = '%(asctime)s %(levelname)s %(message)s'
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt='%Y-%m-%dT%H:%M:%SZ',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('wx-sniper')

def log_event(event_type: str, **kwargs):
    """Log structured event for CloudWatch Insights queries."""
    data = {'event': event_type, 'ts': datetime.now(timezone.utc).isoformat(), **kwargs}
    logger.info(json.dumps(data))

# ============================================================
# CONFIGURATION
# ============================================================

STATIONS = {
    "KNYC": {
        "kalshi_high_ticker": "KXHIGHNY",
        "kalshi_low_ticker": "KXLOWTNYC",
        "timezone": "America/New_York",
        "name": "NYC"
    },
    "KPHL": {
        "kalshi_high_ticker": "KXHIGHPHIL",
        "kalshi_low_ticker": "KXLOWTPHIL",
        "timezone": "America/New_York",
        "name": "Philadelphia"
    },
    "KMDW": {
        "kalshi_high_ticker": "KXHIGHCHI",
        "kalshi_low_ticker": "KXLOWTCHI",
        "timezone": "America/Chicago",
        "name": "Chicago"
    },
    "KLAX": {
        "kalshi_high_ticker": "KXHIGHLAX",
        "kalshi_low_ticker": "KXLOWTLAX",
        "timezone": "America/Los_Angeles",
        "name": "Los Angeles"
    },
    "KMIA": {
        "kalshi_high_ticker": "KXHIGHMIA",
        "kalshi_low_ticker": "KXLOWTMIA",
        "timezone": "America/New_York",
        "name": "Miami"
    },
    "KAUS": {
        "kalshi_high_ticker": "KXHIGHAUS",
        "kalshi_low_ticker": "KXLOWTAUS",
        "timezone": "America/Chicago",
        "name": "Austin"
    },
    "KSFO": {
        "kalshi_high_ticker": "KXHIGHTSFO",
        "kalshi_low_ticker": None,
        "timezone": "America/Los_Angeles",
        "name": "San Francisco"
    },
    "KSEA": {
        "kalshi_high_ticker": "KXHIGHTSEA",
        "kalshi_low_ticker": None,
        "timezone": "America/Los_Angeles",
        "name": "Seattle"
    },
    "KDCA": {
        "kalshi_high_ticker": "KXHIGHTDC",
        "kalshi_low_ticker": None,
        "timezone": "America/New_York",
        "name": "Washington DC"
    },
    "KMSY": {
        "kalshi_high_ticker": "KXHIGHTNOLA",
        "kalshi_low_ticker": None,
        "timezone": "America/Chicago",
        "name": "New Orleans"
    },
    "KLAS": {
        "kalshi_high_ticker": "KXHIGHTLV",
        "kalshi_low_ticker": None,
        "timezone": "America/Los_Angeles",
        "name": "Las Vegas"
    },
    "KDEN": {
        "kalshi_high_ticker": "KXHIGHDEN",
        "kalshi_low_ticker": "KXLOWTDEN",
        "timezone": "America/Denver",
        "name": "Denver"
    }
}

# ============================================================
# AWS SECRETS MANAGER (Optional)
# ============================================================

def get_aws_secret(secret_name: str, region: str = "us-east-1") -> Optional[dict]:
    """
    Fetch secrets from AWS Secrets Manager.
    Returns None if boto3 not available or secret not found.
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
        
        client = boto3.client('secretsmanager', region_name=region)
        response = client.get_secret_value(SecretId=secret_name)
        return json.loads(response['SecretString'])
    except ImportError:
        logger.debug("boto3 not installed, skipping Secrets Manager")
        return None
    except Exception as e:
        logger.debug(f"Secrets Manager unavailable: {e}")
        return None

# ============================================================
# METAR PARSING
# ============================================================

@dataclass
class ParsedMETAR:
    raw: str
    temp_c: Optional[float] = None
    temp_f: Optional[int] = None
    six_hr_max_c: Optional[float] = None
    six_hr_min_c: Optional[float] = None

def nws_round(temp_f: float) -> int:
    """NWS rounding: round half up (asymmetric)."""
    return math.floor(temp_f + 0.5)

def c_to_f_nws(temp_c: float) -> int:
    """Convert Celsius to Fahrenheit with NWS rounding."""
    return nws_round(temp_c * 9/5 + 32)

def parse_metar(raw: str) -> ParsedMETAR:
    """Parse METAR string for temperature data."""
    result = ParsedMETAR(raw=raw)
    
    # T-group: T01061000 = +10.6°C temp, +0.0°C dewpoint
    t_match = re.search(r'\bT(\d)(\d{3})(\d)(\d{3})\b', raw)
    if t_match:
        temp_sign = -1 if t_match.group(1) == '1' else 1
        result.temp_c = temp_sign * int(t_match.group(2)) / 10.0
        result.temp_f = nws_round(result.temp_c * 9/5 + 32)
    
    # 6-hour max (1-group): 1snTTT where sn=sign, TTT=tenths C
    rmk_idx = raw.find('RMK')
    if rmk_idx > 0:
        remarks = raw[rmk_idx:]
        max_match = re.search(r'\b1([01])(\d{3})\b', remarks)
        if max_match:
            sign = -1 if max_match.group(1) == '1' else 1
            result.six_hr_max_c = sign * int(max_match.group(2)) / 10.0
        
        min_match = re.search(r'\b2([01])(\d{3})\b', remarks)
        if min_match:
            sign = -1 if min_match.group(1) == '1' else 1
            result.six_hr_min_c = sign * int(min_match.group(2)) / 10.0
    
    return result

# ============================================================
# BRACKET STATE
# ============================================================

class BracketState:
    """Tracks state of a single bracket."""
    
    def __init__(self, ticker: str, subtitle: str, floor_strike: Optional[int], 
                 cap_strike: Optional[int], strike_type: str, signal_type: str, station: str):
        self.ticker = ticker
        self.subtitle = subtitle
        self.floor_strike = floor_strike
        self.cap_strike = cap_strike
        self.strike_type = strike_type
        self.signal_type = signal_type
        self.station = station
        
        self.no_ask: int = 100
        self.yes_ask: int = 100
        self.status: str = 'open'
        self.traded: bool = False
    
    def check_status(self, observed_high: Optional[int], observed_low: Optional[int]) -> str:
        """Determine current status based on observations."""
        
        if self.signal_type == 'high':
            if observed_high is None:
                return 'open'
            
            if self.strike_type == 'between':
                if self.cap_strike is not None and observed_high > self.cap_strike:
                    return 'dead'
                return 'open'
            
            elif self.strike_type == 'greater':
                if self.floor_strike is not None and observed_high >= self.floor_strike:
                    return 'locked'
                return 'open'
            
            elif self.strike_type == 'less':
                if self.cap_strike is not None and observed_high > self.cap_strike:
                    return 'dead'
                return 'open'
        
        elif self.signal_type == 'low':
            if observed_low is None:
                return 'open'
            
            if self.strike_type == 'between':
                if self.floor_strike is not None and observed_low < self.floor_strike:
                    return 'dead'
                return 'open'
            
            elif self.strike_type == 'greater':
                if self.floor_strike is not None and observed_low < self.floor_strike:
                    return 'dead'
                return 'open'
            
            elif self.strike_type == 'less':
                if self.cap_strike is not None and observed_low <= self.cap_strike:
                    return 'locked'
                return 'open'
        
        return 'open'
    
    def describe_resolution(self, observed_high: Optional[int], observed_low: Optional[int]) -> str:
        """Get human-readable explanation of why bracket resolved."""
        status = self.check_status(observed_high, observed_low)
        
        if self.signal_type == 'high':
            if status == 'dead':
                return f"HIGH {observed_high}°F > cap {self.cap_strike}°F"
            elif status == 'locked':
                return f"HIGH {observed_high}°F >= floor {self.floor_strike}°F"
        else:
            if status == 'dead':
                return f"LOW {observed_low}°F < floor {self.floor_strike}°F"
            elif status == 'locked':
                return f"LOW {observed_low}°F <= cap {self.cap_strike}°F"
        
        return "Still open"

# ============================================================
# STATION STATE
# ============================================================

@dataclass
class StationState:
    station: str
    observed_high: Optional[int] = None
    observed_low: Optional[int] = None
    latest_metar: Optional[str] = None
    latest_temp_f: Optional[int] = None
    metar_time: Optional[datetime] = None
    current_local_date: Optional[str] = None
    
    high_watchlist: List[BracketState] = field(default_factory=list)
    low_watchlist: List[BracketState] = field(default_factory=list)
    resolved_brackets: List[BracketState] = field(default_factory=list)

# ============================================================
# KALSHI CLIENT (with latency tracking)
# ============================================================

class KalshiClient:
    def __init__(self):
        # Try AWS Secrets Manager first
        secrets = get_aws_secret(os.environ.get('AWS_SECRET_NAME', 'wx-sniper/kalshi'))
        
        if secrets:
            self.api_key_id = secrets.get('api_key_id', '')
            self.private_key_str = secrets.get('private_key', '')
            logger.info("Loaded credentials from AWS Secrets Manager")
        else:
            self.api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
            self.private_key_str = os.environ.get("KALSHI_PRIVATE_KEY", "")
        
        self.base_url = "https://api.elections.kalshi.com/trade-api/v2"
        
        # Optimized HTTP session
        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=0,  # No retries for latency-critical ops
        )
        self.session.mount('https://', adapter)
        
        # Latency tracking
        self.last_latency_ms: float = 0
        
        if self.api_key_id and self.private_key_str:
            self.private_key = self._load_private_key()
        else:
            self.private_key = None
            logger.warning("No API credentials found")
    
    def _load_private_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        
        key_str = self.private_key_str.strip()
        key_str = key_str.replace('\\n', '\n').replace('\\r', '')
        
        if '\n' not in key_str and 'PRIVATE KEY' in key_str:
            match = re.search(
                r'(-----BEGIN [A-Z ]*PRIVATE KEY-----)\s*'
                r'([A-Za-z0-9+/=\s]+?)\s*'
                r'(-----END [A-Z ]*PRIVATE KEY-----)',
                key_str
            )
            if match:
                header, content, footer = match.groups()
                content = ''.join(content.split())
                lines = [content[i:i+64] for i in range(0, len(content), 64)]
                key_str = header + '\n' + '\n'.join(lines) + '\n' + footer + '\n'
        
        if not key_str.endswith('\n'):
            key_str += '\n'
        
        return serialization.load_pem_private_key(
            key_str.encode(),
            password=None,
            backend=default_backend()
        )
    
    def _sign_request(self, timestamp_ms: int, method: str, path: str) -> str:
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        
        message = f"{timestamp_ms}{method}{path}"
        signature = self.private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode()
    
    def _make_request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        start = time.perf_counter()
        
        timestamp_ms = int(time.time() * 1000)
        sign_path = f"/trade-api/v2{endpoint}"
        url = f"{self.base_url}{endpoint}"
        
        if params:
            query_string = "&".join(f"{k}={v}" for k, v in params.items())
            sign_path = f"/trade-api/v2{endpoint}?{query_string}"
            url = f"{self.base_url}{endpoint}?{query_string}"
        
        signature = self._sign_request(timestamp_ms, method.upper(), sign_path)
        
        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "Content-Type": "application/json"
        }
        
        if method.upper() == "GET":
            response = self.session.get(url, headers=headers, timeout=10)
        elif method.upper() == "POST":
            response = self.session.post(url, headers=headers, json=data, timeout=10)
        else:
            raise ValueError(f"Unsupported method: {method}")
        
        self.last_latency_ms = (time.perf_counter() - start) * 1000
        
        response.raise_for_status()
        return response.json()
    
    def get_event(self, event_ticker: str) -> Dict:
        return self._make_request("GET", f"/events/{event_ticker}", 
                                  params={"with_nested_markets": "true"})
    
    def create_order(self, ticker: str, side: str, action: str, count: int, 
                     order_type: str = "limit", price_cents: int = None) -> Dict:
        data = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type
        }
        if order_type == "limit" and price_cents is not None:
            if side == "yes":
                data["yes_price"] = price_cents
            else:
                data["no_price"] = price_cents
        
        log_event('order_create', **data)
        result = self._make_request("POST", "/portfolio/orders", data=data)
        log_event('order_result', latency_ms=self.last_latency_ms, success=True)
        return result

# ============================================================
# MAIN SNIPER
# ============================================================

class WXSniper:
    def __init__(self):
        self.kalshi = KalshiClient()
        
        # Optimized NWS session
        self.nws_session = requests.Session()
        adapter = HTTPAdapter(pool_connections=15, pool_maxsize=15, max_retries=0)
        self.nws_session.mount('https://', adapter)
        self.nws_session.headers.update({'User-Agent': 'WXSniper/4.6'})
        
        # Config
        self.live_mode = os.environ.get('LIVE_MODE', 'false').lower() == 'true'
        self.max_price = int(os.environ.get('MAX_PRICE', '95'))
        self.order_size = int(os.environ.get('ORDER_SIZE', '10'))
        
        # State
        self.states: Dict[str, StationState] = {
            station: StationState(station=station) for station in STATIONS
        }
        
        # Tracking
        self.last_metar_poll: Optional[datetime] = None
        self.last_price_poll: Optional[datetime] = None
        self.snipes: List[dict] = []
        
        # Polling mode display
        self.polling_mode = "NORMAL"
        self.poll_interval_ms = 60000
        
        # Graceful shutdown
        self.running = True
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)
        
        # Pre-compute date suffix
        self._today_suffix = None
        self._today_suffix_date = None
        
        # Initialize ASOS Scout (wethr.net integration)
        self.scout = None
        try:
            from wethr_integration import create_scout
            self.scout = create_scout(self)
            logger.info("[INIT] ASOS Scout enabled")
        except ImportError:
            logger.info("[INIT] ASOS Scout disabled (wethr_integration.py not found)")
    
    def _handle_shutdown(self, signum, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        log_event('shutdown', signal=signum)
        self.running = False
    
    def _get_today_suffix(self) -> str:
        """Get cached date suffix (avoids repeated datetime formatting)."""
        today = datetime.now(timezone.utc).date()
        if self._today_suffix_date != today:
            self._today_suffix = datetime.now(timezone.utc).strftime('%y%b%d').upper()
            self._today_suffix_date = today
        return self._today_suffix
    
    def get_local_date(self, station: str) -> str:
        tz_name = STATIONS.get(station, {}).get('timezone', 'America/New_York')
        return datetime.now(ZoneInfo(tz_name)).strftime('%Y-%m-%d')
    
    def _warm_connections(self):
        """Pre-establish TCP/TLS connections to reduce first-request latency."""
        log_event('connection_warm_start')
        
        # Warm Kalshi connection
        try:
            self.kalshi._make_request("GET", "/exchange/status")
            logger.info(f"  ✓ Kalshi connection warm ({self.kalshi.last_latency_ms:.0f}ms)")
        except Exception as e:
            logger.warning(f"  ✗ Kalshi warm failed: {e}")
        
        # Warm NWS connections (first 3 stations)
        for station in list(self.states.keys())[:3]:
            try:
                self._fetch_metar_nws_txt(station)
            except:
                pass
        logger.info("  ✓ NWS connections warm")
        
        log_event('connection_warm_complete')
    
    # ============================================================
    # INITIALIZATION
    # ============================================================
    
    def init_watchlists(self):
        """Initialize watchlists with all brackets from Kalshi."""
        logger.info("[INIT] Building watchlists...")
        today_suffix = self._get_today_suffix()
        
        for station, state in self.states.items():
            cfg = STATIONS.get(station, {})
            high_ticker = cfg.get('kalshi_high_ticker')
            low_ticker = cfg.get('kalshi_low_ticker')
            
            state.high_watchlist = []
            state.low_watchlist = []
            state.resolved_brackets = []
            
            if high_ticker:
                try:
                    event = f"{high_ticker}-{today_suffix}"
                    event_data = self.kalshi.get_event(event)
                    event_obj = event_data.get('event', event_data)
                    markets = event_obj.get('markets', [])
                    
                    for m in markets:
                        floor_val = m.get('floor_strike')
                        cap_val = m.get('cap_strike')
                        b = BracketState(
                            ticker=m.get('ticker', ''),
                            subtitle=m.get('yes_sub_title', m.get('subtitle', '')),
                            floor_strike=int(floor_val) if floor_val is not None else None,
                            cap_strike=int(cap_val) if cap_val is not None else None,
                            strike_type=m.get('strike_type', 'between'),
                            signal_type='high',
                            station=station
                        )
                        b.no_ask = self._parse_price(m.get('no_ask'), m.get('no_ask_dollars'))
                        b.yes_ask = self._parse_price(m.get('yes_ask'), m.get('yes_ask_dollars'))
                        state.high_watchlist.append(b)
                    
                    logger.info(f"  {station} HIGH: {len(state.high_watchlist)} brackets")
                except Exception as e:
                    logger.error(f"  {station} HIGH: ERROR - {e}")
            
            if low_ticker:
                try:
                    event = f"{low_ticker}-{today_suffix}"
                    event_data = self.kalshi.get_event(event)
                    event_obj = event_data.get('event', event_data)
                    markets = event_obj.get('markets', [])
                    
                    for m in markets:
                        floor_val = m.get('floor_strike')
                        cap_val = m.get('cap_strike')
                        b = BracketState(
                            ticker=m.get('ticker', ''),
                            subtitle=m.get('yes_sub_title', m.get('subtitle', '')),
                            floor_strike=int(floor_val) if floor_val is not None else None,
                            cap_strike=int(cap_val) if cap_val is not None else None,
                            strike_type=m.get('strike_type', 'between'),
                            signal_type='low',
                            station=station
                        )
                        b.no_ask = self._parse_price(m.get('no_ask'), m.get('no_ask_dollars'))
                        b.yes_ask = self._parse_price(m.get('yes_ask'), m.get('yes_ask_dollars'))
                        state.low_watchlist.append(b)
                    
                    logger.info(f"  {station} LOW: {len(state.low_watchlist)} brackets")
                except Exception as e:
                    logger.error(f"  {station} LOW: ERROR - {e}")
        
        self.last_price_poll = datetime.now(timezone.utc)
    
    def _parse_price(self, price_raw, price_dollars) -> int:
        """Parse price from Kalshi API response."""
        if price_dollars:
            try:
                val = int(float(price_dollars) * 100)
                if val > 0:
                    return val
            except:
                pass
        if price_raw is not None:
            try:
                if isinstance(price_raw, str):
                    val = int(float(price_raw) * 100)
                elif price_raw < 2:
                    val = int(price_raw * 100)
                else:
                    val = int(price_raw)
                if val > 0:
                    return val
            except:
                pass
        return 0
    
    def fetch_historical_temps(self):
        """Fetch historical METARs to get accurate daily high/low for TODAY only."""
        logger.info("[INIT] Fetching historical temps...")
        
        try:
            ids_param = ','.join(self.states.keys())
            url = f"https://aviationweather.gov/api/data/metar?ids={ids_param}&format=json&hours=24"
            
            resp = self.nws_session.get(url, timeout=30)
            if resp.status_code != 200:
                logger.error(f"  [HIST] HTTP {resp.status_code}")
                return
            
            metars = resp.json()
            if not isinstance(metars, list):
                metars = [metars] if metars else []
            
            # Initialize each station with its current local date
            for station, state in self.states.items():
                cfg = STATIONS.get(station, {})
                tz_name = cfg.get('timezone', 'America/New_York')
                tz = ZoneInfo(tz_name)
                state.current_local_date = datetime.now(tz).strftime('%Y-%m-%d')
                state.observed_high = None
                state.observed_low = None
            
            for metar_data in metars:
                station = metar_data.get('icaoId') or metar_data.get('stationId')
                if not station or station not in self.states:
                    continue
                
                state = self.states[station]
                cfg = STATIONS.get(station, {})
                tz_name = cfg.get('timezone', 'America/New_York')
                tz = ZoneInfo(tz_name)
                
                raw = metar_data.get('rawOb', '')
                
                # Parse METAR time to check if it's from today (local time)
                time_match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', raw)
                if time_match:
                    day = int(time_match.group(1))
                    hour = int(time_match.group(2))
                    minute = int(time_match.group(3))
                    now_utc = datetime.now(timezone.utc)
                    try:
                        metar_utc = now_utc.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
                        # Handle month rollover
                        if metar_utc > now_utc:
                            metar_utc = metar_utc.replace(month=metar_utc.month - 1 if metar_utc.month > 1 else 12)
                    except ValueError:
                        metar_utc = now_utc
                    
                    metar_local = metar_utc.astimezone(tz)
                    metar_local_date = metar_local.strftime('%Y-%m-%d')
                    
                    # Skip METARs from previous days
                    if metar_local_date != state.current_local_date:
                        continue
                    
                    metar_local_hour = metar_local.hour
                else:
                    continue  # Skip if we can't parse the time
                
                parsed = parse_metar(raw)
                
                # T-group temperature (current reading) - always valid for today
                if parsed.temp_f is not None:
                    if state.observed_high is None or parsed.temp_f > state.observed_high:
                        state.observed_high = parsed.temp_f
                    if state.observed_low is None or parsed.temp_f < state.observed_low:
                        state.observed_low = parsed.temp_f
                
                # 6-hour max - only use if it doesn't span midnight
                if parsed.six_hr_max_c is not None:
                    if metar_local_hour >= 6:  # Safe - 6-hr period is within today
                        max_f = c_to_f_nws(parsed.six_hr_max_c)
                        if state.observed_high is None or max_f > state.observed_high:
                            state.observed_high = max_f
                
                # 6-hour min - only use if it doesn't span midnight
                if parsed.six_hr_min_c is not None:
                    if metar_local_hour >= 6:  # Safe - 6-hr period is within today
                        min_f = c_to_f_nws(parsed.six_hr_min_c)
                        if state.observed_low is None or min_f < state.observed_low:
                            state.observed_low = min_f
            
            for station, state in self.states.items():
                logger.info(f"  [{station}] HIGH={state.observed_high}°F LOW={state.observed_low}°F")
            
        except Exception as e:
            logger.error(f"  [HIST] Batch fetch error: {e}")
    
    def prune_watchlists(self):
        """Remove already-resolved brackets from watchlists."""
        logger.info("[INIT] Pruning watchlists...")
        
        for station, state in self.states.items():
            still_open = []
            for b in state.high_watchlist:
                status = b.check_status(state.observed_high, state.observed_low)
                if status == 'open':
                    still_open.append(b)
                else:
                    b.status = status
                    state.resolved_brackets.append(b)
            state.high_watchlist = still_open
            
            still_open = []
            for b in state.low_watchlist:
                status = b.check_status(state.observed_high, state.observed_low)
                if status == 'open':
                    still_open.append(b)
                else:
                    b.status = status
                    state.resolved_brackets.append(b)
            state.low_watchlist = still_open
            
            high_count = len(state.high_watchlist)
            low_count = len(state.low_watchlist)
            resolved = len(state.resolved_brackets)
            logger.info(f"  [{station}] Watching: {high_count} HIGH, {low_count} LOW | Resolved: {resolved}")
    
    # ============================================================
    # POLLING & SNIPE DETECTION
    # ============================================================
    
    def _fetch_metar_nws_txt(self, station: str) -> Optional[str]:
        """Fetch METAR from NWS TXT (often faster, no caching)."""
        try:
            url = f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{station}.TXT"
            resp = self.nws_session.get(url, timeout=3)
            if resp.status_code == 200:
                lines = resp.text.strip().split('\n')
                if len(lines) >= 2:
                    return lines[1].strip()
        except:
            pass
        return None
    
    def _fetch_metars_aviation_api(self, stations: List[str]) -> Dict[str, str]:
        """Fetch METARs from Aviation Weather API (batch)."""
        result = {}
        try:
            ids_param = ','.join(stations)
            url = f"https://aviationweather.gov/api/data/metar?ids={ids_param}&format=json&_t={int(time.time())}"
            resp = self.nws_session.get(url, timeout=10)
            if resp.status_code == 200:
                metars = resp.json()
                if not isinstance(metars, list):
                    metars = [metars]
                for m in metars:
                    station = m.get('icaoId') or m.get('stationId')
                    raw = m.get('rawOb', '')
                    if station and raw:
                        result[station] = raw
        except:
            pass
        return result
    
    def poll_and_snipe(self):
        """Fetch METARs, detect transitions, execute snipes."""
        active_stations = [
            station for station, state in self.states.items()
            if state.high_watchlist or state.low_watchlist
        ]
        
        if not active_stations:
            return
        
        now_minute = datetime.now(timezone.utc).minute
        is_hot = 50 <= now_minute <= 59 or now_minute <= 5
        
        # Only log outside hot window to reduce I/O
        if not is_hot:
            logger.info(f"[METAR] Polling {len(active_stations)} stations...")
        
        poll_start = time.perf_counter()
        
        try:
            metars_found = {}
            
            # Parallel NWS TXT fetch
            with ThreadPoolExecutor(max_workers=12) as executor:
                futures = {executor.submit(self._fetch_metar_nws_txt, station): station 
                          for station in active_stations}
                for future in as_completed(futures, timeout=6):
                    station = futures[future]
                    try:
                        raw = future.result()
                        if raw:
                            metars_found[station] = raw
                    except:
                        pass
            
            # Fallback to Aviation API
            missing = [s for s in active_stations if s not in metars_found]
            if missing:
                api_metars = self._fetch_metars_aviation_api(missing)
                metars_found.update(api_metars)
            
            poll_latency = (time.perf_counter() - poll_start) * 1000
            
            if not is_hot:
                logger.info(f"  [METAR] Got {len(metars_found)} METARs in {poll_latency:.0f}ms")
            
            # Process METARs
            for station, raw in metars_found.items():
                if station not in self.states:
                    continue
                
                state = self.states[station]
                
                if raw != state.latest_metar:
                    now_utc = datetime.now(timezone.utc)
                    log_event('metar_new', station=station, minute=now_utc.minute, 
                             second=now_utc.second, raw=raw[:80])
                    
                    try:
                        with open('/tmp/metar_drops.log', 'a') as f:
                            f.write(f"{now_utc.isoformat()},{station},{now_utc.minute},{now_utc.second},{raw[:80]}\n")
                    except:
                        pass
                
                parsed = parse_metar(raw)
                state.latest_metar = raw
                
                time_match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', raw)
                if time_match:
                    day = int(time_match.group(1))
                    hour = int(time_match.group(2))
                    minute = int(time_match.group(3))
                    now_utc = datetime.now(timezone.utc)
                    try:
                        state.metar_time = now_utc.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
                    except ValueError:
                        state.metar_time = now_utc
                else:
                    state.metar_time = datetime.now(timezone.utc)
                
                # Get station's local timezone for 6-hour group checks
                cfg = STATIONS.get(station, {})
                tz_name = cfg.get('timezone', 'America/New_York')
                tz = ZoneInfo(tz_name)
                metar_local = state.metar_time.astimezone(tz)
                metar_local_hour = metar_local.hour
                
                # NOTE: We do NOT reset observed_high/low here - that happens in check_date_rollover()
                # This prevents mid-processing resets that can cause false triggers
                
                old_high = state.observed_high
                old_low = state.observed_low
                
                if parsed.temp_f is not None:
                    state.latest_temp_f = parsed.temp_f
                    if state.observed_high is None or parsed.temp_f > state.observed_high:
                        state.observed_high = parsed.temp_f
                    if state.observed_low is None or parsed.temp_f < state.observed_low:
                        state.observed_low = parsed.temp_f
                
                # Process 6-hour max/min ONLY if it doesn't span midnight
                # If METAR is before 6 AM local, the 6-hour period spans midnight - IGNORE
                six_hr_spans_midnight = metar_local_hour < 6
                
                if parsed.six_hr_max_c is not None:
                    if six_hr_spans_midnight:
                        log_event('synoptic_max_ignored', station=station, 
                                 reason=f"6-hr period spans midnight (METAR at {metar_local_hour}:00 local)")
                    else:
                        max_f = c_to_f_nws(parsed.six_hr_max_c)
                        if state.observed_high is None or max_f > state.observed_high:
                            state.observed_high = max_f
                            log_event('synoptic_max', station=station, temp_f=max_f)
                
                if parsed.six_hr_min_c is not None:
                    if six_hr_spans_midnight:
                        log_event('synoptic_min_ignored', station=station,
                                 reason=f"6-hr period spans midnight (METAR at {metar_local_hour}:00 local)")
                    else:
                        min_f = c_to_f_nws(parsed.six_hr_min_c)
                        if state.observed_low is None or min_f < state.observed_low:
                            state.observed_low = min_f
                            log_event('synoptic_min', station=station, temp_f=min_f)
                
                if state.observed_high != old_high or state.observed_low != old_low:
                    log_event('temp_update', station=station, 
                             high=state.observed_high, low=state.observed_low)
                    self._check_transitions(state)
        
        except Exception as e:
            logger.error(f"[METAR] Poll error: {e}")
        
        self.last_metar_poll = datetime.now(timezone.utc)
    
    def _check_transitions(self, state: StationState):
        """Check watchlists for OPEN → DEAD or OPEN → LOCKED transitions."""
        
        # Check HIGH watchlist
        still_watching = []
        for b in state.high_watchlist:
            new_status = b.check_status(state.observed_high, state.observed_low)
            
            if new_status == 'dead' and b.status == 'open':
                reason = b.describe_resolution(state.observed_high, state.observed_low)
                self._snipe(b, 'BUY_NO', b.no_ask, reason)
                b.status = 'dead'
                state.resolved_brackets.append(b)
            
            elif new_status == 'locked' and b.status == 'open':
                reason = b.describe_resolution(state.observed_high, state.observed_low)
                self._snipe(b, 'BUY_YES', b.yes_ask, reason)
                b.status = 'locked'
                state.resolved_brackets.append(b)
            
            else:
                still_watching.append(b)
        
        state.high_watchlist = still_watching
        
        # Check LOW watchlist
        still_watching = []
        for b in state.low_watchlist:
            new_status = b.check_status(state.observed_high, state.observed_low)
            
            if new_status == 'dead' and b.status == 'open':
                reason = b.describe_resolution(state.observed_high, state.observed_low)
                self._snipe(b, 'BUY_NO', b.no_ask, reason)
                b.status = 'dead'
                state.resolved_brackets.append(b)
            
            elif new_status == 'locked' and b.status == 'open':
                reason = b.describe_resolution(state.observed_high, state.observed_low)
                self._snipe(b, 'BUY_YES', b.yes_ask, reason)
                b.status = 'locked'
                state.resolved_brackets.append(b)
            
            else:
                still_watching.append(b)
        
        state.low_watchlist = still_watching
        
        # EJECTION SEAT: Check if METAR contradicts any Scout positions
        if self.scout:
            all_brackets = state.high_watchlist + state.low_watchlist + state.resolved_brackets
            self.scout.check_ejection_seat(
                station=state.station,
                observed_high=state.observed_high,
                observed_low=state.observed_low,
                brackets=all_brackets
            )
    
    def _snipe(self, bracket: BracketState, action: str, price: int, reason: str):
        """Execute a snipe trade with QC hedge."""
        side = 'no' if action == 'BUY_NO' else 'yes'
        execution_price = 99  # Always bid max for guaranteed fill
        
        snipe_record = {
            'time': datetime.now(timezone.utc).isoformat(),
            'station': bracket.station,
            'ticker': bracket.ticker,
            'subtitle': bracket.subtitle,
            'action': action,
            'side': side,
            'price': execution_price,
            'quantity': self.order_size,
            'original_ask': price,
            'reason': reason,
            'live': self.live_mode,
            'success': False,
        }
        
        log_event('snipe_attempt', **{k: v for k, v in snipe_record.items() if k != 'time'})
        
        if self.live_mode:
            try:
                # BUY
                result = self.kalshi.create_order(
                    ticker=bracket.ticker,
                    side=side,
                    action='buy',
                    count=self.order_size,
                    order_type='limit',
                    price_cents=execution_price
                )
                snipe_record['success'] = True
                snipe_record['order_id'] = result.get('order', {}).get('order_id')
                snipe_record['buy_latency_ms'] = self.kalshi.last_latency_ms
                
                log_event('snipe_buy_success', order_id=snipe_record['order_id'],
                         latency_ms=snipe_record['buy_latency_ms'])
                
                # HEDGE SELL
                try:
                    hedge_result = self.kalshi.create_order(
                        ticker=bracket.ticker,
                        side=side,
                        action='sell',
                        count=self.order_size,
                        order_type='limit',
                        price_cents=99
                    )
                    snipe_record['hedge_order_id'] = hedge_result.get('order', {}).get('order_id')
                    log_event('snipe_hedge_success', order_id=snipe_record['hedge_order_id'])
                except Exception as e:
                    snipe_record['hedge_error'] = str(e)
                    log_event('snipe_hedge_failed', error=str(e))
                    
            except Exception as e:
                snipe_record['error'] = str(e)
                log_event('snipe_buy_failed', error=str(e))
        else:
            snipe_record['success'] = True
            log_event('snipe_dry_run')
        
        bracket.traded = True
        self.snipes.append(snipe_record)
    
    # ============================================================
    # PRICE REFRESH
    # ============================================================
    
    def refresh_prices(self):
        """Refresh prices for watched brackets."""
        logger.info("[PRICES] Refreshing...")
        today_suffix = self._get_today_suffix()
        
        for station, state in self.states.items():
            cfg = STATIONS.get(station, {})
            
            if state.high_watchlist:
                high_ticker = cfg.get('kalshi_high_ticker')
                if high_ticker:
                    try:
                        event = f"{high_ticker}-{today_suffix}"
                        event_data = self.kalshi.get_event(event)
                        event_obj = event_data.get('event', event_data)
                        markets = {m.get('ticker'): m for m in event_obj.get('markets', [])}
                        
                        for b in state.high_watchlist:
                            if b.ticker in markets:
                                m = markets[b.ticker]
                                b.no_ask = self._parse_price(m.get('no_ask'), m.get('no_ask_dollars'))
                                b.yes_ask = self._parse_price(m.get('yes_ask'), m.get('yes_ask_dollars'))
                    except Exception as e:
                        logger.error(f"  {station} HIGH prices: {e}")
            
            if state.low_watchlist:
                low_ticker = cfg.get('kalshi_low_ticker')
                if low_ticker:
                    try:
                        event = f"{low_ticker}-{today_suffix}"
                        event_data = self.kalshi.get_event(event)
                        event_obj = event_data.get('event', event_data)
                        markets = {m.get('ticker'): m for m in event_obj.get('markets', [])}
                        
                        for b in state.low_watchlist:
                            if b.ticker in markets:
                                m = markets[b.ticker]
                                b.no_ask = self._parse_price(m.get('no_ask'), m.get('no_ask_dollars'))
                                b.yes_ask = self._parse_price(m.get('yes_ask'), m.get('yes_ask_dollars'))
                    except Exception as e:
                        logger.error(f"  {station} LOW prices: {e}")
        
        self.last_price_poll = datetime.now(timezone.utc)
    
    # ============================================================
    # MAIN LOOP
    # ============================================================
    
    def run(self):
        """Main entry point."""
        logger.info("=" * 60)
        logger.info("WX SNIPER v4.6 - AWS OPTIMIZED")
        logger.info("=" * 60)
        logger.info(f"Mode: {'LIVE 🔴' if self.live_mode else 'DRY RUN 🧪'}")
        logger.info(f"Max price: {self.max_price}¢")
        logger.info(f"Order size: {self.order_size} contracts")
        logger.info("=" * 60)
        
        # Start dashboard
        HealthHandler.sniper = self
        dashboard_thread = threading.Thread(target=start_health_server, daemon=True)
        dashboard_thread.start()
        
        # Warm connections for lowest latency
        self._warm_connections()
        
        # Initialize
        self.init_watchlists()
        self.fetch_historical_temps()
        self.prune_watchlists()
        
        # Initial poll
        logger.info("\n[INIT] Initial METAR poll...")
        self.poll_and_snipe()
        
        logger.info("\n[RUNNING] Starting main loop...")
        last_price_refresh = time.time()
        
        while self.running:
            now = datetime.now(timezone.utc)
            minute = now.minute
            
            is_metar_drop = 50 <= minute <= 59
            is_hot = minute <= 5
            
            if is_metar_drop:
                self.polling_mode = "💥 METAR DROP"
                self.poll_interval_ms = 200
            elif is_hot:
                self.polling_mode = "🔥 HOT"
                self.poll_interval_ms = 2000
            else:
                self.polling_mode = "NORMAL"
                self.poll_interval_ms = 60000
            
            self.poll_and_snipe()
            
            # Poll ASOS Scout (wethr.net) - runs every 30s, dormant during METAR window
            if self.scout:
                self.scout.poll()
            
            if time.time() - last_price_refresh > 300:
                self.refresh_prices()
                last_price_refresh = time.time()
            
            if is_metar_drop:
                time.sleep(0.2)
            elif is_hot:
                time.sleep(2)
            else:
                time.sleep(60)
        
        logger.info("[SHUTDOWN] Graceful shutdown complete")

# ============================================================
# DASHBOARD (unchanged from v4.5)
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    sniper: Optional[WXSniper] = None
    
    def log_message(self, format, *args):
        pass
    
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode())
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(self.build_dashboard().encode('utf-8'))
    
    def build_dashboard(self) -> str:
        s = HealthHandler.sniper
        if not s:
            return "<html><body>Not initialized</body></html>"
        
        now = datetime.now(timezone.utc)
        now_et = datetime.now(ZoneInfo('America/New_York'))
        minute = now.minute
        is_hot = minute >= 50 or minute <= 5
        
        total_watching = sum(len(st.high_watchlist) + len(st.low_watchlist) for st in s.states.values())
        total_resolved = sum(len(st.resolved_brackets) for st in s.states.values())
        
        today_et = now_et.date()
        todays_snipes = []
        for snipe in s.snipes:
            try:
                snipe_time = datetime.fromisoformat(snipe['time'].replace('Z', '+00:00'))
                snipe_et = snipe_time.astimezone(ZoneInfo('America/New_York'))
                if snipe_et.date() == today_et:
                    todays_snipes.append(snipe)
            except:
                todays_snipes.append(snipe)
        
        # Get Scout positions for display
        scout_positions = []
        scout_status = "DISABLED"
        if s.scout:
            scout_positions = s.scout.positions
            scout_status = "DORMANT" if s.scout.dormant else "ACTIVE"
        
        html = f'''<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>WX Sniper v5.0</title>
<meta http-equiv="refresh" content="{'10' if is_hot else '30'}">
<style>
body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, sans-serif; padding: 20px; }}
h1 {{ color: #58a6ff; }}
h2 {{ color: #8b949e; border-bottom: 1px solid #30363d; padding-bottom: 8px; margin-top: 30px; }}
h3 {{ color: #58a6ff; margin-top: 20px; }}
h4 {{ color: #8b949e; margin: 15px 0 5px 0; font-size: 14px; }}
table {{ border-collapse: collapse; width: 100%; max-width: 900px; margin: 10px 0; }}
th, td {{ padding: 6px 10px; text-align: left; border: 1px solid #30363d; }}
th {{ background: #161b22; }}
.dead {{ color: #f85149; }}
.locked {{ color: #3fb950; }}
.open {{ color: #58a6ff; font-weight: bold; }}
.hot {{ background: #3d1c1c; padding: 5px 10px; border-radius: 4px; }}
.snipe {{ background: #1c3d1c; }}
.scout {{ background: #1c1c3d; }}
.ejected {{ background: #3d1c1c; opacity: 0.7; }}
.time {{ color: #8b949e; font-size: 12px; }}
.stats {{ display: flex; gap: 20px; margin: 10px 0; flex-wrap: wrap; }}
.stat {{ background: #161b22; padding: 10px 15px; border-radius: 6px; }}
.stat-value {{ font-size: 24px; font-weight: bold; color: #58a6ff; }}
.stat-label {{ font-size: 12px; color: #8b949e; }}
.scout-active {{ color: #3fb950; }}
.scout-dormant {{ color: #f0883e; }}
.scout-disabled {{ color: #8b949e; }}
.resolved-row {{ opacity: 0.6; }}
details {{ margin: 10px 0; }}
summary {{ cursor: pointer; color: #8b949e; }}
.metar-info {{ background: #161b22; padding: 8px 12px; border-radius: 4px; margin: 8px 0; font-size: 13px; }}
.metar-info strong {{ color: #58a6ff; }}
.current-temp {{ font-size: 18px; color: #f0f6fc; }}
.latency {{ color: #8b949e; font-size: 11px; }}
</style>
</head><body>
<h1>&#127919; WX Sniper v5.0 (AWS + Scout)</h1>
<p>
    Mode: <strong>{"LIVE &#128308;" if s.live_mode else "DRY RUN &#129514;"}</strong> |
    Max price: <strong>{s.max_price}&#162;</strong> |
    Polling: <strong>{s.polling_mode}</strong> ({s.poll_interval_ms}ms) |
    Scout: <strong class="scout-{scout_status.lower()}">{scout_status}</strong> |
    <span class="latency">Last Kalshi: {s.kalshi.last_latency_ms:.0f}ms</span>
</p>

<div class="stats">
    <div class="stat"><div class="stat-value">{total_watching}</div><div class="stat-label">Watching</div></div>
    <div class="stat"><div class="stat-value">{total_resolved}</div><div class="stat-label">Resolved</div></div>
    <div class="stat"><div class="stat-value">{len(todays_snipes)}</div><div class="stat-label">METAR Trades</div></div>
    <div class="stat"><div class="stat-value">{len(scout_positions)}</div><div class="stat-label">Scout Trades</div></div>
</div>

<p class="time">
    ET: {now_et.strftime("%b %d, %Y %I:%M:%S %p")} |
    Last METAR: {s.last_metar_poll.astimezone(ZoneInfo('America/New_York')).strftime("%I:%M:%S %p") if s.last_metar_poll else "Never"} ET |
    Last Kalshi: {s.last_price_poll.astimezone(ZoneInfo('America/New_York')).strftime("%I:%M:%S %p") if s.last_price_poll else "Never"} ET
</p>
'''
        
        # Today's Trades
        if todays_snipes:
            html += f"<h2>&#9889; METAR Trades ({len(todays_snipes)})</h2>"
            html += '<table><tr><th>Time (ET)</th><th>Station</th><th>Bracket</th><th>Action</th><th>Qty</th><th>Price</th><th>Latency</th><th>Status</th></tr>'
            for snipe in reversed(todays_snipes):
                try:
                    snipe_time = datetime.fromisoformat(snipe['time'].replace('Z', '+00:00'))
                    snipe_et = snipe_time.astimezone(ZoneInfo('America/New_York'))
                    time_str = snipe_et.strftime("%I:%M:%S %p")
                except:
                    time_str = snipe['time'][11:19]
                
                if not snipe.get('live'):
                    status = "&#129514; DRY"
                elif snipe.get('success'):
                    status = "&#9989; FILLED"
                else:
                    status = "&#10060; FAILED"
                
                action_class = "locked" if snipe['action'] == 'BUY_YES' else "dead"
                latency = snipe.get('buy_latency_ms', '?')
                qty = snipe.get('quantity', s.order_size)
                html += f'''<tr class="snipe">
                    <td class="time">{time_str}</td>
                    <td>{snipe['station']}</td>
                    <td>{snipe['subtitle']}</td>
                    <td class="{action_class}">{snipe['action']}</td>
                    <td>{qty}</td>
                    <td>{snipe['price']}&#162;</td>
                    <td class="latency">{latency}ms</td>
                    <td>{status}</td>
                </tr>'''
            html += '</table>'
        else:
            html += "<h2>&#9889; METAR Trades (0)</h2><p style='color:#8b949e'>No METAR snipes yet today</p>"
        
        # Scout Trades Section
        if scout_positions:
            html += f"<h2>&#128373; Scout Trades ({len(scout_positions)})</h2>"
            html += '<table><tr><th>Time (ET)</th><th>Station</th><th>Ticker</th><th>Action</th><th>Qty</th><th>Price</th><th>Reason</th><th>Status</th></tr>'
            for pos in reversed(scout_positions):
                try:
                    pos_et = pos.entry_time.astimezone(ZoneInfo('America/New_York'))
                    time_str = pos_et.strftime("%I:%M:%S %p")
                except:
                    time_str = "?"
                
                if pos.ejected:
                    status = f"&#128680; EJECTED"
                    row_class = "ejected"
                else:
                    status = "&#9989; OPEN"
                    row_class = "scout"
                
                action_class = "locked" if pos.action == 'BUY_YES' else "dead"
                reason_short = pos.reason[:40] + "..." if len(pos.reason) > 40 else pos.reason
                
                html += f'''<tr class="{row_class}">
                    <td class="time">{time_str}</td>
                    <td>{pos.station}</td>
                    <td>{pos.ticker[-15:]}</td>
                    <td class="{action_class}">{pos.action}</td>
                    <td>{pos.quantity}</td>
                    <td>{pos.entry_price}&#162;</td>
                    <td title="{pos.reason}">{reason_short}</td>
                    <td>{status}</td>
                </tr>'''
            html += '</table>'
        
        # Watchlists
        html += "<h2>Watchlists</h2>"
        
        for station, state in s.states.items():
            cfg = STATIONS.get(station, {})
            city = cfg.get('name', station)
            tz_name = cfg.get('timezone', 'America/New_York')
            tz = ZoneInfo(tz_name)
            
            watching_count = len(state.high_watchlist) + len(state.low_watchlist)
            if watching_count == 0 and len(state.resolved_brackets) == 0:
                continue
            
            metar_local_str = "?"
            metar_is_stale = False
            if state.metar_time:
                metar_local = state.metar_time.astimezone(tz)
                metar_local_str = metar_local.strftime("%I:%M %p")
                
                # Check if METAR is from yesterday (stale)
                now_local = datetime.now(tz)
                if metar_local.date() < now_local.date():
                    metar_is_stale = True
                    metar_local_str += " <span style='color:#f0883e'>(yesterday)</span>"
            
            current_temp_str = f"{state.latest_temp_f}°F" if state.latest_temp_f is not None else "?"
            
            # Show the actual values but label appropriately
            high_val = f"{state.observed_high}&#176;F" if state.observed_high is not None else "—"
            low_val = f"{state.observed_low}&#176;F" if state.observed_low is not None else "—"
            
            if metar_is_stale:
                range_label = "<span style='color:#f0883e'>Yesterday's Range:</span>"
            else:
                range_label = "<strong>Day's Range:</strong>"
            
            html += f'''<h3>{city} ({station}) - {watching_count} watching</h3>
            <div class="metar-info">
                <strong>Latest METAR:</strong> {metar_local_str} local &nbsp;&nbsp; <span class="current-temp">{current_temp_str}</span> &nbsp;&nbsp;|&nbsp;&nbsp;
                {range_label} <strong>HIGH</strong> {high_val} &nbsp; <strong>LOW</strong> {low_val}
            </div>'''
            
            if state.high_watchlist:
                html += '<h4>HIGH Watchlist</h4>'
                html += '<table><tr><th>Bracket</th><th>Floor</th><th>Cap</th><th>NO Ask</th><th>YES Ask</th><th>Status</th></tr>'
                for b in sorted(state.high_watchlist, key=lambda x: x.floor_strike if x.floor_strike is not None else -999, reverse=True):
                    floor_display = b.floor_strike if b.floor_strike is not None else "—"
                    cap_display = b.cap_strike if b.cap_strike is not None else "—"
                    html += f'''<tr>
                        <td>{b.subtitle}</td>
                        <td>{floor_display}</td>
                        <td>{cap_display}</td>
                        <td>{b.no_ask}&#162;</td>
                        <td>{b.yes_ask}&#162;</td>
                        <td class="open">OPEN</td>
                    </tr>'''
                html += '</table>'
            
            if state.low_watchlist:
                html += '<h4>LOW Watchlist</h4>'
                html += '<table><tr><th>Bracket</th><th>Floor</th><th>Cap</th><th>NO Ask</th><th>YES Ask</th><th>Status</th></tr>'
                for b in sorted(state.low_watchlist, key=lambda x: x.cap_strike if x.cap_strike is not None else 999):
                    floor_display = b.floor_strike if b.floor_strike is not None else "—"
                    cap_display = b.cap_strike if b.cap_strike is not None else "—"
                    html += f'''<tr>
                        <td>{b.subtitle}</td>
                        <td>{floor_display}</td>
                        <td>{cap_display}</td>
                        <td>{b.no_ask}&#162;</td>
                        <td>{b.yes_ask}&#162;</td>
                        <td class="open">OPEN</td>
                    </tr>'''
                html += '</table>'
            
            high_resolved = [b for b in state.resolved_brackets if b.signal_type == 'high']
            low_resolved = [b for b in state.resolved_brackets if b.signal_type == 'low']
            
            if high_resolved or low_resolved:
                total_resolved_count = len(high_resolved) + len(low_resolved)
                html += f'<details><summary>Resolved ({total_resolved_count})</summary>'
                
                if high_resolved:
                    html += '<h4>HIGH Resolved</h4>'
                    html += '<table><tr><th>Bracket</th><th>Status</th><th>Traded?</th></tr>'
                    for b in high_resolved[-15:]:
                        status_class = "locked" if b.status == 'locked' else "dead"
                        traded = "&#9989;" if b.traded else "—"
                        html += f'<tr class="resolved-row"><td>{b.subtitle}</td><td class="{status_class}">{b.status.upper()}</td><td>{traded}</td></tr>'
                    html += '</table>'
                
                if low_resolved:
                    html += '<h4>LOW Resolved</h4>'
                    html += '<table><tr><th>Bracket</th><th>Status</th><th>Traded?</th></tr>'
                    for b in low_resolved[-15:]:
                        status_class = "locked" if b.status == 'locked' else "dead"
                        traded = "&#9989;" if b.traded else "—"
                        html += f'<tr class="resolved-row"><td>{b.subtitle}</td><td class="{status_class}">{b.status.upper()}</td><td>{traded}</td></tr>'
                    html += '</table>'
                
                html += '</details>'
        
        html += "</body></html>"
        return html


def start_health_server():
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    logger.info(f"[HTTP] Dashboard on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    sniper = WXSniper()
    sniper.run()
