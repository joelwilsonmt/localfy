import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime, timezone

DATA_PATH = os.environ.get("DATA_PATH", "/data")
DB_PATH = os.path.join(DATA_PATH, "localfy.db")


def init_db():
    os.makedirs(DATA_PATH, exist_ok=True)
    with get_conn() as conn:
        # WAL lets the sync task write while web requests read without "database
        # is locked". It's stored in the database file, so setting it once here
        # covers every later connection.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sync_targets (
                id          TEXT PRIMARY KEY,
                type        TEXT NOT NULL,
                name        TEXT NOT NULL,
                spotify_url TEXT,
                enabled     INTEGER DEFAULT 0,
                schedule    TEXT DEFAULT 'daily',
                last_synced TEXT,
                track_count INTEGER DEFAULT 0,
                image_url   TEXT
            );

            CREATE TABLE IF NOT EXISTS downloaded_tracks (
                spotify_id    TEXT PRIMARY KEY,
                name          TEXT,
                artist        TEXT,
                target_id     TEXT,
                downloaded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id   TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                status      TEXT,
                message     TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS failed_tracks (
                spotify_id TEXT NOT NULL,
                target_id  TEXT NOT NULL,
                name       TEXT,
                artist     TEXT,
                error      TEXT,
                failed_at  TEXT,
                PRIMARY KEY (target_id, spotify_id)
            );

            CREATE INDEX IF NOT EXISTS idx_downloaded_target ON downloaded_tracks(target_id);
            CREATE INDEX IF NOT EXISTS idx_logs_target ON sync_logs(target_id, id);
        """)
        _migrate(conn)


# Schema changes on top of the original (v0) schema above, applied in order and
# tracked with PRAGMA user_version. Each runs in its own transaction.
_MIGRATIONS = [
    # v1: one downloaded_tracks row per (playlist, track). It used to be keyed by
    # spotify_id alone, so a track in two playlists was only ever recorded for the
    # first — every other playlist re-sent it to spotdl on every sync.
    """
    CREATE TABLE downloaded_tracks_v1 (
        spotify_id    TEXT NOT NULL,
        target_id     TEXT NOT NULL,
        name          TEXT,
        artist        TEXT,
        downloaded_at TEXT,
        PRIMARY KEY (target_id, spotify_id)
    );
    INSERT OR IGNORE INTO downloaded_tracks_v1
        SELECT spotify_id, COALESCE(target_id, ''), name, artist, downloaded_at FROM downloaded_tracks;
    DROP TABLE downloaded_tracks;
    ALTER TABLE downloaded_tracks_v1 RENAME TO downloaded_tracks;
    CREATE INDEX idx_downloaded_spotify ON downloaded_tracks(spotify_id);
    """,
    # v2: each target's Spotify track list as of its last fetch, so pages and the
    # player queue don't call Spotify on every view (and still work when it 403s).
    """
    CREATE TABLE target_tracks (
        target_id      TEXT NOT NULL,
        position       INTEGER NOT NULL,
        spotify_id     TEXT NOT NULL,
        name           TEXT,
        artist         TEXT,
        primary_artist TEXT,
        duration_ms    INTEGER,
        PRIMARY KEY (target_id, position)
    );
    ALTER TABLE sync_targets ADD COLUMN tracks_fetched_at TEXT;
    """,
    # v3: a one-line result per sync run ("3 new · 1 failed") for the Activity page.
    """
    ALTER TABLE sync_logs ADD COLUMN summary TEXT;
    """,
]


def _migrate(conn: sqlite3.Connection):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for n, script in enumerate(_MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {n};\nCOMMIT;")


def reconcile_running_logs() -> list[str]:
    """On startup, mark any sync log left 'running' (process killed mid-sync) as
    interrupted, and return the affected target ids so the (in-memory) progress
    store can reflect that a sync was cut short rather than silently showing idle."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        target_ids = [
            r["target_id"]
            for r in conn.execute("SELECT DISTINCT target_id FROM sync_logs WHERE status = 'running'")
        ]
        conn.execute(
            "UPDATE sync_logs SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
            (now,),
        )
    return target_ids


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # Brief write contention (a sync writing while a page reads) waits instead
    # of erroring. WAL mode is set once in init_db.
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- sync_targets ---

