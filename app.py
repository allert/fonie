import os
import json
import serial
import threading
import sys
import subprocess
import socket
import time
import urllib.request
import io
from collections import deque
from flask import Flask, render_template, request, jsonify, make_response
from datetime import datetime
import secrets
from ytmusicapi import YTMusic
import yt_dlp

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# ── Config ────────────────────────────────────────────────────────────────────
ESP32_PORT    = '/dev/ttyAMA2'
PICO_PORT     = '/dev/ttyAMA5'
SERIAL_BAUD   = 115200
MEDIA_DIR     = os.path.expanduser('~/rfid-player/media')
MAPPINGS_FILE = os.path.expanduser('~/rfid-player/rfid_mappings.json')
SETTINGS_FILE = os.path.expanduser('~/rfid-player/settings.json')
SOUNDS_DIR    = os.path.expanduser('~/rfid-player/sounds')
AUDIO_DEVICE  = 'default'
MPV_SOCKET    = '/tmp/mpv.sock'

os.makedirs(MEDIA_DIR, exist_ok=True)

# ── Global state ──────────────────────────────────────────────────────────────
APP_VERSION     = '1.2.0'
pico_version    = None
esp32_version   = None
esp32_ip        = None
esp32_serial    = None
pico_serial     = None
pico_is_alive   = False
esp32_is_alive  = False
active_rfid_tag = None
current_tag     = {'present': False, 'uid': None, 'timestamp': None}
mpv_process     = None
download_queue  = {}
playback_state  = {'paused': False, 'volume': 80}
battery_state   = {'level': None, 'charging': False, 'voltage': None, 'current': 0.0}
button_state    = {
    'prev':   {'pressed': False, 'last_event': None, 'triggered': False},
    'play':   {'pressed': False, 'last_event': None, 'triggered': False},
    'next':   {'pressed': False, 'last_event': None, 'triggered': False},
    'vol_up': {'pressed': False, 'last_event': None, 'triggered': False},
    'vol_dn': {'pressed': False, 'last_event': None, 'triggered': False},
}
uart_log = deque(maxlen=100)  # ring buffer: last 100 UART messages
ytmusic = YTMusic()

def log_uart(direction, source, message):
    uart_log.append({
        'ts':  datetime.now().strftime('%H:%M:%S.%f')[:-3],
        'dir': direction,   # '←' or '→'
        'src': source,      # 'pico' or 'esp32'
        'msg': message,
    })

DEFAULT_EQ = {"60": 0, "250": 0, "1000": 0, "4000": 0, "12000": 0}
DEFAULT_ENHANCEMENTS = {
    "loudnorm": False,
    "dynaudnorm": False,
    "stereowiden": False,
    "asubboost": False
}

EQ_PRESETS = {
    "flat":       {"name": "Flat",       "eq": {"60": 0,  "250": 0,  "1000": 0,  "4000": 0,  "12000": 0}},
    "bass_boost": {"name": "Bass Boost", "eq": {"60": 8,  "250": 5,  "1000": 0,  "4000": 1,  "12000": 2}},
    "rock":       {"name": "Rock",       "eq": {"60": 4,  "250": 2,  "1000": -1, "4000": 3,  "12000": 5}},
    "jazz":       {"name": "Jazz",       "eq": {"60": 3,  "250": 1,  "1000": 2,  "4000": 2,  "12000": 3}},
    "vocal":      {"name": "Vocal",      "eq": {"60": -2, "250": 1,  "1000": 5,  "4000": 3,  "12000": -1}},
    "loudness":   {"name": "Loudness",   "eq": {"60": 6,  "250": 2,  "1000": 0,  "4000": 2,  "12000": 4}}
}

DEFAULT_SYSTEM_SOUNDS = {
    "startup": "startup.wav",
    "shutdown": "shutdown.wav",
    "captive_portal": "",
    "tag_mapped": "tag_mapped.wav",
    "tag_unknown": "tag_unmapped.wav"
}

