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


def _song_dict(track: dict, album: dict | None = None) -> dict:
    """Map a Spotify track object to spotdl's .spotdl Song schema.

    Lets us hand spotdl pre-resolved metadata so it downloads straight from
    YouTube without re-querying Spotify for every track (the per-track lookup
    that gets rate-limited). `album` supplies album-level fields when the track
    object omits them (e.g. simplified album-track objects).
    """
    alb = album or track.get("album") or {}
    artists = [a["name"] for a in track.get("artists", [])] or [""]
    album_artists = alb.get("artists") or []
    release = alb.get("release_date", "") or ""
    try:
        year = int(release[:4]) if release else 0
    except ValueError:
        year = 0
    images = alb.get("images") or []
    return {
        "name": track.get("name", ""),
        "artists": artists,
        "artist": artists[0],
        "genres": [],
        "disc_number": track.get("disc_number", 1),
        "disc_count": 1,
        "album_name": alb.get("name", ""),
        "album_artist": album_artists[0]["name"] if album_artists else artists[0],
        "duration": round(track.get("duration_ms", 0) / 1000),
        "year": year,
        "date": release,
        "track_number": track.get("track_number", 1),
        "tracks_count": alb.get("total_tracks", 0),
        "song_id": track["id"],
        "explicit": track.get("explicit", False),
        "publisher": "",
        "url": track.get("external_urls", {}).get("spotify", f"https://open.spotify.com/track/{track['id']}"),
        "isrc": (track.get("external_ids") or {}).get("isrc", ""),
        "cover_url": images[0]["url"] if images else "",
        "copyright_text": "",
        "download_url": None,
        "lyrics": None,
        "popularity": track.get("popularity", 0),
        "album_id": alb.get("id", ""),
        "artist_id": (track.get("artists") or [{}])[0].get("id", ""),
        "album_type": alb.get("album_type", "album"),
    }


def get_playlist_info(playlist_id: str) -> dict:
    """Dashboard-item dict for an arbitrary playlist (for add-by-URL)."""
    sp = get_client()
    p = sp.playlist(playlist_id, fields="id,name,images,external_urls,tracks.total")
    return {
        "id": p["id"], "name": p["name"], "type": "playlist",
        "spotify_url": p["external_urls"]["spotify"],
        "track_count": p.get("tracks", {}).get("total", 0),
        "image_url": p["images"][0]["url"] if p.get("images") else None,
    }


def get_album_info(album_id: str) -> dict:
    """Dashboard-item dict for an arbitrary album (for add-by-URL)."""
    sp = get_client()
    a = sp.album(album_id)
    artist = a["artists"][0]["name"] if a.get("artists") else ""
    return {
        "id": a["id"], "name": f"{artist} — {a['name']}", "type": "album",
        "spotify_url": a["external_urls"]["spotify"],
        "track_count": a.get("total_tracks", 0),
        "image_url": a["images"][0]["url"] if a.get("images") else None,
    }


def get_playlist_song_meta(playlist_id: str) -> list[dict]:
    """All playlist tracks as spotdl Song dicts (one Spotify paged fetch, no per-track lookups)."""
    sp = get_client()
    out = []
    response = sp.playlist_tracks(playlist_id, limit=100)
    while response:
        for item in response["items"]:
            track = item.get("track") or item.get("item")
            if not track or track.get("type") != "track" or track.get("is_local") or not track.get("id"):
                continue
            out.append(_song_dict(track))
        response = sp.next(response) if response.get("next") else None
    return out


def get_album_song_meta(album_id: str) -> list[dict]:
    """All album tracks as spotdl Song dicts.

    album_tracks returns simplified track objects (no album field), so we fetch
    the album once and pass it to _song_dict for the album-level metadata.
    """
    sp = get_client()
    album = sp.album(album_id)
    out = []
    response = sp.album_tracks(album_id, limit=50)
    while response:
        for track in response["items"]:
            if track and track.get("id"):
                out.append(_song_dict(track, album))
        response = sp.next(response) if response.get("next") else None
    return out


def get_liked_song_meta() -> list[dict]:
    """All liked songs as spotdl Song dicts."""
    sp = get_client()
    out = []
    response = sp.current_user_saved_tracks(limit=50)
    while response:
        for item in response["items"]:
            track = item.get("track")
            if track and track.get("id"):
                out.append(_song_dict(track))
        response = sp.next(response) if response["next"] else None
    return out


def get_liked_track_ids() -> list[tuple[str, str, str]]:
    """Returns list of (spotify_id, name, artist) for all liked songs."""
    sp = get_client()
    results = []
    response = sp.current_user_saved_tracks(limit=50)
    while response:
        for item in response["items"]:
            track = item["track"]
            if track and track.get("id"):
                artists = track.get("artists") or []
                results.append((
                    track["id"],
                    track["name"],
                    artists[0]["name"] if artists else "",
                ))
        response = sp.next(response) if response["next"] else None
    return results
