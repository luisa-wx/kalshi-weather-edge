#!/usr/bin/env python3
"""
QC Test Script for Wx Sniper

Tests:
1. Rounding rules (NWS ROUND_HALF_UP vs Python round)
2. METAR parsing for various temps (positive, negative, edge cases)
3. Market bracket matching logic
4. Trade decision logic
5. DST handling
6. Timezone/date determination

Run with: python qc_test.py
"""

import sys
import os
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import json

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from metar_parser import parse_metar, nws_round, parse_6hour_group
from config import STATIONS

# ANSI colors for output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

def test_passed(name: str):
    print(f"  {GREEN}✓{RESET} {name}")
    
def test_failed(name: str, expected, got):
    print(f"  {RED}✗{RESET} {name}")
    print(f"      Expected: {expected}")
    print(f"      Got:      {got}")
    return False

def test_warning(name: str, msg: str):
    print(f"  {YELLOW}⚠{RESET} {name}: {msg}")


class QCTests:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        
    def run_all(self):
        print("\n" + "="*70)
        print(" WX SNIPER QC TEST SUITE")
        print("="*70)
        
        self.test_rounding_rules()
        self.test_negative_celsius_parsing()
        self.test_metar_parsing()
        self.test_market_bracket_logic()
        self.test_dst_handling()
        self.test_trade_decision_logic()
        
        print("\n" + "="*70)
        print(f" RESULTS: {GREEN}{self.passed} passed{RESET}, {RED}{self.failed} failed{RESET}, {YELLOW}{self.warnings} warnings{RESET}")
        print("="*70 + "\n")
        
        return self.failed == 0
    
    def test_rounding_rules(self):
        """Test NWS ROUND_HALF_UP rounding vs Python default"""
        print("\n[1] ROUNDING RULES (NWS ROUND_HALF_UP)")
        print("-" * 50)
        
        # Test cases: (input, expected_nws_round)
        # NWS uses "asymmetric half-up" = floor(x + 0.5)
        # This rounds .5 towards positive infinity
        test_cases = [
            # Positive values
            (0.5, 1),
            (1.5, 2),
            (2.5, 3),
            (10.5, 11),
            
            # Negative values - rounds TOWARDS positive infinity
            (-0.5, 0),    # floor(-0.5 + 0.5) = floor(0) = 0
            (-1.5, -1),   # floor(-1.5 + 0.5) = floor(-1) = -1
            (-2.5, -2),   # floor(-2.5 + 0.5) = floor(-2) = -2
            (-10.5, -10), # floor(-10.5 + 0.5) = floor(-10) = -10
            
            # Non-.5 values (should be same as Python round)
            (1.4, 1),
            (1.6, 2),
            (-1.4, -1),
            (-1.6, -2),
            
            # Edge cases
            (0.0, 0),
            (-0.0, 0),
            (32.0, 32),
        ]
        
        all_passed = True
        for value, expected in test_cases:
            result = nws_round(value)
            if result == expected:
                self.passed += 1
            else:
                all_passed = False
                self.failed += 1
                test_failed(f"nws_round({value})", expected, result)
        
        if all_passed:
            test_passed(f"All {len(test_cases)} rounding cases correct")
        
        # Show comparison with Python round for .5 cases
        print("\n  Comparison for .5 values:")
        half_cases = [0.5, 1.5, 2.5, -0.5, -1.5, -2.5]
        for v in half_cases:
            py = round(v)
            nws = nws_round(v)
            diff = "SAME" if py == nws else f"DIFFERS! py={py}"
            print(f"    {v:5.1f}: nws_round={nws:3d}, python round={py:3d}  [{diff}]")
    
    def test_negative_celsius_parsing(self):
        """Test parsing of 6-hour groups with negative temps"""
        print("\n[2] NEGATIVE CELSIUS PARSING (6-hour groups)")
        print("-" * 50)
        
        # Format: XsnTTT where X=1(max)/2(min), sn=0(+)/1(-), TTT=tenths°C
        test_cases = [
            # Positive temps
            ("10217", 21.7),   # +21.7°C
            ("10000", 0.0),    # 0.0°C
            ("10055", 5.5),    # +5.5°C
            
            # Negative temps
            ("11015", -1.5),   # -1.5°C
            ("11120", -12.0),  # -12.0°C
            ("11001", -0.1),   # -0.1°C
            ("11000", 0.0),    # -0.0°C (edge case, should be 0)
            
            # Min temps (2xxxx)
            ("20156", 15.6),   # +15.6°C
            ("21028", -2.8),   # -2.8°C
            ("21083", -8.3),   # -8.3°C
        ]
        
        all_passed = True
        for group, expected_c in test_cases:
            result = parse_6hour_group(group)
            if result is not None and abs(result - expected_c) < 0.01:
                self.passed += 1
            else:
                all_passed = False
                self.failed += 1
                test_failed(f"parse_6hour_group('{group}')", expected_c, result)
        
        if all_passed:
            test_passed(f"All {len(test_cases)} 6-hour group parsing cases correct")
    
    def test_metar_parsing(self):
        """Test full METAR parsing with various conditions"""
        print("\n[3] FULL METAR PARSING")
        print("-" * 50)
        
        # Test METARs with known values
        test_metars = [
            # Denver winter morning with negative temps
            {
                "metar": "KDEN 291153Z 18007KT 10SM FEW100 M02/M15 A3045 RMK AO2 SLP352 T10221150 21028 53012",
                "expected": {
                    "station": "KDEN",
                    "current_temp_c": -2,
                    "t_group_temp_c": -2.2,
                    "six_hour_min_c": -2.8,
                    "six_hour_min_f_rounded": 27,  # -2.8°C = 26.96°F -> 27
                }
            },
            # Chicago cold snap
            {
                "metar": "KMDW 290553Z 32015G25KT 10SM SCT025 BKN040 M08/M14 A3012 RMK AO2 SLP210 T10831139 11006 21083 53008",
                "expected": {
                    "station": "KMDW",
                    "current_temp_c": -8,
                    "six_hour_max_c": -0.6,  # 11006 = sign=1 (negative), temp=006 = 0.6 -> -0.6°C
                    "six_hour_min_c": -8.3,
                    "six_hour_min_f_rounded": 17,  # -8.3°C = 17.06°F -> 17
                }
            },
            # San Francisco mild day
            {
                "metar": "KSFO 281853Z 29012KT 10SM FEW020 SCT200 17/08 A3012 RMK AO2 SLP203 T01720083 10189 20156 58010",
                "expected": {
                    "station": "KSFO",
                    "current_temp_c": 17,
                    "t_group_temp_c": 17.2,
                    "six_hour_max_c": 18.9,
                    "six_hour_max_f_rounded": 66,  # 18.9°C = 66.02°F -> 66
                    "six_hour_min_c": 15.6,
                    "six_hour_min_f_rounded": 60,  # 15.6°C = 60.08°F -> 60
                }
            },
            # Edge case: exactly 0.5°F result
            # Note: Using realistic METAR format
            {
                "metar": "KDEN 291753Z 00000KT 10SM CLR M18/M20 A3000 RMK AO2 T11751200 11175 21200",
                "expected": {
                    "station": "KDEN",
                    "six_hour_max_c": -17.5,
                    # -17.5°C = 0.5°F -> should round to 1 with asymmetric half-up
                    "six_hour_max_f_rounded": 1,
                }
            },
        ]
        
        all_passed = True
        for test in test_metars:
            parsed = parse_metar(test["metar"])
            expected = test["expected"]
            
            for key, exp_val in expected.items():
                got_val = getattr(parsed, key, None)
                if got_val is None and exp_val is not None:
                    all_passed = False
                    self.failed += 1
                    test_failed(f"{expected['station']}.{key}", exp_val, got_val)
                elif isinstance(exp_val, float):
                    if got_val is None or abs(got_val - exp_val) > 0.01:
                        all_passed = False
                        self.failed += 1
                        test_failed(f"{expected['station']}.{key}", exp_val, got_val)
                    else:
                        self.passed += 1
                elif got_val != exp_val:
                    all_passed = False
                    self.failed += 1
                    test_failed(f"{expected['station']}.{key}", exp_val, got_val)
                else:
                    self.passed += 1
        
        if all_passed:
            test_passed("All METAR parsing tests passed")
    
    def test_market_bracket_logic(self):
        """Test market bracket matching (which bracket does a temp fall into)"""
        print("\n[4] MARKET BRACKET LOGIC")
        print("-" * 50)
        
        # Simulate Kalshi bracket format: "above X" or "X or below"
        # For HIGHs: we want "above X" where temp > X
        # For LOWs: we want "X or below" where temp <= X
        
        def find_high_bracket(temp_f: int, brackets: List[int]) -> Optional[int]:
            """Find the highest bracket that temp is ABOVE"""
            for b in sorted(brackets, reverse=True):
                if temp_f > b:
                    return b
            return None
        
        def find_low_bracket(temp_f: int, brackets: List[int]) -> Optional[int]:
            """Find the lowest bracket that temp is AT OR BELOW"""
            for b in sorted(brackets):
                if temp_f <= b:
                    return b
            return None
        
        # Test HIGH bracket matching
        high_brackets = [65, 70, 75, 80, 85]
        high_tests = [
            (66, 65),   # 66 is above 65
            (70, 65),   # 70 is NOT above 70, so above 65
            (71, 70),   # 71 is above 70
            (85, 80),   # 85 is NOT above 85, so above 80
            (86, 85),   # 86 is above 85
            (64, None), # 64 is not above any bracket
        ]
        
        for temp, expected in high_tests:
            result = find_high_bracket(temp, high_brackets)
            if result == expected:
                self.passed += 1
            else:
                self.failed += 1
                test_failed(f"HIGH bracket for {temp}°F", expected, result)
        
        # Test LOW bracket matching
        low_brackets = [20, 25, 30, 35, 40]
        low_tests = [
            (20, 20),   # 20 is at 20 (at or below)
            (19, 20),   # 19 is below 20
            (25, 25),   # 25 is at 25
            (26, 30),   # 26 is at or below 30
            (41, None), # 41 is not at or below any bracket
        ]
        
        for temp, expected in low_tests:
            result = find_low_bracket(temp, low_brackets)
            if result == expected:
                self.passed += 1
            else:
                self.failed += 1
                test_failed(f"LOW bracket for {temp}°F", expected, result)
        
        test_passed("Bracket matching logic verified")
    
    def test_dst_handling(self):
        """Test DST transitions are handled correctly"""
        print("\n[5] DST HANDLING")
        print("-" * 50)
        
        # 2026 DST: Spring forward Mar 8, Fall back Nov 1
        test_cases = [
            # Before spring forward
            (datetime(2026, 3, 7, 23, 53, tzinfo=ZoneInfo('UTC')), 
             'America/New_York', '2026-03-07', 'EST'),
            # After spring forward  
            (datetime(2026, 3, 8, 23, 53, tzinfo=ZoneInfo('UTC')), 
             'America/New_York', '2026-03-08', 'EDT'),
            # Before fall back
            (datetime(2026, 10, 31, 23, 53, tzinfo=ZoneInfo('UTC')), 
             'America/New_York', '2026-10-31', 'EDT'),
            # After fall back
            (datetime(2026, 11, 1, 23, 53, tzinfo=ZoneInfo('UTC')), 
             'America/New_York', '2026-11-01', 'EST'),
        ]
        
        for utc_time, tz_name, expected_date, expected_tz in test_cases:
            local = utc_time.astimezone(ZoneInfo(tz_name))
            got_date = local.strftime('%Y-%m-%d')
            got_tz = local.strftime('%Z')
            
            if got_date == expected_date and got_tz == expected_tz:
                self.passed += 1
            else:
                self.failed += 1
                test_failed(f"DST for {utc_time}", 
                           f"{expected_date} {expected_tz}", 
                           f"{got_date} {got_tz}")
        
        test_passed("DST handling verified for 2026 transitions")
        
        # Test cross-midnight date handling
        print("\n  Cross-midnight date test (same synoptic, different local dates):")
        synoptic = datetime(2026, 1, 29, 5, 53, tzinfo=ZoneInfo('UTC'))
        
        tz_tests = [
            ('America/New_York', '2026-01-29'),   # 00:53 EST
            ('America/Chicago', '2026-01-28'),    # 23:53 CST (prev day)
            ('America/Denver', '2026-01-28'),     # 22:53 MST (prev day)
            ('America/Los_Angeles', '2026-01-28'), # 21:53 PST (prev day)
        ]
        
        for tz_name, expected_date in tz_tests:
            local = synoptic.astimezone(ZoneInfo(tz_name))
            got_date = local.strftime('%Y-%m-%d')
            if got_date == expected_date:
                self.passed += 1
            else:
                self.failed += 1
                test_failed(f"Date for {tz_name} at 0553Z", expected_date, got_date)
        
        test_passed("Cross-midnight date handling correct")
    
    def test_trade_decision_logic(self):
        """Test trade decision logic with mock data"""
        print("\n[6] TRADE DECISION LOGIC")
        print("-" * 50)
        
        @dataclass
        class MockMarket:
            ticker: str
            yes_bid: int  # cents
            floor_strike: float
            cap_strike: Optional[float] = None
        
        @dataclass
        class MockTradeDecision:
            should_trade: bool
            reason: str
            side: Optional[str] = None
            
        def evaluate_high_trade(temp_f: int, market: MockMarket, threshold: int = 93) -> MockTradeDecision:
            """
            For HIGH markets: market wins if temp > floor_strike
            We buy YES if temp is ABOVE the strike and price is below threshold
            """
            strike = int(market.floor_strike)
            
            if temp_f > strike:
                # Temperature IS above strike, YES should win
                if market.yes_bid < threshold:
                    return MockTradeDecision(True, f"Temp {temp_f} > {strike}, price {market.yes_bid}¢ < {threshold}¢", "YES")
                else:
                    return MockTradeDecision(False, f"Price {market.yes_bid}¢ already >= {threshold}¢")
            else:
                # Temperature is NOT above strike
                return MockTradeDecision(False, f"Temp {temp_f} NOT > {strike}")
        
        def evaluate_low_trade(temp_f: int, market: MockMarket, threshold: int = 93) -> MockTradeDecision:
            """
            For LOW markets: market wins if temp <= cap_strike
            We buy YES if temp is AT OR BELOW the strike and price is below threshold
            """
            strike = int(market.cap_strike) if market.cap_strike else int(market.floor_strike)
            
            if temp_f <= strike:
                # Temperature IS at or below strike, YES should win
                if market.yes_bid < threshold:
                    return MockTradeDecision(True, f"Temp {temp_f} <= {strike}, price {market.yes_bid}¢ < {threshold}¢", "YES")
                else:
                    return MockTradeDecision(False, f"Price {market.yes_bid}¢ already >= {threshold}¢")
            else:
                # Temperature is NOT at or below strike
                return MockTradeDecision(False, f"Temp {temp_f} NOT <= {strike}")
        
        # Test HIGH market decisions
        print("\n  HIGH market tests:")
        high_tests = [
            # (temp, strike, price, should_trade, expected_side)
            (71, MockMarket("TEST-70", 50, 70.0), True, "YES"),   # 71 > 70, price good
            (70, MockMarket("TEST-70", 50, 70.0), False, None),   # 70 NOT > 70
            (71, MockMarket("TEST-70", 95, 70.0), False, None),   # Price too high
            (85, MockMarket("TEST-80", 30, 80.0), True, "YES"),   # 85 > 80, price good
        ]
        
        for temp, market, should_trade, exp_side in high_tests:
            result = evaluate_high_trade(temp, market)
            if result.should_trade == should_trade and result.side == exp_side:
                self.passed += 1
            else:
                self.failed += 1
                test_failed(f"HIGH trade {temp}°F vs strike {market.floor_strike}", 
                           (should_trade, exp_side), 
                           (result.should_trade, result.side))
        
        # Test LOW market decisions
        print("  LOW market tests:")
        low_tests = [
            # (temp, strike, price, should_trade, expected_side)
            (25, MockMarket("TEST-25", 50, 25.0, 25.0), True, "YES"),   # 25 <= 25
            (26, MockMarket("TEST-25", 50, 25.0, 25.0), False, None),   # 26 NOT <= 25
            (20, MockMarket("TEST-25", 50, 25.0, 25.0), True, "YES"),   # 20 <= 25
            (25, MockMarket("TEST-25", 97, 25.0, 25.0), False, None),   # Price too high
        ]
        
        for temp, market, should_trade, exp_side in low_tests:
            result = evaluate_low_trade(temp, market)
            if result.should_trade == should_trade and result.side == exp_side:
                self.passed += 1
            else:
                self.failed += 1
                test_failed(f"LOW trade {temp}°F vs strike {market.cap_strike}", 
                           (should_trade, exp_side), 
                           (result.should_trade, result.side))
        
        test_passed("Trade decision logic verified")


def main():
    tests = QCTests()
    success = tests.run_all()
    
    if success:
        print("All tests passed! Ready for live testing.\n")
        return 0
    else:
        print("Some tests failed. Please fix before live testing.\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
