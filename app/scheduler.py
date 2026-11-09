import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import matching, status

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

# Dev/maintenance switch: run the web app without scheduled syncs (manual syncs
# from the UI still work).
DISABLED = os.environ.get("LOCALFY_DISABLE_SCHEDULER", "").lower() in ("1", "true", "yes")

_INTERVALS = {
    "daily": dict(days=1),
    "weekly": dict(weeks=1),
    "monthly": dict(days=30),
}

# ── Background tasks ───────────────────────────────────────────────────────────
# The event loop only keeps weak references to tasks, so a fire-and-forget
# asyncio.create_task() can be garbage-collected mid-run. Hold them here until done.
_background: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


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


def track_on_disk(output_dir: str, song: dict) -> bool:
    """One-shot presence check for a single song (rebuilds the dir index)."""
    return matching.find_song(matching.index_dir(output_dir), song) is not None


async def download_one(target: dict, song: dict) -> tuple[bool, str | None]:
    """Download one pre-resolved song into a target's folder.

    Success is judged by the file landing on disk — spotdl exits 0 even when
    nothing was written. Returns (landed, failure reason).
    """
    from app import database as db
    from app import downloader
    out_dir = downloader.music_dir(target)
    code, output = await downloader.download_from_meta([song], out_dir)
    if track_on_disk(out_dir, song):
        db.mark_downloaded_many(target["id"], [song])
        db.remove_failed_track(target["id"], song["song_id"])
        return True, None
    return False, downloader.classify_failure(output) if code != 0 else "Download finished but no file appeared"


async def _download_in_chunks(songs: list[dict], output_dir: str, on_line, target_id: str
                              ) -> tuple[int, list[dict], str]:
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
    outputs, landed_ids, failed = [], set(), []
    for i in range(0, len(songs), _CHUNK):
        chunk = songs[i:i + _CHUNK]
        _code, output = await downloader.download_from_meta(chunk, output_dir, on_line=on_line)
        outputs.append(output)
        idx = matching.index_dir(output_dir)
        landed = [s for s in chunk if matching.find_song(idx, s)]
        db.mark_downloaded_many(target_id, landed)
        landed_ids.update(s["song_id"] for s in landed)
        missed = [s for s in chunk if s["song_id"] not in landed_ids]
        # Per-track reason from this batch's output, shown on the playlist page.
        reasons = downloader.failure_reasons(output, missed)
        failed.extend({**s, "error": reasons[s["song_id"]]} for s in missed)
    return len(landed_ids), failed, "\n".join(outputs)


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

        if target["type"] not in ("playlist", "liked_songs", "album"):
            db.log_sync_finish(log_id, "error", f"Unknown type: {target['type']}")
            prog.set_progress(target_id, {"status": "error", "message": "Unknown type", **state})
            return

        # spotdl authenticates app-only (client credentials), which can't read
        # PRIVATE playlists. So we resolve the track list ourselves (user auth)
        # and hand spotdl pre-resolved metadata — no per-track Spotify lookups.
        # spotipy is blocking; a big playlist is many paged requests, so keep it
        # off the event loop or every page and progress poll stalls meanwhile.
        all_songs = await asyncio.to_thread(spotify.get_target_song_meta, target)
        db.replace_target_tracks(target_id, all_songs)
        output_dir = downloader.music_dir(target)
        already = db.get_downloaded_ids(target_id)
        known_failures = {f["spotify_id"] for f in db.get_failed_tracks(target_id)}
        # What's on disk decides what to download; the downloaded_tracks record
        # follows it rather than the other way round.
        # - In the folder but not recorded (recorded under another playlist
        #   before downloads were tracked per target, or the DB was rebuilt):
        #   record it, don't re-send it.
        # - Recorded but no longer in the folder (deleted, or recorded by older
        #   versions that trusted spotdl's exit code): drop the stale record and
        #   download it again, or it would never be fetched.
        idx = matching.index_dir(output_dir)
        present = [s for s in all_songs if matching.find_song(idx, s)]
        present_ids = {s["song_id"] for s in present}
        db.mark_downloaded_many(target_id, [s for s in present if s["song_id"] not in already])
        gone = ({s["song_id"] for s in all_songs} & already) - present_ids
        if gone:
            logger.info("%s: %d recorded tracks are missing from disk; downloading them again",
                        target["name"], len(gone))
            db.forget_downloads(target_id, gone)
        new_songs = [s for s in all_songs if s["song_id"] not in present_ids]
        state["total"] = len(all_songs)
        state["skipped"] = len(all_songs) - len(new_songs)
        prog.set_progress(target_id, {"status": "running", **state})   # "0 of N" before downloads
        landed, failed, output = await _download_in_chunks(new_songs, output_dir, on_line, target_id)
        db.replace_failed_tracks(target_id, failed)
        # A real failure only when there were tracks never tried before and NOTHING
        # landed — by our on-disk match or by spotdl's own "Downloaded" count (the
        # latter guards against a filename-match false-negative). Tracks that
        # already failed are retried every run; failing again is expected and
        # shows in the summary, but mustn't mark the whole sync as failed (that
        # would also stop last_synced advancing, so the playlist reads overdue).
        fresh = [s for s in new_songs if s["song_id"] not in known_failures]
        code = 0 if (not fresh or landed > 0 or state["downloaded"] > 0) else 1

        result = "ok" if code == 0 else "error"
        parts = [f"{landed} new" if landed else "nothing new"]
        if failed:
            parts.append(f"{len(failed)} couldn't be downloaded")
        db.log_sync_finish(log_id, result, output[-2000:], summary=" · ".join(parts))
        logger.info("spotdl output for %s:\n%s", target["name"], output[-3000:])

        if code == 0:
            db.set_target_synced(target_id, state["total"])
            # Regenerate the folder's .m3u8 (Spotify order) for external players.
            try:
                idx = matching.index_dir(output_dir)
                ordered = [{"filename": fn, "title": s["name"], "artist": s["artist"]}
                           for s in all_songs if (fn := matching.find_song(idx, s))]
                downloader.write_m3u(target, ordered)
            except Exception:
                logger.exception("Couldn't write playlist file for %s", target_id)

        prog.set_progress(target_id, {
            "status": "done" if code == 0 else "error",
            **state,
            # A human reason, not spotdl's raw output (that's in the sync log).
            "message": (f"Nothing downloaded: {downloader.classify_failure(output)}" if code != 0 else None),
        })
        logger.info("Sync finished for %s: %s", target["name"], result)

    except Exception as exc:
        logger.exception("Sync failed for %s", target_id)
        if log_id is not None:
            db.log_sync_finish(log_id, "error", str(exc))
        prog.set_progress(target_id, {"status": "error", "message": str(exc), **state})


