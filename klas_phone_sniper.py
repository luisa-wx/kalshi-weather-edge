#!/usr/bin/env python3
"""
klas_phone_sniper.py - Live KLAS ASOS phone sniper
=====================================================================

Calls KLAS ASOS phone every ~60s during the afternoon climb.
Tracks running probable_high from phone readings.
Places REAL trades on Kalshi when brackets are killed.
Sleeps during METAR hot window (:50-:05) to avoid conflicts.
Stops at a configurable cutoff time.

Run alongside smart_poller.py, but with KLAS removed from Scout.

Usage:
    python3 klas_phone_sniper.py

Requires .env with:
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER,
    DEEPGRAM_API_KEY, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY
    Optional: LIVE_MODE=true, SCOUT_BID_PRICE=99, SCOUT_TRADE_QUANTITY=1
"""

import os
import sys
import time
import math
import json
import re
import logging
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
logger = logging.getLogger('phone_sniper')

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

from twilio.rest import Client
twilio_client = Client(account_sid, auth_token)

# ─── Config ───────────────────────────────────────────────────
KLAS_NUMBER      = '+17025825334'
LISTEN_SECONDS   = 35
POLL_INTERVAL    = 60       # target seconds between call starts
COST_PER_CALL    = 0.07

BID_PRICE        = int(os.getenv('SCOUT_BID_PRICE', '99'))
TRADE_QUANTITY   = int(os.getenv('SCOUT_TRADE_QUANTITY', '1'))
LIVE_MODE        = os.getenv('LIVE_MODE', 'false').lower() == 'true'

# Stop time: 4:15 PM PST = 00:15 UTC next day
# For Feb 1 PST -> Feb 2 00:15 UTC
# Adjust this if needed
STOP_UTC_HOUR    = 0
STOP_UTC_MINUTE  = 15

KALSHI_HIGH_TICKER = 'KXHIGHTLV'  # KLAS high series

LOG_FILE = '/tmp/klas_phone_sniper.csv'

# ─── Parsing (proven against real KLAS transcripts) ───────────

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


# ─── Kalshi Client (imported from smart_poller) ───────────────
# We import directly so we share the same auth logic

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from smart_poller import KalshiClient, BracketState, STATIONS
    kalshi = KalshiClient()
    logger.info("[KALSHI] Client initialized")
except ImportError as e:
    logger.error(f"[KALSHI] Cannot import from smart_poller: {e}")
    logger.error("  Make sure smart_poller.py is in the same directory")
    sys.exit(1)


# ─── Bracket loading from Kalshi API ─────────────────────────

def get_today_suffix() -> str:
    """Get Kalshi date suffix like '26FEB01'."""
    now_utc = datetime.now(timezone.utc)
    # KLAS is PST (UTC-8). Trading day = local standard date.
    local = now_utc - timedelta(hours=8)
    return local.strftime('%y%b%d').upper()


def load_brackets() -> list:
    """Fetch KLAS HIGH brackets from Kalshi API."""
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
            
            # Skip closed/determined markets
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
                ticker=ticker,
                subtitle=subtitle,
                floor_strike=floor_strike,
                cap_strike=cap_strike,
                strike_type=strike_type,
                signal_type='high',
                station='KLAS'
            )
            b.no_ask = no_ask
            b.yes_ask = yes_ask
            brackets.append(b)

            logger.info(f"  {subtitle:<25s} floor={floor_strike} cap={cap_strike} "
                       f"({strike_type}) NO@{no_ask}¢ YES@{yes_ask}¢ [{ticker}]")

        logger.info(f"[BRACKETS] Loaded {len(brackets)} active brackets")
        return brackets

    except Exception as e:
        logger.error(f"[BRACKETS] Failed to load: {e}")
        return []


def parse_kalshi_price(price_raw, price_dollars) -> int:
    """Parse Kalshi price into cents."""
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
    return 100  # default to max if unknown


# ─── State ────────────────────────────────────────────────────

