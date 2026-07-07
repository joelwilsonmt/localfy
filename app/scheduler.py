import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

_INTERVALS = {
    "daily": dict(days=1),
    "weekly": dict(weeks=1),
    "monthly": dict(days=30),
}

# ── Concurrency control ────────────────────────────────────────────────────────
# Targets currently being synced, so a duplicate (double-click, or a scheduled
# fire landing on a manual run) is skipped instead of racing — both reading the
# same already-downloaded set and downloading the same files twice. Accessed only
# from the event loop, so plain set ops are race-free.
_active_syncs: set[str] = set()
_MAX_CONCURRENT = 2
_sync_sem: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _sync_sem
    if _sync_sem is None:
        _sync_sem = asyncio.Semaphore(_MAX_CONCURRENT)
    return _sync_sem


def is_syncing(target_id: str) -> bool:
    return target_id in _active_syncs


def active_syncs() -> list[str]:
    return list(_active_syncs)


def _claim_sync(target_id: str) -> bool:
    """Atomically claim a target for syncing; False if one is already in flight."""
    if target_id in _active_syncs:
        return False
    _active_syncs.add(target_id)
    return True

# spotdl logs one of these per track as it finishes; the captured name is shown
# in the UI so a long sync visibly progresses track-by-track.
_RE_DOWNLOADED = re.compile(r'Downloaded "([^"]+)"')

# Tracks per spotdl invocation. Small batches keep progress moving, commit each
# batch to the DB as it finishes (so a mid-run failure doesn't lose everything),
# and avoid the mass rate-limiting that stalls one giant 200-URL call.
_CHUNK = 10

_RE_NONWORD = re.compile(r'[^\w\s]')
_AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".opus"}


def _norm(s: str) -> str:
    return _RE_NONWORD.sub('', (s or '').lower()).strip()


def _present_index(output_dir: str) -> dict[str, list[tuple[str, str]]]:
    """Map normalized title → [(normalized artist, filename)] for files on disk."""
    idx: dict[str, list[tuple[str, str]]] = {}
    try:
        names = os.listdir(output_dir)
    except OSError:
        return idx
    for fname in names:
        if os.path.splitext(fname)[1].lower() not in _AUDIO_EXTS:
            continue
        stem = os.path.splitext(fname)[0]
        artist, title = stem.split(" - ", 1) if " - " in stem else ("", stem)
        idx.setdefault(_norm(title), []).append((_norm(artist), fname))
    return idx


def _match_file(idx: dict[str, list[tuple[str, str]]], song: dict) -> str | None:
    """The on-disk filename for this song, or None. Requires a title match AND
    whole-word artist agreement.

    Title alone would falsely match a different artist's same-titled track (covers,
    "Intro", "Interlude") and permanently mark it downloaded. The on-disk artist is
    spotdl's joined `{artists}`, so we match on whole tokens: either the song's
    primary-artist words all appear in the filename's artist words, or the file's
    artist words are all among the song's artists (multi-artist files, any order).

    Whole-token comparison — not substring — avoids "sia" spuriously matching
    "siavash". An artist-less song only matches an artist-less file (never a known
    artist against an unknown one), so a metadata-poor "Intro" can't hijack a
    same-titled track that happens to already be on disk.
    """
    entries = idx.get(_norm(song.get("name", "")))
    if entries is None:
        return None
    primary_tokens = set(_norm(song.get("artist", "")).split())
    full_tokens = set(_norm(" ".join(song.get("artists") or [song.get("artist", "")])).split())
    for fa, fname in entries:
        fa_tokens = set(fa.split())
        if not fa_tokens or not primary_tokens:
            if not fa_tokens and not primary_tokens:
                return fname   # both artist-less; the title match is all there is
            continue
        if primary_tokens <= fa_tokens or fa_tokens <= full_tokens:
            return fname
    return None


def _song_present(idx: dict[str, list[tuple[str, str]]], song: dict) -> bool:
    return _match_file(idx, song) is not None


def track_on_disk(output_dir: str, song: dict) -> bool:
    """Public one-shot presence check for a single song (rebuilds the dir index)."""
    return _song_present(_present_index(output_dir), song)


