import asyncio
import json
import os
import re
import subprocess
import tempfile
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


def classify_failure(output: str) -> str:
    """Turn spotdl's error output into a short, human reason a track didn't download."""
    o = (output or "").lower()
    if "sign in to confirm" in o or "not a bot" in o or "429" in o:
        return "YouTube is rate-limiting downloads right now — try again later"
    if "private video" in o or "video unavailable" in o or "video is unavailable" in o \
            or "has been removed" in o or "no longer available" in o or "account associated" in o:
        return "The matched YouTube video is unavailable"
    if "age" in o and "restrict" in o:
        return "YouTube video is age-restricted and can't be downloaded"
    if "drm" in o or "protected" in o:
        return "YouTube blocked the download (protected video)"
    if "yt-dlp download error" in o or "audioprovidererror" in o or "unable to download" in o \
            or "requested format is not available" in o or "unable to extract" in o:
        return "YouTube wouldn't allow the download"
    if "lookuperror" in o or "no results found" in o or "could not match" in o \
            or "no matching" in o or "found 0 results" in o:
        return "No match found on YouTube"
    return "Download failed"


def _redact(cmd: list[str]) -> list[str]:
    """Redact credential values so they don't reach logs (or anyone reading them)."""
    out, hide_next = [], False
    for tok in cmd:
        if hide_next:
            out.append("***"); hide_next = False
        elif tok in ("--client-secret", "--client-id"):
            out.append(tok); hide_next = True
        else:
            out.append(tok)
    return out


async def _run_async(args: list[str], on_line=None, cwd: str = None) -> tuple[int, str]:
    cmd = _base_args() + args
    logger.info("Running: %s", " ".join(_redact(cmd)))
    # PYTHONUNBUFFERED forces spotdl to flush each log line immediately. Without
    # it, Python block-buffers stdout when it's a pipe (not a TTY), so we'd see
    # no output — and fire no on_line progress callbacks — until the process exits.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd or DOWNLOAD_PATH,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
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


# Retained (not on the active sync path — albums now go through the pre-resolved
# meta/chunk path for failure-recovery parity with playlists). This is the only
# implementation of the "delete removed tracks" setting, which spotdl's `sync`
# supports but the `download` meta path does not; kept until that's ported over.
async def sync_album(album_id: str, album_url: str, folder_name: str, on_line=None) -> tuple[int, str]:
    sync_file = os.path.join(DATA_PATH, "sync", f"album_{album_id}.spotdl")
    output_dir = os.path.join(DOWNLOAD_PATH, "Albums", _safe_name(folder_name))
    os.makedirs(output_dir, exist_ok=True)
    no_delete = _sync_delete_flag()

    if os.path.exists(sync_file):
        args = ["sync", sync_file] + no_delete + ["--output", _output(output_dir)]
    else:
        args = ["sync", album_url, "--save-file", sync_file] + no_delete + ["--output", _output(output_dir)]

    return await _run_async(args + _NO_LYRICS, on_line=on_line)


# Trailing `--lyrics` with no providers disables lyrics fetching (3 providers
# spotdl hits per track that nearly always fail here, ~12s wasted each). Must be
# last so argparse's nargs='*' consumes nothing.
_NO_LYRICS = ["--lyrics"]


async def download_from_meta(songs: list[dict], output_dir: str, on_line=None) -> tuple[int, str]:
    """Download tracks from pre-resolved spotdl Song dicts via a temp .spotdl file.

    spotdl reads the metadata from the file and goes straight to YouTube — no
    per-track Spotify lookups, which is what the rate limiting throttles.
    """
    if not songs:
        return 0, "No new tracks to download."
    os.makedirs(output_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".spotdl", dir=DATA_PATH)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(songs, f)
        return await _run_async(
            ["download", path, "--output", _output(output_dir)] + _NO_LYRICS, on_line=on_line
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def download_single_track(spotify_url: str, output_dir: str, on_line=None) -> tuple[int, str]:
    os.makedirs(output_dir, exist_ok=True)
    return await _run_async(["download", spotify_url, "--output", _output(output_dir)] + _NO_LYRICS, on_line=on_line)


async def download_youtube(youtube_url: str, spotify_url: str, output_dir: str, on_line=None) -> tuple[int, str]:
    """Download a specific YouTube video, tagged with the Spotify track's metadata.

    Uses spotdl's `<youtube>|<spotify>` syntax — for tracks spotdl couldn't match
    on its own, the user pastes the right video and we fetch exactly that.
    """
    os.makedirs(output_dir, exist_ok=True)
    target = f"{youtube_url}|{spotify_url}"
    return await _run_async(["download", target, "--output", _output(output_dir)] + _NO_LYRICS, on_line=on_line)


def write_m3u(target: dict, tracks: list[dict]) -> str | None:
    """Write '<Name>.m3u8' inside the target's folder so external players
    (Jellyfin, Navidrome, VLC) see a real playlist in Spotify order, not just a
    folder of files. Entries are bare filenames (relative to the folder), which
    keeps the playlist valid from both the host and the container.
    """
    folder = music_dir(target)
    if not tracks or not os.path.isdir(folder):
        return None
    path = os.path.join(folder, _safe_name(target["name"]) + ".m3u8")
    lines = ["#EXTM3U"]
    for t in tracks:
        label = f"{t['artist']} - {t['title']}" if t.get("artist") else (t.get("title") or t["filename"])
        lines.append(f"#EXTINF:-1,{label}")
        lines.append(t["filename"])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def make_zip_file(target: dict) -> tuple[str | None, str | None]:
    """Write all of a target's tracks to a temp .zip on disk; return (name, path).

    Streams to disk instead of building the whole archive in memory — a large
    "Liked Songs" target could be many GB and OOM-kill the container otherwise.
    The caller is responsible for deleting the temp file after sending it.
    """
    folder = music_dir(target)
    if not os.path.isdir(folder):
        return None, None
    files = [f for f in sorted(os.listdir(folder)) if os.path.isfile(os.path.join(folder, f))]
    if not files:
        return None, None
    fd, path = tempfile.mkstemp(suffix=".zip", dir=DATA_PATH)
    os.close(fd)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for fname in files:
            zf.write(os.path.join(folder, fname), fname)
    return _safe_name(target["name"]) + ".zip", path


def dir_size(path: str = None) -> int:
    """Total bytes of audio files under a directory (defaults to the whole library)."""
    root = path or DOWNLOAD_PATH
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if os.path.splitext(f)[1].lower() in {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".opus"}:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    return total


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