class PhoneSniperState:
    def __init__(self):
        self.probable_high = None      # conservative (lowest_probable of max reading)
        self.probable_high_max = None  # optimistic (highest_probable of max reading)
        self.call_count = 0
        self.parse_failures = 0
        self.total_cost = 0.0
        self.traded_tickers = set()
        self.trades = []
        self.readings = []
        self.last_zulu = None

    def update_high(self, temp_c: int, zulu: str):
        candidates = compute_omo_candidates(temp_c)
        lowest_probable = min(candidates)
        highest_probable = max(candidates)

        self.readings.append((datetime.now(timezone.utc).isoformat(), zulu, temp_c, candidates))
        self.last_zulu = zulu

        old_high = self.probable_high
        changed = False

        if self.probable_high is None or lowest_probable > self.probable_high:
            self.probable_high = lowest_probable
            changed = True

        if self.probable_high_max is None or highest_probable > self.probable_high_max:
            self.probable_high_max = highest_probable

        return changed, lowest_probable, highest_probable, candidates


state = PhoneSniperState()


# ─── Bracket checking and trading ─────────────────────────────

def check_and_trade(brackets: list):
    """Check all brackets against probable_high. Trade on transitions."""
    if state.probable_high is None:
        return

    for b in brackets:
        if b.ticker in state.traded_tickers:
            continue
        if b.traded:
            continue

        old_status = b.status
        new_status = b.check_status(state.probable_high, None)

        if new_status == old_status:
            continue

        # TRANSITION DETECTED
        b.status = new_status

        if new_status == 'dead':
            action = 'BUY_NO'
            side = 'no'
            price = b.no_ask
            icon = '🔴'
        elif new_status == 'locked':
            action = 'BUY_YES'
            side = 'yes'
            price = b.yes_ask
            icon = '🟢'
        else:
            continue

        logger.info(f"\n{'='*60}")
        logger.info(f"{icon} BRACKET KILL: {b.subtitle}")
        logger.info(f"   {old_status} → {new_status} | probable_high={state.probable_high}°F")
        logger.info(f"   Action: {action} {b.ticker} @ {BID_PRICE}¢ x{TRADE_QUANTITY}")
        logger.info(f"   Market: NO@{b.no_ask}¢ YES@{b.yes_ask}¢")
        logger.info(f"{'='*60}")

        # EXECUTE TRADE
        if not LIVE_MODE:
            logger.info(f"   🧪 DRY RUN — would {action}")
            state.traded_tickers.add(b.ticker)
            b.traded = True
            state.trades.append({
                'time': datetime.now(timezone.utc).isoformat(),
                'ticker': b.ticker,
                'subtitle': b.subtitle,
                'action': action,
                'price': BID_PRICE,
                'quantity': TRADE_QUANTITY,
                'dry_run': True,
                'probable_high': state.probable_high,
            })
            continue

        # LIVE TRADE
        try:
            result = kalshi.create_order(
                ticker=b.ticker,
                side=side,
                action='buy',
                count=TRADE_QUANTITY,
                order_type='limit',
                price_cents=BID_PRICE
            )
            order_id = result.get('order', {}).get('order_id', '???')
            logger.info(f"   ✅ ORDER PLACED: {order_id} — {action} @ {BID_PRICE}¢ x{TRADE_QUANTITY}")
            logger.info(f"   Holding to settlement for {100 - BID_PRICE}¢/contract profit")

            state.traded_tickers.add(b.ticker)
            b.traded = True
            state.trades.append({
                'time': datetime.now(timezone.utc).isoformat(),
                'ticker': b.ticker,
                'subtitle': b.subtitle,
                'action': action,
                'price': BID_PRICE,
                'quantity': TRADE_QUANTITY,
                'order_id': order_id,
                'dry_run': False,
                'probable_high': state.probable_high,
            })

        except Exception as e:
            logger.error(f"   ❌ TRADE FAILED: {e}")


# ─── Single ASOS call ────────────────────────────────────────