async def _retry_failed(target_id: str):
    """Re-attempt a target's failed tracks one at a time, reporting which is being
    searched so the UI can show real per-track retry progress."""
    from app import database as db
    from app import progress as prog
    from app import spotify

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
            succeeded, last_result = 0, None
            try:
                for i, f in enumerate(failed):
                    label = f"{f['name']} — {f['artist']}" if f.get("artist") else f["name"]
                    prog.set_progress(target_id, {
                        "status": "running", "mode": "retry", "current": label, "result": last_result,
                        "downloaded": succeeded, "done": i, "total": len(failed),
                    })
                    # Same pre-resolved metadata path as a sync (one user-auth
                    # lookup, then straight to YouTube) rather than handing spotdl
                    # a Spotify URL to resolve itself.
                    try:
                        song = await asyncio.to_thread(spotify.get_track_song_meta, f["spotify_id"])
                    except Exception:
                        logger.warning("Couldn't load %s from Spotify for retry", f["spotify_id"], exc_info=True)
                        song = None
                    if song is None:
                        reason = "Couldn't load this track from Spotify"
                        db.set_failed_error(target_id, f["spotify_id"], reason)
                        last_result = f"✗ {f['name']} — {reason}"
                    else:
                        landed, reason = await download_one(target, song)
                        if landed:
                            succeeded += 1
                            last_result = f"✓ {f['name']} — downloaded"
                        else:
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


def interval_for(schedule: str) -> timedelta:
    return timedelta(**_INTERVALS.get(schedule, _INTERVALS["daily"]))


def next_run_for(last_synced: str | None, delta: timedelta, now: datetime) -> datetime:
    """When a target's scheduled sync should next fire.

    Seeded from the last successful sync. Without this, IntervalTrigger
    restarts the clock at now+interval on every app restart, so a target whose
    interval exceeds the restart cadence (weekly/monthly) would never fire.
    """
    last_dt = status.parse_ts(last_synced)
    if last_dt is None:
        return now + delta
    due = last_dt + delta
    return due if due > now else now + timedelta(seconds=30)   # overdue → run shortly


def register_target(target_id: str, schedule: str):
    from app import database as db
    if DISABLED:
        return   # nothing is scheduled; manual syncs still work
    delta = interval_for(schedule)
    target = db.get_target(target_id)
    next_run = next_run_for(target.get("last_synced") if target else None, delta, datetime.now(timezone.utc))

    scheduler.add_job(
        _run_sync,
        trigger=IntervalTrigger(seconds=int(delta.total_seconds())),
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
