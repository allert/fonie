# Fonie — Power Architecture

## Overview

Fonie runs on a custom 3S 18650 battery pack with USB-C PD charging. A Pololu 2808 soft power switch gates all downstream loads, enabling clean power-on via button press and soft shutdown from firmware.

## Power Flow Diagram

```
                USB-C PD Charger
                (15-20V input)
                      │
                      ▼
              ┌───────────────┐
              │  CC/CV Buck   │
              │  Converter    │
              │  → 12.7V out  │
              └───────┬───────┘
                      │ charge current
                      ▼
              ┌───────────────┐
              │     BMS       │
              │  3S, 20A+     │         ┌──────────────────┐
              │  w/ balancing │◄────────│  3S 18650 Pack   │
              │               │────────►│  3 × 2900mAh     │
              │  B+  P+  P-  │         │  NCA cells       │
              └──┬───┬────┬──┘         │  9.0 - 12.6V     │
                 │   │    │            └──────────────────┘
                 │   │    │
        ┌────────┘   │    └──────────────────────┐
        │            │                           │
   ┌────┴────┐       │                      Common GND
   │ INA226  │       │                      (all devices)
   │ Current │       │
   │ Sensor  │       │
   └────┬────┘       │
        │            │
        └────┬───────┘
             │ BMS P+ output (9-12.6V)
             ▼
     ┌───────────────┐
     │  Pololu 2808  │
     │  Power Switch │
     │  LV, 2-20V   │
     │  6A cont.     │
     │               │
     │  VIN ← BMS P+ │
     │  VOUT → loads │
     │  A ← button  │
     │  OFF ← GP14  │
     └───────┬───────┘
             │ VOUT (switched, 9-12.6V)
             │
        ┌────┴────────────────────┐
        │                         │
        ▼                         ▼
┌───────────────┐         ┌───────────────┐
│ InnoMaker     │         │  5V Buck      │
│ Merus Amp Hat │         │  Converter    │
│ 2×80W         │         │               │
│               │         └───────┬───────┘
│ DC in: 9-24V  │                 │ 5V
│ Powers Pi via │                 │
│ GPIO header   │           ┌─────┼─────────────┐
│               │           │     │             │
│ → Speakers    │           ▼     ▼             ▼
└───────────────┘       ┌──────┐ ┌────────┐ ┌──────┐
                        │Pico  │ │ESP32-C3│ │PN532 │
                        │VSYS  │ │VIN     │ │VCC   │
                        └──────┘ └────────┘ └──────┘
                           │
                           ▼
                     LED Ring, Matrix,
                     Strips (5V power)
```

## Components

### Battery Pack

- **Chemistry:** NCA (Nickel Cobalt Aluminum) Li-ion
- **Configuration:** 3S1P (three cells in series)
- **Cells:** 2900mAh each (BAK N18650CL-29 type)
- **Voltage range:** 9.0V (empty, 3.0V/cell) to 12.6V (full, 4.2V/cell)
- **Nominal voltage:** 11.1V (3.7V/cell)

### BMS (Battery Management System)

- **Configuration:** 3S
- **Continuous discharge rating:** 20A+
- **Features:** overcharge, overdischarge, overcurrent, short circuit protection, cell balancing
- **Separate charge/discharge paths** for simultaneous charge and playback (true pass-through)

### Charging

- **Input:** USB-C PD charger (65W+ recommended)
- **PD negotiation:** 15V or 20V (handled by charger/device)
- **CC/CV buck converter:** steps PD voltage down to 12.7V
  - CC (constant current) phase: bulk charging
  - CV (constant voltage) phase: taper charging as cells approach 4.2V/cell
- **Charging path is independent of the Pololu switch** — the CC/CV buck connects directly to the BMS charge input, so charging occurs whether the unit is on or off

### INA226 Current/Voltage Sensor

- **Position:** high-side, between battery B+ terminal and BMS B+ terminal
- **Shunt resistor:** 2mΩ (R002)
- **Measurement:**
  - Bus voltage: battery pack voltage (LSB = 1.25mV)
  - Shunt current: bidirectional (LSB = 2.5µV across shunt)
  - Positive current = discharging
  - Negative current = charging
- **I2C address:** 0x40
- **Connected to:** Pico RP2040 via Wire1 (GP6 SDA, GP7 SCL)
- **Config register:** 0x4527 (16-sample averaging, 1.1ms conversion, continuous shunt+bus)
- **Calibration register:** 25600

### Pololu 2808 Mini Pushbutton Power Switch

- **Variant:** LV (Low Voltage) with reverse voltage protection
- **Input range:** 2.2V to 20V
- **Continuous current:** 6A (at 55°C ambient)
- **Off-state current:** ~0.01µA (negligible)
- **On-resistance:** 16mΩ at 4.5V VIN

**Wiring:**

