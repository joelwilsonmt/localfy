from datetime import datetime, timedelta, timezone

from app.status import ago, sync_status

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def _target(enabled=True, hours_ago=None):
    last = (NOW - timedelta(hours=hours_ago)).isoformat() if hours_ago is not None else None
    return {"enabled": enabled, "last_synced": last}


def test_ago_wording():
    assert ago(NOW - timedelta(seconds=20), NOW) == "just now"
    assert ago(NOW - timedelta(minutes=1), NOW) == "1 minute ago"
    assert ago(NOW - timedelta(days=69), NOW) == "69 days ago"
    assert ago(None, NOW) == "never"


def test_on_schedule_shows_next_run():
    st = sync_status(_target(hours_ago=20), DAY, NOW)
    assert (st["text"], st["overdue"]) == ("Next sync in 4 hours", False)


def test_overdue_after_grace():
    assert sync_status(_target(hours_ago=24.5), DAY, NOW)["overdue"] is False   # within the 1h grace
    st = sync_status(_target(hours_ago=24 * 69), DAY, NOW)
    assert st["overdue"] is True and st["text"] == "Overdue · synced 69 days ago"


def test_not_auto_syncing_is_never_overdue():
    st = sync_status(_target(enabled=False, hours_ago=24 * 69), DAY, NOW)
    assert (st["text"], st["overdue"]) == ("Synced 69 days ago", False)
    assert sync_status(_target(enabled=False), DAY, NOW)["text"] == "Not auto-syncing"
    assert sync_status(_target(enabled=True), DAY, NOW)["text"] == "First sync pending"
