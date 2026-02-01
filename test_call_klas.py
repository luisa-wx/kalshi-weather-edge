#!/usr/bin/env python3
"""
test_call_klas.py - Call KLAS ASOS, record, transcribe, parse temp
==================================================================

Step 1: Twilio calls (702) 582-5334 (KLAS ASOS)
Step 2: Records for 35 seconds
Step 3: Downloads the WAV
Step 4: Sends to Deepgram for transcription
Step 5: Parses temperature from transcript

WHAT WE LEARNED FROM THE FIRST CALL:
  The ASOS phone voice reports temperature in WHOLE DEGREES CELSIUS,
  not Fahrenheit. Digits are space-separated. Format:

  "Harry Reid International Airport. Automated weather observation,
   2 2 0 niner Zulu. Wind, calm. Visibility, 1 0. Sky condition, 
   few 2 5000. Temperature, 2 1 Celsius. Dew point, minus 0 4 Celsius.
   Altimeter, 3 0 1 3. Remarks, density altitude, 3,200."

  The recording LOOPS, so we may catch a partial + full cycle.

Run: python3 test_call_klas.py

Requires .env with:
  TWILIO_ACCOUNT_SID=...
  TWILIO_AUTH_TOKEN=...
  TWILIO_PHONE_NUMBER=...
  DEEPGRAM_API_KEY=...
"""

import os
import sys
import time
import math
import json
import re
import requests
from dotenv import load_dotenv

load_dotenv()

# ---- Credentials ----
account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
from_num = os.getenv('TWILIO_PHONE_NUMBER')
deepgram_key = os.getenv('DEEPGRAM_API_KEY')

# ---- Validate ----
missing = []
if not account_sid: missing.append('TWILIO_ACCOUNT_SID')
if not auth_token: missing.append('TWILIO_AUTH_TOKEN')
if not from_num: missing.append('TWILIO_PHONE_NUMBER')
if not deepgram_key: missing.append('DEEPGRAM_API_KEY')

if missing:
    print(f"❌ Missing env vars: {', '.join(missing)}")
    print("   Add them to your .env file")
    sys.exit(1)

print(f"✅ Twilio SID: {account_sid[:10]}...")
print(f"✅ Deepgram key: {deepgram_key[:10]}...")

# ---- Config ----
KLAS_ASOS_NUMBER = '+17025825334'
LISTEN_SECONDS = 35

# ==============================================================
# PARSING FUNCTIONS (tuned to actual ASOS phone format)
# ==============================================================

# Aviation phonetic digits the ASOS voice uses
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
    """
    Convert space-separated digits to integer.
    ASOS says: "2 1" -> 21, "minus 0 4" -> -4, "1 0 2" -> 102
    """
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
    """NWS rounding: round half up (asymmetric)."""
    return math.floor(val + 0.5)


def c_to_f_nws(temp_c: int) -> int:
    """Convert whole Celsius to Fahrenheit with NWS rounding."""
    return nws_round(temp_c * 9.0/5.0 + 32)


def parse_temperature(transcript: str):
    """
    Extract temperature from ASOS transcript.
    Format: "Temperature, 2 1 Celsius" or "Temperature, minus 0 4 Celsius"
    Returns: (temp_c: int, temp_f: int) or (None, None)
    """
    text = transcript.lower()
    
    # Primary: "temperature" ... digits ... "celsius"
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
    
    # Fallback: just "temperature" followed by digits
    temp_idx = text.find('temperature')
    if temp_idx >= 0:
        after = text[temp_idx + len('temperature'):temp_idx + len('temperature') + 30]
        after = after.replace(',', ' ').strip()
        val = spaced_digits_to_int(after)
        if val is not None and -60 <= val <= 60:
            return val, c_to_f_nws(val)
    
    return None, None


def parse_dewpoint(transcript: str):
    """
    Extract dew point. Format: "Dew point, minus 0 4 Celsius"
    Returns: (dp_c: int, dp_f: int) or (None, None)
    """
    text = transcript.lower()
    
    match = re.search(
        r'dew\s*point[,\s]+(minus\s+|negative\s+)?([\d\s]+?)(?:\s*celsius|\s*\.|$)',
        text
    )
    if match:
        sign = match.group(1)
        digit_str = match.group(2).strip()
        val = spaced_digits_to_int(("minus " if sign else "") + digit_str)
        if val is not None and -80 <= val <= 50:
            return val, c_to_f_nws(val)
    
    dp_idx = text.find('dew point')
    if dp_idx < 0:
        dp_idx = text.find('dewpoint')
    if dp_idx >= 0:
        keyword_len = len('dew point') if 'dew point' in text else len('dewpoint')
        after = text[dp_idx + keyword_len:dp_idx + keyword_len + 30]
        after = after.replace(',', ' ').strip()
        val = spaced_digits_to_int(after)
        if val is not None and -80 <= val <= 50:
            return val, c_to_f_nws(val)
    
    return None, None


