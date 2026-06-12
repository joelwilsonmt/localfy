import logging
import os
import re
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

DOWNLOAD_PATH = os.environ.get("DOWNLOAD_PATH", "/music")

_INTERVALS = {
    "daily": dict(days=1),
    "weekly": dict(weeks=1),
    "monthly": dict(days=30),
}

_RE_TOTAL = re.compile(r'(\d+)\s+songs?', re.IGNORECASE)


async def _run_sync(target_id: str, force: bool = False):
    from app import database as db
    from app import downloader, spotify
    from app import progress as prog

    target = db.get_target(target_id)
    if not target or (not target["enabled"] and not force):
        return

    log_id = db.log_sync_start(target_id)
    logger.info("Starting sync for %s (%s)", target["name"], target_id)

    state = {"downloaded": 0, "skipped": 0, "total": target.get("track_count") or 0}
    prog.set_progress(target_id, {"status": "running", **state})

    def on_line(line: str):
        lower = line.lower()
        changed = False
        if "downloaded" in lower:
            state["downloaded"] += 1
            changed = True
        elif "skipping" in lower or "skipped" in lower:
            state["skipped"] += 1
            changed = True
        m = _RE_TOTAL.search(line)
        if m:
            state["total"] = int(m.group(1))
            changed = True
        if changed:
            prog.set_progress(target_id, {"status": "running", **state})

    try:
        if target["type"] == "playlist":
            code, output = await downloader.sync_playlist(
                target_id, target["spotify_url"], target["name"], on_line=on_line
            )
        elif target["type"] == "album":
            code, output = await downloader.sync_album(
                target_id, target["spotify_url"], target["name"], on_line=on_line
            )
        elif target["type"] == "liked_songs":
            all_tracks = spotify.get_liked_track_ids()
            already = db.get_downloaded_ids(target_id)
            new_tracks = [(tid, name, artist) for tid, name, artist in all_tracks if tid not in already]
            state["total"] = len(all_tracks)
            output_dir = os.path.join(DOWNLOAD_PATH, "Liked Songs")
            ids = [t[0] for t in new_tracks]
            code, output = await downloader.download_tracks(ids, output_dir, on_line=on_line)
            if code == 0:
                for tid, name, artist in new_tracks:
                    db.mark_downloaded(tid, name, artist, target_id)
        else:
            db.log_sync_finish(log_id, "error", f"Unknown type: {target['type']}")
            prog.set_progress(target_id, {"status": "error", "message": "Unknown type", **state})
            return

        status = "ok" if code == 0 else "error"
        db.log_sync_finish(log_id, status, output[-2000:])
        logger.info("spotdl output for %s:\n%s", target["name"], output[-3000:])

        if code == 0:
            info = spotify.get_liked_songs_info() if target["type"] == "liked_songs" else None
            count = info["track_count"] if info else target["track_count"]
            db.set_target_synced(target_id, count)

        prog.set_progress(target_id, {
            "status": "done" if code == 0 else "error",
            **state,
            "message": output[-500:] if code != 0 else None,
        })
        logger.info("Sync finished for %s: %s", target["name"], status)

    except Exception as exc:
        logger.exception("Sync failed for %s", target_id)
        db.log_sync_finish(log_id, "error", str(exc))
        prog.set_progress(target_id, {"status": "error", "message": str(exc), **state})


def register_target(target_id: str, schedule: str):
    interval = _INTERVALS.get(schedule, dict(days=1))
    scheduler.add_job(
        _run_sync,
        trigger=IntervalTrigger(**interval),
        id=target_id,
        kwargs={"target_id": target_id},
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Registered job for %s (%s)", target_id, schedule)


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
