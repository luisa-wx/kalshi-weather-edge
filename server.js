const express = require('express');
const fetch = require('node-fetch');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

// =============================================================================
// CITY CONFIGURATIONS
// hasLow: whether Kalshi has LOW temp markets for this city
// =============================================================================
const CITIES = {
    NYC: { 
        name: 'NYC (Central Park)', 
        station: 'NYC', 
        network: 'NY_ASOS', 
        icao: 'KNYC',
        nwsSite: 'KNYC',  // for NWS time series
        wethrStation: 'KNYC', // wethr.net station code
        tz: 'America/New_York',
        utcOffset: -5,
        dstOffset: -4,
        kalshiHighSeries: 'KXHIGHNY',
        kalshiLowSeries: 'KXLOWTNYC',
        hasLow: true
    },
    PHL: { 
        name: 'Philadelphia', 
        station: 'PHL', 
        network: 'PA_ASOS', 
        icao: 'KPHL',
        nwsSite: 'KPHL',
        wethrStation: 'KPHL',
        tz: 'America/New_York',
        utcOffset: -5,
        dstOffset: -4,
        kalshiHighSeries: 'KXHIGHPHL',
        kalshiLowSeries: 'KXLOWTPHL',
        hasLow: true
    },
    CHI: { 
        name: 'Chicago (Midway)', 
        station: 'MDW', 
        network: 'IL_ASOS', 
        icao: 'KMDW',
        nwsSite: 'KMDW',
        wethrStation: 'KMDW',
        tz: 'America/Chicago',
        utcOffset: -6,
        dstOffset: -5,
        kalshiHighSeries: 'KXHIGHCHI',
        kalshiLowSeries: 'KXLOWTCHI',
        hasLow: true
    },
    LAX: { 
        name: 'Los Angeles', 
        station: 'LAX', 
        network: 'CA_ASOS', 
        icao: 'KLAX',
        nwsSite: 'KLAX',
        wethrStation: 'KLAX',
        tz: 'America/Los_Angeles',
        utcOffset: -8,
        dstOffset: -7,
        kalshiHighSeries: 'KXHIGHLAX',
        kalshiLowSeries: 'KXLOWTLAX',
        hasLow: true
    },
    MIA: { 
        name: 'Miami', 
        station: 'MIA', 
        network: 'FL_ASOS', 
        icao: 'KMIA',
        nwsSite: 'KMIA',
        wethrStation: 'KMIA',
        tz: 'America/New_York',
        utcOffset: -5,
        dstOffset: -4,
        kalshiHighSeries: 'KXHIGHMIA',
        kalshiLowSeries: 'KXLOWTMIA',
        hasLow: true
    },
    AUS: { 
        name: 'Austin', 
        station: 'AUS', 
        network: 'TX_ASOS', 
        icao: 'KAUS',
        nwsSite: 'KAUS',
        wethrStation: 'KAUS',
        tz: 'America/Chicago',
        utcOffset: -6,
        dstOffset: -5,
        kalshiHighSeries: 'KXHIGHAUT',
        kalshiLowSeries: 'KXLOWTAUT',
        hasLow: true
    },
    SFO: { 
        name: 'San Francisco', 
        station: 'SFO', 
        network: 'CA_ASOS', 
        icao: 'KSFO',
        nwsSite: 'KSFO',
        wethrStation: 'KSFO',
        tz: 'America/Los_Angeles',
        utcOffset: -8,
        dstOffset: -7,
        kalshiHighSeries: 'KXHIGHTSFO',
        kalshiLowSeries: null,  // No low market
        hasLow: false
    },
    SEA: { 
        name: 'Seattle', 
        station: 'SEA', 
        network: 'WA_ASOS', 
        icao: 'KSEA',
        nwsSite: 'KSEA',
        wethrStation: 'KSEA',
        tz: 'America/Los_Angeles',
        utcOffset: -8,
        dstOffset: -7,
        kalshiHighSeries: 'KXHIGHSEA',
        kalshiLowSeries: null,
        hasLow: false
    },
    DCA: { 
        name: 'Washington DC', 
        station: 'DCA', 
        network: 'VA_ASOS', 
        icao: 'KDCA',
        nwsSite: 'KDCA',
        wethrStation: 'KDCA',
        tz: 'America/New_York',
        utcOffset: -5,
        dstOffset: -4,
        kalshiHighSeries: 'KXHIGHTDC',
        kalshiLowSeries: null,
        hasLow: false
    },
    MSY: { 
        name: 'New Orleans', 
        station: 'MSY', 
        network: 'LA_ASOS', 
        icao: 'KMSY',
        nwsSite: 'KMSY',
        wethrStation: 'KMSY',
        tz: 'America/Chicago',
        utcOffset: -6,
        dstOffset: -5,
        kalshiHighSeries: 'KXHIGHNO',
        kalshiLowSeries: null,
        hasLow: false
    },
    LAS: { 
        name: 'Las Vegas', 
        station: 'LAS', 
        network: 'NV_ASOS', 
        icao: 'KLAS',
        nwsSite: 'KLAS',
        wethrStation: 'KLAS',
        tz: 'America/Los_Angeles',
        utcOffset: -8,
        dstOffset: -7,
        kalshiHighSeries: 'KXHIGHLAS',
        kalshiLowSeries: null,
        hasLow: false
    },
    DEN: { 
        name: 'Denver', 
        station: 'DEN', 
        network: 'CO_ASOS', 
        icao: 'KDEN',
        nwsSite: 'KDEN',
        wethrStation: 'KDEN',
        tz: 'America/Denver',
        utcOffset: -7,
        dstOffset: -6,
        kalshiHighSeries: 'KXHIGHDEN',
        kalshiLowSeries: 'KXLOWTDEN',
        hasLow: true
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

// =============================================================================
// NWS ASYMMETRIC HALF-UP ROUNDING
// This is critical! -2.5 rounds to -2, not -3 (rounds toward positive infinity)
// =============================================================================

// Get timezone abbreviation
function getTzAbbrev(tz) {
    const abbrevs = {
        'America/New_York': 'ET',
        'America/Chicago': 'CT',
        'America/Denver': 'MT',
        'America/Los_Angeles': 'PT'
    };
    return abbrevs[tz] || tz;
}

function nwsRound(value) {
    // Asymmetric half-up: always round 0.5 toward positive infinity
    // Math.round does this naturally in JS for positive numbers
    // For negative: -2.5 should become -2, not -3
    // Math.round(-2.5) = -2 in JS (rounds toward +infinity), which is correct!
    return Math.round(value);
}

// Convert Celsius to Fahrenheit with NWS rounding
function celsiusToFahrenheit(tempC) {
    const exactF = tempC * 9/5 + 32;
    return nwsRound(exactF);
}

// =============================================================================
// METAR PARSING
// =============================================================================

// Parse T-group from METAR (precise temp to 0.1°C)
// Format: T[sign][temp][sign][dewpoint] where sign: 0=positive, 1=negative
// Example: T10561139 = temp -5.6°C, dewpoint -13.9°C
function parseMetarTempPrecise(metar) {
    const match = metar.match(/T(\d)(\d{3})(\d)(\d{3})/);
    if (match) {
        const tempSign = match[1] === '1' ? -1 : 1;
        const tempTenths = parseInt(match[2]);
        return { tempC: tempSign * tempTenths / 10, isPrecise: true };
    }
    return null;
}

// Parse 6-hour MAX temperature from METAR RMK section
// Format: 1snTTT where s=sign (0=pos, 1=neg), nTTT=temp in tenths °C
// Example: 10056 = +5.6°C max over last 6 hours
// This appears in 00Z, 06Z, 12Z, 18Z reports
function parseSixHourMax(metar) {
    // Look in RMK section for 1xxxx group (but NOT 10/ which is sky condition)
    const rmkMatch = metar.match(/RMK.*?\s1(\d)(\d{3})(?:\s|$)/);
    if (rmkMatch) {
        const sign = rmkMatch[1] === '1' ? -1 : 1;
        const tempTenths = parseInt(rmkMatch[2]);
        const tempC = sign * tempTenths / 10;
        return { tempC, tempF: celsiusToFahrenheit(tempC), source: '6hr_max' };
    }
    return null;
}

// Parse 6-hour MIN temperature from METAR RMK section  
// Format: 2snTTT where s=sign (0=pos, 1=neg), nTTT=temp in tenths °C
// Example: 21012 = -1.2°C min over last 6 hours
// This appears in 00Z, 06Z, 12Z, 18Z reports
function parseSixHourMin(metar) {
    const rmkMatch = metar.match(/RMK.*?\s2(\d)(\d{3})(?:\s|$)/);
    if (rmkMatch) {
        const sign = rmkMatch[1] === '1' ? -1 : 1;
        const tempTenths = parseInt(rmkMatch[2]);
        const tempC = sign * tempTenths / 10;
        return { tempC, tempF: celsiusToFahrenheit(tempC), source: '6hr_min' };
    }
    return null;
}

// Parse 24-hour MAX/MIN from METAR RMK section
// Format: 4snTTTsnTTT where first is max, second is min
// Example: 401001015 = max +10.0°C, min -1.5°C over last 24 hours
// This appears in midnight local reports
function parse24HourMaxMin(metar) {
    const rmkMatch = metar.match(/RMK.*?\s4(\d)(\d{3})(\d)(\d{3})(?:\s|$)/);
    if (rmkMatch) {
        const maxSign = rmkMatch[1] === '1' ? -1 : 1;
        const maxTenths = parseInt(rmkMatch[2]);
        const minSign = rmkMatch[3] === '1' ? -1 : 1;
        const minTenths = parseInt(rmkMatch[4]);
        return {
            max: { tempC: maxSign * maxTenths / 10, tempF: celsiusToFahrenheit(maxSign * maxTenths / 10) },
            min: { tempC: minSign * minTenths / 10, tempF: celsiusToFahrenheit(minSign * minTenths / 10) },
            source: '24hr_maxmin'
        };
    }
    return null;
}

// Parse standard temp from METAR (integer °C)
// Format: TT/DD where M prefix means negative
// Example: M06/M14 = temp -6°C, dewpoint -14°C
function parseMetarTemp(metar) {
    const match = metar.match(/\s(M?\d{2})\/(M?\d{2})[\s\b]/);
    if (match) {
        let tempStr = match[1];
        const tempC = parseInt(tempStr.replace('M', '-'));
        return { tempC, isPrecise: false };
    }
    return null;
}

// Get Fahrenheit value(s) from Celsius reading
// For precise readings (0.1°C), we get a single F value
// For imprecise readings (integer °C), we get a range due to rounding uncertainty
function getTempFahrenheit(tempC, isPrecise) {
    if (isPrecise) {
        // Precise: ±0.05°C uncertainty
        const exactF = tempC * 9/5 + 32;
        const roundedF = nwsRound(exactF);
        // Check if rounding could go either way
        const minF = nwsRound((tempC - 0.05) * 9/5 + 32);
        const maxF = nwsRound((tempC + 0.05) * 9/5 + 32);
        return { 
            tempF: roundedF, 
            minF: minF, 
            maxF: maxF, 
            exactF: exactF,
            isPrecise: true 
        };
    } else {
        // Imprecise: integer °C means actual could be ±0.5°C
        const minF = nwsRound((tempC - 0.5) * 9/5 + 32);
        const maxF = nwsRound((tempC + 0.5) * 9/5 + 32);
        const nominalF = nwsRound(tempC * 9/5 + 32);
        return { 
            tempF: nominalF, 
            minF: minF, 
            maxF: maxF, 
            exactF: tempC * 9/5 + 32,
            isPrecise: false 
        };
    }
}

// =============================================================================
// DATA FETCHING - Aviation Weather API (primary) + NWS Time Series (secondary)
// =============================================================================

// Fetch METAR data from Aviation Weather API
async function fetchAviationWeatherData(cityCode) {
    const config = CITIES[cityCode];
    const localDate = getLocalMarketDate(cityCode);
    
    try {
        // Fetch recent METARs (last 24 hours)
        const url = `https://aviationweather.gov/api/data/metar?ids=${config.icao}&format=json&hours=24`;
        const response = await fetch(url);
        const data = await response.json();
        
        if (!Array.isArray(data) || data.length === 0) {
            console.log(`No Aviation Weather data for ${cityCode}, falling back to Iowa State`);
            return fetchIowaStateData(cityCode);
        }
        
        const readings = [];
        
        for (const obs of data) {
            const rawOb = obs.rawOb;
            if (!rawOb) continue;
            
            // Parse observation time
            const obsTime = new Date(obs.obsTime * 1000); // Unix timestamp
            const readingLocalDate = utcToLocalDate(obsTime, cityCode);
            
            if (readingLocalDate !== localDate) continue;
            
            // Parse temperature from raw METAR (we do it ourselves, don't trust their maxT/minT)
            let parsed = parseMetarTempPrecise(rawOb);
            if (!parsed) parsed = parseMetarTemp(rawOb);
            if (!parsed) continue;
            
            const fData = getTempFahrenheit(parsed.tempC, parsed.isPrecise);
            const offset = isDST(obsTime) ? config.dstOffset : config.utcOffset;
            const localTime = new Date(obsTime.getTime() + offset * 60 * 60 * 1000);
            
            readings.push({
                timeUTC: obsTime.toISOString(),
                timeLocal: localTime.toISOString().substring(11, 16),
                tempC: parsed.tempC,
                isPrecise: parsed.isPrecise,
                tempF: fData.tempF,
                minF: fData.minF,
                maxF: fData.maxF,
                exactF: fData.exactF,
                source: 'aviation'
            });
        }
        
        return { readings, localDate, cityConfig: config, source: 'aviationweather.gov' };
        
    } catch (error) {
        console.error(`Aviation Weather API error for ${cityCode}:`, error.message);
        return fetchIowaStateData(cityCode);
    }
}

// Fallback: Fetch METAR data from Iowa State
async function fetchIowaStateData(cityCode) {
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
        
        const fData = getTempFahrenheit(parsed.tempC, parsed.isPrecise);
        const offset = isDST(utcTime) ? config.dstOffset : config.utcOffset;
        const localTime = new Date(utcTime.getTime() + offset * 60 * 60 * 1000);
        
        readings.push({
            timeUTC: timeStr,
            timeLocal: localTime.toISOString().substring(11, 16),
            tempC: parsed.tempC,
            isPrecise: parsed.isPrecise,
            tempF: fData.tempF,
            minF: fData.minF,
            maxF: fData.maxF,
            exactF: fData.exactF,
            source: 'iowa'
        });
    }
    
    return { readings, localDate, cityConfig: config, source: 'mesonet.agron.iastate.edu' };
}

// Fetch NWS Time Series data (for 6-hour max/min verification)
// This scrapes weather.gov/wrh/timeseries?site=XXXX
async function fetchNWSTimeSeries(cityCode) {
    const config = CITIES[cityCode];
    
    try {
        // NWS time series page - we'll parse the HTML for the data table
        const url = `https://www.weather.gov/wrh/timeseries?site=${config.nwsSite.toLowerCase()}`;
        const response = await fetch(url);
        const html = await response.text();
        
        // Extract 6hr max/min from the table if available
        // Look for "6 Hr Max" and "6 Hr Min" columns
        const maxMatch = html.match(/6 Hr\s*Max[^<]*<\/th>[\s\S]*?<td[^>]*>(\d+)<\/td>/i);
        const minMatch = html.match(/6 Hr\s*Min[^<]*<\/th>[\s\S]*?<td[^>]*>(\d+)<\/td>/i);
        
        // Also try to get the most recent temp reading
        // The table format varies, so this is best-effort
        const tempMatches = [];
        const tempRegex = /<td[^>]*>(\d{1,3})<\/td>/g;
        let match;
        while ((match = tempRegex.exec(html)) !== null) {
            const temp = parseInt(match[1]);
            if (temp > -50 && temp < 150) { // Reasonable temp range in F
                tempMatches.push(temp);
            }
        }
        
        return {
            sixHourMax: maxMatch ? parseInt(maxMatch[1]) : null,
            sixHourMin: minMatch ? parseInt(minMatch[1]) : null,
            recentTemps: tempMatches.slice(0, 10), // First 10 reasonable temps found
            source: 'weather.gov',
            url: url
        };
        
    } catch (error) {
        console.error(`NWS Time Series error for ${cityCode}:`, error.message);
        return { sixHourMax: null, sixHourMin: null, error: error.message };
    }
}

// Fetch from NWS API (api.weather.gov) - gives precise temps and raw METAR
async function fetchNWSAPI(cityCode) {
    const config = CITIES[cityCode];
    const localDate = getLocalMarketDate(cityCode);
    
    try {
        // Fetch recent observations from NWS API
        const url = `https://api.weather.gov/stations/${config.icao}/observations?limit=48`;
        const response = await fetch(url, {
            headers: {
                'User-Agent': 'KalshiWeatherEdge/1.0 (weather trading app)',
                'Accept': 'application/geo+json'
            }
        });
        
        if (!response.ok) {
            console.log(`NWS API returned ${response.status} for ${cityCode}`);
            return null;
        }
        
        const data = await response.json();
        const features = data.features || [];
        
        const readings = [];
        let sixHourMaxF = null;
        let sixHourMinF = null;
        
        for (const feature of features) {
            const props = feature.properties;
            if (!props || !props.timestamp) continue;
            
            const obsTime = new Date(props.timestamp);
            const readingLocalDate = utcToLocalDate(obsTime, cityCode);
            
            // Only include readings from today (market date)
            if (readingLocalDate !== localDate) continue;
            
            // Parse temperature - prefer raw METAR for precision
            let tempC = null;
            let isPrecise = false;
            
            if (props.rawMessage) {
                // Try T-group first for 0.1°C precision
                const precise = parseMetarTempPrecise(props.rawMessage);
                if (precise) {
                    tempC = precise.tempC;
                    isPrecise = true;
                }
                
                // Check for 6-hour max/min in RMK section
                const sixMax = parseSixHourMax(props.rawMessage);
                const sixMin = parseSixHourMin(props.rawMessage);
                
                if (sixMax && (sixHourMaxF === null || sixMax.tempF > sixHourMaxF)) {
                    sixHourMaxF = sixMax.tempF;
                    console.log(`[${cityCode}] 6-hour MAX from METAR: ${sixMax.tempC}°C = ${sixMax.tempF}°F`);
                }
                if (sixMin && (sixHourMinF === null || sixMin.tempF < sixHourMinF)) {
                    sixHourMinF = sixMin.tempF;
                    console.log(`[${cityCode}] 6-hour MIN from METAR: ${sixMin.tempC}°C = ${sixMin.tempF}°F`);
                }
            }
            
            // Fallback to API-provided temp (also precise from NWS)
            if (tempC === null && props.temperature && props.temperature.value !== null) {
                tempC = props.temperature.value;
                isPrecise = true; // NWS API gives precise values
            }
            
            if (tempC === null) continue;
            
            const fData = getTempFahrenheit(tempC, isPrecise);
            const offset = isDST(obsTime) ? config.dstOffset : config.utcOffset;
            const localTime = new Date(obsTime.getTime() + offset * 60 * 60 * 1000);
            
            readings.push({
                timeUTC: obsTime.toISOString(),
                timeLocal: localTime.toISOString().substring(11, 16),
                tempC: tempC,
                isPrecise: isPrecise,
                tempF: fData.tempF,
                minF: fData.minF,
                maxF: fData.maxF,
                exactF: fData.exactF,
                rawMessage: props.rawMessage,
                source: 'nws_api'
            });
        }
        
        return { 
            readings, 
            localDate, 
            cityConfig: config, 
            source: 'api.weather.gov',
            sixHourMaxF,
            sixHourMinF
        };
        
    } catch (error) {
        console.error(`NWS API error for ${cityCode}:`, error.message);
        return null;
    }
}

// Primary data fetch function - combines sources
async function fetchMetarData(cityCode) {
    // Try NWS API first (most authoritative, has raw METAR with 6-hr data)
    const nwsResult = await fetchNWSAPI(cityCode);
    if (nwsResult && nwsResult.readings.length > 0) {
        return nwsResult;
    }
    
    // Fallback to Aviation Weather API
    const result = await fetchAviationWeatherData(cityCode);
    return result;
}

// Fetch Kalshi markets for a series - filtered to today's markets only
async function fetchKalshiMarkets(seriesTicker, localDate) {
    if (!seriesTicker) return [];
    
    try {
        // Get all markets in the series
        const url = `https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=${seriesTicker}&limit=200`;
        const response = await fetch(url);
        const data = await response.json();
        const allMarkets = data.markets || [];
        
        // Filter to only today's markets
        // Market tickers look like: KXHIGHNY-26JAN26-T27 or KXHIGHNY-26JAN26-B26
        // We want to match the date part
        if (!localDate) return allMarkets;
        
        // Convert localDate (YYYY-MM-DD) to Kalshi format (DDMMMYY or DMMMYY)
        const dateParts = localDate.split('-');
        const year = dateParts[0].slice(2); // "26" from "2026"
        const months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
        const month = months[parseInt(dateParts[1]) - 1];
        const day = parseInt(dateParts[2]).toString(); // Remove leading zero: "07" -> "7"
        const kalshiDate = `${day}${month}${year}`; // e.g., "7JAN26" or "27JAN26"
        
        console.log(`[Kalshi] Filtering ${seriesTicker} markets for date: ${kalshiDate} (from localDate: ${localDate})`);
        
        const todayMarkets = allMarkets.filter(m => {
            // Check if the market ticker contains today's date
            const matches = m.ticker && m.ticker.includes(kalshiDate);
            if (!matches && m.ticker) {
                // Debug: log first few non-matching tickers
                console.log(`[Kalshi] Ticker ${m.ticker} does not match ${kalshiDate}`);
            }
            return matches;
        });
        
        console.log(`[Kalshi] Found ${todayMarkets.length} markets for today (${allMarkets.length} total)`);
        
        return todayMarkets;
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
        icao: config.icao,
        hasLow: config.hasLow
    }));
    res.json(cities);
});

