import asyncio
import datetime
import logging
import os
import re
import threading
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from app import database as db
from app import matching
from app import scheduler as sched
from app import status
from app.auth import is_authenticated
from app.auth import router as auth_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(__file__)

# Pages render from what's in the DB and refresh it from Spotify in the
# background once it's older than this (stale-while-revalidate), so a page view
# never waits on Spotify except the very first time.
LIBRARY_TTL = datetime.timedelta(minutes=5)
TRACKS_TTL = datetime.timedelta(minutes=15)

_RE_SPOTIFY_ID = re.compile(r'[A-Za-z0-9]{22}')


def _stale(ts: str | None, ttl: datetime.timedelta) -> bool:
    dt = status.parse_ts(ts)
    return dt is None or datetime.datetime.now(datetime.timezone.utc) - dt > ttl


_refreshing: set[str] = set()
_refresh_lock = threading.Lock()


def _refresh_once(key: str, fn, *args):
    """Run a background refresh unless one for the same key is already running."""
    with _refresh_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)
    try:
        fn(*args)
    except Exception:
        logger.exception("Background refresh %s failed", key)
    finally:
        with _refresh_lock:
            _refreshing.discard(key)


def _refresh_library():
    """Pull the user's liked songs / playlists / saved albums into sync_targets."""
    from app import spotify
    items = [spotify.get_liked_songs_info()] + spotify.get_user_playlists() + spotify.get_saved_albums()
    for item in items:
        db.upsert_target(id=item["id"], type=item["type"], name=item["name"],
                         spotify_url=item["spotify_url"], image_url=item.get("image_url"),
                         track_count=item.get("track_count"))
    db.set_setting("library_refreshed_at", datetime.datetime.now(datetime.timezone.utc).isoformat())


def _refresh_tracks(target: dict):
    """Re-fetch a target's Spotify track list into its snapshot."""
    from app import spotify
    db.replace_target_tracks(target["id"], spotify.get_target_song_meta(target))


def _track_rows(target: dict, background_tasks: BackgroundTasks
                ) -> tuple[list[dict] | None, str | None, int | None]:
    """(snapshot rows, Spotify error, its HTTP status) for a target. Fetches live
    only when there's no snapshot yet; otherwise serves it and refreshes in the
    background if stale."""
    if target["type"] == "singles":
        return None, None, None   # local-only collection by design — no Spotify side
    if target.get("tracks_fetched_at") is None:
        try:
            _refresh_tracks(target)
        except Exception as exc:
            logger.exception("Failed to fetch Spotify tracks for %s", target["id"])
            return None, str(exc), getattr(exc, "http_status", None)
    elif _stale(target["tracks_fetched_at"], TRACKS_TTL):
        background_tasks.add_task(_refresh_once, f"tracks:{target['id']}", _refresh_tracks, target)
    return db.get_target_tracks(target["id"]), None, None


def _merge_tracks(rows: list[dict], local_tracks: list[dict]) -> list[dict]:
    """Annotate each snapshot row with the local file it was downloaded to, if any."""
    idx = matching.build_index([lt["filename"] for lt in local_tracks])
    out = []
    for r in rows:
        fn = matching.find(idx, r["name"], r["primary_artist"], [r["artist"]])
        out.append({"id": r["spotify_id"], "name": r["name"], "artist": r["artist"],
                    "downloaded": fn is not None, "filename": fn})
    return out


def _downloaded_count(target: dict, rows: list[dict]) -> int:
    """How many of a target's tracks are on disk. Counted against its snapshot
    rows when there are some, so files for tracks since removed from the
    playlist don't push it past the total ("187 of 176")."""
    from app.downloader import music_dir
    files = matching.audio_files(music_dir(target))
    if not rows:
        # No track list yet (not synced or opened since upgrading): a file
        # count is the best guess, but never claim more than the total.
        return min(len(files), target.get("track_count") or len(files))
    idx = matching.build_index(files)
    return sum(1 for r in rows if matching.find(idx, r["name"], r["primary_artist"], [r["artist"]]))


