# Fonie — GPIO Mapping

Complete pin assignments for all three compute nodes. Verified against running firmware source code where applicable.

---

## RP2040 Zero (Waveshare)

Source of truth: `firmware/pico/src/main.cpp`

### Assigned Pins

| GPIO | Function | Direction | Protocol | Notes |
|------|----------|-----------|----------|-------|
| GP0 | UART TX → Pi | OUT | Serial1, 115200 | `Serial1.setTX(0)` |
| GP1 | UART RX ← Pi | IN | Serial1, 115200 | `Serial1.setRX(1)` |
| GP2 | LED Ring data | OUT | PIO / WS2812 | 24 LEDs, 70mm ring around NFC scanner |
| GP3 | LED Matrix data | OUT | PIO / WS2812 | 64 LEDs, 8×8 matrix |
| GP4 | LED Strip L data | OUT | PIO / WS2812 | **NEW — not yet in firmware.** Speaker L surround |
| GP5 | LED Strip R data | OUT | PIO / WS2812 | **NEW — not yet in firmware.** Speaker R surround |
| GP6 | INA226 I2C SDA | I/O | Wire1, addr 0x40 | `Wire1.setSDA(6)` |
| GP7 | INA226 I2C SCL | OUT | Wire1 | `Wire1.setSCL(7)` |
| GP8 | DS18B20 temp sensor | IN | 1-Wire | **PLANNED.** Battery pack temperature |
| GP14 | Pololu OFF | OUT | Digital | **NEW — not yet in firmware.** Drive HIGH to cut power |
| GP15 | Button: Volume Down | IN | Digital, INPUT_PULLDOWN | HIGH when pressed |
| GP16 | Onboard RGB LED | OUT | PIO / WS2812 | Single NeoPixel on the RP2040 Zero board |
| GP26 | Button: Volume Up | IN | Digital, INPUT_PULLDOWN | HIGH when pressed |
| GP27 | Button: Forward/Next | IN | Digital, INPUT_PULLDOWN | HIGH when pressed |
| GP28 | Button: Play/Pause | IN | Digital, INPUT_PULLDOWN | HIGH when pressed. Also wired to Pololu pin A → GND |
| GP29 | Button: Back/Prev | IN | Digital, INPUT_PULLDOWN | HIGH when pressed |

### Inaccessible Pins

| GPIO | Reason |
|------|--------|
| GP9 | Blocked by header mounting in enclosure |
| GP10 | Blocked by header mounting in enclosure |
| GP11 | Blocked by header mounting in enclosure |
| GP12 | Blocked by header mounting in enclosure |
| GP13 | Blocked by header mounting in enclosure |

### Reserved Pins (Pico Internal)

| GPIO | Function |
|------|----------|
| GP23 | SMPS power save mode |
| GP24 | VBUS sense |
| GP25 | Onboard LED (directly on RP2040 chip, separate from WS2812 on GP16) |

### Free Pins

GP17, GP18, GP19, GP20, GP21, GP22 — six GPIOs available for future use.

### SWD Programming Pads

Located on the bottom of the RP2040 Zero board (may be difficult to access depending on mounting).

| Pad | Function | Notes |
|-----|----------|-------|
| SWDIO | Serial Wire Data I/O | Connect to a Pi GPIO for OpenOCD flashing |
| SWCLK | Serial Wire Clock | Connect to a Pi GPIO for OpenOCD flashing |
| GND | Ground | Already shared with Pi via power rails |

---

## ESP32-C3 Super Mini

Source of truth: `firmware/esp32/src/main.cpp`

### Assigned Pins

| GPIO | Function | Direction | Protocol | Notes |
|------|----------|-----------|----------|-------|
| 3 | PN532 I2C SDA | I/O | Wire, default addr | `Wire.begin(3, 4)` |
| 4 | PN532 I2C SCL | OUT | Wire | |
| 20 | UART RX ← Pi | IN | HardwareSerial(1), 115200 | `RpiSerial.begin(115200, SERIAL_8N1, 20, 21)` |
| 21 | UART TX → Pi | OUT | HardwareSerial(1), 115200 | |

### Notes

- `Serial` (USB CDC) is used for debug output at 115200 baud
- `HardwareSerial RpiSerial(1)` uses UART1 — the C3 only exposes UART0 (USB) and UART1
- No PN532 reset pin is used in the current firmware (removed during C3 port; the WROOM version used GPIO15 for reset)

---

## Raspberry Pi 4B

### Assigned Pins

| Pi GPIO | Physical Pin | Function | Device | Notes |
|---------|-------------|----------|--------|-------|
| GPIO 0 | 27 | UART2 TX → ESP32-C3 RX | `/dev/ttyAMA2` | `dtoverlay=uart2` |
| GPIO 1 | 28 | UART2 RX ← ESP32-C3 TX | `/dev/ttyAMA2` | |
| GPIO 12 | 32 | UART5 TX → Pico GP1 (RX) | `/dev/ttyAMA5` | `dtoverlay=uart5` |
| GPIO 13 | 33 | UART5 RX ← Pico GP0 (TX) | `/dev/ttyAMA5` | |
| GPIO 18 | 12 | I2S BCLK | Merus amp hat | `dtoverlay=merus-amp` |
| GPIO 19 | 35 | I2S LRCLK | Merus amp hat | |
| GPIO 20 | 38 | I2S DIN | Merus amp hat | |
| GPIO 21 | 40 | I2S DOUT | Merus amp hat | |

### Planned Pins

| Pi GPIO | Function | Notes |
|---------|----------|-------|
| TBD | SWDIO → Pico debug pad | For OpenOCD flashing of Pico firmware |
| TBD | SWCLK → Pico debug pad | For OpenOCD flashing of Pico firmware |

### Known Conflicts

- `dtoverlay=uart4` (GPIO 8/9) conflicts with `dtoverlay=merus-amp` — do not use.
- The InnoMaker amp hat consumes GPIO 18-21 for I2S. These pins pass through the hat's header but are not available for other functions.

### config.txt

```
[all]
enable_uart=1
dtoverlay=merus-amp
dtoverlay=uart2
dtoverlay=uart5
```

### UART Wiring Summary

```
Pi GPIO 0  (pin 27) ──TX──→ ESP32-C3 GPIO 20 (RX)
Pi GPIO 1  (pin 28) ←─RX─── ESP32-C3 GPIO 21 (TX)

Pi GPIO 12 (pin 32) ──TX──→ Pico GP1 (RX)
Pi GPIO 13 (pin 33) ←─RX─── Pico GP0 (TX)
```

Both UARTs run at 115200 baud, 8N1.