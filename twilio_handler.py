"""
Twilio SMS Handler for Leidos Flight Service METAR requests

This module handles:
1. Sending METAR requests to Leidos (358-782)
2. Receiving responses via webhook
3. Parsing and routing responses to the trading logic
"""

import os
import re
from datetime import datetime, timezone
from typing import Optional, Callable
from flask import Flask, request, Response
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from config import (
    TWILIO_ACCOUNT_SID, 
    TWILIO_AUTH_TOKEN, 
    TWILIO_PHONE_NUMBER,
    LEIDOS_PHONE_NUMBER,
    STATIONS
)
from metar_parser import parse_metar, MetarTemps, format_metar_summary


# Global callback for when we receive a METAR
_metar_callback: Optional[Callable[[str, MetarTemps], None]] = None


def set_metar_callback(callback: Callable[[str, MetarTemps], None]):
    """Set the callback function for when METARs are received"""
    global _metar_callback
    _metar_callback = callback


class TwilioSMSClient:
    """Client for sending SMS via Twilio"""
    
    def __init__(self):
        self.account_sid = TWILIO_ACCOUNT_SID
        self.auth_token = TWILIO_AUTH_TOKEN
        self.from_number = TWILIO_PHONE_NUMBER
        
        if self.account_sid and self.auth_token:
            self.client = Client(self.account_sid, self.auth_token)
        else:
            self.client = None
            print("[TWILIO] Warning: Credentials not configured")
    
    def send_metar_request(self, station: str) -> Optional[str]:
        """
        Send a METAR request to Leidos Flight Service
        
        Args:
            station: ICAO station code (e.g., "KSFO", "KLAS")
            
        Returns:
            Message SID if successful, None otherwise
        """
        if not self.client:
            print("[TWILIO] Cannot send - client not configured")
            return None
        
        message_body = f"METAR {station}"
        
        try:
            message = self.client.messages.create(
                body=message_body,
                from_=self.from_number,
                to=LEIDOS_PHONE_NUMBER
            )
            print(f"[TWILIO] Sent '{message_body}' -> {LEIDOS_PHONE_NUMBER} (SID: {message.sid})")
            return message.sid
            
        except Exception as e:
            print(f"[TWILIO] Error sending message: {e}")
            return None
    
    def request_all_stations(self) -> dict:
        """Request METARs for all configured stations"""
        results = {}
        for station in STATIONS.keys():
            sid = self.send_metar_request(station)
            results[station] = sid
        return results


def extract_metar_from_sms(sms_body: str) -> Optional[str]:
    """
    Extract METAR text from Leidos SMS response
    
    Leidos responses typically contain the raw METAR string.
    We need to extract just the METAR portion.
    """
    # Clean up the message
    text = sms_body.strip()
    
    # Look for METAR pattern: starts with K followed by 3 letters (US stations)
    # METAR format: STATION DDHHMMZ ...
    metar_pattern = re.compile(
        r'(K[A-Z]{3}\s+\d{6}Z\s+.+?)(?:\n|$)',
        re.DOTALL
    )
    
    match = metar_pattern.search(text)
    if match:
        return match.group(1).strip()
    
    # If no match, the whole message might be the METAR
    if text.startswith('K') and 'Z' in text[:15]:
        return text
    
    return None


