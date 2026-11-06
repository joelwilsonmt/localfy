import spotipy
from app.auth import get_auth_manager


def get_client() -> spotipy.Spotify:
    return spotipy.Spotify(auth_manager=get_auth_manager())


def _playlist_total(playlist: dict) -> int | None:
    """Track count from a playlist object. Spotify's 2026 API renamed the
    playlist's `tracks` field to `items`; read either."""
    for key in ("items", "tracks"):
        value = playlist.get(key)
        if isinstance(value, dict) and value.get("total") is not None:
            return value["total"]
    return None


def _image(images: list[dict] | None) -> str | None:
    """A ~300px image: sharp at the 160px detail size on 2x screens without
    pulling 640px originals for every 46px card. Spotify lists largest first;
    playlist mosaics may omit sizes, in which case take the first."""
    if not images:
        return None
    sized = [i for i in images if i.get("width")]
    if not sized:
        return images[0]["url"]
    return min(sized, key=lambda i: abs(i["width"] - 300))["url"]


def get_user_playlists() -> list[dict]:
    sp = get_client()
    results = []
    response = sp.current_user_playlists(limit=50)
    while response:
        for item in response["items"]:
            if not item:
                continue
            results.append({
                "id": item["id"],
                "name": item["name"],
                "spotify_url": item["external_urls"]["spotify"],
                "track_count": _playlist_total(item),
                "image_url": _image(item.get("images")),
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
                "image_url": _image(album.get("images")),
                "type": "album",
            })
        response = sp.next(response) if response["next"] else None
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
    p = sp.playlist(playlist_id, fields="id,name,images,external_urls,tracks.total,items.total")
    return {
        "id": p["id"], "name": p["name"], "type": "playlist",
        "spotify_url": p["external_urls"]["spotify"],
        "track_count": _playlist_total(p) or 0,
        "image_url": _image(p.get("images")),
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
        "image_url": _image(a.get("images")),
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


def search_tracks(query: str, limit: int = 8) -> list[dict]:
    """Spotify track search for the nav bar's 'download a single' results."""
    sp = get_client()
    res = sp.search(q=query, type="track", limit=limit)
    out = []
    for t in (res.get("tracks") or {}).get("items") or []:
        if not t or not t.get("id"):
            continue
        out.append({
            "id": t["id"],
            "name": t["name"],
            "artist": ", ".join(a["name"] for a in t.get("artists", [])),
            "album": (t.get("album") or {}).get("name", ""),
        })
    return out


def get_track_song_meta(track_id: str) -> dict:
    """One track as a spotdl Song dict (for one-off single downloads)."""
    sp = get_client()
    return _song_dict(sp.track(track_id))


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


def get_target_song_meta(target: dict) -> list[dict]:
    """A sync target's full track list as spotdl Song dicts, in Spotify order."""
    if target["type"] == "playlist":
        return get_playlist_song_meta(target["id"])
    if target["type"] == "album":
        return get_album_song_meta(target["id"])
    if target["type"] == "liked_songs":
        return get_liked_song_meta()
    raise LookupError(f"{target['type']} has no Spotify track list")
