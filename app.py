import os
import json
import serial
import threading
from flask import Flask, render_template, request, redirect, jsonify, url_for
from spotipy.oauth2 import SpotifyOAuth
import spotipy
from datetime import datetime
import secrets

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Configuration
SERIAL_PORT = '/dev/serial0'
SERIAL_BAUD = 115200
CONFIG_FILE = 'rfid_mappings.json'
SPOTIFY_CACHE = '.spotifycache'

SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')
SPOTIFY_REDIRECT_URI = os.environ.get('SPOTIFY_REDIRECT_URI', 'https://fonie2.local:5000/callback')
SPOTIFY_SCOPE = 'user-read-playback-state user-modify-playback-state user-read-currently-playing streaming'

# Global state
spotify_client = None
serial_connection = None
active_rfid_tag = None

# ==================== SPOTIFY OAUTH ====================
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

# ==================== RFID MAPPINGS ====================
def load_mappings():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_mappings(mappings):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(mappings, f, indent=2)

# ==================== SERIAL COMMUNICATION ====================
def serial_listener():
    global serial_connection
    
    while True:
        try:
            if not serial_connection:
                serial_connection = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=1)
                print(f"✅ Connected to ESP32 on {SERIAL_PORT}")
            
            if serial_connection.in_waiting:
                line = serial_connection.readline().decode('utf-8').strip()
                if line:
                    try:
                        event = json.loads(line)
                        handle_event(event)
                    except json.JSONDecodeError:
                        print(f"❌ Invalid JSON: {line}")
                    
        except serial.SerialException as e:
            print(f"❌ Serial error: {e}")
            serial_connection = None
            threading.Event().wait(5)
        except Exception as e:
            print(f"❌ Error: {e}")
            threading.Event().wait(1)

def handle_event(event):
    """Handle JSON events from ESP32"""
    global spotify_client, active_rfid_tag
    
    event_type = event.get('event')
    uid = event.get('uid')
    
    if event_type == 'TAG_ON':
        print(f"📱 TAG ON: {uid}")
        handle_rfid_on(uid)
    
    elif event_type == 'TAG_OFF':
        print(f"📱 TAG OFF: {uid}")
        handle_rfid_off(uid)
    
    elif event_type == 'READY':
        print("✅ ESP32 ready!")

def handle_rfid_on(uid):
    """Tag placed on reader"""
    global spotify_client, active_rfid_tag
    
    mappings = load_mappings()
    
    if active_rfid_tag and active_rfid_tag != uid:
        try:
            if spotify_client:
                spotify_client.pause_playback()
        except:
            pass
    
    if uid in mappings:
        track_info = mappings[uid]
        
        if not spotify_client:
            spotify_client = get_spotify_client()
        
        if spotify_client:
            play_spotify_track(spotify_client, track_info['uri'])
            active_rfid_tag = uid
    else:
        print(f"⚠️  Unknown tag: {uid}")

def handle_rfid_off(uid):
    """Tag removed from reader"""
    global spotify_client, active_rfid_tag
    
    if uid == active_rfid_tag:
        try:
            if spotify_client:
                spotify_client.pause_playback()
        except:
            pass
        active_rfid_tag = None

def play_spotify_track(sp_client, spotify_uri):
    """Play track on Spotify"""
    try:
        devices = sp_client.devices()
        
        if not devices['devices']:
            print("❌ No Spotify devices available")
            return False
        
        device_id = devices['devices'][0]['id']
        
        if spotify_uri.startswith('spotify:album:') or spotify_uri.startswith('spotify:playlist:'):
            sp_client.start_playback(device_id=device_id, context_uri=spotify_uri)
        else:
            sp_client.start_playback(device_id=device_id, uris=[spotify_uri])
        
        print(f"▶️  Playing: {spotify_uri}")
        return True
        
    except Exception as e:
        print(f"❌ Error playing track: {e}")
        return False

