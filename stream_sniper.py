#!/usr/bin/env python3
"""
stream_sniper.py - Real-time streaming ASOS phone sniper
=========================================================

Keeps a single long-running call to ASOS, streaming audio continuously.
The ASOS recording loops every ~60s, so we get a fresh temperature
reading every cycle without hanging up and redialing.

ARCHITECTURE:
  1. Twilio calls ASOS phone number (one call, stays connected)
  2. Twilio streams raw mulaw audio via WebSocket to THIS server
  3. This server pipes audio to Deepgram streaming WebSocket
  4. Deepgram sends back real-time transcript fragments continuously
  5. Every time we see "temperature ... digits ... celsius" → update state → check brackets
  6. Call stays open until schedule ends or Ctrl+C

COST: ~$0.65/hour ($0.007/min Twilio + $0.004/min Deepgram)
  vs ~$2.40/hour with call-per-reading approach

REQUIRES:
  - nginx + SSL on this server (wss:// proxy to localhost:8765)
  - .env with: TWILIO_*, DEEPGRAM_API_KEY, KALSHI_*

Usage:
  python3 stream_sniper.py --station KLAS --ws-url wss://54-91-7-11.nip.io/stream
  python3 stream_sniper.py --station KSFO --ws-url wss://54-91-7-11.nip.io/stream --start 10:00 --end 16:00
  python3 stream_sniper.py --station KLAS --ws-url wss://54-91-7-11.nip.io/stream --run-minutes 120 --live
"""

import os
import sys
import math
import json
import re
import time
import logging
import asyncio
import base64
import argparse
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('stream_sniper')
logging.getLogger('twilio.http_client').setLevel(logging.WARNING)
logging.getLogger('websockets').setLevel(logging.WARNING)

# ─── Credentials ──────────────────────────────────────────────
account_sid  = os.getenv('TWILIO_ACCOUNT_SID')
auth_token   = os.getenv('TWILIO_AUTH_TOKEN')
from_num     = os.getenv('TWILIO_PHONE_NUMBER')
deepgram_key = os.getenv('DEEPGRAM_API_KEY')

missing = []
for var in ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'DEEPGRAM_API_KEY']:
    if not os.getenv(var):
        missing.append(var)
if missing:
    print(f"❌ Missing env vars: {', '.join(missing)}")
    sys.exit(1)

from twilio.rest import Client as TwilioClient
twilio_client = TwilioClient(account_sid, auth_token)

import websockets


# ─── Station registry ─────────────────────────────────────────

STATION_REGISTRY = {
    'KLAS': {
        'name': 'Las Vegas McCarran',
        'phone': '+17025825334',
        'tz_offset': -8,
        'high_ticker': 'KXHIGHTLV',
        'low_ticker': None,
    },
    'KSFO': {
        'name': 'San Francisco International',
        'phone': '+16508278593',
        'tz_offset': -8,
        'high_ticker': 'KXHIGHTSFO',
        'low_ticker': None,
    },
    'KNYC': {
        'name': 'NYC Central Park',
        'phone': '+12122159324',
        'tz_offset': -5,
        'high_ticker': 'KXHIGHNY',
        'low_ticker': 'KXLOWTNYC',
    },
    'KLAX': {
        'name': 'Los Angeles International',
        'phone': '+13102424750',
        'tz_offset': -8,
        'high_ticker': 'KXHIGHLAX',
        'low_ticker': 'KXLOWTLAX',
    },
    'KMDW': {
        'name': 'Chicago Midway',
        'phone': '+17738386336',
        'tz_offset': -6,
        'high_ticker': 'KXHIGHCHI',
        'low_ticker': 'KXLOWTCHI',
    },
    'KMIA': {
        'name': 'Miami International',
        'phone': '+13058699356',
        'tz_offset': -5,
        'high_ticker': 'KXHIGHMIA',
        'low_ticker': 'KXLOWTMIA',
    },
    'KDEN': {
        'name': 'Denver International',
        'phone': '+13033426164',
        'tz_offset': -7,
        'high_ticker': 'KXHIGHDEN',
        'low_ticker': 'KXLOWTDEN',
    },
    'KPHL': {
        'name': 'Philadelphia International',
        'phone': '+12154925857',
        'tz_offset': -5,
        'high_ticker': 'KXHIGHPHIL',
        'low_ticker': 'KXLOWTPHIL',
    },
    'KAUS': {
        'name': 'Austin-Bergstrom',
        'phone': '+15125304777',
        'tz_offset': -6,
        'high_ticker': 'KXHIGHAUS',
        'low_ticker': 'KXLOWTAUS',
    },
    'KSEA': {
        'name': 'Seattle-Tacoma',
        'phone': '+12064331794',
        'tz_offset': -8,
        'high_ticker': 'KXHIGHTSEA',
        'low_ticker': None,
    },
    'KDCA': {
        'name': 'Washington Reagan National',
        'phone': '+17034199555',
        'tz_offset': -5,
        'high_ticker': 'KXHIGHTDC',
        'low_ticker': None,
    },
    'KMSY': {
        'name': 'New Orleans Lakefront',
        'phone': '+15044721412',
        'tz_offset': -6,
        'high_ticker': 'KXHIGHTNOLA',
        'low_ticker': None,
    },
}


