const express = require('express');
const fetch = require('node-fetch');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

// =============================================================================
// CITY CONFIGURATIONS
// =============================================================================
const CITIES = {
    NYC: { 
        name: 'NYC (Central Park)', 
        station: 'NYC', 
        network: 'NY_ASOS', 
        icao: 'KNYC',
        tz: 'America/New_York',
        utcOffset: -5,
        dstOffset: -4,
        kalshiSeries: 'KXHIGHNY',  // High temp NYC
        kalshiLowSeries: 'KXLOWNY' // Low temp NYC
    },
    PHL: { 
        name: 'Philadelphia', 
        station: 'PHL', 
        network: 'PA_ASOS', 
        icao: 'KPHL',
        tz: 'America/New_York',
        utcOffset: -5,
        dstOffset: -4,
        kalshiSeries: 'KXHIGHPHL',
        kalshiLowSeries: 'KXLOWPHL'
    },
    CHI: { 
        name: 'Chicago (Midway)', 
        station: 'MDW', 
        network: 'IL_ASOS', 
        icao: 'KMDW',
        tz: 'America/Chicago',
        utcOffset: -6,
        dstOffset: -5,
        kalshiSeries: 'KXHIGHCHI',
        kalshiLowSeries: 'KXLOWCHI'
    },
    LAX: { 
        name: 'Los Angeles', 
        station: 'LAX', 
        network: 'CA_ASOS', 
        icao: 'KLAX',
        tz: 'America/Los_Angeles',
        utcOffset: -8,
        dstOffset: -7,
        kalshiSeries: 'KXHIGHLAX',
        kalshiLowSeries: 'KXLOWLAX'
    },
    MIA: { 
        name: 'Miami', 
        station: 'MIA', 
        network: 'FL_ASOS', 
        icao: 'KMIA',
        tz: 'America/New_York',
        utcOffset: -5,
        dstOffset: -4,
        kalshiSeries: 'KXHIGHMIA',
        kalshiLowSeries: 'KXLOWMIA'
    },
    AUS: { 
        name: 'Austin', 
        station: 'AUS', 
        network: 'TX_ASOS', 
        icao: 'KAUS',
        tz: 'America/Chicago',
        utcOffset: -6,
        dstOffset: -5,
        kalshiSeries: 'KXHIGHAUS',
        kalshiLowSeries: 'KXLOWAUS'
    },
    SFO: { 
        name: 'San Francisco', 
        station: 'SFO', 
        network: 'CA_ASOS', 
        icao: 'KSFO',
        tz: 'America/Los_Angeles',
        utcOffset: -8,
        dstOffset: -7,
        kalshiSeries: 'KXHIGHSFO',
        kalshiLowSeries: 'KXLOWSFO'
    },
    SEA: { 
        name: 'Seattle', 
        station: 'SEA', 
        network: 'WA_ASOS', 
        icao: 'KSEA',
        tz: 'America/Los_Angeles',
        utcOffset: -8,
        dstOffset: -7,
        kalshiSeries: 'KXHIGHSEA',
        kalshiLowSeries: 'KXLOWSEA'
    },
    DCA: { 
        name: 'Washington DC', 
        station: 'DCA', 
        network: 'VA_ASOS', 
        icao: 'KDCA',
        tz: 'America/New_York',
        utcOffset: -5,
        dstOffset: -4,
        kalshiSeries: 'KXHIGHDC',
        kalshiLowSeries: 'KXLOWDC'
    },
    MSY: { 
        name: 'New Orleans', 
        station: 'MSY', 
        network: 'LA_ASOS', 
        icao: 'KMSY',
        tz: 'America/Chicago',
        utcOffset: -6,
        dstOffset: -5,
        kalshiSeries: 'KXHIGHMSY',
        kalshiLowSeries: 'KXLOWMSY'
    },
    LAS: { 
        name: 'Las Vegas', 
        station: 'LAS', 
        network: 'NV_ASOS', 
        icao: 'KLAS',
        tz: 'America/Los_Angeles',
        utcOffset: -8,
        dstOffset: -7,
        kalshiSeries: 'KXHIGHLAS',
        kalshiLowSeries: 'KXLOWLAS'
    },
    DEN: { 
        name: 'Denver', 
        station: 'DEN', 
        network: 'CO_ASOS', 
        icao: 'KDEN',
        tz: 'America/Denver',
        utcOffset: -7,
        dstOffset: -6,
        kalshiSeries: 'KXHIGHDEN',
        kalshiLowSeries: 'KXLOWDEN'
    }
};

// =============================================================================
// UTILITY FUNCTIONS
// =============================================================================

// Check if date is in DST (US rules)
function isDST(date) {
    const year = date.getFullYear();
    const marchFirst = new Date(year, 2, 1);
    const dstStart = new Date(year, 2, 14 - marchFirst.getDay());
    dstStart.setHours(2, 0, 0, 0);
    const novFirst = new Date(year, 10, 1);
    const dstEnd = new Date(year, 10, 7 - novFirst.getDay());
    dstEnd.setHours(2, 0, 0, 0);
    return date >= dstStart && date < dstEnd;
}

