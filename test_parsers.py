"""
Tests for NWS product parsers.

Uses real product examples from IEM archives and synthetic test cases
to verify correct parsing of DSM, METAR T-groups, and 6-hour groups.
"""

import sys
sys.path.insert(0, ".")

from parsers import (
    parse_dsm, parse_metar, parse_product, classify_product,
    ProductType, DataSource
)
from nws_math import nws_round, c_to_f_nws, probable_range_from_whole_c


def test_nws_round():
    """Verify NWS rounding (half toward positive infinity)."""
    assert nws_round(70.5) == 71, "70.5 should round to 71"
    assert nws_round(70.4) == 70, "70.4 should round to 70"
    assert nws_round(-0.5) == 0, "-0.5 should round to 0 (toward +inf)"
    assert nws_round(-1.5) == -1, "-1.5 should round to -1"
    assert nws_round(0.0) == 0
    assert nws_round(99.5) == 100
    assert nws_round(-10.5) == -10
    print("  nws_round: PASS")


def test_c_to_f():
    """Verify C->F conversion with NWS rounding."""
    # 22.2°C -> 72.0°F -> nws_round -> 72
    assert c_to_f_nws(22.2) == 72
    # 0.0°C -> 32.0°F -> 32
    assert c_to_f_nws(0.0) == 32
    # -2.8°C -> 26.96°F -> 27
    assert c_to_f_nws(-2.8) == 27
    # 21.7°C -> 71.06°F -> 71
    assert c_to_f_nws(21.7) == 71
    # 22.0°C -> 71.6°F -> 72
    assert c_to_f_nws(22.0) == 72
    # -17.8°C -> -0.04°F -> 0
    assert c_to_f_nws(-17.8) == 0
    print("  c_to_f_nws: PASS")


def test_probable_range():
    """Test probable F range from whole C readings."""
    # 22°C whole -> could be 21.5 to 22.4999°C
    low, high = probable_range_from_whole_c(22)
    # 21.5°C = 70.7°F -> nws_round -> 71
    # 22.4999°C = 72.4998°F -> nws_round -> 72
    assert low == 71, f"Expected low=71, got {low}"
    assert high == 72, f"Expected high=72, got {high}"
    print(f"  probable_range(22°C): {low}-{high}°F PASS")

    # Also test: 21°C whole -> spans a bracket boundary?
    low2, high2 = probable_range_from_whole_c(21)
    print(f"  probable_range(21°C): {low2}-{high2}°F")
    # 20.5°C = 68.9°F -> 69, 21.4999°C = 70.6998°F -> 71
    assert low2 == 69
    assert high2 == 71


def test_dsm_parse_nyc():
    """Parse a real NYC DSM from IEM archives (Feb 9, 2026)."""
    # Real product from IEM: DSMNYC, 2026-02-09
    raw = """657 
CXUS41 KOKX 092115
DSMNYC
KNYC DS 1600 09/02 311559/ 100622// 31/ 10//0181559/00/00/00/00/00/
00/00/00/00/00/00/00/00/00/00/00/00/-/-/-/-/-/-/-/-/-/30140505/
30240607"""

    result = parse_dsm(raw, station_hint="KNYC")

    assert result.product_type == ProductType.DSM
    assert result.station == "KNYC"
    assert result.has_data
    assert len(result.temperatures) == 2

    high = result.temperatures[0]
    assert high.source == DataSource.DSM_HIGH
    assert high.temp_f == 31, f"Expected high=31, got {high.temp_f}"
    assert high.time_of_occurrence == "1559"

    low = result.temperatures[1]
    assert low.source == DataSource.DSM_LOW
    assert low.temp_f == 10, f"Expected low=10, got {low.temp_f}"
    assert low.time_of_occurrence == "0622"

    print(f"  DSM NYC: high={high.temp_f}°F at {high.time_of_occurrence}, "
          f"low={low.temp_f}°F at {low.time_of_occurrence} PASS")


def test_dsm_parse_chicago():
    """Parse a real Chicago Midway DSM."""
    raw = """189 
CXUS43 KLOT 022215
DSMMDW
KMDW DS 1600 02/02 451503/ 320207// 45/ 31//9951443/T/T/T/T/T/T/T/00/
00/00/00/00/00/00/00/00/00/-/-/-/-/-/-/-/-/-/15160202/13270050/1"""

    result = parse_dsm(raw, station_hint="KMDW")

    assert result.station == "KMDW"
    assert result.has_data

    high = result.temperatures[0]
    assert high.temp_f == 45
    assert high.time_of_occurrence == "1503"

    low = result.temperatures[1]
    assert low.temp_f == 32
    assert low.time_of_occurrence == "0207"

    print(f"  DSM MDW: high={high.temp_f}°F, low={low.temp_f}°F PASS")