# ─── CLI args ─────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description='Stream Sniper — real-time ASOS phone trading')
    p.add_argument('--station', required=True, help='ICAO station code (e.g. KLAS, KSFO)')
    p.add_argument('--ws-url', required=True, help='WebSocket URL for Twilio (e.g. wss://54-91-7-11.nip.io/stream)')
    p.add_argument('--phone', default=None, help='Override ASOS phone number')
    p.add_argument('--high-ticker', default=None, help='Override Kalshi HIGH ticker prefix')
    p.add_argument('--signal', default='high', choices=['high', 'low'], help='Trade HIGH or LOW brackets')
    p.add_argument('--run-minutes', type=int, default=None, help='Run for N minutes then stop')
    p.add_argument('--start', default=None, help='Start time in station local time (HH:MM)')
    p.add_argument('--end', default=None, help='End time in station local time (HH:MM)')
    p.add_argument('--start-utc', default=None, help='Start time in UTC (HH:MM)')
    p.add_argument('--end-utc', default=None, help='End time in UTC (HH:MM)')
    p.add_argument('--bid', type=int, default=99, help='Bid price in cents (default: 99)')
    p.add_argument('--qty', type=int, default=1, help='Trade quantity (default: 1)')
    p.add_argument('--live', action='store_true', help='Enable live trading (default: dry run)')
    p.add_argument('--port', type=int, default=8765, help='WebSocket server port (default: 8765)')
    return p.parse_args()


# ─── Temperature parsing ─────────────────────────────────────

AVIATION_DIGITS = {
    'zero': '0', 'oh': '0', 'o': '0',
    'one': '1', 'wun': '1',
    'two': '2', 'to': '2', 'too': '2',
    'three': '3', 'tree': '3',
    'four': '4', 'fower': '4',
    'five': '5', 'fife': '5',
    'six': '6',
    'seven': '7',
    'eight': '8', 'ait': '8',
    'nine': '9', 'niner': '9', 'liner': '9',
}


def spaced_digits_to_int(text: str):
    negative = False
    tokens = text.strip().split()
    digits = []
    for t in tokens:
        t_lower = t.lower().strip('.,;:')
        if t_lower in ('minus', 'negative'):
            negative = True
            continue
        if t_lower in AVIATION_DIGITS:
            digits.append(AVIATION_DIGITS[t_lower])
            continue
        if t_lower.isdigit():
            digits.append(t_lower)
            continue
        if digits:
            break
    if not digits:
        return None
    result = int(''.join(digits))
    return -result if negative else result


def nws_round(val: float) -> int:
    return math.floor(val + 0.5)


def c_to_f_nws(temp_c: int) -> int:
    return nws_round(temp_c * 9.0 / 5.0 + 32)