// Convert UTC to local date string
function utcToLocalDate(utcDate, cityCode) {
    const config = CITIES[cityCode];
    const offset = isDST(utcDate) ? config.dstOffset : config.utcOffset;
    const localTime = new Date(utcDate.getTime() + offset * 60 * 60 * 1000);
    return localTime.toISOString().split('T')[0];
}

// Get local date for a city
function getLocalMarketDate(cityCode) {
    return utcToLocalDate(new Date(), cityCode);
}

// Parse T-group from METAR (precise temp)
function parseMetarTempPrecise(metar) {
    const match = metar.match(/T(\d)(\d{3})(\d)(\d{3})/);
    if (match) {
        const sign = match[1] === '1' ? -1 : 1;
        const tenths = parseInt(match[2]);
        return { tempC: sign * tenths / 10, isPrecise: true };
    }
    return null;
}

// Parse standard temp from METAR
function parseMetarTemp(metar) {
    const match = metar.match(/\s(M?\d{2})\/(M?\d{2})\s/);
    if (match) {
        const tempC = parseInt(match[1].replace('M', '-'));
        return { tempC, isPrecise: false };
    }
    return null;
}

// Get possible F range from C
function getPossibleF(tempC, isPrecise) {
    const uncertainty = isPrecise ? 0.05 : 0.5;
    const minC = tempC - uncertainty;
    const maxC = tempC + uncertainty;
    const minF = Math.round(minC * 9/5 + 32);
    const maxF = Math.round(maxC * 9/5 + 32);
    return { minF, maxF, exactF: tempC * 9/5 + 32 };
}

// =============================================================================
// DATA FETCHING
// =============================================================================

// Fetch METAR data from Iowa State
async function fetchMetarData(cityCode) {
    const config = CITIES[cityCode];
    const now = new Date();
    const localDate = getLocalMarketDate(cityCode);
    const [year, month, day] = localDate.split('-').map(Number);
    
    // Fetch data covering the local day (with buffer)
    const prevDay = new Date(Date.UTC(year, month - 1, day - 1));
    const nextDay = new Date(Date.UTC(year, month - 1, day + 2));
    
    const url = `https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?` +
        `network=${config.network}&station=${config.station}` +
        `&data=metar&year1=${prevDay.getUTCFullYear()}&month1=${prevDay.getUTCMonth() + 1}&day1=${prevDay.getUTCDate()}` +
        `&year2=${nextDay.getUTCFullYear()}&month2=${nextDay.getUTCMonth() + 1}&day2=${nextDay.getUTCDate()}` +
        `&tz=Etc%2FUTC&format=onlycomma&latlon=no&elev=no&missing=M&trace=T` +
        `&direct=no&report_type=1&report_type=3&report_type=4`;
    
    const response = await fetch(url);
    const csv = await response.text();
    
    // Parse and filter to local date
    const lines = csv.trim().split('\n').slice(1);
    const readings = [];
    
    for (const line of lines) {
        const parts = line.split(',');
        if (parts.length < 3) continue;
        
        const timeStr = parts[1];
        const metar = parts.slice(2).join(',');
        const utcTime = new Date(timeStr + 'Z');
        const readingLocalDate = utcToLocalDate(utcTime, cityCode);
        
        if (readingLocalDate !== localDate) continue;
        
        let parsed = parseMetarTempPrecise(metar);
        if (!parsed) parsed = parseMetarTemp(metar);
        if (!parsed) continue;
        
        const fRange = getPossibleF(parsed.tempC, parsed.isPrecise);
        const offset = isDST(utcTime) ? config.dstOffset : config.utcOffset;
        const localTime = new Date(utcTime.getTime() + offset * 60 * 60 * 1000);
        
        readings.push({
            timeUTC: timeStr,
            timeLocal: localTime.toISOString().substring(11, 16),
            tempC: parsed.tempC,
            isPrecise: parsed.isPrecise,
            ...fRange
        });
    }
    
    return { readings, localDate, cityConfig: config };
}

// Fetch Kalshi markets for a series
async function fetchKalshiMarkets(seriesTicker) {
    try {
        const url = `https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=${seriesTicker}&status=open`;
        const response = await fetch(url);
        const data = await response.json();
        return data.markets || [];
    } catch (error) {
        console.error(`Error fetching Kalshi markets for ${seriesTicker}:`, error);
        return [];
    }
}

// =============================================================================
// API ENDPOINTS
// =============================================================================

// Get cities list
app.get('/api/cities', (req, res) => {
    const cities = Object.entries(CITIES).map(([code, config]) => ({
        code,
        name: config.name,
        station: config.station,
        icao: config.icao
    }));
    res.json(cities);
});

