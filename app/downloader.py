import asyncio
import io
import os
import re
import subprocess
import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
DOWNLOAD_PATH = os.environ.get("DOWNLOAD_PATH", "/music")
DATA_PATH = os.environ.get("DATA_PATH", "/data")
AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "mp3")
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "320k")

_ANSI = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Explicit output template (matches spotdl's default). Pinned here because
# scan_tracks() parses "Artist - Title.ext" filenames to match local files
# against Spotify tracks; a future change to spotdl's default would break that.
OUTPUT_TEMPLATE = "{artists} - {title}.{output-ext}"


def _output(output_dir: str) -> str:
    return os.path.join(output_dir, OUTPUT_TEMPLATE)


def _base_args() -> list[str]:
    from app import database as db
    fmt = db.get_setting("audio_format") or AUDIO_FORMAT
    bitrate = db.get_setting("audio_bitrate") or AUDIO_BITRATE
    return [
        "spotdl",
        "--client-id", CLIENT_ID,
        "--client-secret", CLIENT_SECRET,
        "--format", fmt,
        "--bitrate", bitrate,
    ]


def _strip(line: str) -> str:
    return _ANSI.sub('', line).strip()


async def _run_async(args: list[str], on_line=None, cwd: str = None) -> tuple[int, str]:
    cmd = _base_args() + args
    logger.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd or DOWNLOAD_PATH,
    )
    lines = []
    async for raw in proc.stdout:
        line = _strip(raw.decode(errors="replace"))
        if line:
            lines.append(line)
            if on_line:
                on_line(line)
    await proc.wait()
    output = "\n".join(lines)
    logger.debug("spotdl output:\n%s", output)
    return proc.returncode, output


def _sync_delete_flag() -> list[str]:
    from app import database as db
    if db.get_setting("sync_delete_removed", "false") == "true":
        return []
    return ["--sync-without-deleting"]


async def sync_playlist(playlist_id: str, playlist_url: str, folder_name: str, on_line=None) -> tuple[int, str]:
    sync_file = os.path.join(DATA_PATH, "sync", f"{playlist_id}.spotdl")
    output_dir = os.path.join(DOWNLOAD_PATH, _safe_name(folder_name))
    os.makedirs(output_dir, exist_ok=True)
    no_delete = _sync_delete_flag()

    if os.path.exists(sync_file):
        args = ["sync", sync_file] + no_delete + ["--output", _output(output_dir)]
    else:
        args = ["sync", playlist_url, "--save-file", sync_file] + no_delete + ["--output", _output(output_dir)]

    return await _run_async(args, on_line=on_line)


async def sync_album(album_id: str, album_url: str, folder_name: str, on_line=None) -> tuple[int, str]:
    sync_file = os.path.join(DATA_PATH, "sync", f"album_{album_id}.spotdl")
    output_dir = os.path.join(DOWNLOAD_PATH, "Albums", _safe_name(folder_name))
    os.makedirs(output_dir, exist_ok=True)
    no_delete = _sync_delete_flag()

    if os.path.exists(sync_file):
        args = ["sync", sync_file] + no_delete + ["--output", _output(output_dir)]
    else:
        args = ["sync", album_url, "--save-file", sync_file] + no_delete + ["--output", _output(output_dir)]

    return await _run_async(args, on_line=on_line)


async def download_tracks(spotify_ids: list[str], output_dir: str, on_line=None) -> tuple[int, str]:
    if not spotify_ids:
        return 0, "No new tracks to download."
    os.makedirs(output_dir, exist_ok=True)
    uris = [f"https://open.spotify.com/track/{tid}" for tid in spotify_ids]
    return await _run_async(["download"] + uris + ["--output", _output(output_dir)], on_line=on_line)


async def download_single_track(spotify_url: str, output_dir: str, on_line=None) -> tuple[int, str]:
    os.makedirs(output_dir, exist_ok=True)
    return await _run_async(["download", spotify_url, "--output", _output(output_dir)], on_line=on_line)


def make_zip(target: dict) -> tuple[str, bytes]:
    """Return (filename, zip_bytes) of all downloaded tracks for a target."""
    folder = music_dir(target)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        if os.path.isdir(folder):
            for fname in sorted(os.listdir(folder)):
                fpath = os.path.join(folder, fname)
                if os.path.isfile(fpath):
                    zf.write(fpath, fname)
    return _safe_name(target["name"]) + ".zip", buf.getvalue()


def music_dir(target: dict) -> str:
    if target["type"] == "liked_songs":
        return os.path.join(DOWNLOAD_PATH, "Liked Songs")
    elif target["type"] == "album":
        return os.path.join(DOWNLOAD_PATH, "Albums", _safe_name(target["name"]))
    else:
        return os.path.join(DOWNLOAD_PATH, _safe_name(target["name"]))


def scan_tracks(target: dict) -> list[dict]:
    """Return list of {filename, title, artist} for all audio files in target's folder."""
    folder = music_dir(target)
    if not os.path.isdir(folder):
        return []
    exts = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".opus"}
    tracks = []
    for fname in sorted(os.listdir(folder)):
        if os.path.splitext(fname)[1].lower() in exts:
            stem = os.path.splitext(fname)[0]
            if " - " in stem:
                artist, title = stem.split(" - ", 1)
            else:
                artist, title = "", stem
            tracks.append({"filename": fname, "title": title.strip(), "artist": artist.strip()})
    return tracks


def _safe_name(name: str) -> str:
    keep = set(" abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_()")
    return "".join(c if c in keep else "_" for c in name).strip()
