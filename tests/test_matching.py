from app import matching
from app.matching import build_index, find, norm, parse_filename


def test_norm_drops_feat_punctuation_and_case():
    assert norm("Song (feat. Guest)") == "song"
    assert norm("Song [ft. Guest]") == "song"
    assert norm("  Hello,   World! ") == "hello world"
    assert norm(None) == ""


def test_parse_filename_splits_on_first_separator():
    assert parse_filename("A, B - Title - Part 2.mp3") == ("A, B", "Title - Part 2")
    assert parse_filename("Just A Title.opus") == ("", "Just A Title")


def test_multi_artist_file_matches_primary_artist_only():
    # Liked songs / DB rows carry the primary artist; the file has all of them.
    idx = build_index(["Artist A, Artist B - Song.mp3"])
    assert find(idx, "Song", "Artist A") == "Artist A, Artist B - Song.mp3"
    assert find(idx, "Song", "Artist B", ["Artist B", "Artist A"]) == "Artist A, Artist B - Song.mp3"


def test_file_with_fewer_artists_matches_full_artist_list():
    idx = build_index(["Artist A - Song.mp3"])
    assert find(idx, "Song", "Artist A, Artist B", ["Artist A, Artist B"]) == "Artist A - Song.mp3"


def test_same_title_different_artist_does_not_match():
    idx = build_index(["Someone - Intro.mp3"])
    assert find(idx, "Intro", "Other Band") is None


def test_whole_tokens_not_substrings():
    idx = build_index(["Siavash - Track.mp3"])
    assert find(idx, "Track", "Sia") is None


def test_artistless_only_matches_artistless():
    assert find(build_index(["Intro.mp3"]), "Intro", "Band") is None
    assert find(build_index(["Band - Intro.mp3"]), "Intro", "") is None
    assert find(build_index(["Intro.mp3"]), "Intro", "") == "Intro.mp3"


def test_feat_suffix_only_bridges_titles_when_the_guest_is_credited():
    idx = build_index(["Artist - Song (feat. Guest).mp3"])
    assert find(idx, "Song (feat. Guest)", "Artist") == "Artist - Song (feat. Guest).mp3"
    assert find(idx, "Song", "Artist", ["Artist", "Guest"]) == "Artist - Song (feat. Guest).mp3"
    assert find(idx, "Song", "Artist") is None


def test_audio_files_skips_playlists_and_missing_dirs(tmp_path):
    for name in ("b - two.mp3", "a - one.opus", "List.m3u8", "cover.jpg"):
        (tmp_path / name).write_text("")
    assert matching.audio_files(str(tmp_path)) == ["a - one.opus", "b - two.mp3"]
    assert matching.audio_files(str(tmp_path / "nope")) == []


def test_featured_version_does_not_match_solo_file():
    # A different recording: the solo "Song" on disk is not "Song (feat. B)".
    idx = build_index(["Artist A - Song.mp3"])
    assert find(idx, "Song (feat. Artist B)", "Artist A", ["Artist A", "Artist B"]) is None


def test_feat_dropped_from_spotify_title_still_matches():
    # Real case: Spotify dropped "(feat. X)" from the title after download, and X
    # is still credited, so it's the same recording.
    idx = build_index(["Culture Shock, Raphaella Mazaheri-Asadi - Have It All (feat. Raphaella).mp3"])
    assert find(idx, "Have It All", "Culture Shock", ["Culture Shock", "Raphaella"])
    # ...but not when the featured artist isn't credited on the Spotify side.
    assert find(idx, "Have It All", "Culture Shock", ["Culture Shock"]) is None
