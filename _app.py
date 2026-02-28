import os
import json
import serial
import threading
import sys
from flask import Flask, render_template, request, redirect, jsonify, url_for
from spotipy.oauth2 import SpotifyOAuth
import spotipy
from datetime import datetime
import secrets
from dotenv import load_dotenv

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

load_dotenv()

# ── Serial ports ──────────────────────────────────────────────────────────────
ESP32_PORT  = '/dev/ttyAMA2'   # GPIO4/5  – UART3
PICO_PORT   = '/dev/ttyAMA5'   # GPIO12/13  – UART5
SERIAL_BAUD = 115200

CONFIG_FILE    = 'rfid_mappings.json'
SPOTIFY_CACHE  = '.spotifycache'

SPOTIFY_CLIENT_ID     = os.environ.get('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')
SPOTIFY_REDIRECT_URI  = os.environ.get('SPOTIFY_REDIRECT_URI', 'https://fonie2.local:5000/callback')
SPOTIFY_SCOPE = ('user-read-playback-state user-modify-playback-state '
                 'user-read-currently-playing streaming')

# ── Global state ──────────────────────────────────────────────────────────────
spotify_client      = None
esp32_serial        = None
pico_serial         = None
active_rfid_tag     = None
current_tag = {
    'present': False, 'uid': None, 'timestamp': None,
    'mapped': False, 'track_name': None, 'artist': None
}
preferred_device_id = None


# ── Pico communication ────────────────────────────────────────────────────────
def send_pico_event(event: str, **kwargs):
    """Send a semantic event to the Pico over UART."""
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
        send_pico_event("READY")
    except Exception as e:
        print(f"⚠️  Pico not connected: {e}")
        sys.stdout.flush()
        pico_serial = None

def pico_listener():
    """Optional: listen for messages from Pico (button presses, acks, etc.)"""
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
                    try:
                        handle_pico_message(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except serial.SerialException as e:
            print(f"❌ Pico serial error: {e}")
            pico_serial = None
            threading.Event().wait(5)
        except Exception as e:
            print(f"❌ Pico error: {e}")
            threading.Event().wait(1)

def handle_pico_message(msg):
    """Handle messages coming FROM the Pico (buttons, etc.)"""
    event = msg.get('event')
    if event == 'BUTTON':
        btn = msg.get('button')
        print(f"🔘 Button pressed: {btn}")
        # TODO: handle button actions


# ── Spotify OAuth ─────────────────────────────────────────────────────────────
def get_spotify_oauth():
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
        cache_path=SPOTIFY_CACHE,
        open_browser=False
    )

def get_spotify_client():
    global spotify_client
    auth_manager = get_spotify_oauth()
    token_info = auth_manager.get_cached_token()
    if not token_info:
        return None
    if auth_manager.is_token_expired(token_info):
        token_info = auth_manager.refresh_access_token(token_info['refresh_token'])
    spotify_client = spotipy.Spotify(auth_manager=auth_manager)
    return spotify_client


# ── RFID mappings ─────────────────────────────────────────────────────────────
def load_mappings():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_mappings(mappings):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(mappings, f, indent=2)


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
    global spotify_client, active_rfid_tag, current_tag

    event_type = event.get('event')
    uid        = event.get('uid')

    if event_type == 'TAG_ON':
        print(f"📱 TAG ON: {uid}")
        mappings  = load_mappings()
        is_mapped = uid in mappings

        current_tag = {
            'present':    True,
            'uid':        uid,
            'timestamp':  datetime.now().isoformat(),
            'mapped':     is_mapped,
            'track_name': mappings[uid]['name']   if is_mapped else None,
            'artist':     mappings[uid]['artist'] if is_mapped else None,
        }

        send_pico_event("TAG_ON", uid=uid, mapped=is_mapped)
        handle_rfid_on(uid)

    elif event_type == 'TAG_OFF':
        print(f"📱 TAG OFF: {uid}")
        current_tag = {'present': False, 'uid': None,
                       'timestamp': datetime.now().isoformat()}
        send_pico_event("TAG_OFF", uid=uid)
        handle_rfid_off(uid)

    elif event_type == 'READY':
        print("✅ ESP32 ready!")
        send_pico_event("IDLE")

