"""
State Manager - Track daily high/low temperatures per station

This module tracks:
1. Current day's observed high/low for each station
2. Whether we've already traded a particular bracket
3. Resets at midnight local time for each station
"""

import json
import os
from datetime import datetime, date
from typing import Dict, Optional, Set
from dataclasses import dataclass, field, asdict
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from config import STATIONS


@dataclass
class StationState:
    """State for a single station for a single day"""
    station: str
    date_local: str  # YYYY-MM-DD in station's local timezone
    
    # Tracked extremes (updated as we see new data)
    tracked_high_f: Optional[int] = None
    tracked_low_f: Optional[int] = None
    
    # Last observation times
    last_high_update: Optional[str] = None
    last_low_update: Optional[str] = None
    
    # Brackets we've already traded (to avoid double-trading)
    traded_high_brackets: Set[int] = field(default_factory=set)
    traded_low_brackets: Set[int] = field(default_factory=set)
    
    def to_dict(self) -> dict:
        return {
            "station": self.station,
            "date_local": self.date_local,
            "tracked_high_f": self.tracked_high_f,
            "tracked_low_f": self.tracked_low_f,
            "last_high_update": self.last_high_update,
            "last_low_update": self.last_low_update,
            "traded_high_brackets": list(self.traded_high_brackets),
            "traded_low_brackets": list(self.traded_low_brackets)
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'StationState':
        return cls(
            station=data["station"],
            date_local=data["date_local"],
            tracked_high_f=data.get("tracked_high_f"),
            tracked_low_f=data.get("tracked_low_f"),
            last_high_update=data.get("last_high_update"),
            last_low_update=data.get("last_low_update"),
            traded_high_brackets=set(data.get("traded_high_brackets", [])),
            traded_low_brackets=set(data.get("traded_low_brackets", []))
        )


class StateManager:
    """Manages state for all stations"""
    
    def __init__(self, state_dir: str = "state"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(exist_ok=True)
        self.states: Dict[str, StationState] = {}
        self._load_states()
    
    def _state_file(self, station: str) -> Path:
        return self.state_dir / f"{station}_state.json"
    
    def _load_states(self):
        """Load persisted states from disk"""
        for station in STATIONS.keys():
            state_file = self._state_file(station)
            if state_file.exists():
                try:
                    with open(state_file) as f:
                        data = json.load(f)
                    self.states[station] = StationState.from_dict(data)
                except Exception as e:
                    print(f"[STATE] Error loading {station} state: {e}")
    
    def _save_state(self, station: str):
        """Persist state to disk"""
        if station in self.states:
            state_file = self._state_file(station)
            with open(state_file, 'w') as f:
                json.dump(self.states[station].to_dict(), f, indent=2)
    
    def _get_local_date(self, station: str) -> str:
        """Get current date in station's local timezone"""
        tz_name = STATIONS.get(station, {}).get("timezone", "America/New_York")
        tz = ZoneInfo(tz_name)
        return datetime.now(tz).strftime("%Y-%m-%d")
    
    def get_state(self, station: str) -> StationState:
        """
        Get current state for station, creating/resetting if needed
        
        State resets when date changes in station's local timezone
        """
        local_date = self._get_local_date(station)
        
        if station in self.states:
            state = self.states[station]
            # Reset if date changed
            if state.date_local != local_date:
                print(f"[STATE] New day for {station}, resetting state")
                state = StationState(station=station, date_local=local_date)
                self.states[station] = state
                self._save_state(station)
        else:
            # Create new state
            state = StationState(station=station, date_local=local_date)
            self.states[station] = state
            self._save_state(station)
        
        return state
    
    def update_high(self, station: str, temp_f: int, source: str = "unknown") -> bool:
        """
        Update tracked high if new temp is higher
        
        Returns True if this is a new high
        """
        state = self.get_state(station)
        timestamp = datetime.utcnow().isoformat()
        
        if state.tracked_high_f is None or temp_f > state.tracked_high_f:
            old_high = state.tracked_high_f
            state.tracked_high_f = temp_f
            state.last_high_update = timestamp
            self._save_state(station)
            print(f"[STATE] {station} NEW HIGH: {temp_f}°F (was {old_high}°F) from {source}")
            return True
        
        return False
    
    def update_low(self, station: str, temp_f: int, source: str = "unknown") -> bool:
        """
        Update tracked low if new temp is lower
        
        Returns True if this is a new low
        """
        state = self.get_state(station)
        timestamp = datetime.utcnow().isoformat()
        
        if state.tracked_low_f is None or temp_f < state.tracked_low_f:
            old_low = state.tracked_low_f
            state.tracked_low_f = temp_f
            state.last_low_update = timestamp
            self._save_state(station)
            print(f"[STATE] {station} NEW LOW: {temp_f}°F (was {old_low}°F) from {source}")
            return True
        
        return False
    
    def mark_bracket_traded(self, station: str, temp_f: int, bracket_type: str = "high"):
        """Mark a bracket as traded to avoid double-trading"""
        state = self.get_state(station)
        
        if bracket_type == "high":
            state.traded_high_brackets.add(temp_f)
        else:
            state.traded_low_brackets.add(temp_f)
        
        self._save_state(station)
        print(f"[STATE] Marked {station} {bracket_type} bracket {temp_f}°F as traded")
    
    def is_bracket_traded(self, station: str, temp_f: int, bracket_type: str = "high") -> bool:
        """Check if we've already traded this bracket today"""
        state = self.get_state(station)
        
        if bracket_type == "high":
            return temp_f in state.traded_high_brackets
        else:
            return temp_f in state.traded_low_brackets
    
    def get_summary(self) -> str:
        """Get summary of all station states"""
        lines = ["=" * 50, "STATION STATE SUMMARY", "=" * 50]
        
        for station in STATIONS.keys():
            state = self.get_state(station)
            lines.append(f"\n{station} ({STATIONS[station]['name']}):")
            lines.append(f"  Date: {state.date_local}")
            lines.append(f"  Tracked High: {state.tracked_high_f}°F")
            lines.append(f"  Tracked Low: {state.tracked_low_f}°F")
            lines.append(f"  Traded High Brackets: {sorted(state.traded_high_brackets)}")
            lines.append(f"  Traded Low Brackets: {sorted(state.traded_low_brackets)}")
        
        return "\n".join(lines)


# =========== Test/Demo ===========

if __name__ == "__main__":
    print("=" * 60)
    print("STATE MANAGER TEST")
    print("=" * 60)
    
    # Use temp directory for testing
    manager = StateManager(state_dir="test_state")
    
    # Test updates
    print("\n[Test 1] Initial state")
    state = manager.get_state("KSFO")
    print(f"KSFO initial high: {state.tracked_high_f}")
    
    print("\n[Test 2] Update with 65°F high")
    manager.update_high("KSFO", 65, "test")
    
    print("\n[Test 3] Update with 68°F high (should update)")
    manager.update_high("KSFO", 68, "test")
    
    print("\n[Test 4] Update with 66°F high (should NOT update)")
    manager.update_high("KSFO", 66, "test")
    
    print("\n[Test 5] Mark bracket as traded")
    manager.mark_bracket_traded("KSFO", 68, "high")
    print(f"Is 68°F traded? {manager.is_bracket_traded('KSFO', 68, 'high')}")
    print(f"Is 70°F traded? {manager.is_bracket_traded('KSFO', 70, 'high')}")
    
    print("\n" + manager.get_summary())
    
    # Cleanup test files
    import shutil
    shutil.rmtree("test_state", ignore_errors=True)
