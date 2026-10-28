# Smart Household Water System: Sensors, Scenarios & Automatic Valves
## SCV Water Digital Twin & Demo Scenarios Specification

This document defines the physical model, sensor suite, actuator mapping, multi-sensor decision rules, daily timeline, and digital twin scenarios for the SCV Water project.

---

## 1. System Architecture: 1 Input, 2 Outputs

- **Input (Valve IN)**: Incoming municipal/main supply water or storage tank refill valve / emergency isolation valve.
- **Output 1 (Valve 1)**: Filtered/clean water line supplying kitchen tap, cooking outlet, and bathroom/laundry.
- **Output 2 (Valve 2)**: Garden irrigation, toilet flush, or divert/reject/drain/greywater reuse line.

### Core 1-Input, 2-Output Operational Modes
1. **Scenario 1 - Good Water**:
   - Sensor detects acceptable TDS / turbidity (< threshold).
   - Action: "Filter & Deliver" -> `Valve IN = ON`, `Valve 1 = ON`, `Valve 2 = OFF`.
2. **Scenario 2 - Poor Water**:
   - Sensor detects poor water quality (elevated TDS / turbidity).
   - Action: "Divert" -> `Valve 1 = OFF`, `Valve 2 = ON` (redirect to drain or secondary treatment).
3. **Scenario 3 - Filter Flushing**:
   - Filter runtime exceeds threshold $X$ hours.
   - Action: "Auto Flush" -> `Valve IN = ON`, `Valve 2 = ON` for 30–60 seconds, then resume normal delivery.
4. **Scenario 4 - Filter Replacement Alert**:
   - Differential pressure drops or flow rate decreases significantly under open valve conditions.
   - Action: "Filter Service Required" -> Close normal supply, log fault, notify user.
5. **Scenario 5 - Water-Saving / Rainwater Reuse Mode**:
   - Reject / greywater / rainwater diverted to dedicated storage or irrigation rather than lost to drain.
   - Action: "Reuse Water" -> Output 2 redirects water to secondary storage/reuse tank based on quality & demand.

---

## 2. Sensor Suite Specification

| Sensor | Category | Role & Purpose | Recommended Hardware |
| :--- | :--- | :--- | :--- |
| **Temperature Sensor** | Recommended | Measures ambient / pipe water temperature; freezing alert & hot weather detection. | DHT22 / DS18B20 |
| **Humidity Sensor** | Recommended | Relative air humidity; monitors damp utility rooms & reinforces leak detection. | GY-21 / HTU21 / DHT22 |
| **Soil-Moisture Sensor** | Recommended | Measures garden soil volumetric water content; triggers irrigation only on actual need. | Capacitive Soil Moisture v1.2 |
| **Water-Flow Sensor** | Recommended | Real-time flow rate (L/min, mL/s) & cumulative usage (L); detects leakage and abnormal consumption. | YF-S201 (Hall effect pulse) |
| **Ultrasonic Level Sensor** | Recommended | Non-contact water level in storage tank (0–100%); triggers auto-refill & overflow prevention. | HC-SR04 / JSN-SR04T |
| **Real-Time Clock (RTC)** | Recommended | Time of day, scheduled morning/evening routines without requiring cloud NTP lock. | DS3231 / Host Linux Clock |
| **Light Sensor** | Recommended | Ambient illuminance (lux); distinguishes day/night, sun intensity for solar/irrigation cycles. | BH1750 |
| **Rain Sensor** | Recommended | Detects instantaneous rain; inhibits scheduled garden watering when wet. | Raindrop sensor / Weather API |
| **Water-Leak Sensor** | Recommended | Floor / pipe joint wet contact sensor; immediate alert & emergency isolation. | Resistive trace probe / Float switch |
| **Motion / Occupancy** | Optional | Zone movement; infers human presence in kitchen/bath to confirm active demand. | PIR HC-SR501 |
| **Water-Quality (TDS)** | Optional | Total Dissolved Solids / electrical conductivity; guides divert vs clean water delivery. | Analog TDS Sensor Meter |

---

## 3. Actuator & Valve Allocation

- **Valve IN (Inlet / Refill / Safety)**: Controls main incoming supply to storage tank or isolates the entire household upon leak confirmation.
- **Valve 1 (Potable / Indoor Clean Line)**: Supplies kitchen tap, cooking outlet, bathroom shower, and laundry.
- **Valve 2 (Secondary / Outdoor / Drain Line)**: Garden irrigation, toilet flush, filter backwash divert, and reject water recovery.

---

## 4. Multi-Sensor Smart Decision Matrix (12 Scenarios)

