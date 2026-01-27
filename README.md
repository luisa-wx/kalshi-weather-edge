# Kalshi Weather Edge v4

Real-time METAR parser with Kalshi market integration and wethr.net forecast support. Finds arbitrage opportunities ("edges") in Kalshi weather prediction markets.

## The Edge Concept

**Key insight:** We can't know what bracket will WIN, but we CAN know what brackets have already LOST.

- **HIGH markets**: If observed high floor > bracket ceiling → bracket is DEAD
- **LOW markets**: If observed low ceiling < bracket floor → bracket is DEAD
- **Edge opportunity**: Dead bracket + active YES bid > 2¢ → BET NO for guaranteed profit

## What's New in v4

1. **Wethr.net Forecast Integration** 
   - Shows forecasted high/low alongside observed temps
   - Uses `wethr_high` mode with NWS logic (matches Kalshi settlement)
   - Helps understand market context

2. **Improved UI**
   - Cleaner bracket visualization
   - "LEADING" status for brackets currently in winning position
   - Better edge alerts with explanation of why bracket is dead
   - Responsive grid layout

3. **Better Edge Detection**
   - Only flags edges on tradeable markets (not settled/closed)
   - Shows BUY NO price alongside YES bid
   - Clear explanation of why each bracket is mathematically eliminated

## Quick Start

```bash
# Install dependencies
npm install

# Run (without forecast)
npm start

# Run with wethr.net forecast (recommended)
WETHR_API_KEY=your_key_here npm start
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PORT` | No | Server port (default: 3000) |
| `WETHR_API_KEY` | No | wethr.net API key for forecast data |

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/cities` | List all supported cities |
| `GET /api/city/:code` | Full data for one city (weather + markets) |
| `GET /api/summary` | Summary of all cities (good for scanning for edges) |
| `GET /api/forecast/:code` | Wethr.net forecast for a city (requires API key) |

## Supported Cities

| Code | City | Station | Has LOW Market |
|------|------|---------|----------------|
| NYC | New York (Central Park) | KNYC | ✓ |
| PHL | Philadelphia | KPHL | ✓ |
| CHI | Chicago (Midway) | KMDW | ✓ |
| LAX | Los Angeles | KLAX | ✓ |
| MIA | Miami | KMIA | ✓ |
| AUS | Austin | KAUS | ✓ |
| DEN | Denver | KDEN | ✓ |
| SFO | San Francisco | KSFO | |
| SEA | Seattle | KSEA | |
| DCA | Washington DC | KDCA | |
| MSY | New Orleans | KMSY | |
| LAS | Las Vegas | KLAS | |

## How Settlement Works

1. **Data Source**: NWS CLI (Climatological Report Daily)
2. **Time Zone**: Local Standard Time (no DST adjustment)
3. **Rounding**: Asymmetric half-up (C→F conversion)
4. **Authoritative Values**: 6-hour max/min from METAR RMK section

## Market Tickers

- **HIGH markets**: `KXHIGHNY`, `KXHIGHPHIL`, `KXHIGHCHI`, `KXHIGHLAX`, `KXHIGHMIA`, `KXHIGHAUS`, `KXHIGHDEN`, `KXHIGHTSFO`, `KXHIGHTSEA`, `KXHIGHTDC`, `KXHIGHTNOLA`, `KXHIGHTLV`
- **LOW markets**: `KXLOWTNYC`, `KXLOWTPHIL`, `KXLOWTCHI`, `KXLOWTLAX`, `KXLOWTMIA`, `KXLOWTAUS`, `KXLOWTDEN`

## Deployment (DigitalOcean App Platform)

1. Push to GitHub
2. Create new app in DigitalOcean App Platform
3. Connect your repo
4. Add environment variable: `WETHR_API_KEY`
5. Deploy!

## Known Limitations

1. **Latency**: Free NWS API lags 2-20 minutes behind sensors
2. **Rounding Ambiguity**: 5-minute readings have ±1°F uncertainty
3. **Not Financial Advice**: Always verify before betting

## Future Improvements (v5+)

- **TG-FTP METAR**: 30-90 second faster than web API
- **EMWIN ByteBlaster**: Real-time push for sub-minute latency
- **Historical backtesting**
- **Auto-alerts/notifications**

## License

MIT - Use at your own risk!
