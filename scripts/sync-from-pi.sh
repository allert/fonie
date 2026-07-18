#!/bin/bash
# Sync configuration and data files from the Raspberry Pi back to the local repository

set -e
TARGET="${1:-allert@fonie2.local}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "🔄 Syncing configuration files from Pi ($TARGET)..."

# Ensure target directories exist or copy directly
scp "${TARGET}:~/rfid-player/rfid_mappings.json" "$REPO_DIR/rfid_mappings.json" || echo "⚠️  Note: rfid_mappings.json was not found or failed to copy."
scp "${TARGET}:~/rfid-player/settings.json" "$REPO_DIR/settings.json" || echo "⚠️  Note: settings.json was not found or failed to copy."
scp "${TARGET}:~/rfid-player/.env" "$REPO_DIR/.env" || echo "⚠️  Note: .env was not found or failed to copy."

echo "✅ Sync complete!"
