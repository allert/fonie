#!/bin/bash
# Compile and deploy ESP32 firmware over Wi-Fi (OTA)

set -e

# Add PlatformIO Core to PATH if it exists but is not in PATH
if ! command -v pio &> /dev/null; then
    if [ -x "$HOME/.platformio/penv/bin/pio" ]; then
        export PATH="$HOME/.platformio/penv/bin:$PATH"
    fi
fi

TARGET="${1:-fonie-esp32.local}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PI_TARGET="${2:-allert@fonie2.local}"

echo "🚀 Compiling ESP32 Firmware..."
cd "$SCRIPT_DIR/../firmware/esp32"
pio run -e esp32c3_ota

echo "📡 Instructing Pi to connect ESP32 to Wi-Fi over UART..."
WIFI_RESP=$(ssh "$PI_TARGET" "curl -s -X POST http://127.0.0.1:5001/api/esp32/connect_wifi" || echo "{}")
echo "$WIFI_RESP"
ESP_IP=$(echo "$WIFI_RESP" | grep -o '"ip":"[^"]*' | cut -d'"' -f4 || true)

if [ -n "$ESP_IP" ] && [ "$ESP_IP" != "null" ]; then
    echo "🎯 Found ESP32 IP over UART: $ESP_IP"
    TARGET="$ESP_IP"
else
    echo "⏳ Waiting 3s for mDNS resolution..."
    sleep 3
fi

echo "⚡ Flashing ESP32 over Wi-Fi (ArduinoOTA) to $TARGET..."
pio run -e esp32c3_ota -t upload --upload-port "$TARGET"

echo "✅ ESP32 deployed successfully!"