// Parse bracket info from Kalshi market subtitle
function parseBracket(subtitle) {
    let low = null;
    let high = null;
    
    // "45° to 46°" or "45 to 46"
    const rangeMatch = subtitle.match(/(\d+)°?\s*to\s*(\d+)°?/i);
    
    // "≤44°" or "44° or below" or "below 44"
    // Try "X or below" format first (number before keyword)
    const underMatchA = subtitle.match(/(\d+)°?\s*or\s*below/i);
    // Then try "≤X" or "below X" format (keyword before number)
    const underMatchB = subtitle.match(/([≤<]|below)\s*(\d+)°?/i);
    
    // "≥50°" or "50° or above" or "above 50"
    // Try "X or above" format first (number before keyword)
    const overMatchA = subtitle.match(/(\d+)°?\s*or\s*above/i);
    // Then try "≥X" or "above X" format (keyword before number)
    const overMatchB = subtitle.match(/([≥>]|above)\s*(\d+)°?/i);
    
    if (rangeMatch) {
        low = parseInt(rangeMatch[1]);
        high = parseInt(rangeMatch[2]);
    } else if (underMatchA) {
        // "57° or below" - number is in group 1
        high = parseInt(underMatchA[1]);
        low = null;
    } else if (underMatchB) {
        // "≤57" or "below 57" - number is in group 2
        high = parseInt(underMatchB[2]);
        low = null;
    } else if (overMatchA) {
        // "66° or above" - number is in group 1
        low = parseInt(overMatchA[1]);
        high = null;
    } else if (overMatchB) {
        // "≥66" or "above 66" - number is in group 2
        low = parseInt(overMatchB[2]);
        high = null;
    }
    
    return { low, high };
}