def _ordered_local_tracks(target: dict) -> list[dict]:
    """Downloaded tracks shaped for the player, in Spotify order (from the
    snapshot — no Spotify call). Falls back to filename order without one."""
    from app.downloader import scan_tracks
    local = scan_tracks(target)
    rows = db.get_target_tracks(target["id"])
    ordered = [{"filename": m["filename"], "title": m["name"], "artist": m["artist"]}
               for m in _merge_tracks(rows, local) if m["downloaded"]]
    return ordered or local


def _item_sort_key(item: dict, db_targets: dict, local_counts: dict | None = None) -> tuple:
    from app import progress as prog
    t = db_targets.get(item["id"], {})
    enabled = bool(t.get("enabled", 0))
    last_synced = t.get("last_synced")
    syncing = prog.get_progress(item["id"]).get("status") == "running"
    local = (local_counts or {}).get(item["id"], 0)
    spotify_count = t.get("track_count") or 0

    def ts(s):
        if not s:
            return 0.0
        try:
            return datetime.datetime.fromisoformat(s).timestamp()
        except ValueError:
            return 0.0

    is_partial = local > 0 and spotify_count > 0 and local < spotify_count

    bucket = (
        0 if syncing else
        1 if (enabled and last_synced and not is_partial) else
        2 if (enabled and is_partial) else
        3 if enabled else
        4 if is_partial else
        5
    )
    return (bucket, -ts(last_synced), (item.get("name") or "").lower())


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Progress is in-memory and lost on restart. Mark syncs orphaned by a crash/
    # restart as interrupted, and seed their progress so the UI shows that instead
    # of a silently-idle card (the next sync overwrites it with fresh progress).
    from app import progress as prog
    for tid in db.reconcile_running_logs():
        prog.set_progress(tid, {"status": "interrupted",
                                "message": "A sync was interrupted by a restart"})
    if sched.DISABLED:
        logger.warning("Scheduler disabled (LOCALFY_DISABLE_SCHEDULER); only manual syncs will run")
    else:
        sched.load_all_jobs()
        sched.scheduler.start()
    logger.info("Localfy started")
    yield
    if sched.scheduler.running:
        sched.scheduler.shutdown(wait=False)


app = FastAPI(title="Localfy", lifespan=lifespan)
app.include_router(auth_router)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _require_auth(request: Request):
    if not is_authenticated():
        return RedirectResponse("/")
    return None


