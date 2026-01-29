"""
METAR Parser - Extract 6-hour max/min temperatures from synoptic METARs

Format:
- 1-group (6hr max): 1snTTT where sn=0 positive, sn=1 negative, TTT=temp in tenths C
- 2-group (6hr min): 2snTTT same format

Example: 10217 = +21.7°C = 71.06°F
Example: 21012 = -1.2°C = 29.84°F
"""

import re
from dataclasses import dataclass
from typing import Optional, Tuple
from datetime import datetime


@dataclass
class MetarTemps:
    """Parsed temperature data from a METAR"""
    raw_metar: str
    station: str
    observation_time: Optional[datetime]
    
    # Current temps (from main body, less precise)
    current_temp_c: Optional[int] = None
    current_dewpoint_c: Optional[int] = None
    
    # 6-hour extremes (from remarks, 0.1°C precision)
    six_hour_max_c: Optional[float] = None  # From 1-group
    six_hour_min_c: Optional[float] = None  # From 2-group
    
    # T-group temps (0.1°C precision, hourly METARs only)
    t_group_temp_c: Optional[float] = None
    t_group_dewpoint_c: Optional[float] = None
    
    # Converted to Fahrenheit
    @property
    def six_hour_max_f(self) -> Optional[float]:
        if self.six_hour_max_c is not None:
            return self.six_hour_max_c * 1.8 + 32
        return None
    
    @property
    def six_hour_min_f(self) -> Optional[float]:
        if self.six_hour_min_c is not None:
            return self.six_hour_min_c * 1.8 + 32
        return None
    
    @property
    def six_hour_max_f_rounded(self) -> Optional[int]:
        """Rounded to nearest whole degree (how Kalshi settles)"""
        if self.six_hour_max_f is not None:
            return round(self.six_hour_max_f)
        return None
    
    @property
    def six_hour_min_f_rounded(self) -> Optional[int]:
        """Rounded to nearest whole degree (how Kalshi settles)"""
        if self.six_hour_min_f is not None:
            return round(self.six_hour_min_f)
        return None


def parse_6hour_group(group: str) -> Optional[float]:
    """
    Parse a 6-hour temperature group (1snTTT or 2snTTT)
    Returns temperature in Celsius
    
    Format: XsnTTT
    - X = 1 for max, 2 for min
    - sn = 0 for positive, 1 for negative
    - TTT = temperature in tenths of degree C
    
    Example: 10217 -> +21.7°C
    Example: 11015 -> -1.5°C
    """
    if len(group) != 5:
        return None
    
    try:
        sign = int(group[1])
        temp_tenths = int(group[2:5])
        
        temp_c = temp_tenths / 10.0
        if sign == 1:
            temp_c = -temp_c
            
        return temp_c
    except (ValueError, IndexError):
        return None


