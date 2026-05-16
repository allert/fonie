#!/bin/bash
set -e
PI_HOST="${1:-allert@fonie2.local}"

echo "🚀 Deploying Pi code to $PI_HOST..."
ssh $PI_HOST "cd ~/rfid-player && git pull && pip install -r requirements.txt --break-system-packages -q && sudo systemctl restart fonie"
echo "✅ Pi deployed and restarted"
