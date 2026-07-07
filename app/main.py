import asyncio
import datetime
import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from app import database as db
from app import scheduler as sched
from app.auth import is_authenticated, router as auth_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(__file__)

_RE_FEAT = re.compile(r'\s*[\(\[]\s*(?:feat|ft|featuring)[.\s][^\)\]]*[\)\]]', re.IGNORECASE)
_RE_NONWORD = re.compile(r'[^\w\s]')
_RE_WS = re.compile(r'\s+')


def _norm(s: str) -> str:
    s = _RE_FEAT.sub('', s)
    s = _RE_NONWORD.sub('', s.lower())
    return _RE_WS.sub(' ', s).strip()


def _merge_tracks(spotify_tracks: list[dict], local_tracks: list[dict]) -> list[dict]:
    """Annotate each Spotify track with whether it's been downloaded locally."""
    by_full = {}
    by_title = {}
    for lt in local_tracks:
        stem = f"{lt['artist']} - {lt['title']}" if lt["artist"] else lt["title"]
        by_full[_norm(stem)] = lt
        by_title.setdefault(_norm(lt["title"]), []).append(lt)

    result = []
    for st in spotify_tracks:
        local = by_full.get(_norm(f"{st['artist']} - {st['name']}"))
        if local is None:
            # Title-only fallback for artist-list mismatches (the filename holds
            # spotdl's joined {artists}; Spotify's side may be primary-only, e.g.
            # liked songs) — but the artists must still agree on whole tokens, so
            # a same-titled track by a different artist isn't shown as downloaded
            # and wired to play that other artist's file.
            st_tokens = set(_norm(st["artist"]).split())
            for lt in by_title.get(_norm(st["name"]), []):
                lt_tokens = set(_norm(lt["artist"]).split())
                if (st_tokens and lt_tokens and (st_tokens <= lt_tokens or lt_tokens <= st_tokens)) \
                        or (not st_tokens and not lt_tokens):
                    local = lt
                    break
        result.append({**st, "downloaded": local is not None, "filename": local["filename"] if local else None})
    return result


def _spotify_tracks_for(target: dict) -> list[dict]:
    """Fetch a target's tracks in Spotify order (playlist/album/liked)."""
    from app import spotify
    tid = target["id"]
    if target["type"] == "playlist":
        return spotify.get_playlist_tracks(tid)
    elif target["type"] == "album":
        return spotify.get_album_tracks(tid)
    raw = spotify.get_liked_track_ids()  # liked_songs
    return [{"id": t, "name": name, "artist": artist,
             "spotify_url": f"https://open.spotify.com/track/{t}", "duration_ms": 0}
            for t, name, artist in raw]


def _ordered_local_tracks(target: dict) -> list[dict]:
    """Downloaded tracks shaped for the player, in Spotify playlist order.

    Falls back to filesystem (alphabetical) order if the Spotify lookup fails,
    so the dashboard play button matches the detail page rather than whatever
    sorts first by filename.
    """
    from app.downloader import scan_tracks
    local = scan_tracks(target)
    try:
        spotify_tracks = _spotify_tracks_for(target)
    except Exception:
        logger.exception("Spotify order unavailable for %s; using filename order", target["id"])
        return local
    ordered = [{"filename": m["filename"], "title": m["name"], "artist": m["artist"]}
               for m in _merge_tracks(spotify_tracks, local) if m["downloaded"]]
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
    sched.load_all_jobs()
    sched.scheduler.start()
    logger.info("Localfy started")
    yield
    sched.scheduler.shutdown(wait=False)