def parse_t_group(t_group: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Parse T-group from hourly METAR remarks: TsnTTTsnTTT
    Returns (temperature_c, dewpoint_c)
    
    Example: T01060044 -> temp=+10.6°C, dewpoint=+4.4°C
    Example: T10121006 -> temp=-1.2°C, dewpoint=+0.6°C
    """
    if not t_group.startswith('T') or len(t_group) != 9:
        return None, None
    
    try:
        temp_sign = int(t_group[1])
        temp_tenths = int(t_group[2:5])
        dew_sign = int(t_group[5])
        dew_tenths = int(t_group[6:9])
        
        temp_c = temp_tenths / 10.0
        if temp_sign == 1:
            temp_c = -temp_c
            
        dew_c = dew_tenths / 10.0
        if dew_sign == 1:
            dew_c = -dew_c
            
        return temp_c, dew_c
    except (ValueError, IndexError):
        return None, None


def parse_metar(metar_text: str) -> MetarTemps:
    """
    Parse a raw METAR string and extract temperature data
    
    Example METAR:
    KSFO 281853Z 29012KT 10SM FEW020 SCT200 17/08 A3012 RMK AO2 SLP203 T01720083 10189 20156 58010
    
    Key parts:
    - KSFO: Station
    - 281853Z: Day 28, time 18:53 UTC
    - T01720083: T-group (temp 17.2°C, dewpoint 8.3°C)
    - 10189: 6-hour max 18.9°C
    - 20156: 6-hour min 15.6°C
    """
    result = MetarTemps(
        raw_metar=metar_text,
        station="",
        observation_time=None
    )
    
    # Clean up the METAR text
    metar_text = metar_text.strip().upper()
    
    # Remove "METAR" or "SPECI" prefix if present
    if metar_text.startswith("METAR "):
        metar_text = metar_text[6:]
    elif metar_text.startswith("SPECI "):
        metar_text = metar_text[6:]
    
    parts = metar_text.split()
    
    if not parts:
        return result
    
    # Extract station (first part, 4 letters starting with K for US)
    if len(parts[0]) == 4:
        result.station = parts[0]
    
    # Extract observation time (format: DDHHMMz)
    for part in parts[:3]:
        if part.endswith('Z') and len(part) == 7:
            try:
                day = int(part[0:2])
                hour = int(part[2:4])
                minute = int(part[4:6])
                # Create datetime (assume current month/year)
                now = datetime.utcnow()
                result.observation_time = now.replace(
                    day=day, hour=hour, minute=minute, second=0, microsecond=0
                )
            except ValueError:
                pass
            break
    
    # Extract current temp/dewpoint from main body (format: TT/DD or MTT/MDD for negative)
    temp_dew_pattern = re.compile(r'\b(M?\d{2})/(M?\d{2})\b')
    match = temp_dew_pattern.search(metar_text)
    if match:
        temp_str, dew_str = match.groups()
        try:
            result.current_temp_c = -int(temp_str[1:]) if temp_str.startswith('M') else int(temp_str)
            result.current_dewpoint_c = -int(dew_str[1:]) if dew_str.startswith('M') else int(dew_str)
        except ValueError:
            pass
    
    # Look for remarks section
    rmk_idx = metar_text.find('RMK')
    if rmk_idx == -1:
        return result
    
    remarks = metar_text[rmk_idx:]
    
    # Extract T-group (hourly precision temps): T followed by 8 digits
    t_group_pattern = re.compile(r'\bT(\d{8})\b')
    t_match = t_group_pattern.search(remarks)
    if t_match:
        t_group = 'T' + t_match.group(1)
        result.t_group_temp_c, result.t_group_dewpoint_c = parse_t_group(t_group)
    
    # Extract 6-hour maximum (1-group): 1 followed by 4 digits
    max_pattern = re.compile(r'\b(1[01]\d{3})\b')
    max_match = max_pattern.search(remarks)
    if max_match:
        result.six_hour_max_c = parse_6hour_group(max_match.group(1))
    
    # Extract 6-hour minimum (2-group): 2 followed by 4 digits
    min_pattern = re.compile(r'\b(2[01]\d{3})\b')
    min_match = min_pattern.search(remarks)
    if min_match:
        result.six_hour_min_c = parse_6hour_group(min_match.group(1))
    
    return result


def format_metar_summary(parsed: MetarTemps) -> str:
    """Format parsed METAR data for logging/display"""
    lines = [
        f"Station: {parsed.station}",
        f"Time: {parsed.observation_time}",
        f"Raw: {parsed.raw_metar[:80]}{'...' if len(parsed.raw_metar) > 80 else ''}"
    ]
    
    if parsed.current_temp_c is not None:
        lines.append(f"Current Temp: {parsed.current_temp_c}°C")
    
    if parsed.t_group_temp_c is not None:
        t_f = parsed.t_group_temp_c * 1.8 + 32
        lines.append(f"T-Group Temp: {parsed.t_group_temp_c}°C = {t_f:.1f}°F")
    
    if parsed.six_hour_max_c is not None:
        lines.append(
            f"6-Hour MAX: {parsed.six_hour_max_c}°C = {parsed.six_hour_max_f:.2f}°F "
            f"(rounds to {parsed.six_hour_max_f_rounded}°F)"
        )
    
    if parsed.six_hour_min_c is not None:
        lines.append(
            f"6-Hour MIN: {parsed.six_hour_min_c}°C = {parsed.six_hour_min_f:.2f}°F "
            f"(rounds to {parsed.six_hour_min_f_rounded}°F)"
        )
    
    return "\n".join(lines)


# Test/demo
if __name__ == "__main__":
    # Example synoptic METAR with 6-hour groups
    test_metars = [
        # SFO with 6-hour max/min
        "KSFO 281853Z 29012KT 10SM FEW020 SCT200 17/08 A3012 RMK AO2 SLP203 T01720083 10189 20156 58010",
        # LAS example
        "KLAS 281853Z 25008KT 10SM CLR 21/M04 A3018 RMK AO2 SLP150 T02111044 10228 20167 58012",
        # Example with negative temps
        "KDEN 281853Z 36009KT 10SM FEW120 M02/M15 A3045 RMK AO2 SLP320 T10221150 11005 21028",
    ]
    
    print("=" * 60)
    print("METAR PARSER TEST")
    print("=" * 60)
    
    for metar in test_metars:
        parsed = parse_metar(metar)
        print("\n" + format_metar_summary(parsed))
        print("-" * 60)
