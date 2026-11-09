import os

import pytest

from app import downloader
from app.downloader import _redact, _safe_name, classify_failure


@pytest.mark.parametrize("output, reason", [
    ("ERROR: Sign in to confirm you're not a bot", "YouTube is rate-limiting downloads right now — try again later"),
    ("ERROR: [youtube] abc: Video unavailable", "The matched YouTube video is unavailable"),
    ("This video is age-restricted", "YouTube video is age-restricted and can't be downloaded"),
    ("LookupError: No results found for song", "No match found on YouTube"),
    ("something else entirely", "Download failed"),
    (None, "Download failed"),
])
def test_classify_failure(output, reason):
    assert classify_failure(output) == reason


def test_safe_name_strips_path_characters():
    assert _safe_name("../../etc/passwd") == "______etc_passwd"
    assert _safe_name("Mix: Vol. 1 / Live") == "Mix_ Vol_ 1 _ Live"


def test_redact_hides_credentials():
    cmd = ["spotdl", "--client-id", "abc", "--client-secret", "shh", "download", "x"]
    assert _redact(cmd) == ["spotdl", "--client-id", "***", "--client-secret", "***", "download", "x"]


def test_music_dir_layout(env):
    music = str(env.music)
    assert downloader.music_dir({"type": "liked_songs", "name": "x"}) == os.path.join(music, "Liked Songs")
    assert downloader.music_dir({"type": "album", "name": "A / B"}) == os.path.join(music, "Albums", "A _ B")
    assert downloader.music_dir({"type": "playlist", "name": "Road Trip"}) == os.path.join(music, "Road Trip")


def test_dedupe_hardlinks_identical_copies(env):
    a, b = env.music / "A", env.music / "B"
    a.mkdir()
    b.mkdir()
    for folder in (a, b):
        (folder / "Artist - Song.mp3").write_bytes(b"x" * 1000)
    (a / "Artist - Unique.mp3").write_bytes(b"y" * 10)

    report = downloader.storage_report()
    assert (report["dup_files"], report["wasted_bytes"]) == (1, 1000)

    result = downloader.dedupe_hardlinks()
    assert (result["linked"], result["saved_bytes"], result["failed"]) == (1, 1000, 0)
    assert os.stat(a / "Artist - Song.mp3").st_ino == os.stat(b / "Artist - Song.mp3").st_ino
    assert downloader.storage_report()["dup_files"] == 0


def test_dedupe_never_links_different_audio(env):
    # Same filename and size, different bytes: not a duplicate.
    a, b = env.music / "A", env.music / "B"
    a.mkdir()
    b.mkdir()
    (a / "Artist - Song.mp3").write_bytes(b"x" * 1000)
    (b / "Artist - Song.mp3").write_bytes(b"y" * 1000)
    result = downloader.dedupe_hardlinks()
    assert result["linked"] == 0
    assert (b / "Artist - Song.mp3").read_bytes() == b"y" * 1000


def _no_hardlinks(monkeypatch):
    """Make os.link fail the way it does on exFAT/FAT."""
    import errno

    def fail(*_a, **_k):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(downloader.os, "link", fail)


def _dupes(env):
    a, b = env.music / "A", env.music / "B"
    a.mkdir()
    b.mkdir()
    for folder in (a, b):
        (folder / "Artist - Song.mp3").write_bytes(b"x" * 1000)
    return a, b


def test_hardlink_probe_cleans_up(env):
    assert downloader.hardlinks_supported() is True
    assert not [p for p in os.listdir(env.music) if p.startswith(".lfy-linktest-")]


def test_hardlink_probe_detects_exfat_like_filesystems(env, monkeypatch):
    _no_hardlinks(monkeypatch)
    assert downloader.hardlinks_supported() is False
    assert not [p for p in os.listdir(env.music) if p.startswith(".lfy-linktest-")]


def test_storage_report_flags_missing_hardlinks_only_with_duplicates(env, monkeypatch):
    _no_hardlinks(monkeypatch)
    assert downloader.storage_report()["hardlinks"] is None   # nothing to dedupe, not probed
    _dupes(env)
    report = downloader.storage_report()
    assert report["dup_files"] == 1 and report["hardlinks"] is False


def test_dedupe_refuses_without_hardlinks_and_leaves_files(env, monkeypatch):
    _no_hardlinks(monkeypatch)
    a, b = _dupes(env)
    assert downloader.dedupe_hardlinks() == {"linked": 0, "saved_bytes": 0, "failed": 0, "unsupported": True}
    assert os.stat(a / "Artist - Song.mp3").st_ino != os.stat(b / "Artist - Song.mp3").st_ino