def test_dsm_parse_philly():
    """Parse a real Philadelphia/ACY DSM."""
    raw = """896 
CXUS41 KPHI 032117
DSMACY
KACY DS 1600 03/05 611425/ 520558// 61/ 52//0090243/00/00/00/00/00/
00/00/00/00/00/00/00/00/00/00/00/00/-/-/-/-/-/-/-/-/-/10161417/
07261235/1"""

    result = parse_dsm(raw, station_hint="KACY")

    assert result.station == "KACY"
    assert result.has_data

    high = result.temperatures[0]
    assert high.temp_f == 61
    low = result.temperatures[1]
    assert low.temp_f == 52

    print(f"  DSM ACY: high={high.temp_f}°F, low={low.temp_f}°F PASS")


def test_dsm_negative_temps():
    """Test DSM with negative temperatures."""
    raw = """DSMNYC
KNYC DS 1600 01/15 -51200/ -150800// -5/ -15//..."""

    result = parse_dsm(raw)
    assert result.has_data
    assert result.temperatures[0].temp_f == -5
    assert result.temperatures[1].temp_f == -15
    print(f"  DSM negative: high={result.temperatures[0].temp_f}°F, "
          f"low={result.temperatures[1].temp_f}°F PASS")


def test_metar_t_group():
    """Test METAR T-group parsing."""
    # Synthetic METAR with T-group showing 22.2°C temp, 16.7°C dewpoint
    raw = """METAR KPHL 091753Z 31010KT 10SM FEW250 M01/M06 A3042 RMK AO2 SLP308 T10060061 $"""

    result = parse_metar(raw)

    assert result.station == "KPHL"
    assert result.has_data

    t_group = result.temperatures[0]
    assert t_group.source == DataSource.METAR_T_GROUP
    assert t_group.temp_c == -0.6
    assert t_group.temp_f == c_to_f_nws(-0.6)  # 30.92 -> 31
    assert t_group.precision == "high"

    print(f"  METAR T-group: {t_group.temp_c}°C -> {t_group.temp_f}°F PASS")


def test_metar_t_group_positive():
    """Test T-group with positive temperatures."""
    raw = """METAR KNYC 091751Z 18008KT 10SM SCT250 22/17 A3012 RMK AO2 SLP200 T02220167"""

    result = parse_metar(raw)
    assert result.has_data

    t = result.temperatures[0]
    assert t.temp_c == 22.2
    assert t.temp_f == 72  # 22.2 * 9/5 + 32 = 71.96 -> nws_round -> 72

    print(f"  METAR T-group positive: {t.temp_c}°C -> {t.temp_f}°F PASS")


def test_metar_6hr_max():
    """Test 6-hour max (1-group) parsing."""
    # METAR at synoptic time with 1-group showing +22.2°C max
    raw = """METAR KPHL 091800Z 31012KT 10SM FEW250 20/15 A3042 RMK AO2 SLP308 10222 T02000150"""

    result = parse_metar(raw)

    # Should have both T-group and 6-hr max
    sources = {t.source for t in result.temperatures}
    assert DataSource.METAR_T_GROUP in sources
    assert DataSource.METAR_6HR_MAX in sources

    six_hr = [t for t in result.temperatures if t.source == DataSource.METAR_6HR_MAX][0]
    assert six_hr.temp_c == 22.2
    assert six_hr.temp_f == 72

    print(f"  METAR 6-hr max: {six_hr.temp_c}°C -> {six_hr.temp_f}°F PASS")


def test_metar_6hr_min():
    """Test 6-hour min (2-group) parsing."""
    raw = """METAR KMDW 091200Z 27015KT 10SM OVC020 M03/M08 A3020 RMK AO2 SLP240 21028 T10281078"""

    result = parse_metar(raw)

    six_hr = [t for t in result.temperatures if t.source == DataSource.METAR_6HR_MIN]
    assert len(six_hr) == 1
    assert six_hr[0].temp_c == -2.8
    assert six_hr[0].temp_f == 27  # -2.8 * 9/5 + 32 = 26.96 -> 27

    print(f"  METAR 6-hr min: {six_hr[0].temp_c}°C -> {six_hr[0].temp_f}°F PASS")


