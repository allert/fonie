#!/bin/bash
# Compile and deploy ESP32 firmware over Wi-Fi (OTA)

set -e
TARGET="${1:-fonie-esp32.local}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Compiling ESP32 Firmware..."
cd "$SCRIPT_DIR/../firmware/esp32"
pio run -e esp32c3_ota

echo "⚡ Flashing ESP32 over Wi-Fi (ArduinoOTA)..."
pio run -e esp32c3_ota -t upload --upload-port "$TARGET"

echo "✅ ESP32 deployed successfully!"
