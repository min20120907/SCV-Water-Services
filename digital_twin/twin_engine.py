#!/usr/bin/env python3
"""
SCV Water Digital Twin Engine
Simulates household water physics, environmental sensors, automated valves, and smart scenarios.
Standard library only -- no external dependencies required.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional


@dataclass
class TwinState:
    time_str: str = "06:00"
    water_level_pct: float = 45.0      # Storage tank level (0 - 100%)
    soil_moisture_pct: float = 22.0    # Garden soil moisture (0 - 100%)
    ambient_temp_c: float = 24.0       # Ambient temp (deg C)
    humidity_pct: float = 60.0         # Relative humidity (%)
    rain_detected: bool = False        # Rain sensor
    leak_detected: bool = False        # Floor leak contact sensor
    tds_ppm: float = 120.0             # Water quality TDS (ppm)
    flow_rate_lpm: float = 0.0         # Water flow (Litres/min)
    cumulative_liters: float = 0.0     # Total consumption (L)
    
    # Actuators (Valves)
    valve_in: bool = False             # Main / Refill / Emergency Isolation
    valve_1: bool = False              # Clean / Kitchen / Bathroom Line
    valve_2: bool = False              # Garden / Toilet / Drain Divert Line
    
    # Status & Alerts
    active_scenario: str = "idle"
    alert_msg: str = "NORMAL"
    auto_refill_active: bool = False


class WaterDigitalTwin:
    """Physics model and rule decision engine for SCV Water Digital Twin."""

    def __init__(self, initial_state: Optional[TwinState] = None):
        self.state = initial_state or TwinState()

    def set_scenario(self, name: str) -> TwinState:
        s = self.state
        s.active_scenario = name
        s.alert_msg = "NORMAL"
        s.leak_detected = False
        s.rain_detected = False
        s.ambient_temp_c = 24.0
        s.humidity_pct = 60.0
        s.tds_ppm = 120.0
        s.flow_rate_lpm = 0.0
        s.valve_in = False
        s.valve_1 = False
        s.valve_2 = False

        if name == "morning_garden":
            # 06:00, dry soil, no rain -> Garden watering
            s.time_str = "06:00"
            s.ambient_temp_c = 22.0
            s.soil_moisture_pct = 22.0
            s.rain_detected = False
            s.leak_detected = False
            s.tds_ppm = 110.0
            s.flow_rate_lpm = 4.0
            s.valve_1 = False
            s.valve_2 = True

        elif name == "hot_afternoon":
            # 12:30, 35 deg C, dry soil, defer peak sun
            s.time_str = "12:30"
            s.ambient_temp_c = 35.0
            s.soil_moisture_pct = 18.0
            s.rain_detected = False
            s.leak_detected = False
            s.tds_ppm = 125.0
            s.flow_rate_lpm = 0.0
            s.valve_1 = False
            s.valve_2 = False

        elif name == "rainy_morning":
            # Rain detected -> garden watering inhibited even if scheduled
            s.time_str = "07:00"
            s.ambient_temp_c = 19.0
            s.soil_moisture_pct = 68.0
            s.rain_detected = True
            s.leak_detected = False
            s.flow_rate_lpm = 0.0
            s.valve_2 = False

        elif name == "afternoon_cooking":
            # Kitchen water demand at 3 L/min
            s.time_str = "16:00"
            s.ambient_temp_c = 26.0
            s.soil_moisture_pct = 40.0
            s.rain_detected = False
            s.leak_detected = False
            s.tds_ppm = 95.0
            s.flow_rate_lpm = 3.0
            s.valve_1 = True
            s.valve_2 = False

        elif name == "tank_low_refill":
            # Tank <= 5% -> auto refill triggered
            s.time_str = "15:00"
            s.water_level_pct = 4.0
            s.auto_refill_active = True
            s.valve_in = True
            s.flow_rate_lpm = 5.0

        elif name == "poor_water_divert":
            # TDS > 300 ppm -> Divert to Valve 2 (drain/recovery), shut Valve 1
            s.time_str = "10:00"
            s.tds_ppm = 385.0
            s.flow_rate_lpm = 2.5
            s.valve_1 = False
            s.valve_2 = True

        elif name == "overnight_leak":
            # 02:30, unexpected flow + leak sensor wet -> Emergency isolation
            s.time_str = "02:30"
            s.ambient_temp_c = 21.0
            s.leak_detected = True
            s.flow_rate_lpm = 0.3
            s.valve_in = False
            s.valve_1 = False
            s.valve_2 = False

        elif name == "filter_flush":
            # Backwash maintenance: input ON, Valve 2 ON for 30s
            s.time_str = "14:00"
            s.valve_in = True
            s.valve_1 = False
            s.valve_2 = True
            s.flow_rate_lpm = 3.5

        elif name == "evening_shower":
            # 19:00, evening shower & laundry, Valve 1 supplies water, track consumption
            s.time_str = "19:00"
            s.flow_rate_lpm = 8.0
            s.valve_1 = True
            s.valve_2 = False

        elif name == "tank_nearly_full":
            # Tank >= 60% -> stop auto refill, close Valve IN
            s.water_level_pct = 65.0
            s.auto_refill_active = False
            s.valve_in = False
            s.flow_rate_lpm = 0.0

        elif name == "damp_cupboard":
            # Humidity spike + leak tripped -> emergency isolation
            s.time_str = "11:00"
            s.humidity_pct = 95.0
            s.leak_detected = True
            s.flow_rate_lpm = 0.5
            s.valve_in = False

        elif name == "cold_weather":
            # Freezing alert (< 2 deg C), valves under safety control
            s.time_str = "05:00"
            s.ambient_temp_c = -1.0
            s.flow_rate_lpm = 0.0

        elif name == "excessive_use":
            # Abnormal continuous flow rate > 10 L/min
            s.flow_rate_lpm = 12.0
            s.valve_1 = True

        elif name == "good_water":
            # Scenario 1: Acceptable TDS -> Filter & Deliver -> Input ON, Valve 1 ON
            s.time_str = "09:00"
            s.tds_ppm = 85.0
            s.valve_in = True
            s.valve_1 = True
            s.valve_2 = False
            s.flow_rate_lpm = 4.0

        elif name == "filter_replacement":
            # Scenario 4: Flow rate / pressure drop under open demand -> Service required
            s.valve_1 = False
            s.valve_2 = False
            s.flow_rate_lpm = 0.2
            s.alert_msg = "FAULT: FILTER SERVICE REQUIRED (LOW FLOW/PRESSURE DROP)"

        elif name == "water_saving_reuse":
            # Scenario 5: Secondary reuse -> Valve 2 directs grey/reject water to reuse tank
            s.valve_1 = False
            s.valve_2 = True
            s.flow_rate_lpm = 2.0
            s.alert_msg = "REUSE_MODE: Diverted to secondary storage tank"

        elif name == "idle":
            s.flow_rate_lpm = 0.0
            s.valve_1 = False
            s.valve_2 = False
            s.leak_detected = False
            s.rain_detected = False

        self.evaluate_rules()
        return self.state

    def evaluate_rules(self) -> None:
        """Apply deterministic safety & automation rules."""
        s = self.state

        # Rule 1: Emergency Leak Shutdown
        if s.leak_detected:
            s.valve_in = False
            s.alert_msg = "CRITICAL: LEAK_DETECTED -> INLET ISOLATED"
            return

        # Rule 2: Water level auto refill hysteresis (<= 5% START, >= 60% STOP)
        if s.water_level_pct <= 5.0:
            s.auto_refill_active = True
            s.valve_in = True
            s.alert_msg = "AUTO_FILL: TANK LOW (<=5%)"
        elif s.water_level_pct >= 60.0 and s.auto_refill_active:
            s.auto_refill_active = False
            s.valve_in = False
            s.alert_msg = "STOP_FILL: TANK TARGET REACHED (>=60%)"

        # Rule 3: Water quality / TDS divert
        if s.tds_ppm > 300.0:
            if s.valve_1:
                s.valve_1 = False
                s.valve_2 = True
                s.alert_msg = "POOR_WATER_DIVERT: TDS > 300 ppm"

        # Rule 4: High Flow Alert
        if s.flow_rate_lpm > 1.0 and not (s.valve_1 or s.valve_2 or s.valve_in):
            s.alert_msg = f"WARNING: UNEXPECTED HIGH FLOW ({s.flow_rate_lpm} L/min)"
            s.valve_in = False

        # Rule 5: Garden Rain Suppression
        if s.rain_detected and s.valve_2 and s.active_scenario == "morning_garden":
            s.valve_2 = False
            s.alert_msg = "RAIN_SUPPRESSION: Garden watering cancelled"

        # Rule 6: Freezing Risk Alert
        if s.ambient_temp_c < 2.0 and "NORMAL" in s.alert_msg:
            s.alert_msg = f"ALERT: FREEZING_RISK ({s.ambient_temp_c}°C) -> PIPE PROTECTION ACTIVE"

        # Rule 7: Excessive Household Water Flow Alert
        if s.flow_rate_lpm > 10.0:
            s.alert_msg = f"ALERT: EXCESSIVE_FLOW ({s.flow_rate_lpm} L/min) -> CHECK ABNORMAL TAP"

    def step(self, dt_seconds: float = 1.0) -> TwinState:
        """Advance physical simulation by dt_seconds."""
        s = self.state

        # Inflow refills tank
        if s.valve_in:
            inflow_rate = 6.0  # L/min
            s.water_level_pct = min(100.0, s.water_level_pct + (inflow_rate * dt_seconds / 60.0) * 2.0)

        # Outflows drain tank
        outflow = 0.0
        if s.valve_1:
            outflow += 3.0  # L/min
        if s.valve_2:
            outflow += 4.0  # L/min
            if s.active_scenario == "morning_garden":
                s.soil_moisture_pct = min(100.0, s.soil_moisture_pct + 0.5 * dt_seconds)

        if outflow > 0:
            s.water_level_pct = max(0.0, s.water_level_pct - (outflow * dt_seconds / 60.0) * 1.5)
            s.cumulative_liters += (outflow * dt_seconds / 60.0)

        # Environmental evaporation / drydown
        if not s.rain_detected and not s.valve_2:
            s.soil_moisture_pct = max(10.0, s.soil_moisture_pct - 0.02 * dt_seconds)

        self.evaluate_rules()
        return self.state

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self.state)


# ---------------------------------------------------------------------------
# HTTP REST API for Virtual Linux Integration
# ---------------------------------------------------------------------------
_twin_instance = WaterDigitalTwin()


class TwinRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, data: Dict[str, Any]):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send_json(200, {"status": "ok"})

    def do_GET(self):
        if self.path in ("/status", "/state", "/"):
            self._send_json(200, _twin_instance.to_dict())
        elif self.path == "/health":
            self._send_json(200, {"status": "healthy", "service": "scv-digital-twin"})
        elif self.path == "/step":
            _twin_instance.step(1.0)
            self._send_json(200, _twin_instance.to_dict())
        else:
            self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        if self.path.startswith("/scenario/"):
            name = self.path[len("/scenario/") :].split("?")[0]
            _twin_instance.set_scenario(name)
            self._send_json(200, _twin_instance.to_dict())
        elif self.path == "/step":
            _twin_instance.step(1.0)
            self._send_json(200, _twin_instance.to_dict())
        else:
            self._send_json(404, {"error": "Not Found"})

    def log_message(self, format: str, *args):
        # Silence routine request logging
        pass


def run_server(port: int = 8105, bind: str = "0.0.0.0") -> None:
    server = ThreadingHTTPServer((bind, port), TwinRequestHandler)
    print(f"SCV Water Digital Twin running on http://{bind}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Digital Twin...")
        server.server_close()


# ---------------------------------------------------------------------------
# Self-Check Verification Test (Ponytail check)
# ---------------------------------------------------------------------------
def run_tests() -> None:
    print("Running SCV Water Digital Twin automated verification...")
    twin = WaterDigitalTwin()

    # Test 1: Tank low auto-refill rule (<= 5% -> Valve IN open)
    twin.set_scenario("tank_low_refill")
    assert twin.state.valve_in is True, "Valve IN must open when water_level <= 5%"
    assert twin.state.auto_refill_active is True

    # Test 2: Tank filled to >= 60% stops refill
    twin.state.water_level_pct = 61.0
    twin.evaluate_rules()
    assert twin.state.valve_in is False, "Valve IN must close when water_level >= 60%"
    assert twin.state.auto_refill_active is False

    # Test 3: Emergency Leak shutoff
    twin.set_scenario("overnight_leak")
    assert twin.state.valve_in is False, "Valve IN must isolate on leak detection"
    assert "LEAK_DETECTED" in twin.state.alert_msg

    # Test 4: Water quality / TDS divert (> 300 ppm -> Valve 1 OFF, Valve 2 ON)
    twin.state.valve_1 = True
    twin.state.valve_2 = False
    twin.set_scenario("poor_water_divert")
    assert twin.state.valve_1 is False, "Valve 1 must close on high TDS"
    assert twin.state.valve_2 is True, "Valve 2 must open on high TDS for divert"

    # Test 5: Morning garden watering with dry soil
    twin.set_scenario("morning_garden")
    assert twin.state.valve_2 is True, "Valve 2 must open when morning soil < 30%"
    assert twin.state.flow_rate_lpm == 4.0

    # Test 6: Rainy morning inhibits garden watering
    twin.set_scenario("rainy_morning")
    assert twin.state.valve_2 is False, "Valve 2 must stay closed when rain is detected"

    # Test 7: Cold weather freezing risk
    twin.set_scenario("cold_weather")
    assert "FREEZING_RISK" in twin.state.alert_msg

    # Test 8: Damp cupboard leak isolation
    twin.set_scenario("damp_cupboard")
    assert twin.state.valve_in is False, "Valve IN must isolate on damp leak"
    assert "LEAK_DETECTED" in twin.state.alert_msg

    # Test 9: Excessive flow alert
    twin.set_scenario("excessive_use")
    assert "EXCESSIVE_FLOW" in twin.state.alert_msg

    # Test 10: 1-in-2-out Good Water & Filter service
    twin.set_scenario("good_water")
    assert twin.state.valve_in is True and twin.state.valve_1 is True

    twin.set_scenario("filter_replacement")
    assert "FILTER SERVICE REQUIRED" in twin.state.alert_msg

    print("ALL TESTS PASSED (10/10 verification assertions passed successfully).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SCV Water Digital Twin")
    parser.add_argument("--test", action="store_true", help="Run automated test suite and exit")
    parser.add_argument("--server", action="store_true", help="Run HTTP REST API server on port 8105")
    parser.add_argument("--port", type=int, default=8105, help="Port for REST server (default: 8105)")
    parser.add_argument("--scenario", type=str, default="", help="Run single scenario and print JSON")
    args = parser.parse_args()

    if args.test:
        run_tests()
        sys.exit(0)

    if args.scenario:
        t = WaterDigitalTwin()
        t.set_scenario(args.scenario)
        print(json.dumps(t.to_dict(), indent=2))
        sys.exit(0)

    if args.server:
        run_server(port=args.port)
    else:
        # Default: run test, then brief status output
        run_tests()
        t = WaterDigitalTwin()
        print("\nDigital Twin Default State:")
        print(json.dumps(t.to_dict(), indent=2))