def upsert_target(id: str, type: str, name: str, spotify_url: str, image_url: str = None,
                  track_count: int | None = None):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO sync_targets (id, type, name, spotify_url, image_url, track_count)
            VALUES (?, ?, ?, ?, ?, COALESCE(?, 0))
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, image_url=excluded.image_url,
                track_count=CASE WHEN tracks_fetched_at IS NULL THEN COALESCE(?, track_count)
                                 ELSE track_count END
        """, (id, type, name, spotify_url, image_url, track_count, track_count))
    # Once a target has a track-list snapshot, its count comes from that (the
    # tracks localfy can actually download); Spotify's raw total also counts
    # local files and podcast episodes, and would make the two flip-flop.


def get_all_targets() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM sync_targets ORDER BY type, name").fetchall()
        return [dict(r) for r in rows]


def get_target(id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sync_targets WHERE id = ?", (id,)).fetchone()
        return dict(row) if row else None


def set_target_enabled(id: str, enabled: bool):
    with get_conn() as conn:
        conn.execute("UPDATE sync_targets SET enabled = ? WHERE id = ?", (int(enabled), id))


def set_target_schedule(id: str, schedule: str):
    with get_conn() as conn:
        conn.execute("UPDATE sync_targets SET schedule = ? WHERE id = ?", (schedule, id))


def set_target_synced(id: str, track_count: int):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_targets SET last_synced = ?, track_count = ? WHERE id = ?",
            (now, track_count, id),
        )


def get_enabled_targets() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM sync_targets WHERE enabled = 1").fetchall()
        return [dict(r) for r in rows]


# --- target_tracks (track-list snapshots) ---

def replace_target_tracks(target_id: str, songs: list[dict]):
    """Store a target's Spotify track list (spotdl Song dicts), in order."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (target_id, i, s["song_id"], s.get("name", ""), ", ".join(a for a in s.get("artists") or [] if a),
         s.get("artist", ""), int(s.get("duration") or 0) * 1000)
        for i, s in enumerate(songs)
    ]
    with get_conn() as conn:
        conn.execute("DELETE FROM target_tracks WHERE target_id = ?", (target_id,))
        conn.executemany(
            "INSERT INTO target_tracks (target_id, position, spotify_id, name, artist, primary_artist, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute("UPDATE sync_targets SET tracks_fetched_at = ?, track_count = ? WHERE id = ?",
                     (now, len(rows), target_id))


def get_all_target_tracks() -> dict[str, list[dict]]:
    """Every snapshot, grouped by target, in one query (for the dashboard)."""
    out: dict[str, list[dict]] = {}
    with get_conn() as conn:
        for r in conn.execute(
            "SELECT target_id, spotify_id, name, artist, primary_artist, duration_ms "
            "FROM target_tracks ORDER BY target_id, position"
        ):
            out.setdefault(r["target_id"], []).append(dict(r))
    return out