def handle_rfid_on(uid):
    global spotify_client, active_rfid_tag

    active_rfid_tag = uid
    mappings = load_mappings()

    if uid in mappings:
        track_info = mappings[uid]
        if not spotify_client:
            spotify_client = get_spotify_client()
        if spotify_client:
            success = play_spotify_track(spotify_client, track_info['uri'])
            if success:
                send_pico_event("PLAYING",
                                name=track_info['name'],
                                artist=track_info['artist'])
        else:
            print("❌ No spotify_client!")
            sys.stdout.flush()
    else:
        send_pico_event("TAG_UNKNOWN", uid=uid)

def handle_rfid_off(uid):
    global spotify_client, active_rfid_tag

    if uid == active_rfid_tag:
        try:
            if spotify_client:
                spotify_client.pause_playback()
                # send_pico_event("PAUSED")
        except:
            pass
        active_rfid_tag = None

def play_spotify_track(sp_client, spotify_uri):
    global preferred_device_id
    try:
        devices = sp_client.devices()
        if not devices.get('devices'):
            print("❌ No devices available")
            return False

        device_id = None
        for device in devices['devices']:
            if 'librespot' in device['name'].lower() or 'fonie' in device['name'].lower():
                device_id = device['id']
                break
        if not device_id and preferred_device_id:
            for device in devices['devices']:
                if device['id'] == preferred_device_id:
                    device_id = device['id']
                    break
        if not device_id:
            device_id = devices['devices'][0]['id']

        if spotify_uri.startswith('spotify:album:') or spotify_uri.startswith('spotify:playlist:'):
            sp_client.start_playback(device_id=device_id, context_uri=spotify_uri)
        else:
            sp_client.start_playback(device_id=device_id, uris=[spotify_uri])

        print("✅ Playback started!")
        return True

    except Exception as e:
        print(f"❌ Playback error: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Web routes ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    auth_manager = get_spotify_oauth()
    token_info   = auth_manager.get_cached_token()
    authenticated = token_info and not auth_manager.is_token_expired(token_info)

    user_info = None
    if authenticated:
        try:
            sp = spotipy.Spotify(auth_manager=auth_manager)
            user_info = sp.current_user()
        except:
            pass

    return render_template('index.html',
                           authenticated=authenticated,
                           user_info=user_info,
                           mappings=load_mappings())

@app.route('/api/devices')
def api_devices():
    global spotify_client
    try:
        auth_manager = get_spotify_oauth()
        token_info   = auth_manager.get_cached_token()
        if token_info and auth_manager.is_token_expired(token_info):
            token_info = auth_manager.refresh_access_token(token_info['refresh_token'])
        spotify_client = spotipy.Spotify(auth_manager=auth_manager)
        devices = spotify_client.devices()
        return jsonify(devices.get('devices', []))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/set-device/<device_id>', methods=['POST'])
def set_device(device_id):
    global preferred_device_id
    preferred_device_id = device_id
    return jsonify({'success': True, 'device_id': device_id})

@app.route('/login')
def login():
    return redirect(get_spotify_oauth().get_authorize_url())

@app.route('/callback')
def callback():
    global spotify_client
    code = request.args.get('code')
    if code:
        try:
            get_spotify_oauth().get_access_token(code)
            spotify_client = get_spotify_client()
            return redirect(url_for('index'))
        except Exception as e:
            return f"Authentication failed: {e}", 400
    return "No code received", 400

@app.route('/logout')
def logout():
    global spotify_client
    if os.path.exists(SPOTIFY_CACHE):
        os.remove(SPOTIFY_CACHE)
    spotify_client = None
    return redirect(url_for('index'))

@app.route('/api/mappings')
def api_mappings():
    return jsonify(load_mappings())

@app.route('/api/mappings/add', methods=['POST'])
def add_mapping():
    data       = request.json
    rfid_tag   = data.get('rfid_tag', '').strip()
    spotify_uri = data.get('spotify_uri', '').strip()
    name       = data.get('name', '').strip()
    artist     = data.get('artist', '').strip()

    if not rfid_tag or not spotify_uri:
        return jsonify({'error': 'RFID tag and Spotify URI required'}), 400

    mappings = load_mappings()
    mappings[rfid_tag] = {
        'uri': spotify_uri, 'name': name, 'artist': artist,
        'added': datetime.now().isoformat()
    }
    save_mappings(mappings)
    return jsonify({'success': True})

@app.route('/api/mappings/delete/<rfid_tag>', methods=['POST'])
def delete_mapping(rfid_tag):
    mappings = load_mappings()
    if rfid_tag in mappings:
        del mappings[rfid_tag]
        save_mappings(mappings)
        return jsonify({'success': True})
    return jsonify({'error': 'Mapping not found'}), 404

@app.route('/api/search')
def search():
    global spotify_client
    if not spotify_client:
        spotify_client = get_spotify_client()
    if not spotify_client:
        return jsonify({'error': 'Not authenticated'}), 401

    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify([])

    try:
        results = spotify_client.search(q=query, type='track,album,playlist', limit=10)
        items = []
        for track in results.get('tracks', {}).get('items', []):
            if track:
                items.append({'name': track.get('name', 'Unknown'),
                              'artist': ', '.join([a.get('name') for a in track.get('artists', [])]),
                              'uri': track.get('uri', ''), 'type': 'track'})
        for album in results.get('albums', {}).get('items', []):
            if album:
                items.append({'name': album.get('name', 'Unknown'),
                              'artist': ', '.join([a.get('name') for a in album.get('artists', [])]),
                              'uri': album.get('uri', ''), 'type': 'album'})
        for pl in results.get('playlists', {}).get('items', []):
            if pl:
                items.append({'name': pl.get('name', 'Unknown'),
                              'artist': f"Playlist by {pl.get('owner', {}).get('display_name', '?')}",
                              'uri': pl.get('uri', ''), 'type': 'playlist'})
        return jsonify(items)
    except Exception as e:
        print(f"Search error: {e}")
        return jsonify([])

@app.route('/api/current-tag')
def api_current_tag():
    return jsonify(current_tag)

@app.route('/api/current-playback')
def current_playback():
    global spotify_client
    if not spotify_client:
        spotify_client = get_spotify_client()
    if not spotify_client:
        return jsonify({'playing': False})
    try:
        playback = spotify_client.current_playback()
        if not playback or not playback.get('item'):
            return jsonify({'playing': False})
        item = playback['item']
        return jsonify({
            'playing':    playback.get('is_playing', False),
            'track_name': item.get('name', 'Unknown'),
            'artist':     ', '.join([a.get('name') for a in item.get('artists', [])]),
            'album':      item.get('album', {}).get('name', 'Unknown'),
            'progress':   playback.get('progress_ms', 0),
            'duration':   item.get('duration_ms', 0),
            'image':      item.get('album', {}).get('images', [{}])[0].get('url', '')
        })
    except Exception as e:
        print(f"Playback error: {e}")
        return jsonify({'playing': False})

# ── New API: send arbitrary event to Pico (for testing) ──────────────────────
@app.route('/api/pico/event', methods=['POST'])
def api_pico_event():
    data = request.json
    if not data or 'event' not in data:
        return jsonify({'error': 'event required'}), 400
    event = data.pop('event')
    send_pico_event(event, **data)
    return jsonify({'success': True})


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("🎵 Fonie - RFID Spotify Player")
    print("=" * 50)

    spotify_client = get_spotify_client()
    if spotify_client:
        print("✅ Spotify authenticated from cache")
    else:
        print("⚠️  Not authenticated - visit web UI to login")

    # Connect to Pico first so it's ready when ESP32 events arrive
    pico_connect()

    threading.Thread(target=serial_listener, daemon=True).start()
    print("📡 ESP32 serial listener started")

    threading.Thread(target=pico_listener, daemon=True).start()
    print("📡 Pico serial listener started")

    print("=" * 50)
    print("🌐 Starting on 127.0.0.1:5001")
    print("=" * 50)

    app.run(host='127.0.0.1', port=5001, debug=False)