# ── Landing ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request, error: str = None):
    if is_authenticated():
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "index.html", {"error": error})


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, background_tasks: BackgroundTasks, refresh: bool = False):
    if redir := _require_auth(request):
        return redir
    error = None
    refreshed_at = db.get_setting("library_refreshed_at")
    if refresh or refreshed_at is None:
        try:
            _refresh_library()
        except Exception as e:
            logger.exception("Failed to fetch Spotify data")
            error = str(e)
    elif _stale(refreshed_at, LIBRARY_TTL):
        background_tasks.add_task(_refresh_once, "library", _refresh_library)

    # Everything renders from the DB: the user's library plus any targets added
    # by URL (or since unfollowed), so they keep their sync controls.
    db_targets = {t["id"]: t for t in db.get_all_targets()}
    if error and any(t["type"] in ("playlist", "album", "liked_songs") for t in db_targets.values()):
        error = f"Couldn't refresh from Spotify ({error}). Showing your library as of the last refresh."

    local_counts = {}
    snapshots = db.get_all_target_tracks()
    for tid, t in db_targets.items():
        if t.get("last_synced") or t.get("enabled"):
            try:
                local_counts[tid] = _downloaded_count(t, snapshots.get(tid, []))
            except Exception:
                logger.exception("Couldn't count downloads for %s", tid)
                local_counts[tid] = 0

    def of_type(kind):
        return sorted((t for t in db_targets.values() if t["type"] == kind),
                      key=lambda x: _item_sort_key(x, db_targets, local_counts))

    all_items = of_type("liked_songs") + of_type("playlist") + of_type("album")

    # Targets whose sync is in progress right now, so the card can render its
    # "Syncing…" state on first paint instead of relying on a client-side poll
    # winning a race against page load.
    from app import progress as prog
    syncing = {item["id"] for item in all_items
               if prog.get_progress(item["id"]).get("status") == "running"}

    # "Synced" leads the page: anything auto-syncing, syncing now, or with
    # files on disk. The rest of the Spotify library sits in a collapsed section.
    def is_synced(t):
        return bool(t.get("enabled")) or local_counts.get(t["id"], 0) > 0 or t["id"] in syncing

    now = datetime.datetime.now(datetime.timezone.utc)
    sync_status = {t["id"]: status.sync_status(t, sched.interval_for(t.get("schedule") or "daily"), now)
                   for t in all_items if is_synced(t)}

    return templates.TemplateResponse(request, "dashboard.html", {
        "synced_items": [t for t in all_items if is_synced(t)],
        "library_items": [t for t in all_items if not is_synced(t)],
        "local_counts": local_counts, "syncing": syncing, "sync_status": sync_status,
        "overdue_count": sum(1 for st in sync_status.values() if st["overdue"]),
        "failed_counts": db.get_failed_counts(), "stats": db.get_library_stats(),
        "error": error,
    })


# ── Target detail ─────────────────────────────────────────────────────────────

@app.get("/targets/{target_id}", response_class=HTMLResponse)
def target_detail(target_id: str, request: Request, background_tasks: BackgroundTasks):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return RedirectResponse("/dashboard")

    from app.downloader import scan_tracks
    local_tracks = scan_tracks(target)
    rows, spotify_error, spotify_status = _track_rows(target, background_tasks)

    if rows is not None:
        tracks = _merge_tracks(rows, local_tracks)
    else:
        tracks = [{"id": None, "name": lt["title"], "artist": lt["artist"],
                   "downloaded": True, "filename": lt["filename"]}
                  for lt in local_tracks]

    # Spotify refuses to share some playlists' tracks with third-party apps
    # (Spotify-made mixes and editorial lists). A sync makes the same call, so
    # it can't help — the page says so instead of offering a sync that fails.
    blocked = spotify_status in (403, 404)

    last = status.parse_ts(target.get("last_synced"))
    return templates.TemplateResponse(request, "detail.html", {
        "target": target, "tracks": tracks, "spotify_error": spotify_error,
        "last_synced_ago": status.ago(last, datetime.datetime.now(datetime.timezone.utc)) if last else None,
        "spotify_blocked": blocked, "failed": db.get_failed_tracks(target_id),
    })


# ── Activity ──────────────────────────────────────────────────────────────────

@app.get("/activity", response_class=HTMLResponse)
def activity(request: Request):
    if redir := _require_auth(request):
        return redir
    return templates.TemplateResponse(request, "activity.html", {
        "logs": db.get_all_recent_logs(),
    })


# ── Stream audio ───────────────────────────────────────────────────────────────

@app.get("/targets/{target_id}/local-tracks")
def local_tracks_json(target_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"tracks": _ordered_local_tracks(target)})


@app.get("/targets/{target_id}/scan")
def scan_local(target_id: str, request: Request):
    """Downloaded files plus which snapshot tracks they match — cheap to poll
    during a sync so the detail page can flip rows to playable live."""
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    from app.downloader import scan_tracks
    files = scan_tracks(target)
    matches = {m["id"]: m["filename"] for m in _merge_tracks(db.get_target_tracks(target_id), files)
               if m["downloaded"]}
    return JSONResponse({"tracks": files, "matches": matches})