def make_one_call():
    """Call KLAS, record, transcribe, parse. Returns (temp_c, zulu, transcript) or (None, None, error_msg)."""
    try:
        call = twilio_client.calls.create(
            to=KLAS_NUMBER,
            from_=from_num,
            record=True,
            recording_channels='mono',
            twiml=f'<Response><Pause length="{LISTEN_SECONDS}"/><Hangup/></Response>',
            timeout=30,
        )
        call_sid = call.sid

        # Wait for completion
        for i in range(LISTEN_SECONDS + 20):
            time.sleep(1)
            if (i + 1) % 10 == 0:
                c = twilio_client.calls(call_sid).fetch()
                if c.status in ('completed', 'failed', 'busy', 'no-answer', 'canceled'):
                    break

        # Get recording
        recording = None
        for attempt in range(5):
            recordings = twilio_client.recordings.list(call_sid=call_sid, limit=5)
            if recordings:
                recording = recordings[0]
                break
            time.sleep(3)

        if not recording:
            return None, None, 'no_recording'

        # Download WAV
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording.sid}.wav"
        wav = requests.get(url, auth=(account_sid, auth_token), timeout=30)
        wav.raise_for_status()

        # Transcribe
        dg = requests.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": "nova-2", "language": "en-US", "punctuate": "true",
                     "numerals": "true", "smart_format": "true"},
            headers={"Authorization": f"Token {deepgram_key}", "Content-Type": "audio/wav"},
            data=wav.content, timeout=30,
        )
        if dg.status_code != 200:
            return None, None, f'deepgram_{dg.status_code}'

        transcript = dg.json()['results']['channels'][0]['alternatives'][0]['transcript']

        # Parse
        temp_c, _ = parse_temperature(transcript)
        zulu = parse_zulu_time(transcript)

        # Cleanup recording
        try:
            twilio_client.recordings(recording.sid).delete()
        except:
            pass

        if temp_c is None:
            return None, None, f'parse_fail: "{transcript[:60]}"'

        return temp_c, zulu, transcript

    except Exception as e:
        return None, None, str(e)


# ─── METAR conflict check ────────────────────────────────────

def is_metar_window() -> bool:
    """True during minutes :50-:59 and :00-:05 (METAR hot window)."""
    minute = datetime.now(timezone.utc).minute
    return 50 <= minute <= 59 or minute <= 5


def is_past_cutoff() -> bool:
    """True if past the stop time."""
    now = datetime.now(timezone.utc)
    # Feb 2 00:15 UTC = Feb 1 4:15 PM PST
    cutoff = now.replace(hour=STOP_UTC_HOUR, minute=STOP_UTC_MINUTE, second=0, microsecond=0)
    # Handle day rollover: if current hour > cutoff hour, cutoff is tomorrow
    # For STOP_UTC_HOUR=0, if we're still on Feb 1 (UTC), cutoff is Feb 2 00:15
    if now.hour >= 6:  # Before midnight UTC — cutoff is next day
        cutoff += timedelta(days=1)
    return now >= cutoff


# ─── CSV logging ──────────────────────────────────────────────

def init_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'w') as f:
            f.write("timestamp,zulu,temp_c,omo_low,omo_high,probable_high,new_high,trade,error\n")


def log_row(ts, zulu, temp_c, candidates, new_high, trade, error):
    with open(LOG_FILE, 'a') as f:
        omo_lo = min(candidates) if candidates else ''
        omo_hi = max(candidates) if candidates else ''
        f.write(f"{ts},{zulu or ''},{temp_c or ''},{omo_lo},{omo_hi},"
                f"{state.probable_high or ''},{new_high},{trade},{error or ''}\n")


# ─── Main loop ────────────────────────────────────────────────

