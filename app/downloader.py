import asyncio
import filecmp
import functools
import json
import logging
import os
import re
import tempfile
import time
import zipfile

from app.matching import AUDIO_EXTS, audio_files, parse_filename

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
            out.append("***")
            hide_next = False
        elif tok in ("--client-secret", "--client-id"):
            out.append(tok)
            hide_next = True
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
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    return total


_size_cache: tuple[float, int] | None = None


def dir_size_cached(max_age: float = 60.0) -> int:
    """dir_size() of the whole library, recomputed at most once a minute: the
    dashboard asks on every render and a big library is a long walk."""
    global _size_cache
    now = time.monotonic()
    if _size_cache is None or now - _size_cache[0] > max_age:
        _size_cache = (now, dir_size())
    return _size_cache[1]


def hardlinks_supported(path: str | None = None) -> bool:
    """Whether the music folder's filesystem can hardlink. exFAT and FAT can't,
    and they're common on external drives shared with Windows or macOS."""
    return _probe_hardlinks(os.path.realpath(path or DOWNLOAD_PATH))


@functools.lru_cache(maxsize=8)
def _probe_hardlinks(root: str) -> bool:
    # There's no portable "what filesystem is this" call, so try it: link a
    # scratch file and clean up. The answer can't change while the app runs.
    try:
        fd, src = tempfile.mkstemp(prefix=".lfy-linktest-", dir=root)
    except OSError:
        logger.warning("Couldn't probe hardlink support in %s", root, exc_info=True)
        return False
    os.close(fd)
    dst = src + ".link"
    try:
        os.link(src, dst)
        return True
    except OSError:
        return False
    finally:
        for p in (dst, src):
            try:
                os.remove(p)
            except OSError:
                pass


def _duplicate_groups() -> list[tuple[int, list[tuple[str, int]]]]:
    """Audio files stored more than once: groups of identical (filename, size)
    across folders, as (size, [(path, inode)]). Inode-aware, so copies that are
    already hardlinked together don't count as waste."""
    groups: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for dirpath, _dirs, files in os.walk(DOWNLOAD_PATH):
        for f in files:
            if os.path.splitext(f)[1].lower() not in AUDIO_EXTS:
                continue
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            groups.setdefault((f, st.st_size), []).append((p, st.st_ino))
    out = []
    for (_fname, size), entries in sorted(groups.items()):
        if len(entries) > 1 and len({ino for _, ino in entries}) > 1:
            out.append((size, entries))
    return out


def storage_report() -> dict:
    """Library size plus how much space duplicate downloads are wasting (the
    same track downloaded into several playlist folders)."""
    dup_files, wasted, top = 0, 0, []
    for size, entries in _duplicate_groups():
        extra = len({ino for _, ino in entries}) - 1
        dup_files += extra
        wasted += size * extra
        top.append({
            "filename": os.path.basename(entries[0][0]),
            "size": size,
            "copies": len(entries),
            "folders": sorted({os.path.relpath(os.path.dirname(p), DOWNLOAD_PATH) or "."
                               for p, _ in entries}),
        })
    top.sort(key=lambda g: -(g["size"] * (g["copies"] - 1)))
    return {"total_bytes": dir_size(), "dup_files": dup_files,
            "wasted_bytes": wasted, "group_count": len(top), "top": top[:8],
            # Only probed when there's something to dedupe; None = not checked.
            "hardlinks": hardlinks_supported() if top else None}


def dedupe_hardlinks() -> dict:
    """Replace duplicate copies with hardlinks to one canonical file.

    Every playlist folder keeps its entry, but identical audio is stored once.
    Links via a temp name + os.replace so a failure (e.g. copies on different
    filesystems) leaves the original file untouched.
    """
    if not hardlinks_supported():
        # Every link would fail; say so once instead of N times.
        return {"linked": 0, "saved_bytes": 0, "failed": 0, "unsupported": True}
    linked, saved, failed = 0, 0, 0
    for size, entries in _duplicate_groups():
        canonical, can_ino = entries[0]
        for p, ino in entries[1:]:
            if ino == can_ino:
                continue
            # Same name and size is only a candidate; never replace a file with
            # a link to different audio.
            try:
                identical = filecmp.cmp(canonical, p, shallow=False)
            except OSError:
                identical = False
            if not identical:
                continue
            tmp = p + ".lfy-dedupe-tmp"
            try:
                os.link(canonical, tmp)
                os.replace(tmp, p)
                linked += 1
                saved += size
            except OSError:
                failed += 1
                logger.warning("Couldn't hardlink %s -> %s", p, canonical)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return {"linked": linked, "saved_bytes": saved, "failed": failed}


def music_dir(target: dict) -> str:
    if target["type"] == "liked_songs":
        return os.path.join(DOWNLOAD_PATH, "Liked Songs")
    elif target["type"] == "album":
        return os.path.join(DOWNLOAD_PATH, "Albums", _safe_name(target["name"]))
    elif target["type"] == "singles":
        return os.path.join(DOWNLOAD_PATH, "Singles")
    else:
        return os.path.join(DOWNLOAD_PATH, _safe_name(target["name"]))


def scan_tracks(target: dict) -> list[dict]:
    """Return list of {filename, title, artist} for all audio files in target's folder."""
    tracks = []
    for fname in audio_files(music_dir(target)):
        artist, title = parse_filename(fname)
        tracks.append({"filename": fname, "title": title, "artist": artist})
    return tracks


def _safe_name(name: str) -> str:
    keep = set(" abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_()")
    return "".join(c if c in keep else "_" for c in name).strip()
