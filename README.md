# WX Sniper 🌡️

Automated temperature market trading bot for Kalshi. Uses 6-hour max/min temperature data from METAR reports to execute trades.

## Strategy

At synoptic times (00Z, 06Z, 12Z, 18Z), ASOS stations report 6-hour max/min temperatures in METAR remarks. When these values set new daily highs/lows, the bot buys the corresponding Kalshi bracket if priced below 90¢.

## Data Flow

```
METAR drops at :53 past synoptic hour
         ↓
   Parse 6-hour groups (1xxxx = max, 2xxxx = min)
         ↓
   Compare to tracked daily high/low
         ↓
   If new extreme → find Kalshi bracket → execute trade
```

## Running the Bot

### Quick Start (Dry Run)

```bash
# Install dependencies
pip install -r requirements.txt

# Run server (dry run mode - no real trades)
python main.py server --port 5000
```

Then open http://localhost:5000 to:
- Paste METARs manually from your phone
- View current state

### Live Trading

```bash
python main.py server --port 5000 --live
```

⚠️ **WARNING**: `--live` enables real trades on Kalshi!

## Deployment on DigitalOcean App Platform

1. Push code to GitHub
2. Connect repo to App Platform
3. Set environment variables:
   - `TWILIO_ACCOUNT_SID` - Your Twilio Account SID (AC...)
   - `TWILIO_AUTH_TOKEN` - Your Twilio Auth Token
4. Deploy!

Your webhook URL will be: `https://your-app.ondigitalocean.app/sms/webhook`

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID (AC...) | For SMS |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token | For SMS |
| `KALSHI_API_KEY_ID` | Kalshi API Key ID | Yes |
| `KALSHI_PRIVATE_KEY` | Kalshi RSA Private Key | Yes |

## Files

- `main.py` - Entry point, CLI interface
- `config.py` - Configuration (uses env vars for secrets)
- `metar_parser.py` - Parses METAR 6-hour groups
- `aviation_weather.py` - Polls aviationweather.gov API
- `kalshi_client.py` - Kalshi API wrapper
- `twilio_handler.py` - SMS webhook handling
- `state_manager.py` - Tracks daily highs/lows
- `trader.py` - Core trading logic

## SMS Workflow

Since Twilio can't send TO short codes (358-782):

1. Text `METAR KSFO` to 358-782 from your personal phone
2. Forward Leidos reply to your Twilio number
3. Webhook receives it → bot processes → trades

Or just use the web UI to paste METARs directly!

## Commands

```bash
# Run hybrid server (webhook + polling + UI)
python main.py server --port 5000

# Test with sample METAR
python main.py test --metar "KSFO 281853Z ..."

# Test aviationweather.gov polling
python main.py poll

# Discover Kalshi ticker formats
python main.py discover

# View current state
python main.py status
```

## License

MIT
