"""Matching Spotify tracks to the audio files spotdl wrote.

spotdl names files "{artists} - {title}.{ext}" (see downloader.OUTPUT_TEMPLATE),
where {artists} is every artist joined with ", ". This module is the single
place that decides "is this Spotify track already on disk, and which file is
it" — the sync, the detail page, the player queue and library search all go
through it so they can never disagree.
"""
import os
import re
from typing import NamedTuple

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".opus"}

_RE_FEAT = re.compile(r'\s*[\(\[]\s*(?:feat|ft|featuring)[.\s]([^\)\]]*)[\)\]]', re.IGNORECASE)
_RE_NONWORD = re.compile(r'[^\w\s]')
_RE_WS = re.compile(r'\s+')


class Entry(NamedTuple):
    artist_tokens: set[str]
    filename: str
    title: str               # normalized title with any "(feat. X)" kept
    feat_tokens: set[str]    # artists named in the title's "(feat. X)"


# feat-stripped normalized title -> files with that title
Index = dict[str, list[Entry]]


def _clean(s: str) -> str:
    return _RE_WS.sub(' ', _RE_NONWORD.sub('', s.lower())).strip()


def norm(s: str | None) -> str:
    """Lowercase, drop "(feat. X)" and punctuation, collapse whitespace."""
    return _clean(_RE_FEAT.sub('', s or ''))


def _feat_tokens(title: str | None) -> set[str]:
    return set(_clean(" ".join(_RE_FEAT.findall(title or ""))).split())


def tokens(s: str | None) -> set[str]:
    return set(norm(s).split())


def parse_filename(fname: str) -> tuple[str, str]:
    """(artist, title) from "Artist - Title.ext"; artist is "" if there's no " - "."""
    stem = os.path.splitext(fname)[0]
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
    else:
        artist, title = "", stem
    return artist.strip(), title.strip()


def audio_files(folder: str) -> list[str]:
    """Audio filenames in a folder, sorted; [] if the folder doesn't exist."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(f for f in names if os.path.splitext(f)[1].lower() in AUDIO_EXTS)


def build_index(filenames: list[str]) -> Index:
    idx: Index = {}
    for fname in filenames:
        artist, title = parse_filename(fname)
        idx.setdefault(norm(title), []).append(
            Entry(tokens(artist), fname, _clean(title), _feat_tokens(title)))
    return idx


def index_dir(folder: str) -> Index:
    return build_index(audio_files(folder))


def find(idx: Index, title: str, artist: str = "", artists: list[str] | None = None) -> str | None:
    """The on-disk filename for a track, or None.

    Requires a title match AND whole-word artist agreement. Title alone would
    falsely match a different artist's same-titled track (covers, "Intro") and
    permanently mark it downloaded. The file's artist is spotdl's joined
    {artists}, so either the track's primary-artist words all appear in the
    file's artist words, or the file's artist words are all among the track's
    artists (multi-artist files, any order).

    Whole tokens, not substrings, so "sia" never matches "siavash". An
    artist-less track only matches an artist-less file, so a metadata-poor
    "Intro" can't hijack a same-titled track that happens to be on disk.

    Titles that differ only by a "(feat. X)" part match only when X is
    credited on the other side too. Spotify sometimes drops or adds that
    suffix after a track was downloaded (same recording), but "Song (feat. B)"
    must never match a solo "A - Song" already on disk.
    """
    entries = idx.get(norm(title))
    if not entries:
        return None
    primary = tokens(artist)
    full = tokens(" ".join(artists)) if artists else primary
    title_clean, title_feat = _clean(title or ""), _feat_tokens(title)
    for file_tokens, fname, file_title, file_feat in entries:
        if file_title != title_clean and not (title_feat <= file_tokens and file_feat <= full | primary):
            continue
        if not file_tokens or not primary:
            if not file_tokens and not primary:
                return fname
            continue
        if primary <= file_tokens or file_tokens <= full:
            return fname
    return None


def find_song(idx: Index, song: dict) -> str | None:
    """find() for a spotdl Song dict (name / artist / artists)."""
    return find(idx, song.get("name", ""), song.get("artist", ""), song.get("artists"))