def create_webhook_app() -> Flask:
    """
    Create Flask app to receive Twilio webhooks
    
    Configure your Twilio number's webhook URL to point to:
    https://your-server.com/sms/webhook
    """
    app = Flask(__name__)
    
    @app.route('/sms/webhook', methods=['POST'])
    def sms_webhook():
        """Handle incoming SMS from Twilio"""
        
        # Get message details
        from_number = request.form.get('From', '')
        body = request.form.get('Body', '')
        timestamp = datetime.now(timezone.utc)
        
        print(f"\n[SMS RECEIVED] {timestamp.isoformat()}")
        print(f"  From: {from_number}")
        print(f"  Body: {body[:100]}{'...' if len(body) > 100 else ''}")
        
        # Check if this is from Leidos
        # Note: The actual number format may vary
        if 'FLTSVC' in from_number or '358782' in from_number.replace('-', '').replace(' ', ''):
            is_leidos = True
        else:
            # Accept any response during testing
            is_leidos = True
            print(f"  [Note: From number doesn't match Leidos, processing anyway]")
        
        if is_leidos:
            # Extract and parse the METAR
            metar_text = extract_metar_from_sms(body)
            
            if metar_text:
                print(f"  [Extracted METAR]: {metar_text[:60]}...")
                parsed = parse_metar(metar_text)
                print(f"  [Parsed Station]: {parsed.station}")
                
                reply_parts = [f"✅ {parsed.station}"]
                
                if parsed.six_hour_max_c is not None:
                    print(f"  [6HR MAX]: {parsed.six_hour_max_f:.1f}°F -> {parsed.six_hour_max_f_rounded}°F")
                    reply_parts.append(f"MAX:{parsed.six_hour_max_f_rounded}°F")
                if parsed.six_hour_min_c is not None:
                    print(f"  [6HR MIN]: {parsed.six_hour_min_f:.1f}°F -> {parsed.six_hour_min_f_rounded}°F")
                    reply_parts.append(f"MIN:{parsed.six_hour_min_f_rounded}°F")
                
                if len(reply_parts) == 1:
                    reply_parts.append("(no 6hr groups)")
                
                # Trigger callback if set
                if _metar_callback:
                    try:
                        result = _metar_callback(metar_text, parsed)
                        if result:
                            reply_parts.append("🎯 TRADING")
                    except Exception as e:
                        print(f"  [Callback Error]: {e}")
                        reply_parts.append(f"⚠️ {str(e)[:20]}")
                
                reply_msg = " | ".join(reply_parts)
            else:
                print(f"  [Could not extract METAR from message]")
                reply_msg = "❌ Could not parse METAR"
        
        # Respond with confirmation
        resp = MessagingResponse()
        resp.message(reply_msg)
        return Response(str(resp), mimetype='application/xml')
    
    @app.route('/health', methods=['GET'])
    def health():
        """Health check endpoint"""
        return {'status': 'ok', 'time': datetime.now(timezone.utc).isoformat()}
    
    return app


# =========== Test/Demo ===========

if __name__ == "__main__":
    print("=" * 60)
    print("TWILIO SMS HANDLER TEST")
    print("=" * 60)
    
    # Test METAR extraction
    test_messages = [
        # Typical Leidos response format (hypothetical)
        "KSFO 281853Z 29012KT 10SM FEW020 SCT200 17/08 A3012 RMK AO2 SLP203 T01720083 10189 20156 58010",
        
        # With some preamble
        "METAR: KLAS 281853Z 25008KT 10SM CLR 21/M04 A3018 RMK AO2 SLP150 T02111044 10228 20167 58012",
        
        # Multi-line
        """Latest observation:
KDEN 281853Z 36009KT 10SM FEW120 M02/M15 A3045 RMK AO2 SLP320 T10221150 11005 21028
Valid as of 1853Z""",
    ]
    
    print("\n[Testing METAR extraction]")
    for msg in test_messages:
        print(f"\nInput: {msg[:50]}...")
        extracted = extract_metar_from_sms(msg)
        if extracted:
            print(f"Extracted: {extracted[:60]}...")
            parsed = parse_metar(extracted)
            print(f"Station: {parsed.station}")
            if parsed.six_hour_max_c:
                print(f"6HR Max: {parsed.six_hour_max_f:.1f}°F")
        else:
            print("Could not extract METAR")
    
    print("\n" + "=" * 60)
    print("TWILIO SETUP INSTRUCTIONS")
    print("=" * 60)
    print("""
To set up Twilio:

1. Sign up at https://www.twilio.com (free trial gives you $15 credit)

2. Get a phone number (~$1.15/month)
   - Go to Phone Numbers > Manage > Buy a number
   - Get any US number with SMS capability

3. Get your credentials:
   - Account SID: Found on dashboard
   - Auth Token: Found on dashboard (click to reveal)

4. Configure webhook:
   - Go to Phone Numbers > Manage > Active Numbers
   - Click your number
   - Under "Messaging", set webhook URL to:
     https://your-digitalocean-droplet.com/sms/webhook
   - Method: POST

5. Update config.py with your credentials:
   TWILIO_ACCOUNT_SID = "your_sid"
   TWILIO_AUTH_TOKEN = "your_token"
   TWILIO_PHONE_NUMBER = "+1234567890"  # Your Twilio number

6. Verify Leidos number format:
   - Try texting 358-782 manually first
   - Check what number the response comes from
   - Update LEIDOS_PHONE_NUMBER in config.py

Cost estimate for your usage:
- Phone number: $1.15/month
- Outbound SMS: ~$0.0079/message
- Inbound SMS: ~$0.0079/message
- 4 synoptic times × 2 stations × 2 messages = 16 messages/day
- Daily cost: ~$0.25
- Monthly cost: ~$8-9 total
    """)
