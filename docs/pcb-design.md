# Fonie — Custom Carrier PCB Design Specification 🛠️📐

This document outlines the complete schematic architecture, component selection (BOM), connector layout, and KiCad design guide for creating a single, professional **Fonie Mainboard Carrier PCB**.

---

## 🎯 Objectives & Benefits

1. **Zero Point-to-Point Wiring Harnesses:** Replaces loose wires, crimp pins, and protoboards with a clean 2-layer PCB.
2. **Direct Raspberry Pi 4 Docking:** Features a female 2×20 GPIO socket header that docks directly onto the Pi 4.
3. **Integrated Compute Nodes:** Sockets/footprints for:
   - **RP2040 Zero** (Waveshare module or stamp footprint)
   - **ESP32-C3 SuperMini** (or ESP32-C3-WROOM module)
   - **INA226 I2C Battery Telemetry Sensor**
4. **Onboard Power Management:**
   - Onboard Pololu 2808 soft switch connector / circuit
   - High-efficiency 5V 3A Synchronous Step-Down Buck Converter (e.g., MP2307 / TPS54331)
5. **Clean Polarized Connectors:**
   - **JST-PH 2.0mm / 2.54mm headers** for 5× Front Buttons, NeoPixel Matrix, NeoPixel Ring, Left/Right Surround LED Strips, and PN532 NFC scanner.

---

## 🏗️ Schematic Block Diagram

```
                 3S Li-ion Battery Pack (9.0V – 12.6V)
                                │
                                ▼
                       ┌────────────────┐
                       │  INA226 Sensor │  (I2C to Pico)
                       └───────┬────────┘
                               │
                               ▼
                       ┌────────────────┐
                       │  Pololu 2808   │  ◄── Vol Up Button (A pin)
                       │  Soft Switch   │  ◄── Pico GP14 (OFF pin)
                       └───────┬────────┘
                               │ Switched 12V (VOUT)
                               ▼
            ┌──────────────────┴──────────────────┐
            │                                     │
            ▼                                     ▼
   ┌─────────────────┐                   ┌──────────────────┐
   │ 5V 3A Buck Reg  │                   │ Merus Amp Hat /  │
   │ (TPS54331/MP2307)                   │ DC Barrel Jack   │
   └────────┬────────┘                   └──────────────────┘
            │ 5.1V Rail
            ├──────────────────────┬──────────────────────┐
            ▼                      ▼                      ▼
    Raspberry Pi 4 2×20      RP2040 Zero            ESP32-C3
   (GPIO Pins 2/4 + GND)    (5V & GND Pins)        (5V & GND Pins)
```

---

## 📌 Connector & Pin Routing Netlist

### 1. Raspberry Pi 4 (2×20 Header - Female Socket)
* **Pin 2, 4 (+5V):** Connected to Onboard 5.1V Buck Converter Output
* **Pin 6, 9, 14, 20, 25, 30, 34, 39 (GND):** Common Ground Plane
* **Pin 8 (GPIO 14 - UART0 TX):** Routed to RP2040 Pico `GP1` (RX)
* **Pin 10 (GPIO 15 - UART0 RX):** Routed to RP2040 Pico `GP0` (TX)
* **Pin 15 (GPIO 22 - Poweroff Sense):** Routed to RP2040 Pico `GP8`
* **Pin 32 (GPIO 12 - UART5 TX):** Routed to ESP32-C3 `IO20` (RX)
* **Pin 33 (GPIO 13 - UART5 RX):** Routed to ESP32-C3 `IO21` (TX)

### 2. RP2040 Pico Module (Sockets / Castellated Stamp)
* **GP0 (TX):** → Pi GPIO 15 (RX)
* **GP1 (RX):** ← Pi GPIO 14 (TX)
* **GP2:** → JST Connector: NeoPixel Ring Data (24 LEDs)
* **GP3:** → JST Connector: NeoPixel 8×8 Matrix Data (64 LEDs)
* **GP4:** → JST Connector: Left Surround LED Strip (26 LEDs)
* **GP5:** → JST Connector: Right Surround LED Strip (26 LEDs)
* **GP6 (SDA), GP7 (SCL):** → INA226 Sensor Module
* **GP8:** ← Pi GPIO 22 (Shutdown Sense)
* **GP14:** → Pololu 2808 `OFF` Pin
* **GP15:** ← JST Connector: Volume Down Button
* **GP26:** ← JST Connector: Volume Up Button (+ routed to Pololu Pin `A`)
* **GP27:** ← JST Connector: Next / Forward Button
* **GP28:** ← JST Connector: Play / Pause Button
* **GP29:** ← JST Connector: Prev / Back Button

### 3. ESP32-C3 Module
* **IO20 (RX):** ← Pi GPIO 12 (TX)
* **IO21 (TX):** → Pi GPIO 13 (RX)
* **IO3 (SDA), IO4 (SCL):** → PN532 NFC Module (I2C Header)

---

## 📦 Bill of Materials (BOM)

| Qty | Component Description | Footprint / Package | Notes |
|:---:|:---|:---|:---|
| 1 | Custom Carrier PCB | 2-Layer 100mm × 80mm | FR4 1.6mm thickness, Green/Black silkscreen |
| 1 | 2×20 Female Header Socket | 2.54mm Pitch Through-Hole | Docks directly onto Pi 4 GPIO pins |
| 1 | MP2307 / TPS54331 5V 3A Buck | Integrated SMT / Module | Adjustable trimpot or fixed 5.1V resistors |
| 1 | Pololu 2808 Switch Header | 1×5 2.54mm Male Header | VIN, GND, VOUT, A, OFF |
| 1 | INA226 Telemetry Header | 1×4 2.54mm Female Socket | VCC, GND, SDA, SCL |
| 5 | JST-PH 3-Pin / 4-Pin Headers | 2.0mm / 2.54mm Shrouded | NeoPixels, Buttons, PN532 I2C |
| 5 | 100nF Ceramic Capacitors | 0805 SMD | Decoupling power pins |
| 2 | 10kΩ Resistors | 0805 SMD | Pull-ups / line protection |

---

## 🎨 Recommended Next Steps for KiCad Fabrication

1. **KiCad Project Setup:** Create `Fonie_Carrier_v1.0.kicad_pro` using the pin netlist above.
2. **JLCPCB / PCBWay Ordering:**
   - 2-layer PCB fabrication (~$5 for 5 boards).
   - Optional SMT Assembly (JLCPCB SMT) to pre-solder all capacitors, resistors, regulators, and JST connectors automatically!

This carrier PCB transforms Fonie into a clean, drop-in, plug-and-play hardware module! 🚀