def parse_temperature(transcript: str):
    text = transcript.lower()
    match = re.search(
        r'temperature[,\s]+(minus\s+|negative\s+)?([\d\s]+?)(?:\s*celsius|\s*\.|$)',
        text
    )
    if match:
        sign = match.group(1)
        digit_str = match.group(2).strip()
        val = spaced_digits_to_int(("minus " if sign else "") + digit_str)
        if val is not None and -60 <= val <= 60:
            return val, c_to_f_nws(val)
    temp_idx = text.find('temperature')
    if temp_idx >= 0:
        after = text[temp_idx + len('temperature'):temp_idx + len('temperature') + 30]
        after = after.replace(',', ' ').strip()
        val = spaced_digits_to_int(after)
        if val is not None and -60 <= val <= 60:
            return val, c_to_f_nws(val)
    return None, None


def parse_zulu_time(transcript: str):
    text = transcript.lower()
    zulu_idx = text.find('zulu')
    if zulu_idx < 0:
        return None
    before = text[:zulu_idx].replace(',', ' ').strip()
    tokens = before.split()
    digits = []
    for t in reversed(tokens):
        t_clean = t.strip('.,;:')
        if t_clean in AVIATION_DIGITS:
            digits.insert(0, AVIATION_DIGITS[t_clean])
        elif t_clean.isdigit() and len(t_clean) == 1:
            digits.insert(0, t_clean)
        elif len(digits) > 0:
            break
        if len(digits) == 4:
            break
    if len(digits) == 4:
        return ''.join(digits) + 'Z'
    return None


def compute_omo_candidates(temp_c: int) -> list:
    center_f = c_to_f_nws(temp_c)
    candidates = []
    for omo_f in range(center_f - 3, center_f + 4):
        exact_c = (omo_f - 32) * 5.0 / 9.0
        if nws_round(exact_c) == temp_c:
            candidates.append(omo_f)
    return sorted(candidates)


# ─── Kalshi ───────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from smart_poller import KalshiClient, BracketState
    kalshi = KalshiClient()
    logger.info("[KALSHI] Client initialized")
except ImportError as e:
    logger.error(f"[KALSHI] Cannot import from smart_poller: {e}")
    sys.exit(1)


# ─── Bracket loading ─────────────────────────────────────────

def get_today_suffix(tz_offset: int) -> str:
    now_utc = datetime.now(timezone.utc)
    local = now_utc + timedelta(hours=tz_offset)
    return local.strftime('%y%b%d').upper()


def parse_kalshi_price(price_raw, price_dollars) -> int:
    if price_dollars is not None:
        try:
            return int(round(float(price_dollars) * 100))
        except (ValueError, TypeError):
            pass
    if price_raw is not None:
        try:
            return int(price_raw)
        except (ValueError, TypeError):
            pass
    return 100


def load_brackets(ticker_prefix: str, tz_offset: int, station: str, signal_type: str) -> list:
    suffix = get_today_suffix(tz_offset)
    event_ticker = f"{ticker_prefix}-{suffix}"
    logger.info(f"[BRACKETS] Fetching {event_ticker}...")
    try:
        event_data = kalshi.get_event(event_ticker)
        event_obj = event_data.get('event', event_data)
        markets = event_obj.get('markets', [])
        brackets = []
        for m in markets:
            ticker = m.get('ticker', '')
            subtitle = m.get('yes_sub_title', m.get('subtitle', ticker))
            status = m.get('status', '')
            result = m.get('result', '')
            if status not in ('active', 'open', 'initialized'):
                continue
            if result and result != '':
                continue
            floor_raw = m.get('floor_strike')
            cap_raw = m.get('cap_strike')
            strike_type = m.get('strike_type', 'between')
            floor_strike = int(floor_raw) if floor_raw is not None else None
            cap_strike = int(cap_raw) if cap_raw is not None else None
            no_ask = parse_kalshi_price(m.get('no_ask'), m.get('no_ask_dollars'))
            yes_ask = parse_kalshi_price(m.get('yes_ask'), m.get('yes_ask_dollars'))
            b = BracketState(
                ticker=ticker, subtitle=subtitle,
                floor_strike=floor_strike, cap_strike=cap_strike,
                strike_type=strike_type, signal_type=signal_type, station=station
            )
            b.no_ask = no_ask
            b.yes_ask = yes_ask
            brackets.append(b)
            logger.info(f"  {subtitle:<25s} floor={floor_strike} cap={cap_strike} "
                       f"({strike_type}) NO@{no_ask}¢ [{ticker}]")
        logger.info(f"[BRACKETS] Loaded {len(brackets)} active brackets")
        return brackets
    except Exception as e:
        logger.error(f"[BRACKETS] Failed: {e}")
        return []


