# Developer Setup Guide

This guide details how to set up a new development machine to work on the Fonie project, compile firmware, and deploy code to the Raspberry Pi, Pico, and ESP32-C3.

---

## 🛠️ Prerequisites

On your new development machine, ensure the following are installed:

1. **Python 3.9+** and pip.
2. **PlatformIO Core (CLI)**:
   - Used to compile and flash the microcontrollers.
   - Installation: `pip install platformio` or install the **PlatformIO IDE** extension in VS Code.
3. **SSH Client**:
   - Standard `ssh` and `scp` utilities must be available in your system path.

---

## 🔑 1. Setup SSH Authentication (Crucial)

To deploy or sync configurations, the deployment scripts rely on passwordless SSH/SCP authentication.

1. **Generate SSH Key** (if you don't already have one):
   ```bash
   ssh-keygen -t ed25519 -C "your_email@example.com"
   ```
2. **Copy the key to the Raspberry Pi**:
   ```bash
   ssh-copy-id allert@fonie2.local
   ```
   *Replace `allert@fonie2.local` with the Pi's actual hostname/IP if different.*
3. **Test connection**: Ensure you can SSH into the Pi without being prompted for a password:
   ```bash
   ssh allert@fonie2.local
   ```

---

## 🔄 2. Sync Configuration Files

The active tag database, player volume/brightness settings, and Spotify credentials live on the running Pi. To download them to your local environment for backup, development, and reference:

- **Linux / macOS**:
  ```bash
  ./scripts/sync-from-pi.sh
  ```
- **Windows (PowerShell)**:
  ```powershell
  .\scripts\sync-from-pi.ps1
  ```

This will copy the following files (which are ignored in git) to your local repository root:
- `rfid_mappings.json` (Tag-to-music bindings)
- `settings.json` (Active volume/brightness)
- `.env` (Spotify API Credentials)

If you are setting up a fully offline developer workspace, you can manually copy `settings.example.json` to `settings.json` and set mock credentials in a `.env` file:
```env
SPOTIFY_CLIENT_ID=mock_id
SPOTIFY_CLIENT_SECRET=mock_secret
SPOTIFY_REDIRECT_URI=https://fonie2.local:5000/callback
```

---

## 🚀 3. Flashing & Deployment Workflow

Scripts are provided in the [scripts/](file:///c:/Users/aller/Projects/fonie/fonie/scripts) directory to build and deploy everything.

### A. Deploying Python Web App (Pi)
Copies the Flask code and templates to the Pi and restarts the systemd service.
- **Linux / macOS**: `./scripts/deploy-pi.sh [allert@fonie2.local]`
- **Windows**: `.\scripts\deploy-pi.ps1 [-Target allert@fonie2.local]`

### B. Deploying RP2040 Pico Firmware
Compiles the PlatformIO project under `firmware/pico`, transfers the binary to the Pi, and runs the UART flasher script on the Pi.
- **Linux / macOS**: `./scripts/deploy-pico.sh [allert@fonie2.local]`
- **Windows**: `.\scripts\deploy-pico.ps1 [-Target allert@fonie2.local]`

### C. Deploying ESP32-C3 Firmware
Compiles the PlatformIO project under `firmware/esp32` and uploads the firmware over Wi-Fi (ArduinoOTA).
- **Linux / macOS**: `./scripts/deploy-esp32.sh [fonie-esp32.local]`
- **Windows**: `.\scripts\deploy-esp32.ps1 [-Target fonie-esp32.local]`

---

## 📋 4. Verify Local Compilations

To verify that your compiler environment is correctly set up, run:
```bash
cd firmware/pico
pio run
cd ../esp32
pio run
```
PlatformIO will automatically download the required toolchains (Earle Philhower RP2040 core, ESP32 framework) and library dependencies (`Adafruit NeoPixel`, `ArduinoJson`).
