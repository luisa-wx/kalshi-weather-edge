#!/usr/bin/env python3
"""
test_call_klas.py - Call KLAS ASOS, record, transcribe, parse temp
==================================================================

Step 1: Twilio calls (702) 582-5334 (KLAS ASOS)
Step 2: Records for 35 seconds
Step 3: Downloads the WAV
Step 4: Sends to Deepgram for transcription
Step 5: Parses "temperature XX" from transcript

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
import json
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
LISTEN_SECONDS = 35  # ASOS recording is ~25-30s, give it 35 to be safe

# ==============================================================
# STEP 1: Call KLAS ASOS and record
# ==============================================================

from twilio.rest import Client

client = Client(account_sid, auth_token)

print(f"\n📞 Calling KLAS ASOS at {KLAS_ASOS_NUMBER}...")
print(f"   Will record for {LISTEN_SECONDS}s then hang up\n")

try:
    # TwiML tells Twilio: pause 1s (let ASOS pick up), then record
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

# Call lifecycle: queued → ringing → in-progress → completed
# Total time: ~5s ring + 1s pause + 35s record + ~3s hangup ≈ 44s
WAIT_TIME = LISTEN_SECONDS + 20
print(f"\n⏳ Waiting {WAIT_TIME}s for call to complete...")

for i in range(WAIT_TIME):
    time.sleep(1)
    if (i + 1) % 10 == 0:
        # Check call status
        call = client.calls(call_sid).fetch()
        print(f"   {i+1}s elapsed - Status: {call.status}")
        if call.status == 'completed':
            print("   ✅ Call completed!")
            break
        elif call.status in ('failed', 'busy', 'no-answer', 'canceled'):
            print(f"   ❌ Call ended with status: {call.status}")
            sys.exit(1)
else:
    # Final check
    call = client.calls(call_sid).fetch()
    print(f"   Final status: {call.status}")

# ==============================================================
# STEP 3: Download the recording
# ==============================================================

print(f"\n📼 Fetching recordings for call {call_sid}...")

# Twilio sometimes needs a moment to process the recording
for attempt in range(5):
    recordings = client.recordings.list(call_sid=call_sid, limit=5)
    if recordings:
        break
    print(f"   No recordings yet, waiting 5s... (attempt {attempt+1}/5)")
    time.sleep(5)

if not recordings:
    print("❌ No recordings found! The call may not have connected.")
    print("   Check your Twilio dashboard for call details.")
    sys.exit(1)

recording = recordings[0]
print(f"   Recording SID: {recording.sid}")
print(f"   Duration: {recording.duration}s")

# Download WAV
recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording.sid}.wav"
print(f"   Downloading from: {recording_url}")

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
    "numerals": "true",      # "five eight" → "58"
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
print(f"📝 TRANSCRIPT:")
print(f"{'='*60}")
print(transcript)
print(f"{'='*60}")

# ==============================================================
# STEP 5: Parse temperature
# ==============================================================

import re

def parse_temp(text):
    """Extract temperature from ASOS transcript."""
    text = text.lower()
    
    # Pattern: "temperature XX" (Deepgram numerals mode)
    match = re.search(r'temperature\s+(minus\s+|negative\s+)?(\d{1,3})', text)
    if match:
        val = int(match.group(2))
        if match.group(1):
            val = -val
        return val
    
    return None

def parse_dewpoint(text):
    """Extract dew point from transcript."""
    text = text.lower()
    match = re.search(r'dew\s*point\s+(minus\s+|negative\s+)?(\d{1,3})', text)
    if match:
        val = int(match.group(2))
        if match.group(1):
            val = -val
        return val
    return None

def parse_zulu(text):
    """Extract observation time."""
    text = text.lower()
    match = re.search(r'(\d{4})\s*zulu', text)
    if match:
        return match.group(1) + 'Z'
    return None

temp = parse_temp(transcript)
dp = parse_dewpoint(transcript)
zulu = parse_zulu(transcript)

print(f"\n🌡️  PARSED RESULTS:")
print(f"   Temperature: {temp}°F" if temp is not None else "   Temperature: ❌ NOT FOUND")
print(f"   Dew Point:   {dp}°F" if dp is not None else "   Dew Point:   ❌ NOT FOUND")
print(f"   Zulu Time:   {zulu}" if zulu else "   Zulu Time:   ❌ NOT FOUND")

if temp is not None:
    print(f"\n✅ SUCCESS — KLAS OMO is {temp}°F")
    print(f"   This is the raw 1-minute observation. No rounding ambiguity.")
else:
    print(f"\n⚠️  Could not parse temperature. Check transcript above.")
    print(f"   The ASOS voice may use a format Deepgram transcribed differently.")
    print(f"   Listen to the WAV at: {wav_path}")

# ==============================================================
# STEP 6: Clean up recording (optional, saves Twilio storage $)
# ==============================================================

try:
    client.recordings(recording.sid).delete()
    print(f"\n🗑️  Deleted Twilio recording {recording.sid}")
except Exception as e:
    print(f"\n⚠️  Could not delete recording: {e}")

print(f"\n📁 WAV file kept at: {wav_path}")
print(f"   Play it: aplay {wav_path}")
print(f"   Or: ffplay -nodisp {wav_path}")
