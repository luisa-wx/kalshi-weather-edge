# Kalshi Weather Edge

Real-time METAR parser with Kalshi market integration. Finds "dead" brackets (eliminated by observed temperature floor) that are still trading.

## The Edge

**Key insight:** We can't know what bracket will WIN, but we CAN know what brackets have LOST.

- METAR data shows the observed temperature
- If observed floor > bracket ceiling → that bracket is **DEAD**
- If a dead bracket is still trading at >2¢ → that's your edge (bet NO)

## Features

- Auto-fetches METAR data from Iowa State ASOS
- Auto-fetches Kalshi market prices (no auth needed)
- Converts UTC to local time (handles DST)
- Highlights brackets with edge opportunities
- Auto-refreshes every 60 seconds

## Cities Supported

| City | Station | Kalshi Series |
|------|---------|---------------|
| NYC (Central Park) | KNYC | KXHIGHNY |
| Philadelphia | KPHL | KXHIGHPHL |
| Chicago (Midway) | KMDW | KXHIGHCHI |
| Los Angeles | KLAX | KXHIGHLAX |
| Miami | KMIA | KXHIGHMIA |
| Austin | KAUS | KXHIGHAUS |
| San Francisco | KSFO | KXHIGHSFO |
| Seattle | KSEA | KXHIGHSEA |
| Washington DC | KDCA | KXHIGHDC |
| New Orleans | KMSY | KXHIGHMSY |
| Las Vegas | KLAS | KXHIGHLAS |
| Denver | KDEN | KXHIGHDEN |

## Deployment to DigitalOcean App Platform

### Option 1: Deploy from GitHub

1. Push this code to a GitHub repo
2. Go to DigitalOcean App Platform
3. Create new app → Connect GitHub repo
4. It will auto-detect Node.js
5. Deploy!

### Option 2: Deploy via CLI

```bash
# Install doctl
brew install doctl  # or see https://docs.digitalocean.com/reference/doctl/

# Auth
doctl auth init

# Create app spec
doctl apps create --spec app.yaml
```

### App Spec (app.yaml)

```yaml
name: kalshi-weather-edge
services:
  - name: web
    github:
      repo: YOUR_USERNAME/kalshi-weather-edge
      branch: main
    build_command: npm install
    run_command: npm start
    http_port: 3000
    instance_size_slug: basic-xxs
    instance_count: 1
```

## Local Development

```bash
npm install
npm start
# Open http://localhost:3000
```

## API Endpoints

- `GET /api/cities` - List all cities
- `GET /api/city/:code` - Get data for a specific city (e.g., `/api/city/DEN`)
- `GET /api/summary` - Get summary of all cities (good for finding edges)

## How It Works

1. **Fetch METAR**: Gets 5-minute temperature observations from Iowa State ASOS
2. **Parse T-group**: Extracts precise temperature from T-group in METAR remarks (0.1°C precision)
3. **Calculate floor**: The minimum possible °F value from the highest reading
4. **Fetch Kalshi**: Gets current market prices for the city's temperature brackets
5. **Find edges**: Brackets where `floor > ceiling` but `yes_price > 2¢`

## Caveats

⚠️ **This is not financial advice!**

- We only see 5-min snapshots; CLI uses 1-minute OMOs
- A spike between readings could push the actual CLI higher
- The day might not be over yet
- Always verify before betting

## License

MIT - Use at your own risk!