def get_target_tracks(target_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT spotify_id, name, artist, primary_artist, duration_ms FROM target_tracks "
            "WHERE target_id = ? ORDER BY position",
            (target_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- downloaded_tracks ---

def get_downloaded_ids(target_id: str) -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT spotify_id FROM downloaded_tracks WHERE target_id = ?", (target_id,)
        ).fetchall()
        return {r["spotify_id"] for r in rows}


def search_downloaded(query: str, limit: int = 80) -> list[dict]:
    """Matching downloads: up to `limit` distinct tracks, with a row for each
    playlist a track is in (so a caller can fall back to another copy)."""
    like = f"%{query}%"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT spotify_id, name, artist, target_id FROM downloaded_tracks "
            "WHERE spotify_id IN ("
            "  SELECT spotify_id FROM downloaded_tracks WHERE name LIKE ? OR artist LIKE ? "
            "  GROUP BY spotify_id ORDER BY MIN(artist), MIN(name) LIMIT ?"
            ") ORDER BY artist, name, spotify_id",
            (like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_downloaded(spotify_id: str, name: str, artist: str, target_id: str):
    mark_downloaded_many(target_id, [{"song_id": spotify_id, "name": name, "artist": artist}])


def mark_downloaded_many(target_id: str, songs: list[dict]):
    """Record several downloads for a target in one transaction (spotdl Song dicts)."""
    if not songs:
        return
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO downloaded_tracks (spotify_id, name, artist, target_id, downloaded_at)
            VALUES (?, ?, ?, ?, ?)
        """, [(s["song_id"], s.get("name", ""), s.get("artist", ""), target_id, now) for s in songs])


def forget_downloads(target_id: str, spotify_ids: set[str] | list[str]):
    """Drop download records for a target (their files are gone)."""
    with get_conn() as conn:
        conn.executemany("DELETE FROM downloaded_tracks WHERE target_id = ? AND spotify_id = ?",
                         [(target_id, sid) for sid in spotify_ids])


# --- settings ---

def get_setting(key: str, default: str = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# --- sync_logs ---

def log_sync_start(target_id: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sync_logs (target_id, started_at, status) VALUES (?, ?, 'running')",
            (target_id, now),
        )
        # Keep sync_logs from growing unbounded: retain the latest 50 per target.
        conn.execute(
            "DELETE FROM sync_logs WHERE target_id = ? AND id NOT IN "
            "(SELECT id FROM sync_logs WHERE target_id = ? ORDER BY id DESC LIMIT 50)",
            (target_id, target_id),
        )
        return cur.lastrowid


def log_sync_finish(log_id: int, status: str, message: str, summary: str | None = None):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_logs SET finished_at = ?, status = ?, message = ?, summary = ? WHERE id = ?",
            (now, status, message, summary, log_id),
        )


def get_all_recent_logs(limit: int = 150) -> list[dict]:
    """Latest sync logs across every target, newest first, with target names
    for the activity page (targets since removed render as their raw id)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT l.*, t.name AS target_name, t.type AS target_type "
            "FROM sync_logs l LEFT JOIN sync_targets t ON t.id = l.target_id "
            "ORDER BY l.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_logs(target_id: str, limit: int = 5) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_logs WHERE target_id = ? ORDER BY id DESC LIMIT ?",
            (target_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# --- failed_tracks ---

def replace_failed_tracks(target_id: str, songs: list[dict]):
    """Replace the recorded failed set for a target (songs: spotdl Song dicts).

    Preserves the human-readable error/failed_at already recorded for tracks that
    are still failing — a plain sync re-derives the failed set with no error text,
    which would otherwise wipe reasons set by a retry or a manual YouTube download.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        prior = {
            r["spotify_id"]: (r["error"], r["failed_at"])
            for r in conn.execute(
                "SELECT spotify_id, error, failed_at FROM failed_tracks WHERE target_id = ?",
                (target_id,),
            )
        }
        conn.execute("DELETE FROM failed_tracks WHERE target_id = ?", (target_id,))
        rows = []
        for s in songs:
            prev_error, prev_at = prior.get(s["song_id"], ("", now))
            rows.append((
                s["song_id"], target_id, s.get("name", ""), s.get("artist", ""),
                s.get("error") or prev_error or "", prev_at or now,
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO failed_tracks (spotify_id, target_id, name, artist, error, failed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def get_failed_tracks(target_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM failed_tracks WHERE target_id = ? ORDER BY artist, name", (target_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def remove_failed_track(target_id: str, spotify_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM failed_tracks WHERE target_id = ? AND spotify_id = ?",
                     (target_id, spotify_id))


def set_failed_error(target_id: str, spotify_id: str, error: str):
    with get_conn() as conn:
        conn.execute("UPDATE failed_tracks SET error = ? WHERE target_id = ? AND spotify_id = ?",
                     (error, target_id, spotify_id))


def get_failed_counts() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT target_id, COUNT(*) AS c FROM failed_tracks GROUP BY target_id"
        ).fetchall()
        return {r["target_id"]: r["c"] for r in rows}


# --- stats ---

def get_library_stats() -> dict:
    with get_conn() as conn:
        total_tracks = conn.execute(
            "SELECT COUNT(DISTINCT spotify_id) AS c FROM downloaded_tracks"
        ).fetchone()["c"]
        enabled = conn.execute("SELECT COUNT(*) AS c FROM sync_targets WHERE enabled = 1").fetchone()["c"]
        synced = conn.execute(
            "SELECT COUNT(*) AS c FROM sync_targets WHERE last_synced IS NOT NULL"
        ).fetchone()["c"]
        failed = conn.execute("SELECT COUNT(*) AS c FROM failed_tracks").fetchone()["c"]
        last = conn.execute("SELECT MAX(last_synced) AS m FROM sync_targets").fetchone()["m"]
    return {
        "total_tracks": total_tracks, "enabled_targets": enabled,
        "synced_targets": synced, "failed_tracks": failed, "last_synced": last,
    }
