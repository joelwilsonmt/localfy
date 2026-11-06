import os

import pytest
from fastapi.testclient import TestClient
from spotipy.exceptions import SpotifyException

from app import database as db
from app import downloader, main, spotify
from app.main import youtube_video_url
from tests.conftest import add_target, make_song, song_filename

TRACK = make_song("t" * 22, "Song One", "Artist A", "Artist B")
OTHER = make_song("o" * 22, "Song Two", "Artist C")


@pytest.fixture
def client(env, monkeypatch):
    monkeypatch.setattr(main, "is_authenticated", lambda: True)
    with TestClient(main.app) as c:
        yield c


def _no_spotify(*_a, **_k):
    raise AssertionError("page should not call Spotify")


def test_healthz_needs_no_auth(env, monkeypatch):
    monkeypatch.setattr(main, "is_authenticated", lambda: False)
    with TestClient(main.app) as c:
        assert c.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?si=abc123",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDxyz",
    "youtube.com/shorts/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
    "  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ",
])
def test_youtube_links_are_canonicalized(url):
    assert youtube_video_url(url) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    "https://evil.example/?youtube.com/watch?v=dQw4w9WgXcQ",
    "--yt-dlp-args=--exec id youtu.be/dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ|https://open.spotify.com/track/x",
    "https://www.youtube.com/watch?v=short",
    "",
])
def test_non_video_links_are_rejected(url):
    assert youtube_video_url(url) is None