// Analyze brackets for HIGH temp market
function analyzeHighBrackets(markets, observedHighFloor, observedHighCeiling) {
    return markets.map(market => {
        const subtitle = market.yes_sub_title || market.title || '';
        const { low, high } = parseBracket(subtitle);
        
        // Check if market is still tradeable
        const marketStatus = market.status || 'unknown';
        const isSettled = market.result && market.result !== '';
        const isTradeable = marketStatus === 'active' && !isSettled;
        
        // For HIGH temp markets:
        // The question is "what will the HIGH temperature be?"
        // - "X or below" means high ≤ X → DEAD if observedFloor > X
        // - "X to Y" means high is between X and Y → DEAD if observedFloor > Y
        // - "X or above" means high ≥ X → DEAD if... actually never dead during the day, 
        //   only dead at end of day if final high < X
        
        let status = 'POSSIBLE';
        let isDead = false;
        
        // If market is settled, mark based on result
        if (isSettled) {
            status = market.result === 'yes' ? 'WON' : 'LOST';
            isDead = market.result === 'no';
        } else if (observedHighFloor !== null && high !== null && observedHighFloor > high) {
            // We've already exceeded the bracket's ceiling
            // e.g., floor is 86°F, bracket is "65° or below" (high=65) → DEAD
            // e.g., floor is 86°F, bracket is "66° to 67°" (high=67) → DEAD
            status = 'DEAD';
            isDead = true;
        } else if (observedHighFloor !== null && low !== null && high === null && observedHighFloor >= low) {
            // "X or above" bracket and we've already hit X → this could win
            status = 'IN_RANGE';
        } else if (observedHighFloor !== null && low !== null && high !== null && 
                   observedHighFloor >= low && observedHighFloor <= high) {
            // Range bracket and current floor is within range
            status = 'IN_RANGE';
        }
        
        // Edge detection for HIGH markets:
        // - YES edge: bracket is dead but YES still has active bid (actual opportunity)
        // - Only flag if market is still tradeable (not settled/closed)
        const yesPrice = market.yes_bid || 0;
        const lastPrice = market.last_price || 0;
        // Only flag edge if there's an ACTIVE BID we can sell into, not just historical price
        const hasYesEdge = isDead && isTradeable && yesPrice > 2;
        
        return {
            ticker: market.ticker,
            title: market.title,
            subtitle: market.yes_sub_title,
            low,
            high,
            yesPrice: yesPrice,
            yesPriceAsk: market.yes_ask || 0,
            noPrice: market.no_bid || 0,
            noPriceAsk: market.no_ask || 0,
            lastPrice: lastPrice,
            volume: market.volume || 0,
            status,
            isDead,
            hasYesEdge,
            isTradeable,
            marketStatus,
            isSettled,
            marketType: 'HIGH'
        };
    });
}

