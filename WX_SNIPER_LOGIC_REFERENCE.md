# WX Sniper v4.5 - Complete Logic Reference

This document contains ALL the critical logic for the WX Sniper temperature trading bot.
When implementing or debugging, refer to this document for exact specifications.

---

## TABLE OF CONTENTS
1. [Core Strategy](#1-core-strategy)
2. [Temperature Conversion (CRITICAL)](#2-temperature-conversion-critical)
3. [METAR Parsing](#3-metar-parsing)
4. [Bracket Resolution Logic (CRITICAL)](#4-bracket-resolution-logic-critical)
5. [Polling Schedule](#5-polling-schedule)
6. [Order Execution](#6-order-execution)
7. [Station Configuration](#7-station-configuration)
8. [Data Flow Summary](#8-data-flow-summary)

---

## 1. CORE STRATEGY

We are a **LATENCY SNIPER**. When a METAR drops showing a new temperature reading, we detect which brackets have TRANSITIONED from OPEN to a resolved state (DEAD or LOCKED) **before the market reprices**.

**Key Insight**: Temperature extremes are monotonic:
- **Daily HIGH** can only go UP (warmest reading so far)
- **Daily LOW** can only go DOWN (coldest reading so far)

This means once a bracket is resolved, it stays resolved.

**Trading Actions**:
- `OPEN → DEAD`: BUY NO (bracket can never win, NO settles at $1)
- `OPEN → LOCKED`: BUY YES (bracket guaranteed to win, YES settles at $1)

---

## 2. TEMPERATURE CONVERSION (CRITICAL)

### NWS Rounding Rule: "Round Half Up Asymmetric"

The NWS uses `math.floor(value + 0.5)` which rounds 0.5 UP (not banker's rounding).

```python
def nws_round(temp_f: float) -> int:
    """NWS rounding: round half up (asymmetric)."""
    return math.floor(temp_f + 0.5)
```

**Examples**:
- `70.5°F → 71°F` (rounds UP)
- `70.4°F → 70°F`
- `70.6°F → 71°F`
- `-0.5°F → 0°F` (rounds toward positive infinity)
- `-1.5°F → -1°F`

### Celsius to Fahrenheit Conversion

```python
def c_to_f_nws(temp_c: float) -> int:
    """Convert Celsius to Fahrenheit with NWS rounding."""
    return nws_round(temp_c * 9/5 + 32)
```

**Formula**: `F = C × 1.8 + 32`, then apply nws_round()

### Why This Matters

6-hour groups in METARs report temperatures in **tenths of Celsius**. The conversion and rounding determines which bracket wins.

**Example**:
- 6-hour max = `10217` → +21.7°C → 21.7 × 1.8 + 32 = 71.06°F → rounds to **71°F**
- If market has "70° or below" bracket, this is now DEAD (71 > 70)

---

## 3. METAR PARSING

### METAR Structure

Example: `KSFO 281853Z 29012KT 10SM FEW020 17/08 A3012 RMK AO2 SLP203 T01720083 10189 20156`

Key parts:
- `KSFO` - Station identifier
- `281853Z` - Day 28, time 18:53 UTC
- `17/08` - Current temp/dewpoint (whole Celsius, less precise)
- `RMK` - Start of remarks section
- `T01720083` - T-group (precise temp: 17.2°C, dewpoint: 8.3°C)
- `10189` - 6-hour MAX: +18.9°C
- `20156` - 6-hour MIN: +15.6°C

### T-Group Parsing (Current Temperature)

Format: `T{sign1}{TTT}{sign2}{DDD}` where:
- sign: 0 = positive, 1 = negative
- TTT/DDD: tenths of degree Celsius

```python
# Regex: \bT(\d)(\d{3})(\d)(\d{3})\b
t_match = re.search(r'\bT(\d)(\d{3})(\d)(\d{3})\b', raw)
if t_match:
    temp_sign = -1 if t_match.group(1) == '1' else 1
    temp_c = temp_sign * int(t_match.group(2)) / 10.0
    temp_f = nws_round(temp_c * 9/5 + 32)
```

**Example**: `T01720083`
- Temp: sign=0 (positive), value=172 → +17.2°C → 63°F
- Dewpoint: sign=0, value=083 → +8.3°C

### 6-Hour Group Parsing (CRITICAL FOR TRADING)

These appear in the **remarks section** (after `RMK`) at synoptic times.

**6-Hour Maximum (1-group)**: Format `1{sign}{TTT}`
```python
# Must be in remarks, exactly 5 digits starting with 1
max_match = re.search(r'\b1([01])(\d{3})\b', remarks)
if max_match:
    sign = -1 if max_match.group(1) == '1' else 1
    six_hr_max_c = sign * int(max_match.group(2)) / 10.0
```

**6-Hour Minimum (2-group)**: Format `2{sign}{TTT}`
```python
min_match = re.search(r'\b2([01])(\d{3})\b', remarks)
if min_match:
    sign = -1 if min_match.group(1) == '1' else 1
    six_hr_min_c = sign * int(min_match.group(2)) / 10.0
```

**Examples**:
- `10217` → 1-group, sign=0, 217 → +21.7°C
- `11015` → 1-group, sign=1, 015 → -1.5°C
- `20156` → 2-group, sign=0, 156 → +15.6°C
- `21028` → 2-group, sign=1, 028 → -2.8°C

---

## 4. BRACKET RESOLUTION LOGIC (CRITICAL)

Each Kalshi bracket has:
- `floor_strike`: Lower bound (can be None for "X or below" brackets)
- `cap_strike`: Upper bound (can be None for "X or above" brackets)
- `strike_type`: `'between'`, `'greater'`, or `'less'`
- `signal_type`: `'high'` or `'low'` (which market type)

### HIGH MARKETS

The observed high can only go UP. We track `observed_high` (warmest seen so far).

#### "X to Y" (between) - e.g., "21° to 22°"
- YES wins if: `floor <= final_high <= cap`
- **DEAD if**: `observed_high > cap` (exceeded range, can never come back)
- **Can NEVER be LOCKED**: High could keep rising and exceed cap later

```python
if strike_type == 'between':
    if cap_strike is not None and observed_high > cap_strike:
        return 'dead'
    return 'open'  # Never locked
```

#### "X or above" (greater) - e.g., "23° or above"
- YES wins if: `final_high >= floor`
- **LOCKED if**: `observed_high >= floor` (we hit threshold, guaranteed win)
- **Can NEVER be DEAD**: High keeps rising, which helps this bracket

```python
if strike_type == 'greater':
    if floor_strike is not None and observed_high >= floor_strike:
        return 'locked'
    return 'open'  # Never dead
```

#### "X or below" (less) - e.g., "18° or below"
- YES wins if: `final_high <= cap`
- **DEAD if**: `observed_high > cap` (too warm, can never come back)
- **Can NEVER be LOCKED**: High could still rise

```python
if strike_type == 'less':
    if cap_strike is not None and observed_high > cap_strike:
        return 'dead'
    return 'open'  # Never locked
```

### LOW MARKETS

The observed low can only go DOWN. We track `observed_low` (coldest seen so far).

#### "X to Y" (between) - e.g., "5° to 6°"
- YES wins if: `floor <= final_low <= cap`
- **DEAD if**: `observed_low < floor` (dropped below range, can never rise)
- **Can NEVER be LOCKED**: Low could keep dropping

```python
if strike_type == 'between':
    if floor_strike is not None and observed_low < floor_strike:
        return 'dead'
    return 'open'  # Never locked
```

#### "X or above" (greater) - e.g., "5° or above"
- YES wins if: `final_low >= floor`
- **DEAD if**: `observed_low < floor` (dropped below, can never rise back)
- **Can NEVER be LOCKED**: Low could still drop below floor later

```python
if strike_type == 'greater':
    if floor_strike is not None and observed_low < floor_strike:
        return 'dead'
    return 'open'  # Never locked
```

#### "X or below" (less) - e.g., "4° or below"
- YES wins if: `final_low <= cap`
- **LOCKED if**: `observed_low <= cap` (we hit it, low can only go lower)
- **Can NEVER be DEAD**: Low keeps dropping, which helps this bracket

```python
if strike_type == 'less':
    if cap_strike is not None and observed_low <= cap_strike:
        return 'locked'
    return 'open'  # Never dead
```

### Resolution Summary Table

| Market | Strike Type | DEAD when | LOCKED when |
|--------|-------------|-----------|-------------|
| HIGH | between | high > cap | NEVER |
| HIGH | greater (X or above) | NEVER | high >= floor |
| HIGH | less (X or below) | high > cap | NEVER |
| LOW | between | low < floor | NEVER |
| LOW | greater (X or above) | low < floor | NEVER |
| LOW | less (X or below) | NEVER | low <= cap |

---

## 5. POLLING SCHEDULE

### METAR Drop Timing

METARs typically drop around :53-:54 past the hour, but can vary by station.
Vegas (KLAS) has been observed dropping at :58.

### Polling Windows (v4.5)

| Minutes | Mode | Interval | Reason |
|---------|------|----------|--------|
| :50-:59 | METAR DROP | 200ms | Primary drop window |
| :00-:05 | HOT/BUFFER | 2s | Catch late drops |
| :06-:49 | NORMAL | 60s | Conserve resources |

### Data Sources (Priority Order)

1. **NWS TXT** (Primary): `https://tgftp.nws.noaa.gov/data/observations/metar/stations/{STATION}.TXT`
   - Often faster, less caching
   - Fetched in parallel for all stations
   - Timeout: 3 seconds

2. **Aviation Weather API** (Fallback): `https://aviationweather.gov/api/data/metar?ids={STATIONS}&format=json`
   - Batch request for missing stations
   - May have caching
   - Timeout: 10 seconds

### METAR Change Logging

All METAR changes are logged to `/tmp/metar_drops.log` with format:
```
{ISO_timestamp},{station},{minute},{second},{raw_metar_first_80_chars}
```

Use this to analyze actual drop patterns per station.

---

## 6. ORDER EXECUTION

### Trade Execution Flow

When a bracket transitions from OPEN to DEAD or LOCKED:

```
1. DETECT: bracket.check_status() returns 'dead' or 'locked'
2. DECIDE: 
   - If DEAD → action = BUY_NO, side = 'no'
   - If LOCKED → action = BUY_YES, side = 'yes'
3. CHECK GLOBAL LOCK: if ticker in self.processed_tickers → skip
4. EXECUTE BUY: Place limit order at 99¢ to guarantee fill
5. HOLD TO SETTLEMENT: Contract pays out $1.00 (100¢) → 1¢ profit
```

### Order Parameters

```python
# BUY order (the ONLY order we place)
{
    "ticker": bracket.ticker,      # e.g., "KXHIGHNY-26JAN30-B21"
    "side": "yes" or "no",
    "action": "buy",
    "count": 10,
    "type": "limit",
    "yes_price": 99  # or "no_price": 99 depending on side
}
```

### Why No Hedge Sell

Previously we placed an immediate sell at 99¢ after buying. This was wrong:

- Contracts settle at 100¢ (not 99¢). Selling at 99¢ = giving away your profit.
- On dead brackets, the sell filled immediately as a taker (counterparty exists).
- Result: Buy at 99¢ + fees, Sell at 99¢ - fees = net loss from double fees.
- Kalshi netted the pair, leaving a worthless Yes at 1¢ that expired to $0.

**Correct approach**: Buy the winning side, hold to settlement, collect 100¢.

### Emergency Exit (Ejection Seat Only)

Only sell if the Ejection Seat confirms the Scout was wrong:

```python
# EMERGENCY SELL — market order to exit immediately
{
    "ticker": bracket.ticker,
    "side": position.side,
    "action": "sell",
    "count": position.quantity,
    "type": "market",
    "yes_price": 1  # or "no_price": 1 — will fill at best available
}
```

### Global Ticker Lock

Prevents METAR sniper and Scout from double-trading the same bracket:

```python
# In WXSniper.__init__
self.processed_tickers = set()

# In _snipe() and execute_scout_trade()
if bracket.ticker in self.processed_tickers:
    return  # Already traded

# After successful trade
self.processed_tickers.add(bracket.ticker)

# On date rollover
self.processed_tickers.clear()
```

### Kalshi API Authentication

```python
# Signature = RSA-PSS sign of: "{timestamp_ms}{METHOD}{path}"
message = f"{timestamp_ms}{method}{path}"
signature = private_key.sign(
    message.encode(),
    padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.MAX_LENGTH
    ),
    hashes.SHA256()
)
signature_b64 = base64.b64encode(signature).decode()

# Headers
headers = {
    "KALSHI-ACCESS-KEY": api_key_id,
    "KALSHI-ACCESS-SIGNATURE": signature_b64,
    "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
    "Content-Type": "application/json"
}
```

### Kalshi Event Ticker Format

```
{series_ticker}-{date_suffix}

Examples:
- KXHIGHNY-26JAN30 (NYC High, Jan 30, 2026)
- KXLOWTNYC-26JAN30 (NYC Low, Jan 30, 2026)

Date suffix format: YYMmmDD (e.g., 26JAN30 = January 30, 2026)
```

---

## 7. STATION CONFIGURATION

### Station Mappings

| ICAO | City | High Ticker | Low Ticker | Timezone |
|------|------|-------------|------------|----------|
| KNYC | NYC | KXHIGHNY | KXLOWTNYC | America/New_York |
| KPHL | Philadelphia | KXHIGHPHIL | KXLOWTPHIL | America/New_York |
| KMDW | Chicago | KXHIGHCHI | KXLOWTCHI | America/Chicago |
| KLAX | Los Angeles | KXHIGHLAX | KXLOWTLAX | America/Los_Angeles |
| KMIA | Miami | KXHIGHMIA | KXLOWTMIA | America/New_York |
| KAUS | Austin | KXHIGHAUS | KXLOWTAUS | America/Chicago |
| KSFO | San Francisco | KXHIGHTSFO | None | America/Los_Angeles |
| KSEA | Seattle | KXHIGHTSEA | None | America/Los_Angeles |
| KDCA | Washington DC | KXHIGHTDC | None | America/New_York |
| KMSY | New Orleans | KXHIGHTNOLA | None | America/Chicago |
| KLAS | Las Vegas | KXHIGHTLV | None | America/Los_Angeles |
| KDEN | Denver | KXHIGHDEN | KXLOWTDEN | America/Denver |

Note: Some stations only have HIGH markets (Low Ticker = None).

---

## 8. DATA FLOW SUMMARY

### Initialization (on startup)

```
1. init_watchlists()
   - For each station, fetch today's event from Kalshi API
   - Parse all brackets (floor_strike, cap_strike, strike_type)
   - Store in high_watchlist and low_watchlist

2. fetch_historical_temps()
   - Fetch last 24 hours of METARs from Aviation Weather API
   - Parse T-groups and 6-hour groups
   - Initialize observed_high and observed_low per station

3. prune_watchlists()
   - Check each bracket against current observed temps
   - Move already-resolved brackets to resolved_brackets list
   - Only watch brackets still in OPEN state
```

### Main Loop

```
WHILE True:
    1. Determine polling mode based on current UTC minute
    
    2. poll_and_snipe():
       a. Fetch METARs (NWS TXT parallel, Aviation API fallback)
       b. For each new METAR:
          - Parse T-group → update latest_temp_f
          - Parse 6-hour groups → update observed_high/low
          - If observed changed → _check_transitions()
       
    3. _check_transitions(state):
       For each bracket in watchlist:
          - new_status = bracket.check_status(observed_high, observed_low)
          - If OPEN → DEAD: _snipe(BUY_NO)
          - If OPEN → LOCKED: _snipe(BUY_YES)
          - Move to resolved_brackets
    
    4. refresh_prices() every 5 minutes
       - Re-fetch current ask prices from Kalshi
    
    5. Sleep based on polling mode (200ms / 2s / 60s)
```

### State Tracking

Per station, we track:
- `observed_high`: Warmest temperature seen today (only goes up)
- `observed_low`: Coldest temperature seen today (only goes down)
- `latest_temp_f`: Most recent temperature reading
- `latest_metar`: Raw METAR string
- `metar_time`: Observation time from METAR
- `high_watchlist`: List of OPEN high brackets
- `low_watchlist`: List of OPEN low brackets
- `resolved_brackets`: List of DEAD/LOCKED brackets

---

## CRITICAL IMPLEMENTATION NOTES

1. **Zero is a valid strike**: When parsing `floor_strike` and `cap_strike`, check for `None` explicitly, not falsiness. Zero is a valid temperature.

2. **Persistent HTTP sessions**: Reuse `requests.Session()` for both Kalshi and NWS to avoid TLS handshake overhead (~100-200ms savings per request).

3. **Environment variables**:
   - `KALSHI_API_KEY_ID`: API key identifier
   - `KALSHI_PRIVATE_KEY`: RSA private key (PEM format, may have literal `\n`)
   - `LIVE_MODE`: Set to `"true"` for real trading
   - `MAX_PRICE`: Maximum price in cents (default 95)
   - `PORT`: Dashboard HTTP port (default 8080)

4. **Day reset**: States should reset at midnight in the station's local timezone (not implemented in current version - relies on restart).

5. **The hedge is REMOVED**: Never sell after buying. Hold to settlement for full profit. Only the Ejection Seat sells (market order if position is wrong).
