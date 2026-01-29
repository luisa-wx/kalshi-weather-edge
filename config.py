"""
WX Sniper Configuration
"""
import os

# Kalshi API Configuration
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "648527bb-4c6a-492f-822e-c2662bb7cd9d")
KALSHI_PRIVATE_KEY = os.environ.get("KALSHI_PRIVATE_KEY", """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEAzxT/ItkSLTWF3PY2JGfWVIRW90kgvstrFgatiEoM9gytnOzF
Cd+TP0/NI9s9rWPq8xfKAOKMVXoCfgF9MLXSIr7B7DXdwTQMHEh8WQM+HNGFHOBi
0QMHkVBUD4we5QfurMtpKLnDrgrw3JbhBHK4+fcYJy16tRliv/wZVrR3VwASM+zz
q9CGjvwKPxQFe9rNoO1BqX4ubNHqQUzK0FK4qBp2iGyXZ7KZ3ESZ68aweL5DDX2+
BxLUCQqPK8BHI48reptmv2QxIeN2Rcga+zTwPQcOPFYOVS8UF1FnS7gRigTwyFQq
xpJu2E0DZYfG7OxiLark6Y9FzVyTCeJvHgTq9wIDAQABAoIBAA7dnQCCwLv0xFVR
FMtBlltJh3ddDTgPy5zbLIiaIvAO3O+6TN12lVAi3xZx3gLyfKKoKdzXu+dESPrZ
Xy6oqXB2bTQTHIY1bEvOJaRmMoadwTE00niFVSe+B9V4jkc2wzrYUgZAalG2K1r1
Nytl6O2DgCe1K207aKFg7ENof8tY7tmuC8h6Z+5b53cKCKUGNTRuUs1bPZnqsA53
u83LkkNs/hz8empsuU/H8iVH8bQo8xihFtTBxJ+tYszMoGH7ssVgSNDTmbAAbwaq
+cESegxwcNh8Ht1dpK2UlPPBZe2X/TUAFLystLm/K2ays0Cq7TuuB4Xo4/2pf2XT
X4wuXdECgYEA2b6SmhsGEvGLCH26mocyMeDicr91A8KyuRprHJrtRG17FCCAExtE
HtFGBMET2AVymZ42RRuys8E1EPeV7Fomx82xwgiuq2Kzh19bOZe6CaGA8qE5+ugO
Hs89cS8h5o5fMO6H4hByQZRfqMBS+FIfKTnW9O13YnlHcRb+yHvCgV0CgYEA83bd
MMNG2y2ApsKJksP3UJ++9eRf2b4Fi9oGKM+afISqi9WXJ9FuGI/zRL+kpO7HqFVC
Wbr8p5WJZWrKxoeZP5hJZtbBI5sDcGsM6EdH8c2hdm1QIZDL9gNWwLsz4IbhIEFC
fFPPyRUjBDTtKiCT8rBlAy2WbJpp0NYLTGc+NGMCgYBWpzmubGy5YzjCU07Mqlr2
cJmNstW9fmEjuvi/dIRSBAPEGb7+W457eSsVP0VHZbuamNTeIcy3Ln+Q1gbq/WGL
iDdikZP5jpkFmZQzUkduB8DKThFF4c2kwzKfdXNXTndhgLvA4myl3odHH+qk+gF+
pY7+//XP0ZX10oHohR/93QKBgQCkBGTZMAUxLUNplM9Hv5uChkwIrbThJQHpiJTz
s4CY+GtIzzkIyy+HfprdqtoJfw+k2ONdPfpuD/DDESHQg5N7Y2W30V/GU+0KNCQ6
66KNRQHMnbIJGto9P1yXdMZrMZLCvxRCW9g02HeBowJPiikBq1IxxOl8+r3kwf5U
l40xjwKBgQCZtaJ10eqiEUkje+ciCzJ7TqfNf+p/buOf/0CWj+4f7Wplx/KYYFbe
ZWIJbg31bgc0Aajr70hWR8f+rFJZbjdsoAdM6W77ZsbQCYeBReDOVwVxf6Yp/tn+
ous6X61e//xt44m08c0g3tac+38Trs6+IN2mABpT/2n3rw97yL4x+w==
-----END RSA PRIVATE KEY-----""")

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")  # AC... from dashboard
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "+18334329646")  # Your toll-free number
TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "MG3c298feb725772f0d6de6f89074ee898")
LEIDOS_PHONE_NUMBER = "358782"  # Short code - can receive from, can't send to

