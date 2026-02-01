#!/usr/bin/env python3
"""
klas_stream_sniper.py - Real-time streaming KLAS ASOS phone sniper
===================================================================

ARCHITECTURE:
  1. Twilio calls KLAS ASOS
  2. Twilio streams raw audio via WebSocket to THIS server
  3. This server pipes audio to Deepgram streaming WebSocket
  4. Deepgram sends back real-time transcript fragments
  5. The moment we see "temperature ... digits ... celsius" → parse → trade
  6. Hang up call → immediately start next call

LATENCY: ~15s from call start to trade (vs ~55s with record-and-download)
COST: Lower per call (shorter calls, no recording storage)

REQUIRES:
  - ngrok (or public domain with SSL) for Twilio to reach this server
  - .env with: TWILIO_*, DEEPGRAM_API_KEY, KALSHI_*, NGROK_URL

Usage:
  Terminal 1: ngrok http 8765
  Terminal 2: python3 klas_stream_sniper.py

  The script reads NGROK_URL from .env or you can pass it as arg:
    python3 klas_stream_sniper.py wss://your-ngrok-url.ngrok-free.dev/stream
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
import signal
import threading
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
    print(f"❌ Missing: {', '.join(missing)}")
    sys.exit(1)

from twilio.rest import Client as TwilioClient
twilio_client = TwilioClient(account_sid, auth_token)

import websockets

# ─── Config ───────────────────────────────────────────────────
STATION          = 'KSFO'
STATION_PHONE    = '+16508278593'   # KSFO ASOS
STATION_TZ_OFFSET = -8              # PST
WS_PORT          = 8765
MAX_CALL_SECONDS = 45       # hang up after this even if no parse (safety)
COST_PER_CALL    = 0.02     # ~$0.014 Twilio (shorter call) + ~$0.003 Deepgram

BID_PRICE        = int(os.getenv('SCOUT_BID_PRICE', '99'))
TRADE_QUANTITY   = int(os.getenv('SCOUT_TRADE_QUANTITY', '1'))
LIVE_MODE        = os.getenv('LIVE_MODE', 'false').lower() == 'true'

# Run for 60 minutes from start
RUN_MINUTES      = 60
KALSHI_HIGH_TICKER = 'KXHIGHTSFO'

LOG_FILE = '/tmp/ksfo_stream_sniper.csv'

# ─── Resolve ngrok URL ────────────────────────────────────────
NGROK_URL = None
if len(sys.argv) > 1:
    NGROK_URL = sys.argv[1]
elif os.getenv('NGROK_URL'):
    NGROK_URL = os.getenv('NGROK_URL')
else:
    # Try to auto-detect from ngrok API
    try:
        resp = requests.get('http://127.0.0.1:4040/api/tunnels', timeout=3)
        tunnels = resp.json().get('tunnels', [])
        for t in tunnels:
            if t.get('proto') == 'https':
                NGROK_URL = t['public_url']
                break
    except:
        pass

if not NGROK_URL:
    print("❌ Cannot determine ngrok URL.")
    print("   Pass it as argument: python3 klas_stream_sniper.py wss://your-url.ngrok-free.dev/stream")
    print("   Or set NGROK_URL in .env")
    print("   Or make sure ngrok is running (we check http://127.0.0.1:4040)")
    sys.exit(1)

# Convert https:// to wss:// for WebSocket
WS_URL = NGROK_URL.replace('https://', 'wss://').replace('http://', 'ws://')
if not WS_URL.endswith('/stream'):
    WS_URL = WS_URL.rstrip('/') + '/stream'

logger.info(f"[NGROK] WebSocket URL: {WS_URL}")


# ─── Parsing (same proven logic from klas_phone_sniper.py) ────

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
    from smart_poller import KalshiClient, BracketState, STATIONS
    kalshi = KalshiClient()
    logger.info("[KALSHI] Client initialized")
except ImportError as e:
    logger.error(f"[KALSHI] Cannot import from smart_poller: {e}")
    sys.exit(1)


# ─── Bracket loading (same as klas_phone_sniper.py) ───────────

def get_today_suffix() -> str:
    now_utc = datetime.now(timezone.utc)
    local = now_utc + timedelta(hours=STATION_TZ_OFFSET)
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


def load_brackets() -> list:
    suffix = get_today_suffix()
    event_ticker = f"{KALSHI_HIGH_TICKER}-{suffix}"
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
                strike_type=strike_type, signal_type='high', station=STATION
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


# ─── State + Trading (same as klas_phone_sniper.py) ───────────

class PhoneSniperState:
    def __init__(self):
        self.probable_high = None
        self.probable_high_max = None
        self.call_count = 0
        self.parse_failures = 0
        self.total_cost = 0.0
        self.traded_tickers = set()
        self.trades = []
        self.readings = []

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


state = PhoneSniperState()


def check_and_trade(brackets: list):
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
        logger.info(f"   Action: {action} {b.ticker} @ {BID_PRICE}¢ x{TRADE_QUANTITY}")
        logger.info(f"{'='*60}")

        if not LIVE_MODE:
            logger.info(f"   🧪 DRY RUN")
            state.traded_tickers.add(b.ticker)
            b.traded = True
            state.trades.append({
                'time': datetime.now(timezone.utc).isoformat(),
                'ticker': b.ticker, 'subtitle': b.subtitle,
                'action': action, 'price': BID_PRICE,
                'quantity': TRADE_QUANTITY, 'dry_run': True,
                'probable_high': state.probable_high,
            })
            continue

        try:
            result = kalshi.create_order(
                ticker=b.ticker, side=side, action='buy',
                count=TRADE_QUANTITY, order_type='limit', price_cents=BID_PRICE
            )
            order_id = result.get('order', {}).get('order_id', '???')
            logger.info(f"   ✅ ORDER PLACED: {order_id}")
            state.traded_tickers.add(b.ticker)
            b.traded = True
            state.trades.append({
                'time': datetime.now(timezone.utc).isoformat(),
                'ticker': b.ticker, 'subtitle': b.subtitle,
                'action': action, 'price': BID_PRICE,
                'quantity': TRADE_QUANTITY, 'order_id': order_id,
                'dry_run': False, 'probable_high': state.probable_high,
            })
        except Exception as e:
            logger.error(f"   ❌ TRADE FAILED: {e}")


# ─── Helpers ──────────────────────────────────────────────────

def is_past_cutoff(start_time) -> bool:
    return (time.time() - start_time) > (RUN_MINUTES * 60)

def init_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'w') as f:
            f.write("timestamp,zulu,temp_c,omo_low,omo_high,probable_high,new_high,latency_s,trade,error\n")

def log_row(ts, zulu, temp_c, candidates, new_high, latency, trade, error):
    with open(LOG_FILE, 'a') as f:
        omo_lo = min(candidates) if candidates else ''
        omo_hi = max(candidates) if candidates else ''
        f.write(f"{ts},{zulu or ''},{temp_c or ''},{omo_lo},{omo_hi},"
                f"{state.probable_high or ''},{new_high},{latency:.1f},{trade},{error or ''}\n")


# ──────────────────────────────────────────────────────────────
# CORE: WebSocket server that receives Twilio audio, pipes to
# Deepgram, and watches for temperature in real-time transcript
# ──────────────────────────────────────────────────────────────

class StreamingCall:
    """Handles one Twilio Media Stream call."""

    def __init__(self, brackets):
        self.brackets = brackets
        self.transcript_so_far = ""
        self.temp_c = None
        self.temp_f = None
        self.zulu = None
        self.parsed = False
        self.call_start = None
        self.stream_sid = None
        self.call_sid = None
        self.deepgram_ws = None

    async def handle_twilio_ws(self, websocket):
        """Handle incoming Twilio Media Stream WebSocket connection."""
        self.call_start = time.time()
        logger.info("[STREAM] Twilio WebSocket connected")

        # Open Deepgram streaming connection
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

        # Run two tasks: receive from Twilio, receive from Deepgram
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
            if self.deepgram_ws and not self.deepgram_ws.closed:
                try:
                    await self.deepgram_ws.close()
                except:
                    pass

    async def _receive_twilio(self, websocket):
        """Receive audio from Twilio and forward to Deepgram."""
        try:
            async for message in websocket:
                data = json.loads(message)
                event = data.get('event')

                if event == 'start':
                    self.stream_sid = data['start'].get('streamSid')
                    self.call_sid = data['start'].get('callSid')
                    logger.info(f"[STREAM] Stream started: {self.stream_sid}")

                elif event == 'media':
                    # Twilio sends base64-encoded mulaw audio
                    payload = data['media']['payload']
                    audio_bytes = base64.b64decode(payload)

                    # Forward raw audio to Deepgram
                    if self.deepgram_ws and not self.deepgram_ws.closed:
                        await self.deepgram_ws.send(audio_bytes)

                elif event == 'stop':
                    logger.info("[STREAM] Twilio stream stopped")
                    # Signal Deepgram to finalize
                    if self.deepgram_ws and not self.deepgram_ws.closed:
                        try:
                            await self.deepgram_ws.send(json.dumps({"type": "CloseStream"}))
                        except:
                            pass
                    break

                # Safety timeout — hang up if we've been listening too long
                if self.call_start and (time.time() - self.call_start) > MAX_CALL_SECONDS:
                    if not self.parsed:
                        logger.warning(f"[STREAM] {MAX_CALL_SECONDS}s timeout — no parse")
                    break

                # If we got a temp, stop receiving (we'll hang up)
                if self.parsed:
                    break

        except websockets.exceptions.ConnectionClosed:
            pass

    async def _receive_deepgram(self):
        """Receive transcript fragments from Deepgram and watch for temperature."""
        try:
            async for message in self.deepgram_ws:
                data = json.loads(message)

                # Extract transcript from Deepgram response
                channel = data.get('channel', {})
                alternatives = channel.get('alternatives', [])
                if not alternatives:
                    continue

                transcript_chunk = alternatives[0].get('transcript', '')
                is_final = data.get('is_final', False)

                if not transcript_chunk:
                    continue

                # Accumulate transcript
                if is_final:
                    self.transcript_so_far += " " + transcript_chunk
                    self.transcript_so_far = self.transcript_so_far.strip()

                    # Try to parse temperature from accumulated transcript
                    temp_c, temp_f = parse_temperature(self.transcript_so_far)
                    if temp_c is not None:
                        self.temp_c = temp_c
                        self.temp_f = temp_f
                        self.zulu = parse_zulu_time(self.transcript_so_far)
                        self.parsed = True
                        latency = time.time() - self.call_start
                        logger.info(f"[STREAM] 🎯 PARSED in {latency:.1f}s: "
                                   f"{temp_c}°C → {temp_f}°F | {self.zulu or '????Z'}")
                        return  # Exit Deepgram loop — triggers cleanup
                else:
                    # Check interim results too (faster trigger)
                    combined = self.transcript_so_far + " " + transcript_chunk
                    temp_c, temp_f = parse_temperature(combined)
                    if temp_c is not None:
                        self.temp_c = temp_c
                        self.temp_f = temp_f
                        self.zulu = parse_zulu_time(combined)
                        self.parsed = True
                        self.transcript_so_far = combined.strip()
                        latency = time.time() - self.call_start
                        logger.info(f"[STREAM] 🎯 PARSED (interim) in {latency:.1f}s: "
                                   f"{temp_c}°C → {temp_f}°F | {self.zulu or '????Z'}")
                        return

        except websockets.exceptions.ConnectionClosed:
            pass


# ─── WebSocket server ─────────────────────────────────────────

# Global reference to current streaming call (for coordination)
current_call: StreamingCall = None
call_complete_event: asyncio.Event = None
brackets: list = []


async def ws_handler(websocket):
    """Handle incoming WebSocket connections from Twilio."""
    global current_call

    # Only handle /stream path (websockets v13+ uses websocket.request.path)
    ws_path = ''
    try:
        ws_path = websocket.request.path if hasattr(websocket, 'request') else ''
    except:
        pass

    if current_call is None:
        return

    await current_call.handle_twilio_ws(websocket)

    # Signal that the call processing is done
    if call_complete_event:
        call_complete_event.set()


# ─── Call orchestrator ────────────────────────────────────────

async def make_streaming_call():
    """Initiate one call and wait for the stream to complete."""
    global current_call, call_complete_event

    state.call_count += 1
    state.total_cost += COST_PER_CALL

    call_complete_event = asyncio.Event()
    current_call = StreamingCall(brackets)

    now_str = datetime.now(timezone.utc).strftime('%H:%M:%SZ')
    logger.info(f"📞 #{state.call_count:03d} ({now_str}) calling KLAS... [${state.total_cost:.2f}]")

    # TwiML: connect call and stream audio to our WebSocket server
    twiml = f"""<Response>
    <Connect>
        <Stream url="{WS_URL}" />
    </Connect>
