"""
NWS temperature rounding and conversion utilities.

Critical: These must match NWS rounding exactly. NWS uses "round half up"
(toward positive infinity), NOT Python's default "round half even" (banker's rounding).

Examples:
    nws_round(70.5) -> 71  (NOT 70 like Python's round())
    nws_round(-0.5) -> 0   (toward positive infinity)
    nws_round(-1.5) -> -1  (toward positive infinity)
"""

import math


def nws_round(value: float) -> int:
    """NWS rounding: round half toward positive infinity."""
    return math.floor(value + 0.5)


def c_to_f_nws(temp_c: float) -> int:
    """Convert Celsius to Fahrenheit using NWS rounding."""
    return nws_round(temp_c * 9.0 / 5.0 + 32.0)


def f_to_c_nws(temp_f: float) -> float:
    """Convert Fahrenheit to Celsius (no rounding, for intermediate calcs)."""
    return (temp_f - 32.0) * 5.0 / 9.0


def probable_range_from_whole_c(temp_c_whole: int) -> tuple[int, int]:
    """
    Given a whole-degree Celsius reading (from 5-min data or ASOS phone),
    compute the probable Fahrenheit range accounting for the rounding chain.

    The 5-min data path: OMO (whole °F) -> °C -> round to whole °C -> display
    So a displayed X°C could have come from (X-0.5)°C to (X+0.5)°C original,
    which maps to a range of Fahrenheit values.

    Returns (low_f, high_f) as integers.
    """
    low_c = temp_c_whole - 0.5
    high_c = temp_c_whole + 0.4999
    low_f = nws_round(low_c * 9.0 / 5.0 + 32.0)
    high_f = nws_round(high_c * 9.0 / 5.0 + 32.0)
    return (low_f, high_f)