async def _download_in_chunks(songs: list[dict], output_dir: str, on_line, target_id: str) -> tuple[int, list[dict], str]:
    """Download new tracks (spotdl Song dicts) in batches.

    Metadata is pre-resolved (no per-track Spotify lookups). Marking by on-disk
    presence — rather than spotdl's batch exit code — keeps a single failed track
    in a batch (e.g. an AudioProviderError for a song not on YouTube) from
    discarding the rest, and never marks a track that didn't download.

    Returns (landed_count, failed_songs, combined_output). Failed songs are the
    ones whose file never appeared — surfaced in the UI and retried next sync.
    """
    from app import database as db
    from app import downloader
    outputs, landed = [], 0
    for i in range(0, len(songs), _CHUNK):
        chunk = songs[i:i + _CHUNK]
        _code, output = await downloader.download_from_meta(chunk, output_dir, on_line=on_line)
        outputs.append(output)
        idx = _present_index(output_dir)
        for s in chunk:
            if _song_present(idx, s):
                db.mark_downloaded(s["song_id"], s["name"], s["artist"], target_id)
                landed += 1
    idx = _present_index(output_dir)
    failed = [s for s in songs if not _song_present(idx, s)]
    return landed, failed, "\n".join(outputs)


async def _run_sync(target_id: str, force: bool = False):
    """Sync one target. Skips if already in flight; capped by a global semaphore."""
    from app import progress as prog
    if not _claim_sync(target_id):
        logger.info("Sync already running for %s; skipping duplicate", target_id)
        return
    try:
        async with _get_sem():
            await _sync_target(target_id, force)
    finally:
        _active_syncs.discard(target_id)
        # Never leave a target stuck "running" if something resolved oddly.
        if prog.get_progress(target_id).get("status") == "running":
            prog.set_progress(target_id, {"status": "error", "message": "Sync ended unexpectedly"})


async def _sync_target(target_id: str, force: bool):
    from app import database as db
    from app import downloader, spotify
    from app import progress as prog

    target = db.get_target(target_id)
    if not target or (not target["enabled"] and not force):
        prog.clear_progress(target_id)
        return

    state = {"downloaded": 0, "skipped": 0, "total": target.get("track_count") or 0, "current": None}
    prog.set_progress(target_id, {"status": "running", **state})

    def on_line(line: str):
        changed = False
        m = _RE_DOWNLOADED.search(line)
        if m:
            state["downloaded"] += 1
            state["current"] = m.group(1)
            changed = True
        elif "skipping" in line.lower() or "skipped" in line.lower():
            state["skipped"] += 1
            changed = True
        if changed:
            prog.set_progress(target_id, {"status": "running", **state})

    log_id = None
    try:
        log_id = db.log_sync_start(target_id)
        logger.info("Starting sync for %s (%s)", target["name"], target_id)

        if target["type"] in ("playlist", "liked_songs", "album"):
            # spotdl authenticates app-only (client credentials), which can't read
            # PRIVATE playlists. So we resolve the track list ourselves (user auth)
            # and hand spotdl pre-resolved metadata — no per-track Spotify lookups.
            # Albums go through the same path so their failures land in failed_tracks
            # and surface in the retry UI, just like playlists/liked.
            if target["type"] == "playlist":
                all_songs = spotify.get_playlist_song_meta(target_id)
            elif target["type"] == "album":
                all_songs = spotify.get_album_song_meta(target_id)
            else:
                all_songs = spotify.get_liked_song_meta()
            output_dir = downloader.music_dir(target)
            already = db.get_downloaded_ids(target_id)
            new_songs = [s for s in all_songs if s["song_id"] not in already]
            state["total"] = len(all_songs)
            state["skipped"] = len(all_songs) - len(new_songs)
            prog.set_progress(target_id, {"status": "running", **state})   # "0 of N" before downloads
            landed, failed, output = await _download_in_chunks(new_songs, output_dir, on_line, target_id)
            db.replace_failed_tracks(target_id, failed)
            # A real failure only when there was new work and NOTHING landed — by our
            # on-disk match or by spotdl's own "Downloaded" count. The latter guards
            # against a filename-match false-negative flipping a good sync to "error".
            code = 0 if (not new_songs or landed > 0 or state["downloaded"] > 0) else 1
        else:
            db.log_sync_finish(log_id, "error", f"Unknown type: {target['type']}")
            prog.set_progress(target_id, {"status": "error", "message": "Unknown type", **state})
            return

        status = "ok" if code == 0 else "error"
        db.log_sync_finish(log_id, status, output[-2000:])
        logger.info("spotdl output for %s:\n%s", target["name"], output[-3000:])

        if code == 0:
            # state["total"] is the count we actually processed this run (set for
            # every type above), so albums no longer report a stale track_count.
            count = state["total"] or target["track_count"] or 0
            db.set_target_synced(target_id, count)
            # Regenerate the folder's .m3u8 (Spotify order) for external players.
            try:
                idx = _present_index(output_dir)
                ordered = [{"filename": fn, "title": s["name"], "artist": s["artist"]}
                           for s in all_songs if (fn := _match_file(idx, s))]
                downloader.write_m3u(target, ordered)
            except Exception:
                logger.exception("Couldn't write playlist file for %s", target_id)

        prog.set_progress(target_id, {
            "status": "done" if code == 0 else "error",
            **state,
            "message": (output[-500:] if code != 0 else None),
        })
        logger.info("Sync finished for %s: %s", target["name"], status)

    except Exception as exc:
        logger.exception("Sync failed for %s", target_id)
        if log_id is not None:
            db.log_sync_finish(log_id, "error", str(exc))
        prog.set_progress(target_id, {"status": "error", "message": str(exc), **state})