# ─── State + Trading ─────────────────────────────────────────

class SniperState:
    def __init__(self):
        self.probable_high = None
        self.probable_high_max = None
        self.call_count = 0
        self.parse_count = 0
        self.parse_failures = 0
        self.total_cost = 0.0
        self.traded_tickers = set()
        self.trades = []
        self.readings = []
        self.last_zulu = None
        self.last_temp_c = None

    def update_high(self, temp_c: int, zulu: str):
        candidates = compute_omo_candidates(temp_c)
        lowest_probable = min(candidates)
        highest_probable = max(candidates)
        self.readings.append((datetime.now(timezone.utc).isoformat(), zulu, temp_c, candidates))
        changed = False
        if self.probable_high is None or lowest_probable > self.probable_high:
            self.probable_high = lowest_probable
            changed = True
        if self.probable_high_max is None or highest_probable > self.probable_high_max:
            self.probable_high_max = highest_probable
        return changed, lowest_probable, highest_probable, candidates

    def is_duplicate(self, temp_c: int, zulu: str) -> bool:
        """Same reading as last time (ASOS loop repeat)."""
        if self.last_zulu and self.last_temp_c is not None:
            if zulu == self.last_zulu and temp_c == self.last_temp_c:
                return True
        return False


state = SniperState()


def check_and_trade(brackets: list, bid_price: int, qty: int, live: bool):
    if state.probable_high is None:
        return
    for b in brackets:
        if b.ticker in state.traded_tickers or b.traded:
            continue
        old_status = b.status
        new_status = b.check_status(state.probable_high, None)
        if new_status == old_status:
            continue
        b.status = new_status
        if new_status == 'dead':
            action, side, icon = 'BUY_NO', 'no', '🔴'
        elif new_status == 'locked':
            action, side, icon = 'BUY_YES', 'yes', '🟢'
        else:
            continue

        logger.info(f"\n{'='*60}")
        logger.info(f"{icon} BRACKET KILL: {b.subtitle}")
        logger.info(f"   {old_status} → {new_status} | probable_high={state.probable_high}°F")
        logger.info(f"   Action: {action} {b.ticker} @ {bid_price}¢ x{qty}")
        logger.info(f"{'='*60}")

        if not live:
            logger.info(f"   🧪 DRY RUN")
            state.traded_tickers.add(b.ticker)
            b.traded = True
            state.trades.append({
                'time': datetime.now(timezone.utc).isoformat(),
                'ticker': b.ticker, 'subtitle': b.subtitle,
                'action': action, 'price': bid_price,
                'quantity': qty, 'dry_run': True,
                'probable_high': state.probable_high,
            })
            continue

        try:
            result = kalshi.create_order(
                ticker=b.ticker, side=side, action='buy',
                count=qty, order_type='limit', price_cents=bid_price
            )
            order_id = result.get('order', {}).get('order_id', '???')
            logger.info(f"   ✅ ORDER PLACED: {order_id}")
            state.traded_tickers.add(b.ticker)
            b.traded = True
            state.trades.append({
                'time': datetime.now(timezone.utc).isoformat(),
                'ticker': b.ticker, 'subtitle': b.subtitle,
                'action': action, 'price': bid_price,
                'quantity': qty, 'order_id': order_id,
                'dry_run': False, 'probable_high': state.probable_high,
            })
        except Exception as e:
            logger.error(f"   ❌ TRADE FAILED: {e}")


# ─── Schedule helpers ─────────────────────────────────────────

def parse_time_str(t: str) -> tuple:
    parts = t.strip().split(':')
    return int(parts[0]), int(parts[1])