# Station Configuration
# Maps ICAO code -> Kalshi ticker base
# From your spreadsheet: https://docs.google.com/spreadsheets/d/...
STATIONS = {
    "KNYC": {
        "kalshi_high_ticker": "KXHIGHNY",
        "kalshi_low_ticker": "KXLOWTNYC",
        "timezone": "America/New_York",
        "name": "NYC"
    },
    "KPHL": {
        "kalshi_high_ticker": "KXHIGHPHL",
        "kalshi_low_ticker": "KXLOWTPHL",
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
        "kalshi_low_ticker": None,  # N per your spreadsheet
        "timezone": "America/Los_Angeles",
        "name": "San Francisco"
    },
    "KSEA": {
        "kalshi_high_ticker": "KXHIGHTSEA",
        "kalshi_low_ticker": None,  # N per your spreadsheet
        "timezone": "America/Los_Angeles",
        "name": "Seattle"
    },
    "KDCA": {
        "kalshi_high_ticker": "KXHIGHTDC",
        "kalshi_low_ticker": None,  # N per your spreadsheet
        "timezone": "America/New_York",
        "name": "Washington DC"
    },
    "KMSY": {
        "kalshi_high_ticker": "KXHIGHTNOLA",
        "kalshi_low_ticker": None,  # N per your spreadsheet
        "timezone": "America/Chicago",
        "name": "New Orleans"
    },
    "KLAS": {
        "kalshi_high_ticker": "KXHIGHTLV",
        "kalshi_low_ticker": None,  # N per your spreadsheet
        "timezone": "America/Los_Angeles",
        "name": "Las Vegas"
    },
    "KDEN": {
        "kalshi_high_ticker": "KXHIGHDEN",
        "kalshi_low_ticker": None,  # Spreadsheet shows Y but no ticker listed
        "timezone": "America/Denver",
        "name": "Denver"
    }
}

# Trading Configuration
MAX_TRADE_AMOUNT_CENTS = 200  # $2 max per trade
MIN_EDGE_THRESHOLD = 0.07  # Only trade if bracket price < 93 cents (7+ cent edge)
MAX_BRACKET_PRICE_CENTS = 93  # Don't buy brackets priced above 93 cents

# Synoptic times (UTC hours when 6-hour groups are reported)
# METARs with 6-hour groups drop at ~:53 past these hours
SYNOPTIC_HOURS_UTC = [23, 5, 11, 17]  # Actually 2353Z, 0553Z, 1153Z, 1753Z

# Detailed breakdown:
# 
# | Synoptic Period | METAR Time | Covers       |
# |-----------------|------------|--------------|
# | 0000Z           | ~2353Z     | 1800Z-0000Z  |
# | 0600Z           | ~0553Z     | 0000Z-0600Z  |
# | 1200Z           | ~1153Z     | 0600Z-1200Z  |
# | 1800Z           | ~1753Z     | 1200Z-1800Z  |
#
# ============================================================
# WINTER (Standard Time, Nov - Mar)
# ============================================================
# EST (UTC-5):
#   2353Z = 6:53 PM EST
#   0553Z = 12:53 AM EST  
#   1153Z = 6:53 AM EST
#   1753Z = 12:53 PM EST
#
# CST (UTC-6):
#   2353Z = 5:53 PM CST
#   0553Z = 11:53 PM CST (previous day)
#   1153Z = 5:53 AM CST
#   1753Z = 11:53 AM CST
#
# MST (UTC-7):
#   2353Z = 4:53 PM MST
#   0553Z = 10:53 PM MST (previous day)
#   1153Z = 4:53 AM MST
#   1753Z = 10:53 AM MST
#
# PST (UTC-8):
#   2353Z = 3:53 PM PST
#   0553Z = 9:53 PM PST (previous day)
#   1153Z = 3:53 AM PST
#   1753Z = 9:53 AM PST
#
# ============================================================
# SUMMER (Daylight Time, Mar - Nov)
# ============================================================
# EDT (UTC-4):
#   2353Z = 7:53 PM EDT
#   0553Z = 1:53 AM EDT
#   1153Z = 7:53 AM EDT
#   1753Z = 1:53 PM EDT
#
# CDT (UTC-5):
#   2353Z = 6:53 PM CDT
#   0553Z = 12:53 AM CDT
#   1153Z = 6:53 AM CDT
#   1753Z = 12:53 PM CDT
#
# MDT (UTC-6):
#   2353Z = 5:53 PM MDT
#   0553Z = 11:53 PM MDT (previous day)
#   1153Z = 5:53 AM MDT
#   1753Z = 11:53 AM MDT
#
# PDT (UTC-7):
#   2353Z = 4:53 PM PDT
#   0553Z = 10:53 PM PDT (previous day)
#   1153Z = 4:53 AM PDT
#   1753Z = 10:53 AM PDT
#
# DST CHANGES 2026:
#   Spring forward: March 8, 2026 at 2:00 AM local
#   Fall back: November 1, 2026 at 2:00 AM local

# SMS polling configuration (for manual forward approach)
SMS_POLL_START_SECONDS_BEFORE = 90  # Start polling 1.5 min before :53
SMS_POLL_INTERVAL_SECONDS = 10  # Poll every 10 seconds
SMS_POLL_MAX_DURATION_SECONDS = 300  # Stop after 5 minutes if no response

# AviationWeather.gov API polling (automated fallback)
AVIATIONWEATHER_API_URL = "https://aviationweather.gov/api/data/metar"
AVIATIONWEATHER_USER_AGENT = "WXSniper/1.0 (weather trading bot)"

# Adaptive polling rates
POLL_INTERVAL_NORMAL_SECONDS = 60      # Once per minute normally
POLL_INTERVAL_HOT_SECONDS = 5          # Every 5 seconds during hot window
HOT_WINDOW_START_MINUTE = 51           # Start rapid polling at :51
HOT_WINDOW_END_MINUTE = 58             # End rapid polling at :58
