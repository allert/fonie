#!/bin/bash
set -e

echo "🎵 Fonie Setup Script"
echo "===================="

# Update system
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3-pip nginx openssl watchdog

# Install Python packages
pip3 install flask spotipy python-dotenv pyserial --break-system-packages

# Create directories
mkdir -p ~/rfid-player/templates

# Navigate to app directory
cd ~/rfid-player

# Create .env file
cat > .env << 'EOF'
SPOTIFY_CLIENT_ID=YOUR_CLIENT_ID
SPOTIFY_CLIENT_SECRET=YOUR_CLIENT_SECRET
SPOTIFY_REDIRECT_URI=https://fonie2.local:5000/callback
EOF

echo "⚠️  Edit .env with your Spotify credentials!"

# Create Flask app (paste the full app.py code here or copy from existing)
# [app.py content would go here]

# Create HTML template (paste the full index.html code here)
# [index.html content would go here]

# Setup nginx
sudo tee /etc/nginx/sites-available/fonie > /dev/null << 'EOF'
server {
    listen 5000 ssl;
    server_name fonie2.local;
    ssl_certificate /home/allert/rfid-player/cert.pem;
    ssl_certificate_key /home/allert/rfid-player/key.pem;
    location / {
        proxy_pass http://127.0.0.1:5001;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/fonie /etc/nginx/sites-enabled/
sudo systemctl enable nginx
sudo systemctl restart nginx

# Generate SSL cert
openssl req -x509 -newkey rsa:2048 -nodes -out cert.pem -keyout key.pem -days 365 -subj "/CN=fonie2.local"

# Setup systemd service
sudo tee /etc/systemd/system/fonie.service > /dev/null << 'EOF'
[Unit]
Description=Fonie RFID Spotify Player
After=network.target

[Service]
Type=simple
User=allert
WorkingDirectory=/home/allert/rfid-player
ExecStart=/usr/bin/python3 /home/allert/rfid-player/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable fonie

# Setup watchdog
sudo systemctl enable watchdog
sudo tee /etc/watchdog.conf > /dev/null << 'EOF'
watchdog-device = /dev/watchdog
watchdog-timeout = 10
EOF

# Update fstab for resilience
sudo sed -i 's/defaults,noatime/defaults,noatime,errors=remount-ro/g' /etc/fstab

# Create journal directory
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal

# Update systemd timeout
echo "DefaultTimeoutStopSec=10" | sudo tee -a /etc/systemd/system.conf

echo "✅ Setup complete!"
echo "1. Edit ~/rfid-player/.env with Spotify credentials"
echo "2. Copy app.py and templates/index.html"
echo "3. Run: sudo systemctl start fonie"