app = FastAPI(title="Localfy", lifespan=lifespan)
app.include_router(auth_router)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


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
def dashboard(request: Request):
    if redir := _require_auth(request):
        return redir
    from app import spotify
    try:
        playlists = spotify.get_user_playlists()
        liked = spotify.get_liked_songs_info()
        albums = spotify.get_saved_albums()
        all_items = [liked] + playlists + albums
    except Exception as e:
        logger.exception("Failed to fetch Spotify data")
        return templates.TemplateResponse(request, "dashboard.html", {
            "error": str(e), "items": [], "db_targets": {}, "local_counts": {},
            "syncing": set(), "failed_counts": {}, "stats": db.get_library_stats(),
        })

    for item in all_items:
        db.upsert_target(
            id=item["id"], type=item["type"], name=item["name"],
            spotify_url=item["spotify_url"], image_url=item.get("image_url"),
        )

    db_targets = {t["id"]: t for t in db.get_all_targets()}

    # Include targets added by URL that aren't in the user's own library, so they
    # render with sync controls like the rest.
    live_ids = {item["id"] for item in all_items}
    for tid, t in db_targets.items():
        if tid in live_ids or t["type"] not in ("playlist", "album"):
            continue
        item = {"id": tid, "name": t["name"], "spotify_url": t.get("spotify_url"),
                "track_count": t.get("track_count"), "image_url": t.get("image_url"),
                "type": t["type"]}
        (playlists if t["type"] == "playlist" else albums).append(item)

    from app.downloader import scan_tracks
    local_counts = {}
    for tid, t in db_targets.items():
        if t.get("last_synced") or t.get("enabled"):
            try:
                local_counts[tid] = len(scan_tracks(t))
            except Exception:
                local_counts[tid] = 0

    playlists.sort(key=lambda x: _item_sort_key(x, db_targets, local_counts))
    albums.sort(key=lambda x: _item_sort_key(x, db_targets, local_counts))
    all_items = [liked] + playlists + albums

    # Targets whose sync is in progress right now, so the card can render its
    # "Syncing…" state on first paint instead of relying on a client-side poll
    # winning a race against page load.
    from app import progress as prog
    syncing = {item["id"] for item in all_items
               if prog.get_progress(item["id"]).get("status") == "running"}

    return templates.TemplateResponse(request, "dashboard.html", {
        "items": all_items, "db_targets": db_targets,
        "local_counts": local_counts, "syncing": syncing,
        "failed_counts": db.get_failed_counts(), "stats": db.get_library_stats(),
        "error": None,
    })


# ── Target detail ─────────────────────────────────────────────────────────────

