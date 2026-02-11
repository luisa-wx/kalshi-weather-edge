"""
NWS product parsers for resolving temperature data.

Parses DSM, METAR (T-groups, 6-hour max/min), and SPECI products
to extract high-precision temperature readings that resolve rounding ambiguity.

Product types and their precision:
    DSM:     Whole °F (directly resolves, no conversion needed)
    T-group: Tenths °C -> convert to °F with nws_round (HIGH precision)
    6-hour:  Tenths °C -> convert to °F with nws_round (HIGH precision)
    SPECI:   Same as METAR, may contain T-group
"""

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from nws_math import nws_round, c_to_f_nws

logger = logging.getLogger(__name__)


class ProductType(Enum):
    DSM = "dsm"
    METAR = "metar"
    SPECI = "speci"
    CLI = "cli"
    UNKNOWN = "unknown"


class DataSource(Enum):
    """How the temperature was extracted - determines confidence."""
    DSM_HIGH = "dsm_high"
    DSM_LOW = "dsm_low"
    METAR_T_GROUP = "metar_t_group"
    METAR_6HR_MAX = "metar_6hr_max"    # 1-group
    METAR_6HR_MIN = "metar_6hr_min"    # 2-group
    METAR_24HR_MAX = "metar_24hr_max"  # 4-group
    METAR_24HR_MIN = "metar_24hr_min"  # 4-group
    SPECI_T_GROUP = "speci_t_group"
    CLI_HIGH = "cli_high"
    CLI_LOW = "cli_low"


@dataclass
class ParsedTemperature:
    """A temperature reading extracted from an NWS product."""
    temp_f: int                    # Final temperature in °F (NWS-rounded)
    temp_c: float | None = None    # Original °C value if available
    source: DataSource = DataSource.DSM_HIGH
    station: str = ""              # ICAO station code (e.g., KNYC)
    product_type: ProductType = ProductType.UNKNOWN
    raw_text: str = ""             # The raw product text for debugging
    issue_time: datetime | None = None
    precision: str = "high"        # "high" (tenths °C) or "whole_f" (DSM)
    time_of_occurrence: str = ""   # HHMM LST for DSM temps


@dataclass
class ProductParseResult:
    """All temperature data extracted from a single NWS product."""
    product_type: ProductType
    station: str
    issue_time: datetime | None = None
    awipsid: str = ""
    cccc: str = ""
    temperatures: list[ParsedTemperature] = field(default_factory=list)
    raw_text: str = ""
    parse_errors: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return len(self.temperatures) > 0


# ---------------------------------------------------------------------------
# DSM PARSER
# ---------------------------------------------------------------------------
# DSM format example (from IEM archive):
#   KNYC DS 1600 09/02 311559/ 100622// 31/ 10//...
#
# Structure: {STATION} DS {TIME} {MM/DD} {HIGH}{TIME_HIGH}/ {LOW}{TIME_LOW}//
# HIGH and LOW are in whole °F. TIME is HHMM in LST.
#
# The DS line can also look like:
#   KNYC DS 1600 09/02 311559/ 100622// 31/ 10//0181559/...
# or for midnight DSM (full day):
#   KNYC DS 09/02 311559/ 100622// 31/ 10//...

# Pattern to extract the core DSM data line
# Matches: KXXX DS [optional time] MM/DD HIGH_TEMP TIME/ LOW_TEMP TIME//
DSM_PATTERN = re.compile(
    r'(?P<station>K[A-Z]{3})\s+DS\s+'
    r'(?:\d{4}\s+)?'                        # optional HHMM time
    r'(?P<month>\d{2})/(?P<day>\d{2})\s+'
    r'(?P<high>-?\d{1,3})(?P<high_time>\d{4})/'  # high + HHMM
    r'\s*(?P<low>-?\d{1,3})(?P<low_time>\d{4})'  # low + HHMM
)

# Some DSMs have the format with M for missing:
# KNYC DS 1600 09/02 M/ M// M/ M//
DSM_MISSING_PATTERN = re.compile(
    r'(?P<station>K[A-Z]{3})\s+DS\s+'
    r'(?:\d{4}\s+)?'
    r'(?P<month>\d{2})/(?P<day>\d{2})\s+'
    r'M'
)