# ==================== WEB ROUTES ====================
@app.route('/')
def index():
    auth_manager = get_spotify_oauth()
    token_info = auth_manager.get_cached_token()
    authenticated = token_info and not auth_manager.is_token_expired(token_info)
    
    user_info = None
    if authenticated:
        try:
            sp = spotipy.Spotify(auth_manager=auth_manager)
            user_info = sp.current_user()
        except:
            pass
    
    mappings = load_mappings()
    
    return render_template('index.html',
                         authenticated=authenticated,
                         user_info=user_info,
                         mappings=mappings)

@app.route('/login')
def login():
    auth_manager = get_spotify_oauth()
    auth_url = auth_manager.get_authorize_url()
    return redirect(auth_url)

@app.route('/callback')
def callback():
    global spotify_client
    
    auth_manager = get_spotify_oauth()
    code = request.args.get('code')
    
    if code:
        try:
            auth_manager.get_access_token(code)
            spotify_client = get_spotify_client()
            print("✅ Spotify authenticated")
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
    mappings = load_mappings()
    return jsonify(mappings)

@app.route('/api/mappings/add', methods=['POST'])
def add_mapping():
    data = request.json
    rfid_tag = data.get('rfid_tag', '').strip()
    spotify_uri = data.get('spotify_uri', '').strip()
    name = data.get('name', '').strip()
    artist = data.get('artist', '').strip()
    
    if not rfid_tag or not spotify_uri:
        return jsonify({'error': 'RFID tag and Spotify URI required'}), 400
    
    mappings = load_mappings()
    mappings[rfid_tag] = {
        'uri': spotify_uri,
        'name': name,
        'artist': artist,
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
                items.append({
                    'name': track.get('name', 'Unknown'),
                    'artist': ', '.join([a.get('name', 'Unknown') for a in track.get('artists', [])]),
                    'uri': track.get('uri', ''),
                    'type': 'track'
                })
        
        for album in results.get('albums', {}).get('items', []):
            if album:
                items.append({
                    'name': album.get('name', 'Unknown'),
                    'artist': ', '.join([a.get('name', 'Unknown') for a in album.get('artists', [])]),
                    'uri': album.get('uri', ''),
                    'type': 'album'
                })
        
        for playlist in results.get('playlists', {}).get('items', []):
            if playlist:
                items.append({
                    'name': playlist.get('name', 'Unknown'),
                    'artist': f"Playlist by {playlist.get('owner', {}).get('display_name', 'Unknown')}",
                    'uri': playlist.get('uri', ''),
                    'type': 'playlist'
                })
        
        return jsonify(items)
    except Exception as e:
        print(f"Search error: {e}")
        return jsonify([])

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
            'playing': playback.get('is_playing', False),
            'track_name': item.get('name', 'Unknown'),
            'artist': ', '.join([a.get('name', 'Unknown') for a in item.get('artists', [])]),
            'album': item.get('album', {}).get('name', 'Unknown'),
            'progress': playback.get('progress_ms', 0),
            'duration': item.get('duration_ms', 0),
            'image': item.get('album', {}).get('images', [{}])[0].get('url', '')
        })
    except Exception as e:
        print(f"Playback error: {e}")
        return jsonify({'playing': False})

# ==================== MAIN ====================
if __name__ == '__main__':
    print("🎵 Fonie - RFID Spotify Player")
    print("=" * 50)
    
    spotify_client = get_spotify_client()
    if spotify_client:
        print("✅ Spotify authenticated from cache")
    else:
        print("⚠️  Not authenticated - visit web UI to login")
    
    serial_thread = threading.Thread(target=serial_listener, daemon=True)
    serial_thread.start()
    print("📡 Serial listener started")
    
    print("=" * 50)
    print("🌐 Starting on 127.0.0.1:5001")
    print("=" * 50)
    
    app.run(host='127.0.0.1', port=5001, debug=False)
