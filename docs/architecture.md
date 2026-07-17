# Fonie — System Architecture

## Overview

Fonie is a portable RFID music player for kids. Three compute nodes communicate over UART, coordinated by a Raspberry Pi 4B running a Flask web application.

```
                        ┌─────────────────────────────────────┐
                        │          Raspberry Pi 4B             │
                        │  Flask web UI · yt-dlp · mpv/aplay  │
                        │  /dev/ttyAMA2       /dev/ttyAMA5    │
                        └──────┬──────────────────┬───────────┘
                               │ UART 115200      │ UART 115200
                               │                  │
                   ┌───────────┴───┐        ┌─────┴───────────┐
                   │  ESP32-C3     │        │  RP2040 Zero    │
                   │  Super Mini   │        │  (Waveshare)    │
                   │               │        │                 │
                   │  NFC/RFID     │        │  LEDs, Buttons  │
                   │  WiFi Config  │        │  Battery Monitor│
                   │  WiFi OTA     │        │  Power Control  │
                   └───────┬───────┘        └──┬──────┬───┬───┘
                           │ I2C                │PIO   │I2C│GPIO
                   ┌───────┴───────┐     ┌─────┘  ┌───┘   └───────┐
                   │    PN532      │     │LEDs    │INA226   │Buttons
                   │  NFC Module   │     │        │         │(×5)
                   └───────────────┘     │        │         │
                                    ┌────┴────┐   │    ┌────┴────┐
                                    │Ring 24  │   │    │Prev     │
                                    │Matrix 64│   │    │Play     │
                                    │Strip L  │   │    │Next     │
                                    │Strip R  │   │    │Vol+     │
                                    │RGB ×1   │   │    │Vol-     │
                                    └─────────┘   │    └─────────┘
                                           ┌──────┴──────┐
                                           │  INA226     │
                                           │  Current &  │
                                           │  Voltage    │
                                           └──────┬──────┘
                                                  │
                                           ┌──────┴──────┐
                                           │ 3S 18650    │
                                           │ Battery     │
                                           │ + BMS       │
                                           └─────────────┘
```

## Compute Nodes

### Raspberry Pi 4B — The Brain

Runs all high-level logic: music playback, web interface, content management.

- **OS:** Raspberry Pi OS (64-bit)
- **Application:** `app.py` — Flask web server (systemd service: `fonie.service`)
- **Audio:** yt-dlp downloads from YouTube Music (Premium cookies via Chromium), playback via mpv/aplay through I2S to InnoMaker Merus Amp Hat
- **Communication:** two hardware UARTs to the MCUs
  - `/dev/ttyAMA2` (uart2, GPIO0/1) → ESP32-C3
  - `/dev/ttyAMA5` (uart5, GPIO12/13) → RP2040
- **Device tree overlays** (`/boot/firmware/config.txt`):
  ```
  dtoverlay=merus-amp
  dtoverlay=uart2
  dtoverlay=uart5
  ```
  Note: `dtoverlay=uart4` (GPIO8/9) conflicts with the merus-amp overlay and cannot be used.

### ESP32-C3 Super Mini — NFC Handler

Single job: detect RFID tags and send events to the Pi.

- **Framework:** Arduino (elechouse PN532 library)
- **NFC:** PN532 module over I2C (GPIO3 SDA, GPIO4 SCL)
- **Pi UART:** HardwareSerial(1) on GPIO20 (RX) / GPIO21 (TX), 115200 baud
- **USB Serial:** debug output at 115200 baud
- **Planned:** WiFi captive portal for network config, ArduinoOTA for firmware updates

### RP2040 Zero (Waveshare) — LED/Button/Power Controller

Drives all user-facing I/O, monitors battery, and manages power state.

- **Framework:** Arduino (Earle Philhower core)
- **LEDs:** 5 NeoPixel outputs via PIO (ring, matrix, two speaker strips, onboard RGB)
- **Buttons:** 5 buttons with INPUT_PULLDOWN, read via polling with debounce
- **Battery:** INA226 over I2C (Wire1) for voltage/current, SoC via coulomb counting + NCA OCV lookup
- **Power:** Pololu 2808 OFF pin for soft shutdown
- **Pi UART:** Serial1 on GP0 (TX) / GP1 (RX), 115200 baud
- **USB Serial:** debug output at 115200 baud
- **Animation engine:** 60fps state machine (OFF, TAG_ON_BURST, PLAYING, PAUSED, TAG_OFF_FADE, VOLUME)

## Communication Protocol

All inter-device communication is newline-delimited JSON over UART at 115200 baud.

### ESP32-C3 → Pi

```json
{"event":"READY"}
{"event":"TAG_ON","uid":"04A2F3B1"}
{"event":"TAG_OFF","uid":"04A2F3B1"}
{"event":"ERROR","msg":"No PN532 found"}
```

### Pi → Pico

```json
{"event":"READY"}
{"event":"IDLE"}
{"event":"TAG_ON","mapped":true}
{"event":"TAG_OFF"}
{"event":"TAG_UNKNOWN"}
{"event":"PLAYING","r":0,"g":200,"b":200}
{"event":"PAUSED"}
{"event":"VOLUME","level":75}
{"event":"BRIGHTNESS","ring":60,"matrix":40}
```

### Pico → Pi

```json
{"event":"BUTTON","button":"play","pressed":true}
{"event":"BUTTON_ACTION","action":"pause"}
{"event":"BUTTON_ACTION","action":"resume"}
{"event":"BUTTON_ACTION","action":"next"}
{"event":"BUTTON_ACTION","action":"prev"}
{"event":"BUTTON_ACTION","action":"volume","level":85}
{"event":"SOC","level":72,"voltage":11.400,"current":350.1,"charging":false}
{"event":"SHUTDOWN"}
```

## Audio Chain

```
Pi (mpv/aplay) → I2S → InnoMaker 2×80W Merus Amp Hat → Infinity Reference 4032 (×2, 4Ω)
```

The amp hat accepts 9-24V DC input and powers the Pi through the GPIO header. Audio must be 24-bit or 32-bit (16-bit is not supported by the Merus MA12070P).

**Status:** Amp hat currently fried (wrong power supply plugged into barrel jack). Replacement pending.

## Physical Layout

Stacked from bottom to top:

1. Bottom lid of enclosure
2. 3S 18650 battery pack + BMS
3. Raspberry Pi 4B
4. InnoMaker Amp Hat (on Pi GPIO header)
5. Custom MCU board with RP2040 Zero + ESP32-C3 + PN532 + connectors

The entire stack slides into the enclosure from the bottom. This makes Pi USB ports and some Pico pins physically inaccessible once assembled.

## Firmware Update Paths

| Target | Method | Extra Hardware |
|--------|--------|---------------|
| Pi | `git pull` + `systemctl restart fonie` via SSH | None |
| ESP32-C3 | WiFi OTA (ArduinoOTA) | None |
| Pico | SWD from Pi via OpenOCD (if debug pads accessible) | 2 wires to Pi GPIO |
| Pico | UART OTA from Pi (planned) | None (uses existing UART) |