def parse_dsm(raw_text: str, station_hint: str = "",
              issue_time: datetime | None = None) -> ProductParseResult:
    """
    Parse a DSM (Daily Summary Message) product.

    DSM reports running daily high/low in whole °F. This directly resolves
    the rounding ambiguity from 5-min data.

    Args:
        raw_text: The raw DSM product text
        station_hint: Expected station code (for validation)
        issue_time: When the product was issued (UTC)

    Returns:
        ProductParseResult with extracted temperatures
    """
    result = ProductParseResult(
        product_type=ProductType.DSM,
        station=station_hint,
        issue_time=issue_time,
        raw_text=raw_text,
    )

    match = DSM_PATTERN.search(raw_text)
    if not match:
        # Check if it's a "missing" DSM
        if DSM_MISSING_PATTERN.search(raw_text):
            result.parse_errors.append("DSM contains missing (M) values")
        else:
            result.parse_errors.append(f"Could not parse DSM format from text")
        return result

    station = match.group("station")
    result.station = station

    try:
        high_f = int(match.group("high"))
        high_time = match.group("high_time")
        result.temperatures.append(ParsedTemperature(
            temp_f=high_f,
            source=DataSource.DSM_HIGH,
            station=station,
            product_type=ProductType.DSM,
            raw_text=raw_text[:200],
            issue_time=issue_time,
            precision="whole_f",
            time_of_occurrence=high_time,
        ))
    except (ValueError, IndexError) as e:
        result.parse_errors.append(f"Failed to parse DSM high: {e}")

    try:
        low_f = int(match.group("low"))
        low_time = match.group("low_time")
        result.temperatures.append(ParsedTemperature(
            temp_f=low_f,
            source=DataSource.DSM_LOW,
            station=station,
            product_type=ProductType.DSM,
            raw_text=raw_text[:200],
            issue_time=issue_time,
            precision="whole_f",
            time_of_occurrence=low_time,
        ))
    except (ValueError, IndexError) as e:
        result.parse_errors.append(f"Failed to parse DSM low: {e}")

    if result.has_data:
        logger.info(
            f"DSM parsed: {station} high={result.temperatures[0].temp_f}°F "
            f"low={result.temperatures[1].temp_f if len(result.temperatures) > 1 else '?'}°F"
        )

    return result


# ---------------------------------------------------------------------------
# METAR / SPECI PARSER
# ---------------------------------------------------------------------------
# T-group format: T{sign1}{TTT}{sign2}{DDD}
#   sign: 0=positive, 1=negative
#   TTT/DDD: tenths of degree Celsius
# Example: T02220167 -> temp=+22.2°C, dewpoint=+16.7°C
#          T10281000 -> temp=-2.8°C, dewpoint=-0.0°C
T_GROUP_PATTERN = re.compile(r'\bT(\d)(\d{3})(\d)(\d{3})\b')

# 6-hour max (1-group): 1{sign}{TTT}
# Example: 10222 -> +22.2°C max in last 6 hours
# Only valid in REMARKS section, at synoptic times (00Z, 06Z, 12Z, 18Z)
SIX_HR_MAX_PATTERN = re.compile(r'\b1(\d)(\d{3})\b')

# 6-hour min (2-group): 2{sign}{TTT}
# Example: 21028 -> -2.8°C min in last 6 hours
SIX_HR_MIN_PATTERN = re.compile(r'\b2(\d)(\d{3})\b')

# 24-hour max/min (4-group): 4{sign_max}{TTT}{sign_min}{TTT}
# Example: 401001015 -> max=+10.0°C, min=-1.5°C in last 24 hours
TWENTY_FOUR_HR_PATTERN = re.compile(r'\b4(\d)(\d{3})(\d)(\d{3})\b')

# Station pattern in METAR/SPECI header
METAR_STATION_PATTERN = re.compile(r'\b(METAR|SPECI)\s+(K[A-Z]{3})\b')
# Fallback: just look for KXXX near the start
METAR_STATION_FALLBACK = re.compile(r'\b(K[A-Z]{3})\b')


def _decode_tenths_c(sign_digit: str, value_digits: str) -> float:
    """Decode tenths-of-degree Celsius from sign digit + 3-digit value."""
    sign = -1 if sign_digit == '1' else 1
    return sign * int(value_digits) / 10.0


def _extract_remarks_section(raw_text: str) -> str:
    """Extract the REMARKS section from a METAR/SPECI, starting after 'RMK'."""
    rmk_idx = raw_text.find("RMK")
    if rmk_idx == -1:
        return ""
    return raw_text[rmk_idx:]


