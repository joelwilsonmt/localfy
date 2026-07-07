_store: dict = {}
_track_store: dict = {}  # track_id → "idle" | "running" | "done" | "error"
_track_msg: dict = {}    # track_id → human reason (e.g. why a download failed)


def set_progress(target_id: str, data: dict):
    _store[target_id] = data


def get_progress(target_id: str) -> dict:
    return _store.get(target_id, {"status": "idle"})


def clear_progress(target_id: str):
    _store.pop(target_id, None)


def set_track(track_id: str, status: str, message: str = None):
    _track_store[track_id] = status
    if message is not None:
        _track_msg[track_id] = message


def get_track(track_id: str) -> str:
    return _track_store.get(track_id, "idle")


def get_track_msg(track_id: str) -> str | None:
    return _track_msg.get(track_id)
