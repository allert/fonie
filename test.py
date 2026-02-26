from spotipy.oauth2 import SpotifyOAuth
import spotipy

auth_manager = SpotifyOAuth(
    client_id='a94b2f8d011248e8b83de46cecf9325c',
    client_secret='46a7ace2ff68481694fb47b8da6520f8',
    redirect_uri='https://fonie2.local:5000/callback',
    cache_path='.spotifycache'
)

sp = spotipy.Spotify(auth_manager=auth_manager)
devices = sp.devices()
print(f"Total devices: {len(devices['devices'])}")
for d in devices['devices']:
    print(f"  {d['name']} - Active: {d['is_active']}")