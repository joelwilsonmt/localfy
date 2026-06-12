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
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
        return cur.lastrowid


def log_sync_finish(log_id: int, status: str, message: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_logs SET finished_at = ?, status = ?, message = ? WHERE id = ?",
            (now, status, message, log_id),
        )


def get_recent_logs(target_id: str, limit: int = 5) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_logs WHERE target_id = ? ORDER BY id DESC LIMIT ?",
            (target_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