async def _retry_failed(target_id: str):
    """Re-attempt a target's failed tracks one at a time, reporting which is being
    searched so the UI can show real per-track retry progress."""
    from app import database as db
    from app import downloader
    from app import progress as prog

    if not _claim_sync(target_id):
        logger.info("Sync already running for %s; retry skipped", target_id)
        return
    try:
        async with _get_sem():
            target = db.get_target(target_id)
            if not target:
                prog.clear_progress(target_id)
                return
            failed = db.get_failed_tracks(target_id)
            output_dir = downloader.music_dir(target)
            succeeded, last_result = 0, None
            try:
                for i, f in enumerate(failed):
                    label = f"{f['name']} — {f['artist']}" if f.get("artist") else f["name"]
                    prog.set_progress(target_id, {
                        "status": "running", "mode": "retry", "current": label, "result": last_result,
                        "downloaded": succeeded, "done": i, "total": len(failed),
                    })
                    url = f"https://open.spotify.com/track/{f['spotify_id']}"
                    _code, output = await downloader.download_single_track(url, output_dir)
                    song = {"name": f["name"], "artist": f["artist"],
                            "artists": [f["artist"]], "song_id": f["spotify_id"]}
                    if _song_present(_present_index(output_dir), song):
                        db.mark_downloaded(f["spotify_id"], f["name"], f["artist"], target_id)
                        db.remove_failed_track(target_id, f["spotify_id"])
                        succeeded += 1
                        last_result = f"✓ {f['name']} — downloaded"
                    else:
                        reason = downloader.classify_failure(output)
                        db.set_failed_error(target_id, f["spotify_id"], reason)
                        last_result = f"✗ {f['name']} — {reason}"
                    prog.set_progress(target_id, {
                        "status": "running", "mode": "retry", "current": None, "result": last_result,
                        "downloaded": succeeded, "done": i + 1, "total": len(failed),
                    })
                prog.set_progress(target_id, {
                    "status": "done", "mode": "retry", "result": last_result,
                    "downloaded": succeeded, "total": len(failed),
                })
                logger.info("Retry for %s: %d/%d recovered", target["name"], succeeded, len(failed))
            except Exception as exc:
                logger.exception("Retry failed for %s", target_id)
                prog.set_progress(target_id, {"status": "error", "mode": "retry", "message": str(exc)})
    finally:
        _active_syncs.discard(target_id)
        if prog.get_progress(target_id).get("status") == "running":
            prog.set_progress(target_id, {"status": "error", "message": "Retry ended unexpectedly"})


def register_target(target_id: str, schedule: str):
    from app import database as db
    interval = _INTERVALS.get(schedule, dict(days=1))
    delta = timedelta(**interval)
    now = datetime.now(timezone.utc)

    # Seed next_run from the last successful sync. Without this, IntervalTrigger
    # restarts the clock at now+interval on every app restart, so a target whose
    # interval exceeds the restart cadence (weekly/monthly) would never fire.
    next_run = now + delta
    target = db.get_target(target_id)
    last = target.get("last_synced") if target else None
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            due = last_dt + delta
            next_run = due if due > now else now + timedelta(seconds=30)  # overdue → run shortly
        except ValueError:
            pass

    scheduler.add_job(
        _run_sync,
        trigger=IntervalTrigger(**interval),
        id=target_id,
        kwargs={"target_id": target_id},
        replace_existing=True,
        misfire_grace_time=3600,
        next_run_time=next_run,
    )
    logger.info("Registered job for %s (%s), next run %s", target_id, schedule, next_run.isoformat())


def unregister_target(target_id: str):
    if scheduler.get_job(target_id):
        scheduler.remove_job(target_id)
        logger.info("Removed job for %s", target_id)


def load_all_jobs():
    from app import database as db
    targets = db.get_enabled_targets()
    for t in targets:
        register_target(t["id"], t["schedule"])
    logger.info("Loaded %d scheduled jobs", len(targets))