def print_banner(brackets):
    n = len(brackets)
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║          KLAS PHONE SNIPER — {'LIVE 🔴' if LIVE_MODE else 'DRY RUN 🧪'}                         ║
╠══════════════════════════════════════════════════════════════════╣
║  Station:  KLAS (Harry Reid International, Las Vegas)           ║
║  Phone:    (702) 582-5334                                       ║
║  Bid:      {BID_PRICE}¢ x {TRADE_QUANTITY} contracts                                       ║
║  Stop at:  {STOP_UTC_HOUR:02d}:{STOP_UTC_MINUTE:02d} UTC (4:15 PM PST)                              ║
║  Brackets: {n} active HIGH brackets                              ║
╚══════════════════════════════════════════════════════════════════╝
""")
    for b in brackets:
        print(f"  {b.subtitle:<25s} floor={b.floor_strike} cap={b.cap_strike} "
              f"({b.strike_type}) NO@{b.no_ask}¢ [{b.ticker}]")
    print()


def run():
    init_log()

    # Load brackets from Kalshi
    brackets = load_brackets()
    if not brackets:
        logger.error("No brackets loaded — check Kalshi API or event ticker")
        sys.exit(1)

    print_banner(brackets)

    in_metar_window = False

    try:
        while True:
            # Check cutoff
            if is_past_cutoff():
                logger.info("\n⏰ Past cutoff time — stopping")
                break

            # Check METAR window
            if is_metar_window():
                if not in_metar_window:
                    logger.info("💤 METAR window — phone sniper sleeping (METAR sniper has priority)")
                    in_metar_window = True
                time.sleep(10)
                continue

            if in_metar_window:
                logger.info("⏰ METAR window ended — phone sniper resuming")
                in_metar_window = False

                # Refresh bracket prices after METAR window
                # (a METAR may have moved prices)
                try:
                    new_brackets = load_brackets()
                    if new_brackets:
                        # Preserve traded state
                        for nb in new_brackets:
                            if nb.ticker in state.traded_tickers:
                                nb.traded = True
                                nb.status = 'dead'  # or locked
                        brackets = new_brackets
                        logger.info(f"[BRACKETS] Refreshed {len(brackets)} brackets")
                except:
                    pass

            # Make the call
            state.call_count += 1
            state.total_cost += COST_PER_CALL

            now_str = datetime.now(timezone.utc).strftime('%H:%M:%SZ')
            logger.info(f"📞 #{state.call_count:03d} ({now_str}) calling KLAS... "
                       f"[${state.total_cost:.2f} spent]")

            temp_c, zulu, transcript_or_error = make_one_call()
            ts = datetime.now(timezone.utc).isoformat()

            if temp_c is None:
                state.parse_failures += 1
                logger.warning(f"   ❌ {transcript_or_error}")
                log_row(ts, None, None, None, False, '', transcript_or_error)
                time.sleep(max(5, POLL_INTERVAL - 50))
                continue

            # Update state
            changed, lo_prob, hi_prob, candidates = state.update_high(temp_c, zulu or '????Z')

            marker = ' ⬆️  NEW HIGH' if changed else ''
            logger.info(f"   🌡️  {temp_c}°C → OMO [{lo_prob}-{hi_prob}] | "
                       f"H≥{state.probable_high}°F{marker}")

            # Check bracket kills
            trade_str = ''
            pre_trades = len(state.trades)
            check_and_trade(brackets)
            if len(state.trades) > pre_trades:
                trade_str = state.trades[-1]['action'] + ':' + state.trades[-1]['ticker']

            log_row(ts, zulu, temp_c, candidates, changed, trade_str, '')

            # Sleep until next call
            # Call takes ~50s, so only need ~10s more to hit 60s interval
            time.sleep(max(5, POLL_INTERVAL - 50))

    except KeyboardInterrupt:
        pass

    # ─── Summary ──────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"📊 KLAS PHONE SNIPER — SESSION SUMMARY")
    print(f"{'='*60}")
    print(f"  Calls:          {state.call_count}")
    print(f"  Parse failures: {state.parse_failures}")
    print(f"  Total cost:     ${state.total_cost:.2f}")
    print(f"  Probable HIGH:  ≥{state.probable_high}°F (conservative)")
    if state.probable_high_max:
        print(f"  Probable HIGH:  ≤{state.probable_high_max}°F (optimistic)")

    if state.trades:
        print(f"\n  TRADES ({len(state.trades)}):")
        for t in state.trades:
            dry = " [DRY RUN]" if t['dry_run'] else " ✅"
            print(f"    {t['time']} | {t['action']} {t['subtitle']} @ {t['price']}¢{dry}")
    else:
        print(f"\n  No trades executed")

    all_brackets = brackets
    print(f"\n  Bracket status:")
    for b in all_brackets:
        icon = {'open': '⬜', 'dead': '🔴', 'locked': '🟢'}.get(b.status, '❓')
        traded = ' (TRADED)' if b.traded else ''
        print(f"    {icon} {b.subtitle}: {b.status}{traded}")

    print(f"\n  Log: {LOG_FILE}")
    print(f"{'='*60}")


if __name__ == '__main__':
    run()