def parse_zulu_time(transcript: str):
    """
    Extract observation time. Format: "2 2 0 niner Zulu" -> "2209Z"
    """
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


# ==============================================================
# SELF-TEST (runs against known transcript before making call)
# ==============================================================

print(f"\n🧪 PARSER SELF-TEST (against known ASOS transcript):")
print(f"{'='*60}")

known = ("Sky condition, pew, 2 5000. Temperature, 2 1 Celsius. "
         "Dew point, minus 0 4 Celsius. Altimeter, 3 0 1 3. "
         "Remarks, density altitude, 3,200. "
         "Harry Reid International Airport. "
         "Automated weather observation, 2 2 0 niner, Zulu. "
         "Wind, calm. Visibility, 1 0. Sky condition, 2 2 5000.")

tc, tf = parse_temperature(known)
dc, df = parse_dewpoint(known)
z = parse_zulu_time(known)

tests = [
    ("Temperature C", tc, 21),
    ("Temperature F", tf, 70),   # 21C * 1.8 + 32 = 69.8 -> 70F
    ("Dew Point C",   dc, -4),
    ("Dew Point F",   df, 25),   # -4C * 1.8 + 32 = 24.8 -> 25F
    ("Zulu Time",     z, "2209Z"),
]

all_pass = True
for name, got, expected in tests:
    status = "✅" if got == expected else "❌"
    if got != expected:
        all_pass = False
    print(f"   {status} {name}: got={got}, expected={expected}")

if all_pass:
    print(f"   ✅ ALL TESTS PASS — calling KLAS...\n")
else:
    print(f"   ❌ SOME TESTS FAILED — fix parser before calling")
    sys.exit(1)


# ==============================================================
# STEP 1: Call KLAS ASOS and record
# ==============================================================

from twilio.rest import Client

client = Client(account_sid, auth_token)

print(f"📞 Calling KLAS ASOS at {KLAS_ASOS_NUMBER}...")
print(f"   Will record for {LISTEN_SECONDS}s then hang up\n")

try:
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="1"/>
    <Record maxLength="{LISTEN_SECONDS}" playBeep="false" trim="do-not-trim"/>
    <Hangup/>
