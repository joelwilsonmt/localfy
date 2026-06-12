import spotipy
from app.auth import get_auth_manager


def get_client() -> spotipy.Spotify:
    return spotipy.Spotify(auth_manager=get_auth_manager())


def get_user_playlists() -> list[dict]:
    sp = get_client()
    results = []
    response = sp.current_user_playlists(limit=50)
    while response:
        for item in response["items"]:
            if not item:
                continue
            tracks = item.get("tracks")
            track_count = tracks.get("total") if isinstance(tracks, dict) else None
            results.append({
                "id": item["id"],
                "name": item["name"],
                "spotify_url": item["external_urls"]["spotify"],
                "track_count": track_count,
                "image_url": item["images"][0]["url"] if item.get("images") else None,
                "type": "playlist",
            })
        response = sp.next(response) if response["next"] else None
    return results


def get_liked_songs_info() -> dict:
    sp = get_client()
    response = sp.current_user_saved_tracks(limit=1)
    return {
        "id": "liked_songs",
        "name": "Liked Songs",
        "spotify_url": "https://open.spotify.com/collection/tracks",
        "track_count": response["total"],
        "image_url": None,
        "type": "liked_songs",
    }


def get_saved_albums() -> list[dict]:
    sp = get_client()
    results = []
    response = sp.current_user_saved_albums(limit=50)
    while response:
        for item in response["items"]:
            album = item["album"]
            results.append({
                "id": album["id"],
                "name": f"{album['artists'][0]['name']} — {album['name']}",
                "spotify_url": album["external_urls"]["spotify"],
                "track_count": album["total_tracks"],
                "image_url": album["images"][0]["url"] if album.get("images") else None,
                "type": "album",
            })
        response = sp.next(response) if response["next"] else None
    return results


def get_playlist_tracks(playlist_id: str) -> list[dict]:
    """Returns all tracks in a playlist as {id, name, artist, spotify_url, duration_ms}."""
    sp = get_client()
    results = []
    response = sp.playlist_tracks(playlist_id, limit=100)
    while response:
        for item in response["items"]:
            track = item.get("track") or item.get("item")
            if not track or track.get("type") != "track" or track.get("is_local") or not track.get("id"):
                continue
            results.append({
                "id": track["id"],
                "name": track["name"],
                "artist": ", ".join(a["name"] for a in track.get("artists", [])),
                "spotify_url": track["external_urls"]["spotify"],
                "duration_ms": track.get("duration_ms", 0),
            })
        response = sp.next(response) if response.get("next") else None
    return results


def get_album_tracks(album_id: str) -> list[dict]:
    """Returns all tracks in an album as {id, name, artist, spotify_url, duration_ms}."""
    sp = get_client()
    album = sp.album(album_id)
    album_artist = album["artists"][0]["name"] if album.get("artists") else ""
    results = []
    response = sp.album_tracks(album_id, limit=50)
    while response:
        for track in response["items"]:
            artists = ", ".join(a["name"] for a in track.get("artists", []))
            results.append({
                "id": track["id"],
                "name": track["name"],
                "artist": artists or album_artist,
                "spotify_url": track["external_urls"]["spotify"],
                "duration_ms": track.get("duration_ms", 0),
            })
        response = sp.next(response) if response.get("next") else None
    return results


def get_liked_track_ids() -> list[tuple[str, str, str]]:
    """Returns list of (spotify_id, name, artist) for all liked songs."""
    sp = get_client()
    results = []
    response = sp.current_user_saved_tracks(limit=50)
    while response:
        for item in response["items"]:
            track = item["track"]
            if track:
                results.append((
                    track["id"],
                    track["name"],
                    track["artists"][0]["name"],
                ))
        response = sp.next(response) if response["next"] else None
    return results