// Get data for a specific city
app.get('/api/city/:code', async (req, res) => {
    const cityCode = req.params.code.toUpperCase();
    
    if (!CITIES[cityCode]) {
        return res.status(404).json({ error: 'City not found' });
    }
    
    try {
        // Fetch METAR data
        const { readings, localDate, cityConfig } = await fetchMetarData(cityCode);
        
        // Fetch Kalshi markets
        const kalshiMarkets = await fetchKalshiMarkets(cityConfig.kalshiSeries);
        
        // Calculate observed floor/ceiling
        let observedFloor = null;
        let observedCeiling = null;
        let maxReading = null;
        
        if (readings.length > 0) {
            maxReading = readings.reduce((max, r) => r.minF > max.minF ? r : max);
            observedFloor = maxReading.minF;
            observedCeiling = readings.reduce((max, r) => r.maxF > max.maxF ? r : max).maxF;
        }
        
        // Analyze brackets
        const brackets = kalshiMarkets.map(market => {
            // Parse the bracket from market subtitle (e.g., "45° to 46°" or "≤44°" or "≥50°")
            const subtitle = market.subtitle || market.title || '';
            let low = null;
            let high = null;
            
            // Try to parse bracket ranges
            const rangeMatch = subtitle.match(/(\d+)°?\s*to\s*(\d+)°?/i);
            const underMatch = subtitle.match(/[≤<]\s*(\d+)°?/);
            const overMatch = subtitle.match(/[≥>]\s*(\d+)°?/);
            
            if (rangeMatch) {
                low = parseInt(rangeMatch[1]);
                high = parseInt(rangeMatch[2]);
            } else if (underMatch) {
                high = parseInt(underMatch[1]);
            } else if (overMatch) {
                low = parseInt(overMatch[1]);
            }
            
            // Determine status
            let status = 'unknown';
            let isDead = false;
            
            if (observedFloor !== null && high !== null && observedFloor > high) {
                status = 'DEAD';
                isDead = true;
            } else if (observedFloor !== null && low !== null && observedCeiling !== null) {
                if (observedFloor >= low && observedCeiling <= (high || Infinity)) {
                    status = 'CURRENT';
                } else if (observedCeiling < low) {
                    status = 'NOT_YET';
                } else {
                    status = 'POSSIBLE';
                }
            }
            
            return {
                ticker: market.ticker,
                title: market.title,
                subtitle: market.subtitle,
                low,
                high,
                yesPrice: market.yes_bid || 0,
                yesPriceAsk: market.yes_ask || 0,
                noPrice: market.no_bid || 0,
                noPriceAsk: market.no_ask || 0,
                lastPrice: market.last_price || 0,
                volume: market.volume || 0,
                status,
                isDead,
                // THE EDGE: if dead but still trading above ~2¢
                hasEdge: isDead && (market.yes_bid > 2 || market.last_price > 2)
            };
        });
        
        // Sort brackets by temperature
        brackets.sort((a, b) => (a.low || -999) - (b.low || -999));
        
        res.json({
            city: cityCode,
            cityName: cityConfig.name,
            localDate,
            localTime: new Date().toLocaleTimeString('en-US', { timeZone: cityConfig.tz }),
            observedFloor,
            observedCeiling,
            maxReading,
            readingsCount: readings.length,
            readings: readings.slice(-20).reverse(), // Last 20, newest first
            brackets,
            deadBracketsWithEdge: brackets.filter(b => b.hasEdge),
            timestamp: new Date().toISOString()
        });
        
    } catch (error) {
        console.error(`Error fetching data for ${cityCode}:`, error);
        res.status(500).json({ error: error.message });
    }
});

// Get all cities summary (for dashboard)
app.get('/api/summary', async (req, res) => {
    const results = [];
    
    for (const [code, config] of Object.entries(CITIES)) {
        try {
            const { readings, localDate } = await fetchMetarData(code);
            const kalshiMarkets = await fetchKalshiMarkets(config.kalshiSeries);
            
            let observedFloor = null;
            if (readings.length > 0) {
                observedFloor = readings.reduce((max, r) => r.minF > max.minF ? r : max).minF;
            }
            
            // Count dead brackets with edge
            let edgeCount = 0;
            for (const market of kalshiMarkets) {
                const subtitle = market.subtitle || '';
                const underMatch = subtitle.match(/[≤<]\s*(\d+)°?/);
                const rangeMatch = subtitle.match(/(\d+)°?\s*to\s*(\d+)°?/i);
                
                let high = null;
                if (rangeMatch) high = parseInt(rangeMatch[2]);
                else if (underMatch) high = parseInt(underMatch[1]);
                
                if (observedFloor !== null && high !== null && observedFloor > high) {
                    if ((market.yes_bid > 2) || (market.last_price > 2)) {
                        edgeCount++;
                    }
                }
            }
            
            results.push({
                code,
                name: config.name,
                localDate,
                observedFloor,
                readingsCount: readings.length,
                marketsCount: kalshiMarkets.length,
                edgeCount
            });
        } catch (error) {
            results.push({
                code,
                name: config.name,
                error: error.message
            });
        }
    }
    
    res.json(results);
});

// =============================================================================
// SERVE FRONTEND
// =============================================================================

app.use(express.static('public'));

app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// =============================================================================
// START SERVER
// =============================================================================

app.listen(PORT, () => {
    console.log(`🌡️  Kalshi Weather Edge running on port ${PORT}`);
    console.log(`   Open http://localhost:${PORT}`);
});