</Response>'''

    call = client.calls.create(
        to=KLAS_ASOS_NUMBER,
        from_=from_num,
        twiml=twiml,
        record=True,
    )
    call_sid = call.sid
    print(f"🚀 Call placed! SID: {call_sid}")

except Exception as e:
    print(f"🔥 Twilio call failed: {e}")
    sys.exit(1)

# ==============================================================
# STEP 2: Wait for call to complete
# ==============================================================

WAIT_TIME = LISTEN_SECONDS + 20
print(f"\n⏳ Waiting {WAIT_TIME}s for call to complete...")

for i in range(WAIT_TIME):
    time.sleep(1)
    if (i + 1) % 10 == 0:
        call = client.calls(call_sid).fetch()
        print(f"   {i+1}s elapsed - Status: {call.status}")
        if call.status == 'completed':
            print("   ✅ Call completed!")
            break
        elif call.status in ('failed', 'busy', 'no-answer', 'canceled'):
            print(f"   ❌ Call ended with status: {call.status}")
            sys.exit(1)
else:
    call = client.calls(call_sid).fetch()
    print(f"   Final status: {call.status}")

# ==============================================================
# STEP 3: Download the recording
# ==============================================================

print(f"\n📼 Fetching recordings for call {call_sid}...")

for attempt in range(5):
    recordings = client.recordings.list(call_sid=call_sid, limit=5)
    if recordings:
        break
    print(f"   No recordings yet, waiting 5s... (attempt {attempt+1}/5)")
    time.sleep(5)

if not recordings:
    print("❌ No recordings found!")
    sys.exit(1)

recording = recordings[0]
print(f"   Recording SID: {recording.sid}")
print(f"   Duration: {recording.duration}s")

recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording.sid}.wav"
print(f"   Downloading...")

wav_resp = requests.get(recording_url, auth=(account_sid, auth_token), timeout=30)
wav_resp.raise_for_status()

wav_path = f"/tmp/klas_asos_{recording.sid}.wav"
with open(wav_path, 'wb') as f:
    f.write(wav_resp.content)

print(f"   ✅ Saved {len(wav_resp.content)} bytes to {wav_path}")

# ==============================================================
# STEP 4: Transcribe with Deepgram
# ==============================================================

print(f"\n🎤 Sending to Deepgram for transcription...")

dg_url = "https://api.deepgram.com/v1/listen"
dg_params = {
    "model": "nova-2",
    "language": "en-US",
    "punctuate": "true",
    "numerals": "true",
    "smart_format": "true",
}

dg_headers = {
    "Authorization": f"Token {deepgram_key}",
    "Content-Type": "audio/wav",
}

dg_resp = requests.post(
    dg_url,
    params=dg_params,
    headers=dg_headers,
    data=wav_resp.content,
    timeout=30,
)

if dg_resp.status_code != 200:
    print(f"❌ Deepgram error {dg_resp.status_code}: {dg_resp.text[:500]}")
    sys.exit(1)

dg_data = dg_resp.json()
transcript = dg_data['results']['channels'][0]['alternatives'][0]['transcript']

print(f"\n{'='*60}")
print(f"📝 FULL TRANSCRIPT:")
print(f"{'='*60}")
print(transcript)
print(f"{'='*60}")

# ==============================================================
# STEP 5: Parse all fields
# ==============================================================

temp_c, temp_f = parse_temperature(transcript)
dp_c, dp_f = parse_dewpoint(transcript)
zulu = parse_zulu_time(transcript)

print(f"\n🌡️  PARSED RESULTS:")
print(f"{'='*60}")

if temp_c is not None:
    print(f"   Temperature: {temp_c}°C → {temp_f}°F (NWS rounded)")
else:
    print(f"   Temperature: ❌ NOT FOUND")

if dp_c is not None:
    print(f"   Dew Point:   {dp_c}°C → {dp_f}°F (NWS rounded)")
else:
    print(f"   Dew Point:   ❌ NOT FOUND")

if zulu:
    print(f"   Obs Time:    {zulu}")
else:
    print(f"   Obs Time:    ❌ NOT FOUND")

# ==============================================================
# STEP 6: Rounding analysis
# ==============================================================

if temp_c is not None:
    print(f"\n📊 ROUNDING ANALYSIS:")
    print(f"{'='*60}")
    
    # What range of OMO (whole F) values map to this C reading?
    omo_candidates = []
    for omo_f in range(temp_f - 3, temp_f + 4):
        exact_c = (omo_f - 32) * 5.0 / 9.0
        rounded_c = nws_round(exact_c)
        if rounded_c == temp_c:
            omo_candidates.append(omo_f)
    
    print(f"   Phone says: {temp_c}°C")
    print(f"   NWS conversion: {temp_c}°C × 1.8 + 32 = {temp_c * 1.8 + 32:.1f}°F → {temp_f}°F")
    print(f"   Possible OMO values that round to {temp_c}°C: {omo_candidates}")
    print(f"   Ambiguity window: {min(omo_candidates)}–{max(omo_candidates)}°F")
    
    # Compare to 5-min timeseries display
    print(f"\n   5-min timeseries would display these OMOs as:")
    for omo in omo_candidates:
        exact_c = (omo - 32) * 5.0 / 9.0
        rounded_c = nws_round(exact_c)
        back_to_f = rounded_c * 1.8 + 32
        display_f = nws_round(back_to_f)
        print(f"     OMO {omo}°F → {exact_c:.2f}°C → {rounded_c}°C → {back_to_f:.1f}°F → display {display_f}°F")

    print(f"\n✅ KLAS phone observation: {temp_c}°C = {temp_f}°F")
    print(f"   OMO is one of: {omo_candidates}")
else:
    print(f"\n⚠️  Could not parse temperature. Check transcript above.")
    print(f"   Listen to: {wav_path}")

# ==============================================================
# STEP 7: Clean up
# ==============================================================

try:
    client.recordings(recording.sid).delete()
    print(f"\n🗑️  Deleted Twilio recording")
except Exception as e:
    print(f"\n⚠️  Could not delete recording: {e}")

print(f"📁 WAV kept at: {wav_path}")
