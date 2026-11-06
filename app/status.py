"""Human-readable sync status for the UI ("next sync in 4 hours", "overdue").

Relative wording is computed server-side: a duration doesn't depend on the
viewer's timezone, so there's no UTC-vs-local mismatch to get wrong.
"""
from datetime import datetime, timedelta, timezone

# How late a scheduled sync can run before it's flagged. Covers a sync that's
# queued behind others or waiting out a restart.
OVERDUE_GRACE = timedelta(hours=1)


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _span(delta: timedelta) -> str:
    s = max(0, int(delta.total_seconds()))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = s // size
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return ""


def ago(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "never"
    span = _span(now - dt)
    return f"{span} ago" if span else "just now"


def until(dt: datetime, now: datetime) -> str:
    span = _span(dt - now)
    return f"in {span}" if span else "any moment"


def sync_status(target: dict, interval: timedelta, now: datetime | None = None) -> dict:
    """{"text", "title", "overdue"} describing when a target last / next syncs."""
    now = now or datetime.now(timezone.utc)
    last = parse_ts(target.get("last_synced"))
    title = f"Last synced {last.strftime('%Y-%m-%d %H:%M')} UTC" if last else "Never synced"
    if not target.get("enabled"):
        text = f"Synced {ago(last, now)}" if last else "Not auto-syncing"
        return {"text": text, "title": title, "overdue": False}
    if last is None:
        return {"text": "First sync pending", "title": title, "overdue": False}
    due = last + interval
    if now > due + OVERDUE_GRACE:
        return {"text": f"Overdue · synced {ago(last, now)}", "title": title, "overdue": True}
    return {"text": f"Next sync {until(due, now)}", "title": title, "overdue": False}