@app.get("/stream/{target_id}/{filename:path}")
def stream_audio(target_id: str, filename: str, request: Request):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    from app.downloader import music_dir
    realfolder = os.path.realpath(music_dir(target))
    filepath = os.path.realpath(os.path.join(realfolder, filename))
    # commonpath avoids the startswith prefix bug (e.g. /music/Foo vs /music/Foo2).
    if os.path.commonpath([realfolder, filepath]) != realfolder:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not os.path.isfile(filepath):
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = os.path.splitext(filename)[1].lower()
    media_types = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg",
                   ".m4a": "audio/mp4", ".wav": "audio/wav", ".opus": "audio/opus"}
    return FileResponse(filepath, media_type=media_types.get(ext, "audio/mpeg"))


# ── Download ZIP ───────────────────────────────────────────────────────────────

@app.get("/targets/{target_id}/download-zip")
def download_zip(target_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    from app.downloader import make_zip_file
    zip_name, zip_path = make_zip_file(target)
    if not zip_path:
        return JSONResponse({"error": "no downloaded tracks"}, status_code=404)
    # Stream from disk and delete the temp file once the response is sent.
    return FileResponse(
        zip_path, media_type="application/zip", filename=zip_name,
        background=BackgroundTask(os.remove, zip_path),
    )


# ── Single track download ──────────────────────────────────────────────────────

def _spawn_download(target: dict, track_id: str, song: dict | None = None):
    """Download one track into a target's folder in the background, reporting
    through the per-track status the UI polls. Fetches the track's metadata
    first when it isn't given."""
    async def _do():
        from app import progress as prog
        from app import spotify
        prog.set_track(track_id, "running", "")
        try:
            meta = song or await asyncio.to_thread(spotify.get_track_song_meta, track_id)
            landed, reason = await sched.download_one(target, meta)
            prog.set_track(track_id, "done" if landed else "error", reason)
        except Exception:
            logger.exception("Track download failed for %s", track_id)
            prog.set_track(track_id, "error", "Download failed")

    sched.spawn(_do())


@app.post("/targets/{target_id}/tracks/{track_id}/download")
async def download_track(target_id: str, track_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _RE_SPOTIFY_ID.fullmatch(track_id):
        return JSONResponse({"error": "invalid track id"}, status_code=400)
    _spawn_download(target, track_id)
    return JSONResponse({"status": "started"})


@app.get("/targets/{target_id}/tracks/{track_id}/status")
def track_status(target_id: str, track_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    from app import progress as prog
    return JSONResponse({"status": prog.get_track(track_id), "message": prog.get_track_msg(track_id)})


# ── Toggle enable/disable ─────────────────────────────────────────────────────

@app.post("/targets/{target_id}/toggle")
def toggle_target(target_id: str, request: Request, enabled: str | None = Form(None)):
    """Set tracking on/off. Pass enabled=true|false to set it explicitly
    (idempotent — safe to repeat); without it, flips the current state."""
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    if enabled in ("true", "false"):
        new_enabled = enabled == "true"
    else:
        new_enabled = not bool(target["enabled"])
    db.set_target_enabled(target_id, new_enabled)
    if new_enabled:
        sched.register_target(target_id, target["schedule"])
    else:
        sched.unregister_target(target_id)
    return JSONResponse({"enabled": new_enabled})


# ── Change schedule ───────────────────────────────────────────────────────────

@app.post("/targets/{target_id}/schedule")
def update_schedule(target_id: str, request: Request, schedule: str = Form(...)):
    if redir := _require_auth(request):
        return redir
    if schedule not in ("daily", "weekly", "monthly"):
        return JSONResponse({"error": "invalid schedule"}, status_code=400)
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.set_target_schedule(target_id, schedule)
    if target["enabled"]:
        sched.register_target(target_id, schedule)
    return JSONResponse({"ok": True, "schedule": schedule})


# ── Manual sync ───────────────────────────────────────────────────────────────

@app.post("/targets/{target_id}/sync")
async def manual_sync(target_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    if sched.is_syncing(target_id):
        return JSONResponse({"status": "already running"})
    # Mark "running" synchronously so the status is authoritative the moment this
    # request returns — otherwise a page that navigates here right after triggering
    # a sync can poll /progress before the background task has registered it.
    from app import progress as prog
    prog.set_progress(target_id, {
        "status": "running", "downloaded": 0, "skipped": 0,
        "total": target.get("track_count") or 0,
    })
    sched.spawn(sched._run_sync(target_id, force=True))
    return JSONResponse({"status": "started"})


# ── Sync progress ──────────────────────────────────────────────────────────────

@app.get("/targets/{target_id}/progress")
def get_progress(target_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    from app import progress as prog
    return JSONResponse(prog.get_progress(target_id))


# ── Sync logs ─────────────────────────────────────────────────────────────────

@app.get("/targets/{target_id}/logs")
def get_logs(target_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    logs = db.get_recent_logs(target_id)
    return JSONResponse(logs)


# ── Add target by URL ─────────────────────────────────────────────────────────

# Also accepts localized share links (open.spotify.com/intl-de/playlist/…).
_RE_SPOTIFY_URL = re.compile(r'open\.spotify\.com/(?:intl-[\w-]+/)?(playlist|album)/([A-Za-z0-9]+)')


@app.post("/targets/add")
async def add_target(request: Request, url: str = Form(...)):
    if redir := _require_auth(request):
        return redir
    m = _RE_SPOTIFY_URL.search(url or "")
    if not m:
        return JSONResponse({"error": "Paste a Spotify playlist or album link."}, status_code=400)
    kind, sid = m.group(1), m.group(2)
    from app import spotify
    try:
        fetch = spotify.get_playlist_info if kind == "playlist" else spotify.get_album_info
        info = await asyncio.to_thread(fetch, sid)
    except Exception as e:
        logger.exception("add_target failed")
        return JSONResponse({"error": f"Couldn't load that {kind}: {e}"}, status_code=400)
    db.upsert_target(id=info["id"], type=info["type"], name=info["name"],
                     spotify_url=info["spotify_url"], image_url=info.get("image_url"),
                     track_count=info.get("track_count") or 0)
    db.set_target_enabled(info["id"], True)
    sched.register_target(info["id"], db.get_target(info["id"])["schedule"])
    # register_target seeds next_run at now+interval for a target with no prior
    # sync, so nothing would download for up to a full interval. Kick an initial
    # sync now; set progress synchronously so the returned card shows "Syncing…".
    from app import progress as prog
    prog.set_progress(info["id"], {
        "status": "running", "downloaded": 0, "skipped": 0,
        "total": info.get("track_count") or 0,
    })
    sched.spawn(sched._run_sync(info["id"], force=True))
    return JSONResponse({"ok": True, "id": info["id"], "name": info["name"], "type": info["type"]})


# ── Failed tracks ─────────────────────────────────────────────────────────────

@app.get("/targets/{target_id}/failed")
def failed_tracks(target_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    return JSONResponse({"failed": db.get_failed_tracks(target_id)})


@app.post("/targets/{target_id}/retry-failed")
async def retry_failed_route(target_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    if sched.is_syncing(target_id):
        return JSONResponse({"status": "already running"})
    failed = db.get_failed_tracks(target_id)
    if not failed:
        return JSONResponse({"status": "none"})
    from app import progress as prog
    prog.set_progress(target_id, {"status": "running", "mode": "retry", "current": None,
                                  "downloaded": 0, "done": 0, "total": len(failed)})
    sched.spawn(sched._retry_failed(target_id))
    return JSONResponse({"status": "started", "total": len(failed)})


# The whole input must be a YouTube video link. Only the 11-char video id is
# kept and a canonical URL rebuilt from it, so nothing else the user typed
# (extra text, a leading "-", a "|") ever reaches spotdl's argv.
_RE_YOUTUBE = re.compile(
    r'(?:https?://)?(?:(?:www|m|music)\.)?'
    r'(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/)|youtu\.be/)'
    r'([\w-]{11})(?:[?&#]\S*)?'
)


def youtube_video_url(url: str | None) -> str | None:
    m = _RE_YOUTUBE.fullmatch((url or "").strip())
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


@app.post("/targets/{target_id}/tracks/{track_id}/youtube")
async def youtube_download_route(target_id: str, track_id: str, request: Request, url: str = Form(...)):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _RE_SPOTIFY_ID.fullmatch(track_id):
        return JSONResponse({"error": "invalid track id"}, status_code=400)
    video_url = youtube_video_url(url)
    if not video_url:
        return JSONResponse({"error": "That doesn't look like a YouTube video link."}, status_code=400)

    async def _do():
        from app import progress as prog
        from app.downloader import classify_failure, download_youtube, music_dir
        prog.set_track(track_id, "running", "")
        try:
            folder = music_dir(target)
            before = set(matching.audio_files(folder))
            spotify_url = f"https://open.spotify.com/track/{track_id}"
            code, output = await download_youtube(video_url, spotify_url, folder)
            # spotdl exits 0 even when the video is unavailable/age-restricted and
            # no file is written, so confirm the track is actually on disk before
            # marking it downloaded — otherwise it silently vanishes from the retry UI.
            # Name/artist come from the failed-track row, else the track list;
            # with neither, a new audio file appearing is the evidence.
            f = next((x for x in db.get_failed_tracks(target_id) if x["spotify_id"] == track_id), None)
            if f is None:
                f = next(({"name": r["name"], "artist": r["primary_artist"]}
                          for r in db.get_target_tracks(target_id) if r["spotify_id"] == track_id), None)
            if f:
                song = {"song_id": track_id, "name": f["name"], "artist": f["artist"], "artists": [f["artist"]]}
                landed = sched.track_on_disk(folder, song)
            else:
                song = {"song_id": track_id, "name": "", "artist": ""}
                landed = bool(set(matching.audio_files(folder)) - before)
            if landed:
                db.mark_downloaded_many(target_id, [song])
                db.remove_failed_track(target_id, track_id)
                prog.set_track(track_id, "done")
            else:
                # spotdl exits 0 even on failures; read the output either way.
                reason = classify_failure(output)
                if reason == "Download failed" and code == 0:
                    reason = "Download finished but no file appeared"
                db.set_failed_error(target_id, track_id, reason)
                prog.set_track(track_id, "error", reason)
        except Exception:
            logger.exception("YouTube download failed for %s", track_id)
            prog.set_track(track_id, "error", "Download failed")

    sched.spawn(_do())
    return JSONResponse({"status": "started"})


# ── One-off single downloads (nav search) ─────────────────────────────────────

@app.get("/api/spotify-search")
def api_spotify_search(request: Request, q: str = ""):
    if redir := _require_auth(request):
        return redir
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    from app import spotify
    try:
        return JSONResponse({"results": spotify.search_tracks(q)})
    except Exception:
        logger.exception("Spotify search failed")
        return JSONResponse({"results": []})


@app.post("/api/singles/download")
async def download_single(request: Request, track_id: str = Form(...)):
    """Download one arbitrary track (not part of any synced target) into
    /music/Singles, backed by a 'singles' pseudo-target so the file is
    searchable, streamable, and listed at /targets/singles."""
    if redir := _require_auth(request):
        return redir
    if not _RE_SPOTIFY_ID.fullmatch(track_id):
        return JSONResponse({"error": "invalid track id"}, status_code=400)
    from app import spotify
    try:
        song = await asyncio.to_thread(spotify.get_track_song_meta, track_id)
    except Exception as e:
        return JSONResponse({"error": f"Couldn't load that track: {e}"}, status_code=400)
    db.upsert_target(id="singles", type="singles", name="Singles",
                     spotify_url=None, image_url=None)
    _spawn_download(db.get_target("singles"), track_id, song)
    return JSONResponse({"status": "started"})


# ── Library stats / queue / search ────────────────────────────────────────────

@app.get("/api/stats")
def api_stats(request: Request):
    if redir := _require_auth(request):
        return redir
    # The page already renders the DB stats; this only adds the (slow) disk size.
    from app.downloader import dir_size_cached
    return JSONResponse({"disk_bytes": dir_size_cached()})


@app.get("/api/queue")
def api_queue(request: Request):
    if redir := _require_auth(request):
        return redir
    from app import progress as prog
    active = []
    for tid in sched.active_syncs():
        t = db.get_target(tid)
        active.append({"id": tid, "name": t["name"] if t else tid,
                       "progress": prog.get_progress(tid)})
    return JSONResponse({"active": active})


@app.get("/api/search")
def api_search(request: Request, q: str = ""):
    if redir := _require_auth(request):
        return redir
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    from app.downloader import music_dir
    rows = db.search_downloaded(q, limit=120)
    targets_cache: dict = {}
    index_cache: dict = {}
    seen: set[str] = set()
    results = []
    for r in rows:
        # A track in several playlists has a row per playlist; show it once.
        if r["spotify_id"] in seen:
            continue
        tid = r["target_id"]
        if tid not in targets_cache:
            targets_cache[tid] = db.get_target(tid)
        target = targets_cache[tid]
        if not target:
            continue
        if tid not in index_cache:
            index_cache[tid] = matching.index_dir(music_dir(target))
        fn = matching.find(index_cache[tid], r["name"], r["artist"])
        if not fn:
            continue
        seen.add(r["spotify_id"])
        results.append({"title": r["name"], "artist": r["artist"], "filename": fn,
                        "target_id": tid, "target_name": target["name"],
                        "image_url": target.get("image_url")})
        if len(results) >= 50:
            break
    return JSONResponse({"results": results})


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    if redir := _require_auth(request):
        return redir
    return templates.TemplateResponse(request, "settings.html", {
        "download_path": os.environ.get("DOWNLOAD_PATH", "/music"),
        "audio_format": db.get_setting("audio_format") or os.environ.get("AUDIO_FORMAT", "mp3"),
        "audio_bitrate": db.get_setting("audio_bitrate") or os.environ.get("AUDIO_BITRATE", "320k"),
    })


@app.post("/settings/audio-format")
def update_audio_format(request: Request, audio_format: str = Form(...)):
    if redir := _require_auth(request):
        return redir
    if audio_format not in {"mp3", "flac", "ogg", "opus", "m4a", "wav"}:
        return JSONResponse({"error": "invalid format"}, status_code=400)
    db.set_setting("audio_format", audio_format)
    return JSONResponse({"ok": True})


@app.get("/api/storage-report")
def api_storage_report(request: Request):
    # sync def → FastAPI's threadpool, so the os.walk doesn't block the loop
    if redir := _require_auth(request):
        return redir
    from app.downloader import storage_report
    return JSONResponse(storage_report())


@app.post("/settings/dedupe")
def run_dedupe(request: Request):
    if redir := _require_auth(request):
        return redir
    from app.downloader import dedupe_hardlinks
    result = dedupe_hardlinks()
    if result.get("unsupported"):
        return JSONResponse({**result, "error": "This music folder's filesystem doesn't support hardlinks."},
                            status_code=409)
    return JSONResponse(result)


@app.post("/settings/audio-bitrate")
def update_audio_bitrate(request: Request, audio_bitrate: str = Form(...)):
    if redir := _require_auth(request):
        return redir
    if audio_bitrate not in {"128k", "256k", "320k", "auto", "disable"}:
        return JSONResponse({"error": "invalid bitrate"}, status_code=400)
    db.set_setting("audio_bitrate", audio_bitrate)
    return JSONResponse({"ok": True})
