# 🎵 Fonie - RFID Spotify Player

ESP32 + Raspberry Pi + PN532 NFC reader = automated Spotify playback

## Hardware
- **ESP32 WROOM** - RFID polling via I2C
- **PN532** - NFC/RFID reader (I2C mode)
- **Raspberry Pi 4** - Flask web UI + Spotify integration

## Quick Start

### Fresh Pi Setup
```bash
git clone https://github.com/YOUR_USERNAME/fonie.git
cd fonie
chmod +x setup.sh
./setup.sh
```

### Manual Setup
1. Raspberry Pi OS 64-bit
2. `pip3 install -r requirements.txt`
3. Edit `.env` with Spotify credentials
4. `systemctl start fonie`
5. Visit `https://fonie2.local:5000`

## Files
- `app.py` - Flask backend
- `setup.sh` - Automated setup script
- `templates/index.html` - Web UI
- `esp32/esp.ino` - ESP32 firmware

## Configuration
1. Get Spotify API credentials from [developer.spotify.com](https://developer.spotify.com)
2. Set in `.env`:
```
   SPOTIFY_CLIENT_ID=...
   SPOTIFY_CLIENT_SECRET=...
```

## ESP32 JSON Protocol
```json
{"event":"TAG_ON","uid":"4F02FB46F6180"}
{"event":"TAG_OFF","uid":"4F02FB46F6180"}
```

## License
MIT