def parse_metar(raw_text: str, station_hint: str = "",
                issue_time: datetime | None = None,
                is_speci: bool = False) -> ProductParseResult:
    """
    Parse a METAR or SPECI product for high-precision temperature data.

    Extracts:
    - T-groups (tenths °C, HIGH precision)
    - 6-hour max/min (1-group/2-group, tenths °C, synoptic times only)
    - 24-hour max/min (4-group, tenths °C)

    All from the REMARKS section (after RMK).

    Args:
        raw_text: The raw METAR/SPECI product text
        station_hint: Expected station code
        issue_time: When the product was issued (UTC)
        is_speci: True if this is a SPECI observation

    Returns:
        ProductParseResult with extracted temperatures
    """
    ptype = ProductType.SPECI if is_speci else ProductType.METAR
    result = ProductParseResult(
        product_type=ptype,
        station=station_hint,
        issue_time=issue_time,
        raw_text=raw_text,
    )

    # Try to extract station from the text
    station = station_hint
    m = METAR_STATION_PATTERN.search(raw_text)
    if m:
        station = m.group(2)
    elif not station:
        m2 = METAR_STATION_FALLBACK.search(raw_text)
        if m2:
            station = m2.group(1)
    result.station = station

    remarks = _extract_remarks_section(raw_text)
    if not remarks:
        result.parse_errors.append("No RMK section found in METAR/SPECI")
        return result

    # Parse T-group (temperature + dewpoint in tenths °C)
    t_match = T_GROUP_PATTERN.search(remarks)
    if t_match:
        temp_c = _decode_tenths_c(t_match.group(1), t_match.group(2))
        temp_f = c_to_f_nws(temp_c)
        src = DataSource.SPECI_T_GROUP if is_speci else DataSource.METAR_T_GROUP
        result.temperatures.append(ParsedTemperature(
            temp_f=temp_f,
            temp_c=temp_c,
            source=src,
            station=station,
            product_type=ptype,
            raw_text=raw_text[:200],
            issue_time=issue_time,
            precision="high",
        ))
        logger.debug(f"T-group: {temp_c}°C -> {temp_f}°F ({station})")

    # Parse 6-hour max (1-group)
    # IMPORTANT: Only parse from remarks, and be careful not to match
    # other 1xxxx patterns. The 1-group should be a standalone 5-digit group.
    one_match = SIX_HR_MAX_PATTERN.search(remarks)
    if one_match:
        temp_c = _decode_tenths_c(one_match.group(1), one_match.group(2))
        temp_f = c_to_f_nws(temp_c)
        result.temperatures.append(ParsedTemperature(
            temp_f=temp_f,
            temp_c=temp_c,
            source=DataSource.METAR_6HR_MAX,
            station=station,
            product_type=ptype,
            raw_text=raw_text[:200],
            issue_time=issue_time,
            precision="high",
        ))
        logger.info(f"6-hr max: {temp_c}°C -> {temp_f}°F ({station})")

    # Parse 6-hour min (2-group)
    two_match = SIX_HR_MIN_PATTERN.search(remarks)
    if two_match:
        temp_c = _decode_tenths_c(two_match.group(1), two_match.group(2))
        temp_f = c_to_f_nws(temp_c)
        result.temperatures.append(ParsedTemperature(
            temp_f=temp_f,
            temp_c=temp_c,
            source=DataSource.METAR_6HR_MIN,
            station=station,
            product_type=ptype,
            raw_text=raw_text[:200],
            issue_time=issue_time,
            precision="high",
        ))
        logger.info(f"6-hr min: {temp_c}°C -> {temp_f}°F ({station})")

    # Parse 24-hour max/min (4-group)
    four_match = TWENTY_FOUR_HR_PATTERN.search(remarks)
    if four_match:
        max_c = _decode_tenths_c(four_match.group(1), four_match.group(2))
        min_c = _decode_tenths_c(four_match.group(3), four_match.group(4))
        max_f = c_to_f_nws(max_c)
        min_f = c_to_f_nws(min_c)
        result.temperatures.append(ParsedTemperature(
            temp_f=max_f,
            temp_c=max_c,
            source=DataSource.METAR_24HR_MAX,
            station=station,
            product_type=ptype,
            raw_text=raw_text[:200],
            issue_time=issue_time,
            precision="high",
        ))
        result.temperatures.append(ParsedTemperature(
            temp_f=min_f,
            temp_c=min_c,
            source=DataSource.METAR_24HR_MIN,
            station=station,
            product_type=ptype,
            raw_text=raw_text[:200],
            issue_time=issue_time,
            precision="high",
        ))
        logger.info(f"24-hr: max={max_c}°C/{max_f}°F, min={min_c}°C/{min_f}°F ({station})")

    if not result.has_data:
        result.parse_errors.append("No temperature groups found in remarks")

    return result