| # | Household Scenario | Primary Sensors | Valve Actions | Operational Rational |
| :--- | :--- | :--- | :--- | :--- |
| **1** | **Morning Garden Watering** | Time, Soil Moisture, Rain, Flow | **Valve 2 OPEN** if soil dry & scheduled & rain=NO; else CLOSED | Water plants strictly upon moisture deficit, avoid evaporation loss |
| **2** | **Hot Afternoon** | Temperature, Soil Moisture, Flow | **Valve 2 defer or pulse** if soil critical & tank allows | Respond to thermal stress without midday scorching; defer if peak heat |
| **3** | **Rainy Morning** | Rain, Soil Moisture, Time | **Valve 2 stays CLOSED** | Rain covers moisture requirement; eliminate water waste |
| **4** | **Afternoon Cooking** | Time, Flow, (Occupancy) | **Valve 1 OPEN** on demand | Provide high-grade filtered water and track batch volume |
| **5** | **Evening Shower** | Time, Flow | **Valve 1 OPEN** on demand | Deliver bathroom water and record flow/consumption profile |
| **6** | **Tank Running Low** | Water Level, Flow | **Valve IN OPEN** (Auto Refill) | Triggered when `water_level <= 5%` to prevent pump dry-run |
| **7** | **Tank Nearly Full** | Water Level | **Valve IN CLOSED** (Stop Refill) | Triggered when `water_level >= 60%` (or target cap) to prevent overflow |
| **8** | **Overnight Leak** | Time (00:00-05:00), Flow, Leak Sensor | **Valve IN CLOSED**, Trigger Alert | Flow detected during idle sleep hours + wet floor probe |
| **9** | **Damp Utility Cupboard** | Humidity, Leak Sensor, Flow | **Valve IN CLOSED** upon confirmation | High humidity spike + leak sensor tripped indicates pipe burst |
| **10** | **Cold Weather / Freeze Risk** | Temperature (< 2°C), Flow | Keep valves safe; trigger freeze protection | Guard against burst pipes; alert household before ice forms |
| **11** | **Filter Maintenance** | Flow, Runtime | **Valve 2 OPEN** (Backwash / Flush) | Auto-flush filter to drain for 30–60s when cumulative hours hit threshold |
| **12** | **Excessive Household Use** | Flow, Cumulative Time | Issue warning alert; isolate only if fault | Track continuous abnormal open taps; warn before shutoff |

---

## 5. Complete Daily Operational Timeline

| Time | Event | Trigger Sensors | System Action |
| :---: | :--- | :--- | :--- |
| **06:00** | Morning Garden Check | Time, Soil Moisture, Rain, Flow | If soil moisture < 30% and Rain=NO, Valve 2 opens for 5 min (~20 L). |
| **08:00** | Breakfast & Kitchen Demand | User command, Flow sensor | Valve 1 delivers kitchen water; tracks 3 L/min, accumulates total. |
| **12:30** | Midday Hot Weather Check | Temperature, Soil Moisture, Light | Temperature >= 35°C detected. If soil dry, schedules evening top-up instead of immediate scald. |
| **15:00** | Tank Water Level Inspection | Ultrasonic Level, Flow | Tank level drops to <= 5%. Valve IN opens auto-refill until level hits >= 60%. |
| **19:00** | Evening Shower & Laundry | Flow, Time, User request | Valve 1 supplies indoor demand; logs shower volume. |
| **23:00** | Overnight Guard Active | Time, Flow, Leak Sensor | If Flow > 0.1 L/min for 30 min or Leak=WET, Valve IN closes immediately and raises alert. |

---

## 6. Mathematical Decision Calculations

### A. Garden Watering Volume
$$\text{Water Supplied} = \text{Flow Rate} \times \text{Duration} = 4\text{ L/min} \times 5\text{ min} = 20\text{ Litres}$$
$$\text{Decision Rule: } \mathbf{IF } (\text{Time} = \text{06:00--08:00}) \land (\text{Soil} < 30\%) \land (\text{Rain} = \text{False}) \implies \text{Open Valve 2}$$

### B. Hot-Weather Irrigation
$$\mathbf{IF } (\text{Temp} \ge 35^\circ\text{C}) \land (\text{Soil} < 25\%) \land (\text{Rain} = \text{False}) \implies \text{HOT\_WEATHER\_CHECK: IRRIGATION\_ELIGIBLE (defer to late afternoon)}$$

### C. Kitchen Daily Budget
$$\text{Kitchen Session} = 3\text{ L/min} \times 4\text{ min} = 12\text{ Litres}$$
$$\text{Remaining Allowance} = 40\text{ L} - 12\text{ L} = 28\text{ Litres}$$

### D. Overnight Leak Detection
$$\text{Potential Loss} = 0.3\text{ L/min} \times 60\text{ min/h} \times 2\text{ h} = 36\text{ Litres}$$
$$\mathbf{IF } (\text{Hour} \in [0, 5]) \land (\text{Flow} > 0.1\text{ L/min}) \land (\text{Leak} = \text{True}) \implies \text{CLOSE Valve IN } \land \text{ FIRE LEAK\_ALERT}$$

---

## 7. Digital Twin Automation Logic Rules

```python
# Primary Automated Control Rules
IF water_level <= 5%:
    AUTO_FILL = True
    Valve_IN = OPEN

IF water_level >= 60%:
    STOP_FILL = True
    Valve_IN = CLOSED

IF flow_rate > 1.0: # L/min
    HIGH_FLOW_ALERT = True
    IF leak_detected OR unexpected_hours:
        Valve_IN = CLOSED # Isolate supply

IF tds_quality > 300: # ppm
    Valve_1 = CLOSED # Divert away from clean water
    Valve_2 = OPEN   # Redirect to drain/secondary reuse
```

---

## 8. Virtual Linux & Digital Twin Environment
The Digital Twin runs on Linux (systemd service `scv-digital-twin.service` or CLI runner `digital_twin/run_twin.py`) with continuous physics state, virtual sensors, valve actuator responses, and telemetry publishing.
