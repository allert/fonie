import os
import json
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
me = sp.me()
print(json.dumps(me, indent=2))
