import os
import tempfile
from types import SimpleNamespace

# Module-level config is read at import time; point it somewhere harmless
# before any app module loads. Each test then gets fresh dirs via `env`.
os.environ.setdefault("DATA_PATH", tempfile.mkdtemp(prefix="lfy-data-"))
os.environ.setdefault("DOWNLOAD_PATH", tempfile.mkdtemp(prefix="lfy-music-"))
os.environ["LOCALFY_DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from app import database, downloader  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    music = tmp_path / "music"
    data.mkdir()
    music.mkdir()
    monkeypatch.setattr(database, "DATA_PATH", str(data))
    monkeypatch.setattr(database, "DB_PATH", str(data / "localfy.db"))
    monkeypatch.setattr(downloader, "DATA_PATH", str(data))
    monkeypatch.setattr(downloader, "DOWNLOAD_PATH", str(music))
    database.init_db()
    return SimpleNamespace(data=data, music=music)


def make_song(song_id: str, name: str, *artists: str) -> dict:
    """A minimal spotdl Song dict (the fields localfy reads)."""
    artists = list(artists) or [""]
    return {"song_id": song_id, "name": name, "artists": artists, "artist": artists[0], "duration": 200}


def song_filename(song: dict, ext: str = "mp3") -> str:
    """The filename spotdl would write for a song ("{artists} - {title}.{ext}")."""
    return f"{', '.join(song['artists'])} - {song['name']}.{ext}"


def add_target(id: str, name: str, type: str = "playlist", enabled: bool = True) -> dict:
    database.upsert_target(id=id, type=type, name=name, spotify_url=f"https://open.spotify.com/{type}/{id}")
    database.set_target_enabled(id, enabled)
    return database.get_target(id)
