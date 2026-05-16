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
from flask import Flask, render_template, request, jsonify
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
AUDIO_DEVICE  = 'hw:2,0'
MPV_SOCKET    = '/tmp/mpv.sock'

os.makedirs(MEDIA_DIR, exist_ok=True)

# ── Global state ──────────────────────────────────────────────────────────────
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
    'prev':   {'pressed': False, 'last_event': None},
    'play':   {'pressed': False, 'last_event': None},
    'next':   {'pressed': False, 'last_event': None},
    'vol_up': {'pressed': False, 'last_event': None},
    'vol_dn': {'pressed': False, 'last_event': None},
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

# ── Settings ──────────────────────────────────────────────────────────────────
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            return json.load(f)
    return {'brightness': {'ring': 60, 'matrix': 40}, 'volume': 80}

def save_settings(settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

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

# ── Pico communication ────────────────────────────────────────────────────────
def send_pico(event, **kwargs):
    global pico_serial
    if not pico_serial:
        return
    payload = json.dumps({"event": event, **kwargs})
    try:
        pico_serial.write((payload + '\n').encode())
        print(f"→ Pico: {payload}")
        log_uart('→', 'pico', payload)
        sys.stdout.flush()
    except Exception as e:
        print(f"❌ Pico send error: {e}")

def pico_connect():
    global pico_serial
    try:
        pico_serial = serial.Serial(PICO_PORT, SERIAL_BAUD, timeout=1)
        print(f"✅ Pico connected on {PICO_PORT}")
        send_pico("READY")
        b = load_settings().get('brightness', {})
        if b:
            send_pico("BRIGHTNESS", ring=b.get('ring', 60), matrix=b.get('matrix', 40))
    except Exception as e:
        print(f"⚠️  Pico not connected: {e}")
        pico_serial = None

def handle_pico_message(data):
    global battery_state, button_state, playback_state, pico_is_alive
    event = data.get('event')

    if event == 'PONG':
        pico_is_alive = True
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
            button_state[btn]['pressed']    = pressed
            button_state[btn]['last_event'] = datetime.now().isoformat()
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
            if not pico_serial:
                pico_connect()
                threading.Event().wait(5)
                continue
            if pico_serial.in_waiting:
                line = pico_serial.readline().decode('utf-8').strip()
                if line:
                    print(f"← Pico: {line}")
                    log_uart('←', 'pico', line)
                    try:
                        handle_pico_message(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except serial.SerialException:
            pico_serial = None
            threading.Event().wait(5)
        except Exception:
            threading.Event().wait(1)

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
    try:
        subprocess.run(['aplay', '-D', AUDIO_DEVICE, path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"❌ Sound error: {e}")

def stop_playback():
    global mpv_process
    if mpv_process and mpv_process.poll() is None:
        for vol in range(100, 0, -5):
            mpv_command({"command": ["set_property", "volume", vol]})
            time.sleep(0.05)
        mpv_process.terminate()
        mpv_process = None
    if os.path.exists(MPV_SOCKET):
        try: os.remove(MPV_SOCKET)
        except: pass

def play_mapping(mapping):
    global mpv_process, playback_state
    stop_playback()
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
    mpv_process = subprocess.Popen(
        ['mpv', '--no-video', f'--input-ipc-server={MPV_SOCKET}'] + tracks,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    playback_state['paused'] = False
    color = mapping.get('color')
    if color: send_pico("PLAYING", r=color['r'], g=color['g'], b=color['b'])
    else:     send_pico("PLAYING")

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
        'outtmpl':         os.path.join(media_path, '%(playlist_index)s-%(title)s.%(ext)s'),
        'quiet':           True, 'no_warnings': True,
        'ffmpeg_location': '/usr/bin',
        'postprocessors':  [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
    }
    for url in urls:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([url])
            done += 1
            download_queue[uid]['progress'] = int((done / total) * 100)
        except Exception as e:
            errors.append(str(e))
    if errors and done == 0:
        download_queue[uid] = {'status': 'error', 'progress': 0, 'error': errors[0]}; return
    color = extract_dominant_color(mapping.get('thumbnail'))
    download_queue[uid] = {'status': 'ready', 'progress': 100, 'error': None}
    mappings = load_mappings()
    if uid in mappings:
        mappings[uid]['status']     = 'ready'
        mappings[uid]['media_path'] = media_path
        if color: mappings[uid]['color'] = color
        save_mappings(mappings)
    print(f"✅ Download complete for {uid}")

def start_download(uid, mapping):
    threading.Thread(target=download_mapping, args=(uid, mapping), daemon=True).start()

# ── ESP32 serial listener ─────────────────────────────────────────────────────
def serial_listener():
    global esp32_serial, current_tag
    while True:
        try:
            if not esp32_serial:
                esp32_serial = serial.Serial(ESP32_PORT, SERIAL_BAUD, timeout=1)
                print(f"✅ ESP32 connected on {ESP32_PORT}")
            if esp32_serial.in_waiting:
                line = esp32_serial.readline().decode('utf-8').strip()
                print(f"📨 ESP32: {line}")
                if line:
                    log_uart('←', 'esp32', line)
                    try: handle_esp32_event(json.loads(line))
                    except json.JSONDecodeError: pass
        except serial.SerialException as e:
            print(f"❌ ESP32 serial error: {e}"); esp32_serial = None; threading.Event().wait(5)
        except Exception as e:
            print(f"❌ Error: {e}"); threading.Event().wait(1)

def handle_esp32_event(event):
    global active_rfid_tag, current_tag, esp32_is_alive
    event_type = event.get('event')
    uid        = event.get('uid')
    if event_type == 'PONG':
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
        send_pico("TAG_ON", mapped=is_mapped)
        if is_mapped: play_sound('tag_mapped_32.wav'); play_mapping(mappings[uid])
        else:         play_sound('tag_unknown_32.wav'); send_pico("TAG_UNKNOWN")
        active_rfid_tag = uid
    elif event_type == 'TAG_OFF':
        print(f"📱 TAG OFF: {uid}")
        current_tag = {'present': False, 'uid': None, 'timestamp': datetime.now().isoformat()}
        send_pico("TAG_OFF", uid=uid); stop_playback(); active_rfid_tag = None
    elif event_type == 'READY':
        print("✅ ESP32 ready!"); send_pico("IDLE")

# ── Web routes ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search')
def search():
    query = request.args.get('q', '').strip()
    mtype = request.args.get('type', 'all')
    if not query or len(query) < 2: return jsonify([])
    try:
        results = []
        if mtype in ('all', 'track'):
            for t in ytmusic.search(query, filter='songs', limit=5):
                results.append({'type': 'track', 'id': t.get('videoId'),
                    'title': t.get('title', 'Unknown'),
                    'artist': ', '.join([a['name'] for a in t.get('artists', [])]),
                    'album': t.get('album', {}).get('name', '') if t.get('album') else '',
                    'duration': t.get('duration', ''),
                    'thumbnail': t.get('thumbnails', [{}])[-1].get('url', ''),})
        if mtype in ('all', 'album'):
            for a in ytmusic.search(query, filter='albums', limit=5):
                results.append({'type': 'album', 'id': a.get('browseId'),
                    'title': a.get('title', 'Unknown'),
                    'artist': ', '.join([ar['name'] for ar in a.get('artists', [])]),
                    'year': a.get('year', ''),
                    'thumbnail': a.get('thumbnails', [{}])[-1].get('url', ''),})
        if mtype in ('all', 'playlist'):
            for p in ytmusic.search(query, filter='playlists', limit=5):
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
    return jsonify(mappings)

@app.route('/api/mappings/add', methods=['POST'])
def add_mapping():
    data = request.json
    uid  = data.get('uid', '').strip()
    if not uid: return jsonify({'error': 'UID required'}), 400
    mapping = {'uid': uid, 'type': data.get('type'), 'title': data.get('title'),
               'artist': data.get('artist'), 'ytmusic_id': data.get('id'),
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
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/mappings/retry/<uid>', methods=['POST'])
def retry_mapping(uid):
    mappings = load_mappings()
    if uid not in mappings: return jsonify({'error': 'Not found'}), 404
    mappings[uid]['status'] = 'pending'; save_mappings(mappings)
    start_download(uid, mappings[uid])
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

@app.route('/api/debug')
def api_debug():
    return jsonify({
        'buttons':         button_state,
        'battery':         battery_state,
        'playback':        playback_state,
        'tag':             current_tag,
        'brightness':      load_settings().get('brightness', {}),
        'pico_connected':  pico_is_alive,
        'esp32_connected': esp32_is_alive,
    })

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

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("🎵 Fonie - RFID Music Player")
    print("=" * 50)
    set_system_volume(playback_state['volume'])
    pico_connect()
    threading.Thread(target=serial_listener, daemon=True).start()
    threading.Thread(target=pico_listener,   daemon=True).start()
    print("📡 Serial listeners started")
    print("=" * 50)
    app.run(host='127.0.0.1', port=5001, debug=False)