def should_run(args, tz_offset: int, start_time: float) -> bool:
    now_utc = datetime.now(timezone.utc)

    if args.run_minutes is not None:
        elapsed = time.time() - start_time
        return elapsed < (args.run_minutes * 60)

    if args.start and args.end:
        local = now_utc + timedelta(hours=tz_offset)
        local_hm = local.hour * 60 + local.minute
        start_h, start_m = parse_time_str(args.start)
        end_h, end_m = parse_time_str(args.end)
        start_mins = start_h * 60 + start_m
        end_mins = end_h * 60 + end_m
        if end_mins > start_mins:
            return start_mins <= local_hm < end_mins
        else:
            return local_hm >= start_mins or local_hm < end_mins

    if args.start_utc and args.end_utc:
        utc_hm = now_utc.hour * 60 + now_utc.minute
        start_h, start_m = parse_time_str(args.start_utc)
        end_h, end_m = parse_time_str(args.end_utc)
        start_mins = start_h * 60 + start_m
        end_mins = end_h * 60 + end_m
        if end_mins > start_mins:
            return start_mins <= utc_hm < end_mins
        else:
            return utc_hm >= start_mins or utc_hm < end_mins

    elapsed = time.time() - start_time
    return elapsed < 3600


def should_wait_to_start(args, tz_offset: int) -> bool:
    now_utc = datetime.now(timezone.utc)

    if args.start:
        local = now_utc + timedelta(hours=tz_offset)
        local_hm = local.hour * 60 + local.minute
        start_h, start_m = parse_time_str(args.start)
        start_mins = start_h * 60 + start_m
        if args.end:
            end_h, end_m = parse_time_str(args.end)
            end_mins = end_h * 60 + end_m
            if end_mins > start_mins:
                return local_hm < start_mins
            else:
                return end_mins <= local_hm < start_mins
        return local_hm < start_mins

    if args.start_utc:
        utc_hm = now_utc.hour * 60 + now_utc.minute
        start_h, start_m = parse_time_str(args.start_utc)
        start_mins = start_h * 60 + start_m
        if args.end_utc:
            end_h, end_m = parse_time_str(args.end_utc)
            end_mins = end_h * 60 + end_m
            if end_mins > start_mins:
                return utc_hm < start_mins
            else:
                return end_mins <= utc_hm < start_mins
        return utc_hm < start_mins

    return False


# ─── CSV logging ──────────────────────────────────────────────

LOG_FILE = None

def init_log(station: str):
    global LOG_FILE
    LOG_FILE = f'/tmp/{station.lower()}_stream_sniper.csv'
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'w') as f:
            f.write("timestamp,zulu,temp_c,omo_low,omo_high,probable_high,new_high,trade,error\n")


def log_row(ts, zulu, temp_c, candidates, new_high, trade, error):
    with open(LOG_FILE, 'a') as f:
        omo_lo = min(candidates) if candidates else ''
        omo_hi = max(candidates) if candidates else ''
        f.write(f"{ts},{zulu or ''},{temp_c or ''},{omo_lo},{omo_hi},"
                f"{state.probable_high or ''},{new_high},{trade},{error or ''}\n")


# ──────────────────────────────────────────────────────────────
# CORE: Persistent call with continuous parsing
# ──────────────────────────────────────────────────────────────

parse_queue: asyncio.Queue = None