// Analyze brackets for LOW temp market
function analyzeLowBrackets(markets, observedLowCeiling, observedLowFloor) {
    return markets.map(market => {
        const subtitle = market.yes_sub_title || market.title || '';
        const { low, high } = parseBracket(subtitle);
        
        // Check if market is still tradeable
        const marketStatus = market.status || 'unknown';
        const isSettled = market.result && market.result !== '';
        const isTradeable = marketStatus === 'active' && !isSettled;
        
        // For LOW markets:
        // - A bracket is DEAD if observedLowCeiling < bracket.low (we've already gone below it)
        // - The LOW can only go DOWN throughout the day, never up
        
        let status = 'POSSIBLE';
        let isDead = false;
        
        // If market is settled, mark based on result
        if (isSettled) {
            status = market.result === 'yes' ? 'WON' : 'LOST';
            isDead = market.result === 'no';
        } else if (observedLowCeiling !== null && low !== null && observedLowCeiling < low) {
            status = 'DEAD';
            isDead = true;
        } else if (observedLowCeiling !== null && high !== null && observedLowCeiling <= high) {
            status = 'IN_RANGE';
        }
        
        // Edge detection for LOW markets:
        // - YES edge: bracket is dead (low already went below it) but YES still has active bid
        // - Only flag if market is still tradeable
        const yesPrice = market.yes_bid || 0;
        const lastPrice = market.last_price || 0;
        const hasYesEdge = isDead && isTradeable && yesPrice > 2;
        
        return {
            ticker: market.ticker,
            title: market.title,
            subtitle: market.yes_sub_title,
            low,
            high,
            yesPrice: yesPrice,
            yesPriceAsk: market.yes_ask || 0,
            noPrice: market.no_bid || 0,
            noPriceAsk: market.no_ask || 0,
            lastPrice: lastPrice,
            volume: market.volume || 0,
            status,
            isDead,
            hasYesEdge,
            isTradeable,
            marketStatus,
            isSettled,
            marketType: 'LOW'
        };
    });
}