# ---------------------------------------------------------------------------
# CLI PARSER (for settlement confirmation, not sniping)
# ---------------------------------------------------------------------------
# CLI format varies by WFO but typically includes lines like:
#   TEMPERATURE (F)
#     TODAY
#       MAXIMUM        72
#       MINIMUM        54

CLI_HIGH_PATTERN = re.compile(
    r'MAXIMUM\s+(?:TEMPERATURE\s+)?(\d{1,3})', re.IGNORECASE
)
CLI_LOW_PATTERN = re.compile(
    r'MINIMUM\s+(?:TEMPERATURE\s+)?(\d{1,3})', re.IGNORECASE
)


def parse_cli(raw_text: str, station_hint: str = "",
              issue_time: datetime | None = None) -> ProductParseResult:
    """
    Parse a CLI (Climatological Report) product.

    CLI is the FINAL settlement document. Not used for sniping (drops next morning),
    but useful for confirming settlement and backtesting.
    """
    result = ProductParseResult(
        product_type=ProductType.CLI,
        station=station_hint,
        issue_time=issue_time,
        raw_text=raw_text,
    )

    high_match = CLI_HIGH_PATTERN.search(raw_text)
    if high_match:
        result.temperatures.append(ParsedTemperature(
            temp_f=int(high_match.group(1)),
            source=DataSource.CLI_HIGH,
            station=station_hint,
            product_type=ProductType.CLI,
            raw_text=raw_text[:200],
            issue_time=issue_time,
            precision="whole_f",
        ))

    low_match = CLI_LOW_PATTERN.search(raw_text)
    if low_match:
        result.temperatures.append(ParsedTemperature(
            temp_f=int(low_match.group(1)),
            source=DataSource.CLI_LOW,
            station=station_hint,
            product_type=ProductType.CLI,
            raw_text=raw_text[:200],
            issue_time=issue_time,
            precision="whole_f",
        ))

    return result


# ---------------------------------------------------------------------------
# DISPATCHER - route raw product text to the right parser
# ---------------------------------------------------------------------------

def classify_product(awipsid: str) -> ProductType:
    """Classify an NWS product by its AWIPS ID."""
    if not awipsid:
        return ProductType.UNKNOWN
    base = awipsid[:3].upper()
    if base == "DSM":
        return ProductType.DSM
    elif base == "CLI":
        return ProductType.CLI
    elif base == "MTR" or awipsid.startswith("SA "):
        return ProductType.METAR
    elif base == "SPE":
        return ProductType.SPECI
    return ProductType.UNKNOWN


def parse_product(raw_text: str, awipsid: str = "", cccc: str = "",
                  station_hint: str = "",
                  issue_time: datetime | None = None) -> ProductParseResult:
    """
    Parse any NWS product and extract temperature data.

    This is the main entry point - it classifies the product and routes
    to the appropriate parser.

    Args:
        raw_text: The raw product text from NWWS-OI
        awipsid: The AWIPS/AFOS PIL (e.g., DSMNYC, CLINEW)
        cccc: The issuing center ICAO (e.g., KOKX)
        station_hint: Expected station code
        issue_time: UTC issue time

    Returns:
        ProductParseResult with all extracted temperature data
    """
    ptype = classify_product(awipsid)

    if ptype == ProductType.DSM:
        return parse_dsm(raw_text, station_hint, issue_time)
    elif ptype == ProductType.CLI:
        return parse_cli(raw_text, station_hint, issue_time)
    elif ptype == ProductType.METAR:
        return parse_metar(raw_text, station_hint, issue_time, is_speci=False)
    elif ptype == ProductType.SPECI:
        return parse_metar(raw_text, station_hint, issue_time, is_speci=True)
    else:
        # Try to detect from content
        if " DS " in raw_text and "/" in raw_text:
            return parse_dsm(raw_text, station_hint, issue_time)
        elif "METAR" in raw_text or "SPECI" in raw_text:
            is_speci = "SPECI" in raw_text
            return parse_metar(raw_text, station_hint, issue_time, is_speci)

        result = ProductParseResult(
            product_type=ProductType.UNKNOWN,
            station=station_hint,
            issue_time=issue_time,
            raw_text=raw_text,
        )
        result.parse_errors.append(f"Unknown product type: awipsid={awipsid}")
        return result