</Response>"""

    try:
        call = twilio_client.calls.create(
            to=STATION_PHONE,
            from_=from_num,
            twiml=twiml,
            timeout=30,
        )
        call_sid = call.sid
        logger.info(f"   Call SID: {call_sid}")
    except Exception as e:
        logger.error(f"   ❌ Call failed: {e}")
        current_call = None
        return None, None, str(e)

    # Wait for the WebSocket handler to finish (or timeout)
    try:
        await asyncio.wait_for(call_complete_event.wait(), timeout=MAX_CALL_SECONDS + 10)
    except asyncio.TimeoutError:
        logger.warning("   ⏰ Call timed out waiting for stream")

    # Grab results from the streaming call
    result_call = current_call
    current_call = None

    # Hang up the call
    try:
        twilio_client.calls(call_sid).update(status='completed')
        logger.info("   📴 Call hung up")
    except Exception as e:
        logger.debug(f"   Hangup: {e}")

    if result_call.parsed:
        return result_call.temp_c, result_call.zulu, result_call.transcript_so_far
    else:
        state.parse_failures += 1
        snippet = result_call.transcript_so_far[:80] if result_call.transcript_so_far else 'empty'
        return None, None, f'no_parse: "{snippet}"'


# ─── Main loop ────────────────────────────────────────────────

async def main_loop():
    global brackets

    init_log()
    brackets = load_brackets()
    if not brackets:
        logger.error("No brackets loaded")
        return

    n = len(brackets)
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║     {STATION} STREAM SNIPER — {'LIVE 🔴' if LIVE_MODE else 'DRY RUN 🧪'} — REAL-TIME              ║
╠══════════════════════════════════════════════════════════════════╣
║  Station:  {STATION} (San Francisco International)                  ║
║  Phone:    {STATION_PHONE}                                      ║
║  Mode:     Twilio Media Streams → Deepgram WebSocket            ║
║  Latency:  ~15s per reading (vs ~55s record+download)           ║
║  Runtime:  {RUN_MINUTES} minutes                                            ║
║  Bid:      {BID_PRICE}¢ x {TRADE_QUANTITY} contracts                                       ║
║  Brackets: {n} active HIGH brackets                              ║
║  Stream:   {WS_URL:<52s} ║
╚══════════════════════════════════════════════════════════════════╝
""")
    for b in brackets:
        print(f"  {b.subtitle:<25s} floor={b.floor_strike} cap={b.cap_strike} "
              f"({b.strike_type}) NO@{b.no_ask}¢ [{b.ticker}]")
    print()

    start_time = time.time()

    while True:
        if is_past_cutoff(start_time):
            logger.info(f"⏰ {RUN_MINUTES}min runtime reached — stopping")
            break

        # Phone sniper runs continuously — no METAR sleep needed
        # (Phone calls don't interfere with METAR parsing in smart_poller)

        # Make a streaming call
        temp_c, zulu, transcript_or_error = await make_streaming_call()
        ts = datetime.now(timezone.utc).isoformat()

        if temp_c is None:
            logger.warning(f"   ❌ {transcript_or_error}")
            log_row(ts, None, None, None, False, 0, '', transcript_or_error)
            await asyncio.sleep(3)
            continue

        # Update state
        changed, lo_prob, hi_prob, candidates = state.update_high(temp_c, zulu or '????Z')
        marker = ' ⬆️  NEW HIGH' if changed else ''
        logger.info(f"   🌡️  {temp_c}°C → OMO [{lo_prob}-{hi_prob}] | "
                   f"H≥{state.probable_high}°F{marker}")

        # Check brackets
        trade_str = ''
        pre_trades = len(state.trades)
        check_and_trade(brackets)
        if len(state.trades) > pre_trades:
            trade_str = state.trades[-1]['action'] + ':' + state.trades[-1]['ticker']

        latency = 0
        if current_call and current_call.call_start:
            latency = time.time() - current_call.call_start
        log_row(ts, zulu, temp_c, candidates, changed, latency, trade_str, '')

        # Brief pause before next call
        await asyncio.sleep(3)


async def run():
    """Start WebSocket server and main loop concurrently."""

    # Start WebSocket server
    server = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT)
    logger.info(f"[WS] Server listening on port {WS_PORT}")

    try:
        await main_loop()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        await server.wait_closed()

    # Print summary
    print(f"\n{'='*60}")
    print(f"📊 STREAM SNIPER — SESSION SUMMARY")
    print(f"{'='*60}")
    print(f"  Calls:          {state.call_count}")
    print(f"  Parse failures: {state.parse_failures}")
    print(f"  Total cost:     ${state.total_cost:.2f}")
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
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