class StreamingCall:
    """One long-running Twilio Media Stream call.
    
    Continuously watches transcript for temperature readings.
    Each parse pushes (temp_c, zulu) onto parse_queue for the main loop.
    ASOS loops every ~60s → one reading per minute from a single call.
    """

    def __init__(self):
        self.call_start = None
        self.stream_sid = None
        self.call_sid = None
        self.deepgram_ws = None
        self.transcript_window = ""
        self.total_parses = 0

    def _dg_is_open(self):
        if self.deepgram_ws is None:
            return False
        return self.deepgram_ws.close_code is None

    async def handle_twilio_ws(self, websocket):
        self.call_start = time.time()
        logger.info("[STREAM] Twilio WebSocket connected")

        dg_url = (
            f"wss://api.deepgram.com/v1/listen?"
            f"encoding=mulaw&sample_rate=8000&channels=1"
            f"&model=nova-2&language=en-US"
            f"&punctuate=true&smart_format=true"
            f"&interim_results=true"
        )
        dg_headers = {"Authorization": f"Token {deepgram_key}"}

        try:
            self.deepgram_ws = await websockets.connect(dg_url, additional_headers=dg_headers)
            logger.info("[STREAM] Deepgram WebSocket connected")
        except Exception as e:
            logger.error(f"[STREAM] Deepgram connect failed: {e}")
            return

        try:
            await asyncio.gather(
                self._receive_twilio(websocket),
                self._receive_deepgram(),
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[STREAM] Session ended: {e}")
        finally:
            if self._dg_is_open():
                try:
                    await self.deepgram_ws.close()
                except:
                    pass
            logger.info(f"[STREAM] Call ended after {self.total_parses} parses")

    async def _receive_twilio(self, websocket):
        try:
            async for message in websocket:
                data = json.loads(message)
                event = data.get('event')

                if event == 'start':
                    self.stream_sid = data['start'].get('streamSid')
                    self.call_sid = data['start'].get('callSid')
                    logger.info(f"[STREAM] Stream started: {self.stream_sid}")

                elif event == 'media':
                    payload = data['media']['payload']
                    audio_bytes = base64.b64decode(payload)
                    if self._dg_is_open():
                        await self.deepgram_ws.send(audio_bytes)

                elif event == 'stop':
                    logger.info("[STREAM] Twilio stream stopped")
                    if self._dg_is_open():
                        try:
                            await self.deepgram_ws.send(json.dumps({"type": "CloseStream"}))
                        except:
                            pass
                    break

        except websockets.exceptions.ConnectionClosed:
            logger.info("[STREAM] Twilio WebSocket closed")

    async def _receive_deepgram(self):
        """Continuously watch transcript for temperature readings."""
        try:
            async for message in self.deepgram_ws:
                data = json.loads(message)
                channel = data.get('channel', {})
                alternatives = channel.get('alternatives', [])
                if not alternatives:
                    continue

                transcript_chunk = alternatives[0].get('transcript', '')
                is_final = data.get('is_final', False)

                if not transcript_chunk:
                    continue

                if is_final:
                    self.transcript_window += " " + transcript_chunk
                    self.transcript_window = self.transcript_window.strip()

                    temp_c, temp_f = parse_temperature(self.transcript_window)
                    if temp_c is not None:
                        zulu = parse_zulu_time(self.transcript_window)
                        self.total_parses += 1
                        elapsed = time.time() - self.call_start
                        logger.info(f"[STREAM] 🎯 #{self.total_parses} ({elapsed:.0f}s): "
                                   f"{temp_c}°C → {temp_f}°F | {zulu or '????Z'}")
                        await parse_queue.put((temp_c, zulu))
                        self.transcript_window = ""

                    # Prevent unbounded growth — keep tail for boundary spans
                    if len(self.transcript_window) > 500:
                        self.transcript_window = self.transcript_window[-200:]

                else:
                    # Interim results — faster detection
                    combined = self.transcript_window + " " + transcript_chunk
                    temp_c, temp_f = parse_temperature(combined)
                    if temp_c is not None:
                        zulu = parse_zulu_time(combined)
                        self.total_parses += 1
                        elapsed = time.time() - self.call_start
                        logger.info(f"[STREAM] 🎯 #{self.total_parses} (interim, {elapsed:.0f}s): "
                                   f"{temp_c}°C → {temp_f}°F | {zulu or '????Z'}")
                        await parse_queue.put((temp_c, zulu))
                        self.transcript_window = ""

        except websockets.exceptions.ConnectionClosed:
            logger.info("[STREAM] Deepgram WebSocket closed")


# ─── WebSocket server ─────────────────────────────────────────

active_call: StreamingCall = None
call_connected_event: asyncio.Event = None


async def ws_handler(websocket):
    global active_call

    if active_call is None:
        logger.debug("[WS] No active call object — ignoring connection")
        return

    if call_connected_event:
        call_connected_event.set()

    await active_call.handle_twilio_ws(websocket)


# ─── Main loop ────────────────────────────────────────────────

async def main_loop(args, station_cfg):
    global active_call, call_connected_event, parse_queue

    station = args.station.upper()
    tz_offset = station_cfg['tz_offset']
    station_phone = args.phone or station_cfg['phone']
    ticker_prefix = args.high_ticker or station_cfg.get(f'{args.signal}_ticker')
    signal_type = args.signal
    ws_url = args.ws_url
    if not ws_url.endswith('/stream'):
        ws_url = ws_url.rstrip('/') + '/stream'

    init_log(station)
    brackets = load_brackets(ticker_prefix, tz_offset, station, signal_type)
    if not brackets:
        logger.error("No brackets loaded — check Kalshi API or event ticker")
        return

    n = len(brackets)
    mode_str = 'LIVE 🔴' if args.live else 'DRY RUN 🧪'
    schedule_str = ''
    if args.run_minutes:
        schedule_str = f'{args.run_minutes} minutes from now'
    elif args.start and args.end:
        schedule_str = f'{args.start}-{args.end} local'
    elif args.start_utc and args.end_utc:
        schedule_str = f'{args.start_utc}-{args.end_utc} UTC'
    else:
        schedule_str = '60 minutes (default)'

    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  STREAM SNIPER — {mode_str:<12s}                               ║
╠══════════════════════════════════════════════════════════════════╣
║  Station:   {station} ({station_cfg['name']})
║  Phone:     {station_phone}
║  Signal:    {signal_type.upper()} brackets
║  Schedule:  {schedule_str}
║  Bid:       {args.bid}¢ x {args.qty} contracts
║  Brackets:  {n} active
║  Stream:    {ws_url}
╚══════════════════════════════════════════════════════════════════╝
""")
    for b in brackets:
        print(f"  {b.subtitle:<25s} floor={b.floor_strike} cap={b.cap_strike} "
              f"({b.strike_type}) NO@{b.no_ask}¢ [{b.ticker}]")
    print()

    # Wait for start time if specified
    while should_wait_to_start(args, tz_offset):
        now_utc = datetime.now(timezone.utc)
        local = now_utc + timedelta(hours=tz_offset)
        logger.info(f"⏳ Waiting for start time... (local: {local.strftime('%H:%M')}, UTC: {now_utc.strftime('%H:%M')})")
        await asyncio.sleep(30)

    start_time = time.time()
    last_bracket_refresh = time.time()
    parse_queue = asyncio.Queue()
    call_sid = None

    # ── Outer loop: manages the call. If it drops, redial. ──
    while should_run(args, tz_offset, start_time):

        active_call = StreamingCall()
        call_connected_event = asyncio.Event()
        state.call_count += 1

        now_str = datetime.now(timezone.utc).strftime('%H:%M:%SZ')
        logger.info(f"📞 #{state.call_count} ({now_str}) calling {station}...")

        twiml = f"""<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""

        try:
            call = twilio_client.calls.create(
                to=station_phone,
                from_=from_num,
                twiml=twiml,
                timeout=30,
            )
            call_sid = call.sid
            logger.info(f"   Call SID: {call_sid}")
        except Exception as e:
            logger.error(f"   ❌ Call failed: {e}")
            active_call = None
            await asyncio.sleep(5)
            continue

        # Wait for WebSocket to connect
        try:
            await asyncio.wait_for(call_connected_event.wait(), timeout=55)
            logger.info("   ✅ Stream connected — listening continuously")
        except asyncio.TimeoutError:
            logger.warning("   ⏰ No stream connection — will redial")
            try:
                twilio_client.calls(call_sid).update(status='completed')
            except:
                pass
            active_call = None
            await asyncio.sleep(3)
            continue

        # ── Inner loop: consume parsed readings while call is alive ──
        while should_run(args, tz_offset, start_time):

            # Refresh brackets every 10 minutes
            if time.time() - last_bracket_refresh > 600:
                try:
                    new_brackets = load_brackets(ticker_prefix, tz_offset, station, signal_type)
                    if new_brackets:
                        for nb in new_brackets:
                            if nb.ticker in state.traded_tickers:
                                nb.traded = True
                                nb.status = 'dead'
                        brackets = new_brackets
                except:
                    pass
                last_bracket_refresh = time.time()

            # Wait for next parsed reading (timeout to check call health)
            try:
                temp_c, zulu = await asyncio.wait_for(parse_queue.get(), timeout=90)
            except asyncio.TimeoutError:
                # No reading in 90s — check if call is still alive
                try:
                    c = twilio_client.calls(call_sid).fetch()
                    if c.status in ('completed', 'failed', 'busy', 'no-answer', 'canceled'):
                        logger.warning(f"   📴 Call ended ({c.status}) — will redial")
                        break
                    else:
                        logger.debug(f"   Call still active ({c.status})")
                        continue
                except:
                    logger.warning("   📴 Call status check failed — will redial")
                    break

            ts = datetime.now(timezone.utc).isoformat()
            state.parse_count += 1

            # Deduplicate ASOS loop repeats
            if state.is_duplicate(temp_c, zulu):
                logger.info(f"   🔄 Same reading ({temp_c}°C {zulu}) — skipping")
                continue

            state.last_temp_c = temp_c
            state.last_zulu = zulu

            # Update state
            changed, lo_prob, hi_prob, candidates = state.update_high(temp_c, zulu or '????Z')
            marker = ' ⬆️  NEW HIGH' if changed else ''
            logger.info(f"   🌡️  {temp_c}°C → OMO [{lo_prob}-{hi_prob}] | "
                       f"H≥{state.probable_high}°F{marker}")

            # Check brackets
            trade_str = ''
            pre_trades = len(state.trades)
            check_and_trade(brackets, args.bid, args.qty, args.live)
            if len(state.trades) > pre_trades:
                trade_str = state.trades[-1]['action'] + ':' + state.trades[-1]['ticker']

            log_row(ts, zulu, temp_c, candidates, changed, trade_str, '')

            # Update cost estimate
            elapsed_min = (time.time() - active_call.call_start) / 60
            state.total_cost = elapsed_min * 0.011

        # ── Call ended or schedule done — hang up ──
        if call_sid:
            try:
                twilio_client.calls(call_sid).update(status='completed')
                logger.info("   📴 Call hung up")
            except:
                pass
        active_call = None


# ─── Entry point ──────────────────────────────────────────────

async def run(args, station_cfg):
    server = await websockets.serve(ws_handler, "0.0.0.0", args.port)
    logger.info(f"[WS] Server listening on port {args.port}")

    try:
        await main_loop(args, station_cfg)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        await server.wait_closed()

    print(f"\n{'='*60}")
    print(f"📊 STREAM SNIPER — SESSION SUMMARY")
    print(f"{'='*60}")
    print(f"  Station:        {args.station}")
    print(f"  Calls:          {state.call_count}")
    print(f"  Readings:       {state.parse_count}")
    print(f"  Est. cost:      ${state.total_cost:.2f}")
    print(f"  Probable HIGH:  ≥{state.probable_high}°F")
    if state.trades:
        print(f"\n  TRADES ({len(state.trades)}):")
        for t in state.trades:
            dry = " [DRY]" if t['dry_run'] else " ✅"
            print(f"    {t['time']} | {t['action']} {t['subtitle']} @ {t['price']}¢{dry}")
    else:
        print(f"  No trades")
    print(f"\n  Log: {LOG_FILE}")
    print(f"{'='*60}")


if __name__ == '__main__':
    args = parse_args()

    station = args.station.upper()
    if station not in STATION_REGISTRY:
        print(f"❌ Unknown station: {station}")
        print(f"   Available: {', '.join(sorted(STATION_REGISTRY.keys()))}")
        sys.exit(1)

    station_cfg = STATION_REGISTRY[station]
    ticker = args.high_ticker or station_cfg.get(f'{args.signal}_ticker')
    if not ticker:
        print(f"❌ No {args.signal} ticker configured for {station}")
        sys.exit(1)

    logger.info(f"[CONFIG] Station={station} Phone={args.phone or station_cfg['phone']} "
               f"Ticker={ticker} Signal={args.signal}")

    try:
        asyncio.run(run(args, station_cfg))
    except KeyboardInterrupt:
        print("\nStopped.")
