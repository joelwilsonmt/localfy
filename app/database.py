import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime, timezone

DATA_PATH = os.environ.get("DATA_PATH", "/data")
DB_PATH = os.path.join(DATA_PATH, "localfy.db")


def init_db():
    os.makedirs(DATA_PATH, exist_ok=True)
    os.makedirs(os.path.join(DATA_PATH, "sync"), exist_ok=True)
    with get_conn() as conn:
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
    # WAL lets the sync task write while web requests read without "database is
    # locked"; busy_timeout makes brief write contention wait instead of erroring.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- sync_targets ---

def upsert_target(id: str, type: str, name: str, spotify_url: str, image_url: str = None):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO sync_targets (id, type, name, spotify_url, image_url)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, image_url=excluded.image_url
        """, (id, type, name, spotify_url, image_url))


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


def set_target_track_count(id: str, track_count: int):
    with get_conn() as conn:
        conn.execute("UPDATE sync_targets SET track_count = ? WHERE id = ?", (track_count, id))


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


# --- downloaded_tracks ---

def get_downloaded_ids(target_id: str) -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT spotify_id FROM downloaded_tracks WHERE target_id = ?", (target_id,)
        ).fetchall()
        return {r["spotify_id"] for r in rows}


def search_downloaded(query: str, limit: int = 80) -> list[dict]:
    like = f"%{query}%"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT spotify_id, name, artist, target_id FROM downloaded_tracks "
            "WHERE name LIKE ? OR artist LIKE ? ORDER BY artist, name LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_downloaded(spotify_id: str, name: str, artist: str, target_id: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO downloaded_tracks (spotify_id, name, artist, target_id, downloaded_at)
            VALUES (?, ?, ?, ?, ?)
        """, (spotify_id, name, artist, target_id, now))


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


def log_sync_finish(log_id: int, status: str, message: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_logs SET finished_at = ?, status = ?, message = ? WHERE id = ?",
            (now, status, message, log_id),
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


def clear_failed_tracks(target_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM failed_tracks WHERE target_id = ?", (target_id,))


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
        total_tracks = conn.execute("SELECT COUNT(*) AS c FROM downloaded_tracks").fetchone()["c"]
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
