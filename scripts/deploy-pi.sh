#!/bin/bash
set -e
PI_HOST="${1:-fonie.local}"

echo "🚀 Deploying Pi code to $PI_HOST..."
ssh $PI_HOST "cd ~/fonie && git pull && pip install -r requirements.txt --break-system-packages -q && sudo systemctl restart fonie"
echo "✅ Pi deployed and restarted"