| Pololu Pin | Connection | Notes |
|------------|------------|-------|
| VIN | BMS P+ output | 9-12.6V from battery |
| VOUT | All downstream loads | Switched power rail |
| GND | Common ground | Shared with all devices |
| Pin A | Play/Pause button (other leg to GND) | A→GND = on-only operation |
| OFF | Pico GP14 | Drive HIGH to force power off |

**Behavior:**
- Pin A wired to GND via the button means pressing can only turn the switch ON, never off
- Subsequent button presses while already on have no effect (no toggle)
- Shutdown is controlled exclusively by the Pico driving the OFF pin HIGH
- When off, quiescent draw is 0.01µA — battery self-discharge is the limiting factor

### 5V Buck Converter

- **Input:** Pololu VOUT (9-12.6V)
- **Output:** 5V regulated
- **Feeds:** Pico (VSYS pin), ESP32-C3 (VIN), PN532 (VCC), all WS2812 LEDs (5V power rail)

**Caution:** Do not connect the Pico to a PC via USB while the battery is connected. USB 5V backfeeds into the buck converter output, creating two voltage sources on the same rail. This has destroyed buck converters in this project.

### InnoMaker 2×80W Merus Amp Hat

- **Amplifier IC:** Infineon MA12070P (class-D)
- **Input:** I2S from Pi (GPIO 18-21)
- **Power input:** DC barrel jack, 9-24V (connected to Pololu VOUT)
- **Pi power:** The amp hat supplies 5V to the Pi through the GPIO header from its DC input. When the amp hat is installed, the Pi does not need separate 5V power.
- **Speakers:** 2× Infinity Reference 4032 (4Ω, 4" coaxial)
- **Audio format:** 24-bit or 32-bit only (16-bit not supported)
- **Status:** Currently fried (incorrect power supply plugged into barrel jack). Replacement pending.

## SoC (State of Charge) Estimation

Implemented in Pico firmware. Combines coulomb counting with voltage-based correction.

### Method

1. **Coulomb counting:** INA226 current is integrated over time (read every 500ms). Discharge current decreases SoC, charge current increases SoC.

2. **OCV correction:** When the pack is at rest (current below 50mA for 30+ seconds), the measured voltage is compared against an 11-point NCA OCV lookup table. SoC is gently blended toward the voltage-based estimate (70% existing + 30% voltage-based).

3. **Anchor points:**
   - **Full:** When pack voltage ≥ 12.5V AND charging AND current < 150mA (CV taper detected), SoC snaps to 100%.
   - **Empty:** When pack voltage ≤ 9.1V AND not charging, SoC snaps to 0%.

### OCV Lookup Table (3S NCA pack)

| SoC | Pack Voltage |
|-----|-------------|
| 0% | 9.00V |
| 10% | 9.84V |
| 20% | 10.50V |
| 30% | 10.86V |
| 40% | 11.10V |
| 50% | 11.25V |
| 60% | 11.40V |
| 70% | 11.55V |
| 80% | 11.76V |
| 90% | 12.06V |
| 100% | 12.60V |

### Reporting

SoC is reported to the Pi via UART every 30 seconds and immediately on charge state changes:

```json
{"event":"SOC","level":72,"voltage":11.400,"current":350.1,"charging":false}
```

## Soft Power On/Off Sequence

### Power On

1. Unit is off — Pololu latched off, all loads unpowered, ~0.01µA draw
2. User presses play/pause button
3. Button connects Pololu pin A to GND → Pololu latches ON
4. VOUT comes up → 5V buck starts → Pi, Pico, ESP32 boot
5. Pico firmware starts, begins normal operation

### Soft Shutdown

1. User long-presses play/pause (5 seconds)
2. Pico detects long press (does NOT trigger normal play/pause action)
3. Pico sends `{"event":"SHUTDOWN"}` to Pi over UART
4. Pi executes `sudo shutdown -h now`
5. Pico runs LED fade-out animation as visual feedback
6. After 20-second timeout (generous for Pi to complete shutdown) (or can we detect when Pi is fully shut down?)
7. Pico drives GP14 HIGH → Pololu OFF pin activates → power rail drops
8. Everything powers down cleanly. Battery draw returns to 0.01µA.

### Charging While Off

The CC/CV buck converter connects directly to the BMS, bypassing the Pololu. The battery charges normally whether the unit is on or off.

## Thermal Monitoring (Planned)

| Sensor | Location | Connected To | Notes |
|--------|----------|-------------|-------|
| DS18B20 (1-Wire probe) | Battery pack surface | Pico GP8 | Safety cutoff if > 45°C (charge) or > 60°C (discharge) |
| RP2040 internal temp | Pico die | ADC channel 4 | `analogReadTemp()` |
| BCM2711 internal temp | Pi SoC | `vcgencmd measure_temp` | Throttles at 80°C |