import sqlite3

from app import database as db
from tests.conftest import add_target, make_song


def _user_version() -> int:
    with db.get_conn() as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_migrates_v0_downloads_to_per_target_rows(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(path))
    # The original schema: downloads keyed by spotify_id alone.
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sync_targets (id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
            spotify_url TEXT, enabled INTEGER DEFAULT 0, schedule TEXT DEFAULT 'daily',
            last_synced TEXT, track_count INTEGER DEFAULT 0, image_url TEXT);
        CREATE TABLE downloaded_tracks (spotify_id TEXT PRIMARY KEY, name TEXT, artist TEXT,
            target_id TEXT, downloaded_at TEXT);
        INSERT INTO downloaded_tracks VALUES ('t1', 'Song', 'Artist', 'pA', '2026-01-01');
        INSERT INTO downloaded_tracks VALUES ('t2', 'Other', 'Artist', NULL, '2026-01-01');
    """)
    conn.commit()
    conn.close()

    db.init_db()
    db.init_db()   # idempotent

    assert _user_version() == len(db._MIGRATIONS)
    assert db.get_downloaded_ids("pA") == {"t1"}
    # The same track can now be recorded for a second playlist.
    db.mark_downloaded("t1", "Song", "Artist", "pB")
    assert db.get_downloaded_ids("pB") == {"t1"}
    assert db.get_library_stats()["total_tracks"] == 2   # distinct tracks, not rows


def test_fresh_db_is_at_latest_version(env):
    assert _user_version() == len(db._MIGRATIONS)


def test_track_snapshot_roundtrip(env):
    add_target("p1", "P1")
    songs = [make_song("a" * 22, "First", "X", "Y"), make_song("b" * 22, "Second", "Z")]
    db.replace_target_tracks("p1", songs)
    rows = db.get_target_tracks("p1")
    assert [r["name"] for r in rows] == ["First", "Second"]
    assert rows[0]["artist"] == "X, Y" and rows[0]["primary_artist"] == "X"
    assert rows[0]["duration_ms"] == 200_000
    assert db.get_target("p1")["tracks_fetched_at"]

    db.replace_target_tracks("p1", songs[1:])   # replaces, doesn't append
    assert [r["name"] for r in db.get_target_tracks("p1")] == ["Second"]


def test_upsert_keeps_track_count_unless_given(env):
    db.upsert_target(id="p1", type="playlist", name="P1", spotify_url=None, track_count=12)
    db.upsert_target(id="p1", type="playlist", name="Renamed", spotify_url=None)
    t = db.get_target("p1")
    assert (t["name"], t["track_count"]) == ("Renamed", 12)
    db.upsert_target(id="p1", type="playlist", name="Renamed", spotify_url=None, track_count=15)
    assert db.get_target("p1")["track_count"] == 15


def test_replace_failed_tracks_keeps_known_reason(env):
    s = make_song("c" * 22, "Gone", "Band")
    db.replace_failed_tracks("p1", [s])
    db.set_failed_error("p1", s["song_id"], "No match found on YouTube")
    db.replace_failed_tracks("p1", [s])   # a plain sync carries no error text
    assert db.get_failed_tracks("p1")[0]["error"] == "No match found on YouTube"
