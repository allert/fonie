import os
import json
import serial
import threading
import sys
import subprocess
import time
from flask import Flask, render_template, request, redirect, jsonify
from datetime import datetime
import secrets
from ytmusicapi import YTMusic
import yt_dlp

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# ── Config ────────────────────────────────────────────────────────────────────
ESP32_PORT   = '/dev/ttyAMA2'
PICO_PORT    = '/dev/ttyAMA5'
SERIAL_BAUD  = 115200
MEDIA_DIR    = os.path.expanduser('~/rfid-player/media')
MAPPINGS_FILE = os.path.expanduser('~/rfid-player/rfid_mappings.json')
SOUNDS_DIR   = os.path.expanduser('~/rfid-player/sounds')
AUDIO_DEVICE = 'hw:2,0'

os.makedirs(MEDIA_DIR, exist_ok=True)

# ── Global state ──────────────────────────────────────────────────────────────
esp32_serial   = None
pico_serial    = None
active_rfid_tag = None
current_tag    = {'present': False, 'uid': None, 'timestamp': None}
mpv_process    = None
download_queue = {}   # uid -> {status, progress, error}
ytmusic        = YTMusic()

# ── Mappings ──────────────────────────────────────────────────────────────────
def load_mappings():
    if os.path.exists(MAPPINGS_FILE):
        with open(MAPPINGS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_mappings(mappings):
    with open(MAPPINGS_FILE, 'w') as f:
        json.dump(mappings, f, indent=2)

# ── Pico communication ────────────────────────────────────────────────────────
def send_pico(event, **kwargs):
    global pico_serial
    if not pico_serial:
        return
    payload = json.dumps({"event": event, **kwargs})
    try:
        pico_serial.write((payload + '\n').encode())
        print(f"→ Pico: {payload}")
        sys.stdout.flush()
    except Exception as e:
        print(f"❌ Pico send error: {e}")
        sys.stdout.flush()

def pico_connect():
    global pico_serial
    try:
        pico_serial = serial.Serial(PICO_PORT, SERIAL_BAUD, timeout=1)
        print(f"✅ Pico connected on {PICO_PORT}")
        sys.stdout.flush()
        send_pico("READY")
    except Exception as e:
        print(f"⚠️  Pico not connected: {e}")
        sys.stdout.flush()
        pico_serial = None

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
                    sys.stdout.flush()
        except serial.SerialException as e:
            print(f"❌ Pico serial error: {e}")
            pico_serial = None
            threading.Event().wait(5)
        except Exception as e:
            print(f"❌ Pico error: {e}")
            threading.Event().wait(1)

# ── Audio playback ────────────────────────────────────────────────────────────
def play_sound(filename):
    path = os.path.join(SOUNDS_DIR, filename)
    if not os.path.exists(path):
        return
    try:
        subprocess.run(
            ['aplay', '-D', AUDIO_DEVICE, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )  # run instead of Popen — blocks until done
    except Exception as e:
        print(f"❌ Sound error: {e}")

def play_mapping(mapping):
    global mpv_process
    stop_playback()
    
    media_path = mapping.get('media_path')
    if not media_path or not os.path.exists(media_path):
        print(f"❌ Media path not found: {media_path}")
        return

    tracks = sorted([
        os.path.join(media_path, f)
        for f in os.listdir(media_path)
        if f.endswith(('.mp3', '.m4a', '.opus', '.webm'))
    ])

    if not tracks:
        print("❌ No tracks found")
        return

    print(f"▶️  Playing {len(tracks)} track(s) from {media_path}")
    mpv_process = subprocess.Popen(
        ['mpv', '--no-video'] + tracks,  # no audio-device flag
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    send_pico("PLAYING")

def stop_playback():
    global mpv_process
    if mpv_process and mpv_process.poll() is None:
        mpv_process.terminate()
        mpv_process = None


# ── Download queue ────────────────────────────────────────────────────────────
def download_mapping(uid, mapping):
    """Download audio for a mapping in the background."""
    media_path = os.path.join(MEDIA_DIR, uid)
    os.makedirs(media_path, exist_ok=True)

    download_queue[uid] = {'status': 'downloading', 'progress': 0, 'error': None}

    ytmusic_id = mapping.get('ytmusic_id')
    mtype      = mapping.get('type', 'track')

    if mtype == 'track':
        urls = [f'https://music.youtube.com/watch?v={ytmusic_id}']
    else:
        # album or playlist — fetch all track IDs
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
            print(f"❌ Failed to fetch track list: {e}")
            download_queue[uid] = {'status': 'error', 'progress': 0, 'error': str(e)}
            return

    total  = len(urls)
    done   = 0
    errors = []

    ydl_opts = {
        'format':     'bestaudio/best',
        'outtmpl':    os.path.join(media_path, '%(playlist_index)s-%(title)s.%(ext)s'),
        'quiet':      True,
        'location':   '/usr/bin',
        'no_warnings': True,
        'postprocessors': [{
            'key':            'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
        }],
    }

    for i, url in enumerate(urls):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            done += 1
            download_queue[uid]['progress'] = int((done / total) * 100)
        except Exception as e:
            errors.append(str(e))
            print(f"❌ Download error for {url}: {e}")

    if errors and done == 0:
        download_queue[uid] = {'status': 'error', 'progress': 0, 'error': errors[0]}
    else:
        download_queue[uid] = {'status': 'ready', 'progress': 100, 'error': None}
        mappings = load_mappings()
        if uid in mappings:
            mappings[uid]['status']     = 'ready'
            mappings[uid]['media_path'] = media_path
            save_mappings(mappings)
        print(f"✅ Download complete for {uid}")

def start_download(uid, mapping):
    t = threading.Thread(target=download_mapping, args=(uid, mapping), daemon=True)
    t.start()

# ── ESP32 serial listener ─────────────────────────────────────────────────────
def serial_listener():
    global esp32_serial, current_tag

    while True:
        try:
            if not esp32_serial:
                esp32_serial = serial.Serial(ESP32_PORT, SERIAL_BAUD, timeout=1)
                print(f"✅ ESP32 connected on {ESP32_PORT}")
                sys.stdout.flush()

            if esp32_serial.in_waiting:
                line = esp32_serial.readline().decode('utf-8').strip()
                print(f"📨 ESP32: {line}")
                sys.stdout.flush()
                if line:
                    try:
                        handle_esp32_event(json.loads(line))
                    except json.JSONDecodeError as e:
                        print(f"❌ JSON error: {e}")
                        sys.stdout.flush()

        except serial.SerialException as e:
            print(f"❌ ESP32 serial error: {e}")
            sys.stdout.flush()
            esp32_serial = None
            threading.Event().wait(5)
        except Exception as e:
            print(f"❌ Error: {e}")
            sys.stdout.flush()
            threading.Event().wait(1)

def handle_esp32_event(event):
    global active_rfid_tag, current_tag

    event_type = event.get('event')
    uid        = event.get('uid')

    if event_type == 'TAG_ON':
        print(f"📱 TAG ON: {uid}")
        mappings  = load_mappings()
        is_mapped = uid in mappings and mappings[uid].get('status') == 'ready'

        current_tag = {
            'present':   True,
            'uid':       uid,
            'timestamp': datetime.now().isoformat(),
            'mapped':    is_mapped,
            'title':     mappings[uid]['title']  if is_mapped else None,
            'artist':    mappings[uid]['artist'] if is_mapped else None,
        }

        send_pico("TAG_ON", mapped=is_mapped)

        if is_mapped:
            play_sound('tag_mapped_32.wav')
            play_mapping(mappings[uid])
        else:
            play_sound('tag_unknown_32.wav')
            send_pico("TAG_UNKNOWN")

        active_rfid_tag = uid

    elif event_type == 'TAG_OFF':
        print(f"📱 TAG OFF: {uid}")
        current_tag = {'present': False, 'uid': None, 'timestamp': datetime.now().isoformat()}
        send_pico("TAG_OFF", uid=uid)
        stop_playback()
        active_rfid_tag = None

    elif event_type == 'READY':
        print("✅ ESP32 ready!")
        send_pico("IDLE")

# ── Web routes ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', mappings=load_mappings())

@app.route('/api/search')
def search():
    query = request.args.get('q', '').strip()
    mtype = request.args.get('type', 'all')  # track, album, playlist, all

    if not query or len(query) < 2:
        return jsonify([])

    try:
        results = []

        if mtype in ('all', 'track'):
            tracks = ytmusic.search(query, filter='songs', limit=5)
            for t in tracks:
                results.append({
                    'type':       'track',
                    'id':         t.get('videoId'),
                    'title':      t.get('title', 'Unknown'),
                    'artist':     ', '.join([a['name'] for a in t.get('artists', [])]),
                    'album':      t.get('album', {}).get('name', '') if t.get('album') else '',
                    'duration':   t.get('duration', ''),
                    'thumbnail':  t.get('thumbnails', [{}])[-1].get('url', ''),
                })

        if mtype in ('all', 'album'):
            albums = ytmusic.search(query, filter='albums', limit=5)
            for a in albums:
                results.append({
                    'type':      'album',
                    'id':        a.get('browseId'),
                    'title':     a.get('title', 'Unknown'),
                    'artist':    ', '.join([ar['name'] for ar in a.get('artists', [])]),
                    'year':      a.get('year', ''),
                    'thumbnail': a.get('thumbnails', [{}])[-1].get('url', ''),
                })

        if mtype in ('all', 'playlist'):
            playlists = ytmusic.search(query, filter='playlists', limit=5)
            for p in playlists:
                results.append({
                    'type':      'playlist',
                    'id':        p.get('browseId'),
                    'title':     p.get('title', 'Unknown'),
                    'artist':    p.get('author', ''),
                    'count':     p.get('itemCount', ''),
                    'thumbnail': p.get('thumbnails', [{}])[-1].get('url', ''),
                })

        return jsonify(results)
    except Exception as e:
        print(f"Search error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/mappings')
def api_mappings():
    mappings = load_mappings()
    # Merge in live download queue status
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

    if not uid:
        return jsonify({'error': 'UID required'}), 400

    mapping = {
        'uid':        uid,
        'type':       data.get('type'),
        'title':      data.get('title'),
        'artist':     data.get('artist'),
        'ytmusic_id': data.get('id'),
        'status':     'pending',
        'media_path': None,
        'added':      datetime.now().isoformat(),
    }

    mappings = load_mappings()
    mappings[uid] = mapping
    save_mappings(mappings)

    # Kick off download immediately
    start_download(uid, mapping)

    return jsonify({'success': True})

@app.route('/api/mappings/delete/<uid>', methods=['POST'])
def delete_mapping(uid):
    mappings = load_mappings()
    if uid in mappings:
        # Clean up media
        media_path = mappings[uid].get('media_path')
        if media_path and os.path.exists(media_path):
            import shutil
            shutil.rmtree(media_path, ignore_errors=True)
        del mappings[uid]
        save_mappings(mappings)
        return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/mappings/retry/<uid>', methods=['POST'])
def retry_mapping(uid):
    mappings = load_mappings()
    if uid not in mappings:
        return jsonify({'error': 'Not found'}), 404
    mappings[uid]['status'] = 'pending'
    save_mappings(mappings)
    start_download(uid, mappings[uid])
    return jsonify({'success': True})

@app.route('/api/current-tag')
def api_current_tag():
    return jsonify(current_tag)

@app.route('/api/download-queue')
def api_download_queue():
    return jsonify(download_queue)

@app.route('/api/pico/event', methods=['POST'])
def api_pico_event():
    data = request.json
    if not data or 'event' not in data:
        return jsonify({'error': 'event required'}), 400
    event = data.pop('event')
    send_pico(event, **data)
    return jsonify({'success': True})

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("🎵 Fonie - RFID Music Player")
    print("=" * 50)

    pico_connect()
    threading.Thread(target=serial_listener, daemon=True).start()
    threading.Thread(target=pico_listener,   daemon=True).start()

    print("📡 Serial listeners started")
    print("=" * 50)
    print("🌐 Starting on 127.0.0.1:5001")
    print("=" * 50)

    app.run(host='127.0.0.1', port=5001, debug=False)