// Get data for a specific city
app.get('/api/city/:code', async (req, res) => {
    const cityCode = req.params.code.toUpperCase();
    
    if (!CITIES[cityCode]) {
        return res.status(404).json({ error: 'City not found' });
    }
    
    try {
        // Fetch METAR data (includes 6-hour max/min from RMK section)
        const { readings, localDate, cityConfig, source, sixHourMaxF, sixHourMinF } = await fetchMetarData(cityCode);
        
        // Fetch Kalshi markets for HIGH (filtered to today only)
        const highMarkets = await fetchKalshiMarkets(cityConfig.kalshiHighSeries, localDate);
        
        // Fetch Kalshi markets for LOW (if available, filtered to today only)
        const lowMarkets = cityConfig.hasLow ? await fetchKalshiMarkets(cityConfig.kalshiLowSeries, localDate) : [];
        
        // Calculate observed HIGH floor/ceiling (for high temp market)
        // observedHighFloor = highest confirmed minimum (temp definitely reached at least this)
        // observedHighCeiling = highest possible max (temp might have reached this)
        let observedHighFloor = null;
        let observedHighCeiling = null;
        let maxReading = null;
        
        if (readings.length > 0) {
            // For HIGH: floor is the highest minF we've seen (definitely hit this)
            maxReading = readings.reduce((max, r) => r.minF > max.minF ? r : max);
            observedHighFloor = maxReading.minF;
            observedHighCeiling = readings.reduce((max, r) => r.maxF > max.maxF ? r : max).maxF;
        }
        
        // 6-HOUR MAX IS AUTHORITATIVE - if we have it, use it as the floor!
        // This is the "gold standard" that Kalshi settles on
        if (sixHourMaxF !== null && (observedHighFloor === null || sixHourMaxF > observedHighFloor)) {
            console.log(`[${cityCode}] Using 6-hour max ${sixHourMaxF}°F as HIGH floor (was ${observedHighFloor})`);
            observedHighFloor = sixHourMaxF;
        }
        
        // Calculate observed LOW floor/ceiling (for low temp market)
        // observedLowCeiling = lowest confirmed maximum (temp definitely dropped to at least this)
        // observedLowFloor = lowest possible min (temp might have dropped to this)
        let observedLowCeiling = null;
        let observedLowFloor = null;
        let minReading = null;
        
        if (readings.length > 0) {
            // For LOW: ceiling is the lowest maxF we've seen (definitely hit this low)
            minReading = readings.reduce((min, r) => r.maxF < min.maxF ? r : min);
            observedLowCeiling = minReading.maxF;
            observedLowFloor = readings.reduce((min, r) => r.minF < min.minF ? r : min).minF;
        }
        
        // 6-HOUR MIN IS AUTHORITATIVE for LOW markets
        if (sixHourMinF !== null && (observedLowCeiling === null || sixHourMinF < observedLowCeiling)) {
            console.log(`[${cityCode}] Using 6-hour min ${sixHourMinF}°F as LOW ceiling (was ${observedLowCeiling})`);
            observedLowCeiling = sixHourMinF;
        }
        
        // Analyze brackets
        const highBrackets = analyzeHighBrackets(highMarkets, observedHighFloor, observedHighCeiling);
        const lowBrackets = analyzeLowBrackets(lowMarkets, observedLowCeiling, observedLowFloor);
        
        // Sort brackets by temperature (use high for "X or below", low for "X or above", low for ranges)
        // This puts them in logical order: lowest temps first
        highBrackets.sort((a, b) => {
            const aVal = a.high !== null ? a.high : (a.low !== null ? a.low : 0);
            const bVal = b.high !== null ? b.high : (b.low !== null ? b.low : 0);
            return aVal - bVal;
        });
        lowBrackets.sort((a, b) => {
            const aVal = a.high !== null ? a.high : (a.low !== null ? a.low : 0);
            const bVal = b.high !== null ? b.high : (b.low !== null ? b.low : 0);
            return aVal - bVal;
        });
        
        res.json({
            city: cityCode,
            cityName: cityConfig.name,
            localDate,
            localTime: new Date().toLocaleTimeString('en-US', { timeZone: cityConfig.tz }),
            tzAbbrev: getTzAbbrev(cityConfig.tz),
            tz: cityConfig.tz,  // Full timezone for frontend formatting
            dataSource: source,
            
            // HIGH market data
            high: {
                observedFloor: observedHighFloor,
                observedCeiling: observedHighCeiling,
                sixHourMax: sixHourMaxF,
                maxReading,
                brackets: highBrackets,
                edgeCount: highBrackets.filter(b => b.hasYesEdge).length
            },
            
            // LOW market data (if available)
            low: cityConfig.hasLow ? {
                observedCeiling: observedLowCeiling,
                observedFloor: observedLowFloor,
                sixHourMin: sixHourMinF,
                minReading,
                brackets: lowBrackets,
                edgeCount: lowBrackets.filter(b => b.hasYesEdge).length
            } : null,
            
            hasLow: cityConfig.hasLow,
            readingsCount: readings.length,
            readings: readings.slice(-20).reverse(), // Last 20, newest first
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
            const highMarkets = await fetchKalshiMarkets(config.kalshiHighSeries, localDate);
            const lowMarkets = config.hasLow ? await fetchKalshiMarkets(config.kalshiLowSeries, localDate) : [];
            
            let observedHighFloor = null;
            let observedLowCeiling = null;
            
            if (readings.length > 0) {
                observedHighFloor = readings.reduce((max, r) => r.minF > max.minF ? r : max).minF;
                observedLowCeiling = readings.reduce((min, r) => r.maxF < min.maxF ? r : min).maxF;
            }
            
            // Count edges in HIGH markets
            let highEdgeCount = 0;
            for (const market of highMarkets) {
                const { high } = parseBracket(market.yes_sub_title || '');
                if (observedHighFloor !== null && high !== null && observedHighFloor > high) {
                    if ((market.yes_bid > 2) || (market.last_price > 2)) {
                        highEdgeCount++;
                    }
                }
            }
            
            // Count edges in LOW markets
            let lowEdgeCount = 0;
            for (const market of lowMarkets) {
                const { low } = parseBracket(market.yes_sub_title || '');
                if (observedLowCeiling !== null && low !== null && observedLowCeiling < low) {
                    if ((market.yes_bid > 2) || (market.last_price > 2)) {
                        lowEdgeCount++;
                    }
                }
            }
            
            results.push({
                code,
                name: config.name,
                localDate,
                observedHighFloor,
                observedLowCeiling: config.hasLow ? observedLowCeiling : null,
                readingsCount: readings.length,
                highMarketsCount: highMarkets.length,
                lowMarketsCount: lowMarkets.length,
                highEdgeCount,
                lowEdgeCount,
                totalEdgeCount: highEdgeCount + lowEdgeCount,
                hasLow: config.hasLow
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
// WETHR.NET FORECAST API (with caching to avoid rate limits)
// =============================================================================

// Wethr.net API key (set via environment variable)
const WETHR_API_KEY = process.env.WETHR_API_KEY || '';

// Cache for forecasts: { cityCode: { data: {...}, fetchedAt: timestamp } }
const forecastCache = {};
const FORECAST_CACHE_TTL = 30 * 60 * 1000; // 30 minutes in milliseconds

// Fetch forecast from wethr.net (with caching)
async function fetchWethrForecast(cityCode) {
    const config = CITIES[cityCode];
    if (!config) return null;
    
    // Check cache first
    const cached = forecastCache[cityCode];
    if (cached && (Date.now() - cached.fetchedAt) < FORECAST_CACHE_TTL) {
        console.log(`[Wethr] Using cached forecast for ${cityCode} (${Math.round((Date.now() - cached.fetchedAt) / 1000 / 60)}min old)`);
        return cached.data;
    }
    
    // Use icao code for wethr station (they match for US stations)
    const stationCode = config.wethrStation || config.icao;
    
    if (!WETHR_API_KEY) {
        console.log('WETHR_API_KEY not set, skipping forecast');
        return null;
    }
    
    try {
        console.log(`[Wethr] Fetching fresh forecast for ${cityCode}...`);
        
        // Fetch wethr high/low calculation using NWS logic (matches Kalshi settlement)
        const url = `https://wethr.net/api/v2/observations.php?station_code=${stationCode}&mode=wethr_high&logic=nws`;
        
        const response = await fetch(url, {
            headers: {
                'Authorization': `Bearer ${WETHR_API_KEY}`,
                'Accept': 'application/json'
            }
        });
        
        if (!response.ok) {
            console.log(`Wethr API returned ${response.status} for ${cityCode}`);
            return null;
        }
        
        const data = await response.json();
        
        // Also try to get NWS forecast for future high/low
        let nwsForecast = null;
        try {
            const nwsUrl = `https://wethr.net/api/v2/nws_forecasts.php?station_code=${stationCode}`;
            const nwsResponse = await fetch(nwsUrl, {
                headers: {
                    'Authorization': `Bearer ${WETHR_API_KEY}`,
                    'Accept': 'application/json'
                }
            });
            if (nwsResponse.ok) {
                nwsForecast = await nwsResponse.json();
            }
        } catch (e) {
            console.log('NWS forecast fetch error:', e.message);
        }
        
        const forecastData = {
            date: data.date,
            high: data.wethr_high,
            low: data.wethr_low,
            forecastHigh: nwsForecast?.high,
            forecastLow: nwsForecast?.low,
            timeOfHighUTC: data.time_of_high_utc,
            timeOfLowUTC: data.time_of_low_utc,
            calculationLogic: data.calculation_logic,
            units: data.units,
            source: 'wethr.net'
        };
        
        // Store in cache
        forecastCache[cityCode] = {
            data: forecastData,
            fetchedAt: Date.now()
        };
        
        return forecastData;
        
    } catch (error) {
        console.error(`Wethr API error for ${cityCode}:`, error.message);
        return null;
    }
}

// Forecast endpoint
app.get('/api/forecast/:cityCode', async (req, res) => {
    const cityCode = req.params.cityCode.toUpperCase();
    
    if (!CITIES[cityCode]) {
        return res.status(404).json({ error: 'City not found' });
    }
    
    const forecast = await fetchWethrForecast(cityCode);
    
    if (!forecast) {
        return res.status(503).json({ error: 'Forecast not available' });
    }
    
    res.json(forecast);
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