@app.get("/targets/{target_id}", response_class=HTMLResponse)
def target_detail(target_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return RedirectResponse("/dashboard")

    from app.downloader import scan_tracks
    local_tracks = scan_tracks(target)

    try:
        spotify_tracks = _spotify_tracks_for(target)
    except Exception as exc:
        logger.exception("Failed to fetch Spotify tracks for %s", target_id)
        spotify_tracks = None
        spotify_error = str(exc)
    else:
        spotify_error = None

    if spotify_tracks is not None:
        tracks = _merge_tracks(spotify_tracks, local_tracks)
    else:
        tracks = [{"id": None, "name": lt["title"], "artist": lt["artist"],
                   "spotify_url": None, "duration_ms": 0,
                   "downloaded": True, "filename": lt["filename"]}
                  for lt in local_tracks]

    return templates.TemplateResponse(request, "detail.html", {
        "target": target, "tracks": tracks, "spotify_error": spotify_error,
        "failed": db.get_failed_tracks(target_id),
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
    """Filesystem-only list of downloaded files — cheap to poll during a sync so
    the detail page can flip rows to playable live (no Spotify call)."""
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    from app.downloader import scan_tracks
    return JSONResponse({"tracks": scan_tracks(target)})


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

@app.post("/targets/{target_id}/tracks/{track_id}/download")
async def download_track(target_id: str, track_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def _do():
        from app import progress as prog
        from app.downloader import download_single_track, music_dir
        prog.set_track(track_id, "running")
        try:
            url = f"https://open.spotify.com/track/{track_id}"
            code, _ = await download_single_track(url, music_dir(target))
            prog.set_track(track_id, "done" if code == 0 else "error")
        except Exception:
            prog.set_track(track_id, "error")

    asyncio.create_task(_do())
    return JSONResponse({"status": "started"})


@app.get("/targets/{target_id}/tracks/{track_id}/status")
def track_status(target_id: str, track_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    from app import progress as prog
    return JSONResponse({"status": prog.get_track(track_id), "message": prog.get_track_msg(track_id)})


# ── Toggle enable/disable ─────────────────────────────────────────────────────

@app.post("/targets/{target_id}/toggle")
def toggle_target(target_id: str, request: Request):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
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
    db.set_target_schedule(target_id, schedule)
    target = db.get_target(target_id)
    if target and target["enabled"]:
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
    asyncio.create_task(sched._run_sync(target_id, force=True))
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

_RE_SPOTIFY_URL = re.compile(r'open\.spotify\.com/(playlist|album)/([A-Za-z0-9]+)')


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
        info = spotify.get_playlist_info(sid) if kind == "playlist" else spotify.get_album_info(sid)
    except Exception as e:
        logger.exception("add_target failed")
        return JSONResponse({"error": f"Couldn't load that {kind}: {e}"}, status_code=400)
    db.upsert_target(id=info["id"], type=info["type"], name=info["name"],
                     spotify_url=info["spotify_url"], image_url=info.get("image_url"))
    db.set_target_track_count(info["id"], info.get("track_count") or 0)
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
    asyncio.create_task(sched._run_sync(info["id"], force=True))
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
    asyncio.create_task(sched._retry_failed(target_id))
    return JSONResponse({"status": "started", "total": len(failed)})


_RE_YOUTUBE = re.compile(r'(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)[\w-]+')


@app.post("/targets/{target_id}/tracks/{track_id}/youtube")
async def youtube_download_route(target_id: str, track_id: str, request: Request, url: str = Form(...)):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _RE_YOUTUBE.search(url or ""):
        return JSONResponse({"error": "That doesn't look like a YouTube link."}, status_code=400)

    async def _do():
        from app import progress as prog
        from app.downloader import download_youtube, music_dir, classify_failure
        prog.set_track(track_id, "running")
        try:
            spotify_url = f"https://open.spotify.com/track/{track_id}"
            code, output = await download_youtube(url, spotify_url, music_dir(target))
            # spotdl exits 0 even when the video is unavailable/age-restricted and
            # no file is written, so confirm the track is actually on disk before
            # marking it downloaded — otherwise it silently vanishes from the retry UI.
            f = next((x for x in db.get_failed_tracks(target_id) if x["spotify_id"] == track_id), None)
            song = {"name": f["name"], "artist": f["artist"], "artists": [f["artist"]]} if f else None
            landed = code == 0 and (song is None or sched.track_on_disk(music_dir(target), song))
            if landed:
                db.mark_downloaded(track_id, f["name"] if f else "", f["artist"] if f else "", target_id)
                db.remove_failed_track(target_id, track_id)
                prog.set_track(track_id, "done")
            else:
                reason = classify_failure(output) if code != 0 else "Download finished but no file appeared"
                db.set_failed_error(target_id, track_id, reason)
                prog.set_track(track_id, "error", reason)
        except Exception:
            logger.exception("YouTube download failed for %s", track_id)
            prog.set_track(track_id, "error", "Download failed")

    asyncio.create_task(_do())
    return JSONResponse({"status": "started"})


# ── Library stats / queue / search ────────────────────────────────────────────

@app.get("/api/stats")
def api_stats(request: Request):
    if redir := _require_auth(request):
        return redir
    from app.downloader import dir_size
    stats = db.get_library_stats()
    stats["disk_bytes"] = dir_size()
    return JSONResponse(stats)


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
    from app.downloader import scan_tracks
    rows = db.search_downloaded(q, limit=120)
    targets_cache: dict = {}
    folder_cache: dict = {}
    results = []
    for r in rows:
        tid = r["target_id"]
        if tid not in targets_cache:
            targets_cache[tid] = db.get_target(tid)
        target = targets_cache[tid]
        if not target:
            continue
        if tid not in folder_cache:
            # Keyed by title; the filename artist is spotdl's joined {artists}
            # while the DB stores the primary artist only, so exact-key matching
            # would silently drop every multi-artist track from search results.
            cache: dict = {}
            for t in scan_tracks(target):
                cache.setdefault(_norm(t["title"]), []).append(
                    (set(_norm(t["artist"]).split()), t["filename"]))
            folder_cache[tid] = cache
        r_tokens = set(_norm(r["artist"]).split())
        fn = next((f for toks, f in folder_cache[tid].get(_norm(r["name"]), [])
                   if r_tokens <= toks or (not toks and not r_tokens)), None)
        if not fn:
            continue
        results.append({"title": r["name"], "artist": r["artist"], "filename": fn,
                        "target_id": tid, "target_name": target["name"]})
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
        "sync_delete_removed": db.get_setting("sync_delete_removed", "false") == "true",
    })


@app.post("/settings/sync-delete-removed")
def update_sync_delete(request: Request, enabled: str = Form(...)):
    if redir := _require_auth(request):
        return redir
    db.set_setting("sync_delete_removed", "true" if enabled == "true" else "false")
    return JSONResponse({"ok": True, "sync_delete_removed": enabled == "true"})


@app.post("/settings/audio-format")
def update_audio_format(request: Request, audio_format: str = Form(...)):
    if redir := _require_auth(request):
        return redir
    if audio_format not in {"mp3", "flac", "ogg", "opus", "m4a", "wav"}:
        return JSONResponse({"error": "invalid format"}, status_code=400)
    db.set_setting("audio_format", audio_format)
    return JSONResponse({"ok": True})


@app.post("/settings/audio-bitrate")
def update_audio_bitrate(request: Request, audio_bitrate: str = Form(...)):
    if redir := _require_auth(request):
        return redir
    if audio_bitrate not in {"128k", "256k", "320k", "auto", "disable"}:
        return JSONResponse({"error": "invalid bitrate"}, status_code=400)
    db.set_setting("audio_bitrate", audio_bitrate)
    return JSONResponse({"ok": True})
