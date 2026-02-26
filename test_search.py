#!/usr/bin/env python3
import os
from spotipy.oauth2 import SpotifyOAuth
import spotipy
from dotenv import load_dotenv

load_dotenv()

auth_manager = SpotifyOAuth(
    client_id=os.environ.get('SPOTIFY_CLIENT_ID'),
    client_secret=os.environ.get('SPOTIFY_CLIENT_SECRET'),
    redirect_uri=os.environ.get('SPOTIFY_REDIRECT_URI'),
    cache_path='.spotifycache'
)

sp = spotipy.Spotify(auth_manager=auth_manager)

# Search for a track
print("🔍 Searching for 'The Beatles Yesterday'...")
results = sp.search(q='The Beatles Yesterday', type='track', limit=1)

if results['tracks']['items']:
    track = results['tracks']['items'][0]
    print(f"\n✅ Found track:")
    print(f"   Name: {track['name']}")
    print(f"   Artist: {track['artists'][0]['name']}")
    print(f"   URI: {track['uri']}")
    print(f"   Available: {track['available_markets'][:5]}... ({len(track['available_markets'])} countries)")
    
    # Check if available in NL
    if 'NL' in track['available_markets']:
        print(f"   ✅ Available in Netherlands!")
    else:
        print(f"   ❌ NOT available in Netherlands!")
else:
    print("❌ No results")
