import os
from twilio.rest import Client
from dotenv import load_dotenv

# This looks for a .env file in the same directory and loads it
load_dotenv()

# Pull the credentials
account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
from_num = os.getenv('TWILIO_PHONE_NUMBER')

# DEBUG: Prove it worked
if not account_sid:
    print("❌ ERROR: TWILIO_ACCOUNT_SID is still None. Check your .env file location.")
    exit(1)
else:
    print(f"✅ Loaded SID: {account_sid[:10]}...")

client = Client(account_sid, auth_token)

try:
    call = client.calls.create(
        to='+19176799405',  # Your phone
        from_=from_num,
        url='https://handler.twilio.com/twiml/EH5d2f909a60a6cd5ca7c007d591ad9f5b'
    )
    print(f"🚀 Call initiated! SID: {call.sid}")
except Exception as e:
    print(f"🔥 Twilio Error: {e}")
