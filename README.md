# WX Sniper V2 — NWWS-OI Product Ingestor

Real-time ingestion of NWS products (DSM, METAR, SPECI, CLI) via the NWWS-OI
XMPP feed, with parsing of temperature data for Kalshi weather market sniping.

## What This Does

Connects to the NOAA Weather Wire Service Open Interface (NWWS-OI) — the fastest
NWS text dissemination method — and extracts temperature readings that resolve
rounding ambiguity in Kalshi weather markets.

**Products parsed:**
- **DSM** (Daily Summary Message): Running high/low in whole °F — directly resolves brackets
- **METAR T-groups**: Tenths °C precision — HIGH confidence bracket resolution
- **METAR 6-hour max/min** (1-group/2-group): Tenths °C at synoptic times (00Z/06Z/12Z/18Z)
- **METAR 24-hour max/min** (4-group): Tenths °C
- **SPECI**: Special observations with T-group precision
- **CLI**: Settlement confirmation (drops next morning, not for sniping)

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.template .env
# Edit .env with your NWWS-OI user_ID and password

# 3. Run
./run.sh

# Or with all products visible (for debugging):
./run.sh --watch-all

# Or with XMPP protocol debug logging:
./run.sh --debug
```

## Output

Console shows parsed temperature products in real-time:
```
[18:00:02] DSM    | KNYC | dsm_high        |   72°F at 1430 LST
[18:00:02] DSM    | KNYC | dsm_low         |   58°F at 0545 LST
[18:51:03] METAR  | KPHL | metar_t_group   |   71°F (21.7°C)
[18:51:03] METAR  | KPHL | metar_6hr_max   |   72°F (22.2°C)
```

All products are also logged to `logs/products_YYYY-MM-DD.jsonl` as one JSON object per line.

## File Structure

```
wx_sniper_v2/
├── nwws_client.py     # XMPP client — connects to NWWS-OI, filters, dispatches
├── parsers.py         # Product parsers — DSM, METAR T-group/6hr, SPECI, CLI
├── nws_math.py        # NWS rounding rules (critical: half-up, not banker's)
├── stations.py        # Station config — tickers, WFOs, timezones, DSM IDs
├── test_parsers.py    # Test suite (16 tests with real product examples)
├── run.sh             # Convenience runner (loads .env, validates, runs)
├── .env.template      # Credentials template
├── requirements.txt   # Python deps (just slixmpp)
└── logs/              # Auto-created, daily JSONL product logs
```

## Deploying to EC2

```bash
# On 54.91.7.11
cd /home/ubuntu
git clone <repo> wx_sniper_v2  # or scp the files
cd wx_sniper_v2
pip install -r requirements.txt
cp .env.template .env
nano .env  # fill in NWWS_USER_ID and NWWS_PASSWORD

# Test run (foreground)
./run.sh

# Production run (background with systemd)
sudo cp wx-sniper-v2.service /etc/systemd/system/
sudo systemctl enable wx-sniper-v2
sudo systemctl start wx-sniper-v2
sudo journalctl -u wx-sniper-v2 -f  # watch logs
```

## NWWS-OI Connection Notes

- **Single session only**: Your credentials can only be connected from one machine at a time. A second connection will cause an auth error.
- **Phantom accounts**: If you kill the process without clean disconnect, a phantom session may persist for ~30 minutes. Wait and retry.
- **Random disconnects**: Known NWWS-OI issue. The client auto-reconnects via slixmpp.
- **Server transition**: As of 2/4/2026, Boulder (nwws-oi-bldr.weather.gov) is the active server. The client tries Boulder first, then College Park as fallback.
- **60-message history**: On join, you receive the last 60 messages. These may lack the `<x>` payload if the server recently restarted.

## Stations Monitored

24 stations across all Kalshi temperature markets. DSM product IDs watched:

| Station | City | DSM ID | WFO |
|---------|------|--------|-----|
| KNYC | New York (Central Park) | DSMNYC | KOKX |
| KPHL | Philadelphia | DSMPHL | KPHI |
| KMDW | Chicago Midway | DSMMDW | KLOT |
| KLAX | Los Angeles | DSMLAX | KLOX |
| KDEN | Denver | DSMDEN | KBOU |
| KAUS | Austin | DSMAUS | KEWX |
| ... | (20+ total) | ... | ... |

## What's Next

This is **Step 1** (data flowing). The roadmap:

1. ✅ **NWWS-OI client + parsers** (this module)
2. **Config UI + Kalshi execution** — Flask dashboard to manually configure snipes (station, bracket, direction, contract count, max price, trigger products). When a matching product arrives, fire the order.
3. **Automated ambiguity detection** — Monitor wethr.net `lowest_probable`/`highest_probable` + Kalshi market prices. Auto-detect when two adjacent brackets are both ~50¢ and a resolving product is expected.
4. **Liquidity-aware sizing** — Pull orderbook depth, size orders to target revenue given cash balance and market conditions.

## Tests

```bash
cd wx_sniper_v2
python test_parsers.py
```

Runs 16 tests covering NWS rounding, C→F conversion, DSM parsing (real products from NYC, Chicago, Philly), METAR T-groups, 6-hour max/min, 24-hour groups, SPECI, product classification, and bracket resolution scenarios.
