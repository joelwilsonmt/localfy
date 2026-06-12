import logging
import os
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheFileHandler

logger = logging.getLogger(__name__)

router = APIRouter()

DATA_PATH = os.environ.get("DATA_PATH", "/data")
CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/auth/callback")

SCOPES = " ".join([
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
])


def get_auth_manager() -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_handler=CacheFileHandler(cache_path=os.path.join(DATA_PATH, ".cache")),
        open_browser=False,
        show_dialog=False,
    )


def is_authenticated() -> bool:
    cache_path = os.path.join(DATA_PATH, ".cache")
    if not os.path.exists(cache_path):
        logger.info("Auth check: no cache file at %s", cache_path)
        return False
    auth = get_auth_manager()
    token = auth.get_cached_token()
    if not token:
        logger.info("Auth check: cache file exists but get_cached_token() returned None (scope mismatch?)")
        return False
    if auth.is_token_expired(token):
        try:
            auth.refresh_access_token(token["refresh_token"])
            logger.info("Auth check: token refreshed successfully")
            return True
        except Exception as e:
            logger.warning("Auth check: token refresh failed: %s", e)
            return False
    return True


def get_access_token() -> str | None:
    auth = get_auth_manager()
    token = auth.get_cached_token()
    if not token:
        return None
    if auth.is_token_expired(token):
        try:
            token = auth.refresh_access_token(token["refresh_token"])
        except Exception:
            return None
    return token["access_token"]


@router.get("/auth/login")
def login(request: Request):
    auth = get_auth_manager()
    url = auth.get_authorize_url()
    return RedirectResponse(url)


@router.get("/auth/callback")
def callback(request: Request, code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/?error=spotify_denied")
    auth = get_auth_manager()
    auth.get_access_token(code)
    return RedirectResponse("/dashboard")


@router.get("/auth/logout")
def logout():
    cache_path = os.path.join(DATA_PATH, ".cache")
    if os.path.exists(cache_path):
        os.remove(cache_path)
    return RedirectResponse("/")
