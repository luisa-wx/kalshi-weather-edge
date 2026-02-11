"""
Station configuration for all Kalshi temperature markets.

Maps ICAO station codes to their Kalshi tickers, timezones, and the WFO
(Weather Forecast Office) ICAO codes that issue products for each station.

Updated from project spreadsheet — 20 stations total.
"""

from zoneinfo import ZoneInfo

STATIONS = {
    # --- HIGH + LOW markets ---
    "KNYC": {
        "city": "NYC",
        "high_ticker": "KXHIGHNY",
        "low_ticker": "KXLOWTNYC",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KOKX",
        "dsm_awipsid": "DSMNYC",
        "metar_station": "KNYC",
    },
    "KPHL": {
        "city": "Philadelphia",
        "high_ticker": "KXHIGHPHL",
        "low_ticker": "KXLOWTPHIL",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KPHI",
        "dsm_awipsid": "DSMPHL",
        "metar_station": "KPHL",
    },
    "KMDW": {
        "city": "Chicago",
        "high_ticker": "KXHIGHCHI",
        "low_ticker": "KXLOWTCHI",
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KLOT",
        "dsm_awipsid": "DSMMDW",
        "metar_station": "KMDW",
    },
    "KLAX": {
        "city": "Los Angeles",
        "high_ticker": "KXHIGHLAX",
        "low_ticker": "KXLOWTLAX",
        "timezone": ZoneInfo("America/Los_Angeles"),
        "tz_name": "America/Los_Angeles",
        "wfo_cccc": "KLOX",
        "dsm_awipsid": "DSMLAX",
        "metar_station": "KLAX",
    },
    "KMIA": {
        "city": "Miami",
        "high_ticker": "KXHIGHMIA",
        "low_ticker": "KXLOWTMIA",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KMFL",
        "dsm_awipsid": "DSMMIA",
        "metar_station": "KMIA",
    },
    "KAUS": {
        "city": "Austin",
        "high_ticker": "KXHIGHAUS",
        "low_ticker": "KXLOWTAUS",
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KEWX",
        "dsm_awipsid": "DSMAUS",
        "metar_station": "KAUS",
    },
    "KDEN": {
        "city": "Denver",
        "high_ticker": "KXHIGHDEN",
        "low_ticker": "KXLOWTDEN",
        "timezone": ZoneInfo("America/Denver"),
        "tz_name": "America/Denver",
        "wfo_cccc": "KBOU",
        "dsm_awipsid": "DSMDEN",
        "metar_station": "KDEN",
    },
    # --- HIGH only markets ---
    "KSFO": {
        "city": "San Francisco",
        "high_ticker": "KXHIGHTSFO",
        "low_ticker": None,
        "timezone": ZoneInfo("America/Los_Angeles"),
        "tz_name": "America/Los_Angeles",
        "wfo_cccc": "KMTR",
        "dsm_awipsid": "DSMSFO",
        "metar_station": "KSFO",
    },
    "KSEA": {
        "city": "Seattle",
        "high_ticker": "KXHIGHTSEA",
        "low_ticker": None,
        "timezone": ZoneInfo("America/Los_Angeles"),
        "tz_name": "America/Los_Angeles",
        "wfo_cccc": "KSEW",
        "dsm_awipsid": "DSMSEA",
        "metar_station": "KSEA",
    },
    "KDCA": {
        "city": "Washington DC",
        "high_ticker": "KXHIGHTDC",
        "low_ticker": None,
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KLWX",
        "dsm_awipsid": "DSMDCA",
        "metar_station": "KDCA",
    },
    "KMSY": {
        "city": "New Orleans",
        "high_ticker": "KXHIGHTNOLA",
        "low_ticker": None,
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KLIX",
        "dsm_awipsid": "DSMMSY",
        "metar_station": "KMSY",
    },
    "KLAS": {
        "city": "Las Vegas",
        "high_ticker": "KXHIGHTLV",
        "low_ticker": None,
        "timezone": ZoneInfo("America/Los_Angeles"),
        "tz_name": "America/Los_Angeles",
        "wfo_cccc": "KVEF",
        "dsm_awipsid": "DSMLAS",
        "metar_station": "KLAS",
    },
    "KDFW": {
        "city": "Dallas",
        "high_ticker": "KXHIGHTDAL",
        "low_ticker": None,
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KFWD",
        "dsm_awipsid": "DSMDFW",
        "metar_station": "KDFW",
    },
    "KHOU": {
        "city": "Houston",
        "high_ticker": "KXHIGHTHOU",
        "low_ticker": None,
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KHGX",
        "dsm_awipsid": "DSMHOU",
        "metar_station": "KHOU",
    },
    "KBOS": {
        "city": "Boston",
        "high_ticker": "KXHIGHTBOS",
        "low_ticker": None,
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KBOX",
        "dsm_awipsid": "DSMBOS",
        "metar_station": "KBOS",
    },
    "KMSP": {
        "city": "Minneapolis",
        "high_ticker": "KXHIGHTMIN",
        "low_ticker": None,
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KMPX",
        "dsm_awipsid": "DSMMSP",
        "metar_station": "KMSP",
    },
    "KSAT": {
        "city": "San Antonio",
        "high_ticker": "KXHIGHTSATX",
        "low_ticker": None,
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KEWX",
        "dsm_awipsid": "DSMSAT",
        "metar_station": "KSAT",
    },
    "KOKC": {
        "city": "Oklahoma City",
        "high_ticker": "KXHIGHTOKC",
        "low_ticker": None,
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KOUN",
        "dsm_awipsid": "DSMOKC",
        "metar_station": "KOKC",
    },
    "KPHX": {
        "city": "Phoenix",
        "high_ticker": "KXHIGHTPHX",
        "low_ticker": None,
        "timezone": ZoneInfo("America/Phoenix"),
        "tz_name": "America/Phoenix",
        "wfo_cccc": "KPSR",
        "dsm_awipsid": "DSMPHX",
        "metar_station": "KPHX",
    },
}

# Reverse lookup: DSM awipsid -> station code
DSM_TO_STATION = {v["dsm_awipsid"]: k for k, v in STATIONS.items()}

# Reverse lookup: METAR station -> station code
METAR_TO_STATION = {v["metar_station"]: k for k, v in STATIONS.items()}

# All WFO cccc codes we care about (for NWWS-OI filtering)
WATCHED_WFOS = {v["wfo_cccc"] for v in STATIONS.values()}

# All station codes that have Kalshi markets
ALL_STATIONS = set(STATIONS.keys())
