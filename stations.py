"""
Station configuration for all Kalshi temperature markets.

Maps ICAO station codes to their Kalshi tickers, timezones, and the WFO
(Weather Forecast Office) ICAO codes that issue products for each station.

The WFO cccc is critical for filtering NWWS-OI messages - DSM products
are issued by the WFO, not the station itself. The awipsid for a DSM
is DSM + 3-char station suffix (e.g., DSMNYC for KNYC, DSMMDW for KMDW).
"""

from zoneinfo import ZoneInfo

STATIONS = {
    "KNYC": {
        "city": "New York (Central Park)",
        "high_ticker": "KXHIGHNY",
        "low_ticker": "KXLOWTNYC",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KOKX",       # NWS NYC / Upton
        "dsm_awipsid": "DSMNYC",  # AWIPS PIL for DSM
        "metar_station": "KNYC",
    },
    "KPHL": {
        "city": "Philadelphia",
        "high_ticker": "KXHIGHPHIL",
        "low_ticker": "KXLOWTPHIL",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KPHI",
        "dsm_awipsid": "DSMPHL",
        "metar_station": "KPHL",
    },
    "KMDW": {
        "city": "Chicago Midway",
        "high_ticker": "KXHIGHCHI",
        "low_ticker": "KXLOWTCHI",
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KLOT",
        "dsm_awipsid": "DSMMDW",
        "metar_station": "KMDW",
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
    "KLAX": {
        "city": "Los Angeles",
        "high_ticker": "KXHIGHLA",
        "low_ticker": "KXLOWTLA",
        "timezone": ZoneInfo("America/Los_Angeles"),
        "tz_name": "America/Los_Angeles",
        "wfo_cccc": "KLOX",
        "dsm_awipsid": "DSMLAX",
        "metar_station": "KLAX",
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
    "KDFW": {
        "city": "Dallas/Fort Worth",
        "high_ticker": "KXHIGHDFW",
        "low_ticker": "KXLOWTDFW",
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KFWD",
        "dsm_awipsid": "DSMDFW",
        "metar_station": "KDFW",
    },
    "KHOU": {
        "city": "Houston Hobby",
        "high_ticker": "KXHIGHHOU",
        "low_ticker": "KXLOWTHOU",
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KHGX",
        "dsm_awipsid": "DSMHOU",
        "metar_station": "KHOU",
    },
    "KSEA": {
        "city": "Seattle",
        "high_ticker": "KXHIGHSEA",
        "low_ticker": "KXLOWTSEA",
        "timezone": ZoneInfo("America/Los_Angeles"),
        "tz_name": "America/Los_Angeles",
        "wfo_cccc": "KSEW",
        "dsm_awipsid": "DSMSEA",
        "metar_station": "KSEA",
    },
    "KSFO": {
        "city": "San Francisco",
        "high_ticker": "KXHIGHSFO",
        "low_ticker": "KXLOWTSFO",
        "timezone": ZoneInfo("America/Los_Angeles"),
        "tz_name": "America/Los_Angeles",
        "wfo_cccc": "KMTR",
        "dsm_awipsid": "DSMSFO",
        "metar_station": "KSFO",
    },
    "KLAS": {
        "city": "Las Vegas",
        "high_ticker": "KXHIGHTLV",
        "low_ticker": "KXLOWTTLV",
        "timezone": ZoneInfo("America/Los_Angeles"),
        "tz_name": "America/Los_Angeles",
        "wfo_cccc": "KVEF",
        "dsm_awipsid": "DSMLAS",
        "metar_station": "KLAS",
    },
    "KPHX": {
        "city": "Phoenix",
        "high_ticker": "KXHIGHPHX",
        "low_ticker": "KXLOWTPHX",
        "timezone": ZoneInfo("America/Phoenix"),
        "tz_name": "America/Phoenix",
        "wfo_cccc": "KPSR",
        "dsm_awipsid": "DSMPHX",
        "metar_station": "KPHX",
    },
    "KSAT": {
        "city": "San Antonio",
        "high_ticker": "KXHIGHSAT",
        "low_ticker": "KXLOWTSAT",
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KEWX",
        "dsm_awipsid": "DSMSAT",
        "metar_station": "KSAT",
    },
    "KDCA": {
        "city": "Washington D.C.",
        "high_ticker": "KXHIGHDC",
        "low_ticker": "KXLOWTDC",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KLWX",
        "dsm_awipsid": "DSMDCA",
        "metar_station": "KDCA",
    },
    "KCLT": {
        "city": "Charlotte",
        "high_ticker": "KXHIGHCLT",
        "low_ticker": "KXLOWTCLT",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KGSP",
        "dsm_awipsid": "DSMCLT",
        "metar_station": "KCLT",
    },
    "KBOS": {
        "city": "Boston",
        "high_ticker": "KXHIGHBOS",
        "low_ticker": "KXLOWTBOS",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KBOX",
        "dsm_awipsid": "DSMBOS",
        "metar_station": "KBOS",
    },
    "KBNA": {
        "city": "Nashville",
        "high_ticker": "KXHIGHBNA",
        "low_ticker": "KXLOWTBNA",
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KOHX",
        "dsm_awipsid": "DSMBNA",
        "metar_station": "KBNA",
    },
    "KATL": {
        "city": "Atlanta",
        "high_ticker": "KXHIGHATL",
        "low_ticker": "KXLOWTATL",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KFFC",
        "dsm_awipsid": "DSMATL",
        "metar_station": "KATL",
    },
    "KJAX": {
        "city": "Jacksonville",
        "high_ticker": "KXHIGHJAX",
        "low_ticker": "KXLOWTJAX",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KJAX",
        "dsm_awipsid": "DSMJAX",
        "metar_station": "KJAX",
    },
    "KOKC": {
        "city": "Oklahoma City",
        "high_ticker": "KXHIGHOKC",
        "low_ticker": "KXLOWTOKC",
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KOUN",
        "dsm_awipsid": "DSMOKC",
        "metar_station": "KOKC",
    },
    "KDTW": {
        "city": "Detroit",
        "high_ticker": "KXHIGHDTW",
        "low_ticker": "KXLOWTDTW",
        "timezone": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "wfo_cccc": "KDTX",
        "dsm_awipsid": "DSMDTW",
        "metar_station": "KDTW",
    },
    "KMSP": {
        "city": "Minneapolis",
        "high_ticker": "KXHIGHMSP",
        "low_ticker": "KXLOWTMSP",
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KMPX",
        "dsm_awipsid": "DSMMSP",
        "metar_station": "KMSP",
    },
    "KMSY": {
        "city": "New Orleans",
        "high_ticker": "KXHIGHMSY",
        "low_ticker": "KXLOWTMSY",
        "timezone": ZoneInfo("America/Chicago"),
        "tz_name": "America/Chicago",
        "wfo_cccc": "KLIX",
        "dsm_awipsid": "DSMMSY",
        "metar_station": "KMSY",
    },
}

# Reverse lookup: DSM awipsid -> station code
DSM_TO_STATION = {v["dsm_awipsid"]: k for k, v in STATIONS.items()}

# Reverse lookup: METAR station -> station code (same in most cases)
METAR_TO_STATION = {v["metar_station"]: k for k, v in STATIONS.items()}

# All WFO cccc codes we care about (for NWWS-OI filtering)
WATCHED_WFOS = {v["wfo_cccc"] for v in STATIONS.values()}

# All station codes that have Kalshi markets
ALL_STATIONS = set(STATIONS.keys())
