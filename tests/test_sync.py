import asyncio
import os

import pytest

from app import database as db
from app import downloader, scheduler, spotify
from tests.conftest import add_target, make_song, song_filename


class FakeSpotdl:
    """Stands in for downloader.download_from_meta: writes the file spotdl would
    write for each song, except ids listed in `fail`."""

    def __init__(self, fail=(), code=0, output=""):
        self.fail, self.code, self.output = set(fail), code, output
        self.calls: list[list[str]] = []

    async def __call__(self, songs, output_dir, on_line=None):
        self.calls.append([s["song_id"] for s in songs])
        os.makedirs(output_dir, exist_ok=True)
        for s in songs:
            if s["song_id"] in self.fail:
                continue
            open(os.path.join(output_dir, song_filename(s)), "w").close()
            if on_line:
                on_line(f'Downloaded "{s["name"]}"')
        return self.code, self.output


@pytest.fixture
def fake_spotdl(monkeypatch):
    fake = FakeSpotdl()
    monkeypatch.setattr(downloader, "download_from_meta", fake)
    return fake


SHARED = make_song("s" * 22, "Shared", "Artist A", "Artist B")
NEW = make_song("n" * 22, "New One", "Artist C")
MISSING = make_song("m" * 22, "Nowhere", "Artist D")


def test_sync_records_tracks_already_on_disk_without_redownloading(env, monkeypatch, fake_spotdl):
    # SHARED was recorded under playlist A only (the old one-row-per-track
    # schema) but its file is also in playlist B's folder.
    add_target("pA", "Playlist A")
    target_b = add_target("pB", "Playlist B")
    db.mark_downloaded(SHARED["song_id"], SHARED["name"], SHARED["artist"], "pA")
    folder_b = downloader.music_dir(target_b)
    os.makedirs(folder_b)
    open(os.path.join(folder_b, song_filename(SHARED)), "w").close()

    fake_spotdl.fail = {MISSING["song_id"]}
    monkeypatch.setattr(spotify, "get_target_song_meta", lambda t: [SHARED, NEW, MISSING])

    asyncio.run(scheduler._sync_target("pB", force=False))

    assert fake_spotdl.calls == [[NEW["song_id"], MISSING["song_id"]]]   # SHARED never re-sent
    assert db.get_downloaded_ids("pB") == {SHARED["song_id"], NEW["song_id"]}
    assert [f["spotify_id"] for f in db.get_failed_tracks("pB")] == [MISSING["song_id"]]
    assert [r["spotify_id"] for r in db.get_target_tracks("pB")] == [s["song_id"] for s in (SHARED, NEW, MISSING)]
    target = db.get_target("pB")
    assert target["last_synced"] and target["track_count"] == 3
    m3u = open(os.path.join(folder_b, "Playlist B.m3u8")).read().splitlines()
    assert m3u[0] == "#EXTM3U" and m3u[2] == song_filename(SHARED) and m3u[4] == song_filename(NEW)

    # Second sync: nothing new except the still-missing track.
    fake_spotdl.calls.clear()
    asyncio.run(scheduler._sync_target("pB", force=False))
    assert fake_spotdl.calls == [[MISSING["song_id"]]]


def test_download_one_judges_by_file_not_exit_code(env, monkeypatch):
    target = add_target("p1", "P1")
    monkeypatch.setattr(downloader, "download_from_meta", FakeSpotdl(fail={NEW["song_id"]}, code=0))
    assert asyncio.run(scheduler.download_one(target, NEW)) == (False, "Download finished but no file appeared")

    monkeypatch.setattr(downloader, "download_from_meta",
                        FakeSpotdl(fail={NEW["song_id"]}, code=1, output="ERROR: Video unavailable"))
    assert asyncio.run(scheduler.download_one(target, NEW)) == (False, "The matched YouTube video is unavailable")

    db.replace_failed_tracks("p1", [NEW])
    monkeypatch.setattr(downloader, "download_from_meta", FakeSpotdl())
    assert asyncio.run(scheduler.download_one(target, NEW)) == (True, None)
    assert db.get_downloaded_ids("p1") == {NEW["song_id"]}
    assert db.get_failed_tracks("p1") == []


def test_retry_uses_preresolved_metadata(env, monkeypatch, fake_spotdl):
    add_target("p1", "P1")
    db.replace_failed_tracks("p1", [NEW, MISSING])
    metas = {NEW["song_id"]: NEW}

    def get_meta(track_id):
        if track_id not in metas:
            raise RuntimeError("404")
        return metas[track_id]

    monkeypatch.setattr(spotify, "get_track_song_meta", get_meta)
    asyncio.run(scheduler._retry_failed("p1"))

    assert fake_spotdl.calls == [[NEW["song_id"]]]
    remaining = db.get_failed_tracks("p1")
    assert [f["spotify_id"] for f in remaining] == [MISSING["song_id"]]
    assert remaining[0]["error"] == "Couldn't load this track from Spotify"


def test_spawn_holds_a_reference_until_done():
    async def main():
        done = asyncio.Event()
        task = scheduler.spawn(done.wait())
        assert task in scheduler._background
        done.set()
        await task
        await asyncio.sleep(0)
        assert task not in scheduler._background

    asyncio.run(main())


def test_sync_logs_a_summary_and_human_error(env, monkeypatch):
    add_target("p1", "P1")
    monkeypatch.setattr(spotify, "get_target_song_meta", lambda t: [NEW, MISSING])
    monkeypatch.setattr(downloader, "download_from_meta", FakeSpotdl(fail={MISSING["song_id"]}))
    asyncio.run(scheduler._sync_target("p1", force=False))
    log = db.get_recent_logs("p1", 1)[0]
    assert log["status"] == "ok" and log["summary"] == "1 new · 1 couldn't be downloaded"

    from app import progress
    monkeypatch.setattr(spotify, "get_target_song_meta", lambda t: [MISSING])
    monkeypatch.setattr(downloader, "download_from_meta",
                        FakeSpotdl(fail={MISSING["song_id"]}, code=1, output="ERROR: Video unavailable"))
    asyncio.run(scheduler._sync_target("p1", force=False))
    p = progress.get_progress("p1")
    assert p["status"] == "error"
    assert p["message"] == "Nothing downloaded: The matched YouTube video is unavailable"


def test_next_run_is_seeded_from_last_sync():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    week = timedelta(weeks=1)
    last = now - timedelta(days=2)
    # Due a week after the last sync, not a week after this (re)start.
    assert scheduler.next_run_for(last.isoformat(), week, now) == last + week
    # Long overdue: run shortly instead of waiting another full interval.
    assert scheduler.next_run_for((now - timedelta(days=60)).isoformat(), week, now) == now + timedelta(seconds=30)
    # Never synced: one interval from now.
    assert scheduler.next_run_for(None, week, now) == now + week