def test_youtube_route_validates_input(client):
    add_target("p1", "P1")
    good_id = "t" * 22
    r = client.post(f"/targets/p1/tracks/{good_id}/youtube", data={"url": "https://evil.example/youtu.be/x"})
    assert r.status_code == 400
    r = client.post("/targets/p1/tracks/bad|id/youtube", data={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 400


def test_stream_blocks_path_traversal(client, env):
    add_target("p1", "P1")
    (env.music / "secret.mp3").write_text("x")
    r = client.get("/stream/p1/%2E%2E%2Fsecret.mp3")
    assert r.status_code == 403


def test_toggle_with_explicit_state_is_idempotent(client):
    add_target("p1", "P1", enabled=False)
    for _ in range(2):
        assert client.post("/targets/p1/toggle", data={"enabled": "true"}).json() == {"enabled": True}
    assert db.get_target("p1")["enabled"] == 1
    assert client.post("/targets/p1/toggle").json() == {"enabled": False}   # legacy flip still works


def test_delete_removed_setting_is_gone(client):
    assert client.post("/settings/sync-delete-removed", data={"enabled": "true"}).status_code in (404, 405)
    assert "Delete removed tracks" not in client.get("/settings").text


def test_detail_renders_from_snapshot_without_spotify(client, env, monkeypatch):
    target = add_target("p1", "P1")
    db.replace_target_tracks("p1", [TRACK, OTHER])
    folder = downloader.music_dir(target)
    os.makedirs(folder)
    open(os.path.join(folder, song_filename(TRACK)), "w").close()
    monkeypatch.setattr(spotify, "get_target_song_meta", _no_spotify)

    html = client.get("/targets/p1").text
    assert "Song One" in html and "Song Two" in html
    assert "1 of 2 downloaded" in html

    scan = client.get("/targets/p1/scan").json()
    assert scan["matches"] == {TRACK["song_id"]: song_filename(TRACK)}

    tracks = client.get("/targets/p1/local-tracks").json()["tracks"]
    assert tracks == [{"filename": song_filename(TRACK), "title": "Song One", "artist": "Artist A, Artist B"}]


def test_detail_for_blocked_playlist_offers_no_sync(client, monkeypatch):
    add_target("p1", "Daily Mix 1")

    def blocked(_t):
        raise SpotifyException(403, -1, "Forbidden")

    monkeypatch.setattr(spotify, "get_target_song_meta", blocked)
    html = client.get("/targets/p1").text
    assert "can't be synced" in html
    assert 'id="detail-sync-btn"' not in html


def test_dashboard_falls_back_to_cached_library(client, monkeypatch):
    add_target("p1", "Cached Playlist")
    monkeypatch.setattr(spotify, "get_liked_songs_info", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    html = client.get("/dashboard?refresh=1").text
    assert "Cached Playlist" in html
    assert "Showing your library as of the last refresh" in html


def test_search_lists_a_shared_track_once(client, env):
    for tid, name in (("pA", "A"), ("pB", "B")):
        target = add_target(tid, name)
        folder = downloader.music_dir(target)
        os.makedirs(folder)
        open(os.path.join(folder, song_filename(TRACK)), "w").close()
        db.mark_downloaded(TRACK["song_id"], TRACK["name"], TRACK["artist"], tid)
    results = client.get("/api/search?q=Song").json()["results"]
    assert len(results) == 1 and results[0]["filename"] == song_filename(TRACK)


def test_playlist_total_reads_renamed_items_field():
    assert spotify._playlist_total({"items": {"total": 22}}) == 22
    assert spotify._playlist_total({"tracks": {"total": 7}}) == 7   # pre-2026 shape
    assert spotify._playlist_total({"name": "x"}) is None


def test_dashboard_counts_downloads_against_the_track_list(client, env, monkeypatch):
    # Files for tracks removed from the playlist stay on disk; they must not
    # push the card past its total ("187 of 176").
    target = add_target("p1", "Shrinking")
    db.replace_target_tracks("p1", [TRACK])
    folder = downloader.music_dir(target)
    os.makedirs(folder)
    for s in (TRACK, OTHER):
        open(os.path.join(folder, song_filename(s)), "w").close()
    db.set_setting("library_refreshed_at", "2999-01-01T00:00:00+00:00")
    assert db.get_target("p1")["track_count"] == 1
    html = client.get("/dashboard").text
    assert "1 of 1 downloaded" in html
    assert "2 of 1" not in html


def test_dashboard_splits_synced_from_library(client):
    add_target("p1", "Auto Synced Mix")
    add_target("p2", "Never Touched", enabled=False)
    db.set_setting("library_refreshed_at", "2999-01-01T00:00:00+00:00")
    html = client.get("/dashboard").text
    synced, library = html.split('id="lib-more"')
    assert "Auto Synced Mix" in synced and "Never Touched" not in synced
    assert "Never Touched" in library
    assert 'aria-label="Auto-sync Never Touched"' in library
    assert 'aria-label="Sync schedule for Never Touched"' not in library   # slim card: no schedule select
    assert 'aria-label="Sync schedule for Auto Synced Mix"' in synced


def test_detail_for_other_spotify_errors_still_offers_sync(client, monkeypatch):
    # A message that merely contains "403" isn't a block; only the status code is.
    add_target("p1", "P1")

    def flaky(_t):
        raise RuntimeError("Read timed out after 403 ms")

    monkeypatch.setattr(spotify, "get_target_song_meta", flaky)
    html = client.get("/targets/p1").text
    assert "can't be synced" not in html
    assert "Couldn't load tracks from Spotify" in html


# ── Regression tests for preflight findings ────────────────────────────────────

def _capture_spawn(monkeypatch):
    """Collect spawned background coroutines instead of running them on a live
    loop; the returned list's coroutines can be run with asyncio.run."""
    spawned = []
    monkeypatch.setattr(main.sched, "spawn", lambda coro: spawned.append(coro))
    return spawned


def _run_all(spawned):
    import asyncio
    for c in spawned:
        asyncio.run(c)


@pytest.mark.parametrize("path", ["/dashboard", "/activity", "/settings", "/targets/p1", "/api/queue",
                                  "/targets/p1/progress", "/stream/p1/x.mp3"])
def test_protected_routes_redirect_when_logged_out(env, monkeypatch, path):
    monkeypatch.setattr(main, "is_authenticated", lambda: False)
    with TestClient(main.app) as c:
        r = c.get(path, follow_redirects=False)
    assert r.status_code in (302, 303, 307) and r.headers["location"] == "/"


def test_oauth_callback_rejects_denied_and_mismatched_state(env, monkeypatch):
    from app import auth
    with TestClient(main.app) as c:
        r = c.get("/auth/callback?error=access_denied", follow_redirects=False)
        assert r.headers["location"] == "/?error=spotify_denied"
        monkeypatch.setattr(auth, "_oauth_state", "expected")
        r = c.get("/auth/callback?code=abc&state=forged", follow_redirects=False)
        assert r.headers["location"] == "/?error=state_mismatch"


def test_stream_blocks_sibling_prefix_folder(client, env):
    # /music/P1 must not serve files from /music/P1 Extra (startswith bug).
    add_target("p1", "P1")
    sib = env.music / "P1 Extra"
    sib.mkdir()
    (sib / "a.mp3").write_text("x")
    assert client.get("/stream/p1/..%2FP1%20Extra%2Fa.mp3").status_code == 403


@pytest.mark.parametrize("url", [
    "https://open.spotify.com/playlist/" + "a" * 22,
    "https://open.spotify.com/intl-de/playlist/" + "a" * 22 + "?si=x",
])
def test_add_by_url_accepts_share_links(client, monkeypatch, url):
    spawned = _capture_spawn(monkeypatch)
    monkeypatch.setattr(spotify, "get_playlist_info", lambda sid: {
        "id": sid, "name": "Shared", "type": "playlist", "spotify_url": url, "track_count": 7})
    r = client.post("/targets/add", data={"url": url})
    assert r.status_code == 200 and r.json()["ok"]
    t = db.get_target("a" * 22)
    assert t["enabled"] == 1 and t["track_count"] == 7
    assert len(spawned) == 1   # the first sync starts right away
    spawned[0].close()


def test_add_by_url_rejects_other_links(client):
    assert client.post("/targets/add", data={"url": "https://example.com/playlist/x"}).status_code == 400


@pytest.mark.parametrize("field, value, ok", [
    ("audio_format", "opus", True), ("audio_format", "--exec", False),
    ("audio_bitrate", "auto", True), ("audio_bitrate", "9999k", False),
])
def test_audio_settings_whitelist(client, field, value, ok):
    route = "/settings/audio-format" if field == "audio_format" else "/settings/audio-bitrate"
    r = client.post(route, data={field: value})
    assert (r.status_code == 200) is ok
    assert (db.get_setting(field) == value) is ok


def test_youtube_fetch_checks_disk_even_without_failed_row(client, env, monkeypatch):
    # spotdl exits 0 without writing a file; with no failed-track row the old
    # code marked the track downloaded on the exit code alone.
    from app import downloader as dl
    add_target("p1", "P1")
    spawned = _capture_spawn(monkeypatch)

    async def no_file(video, spotify_url, out_dir, on_line=None):
        os.makedirs(out_dir, exist_ok=True)
        return 0, ""

    monkeypatch.setattr(dl, "download_youtube", no_file)
    tid = "y" * 22
    client.post(f"/targets/p1/tracks/{tid}/youtube", data={"url": "https://youtu.be/dQw4w9WgXcQ"})
    _run_all(spawned)
    assert tid not in db.get_downloaded_ids("p1")


def test_youtube_fetch_marks_downloaded_when_file_lands(client, env, monkeypatch):
    from app import downloader as dl
    target = add_target("p1", "P1")
    db.replace_failed_tracks("p1", [TRACK])
    spawned = _capture_spawn(monkeypatch)

    async def writes_file(video, spotify_url, out_dir, on_line=None):
        os.makedirs(out_dir, exist_ok=True)
        open(os.path.join(out_dir, song_filename(TRACK)), "w").close()
        return 0, ""

    monkeypatch.setattr(dl, "download_youtube", writes_file)
    client.post(f"/targets/p1/tracks/{TRACK['song_id']}/youtube", data={"url": "https://youtu.be/dQw4w9WgXcQ"})
    _run_all(spawned)
    assert TRACK["song_id"] in db.get_downloaded_ids("p1")
    assert db.get_failed_tracks("p1") == []
    assert target


def test_library_refresh_does_not_override_snapshot_count(env):
    add_target("p1", "P1")
    db.replace_target_tracks("p1", [TRACK, OTHER])          # 2 downloadable tracks
    db.upsert_target(id="p1", type="playlist", name="P1", spotify_url=None, track_count=5)  # raw total
    assert db.get_target("p1")["track_count"] == 2


def test_search_limit_counts_distinct_tracks(env):
    # Each track is in two playlists (two rows); a limit of 2 must still give 2 tracks.
    for i in range(3):
        for tid in ("pA", "pB"):
            db.mark_downloaded(f"{i}" * 22, f"Song {i}", "Artist", tid)
    rows = db.search_downloaded("Song", limit=2)
    assert len({r["spotify_id"] for r in rows}) == 2
    assert len(rows) == 4
