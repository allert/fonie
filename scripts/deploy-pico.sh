#!/bin/bash
# Compile Pico firmware locally, copy binary and flash tool to Raspberry Pi, and flash the Pico over UART

set -e

# Add PlatformIO Core to PATH if it exists but is not in PATH
if ! command -v pio &> /dev/null; then
    if [ -x "$HOME/.platformio/penv/bin/pio" ]; then
        export PATH="$HOME/.platformio/penv/bin:$PATH"
    fi
fi

TARGET="${1:-allert@fonie.local}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Compiling Pico Firmware..."
cd "$SCRIPT_DIR/../firmware/pico"
pio run -e pico

FIRMWARE_BIN=".pio/build/pico/firmware.bin"
if [ ! -f "$FIRMWARE_BIN" ]; then
    echo "❌ error: firmware.bin not found!"
    exit 1
fi

echo "📤 Uploading firmware.bin and python flasher to Pi..."
scp "$FIRMWARE_BIN" "${TARGET}:~/rfid-player/firmware.bin"
scp "$SCRIPT_DIR/pico_uart_flash.py" "${TARGET}:~/rfid-player/"

echo "⚡ Flashing Pico over UART..."
ssh "$TARGET" 'cd ~/rfid-player && python3 pico_uart_flash.py firmware.bin'

echo "✅ Pico deployed and restarted!"