def test_metar_24hr():
    """Test 24-hour max/min (4-group) parsing."""
    raw = """METAR KNYC 100000Z 20010KT 10SM SCT250 18/12 A3012 RMK AO2 SLP200 401001015 T01800120"""

    result = parse_metar(raw)

    hr24_max = [t for t in result.temperatures if t.source == DataSource.METAR_24HR_MAX]
    hr24_min = [t for t in result.temperatures if t.source == DataSource.METAR_24HR_MIN]

    assert len(hr24_max) == 1
    assert hr24_max[0].temp_c == 10.0
    assert hr24_max[0].temp_f == 50  # 10.0 * 9/5 + 32 = 50.0

    assert len(hr24_min) == 1
    assert hr24_min[0].temp_c == -1.5
    assert hr24_min[0].temp_f == 29  # -1.5 * 9/5 + 32 = 29.3 -> 29

    print(f"  METAR 24-hr: max={hr24_max[0].temp_f}°F, min={hr24_min[0].temp_f}°F PASS")


def test_speci_parsing():
    """Test SPECI observations."""
    raw = """SPECI KPHL 091830Z 31015G25KT 10SM BKN015 21/16 A3040 RMK AO2 T02110161"""

    result = parse_metar(raw, is_speci=True)

    assert result.product_type == ProductType.SPECI
    assert result.has_data
    assert result.temperatures[0].source == DataSource.SPECI_T_GROUP
    assert result.temperatures[0].temp_c == 21.1
    assert result.temperatures[0].temp_f == 70  # 21.1 * 9/5 + 32 = 69.98 -> 70

    print(f"  SPECI T-group: {result.temperatures[0].temp_c}°C -> "
          f"{result.temperatures[0].temp_f}°F PASS")


def test_product_classification():
    """Test AWIPS ID classification."""
    assert classify_product("DSMNYC") == ProductType.DSM
    assert classify_product("DSMPHL") == ProductType.DSM
    assert classify_product("CLINEW") == ProductType.CLI
    assert classify_product("MTRKPHL") == ProductType.METAR
    assert classify_product("SPEKPHL") == ProductType.SPECI
    assert classify_product("FOOBAR") == ProductType.UNKNOWN
    assert classify_product("") == ProductType.UNKNOWN
    print("  Product classification: PASS")


def test_edge_case_bracket_resolution():
    """
    Test the core use case: a DSM resolves a rounding ambiguity.

    Scenario: 5-min data shows 70°F displayed (whole °C = 21°C).
    Probable range: 21°C whole -> 70-72°F.
    Market brackets: 70-71 and 72-73 are both around 50¢.
    DSM drops showing high = 72°F -> buy 72-73 bracket.
    """
    # The ambiguous 5-min reading
    low_f, high_f = probable_range_from_whole_c(21)
    # 21°C whole -> 69-71°F range (spans 70-71 bracket boundary)
    assert low_f == 69
    assert high_f == 71

    # Now DSM resolves it
    dsm_raw = """DSMNYC
KNYC DS 1600 06/15 721430/ 630500// 72/ 63//..."""

    result = parse_dsm(dsm_raw)
    assert result.temperatures[0].temp_f == 72

    # The DSM says 72°F, which falls in the 72-73 bracket -> BUY YES on 72-73
    resolved_temp = result.temperatures[0].temp_f
    assert resolved_temp == 72
    # In Kalshi bracket encoding: floor_strike=72, cap_strike=73
    # YES wins when 72 <= final <= 73

    print(f"  Bracket resolution: 5-min ambiguous {low_f}-{high_f}°F, "
          f"DSM resolves to {resolved_temp}°F -> 72-73 bracket PASS")


def test_edge_case_t_group_resolution():
    """
    Test T-group resolving the same ambiguity.

    T-group shows 22.2°C -> 72.0°F -> nws_round -> 72°F
    """
    raw = """METAR KNYC 151800Z 20008KT 10SM SCT250 22/17 A3012 RMK AO2 SLP200 10222 T02220167"""

    result = parse_metar(raw)

    # T-group: 22.2°C -> 72°F
    t_group = [t for t in result.temperatures if t.source == DataSource.METAR_T_GROUP][0]
    assert t_group.temp_f == 72

    # 6-hr max also 22.2°C -> 72°F
    six_hr = [t for t in result.temperatures if t.source == DataSource.METAR_6HR_MAX][0]
    assert six_hr.temp_f == 72

    # Both agree -> high confidence
    print(f"  T-group + 6-hr agree: both {t_group.temp_f}°F PASS")


def run_all_tests():
    print("Running parser tests...\n")

    test_nws_round()
    test_c_to_f()
    test_probable_range()
    test_dsm_parse_nyc()
    test_dsm_parse_chicago()
    test_dsm_parse_philly()
    test_dsm_negative_temps()
    test_metar_t_group()
    test_metar_t_group_positive()
    test_metar_6hr_max()
    test_metar_6hr_min()
    test_metar_24hr()
    test_speci_parsing()
    test_product_classification()
    test_edge_case_bracket_resolution()
    test_edge_case_t_group_resolution()

    print(f"\nAll tests passed!")


if __name__ == "__main__":
    run_all_tests()