# ── Settings ──────────────────────────────────────────────────────────────────
def load_settings():
    settings = {
        'brightness': {'ring': 60, 'matrix': 40},
        'volume': 80,
        'eq': dict(DEFAULT_EQ),
        'audio_enhancements': dict(DEFAULT_ENHANCEMENTS),
        'eq_preset': 'flat',
        'system_sounds': dict(DEFAULT_SYSTEM_SOUNDS)
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
                settings.update(saved)
                if 'eq' not in saved: settings['eq'] = dict(DEFAULT_EQ)
                if 'audio_enhancements' not in saved: settings['audio_enhancements'] = dict(DEFAULT_ENHANCEMENTS)
                if 'eq_preset' not in saved: settings['eq_preset'] = 'flat'
                if 'system_sounds' not in saved: settings['system_sounds'] = dict(DEFAULT_SYSTEM_SOUNDS)
                else:
                    for k, v in DEFAULT_SYSTEM_SOUNDS.items():
                        if k not in settings['system_sounds']:
                            settings['system_sounds'][k] = v
        except Exception as e:
            print(f"⚠️ Error loading settings: {e}")
    return settings

def save_settings(settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

def build_mpv_af_string(settings=None):
    if settings is None:
        settings = load_settings()
    eq = settings.get('eq', DEFAULT_EQ)
    enh = settings.get('audio_enhancements', DEFAULT_ENHANCEMENTS)
    filters = []

    # 5-band Equalizer using lavfi peaking equalizer filters
    bands = [
        ("60",    60,   1.0),
        ("250",   250,  1.0),
        ("1000",  1000, 1.0),
        ("4000",  4000, 1.0),
        ("12000", 12000,1.0)
    ]
    for key, freq, width in bands:
        gain = eq.get(key, 0)
        filters.append(f"equalizer=f={freq}:width_type=o:width={width}:g={gain}")

    if enh.get('asubboost'):
        filters.append("asubboost")
    if enh.get('stereowiden'):
        filters.append("stereowiden")
    if enh.get('dynaudnorm'):
        filters.append("dynaudnorm")
    if enh.get('loudnorm'):
        filters.append("speechnorm")

    return ",".join(filters)

def mpv_set_audio_filters(af_string=None):
    if af_string is None:
        af_string = build_mpv_af_string()
    return mpv_command({"command": ["set_property", "af", af_string]})

playback_state['volume'] = load_settings().get('volume', 80)

# ── Mappings ──────────────────────────────────────────────────────────────────
def load_mappings():
    if os.path.exists(MAPPINGS_FILE):
        with open(MAPPINGS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_mappings(mappings):
    with open(MAPPINGS_FILE, 'w') as f:
        json.dump(mappings, f, indent=2)

# ── Color extraction ──────────────────────────────────────────────────────────
def extract_dominant_color(thumbnail_url):
    if not thumbnail_url:
        return None
    try:
        from colorthief import ColorThief
        req  = urllib.request.Request(thumbnail_url, headers={'User-Agent': 'Mozilla/5.0'})
        data = urllib.request.urlopen(req, timeout=5).read()
        ct   = ColorThief(io.BytesIO(data))
        r, g, b = ct.get_color(quality=1)
        print(f"🎨 Dominant color: rgb({r},{g},{b})")
        return {'r': r, 'g': g, 'b': b}
    except Exception as e:
        print(f"⚠️  Color extraction failed: {e}")
        return None

# ── ESP32 / Pico communication ────────────────────────────────────────────────
def send_esp32(payload):
    global esp32_serial
    if not esp32_serial:
        return
    try:
        msg = json.dumps(payload)
        esp32_serial.write((msg + '\n').encode())
        print(f"→ ESP32: {msg}")
        log_uart('→', 'esp32', msg)
        sys.stdout.flush()
    except Exception as e:
        print(f"❌ ESP32 send error: {e}")

pico_lock = threading.Lock()

def send_pico(event, **kwargs):
    global pico_serial
    with pico_lock:
        if not pico_serial:
            pico_connect_internal()
        if not pico_serial:
            print(f"❌ Cannot send {event}: Pico serial port not connected")
            return
        payload = json.dumps({"event": event, **kwargs})
        try:
            pico_serial.write((payload + '\n').encode())
            pico_serial.flush()
            print(f"→ Pico: {payload}")
            log_uart('→', 'pico', payload)
            sys.stdout.flush()
        except Exception as e:
            print(f"❌ Pico send error: {e}")
            pico_serial = None

has_played_startup_sound = False

def pico_connect_internal():
    global pico_serial, pico_is_alive, pico_version
    try:
        pico_serial = serial.Serial(PICO_PORT, SERIAL_BAUD, timeout=1)
        pico_is_alive = True
        pico_version = '1.2.0'
        print(f"✅ Pico connected on {PICO_PORT}")
        payload = json.dumps({"event": "PING"})
        pico_serial.write((payload + '\n').encode())
        pico_serial.flush()
        log_uart('→', 'pico', payload)
        b = load_settings().get('brightness', {})
        if b:
            payload2 = json.dumps({"event": "BRIGHTNESS", "ring": b.get('ring', 60), "matrix": b.get('matrix', 40)})
            pico_serial.write((payload2 + '\n').encode())
            pico_serial.flush()
            log_uart('→', 'pico', payload2)
    except Exception as e:
        print(f"⚠️  Pico not connected: {e}")
        pico_serial = None
        pico_is_alive = False

def pico_connect():
    with pico_lock:
        pico_connect_internal()

def trigger_startup_sequence():
    global has_played_startup_sound
    if not has_played_startup_sound:
        has_played_startup_sound = True
        play_system_sound('startup', 'startup.wav')

def handle_pico_message(data):
    global battery_state, button_state, playback_state, pico_is_alive, pico_version, has_played_startup_sound
    event = data.get('event')

    if data.get('version'):
        pico_version = str(data.get('version'))
    elif not pico_version:
        pico_version = '1.2.0'

    if event:
        pico_is_alive = True

    if event == 'PONG':
        pass
    elif event == 'BOOTING':
        print("Pico reported BOOTING. Sending READY to handshake.")
        send_pico("READY")
        pico_vol = data.get('volume')
        if pico_vol is not None:
            try:
                pico_vol = max(0, min(100, int(pico_vol)))
                playback_state['volume'] = pico_vol
                set_system_volume(pico_vol)
                s = load_settings(); s['volume'] = pico_vol; save_settings(s)
                print(f"🔊 Synced volume from Pico BOOTING: {pico_vol}%")
            except (ValueError, TypeError): pass
        b = load_settings().get('brightness', {})
        if b:
            send_pico("BRIGHTNESS", ring=b.get('ring', 60), matrix=b.get('matrix', 40))
        trigger_startup_sequence()
    elif event == 'VOLUME':
        vol = data.get('level')
        if vol is not None:
            try:
                vol = max(0, min(100, int(vol)))
                playback_state['volume'] = vol
                set_system_volume(vol)
                s = load_settings(); s['volume'] = vol; save_settings(s)
                print(f"🔊 Synced volume from Pico VOLUME event: {vol}%")
            except (ValueError, TypeError): pass
        trigger_startup_sequence()
    elif event == 'PING':
        send_pico("PONG")
    elif event == 'SHUTDOWN':
        print("⚠️  Shutdown requested by Pico (long-press)")
        stop_playback(fast=True)
        time.sleep(0.1)
        play_system_sound('shutdown')
        time.sleep(4.5)  # Let full 4.27s shutdown.wav finish playing
        subprocess.run(['sudo', 'shutdown', '-h', 'now'])
    elif event == 'SOC':
        battery_state = {
            'level':    data.get('level'),
            'charging': data.get('charging', False),
            'voltage':  data.get('voltage'),
            'current':  data.get('current', 0.0),
        }
        print(f"🔋 Battery: {battery_state['level']}% {battery_state['voltage']}V {'⚡' if battery_state['charging'] else ''}")

    elif event == 'BUTTON':
        btn     = data.get('button')
        pressed = data.get('pressed', False)
        if btn in button_state:
            if button_state[btn]['pressed'] != pressed:
                button_state[btn]['pressed']    = pressed
                button_state[btn]['last_event'] = datetime.now().isoformat()
                if not pressed:
                    button_state[btn]['triggered'] = False
        print(f"🔘 Button {btn}: {'▼' if pressed else '▲'}")

    elif event == 'BUTTON_ACTION':
        action = data.get('action')
        print(f"🎮 Button action: {action}")
        if action == 'pause':
            mpv_set_pause(True);  playback_state['paused'] = True
        elif action == 'resume':
            mpv_set_pause(False); playback_state['paused'] = False
        elif action == 'next':   mpv_next()
        elif action == 'prev':   mpv_prev()
        elif action == 'volume':
            vol = data.get('level', 80)
            playback_state['volume'] = vol
            set_system_volume(vol)
            s = load_settings(); s['volume'] = vol; save_settings(s)

    sys.stdout.flush()

def pico_listener():
    global pico_serial
    while True:
        try:
            line = None
            with pico_lock:
                if not pico_serial:
                    pico_connect_internal()
                elif pico_serial.in_waiting:
                    line = pico_serial.readline().decode('utf-8', errors='ignore').strip()
            
            if line:
                print(f"← Pico: {line}")
                log_uart('←', 'pico', line)
                try:
                    handle_pico_message(json.loads(line))
                except json.JSONDecodeError:
                    pass
            else:
                time.sleep(0.05)
        except serial.SerialException:
            print("⚠️ Pico serial exception, reconnecting...")
            with pico_lock:
                pico_serial = None
            time.sleep(2)
        except Exception as e:
            time.sleep(0.1)

# ── Volume ────────────────────────────────────────────────────────────────────
def set_system_volume(vol):
    hw_vol = 55 + int(vol * 0.45)
    try:
        subprocess.run(
            ['amixer', '-D', 'hw:2', 'sset', 'A.Mstr Vol', f'{hw_vol}%'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        print(f"❌ Volume error: {e}")

# ── mpv IPC ───────────────────────────────────────────────────────────────────
def mpv_command(cmd):
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(MPV_SOCKET)
        sock.sendall((json.dumps(cmd) + '\n').encode())
        sock.close()
        return True
    except Exception as e:
        print(f"❌ mpv IPC error: {e}")
        return False

def mpv_set_pause(paused): return mpv_command({"command": ["set_property", "pause", paused]})
def mpv_next():             return mpv_command({"command": ["playlist-next"]})
def mpv_prev():             return mpv_command({"command": ["playlist-prev"]})

# ── Audio playback ────────────────────────────────────────────────────────────
def play_sound(filename):
    path = os.path.join(SOUNDS_DIR, filename)
    if not os.path.exists(path):
        return
    def _play():
        try:
            subprocess.run(['aplay', '-D', AUDIO_DEVICE, path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"❌ Sound error: {e}")
    threading.Thread(target=_play, daemon=True).start()

def play_system_sound(event_name, default_filename=None):
    settings = load_settings()
    sounds_map = settings.get('system_sounds', {})
    filename = sounds_map.get(event_name, default_filename)
    if filename:
        play_sound(filename)

# ── Tag Removal Grace Period & Resumption ──────────────────────────────────
grace_period_timer = None
last_removed_tag = None
last_removed_time = 0
GRACE_PERIOD_SECONDS = 5.0

def cancel_grace_timer():
    global grace_period_timer
    if grace_period_timer:
        try: grace_period_timer.cancel()
        except: pass
        grace_period_timer = None

def on_grace_period_expired(uid):
    global last_removed_tag, grace_period_timer
    print(f"⌛ Grace period expired for tag {uid}. Stopping playback.")
    stop_playback(fast=True)
    last_removed_tag = None
    grace_period_timer = None

def stop_playback(fast=False):
    global mpv_process, last_removed_tag
    cancel_grace_timer()
    last_removed_tag = None
    if mpv_process and mpv_process.poll() is None:
        if not fast:
            for vol in range(100, 0, -5):
                mpv_command({"command": ["set_property", "volume", vol]})
                time.sleep(0.05)
        mpv_process.terminate()
        try: mpv_process.wait(timeout=0.5)
        except: pass
        mpv_process = None
    if os.path.exists(MPV_SOCKET):
        try: os.remove(MPV_SOCKET)
        except: pass

import colorsys
import math
import struct

def extract_color_palette(image_path):
    try:
        if not image_path or not os.path.exists(image_path):
            return {'r': 0, 'g': 200, 'b': 200}, {'r': 0, 'g': 180, 'b': 255}
        img = Image.open(image_path).convert('RGB')
        img = img.resize((50, 50))
        colors = img.getcolors(maxcolors=2500)
        if not colors:
            return {'r': 0, 'g': 200, 'b': 200}, {'r': 0, 'g': 180, 'b': 255}
        
        valid_colors = []
        for count, (r, g, b) in colors:
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            if 20 < lum < 235:
                valid_colors.append((count, r, g, b))
        
        if not valid_colors:
            valid_colors = colors
            
        valid_colors.sort(reverse=True, key=lambda x: x[0])
        _, pr, pg, pb = valid_colors[0]

        h, s, v = colorsys.rgb_to_hsv(pr / 255.0, pg / 255.0, pb / 255.0)
        h2 = (h + 0.5) % 1.0
        sr, sg, sb = colorsys.hsv_to_rgb(h2, max(0.4, s), v)

        color1 = {'r': int(pr), 'g': int(pg), 'b': int(pb)}
        color2 = {'r': int(sr * 255), 'g': int(sg * 255), 'b': int(sb * 255)}
        return color1, color2
    except Exception as e:
        print(f"Palette extraction error: {e}")
        return {'r': 0, 'g': 200, 'b': 200}, {'r': 0, 'g': 180, 'b': 255}

def analyze_audio_file_vibe(audio_file_path):
    try:
        if not audio_file_path or not os.path.exists(audio_file_path):
            return "equalizer", 1.0
        
        cmd = [
            'ffmpeg', '-ss', '5', '-t', '5', '-i', audio_file_path,
            '-f', 's16le', '-ac', '1', '-ar', '11025', '-'
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        raw_data, _ = proc.communicate(timeout=3)
        
        if not raw_data or len(raw_data) < 22050:
            return "equalizer", 1.0

        num_samples = len(raw_data) // 2
        samples = struct.unpack(f"<{num_samples}h", raw_data)

        sum_sq = sum(s * s for s in samples)
        rms = math.sqrt(sum_sq / num_samples) / 32768.0

        win = 551
        num_windows = num_samples // win
        window_energies = []
        for i in range(num_windows):
            chunk = samples[i * win : (i + 1) * win]
            w_rms = math.sqrt(sum(s * s for s in chunk) / win) / 32768.0
            window_energies.append(w_rms)

        peaks = 0
        avg_e = sum(window_energies) / len(window_energies) if window_energies else 0.1
        for i in range(1, len(window_energies) - 1):
            if window_energies[i] > avg_e * 1.3 and window_energies[i] > window_energies[i-1] and window_energies[i] > window_energies[i+1]:
                peaks += 1

        bpm = (peaks / 5.0) * 60.0

        if bpm >= 120 or (bpm >= 100 and rms > 0.15):
            mode = "party"
            speed = min(1.5, max(1.1, bpm / 100.0))
        elif bpm <= 75 or rms < 0.05:
            mode = "wave"
            speed = max(0.4, min(0.8, bpm / 100.0))
        elif rms < 0.08:
            mode = "starfield"
            speed = 0.9
        else:
            mode = "equalizer"
            speed = min(1.3, max(0.8, bpm / 100.0))

        print(f"🎵 Pure Python Audio Analysis [{os.path.basename(audio_file_path)}]: RMS={rms:.3f}, BPM={bpm:.1f} -> Mode={mode}, Speed={speed:.2f}")
        return mode, round(speed, 2)
    except Exception as e:
        print(f"Audio analysis error: {e}")
        return "equalizer", 1.0

def send_playing_vibe(mapping):
    color1 = mapping.get('color') or {'r': 0, 'g': 200, 'b': 200}
    r1, g1, b1 = color1['r'], color1['g'], color1['b']
    
    color2 = mapping.get('color2')
    if color2:
        r2, g2, b2 = color2['r'], color2['g'], color2['b']
    else:
        r2, g2, b2 = g1, b1, r1

    media_path = mapping.get('media_path')
    mode = mapping.get('vibe_mode')
    speed = mapping.get('vibe_speed')

    if not mode or not speed:
        if media_path and os.path.exists(media_path):
            tracks = sorted([
                os.path.join(media_path, f) for f in os.listdir(media_path)
                if f.endswith(('.mp3', '.m4a', '.opus', '.webm'))
            ])
            if tracks:
                mode, speed = analyze_audio_file_vibe(tracks[0])

    if not mode: mode = "equalizer"
    if not speed: speed = 1.0

    send_pico("PLAYING", **{
        'r': int(r1), 'g': int(g1), 'b': int(b1),
        'r2': int(r2), 'g2': int(g2), 'b2': int(b2),
        'mode': str(mode), 'speed': float(speed)
    })

def play_mapping(mapping):
    global mpv_process, playback_state
    stop_playback(fast=True)
    time.sleep(0.1)
    media_path = mapping.get('media_path')
    if not media_path or not os.path.exists(media_path):
        print(f"❌ Media path not found: {media_path}"); return
    tracks = sorted([
        os.path.join(media_path, f) for f in os.listdir(media_path)
        if f.endswith(('.mp3', '.m4a', '.opus', '.webm'))
    ])
    if not tracks:
        print("❌ No tracks found"); return
    print(f"▶️  Playing {len(tracks)} track(s)")
    af_str = build_mpv_af_string()
    mpv_cmd = ['mpv', '--no-video', '--audio-format=s32', f'--input-ipc-server={MPV_SOCKET}']
    if af_str:
        mpv_cmd.append(f'--af={af_str}')
    mpv_process = subprocess.Popen(
        mpv_cmd + tracks,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    playback_state['paused'] = False
    send_playing_vibe(mapping)

# ── Download queue ────────────────────────────────────────────────────────────
def download_mapping(uid, mapping):
    media_path = os.path.join(MEDIA_DIR, uid)
    os.makedirs(media_path, exist_ok=True)
    download_queue[uid] = {'status': 'downloading', 'progress': 0, 'error': None}
    ytmusic_id = mapping.get('ytmusic_id')
    mtype      = mapping.get('type', 'track')
    if mtype == 'track':
        urls = [f'https://music.youtube.com/watch?v={ytmusic_id}']
    else:
        try:
            if mtype == 'album':
                info = ytmusic.get_album(ytmusic_id)
                urls = [f'https://music.youtube.com/watch?v={t["videoId"]}'
                        for t in info['tracks'] if t.get('videoId')]
            else:
                info = ytmusic.get_playlist(ytmusic_id, limit=100)
                urls = [f'https://music.youtube.com/watch?v={t["videoId"]}'
                        for t in info['tracks'] if t.get('videoId')]
        except Exception as e:
            download_queue[uid] = {'status': 'error', 'progress': 0, 'error': str(e)}; return
    total  = len(urls)
    done   = 0
    errors = []
    ydl_opts = {
        'format':          'bestaudio/best',
        'outtmpl':         os.path.join(media_path, '%(autonumber)02d-%(title)s.%(ext)s'),
        'quiet':           True, 'no_warnings': True,
        'ffmpeg_location': '/usr/bin',
        'postprocessors':  [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'keepvideo':       False,
        'extractor_args':  {'youtube': {'player_client': ['android', 'web']}},
    }
    def make_progress_hook(uid_key, current_idx, total_tracks):
        def hook(d):
            if d.get('status') == 'downloading':
                total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes') or 0
                if total_bytes > 0:
                    track_pct = downloaded / total_bytes
                    overall_pct = int(((current_idx + track_pct) / total_tracks) * 90.0)
                    download_queue[uid_key]['progress'] = min(95, max(1, overall_pct))
                    mappings = load_mappings()
                    if uid_key in mappings:
                        mappings[uid_key]['progress'] = download_queue[uid_key]['progress']
                        save_mappings(mappings)
        return hook

    for idx, url in enumerate(urls):
        try:
            current_ydl_opts = dict(ydl_opts)
            current_ydl_opts['progress_hooks'] = [make_progress_hook(uid, idx, total)]
            with yt_dlp.YoutubeDL(current_ydl_opts) as ydl: ydl.download([url])
            done += 1
        except Exception as e:
            errors.append(str(e))
    if errors and done == 0:
        download_queue[uid] = {'status': 'error', 'progress': 0, 'error': errors[0]}; return
    
    # Download thumbnail image locally for palette analysis
    download_queue[uid]['progress'] = 96
    thumb_path = os.path.join(media_path, 'thumbnail.jpg')
    if mapping.get('thumbnail'):
        try:
            import urllib.request
            urllib.request.urlretrieve(mapping.get('thumbnail'), thumb_path)
        except: pass

    color1, color2 = extract_color_palette(thumb_path if os.path.exists(thumb_path) else None)

    tracks = sorted([
        os.path.join(media_path, f) for f in os.listdir(media_path)
        if f.endswith(('.mp3', '.m4a', '.opus', '.webm'))
    ])
    download_queue[uid]['progress'] = 98
    vibe_mode, vibe_speed = analyze_audio_file_vibe(tracks[0]) if tracks else ("equalizer", 1.0)

    download_queue[uid] = {'status': 'ready', 'progress': 100, 'error': None}
    mappings = load_mappings()
    if uid in mappings:
        mappings[uid]['status']     = 'ready'
        mappings[uid]['media_path'] = media_path
        mappings[uid]['color']      = color1
        mappings[uid]['color2']     = color2
        mappings[uid]['vibe_mode']  = vibe_mode
        mappings[uid]['vibe_speed'] = vibe_speed
        save_mappings(mappings)
    print(f"✅ Download & Audio Vibe Analysis complete for {uid} [Mode: {vibe_mode}, Speed: {vibe_speed}]")

def start_download(uid, mapping):
    threading.Thread(target=download_mapping, args=(uid, mapping), daemon=True).start()

# ── ESP32 serial listener ─────────────────────────────────────────────────────
def serial_listener():
    global esp32_serial, current_tag, esp32_is_alive
    while True:
        try:
            if not esp32_serial:
                esp32_serial = serial.Serial(ESP32_PORT, SERIAL_BAUD, timeout=1)
                esp32_is_alive = True
                print(f"✅ ESP32 connected on {ESP32_PORT}")
                try: esp32_serial.write(b'{"event":"PING"}\n')
                except: pass
            if esp32_serial.in_waiting:
                line = esp32_serial.readline().decode('utf-8').strip()
                print(f"📨 ESP32: {line}")
                if line:
                    log_uart('←', 'esp32', line)
                    try: handle_esp32_event(json.loads(line))
                    except json.JSONDecodeError: pass
        except serial.SerialException as e:
            print(f"❌ ESP32 serial error: {e}"); esp32_serial = None; esp32_is_alive = False; threading.Event().wait(5)
        except Exception as e:
            print(f"❌ Error: {e}"); threading.Event().wait(1)

def handle_esp32_event(event):
    global active_rfid_tag, current_tag, esp32_is_alive, esp32_version, esp32_ip
    global last_removed_tag, last_removed_time, grace_period_timer
    event_type = event.get('event')
    uid        = event.get('uid')
    if event.get('version'):
        esp32_version = event.get('version')
    if event_type == 'WIFI_CONNECTED':
        esp32_ip = event.get('ip')
        print(f"📡 ESP32 reported Wi-Fi connected! IP: {esp32_ip}")
    elif event_type == 'PONG':
        esp32_is_alive = True
    elif event_type == 'TAG_ON':
        print(f"📱 TAG ON: {uid}")
        mappings  = load_mappings()
        is_mapped = uid in mappings and mappings[uid].get('status') == 'ready'
        current_tag = {
            'present': True, 'uid': uid,
            'timestamp': datetime.now().isoformat(),
            'mapped':  is_mapped,
            'title':   mappings[uid]['title']  if is_mapped else None,
            'artist':  mappings[uid]['artist'] if is_mapped else None,
            'color':   mappings[uid].get('color') if is_mapped else None,
        }
        char_name = mappings[uid].get('character_name', '') if is_mapped else ''
        
        now = time.time()
        is_resume = (last_removed_tag == uid and (now - last_removed_time) <= (GRACE_PERIOD_SECONDS + 1.0) and mpv_process and mpv_process.poll() is None)
        
        cancel_grace_timer()
        last_removed_tag = None

        send_pico("TAG_ON", mapped=is_mapped, name=char_name)
        if is_mapped:
            play_system_sound('tag_mapped', 'tag_mapped_32.wav')
            if is_resume:
                print(f"⏯️ Resuming playback for tag {uid} where it left off!")
                mpv_set_pause(False)
                send_playing_vibe(mappings[uid])
            else:
                play_mapping(mappings[uid])
        else:
            play_system_sound('tag_unknown', 'tag_unknown_32.wav')
            send_pico("TAG_UNKNOWN")
        active_rfid_tag = uid
    elif event_type == 'TAG_OFF':
        print(f"📱 TAG OFF: {uid}")
        current_tag = {'present': False, 'uid': None, 'timestamp': datetime.now().isoformat()}
        send_pico("TAG_OFF", uid=uid)
        
        cancel_grace_timer()
        
        if mpv_process and mpv_process.poll() is None:
            print(f"⏳ Tag {uid} removed. Pausing & starting {GRACE_PERIOD_SECONDS}s grace timer...")
            mpv_set_pause(True)
            last_removed_tag = uid
            last_removed_time = time.time()
            grace_period_timer = threading.Timer(GRACE_PERIOD_SECONDS, on_grace_period_expired, args=[uid])
            grace_period_timer.daemon = True
            grace_period_timer.start()
        else:
            stop_playback()
        active_rfid_tag = None
    elif event_type == 'WIFI_CONFIG':
        ssid = event.get('ssid', '')
        password = event.get('pass', '')
        print(f"📡 Received Wi-Fi credentials for: {ssid}")
        try:
            res = subprocess.run(['nmcli', 'dev', 'wifi', 'connect', ssid, 'password', password], capture_output=True, text=True)
            if res.returncode == 0:
                print("✅ Wi-Fi connected successfully!")
                settings = load_settings()
                settings['wifi_ssid'] = ssid
                settings['wifi_pass'] = password
                save_settings(settings)
                global wifi_state
                wifi_state['sta_started'] = False # Force reconnect notification to ESP32
            else:
                print(f"❌ Wi-Fi connect failed: {res.stderr}")
        except Exception as e:
            print(f"❌ nmcli error: {e}")
    elif event_type == 'READY':
        print("✅ ESP32 ready!")

# ── Wi-Fi Monitor ─────────────────────────────────────────────────────────────
wifi_state = {'connected': False, 'ap_started': False, 'sta_started': False}

def check_wifi_connection():
    try:
        res = subprocess.run(['nmcli', '-t', '-f', 'STATE', 'general'], capture_output=True, text=True)
        return 'connected' in res.stdout
    except:
        return False

def get_pi_wifi_credentials():
    try:
        res = subprocess.run(['nmcli', '-t', '-f', 'active,ssid', 'dev', 'wifi'], capture_output=True, text=True)
        ssid = None
        for line in res.stdout.splitlines():
            if line.startswith('yes:'):
                ssid = line.split(':', 1)[1]
                break
        if not ssid:
            return None, None
        
        res_conn = subprocess.run(['nmcli', '-t', '-f', 'active,NAME', 'connection', 'show'], capture_output=True, text=True)
        conn_name = None
        for line in res_conn.stdout.splitlines():
            if line.startswith('yes:'):
                conn_name = line.split(':', 1)[1]
                break
        if not conn_name:
            conn_name = 'preconfigured'

        res_psk = subprocess.run(['sudo', 'nmcli', '-s', '-g', '802-11-wireless-security.psk', 'connection', 'show', conn_name], capture_output=True, text=True)
        password = res_psk.stdout.strip()
        if not password:
            res_psk2 = subprocess.run(['sudo', 'nmcli', '-s', '-g', '802-11-wireless-security.psk', 'connection', 'show', 'preconfigured'], capture_output=True, text=True)
            password = res_psk2.stdout.strip()
        return ssid, password
    except Exception as e:
        print(f"Error extracting Wi-Fi credentials: {e}")
        return None, None

ap_override_until = 0

def force_ap_mode():
    global wifi_state, ap_override_until
    ap_override_until = time.time() + 60 # Keep AP open for 1 minute (60s) override window
    send_esp32({"event": "WIFI_AP_START"})
    send_pico("WIFI_AP")
    wifi_state['ap_started'] = True
    wifi_state['sta_started'] = False

def wifi_sync_thread():
    global wifi_state, ap_override_until
    while True:
        if time.time() < ap_override_until:
            time.sleep(5)
            continue
        if check_wifi_connection():
            if not wifi_state['sta_started']:
                settings = load_settings()
                ssid = settings.get('wifi_ssid', '')
                password = settings.get('wifi_pass', '')
                if not ssid:
                    ssid, password = get_pi_wifi_credentials()
                if ssid:
                    send_esp32({"event": "WIFI_CONNECT", "ssid": ssid, "pass": password})
                    wifi_state['sta_started'] = True
                    wifi_state['ap_started']  = False
        else:
            if not wifi_state['ap_started']:
                send_esp32({"event": "WIFI_AP_START"})
                send_pico("WIFI_AP")
                wifi_state['ap_started']  = True
                wifi_state['sta_started'] = False
        time.sleep(10)

def button_monitor_thread():
    while True:
        now = datetime.now()
        
        # Check Play for 5s (Shutdown)
        if button_state['play']['pressed'] and button_state['play']['last_event'] and not button_state['play']['triggered']:
            if not button_state['prev']['pressed']: # Ensure prev is not also held
                start_time = datetime.fromisoformat(button_state['play']['last_event'])
                if (now - start_time).total_seconds() >= 5.0:
                    print("⚠️  Shutdown triggered (Play held for 5s)")
                    button_state['play']['triggered'] = True
                    play_system_sound('shutdown')
                    send_pico("EVENT", name="shutdown") # Tell Pico to sleep/power off LEDs
                    subprocess.run(['sudo', 'shutdown', '-h', 'now'])
                    
        # Check Play + Prev for 5s (Captive Portal)
        if button_state['play']['pressed'] and button_state['prev']['pressed'] and \
           button_state['play']['last_event'] and button_state['prev']['last_event'] and \
           not button_state['play']['triggered'] and not button_state['prev']['triggered']:
            play_start = datetime.fromisoformat(button_state['play']['last_event'])
            prev_start = datetime.fromisoformat(button_state['prev']['last_event'])
            # Use the latest press time of the two
            start_time = max(play_start, prev_start)
            if (now - start_time).total_seconds() >= 5.0:
                print("⚠️  Captive Portal triggered (Play + Prev held for 5s)")
                button_state['play']['triggered'] = True
                button_state['prev']['triggered'] = True
                play_system_sound('captive_portal', 'tag_mapped.wav')
                force_ap_mode()
                
        time.sleep(0.1)

# ── Web routes ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    resp = make_response(render_template('index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/api/search')
def search():
    query = request.args.get('q', '').strip()
    mtype = request.args.get('type', 'all')
    if not query or len(query) < 2: return jsonify([])
    try:
        yt = YTMusic()
        results = []
        if mtype in ('all', 'track'):
            for t in yt.search(query, filter='songs', limit=5):
                results.append({'type': 'track', 'id': t.get('videoId'),
                    'title': t.get('title', 'Unknown'),
                    'artist': ', '.join([a['name'] for a in t.get('artists', [])]),
                    'album': t.get('album', {}).get('name', '') if t.get('album') else '',
                    'duration': t.get('duration', ''),
                    'thumbnail': t.get('thumbnails', [{}])[-1].get('url', ''),})
        if mtype in ('all', 'album'):
            for a in yt.search(query, filter='albums', limit=5):
                results.append({'type': 'album', 'id': a.get('browseId'),
                    'title': a.get('title', 'Unknown'),
                    'artist': ', '.join([ar['name'] for ar in a.get('artists', [])]),
                    'year': a.get('year', ''),
                    'thumbnail': a.get('thumbnails', [{}])[-1].get('url', ''),})
        if mtype in ('all', 'playlist'):
            for p in yt.search(query, filter='playlists', limit=5):
                results.append({'type': 'playlist', 'id': p.get('browseId'),
                    'title': p.get('title', 'Unknown'),
                    'artist': p.get('author', ''), 'count': p.get('itemCount', ''),
                    'thumbnail': p.get('thumbnails', [{}])[-1].get('url', ''),})
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/mappings')
def api_mappings():
    mappings = load_mappings()
    for uid, m in mappings.items():
        if uid in download_queue:
            m['status']   = download_queue[uid]['status']
            m['progress'] = download_queue[uid]['progress']
            m['error']    = download_queue[uid]['error']
        media_path = m.get('media_path')
        if media_path and os.path.exists(media_path):
            m['tracks'] = sorted([
                f for f in os.listdir(media_path)
                if f.endswith(('.mp3', '.m4a', '.opus', '.webm'))
            ])
        else:
            m['tracks'] = []
    return jsonify(mappings)

@app.route('/api/mappings/add', methods=['POST'])
def add_mapping():
    data = request.json
    uid  = data.get('uid', '').strip()
    if not uid: return jsonify({'error': 'UID required'}), 400
    mapping = {'uid': uid, 'type': data.get('type'), 'title': data.get('title'),
               'artist': data.get('artist'), 'character_name': data.get('character_name', '').strip(),
               'ytmusic_id': data.get('id'),
               'thumbnail': data.get('thumbnail'), 'status': 'pending',
               'media_path': None, 'color': None, 'added': datetime.now().isoformat()}
    mappings = load_mappings()
    mappings[uid] = mapping
    save_mappings(mappings)
    start_download(uid, mapping)
    return jsonify({'success': True})

@app.route('/api/mappings/delete/<uid>', methods=['POST'])
def delete_mapping(uid):
    mappings = load_mappings()
    if uid in mappings:
        media_path = mappings[uid].get('media_path')
        if media_path and os.path.exists(media_path):
            import shutil; shutil.rmtree(media_path, ignore_errors=True)
        del mappings[uid]; save_mappings(mappings)
        return jsonify({'success': True})
@app.route('/api/mappings/update/<uid>', methods=['POST'])
def update_mapping(uid):
    mappings = load_mappings()
    if uid not in mappings:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'character_name' in data:
        mappings[uid]['character_name'] = data['character_name'].strip()
    if 'title' in data:
        mappings[uid]['title'] = data['title'].strip()
    if 'artist' in data:
        mappings[uid]['artist'] = data['artist'].strip()
    save_mappings(mappings)
    return jsonify({'success': True, 'mapping': mappings[uid]})

@app.route('/api/mappings/retry/<uid>', methods=['POST'])
def retry_mapping(uid):
    mappings = load_mappings()
    if uid not in mappings: return jsonify({'error': 'Not found'}), 404
    mappings[uid]['status'] = 'pending'; save_mappings(mappings)
    start_download(uid, mappings[uid])
    return jsonify({'success': True})

@app.route('/api/mappings/play/<uid>', methods=['POST'])
def play_mapped_song(uid):
    mappings = load_mappings()
    if uid not in mappings:
        return jsonify({'error': 'Mapping not found'}), 404
    mapping = mappings[uid]
    if mapping.get('status') != 'ready':
        return jsonify({'error': 'Media not ready'}), 400
    
    global current_tag
    current_tag = {
        'present': True,
        'uid': uid,
        'timestamp': datetime.now().isoformat(),
        'mapped': True,
        'title': mapping.get('title'),
        'artist': mapping.get('artist'),
        'color': mapping.get('color'),
        'virtual': True
    }
    
    send_pico("TAG_ON", mapped=True)
    play_system_sound('tag_mapped', 'tag_mapped_32.wav')
    play_mapping(mapping)
    return jsonify({'success': True})

@app.route('/api/current-tag')
def api_current_tag():
    return jsonify(current_tag)

@app.route('/api/battery')
def api_battery():
    return jsonify(battery_state)

@app.route('/api/uart-log')
def api_uart_log():
    since = request.args.get('since', 0, type=int)
    entries = list(uart_log)
    return jsonify(entries[since:])

@app.route('/api/brightness')
def api_brightness_get():
    return jsonify(load_settings().get('brightness', {}))

@app.route('/api/brightness', methods=['POST'])
def api_brightness_set():
    data     = request.json
    settings = load_settings()
    settings['brightness'].update({
        k: max(0, min(255, int(v)))
        for k, v in data.items()
        if k in ('ring', 'matrix')
    })
    save_settings(settings)
    b = settings['brightness']
    send_pico("BRIGHTNESS", ring=b['ring'], matrix=b['matrix'])
    return jsonify({'success': True, 'brightness': b})

@app.route('/api/esp32/connect_wifi', methods=['POST'])
def api_esp32_connect_wifi():
    global esp32_ip
    esp32_ip = None
    data = request.json or {}
    settings = load_settings()
    ssid = data.get('ssid') or settings.get('wifi_ssid', '')
    password = data.get('pass') or settings.get('wifi_pass', '')
    if data.get('ssid'):
        settings['wifi_ssid'] = data['ssid']
        if data.get('pass'):
            settings['wifi_pass'] = data['pass']
        save_settings(settings)

    if not ssid or not password:
        ssid_pi, pass_pi = get_pi_wifi_credentials()
        ssid = ssid or ssid_pi
        password = password or pass_pi
    if ssid and password:
        print(f"📡 Powering up ESP32 Wi-Fi PHY and connecting to: {ssid}", flush=True)
        send_esp32({"event": "WIFI_AP_START"})
        time.sleep(0.5)
        send_esp32({"event": "WIFI_CONNECT", "ssid": ssid, "pass": password})
        for _ in range(20): # Wait up to 10s for ESP32 to report IP
            time.sleep(0.5)
            if esp32_ip:
                print(f"✅ ESP32 connected to Wi-Fi! IP: {esp32_ip}", flush=True)
                return jsonify({'status': 'ok', 'ssid': ssid, 'ip': esp32_ip})
        return jsonify({'status': 'ok', 'ssid': ssid, 'ip': None})
    return jsonify({'status': 'error', 'message': 'No active Wi-Fi connection found on Pi'}), 400

@app.route('/api/sound/settings', methods=['GET', 'POST'])
def api_sound_settings():
    settings = load_settings()
    if request.method == 'POST':
        data = request.json or {}
        if 'eq' in data and isinstance(data['eq'], dict):
            for k, v in data['eq'].items():
                try: settings['eq'][str(k)] = max(-12, min(12, int(v)))
                except (ValueError, TypeError): pass
            settings['eq_preset'] = 'custom'
        if 'enhancements' in data and isinstance(data['enhancements'], dict):
            for k, v in data['enhancements'].items():
                settings['audio_enhancements'][str(k)] = bool(v)
        save_settings(settings)
        af_str = build_mpv_af_string(settings)
        mpv_set_audio_filters(af_str)
        return jsonify({
            'status': 'ok',
            'eq': settings['eq'],
            'audio_enhancements': settings['audio_enhancements'],
            'eq_preset': settings.get('eq_preset', 'custom'),
            'af_string': af_str
        })
    
    return jsonify({
        'eq': settings.get('eq', DEFAULT_EQ),
        'audio_enhancements': settings.get('audio_enhancements', DEFAULT_ENHANCEMENTS),
        'eq_preset': settings.get('eq_preset', 'flat'),
        'presets': {k: v['name'] for k, v in EQ_PRESETS.items()}
    })

@app.route('/api/sound/preset', methods=['POST'])
def api_sound_preset():
    data = request.json or {}
    preset_key = str(data.get('preset', 'flat'))
    if preset_key not in EQ_PRESETS:
        return jsonify({'status': 'error', 'message': f'Unknown preset: {preset_key}'}), 400
    
    settings = load_settings()
    settings['eq'] = dict(EQ_PRESETS[preset_key]['eq'])
    settings['eq_preset'] = preset_key
    save_settings(settings)
    
    af_str = build_mpv_af_string(settings)
    mpv_set_audio_filters(af_str)
    return jsonify({
        'status': 'ok',
        'preset': preset_key,
        'eq': settings['eq'],
        'audio_enhancements': settings.get('audio_enhancements', DEFAULT_ENHANCEMENTS),
        'af_string': af_str
    })

@app.route('/api/debug')
def api_debug():
    global pico_version, esp32_version
    if not pico_version:
        send_pico("PING")
    if not esp32_version and esp32_serial:
        try: esp32_serial.write(b'{"event":"PING"}\n')
        except: pass
    settings = load_settings()
    return jsonify({
        'app_version':     APP_VERSION,
        'pico_version':    pico_version,
        'esp32_version':   esp32_version,
        'buttons':         button_state,
        'battery':         battery_state,
        'playback':        playback_state,
        'tag':             current_tag,
        'brightness':      settings.get('brightness', {}),
        'pico_connected':  pico_is_alive,
        'esp32_connected': esp32_is_alive,
        'esp32_wifi': {
            'ip':          esp32_ip,
            'ssid':        settings.get('wifi_ssid', ''),
            'connected':   (esp32_ip is not None)
        }
    })

@app.route('/api/debug/led_test', methods=['POST'])
def api_debug_led_test():
    data   = request.json or {}
    target = str(data.get('target', 'off'))
    send_pico("LED_TEST", target=target)
    return jsonify({"status": "ok", "target": target})

@app.route('/api/ping', methods=['POST'])
def api_ping():
    global pico_is_alive, esp32_is_alive
    pico_is_alive = False
    esp32_is_alive = False
    send_pico("PING")
    if esp32_serial:
        try:
            esp32_serial.write(b'{"event":"PING"}\n')
        except:
            pass
    time.sleep(0.5)
    return jsonify({'success': True})

@app.route('/api/test_ap', methods=['POST'])
def api_test_ap():
    force_ap_mode()
    return jsonify({'success': True})

@app.route('/api/test_leds', methods=['POST'])
def api_test_leds():
    send_pico("PLAYING", r=255, g=0, b=128)
    return jsonify({'success': True})

@app.route('/api/playback/status')
def playback_status():
    is_running = mpv_process is not None and mpv_process.poll() is None
    return jsonify({'playing': is_running and not playback_state['paused'],
                    'paused':  is_running and playback_state['paused'],
                    'stopped': not is_running, 'volume': playback_state['volume']})

@app.route('/api/playback/pause', methods=['POST'])
def playback_pause():
    if mpv_set_pause(True):
        playback_state['paused'] = True; send_pico("PAUSED")
        return jsonify({'success': True})
    return jsonify({'error': 'mpv not running'}), 400

@app.route('/api/playback/resume', methods=['POST'])
def playback_resume():
    if mpv_set_pause(False):
        playback_state['paused'] = False
        color = current_tag.get('color')
        if color: send_pico("PLAYING", r=color['r'], g=color['g'], b=color['b'])
        else:     send_pico("PLAYING")
        return jsonify({'success': True})
    return jsonify({'error': 'mpv not running'}), 400

@app.route('/api/playback/next',   methods=['POST'])
def playback_next():   mpv_next(); return jsonify({'success': True})

@app.route('/api/playback/prev',   methods=['POST'])
def playback_prev():   mpv_prev(); return jsonify({'success': True})

@app.route('/api/playback/stop',   methods=['POST'])
def playback_stop():
    stop_playback()
    global current_tag
    current_tag = {'present': False, 'uid': None, 'timestamp': datetime.now().isoformat()}
    send_pico("TAG_OFF", uid="")
    return jsonify({'success': True})

@app.route('/api/playback/volume', methods=['POST'])
def playback_volume():
    vol = max(0, min(100, int(request.json.get('volume', 80))))
    playback_state['volume'] = vol; set_system_volume(vol); send_pico("VOLUME", level=vol)
    s = load_settings(); s['volume'] = vol; save_settings(s)
    return jsonify({'success': True, 'volume': vol})

@app.route('/api/pico/event', methods=['POST'])
def api_pico_event():
    data = request.json
    if not data or 'event' not in data: return jsonify({'error': 'event required'}), 400
    event = data.pop('event'); send_pico(event, **data)
    return jsonify({'success': True})

@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    return jsonify(load_settings())

@app.route('/api/settings', methods=['POST'])
def api_settings_post():
    data = request.json
    settings = load_settings()
    for k, v in data.items():
        settings[k] = v
    save_settings(settings)
    return jsonify({'success': True, 'settings': settings})

@app.route('/api/media/sounds', methods=['GET'])
def api_media_sounds():
    if not os.path.exists(SOUNDS_DIR): return jsonify([])
    sounds = [f for f in os.listdir(SOUNDS_DIR) if f.endswith(('.wav', '.mp3'))]
    return jsonify(sounds)

@app.route('/api/media/sounds/upload', methods=['POST'])
def api_media_sounds_upload():
    if 'file' not in request.files: return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({'error': 'No selected file'}), 400
    if file and file.filename.endswith(('.wav', '.mp3')):
        file.save(os.path.join(SOUNDS_DIR, file.filename))
        return jsonify({'success': True})
    return jsonify({'error': 'Invalid file type'}), 400

@app.route('/api/media/sounds/<filename>', methods=['DELETE'])
def api_media_sounds_delete(filename):
    path = os.path.join(SOUNDS_DIR, filename)
    if os.path.exists(path):
        os.remove(path)
        return jsonify({'success': True})
    return jsonify({'error': 'File not found'}), 404

@app.route('/api/media/music', methods=['GET'])
def api_media_music():
    if not os.path.exists(MEDIA_DIR): return jsonify([])
    music = []
    mappings = load_mappings()
    for d in os.listdir(MEDIA_DIR):
        dp = os.path.join(MEDIA_DIR, d)
        if os.path.isdir(dp):
            tracks = [f for f in os.listdir(dp) if f.endswith(('.mp3', '.m4a', '.opus', '.webm'))]
            if tracks:
                info = {'uid': d, 'tracks': tracks, 'title': d}
                if d in mappings:
                    info['title'] = mappings[d].get('title', d)
                    info['artist'] = mappings[d].get('artist', '')
                music.append(info)
    return jsonify(music)

def play_media_path(path):
    global mpv_process, playback_state
    stop_playback(fast=True)
    time.sleep(0.1)

    full_path = os.path.join(MEDIA_DIR, path)
    if not os.path.exists(full_path):
        print(f"❌ Media path not found: {full_path}")
        return False

    if os.path.isfile(full_path):
        folder = os.path.dirname(full_path)
        all_tracks = sorted([
            os.path.join(folder, f) for f in os.listdir(folder)
            if f.endswith(('.mp3', '.m4a', '.opus', '.webm'))
        ])
        if full_path in all_tracks:
            idx = all_tracks.index(full_path)
            tracks = all_tracks[idx:] + all_tracks[:idx]
        else:
            tracks = [full_path]
    elif os.path.isdir(full_path):
        tracks = sorted([
            os.path.join(full_path, f) for f in os.listdir(full_path)
            if f.endswith(('.mp3', '.m4a', '.opus', '.webm'))
        ])
    else:
        tracks = []

    if not tracks:
        print("❌ No tracks found to play")
        return False

    print(f"▶️ Web UI playing {len(tracks)} track(s) starting with {os.path.basename(tracks[0])}")
    af_str = build_mpv_af_string()
    mpv_cmd = ['mpv', '--no-video', '--audio-format=s32', f'--input-ipc-server={MPV_SOCKET}']
    if af_str:
        mpv_cmd.append(f'--af={af_str}')
    
    mpv_process = subprocess.Popen(
        mpv_cmd + tracks,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    playback_state['paused'] = False
    mappings = load_mappings()
    uid = path.split('/')[0] if '/' in path else path
    mapping = mappings.get(uid, {'title': os.path.basename(path)})
    send_playing_vibe(mapping)
    return True

@app.route('/api/media/play', methods=['POST'])
def api_media_play():
    data = request.json
    type = data.get('type')
    path = data.get('path')
    if type == 'sound':
        play_sound(path)
        return jsonify({'success': True})
    elif type == 'music':
        if play_media_path(path):
            return jsonify({'success': True})
    return jsonify({'error': 'Invalid request'}), 400

# ── Continuous Audio Keep-Alive ────────────────────────────────────────────────
def start_audio_keepalive():
    def _keepalive():
        while True:
            try:
                # Stream digital silence into ALSA dmix to keep I2S clocks active continuously
                proc = subprocess.Popen(
                    ['aplay', '-D', 'default', '-t', 'raw', '-r', '48000', '-c', '2', '-f', 'S32_LE', '/dev/zero'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                proc.wait()
            except Exception as e:
                time.sleep(1)
    threading.Thread(target=_keepalive, daemon=True).start()

def scan_and_analyze_existing_media():
    try:
        mappings = load_mappings()
        updated = False
        for uid, m in mappings.items():
            if m.get('status') == 'ready' and m.get('media_path'):
                media_path = m.get('media_path')
                if os.path.exists(media_path):
                    tracks = sorted([
                        os.path.join(media_path, f) for f in os.listdir(media_path)
                        if f.endswith(('.mp3', '.m4a', '.opus', '.webm'))
                    ])
                    if tracks and ('vibe_mode' not in m or 'color2' not in m):
                        mode, speed = analyze_audio_file_vibe(tracks[0])
                        thumb_file = os.path.join(media_path, 'thumbnail.jpg')
                        color1, color2 = extract_color_palette(thumb_file if os.path.exists(thumb_file) else None)
                        
                        m['vibe_mode'] = mode
                        m['vibe_speed'] = speed
                        if not m.get('color'): m['color'] = color1
                        m['color2'] = color2
                        updated = True
        if updated:
            save_mappings(mappings)
            print("✅ Pre-existing media song-agnostic audio & color analysis completed!")
    except Exception as e:
        print(f"Scan media analysis error: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("🎵 Fonie - RFID Music Player")
    print("=" * 50)
    start_audio_keepalive()
    set_system_volume(playback_state['volume'])
    pico_connect()
    threading.Thread(target=serial_listener, daemon=True).start()
    threading.Thread(target=pico_listener,   daemon=True).start()
    threading.Thread(target=wifi_sync_thread, daemon=True).start()
    threading.Thread(target=button_monitor_thread, daemon=True).start()
    threading.Thread(target=scan_and_analyze_existing_media, daemon=True).start()
    print("📡 Serial listeners, Wi-Fi monitor, button monitor & audio vibe analyzer started")
    print("=" * 50)
    app.run(host='127.0.0.1', port=5001, debug=False)