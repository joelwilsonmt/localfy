_store: dict = {}
_track_store: dict = {}  # track_id → "idle" | "running" | "done" | "error"


def set_progress(target_id: str, data: dict):
    _store[target_id] = data


def get_progress(target_id: str) -> dict:
    return _store.get(target_id, {"status": "idle"})


def clear_progress(target_id: str):
    _store.pop(target_id, None)


def set_track(track_id: str, status: str):
    _track_store[track_id] = status


def get_track(track_id: str) -> str:
    return _track_store.get(track_id, "idle")
