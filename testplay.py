import os
from spotipy.oauth2 import SpotifyOAuth
import spotipy
from dotenv import load_dotenv

load_dotenv()

# Auth
auth_manager = SpotifyOAuth(
    client_id=os.environ.get('SPOTIFY_CLIENT_ID'),
    client_secret=os.environ.get('SPOTIFY_CLIENT_SECRET'),
    redirect_uri=os.environ.get('SPOTIFY_REDIRECT_URI'),
    scope='user-read-playback-state user-modify-playback-state',
    cache_path='.spotifycache'
)

sp = spotipy.Spotify(auth_manager=auth_manager)

# Get devices
print("🔍 Getting devices...")
devices = sp.devices()

print(f"Found {len(devices['devices'])} device(s):")
for d in devices['devices']:
    print(f"  - {d['name']} ({d['id']}) - Active: {d['is_active']}")

# Find Fonie
fonie_device = None
for d in devices['devices']:
    if 'Fonie' in d['name'] or 'spotifyd' in d['name'].lower():
        fonie_device = d
        break

if not fonie_device:
    print("❌ Fonie device not found!")
    exit(1)

print(f"\n✅ Found Fonie: {fonie_device['name']}")


# The Beatles Yesterday - confirmed available
track_uri = "spotify:track:3BQHpFgAp4l80e1XslIjNI"

print(f"\n🎵 Playing: {track_uri}")
try:
    sp.start_playback(device_id=fonie_device['id'], uris=[track_uri])
    print("✅ Playback started!")
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()