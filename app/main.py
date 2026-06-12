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
        by_title.setdefault(_norm(lt["title"]), lt)

    result = []
    for st in spotify_tracks:
        full_key = _norm(f"{st['artist']} - {st['name']}")
        title_key = _norm(st["name"])
        local = by_full.get(full_key) or by_title.get(title_key)
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
        })

    for item in all_items:
        db.upsert_target(
            id=item["id"], type=item["type"], name=item["name"],
            spotify_url=item["spotify_url"], image_url=item.get("image_url"),
        )

    db_targets = {t["id"]: t for t in db.get_all_targets()}

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

    return templates.TemplateResponse(request, "dashboard.html", {
        "items": all_items, "db_targets": db_targets,
        "local_counts": local_counts, "error": None,
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


@app.get("/stream/{target_id}/{filename:path}")
def stream_audio(target_id: str, filename: str, request: Request):
    if redir := _require_auth(request):
        return redir
    target = db.get_target(target_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    from app.downloader import music_dir
    folder = music_dir(target)
    filepath = os.path.realpath(os.path.join(folder, filename))
    if not filepath.startswith(os.path.realpath(folder)):
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
    from app.downloader import make_zip
    zip_name, zip_bytes = make_zip(target)
    if not zip_bytes:
        return JSONResponse({"error": "no downloaded tracks"}, status_code=404)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
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
    return JSONResponse({"status": prog.get_track(track_id)})


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


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    if redir := _require_auth(request):
        return redir
    return templates.TemplateResponse(request, "settings.html", {
        "download_path": os.environ.get("DOWNLOAD_PATH", "/music"),
        "audio_format": db.get_setting("audio_format") or os.environ.get("AUDIO_FORMAT", "mp3"),
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
