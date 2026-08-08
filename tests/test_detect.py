"""Tests for automatic source/track assignment.

The rule under test throughout: guess when the evidence is clear, decline when
it is not. A wrong pairing is far more expensive than an empty row, because an
empty row is visible and a wrong one only shows up as English dialogue over
the wrong scene twenty minutes into a render.
"""
from opdub.detect import (
    assign_multitrack,
    assign_separate,
    episode_key,
    file_language,
    stream_language,
)


# --------------------------------------------------------------- episodes ---

def test_episode_from_word_marker():
    # The real library on this machine. The 1999 must not win over the 313.
    assert episode_key(
        "One Piece 1999 Episode 313 Peace Interrupted A Navy Vice Admiral.mp4"
    ) == (None, 313)


def test_episode_ignores_duplicate_marker():
    assert episode_key(
        "One Piece 1999 Episode 313 Peace Interrupted A Navy Vice Admiral(1).mp4"
    ) == (None, 313)


def test_season_episode_form():
    assert episode_key("One Pace - S20E01 - Post-Enies Lobby 01.mp4") == (20, 1)
    assert episode_key("One.Piece.S21E1071.1080p.WEB.mkv") == (21, 1071)


def test_anime_release_style():
    assert episode_key("[SubsPlease] One Piece - 1071 (1080p) [A1B2C3D4].mkv") == (None, 1071)
    assert episode_key("[Group] Show - 313v2 [720p].mkv") == (None, 313)


def test_short_forms():
    assert episode_key("OnePiece_Ep0313_eng.mp4") == (None, 313)
    assert episode_key("Ep.64 - Whisky Peak.mkv") == (None, 64)
    assert episode_key("Show #313.mkv") == (None, 313)
    assert episode_key("Show E313.mkv") == (None, 313)


def test_bare_trailing_number():
    assert episode_key("313 - A Town That Welcomes Pirates.mkv") == (None, 313)
    assert episode_key("src_00.mkv") == (None, 0)


def test_resolution_and_year_are_not_episodes():
    assert episode_key("Show 2003 1080p x264.mkv") is None


def test_no_number_at_all():
    assert episode_key("opening theme.mkv") is None


# --------------------------------------------------------------- language ---

def test_stream_language_from_tag():
    assert stream_language({"index": 1, "language": "jpn"})[0] == "jpn"
    assert stream_language({"index": 2, "language": "en"})[0] == "eng"


def test_stream_language_from_title_when_untagged():
    assert stream_language({"index": 2, "language": None,
                            "title": "English Dub"})[0] == "eng"
    assert stream_language({"index": 1, "language": None,
                            "title": "Japanese"})[0] == "jpn"


def test_untagged_untitled_stream_is_unknown():
    assert stream_language({"index": 1, "language": None, "title": None})[0] is None


def test_file_language_prefers_stream_tag_over_filename():
    # Filename says eng, the stream says jpn. The stream wins: a mislabelled
    # filename is exactly the mistake this is meant to survive.
    pr = {"name": "Episode 313.eng.mp4",
          "audio": [{"index": 1, "language": "jpn", "title": None}]}
    assert file_language(pr)[0] == "jpn"


def test_file_language_falls_back_to_filename():
    pr = {"name": "Episode 313.eng.mp4",
          "audio": [{"index": 1, "language": None, "title": None}]}
    assert file_language(pr)[0] == "eng"


# ------------------------------------------------------- separate  files ----

def _f(name, lang=None, title=None, path=None):
    return {"path": path or f"/input/sources/{name}", "name": name, "error": None,
            "audio": [{"index": 1, "language": lang, "title": title}]}


def test_separate_pairs_by_tag():
    r = assign_separate([
        _f("One Piece 1999 Episode 313 Title.mp4", lang="eng"),
        _f("One Piece 1999 Episode 313 Title(1).mp4", lang="jpn"),
        _f("One Piece 1999 Episode 314 Title.mp4", lang="eng"),
        _f("One Piece 1999 Episode 314 Title(1).mp4", lang="jpn"),
    ])
    assert len(r["pairs"]) == 2
    assert r["unmatched"] == []
    p = r["pairs"][0]
    assert p["episode"] == 313
    assert p["jpn"]["path"].endswith("Title(1).mp4")
    assert p["dub"]["path"].endswith("Title.mp4")


def test_separate_infers_the_second_of_a_pair():
    # Only one of the two carries a tag; the other must be its counterpart.
    r = assign_separate([
        _f("Ep 313.mp4", lang="jpn"),
        _f("Ep 313 (1).mp4"),
    ])
    assert len(r["pairs"]) == 1
    assert r["pairs"][0]["dub"]["path"].endswith("Ep 313 (1).mp4")


def test_separate_declines_when_neither_file_is_identifiable():
    # Two files, same episode, no tags and no language words: which is which
    # is unknowable, so both are left for the operator.
    r = assign_separate([_f("Ep 313.mp4"), _f("Ep 313 (1).mp4")])
    assert r["pairs"] == []
    assert len(r["unmatched"]) == 2
    assert "cannot tell which language" in r["unmatched"][0]["why"]


def test_separate_leaves_an_unpartnered_episode():
    r = assign_separate([_f("Ep 313.jpn.mp4", lang="jpn")])
    assert r["pairs"] == []
    assert "no counterpart" in r["unmatched"][0]["why"]


def test_separate_reports_files_without_episode_numbers():
    r = assign_separate([_f("bonus feature.mkv", lang="jpn")])
    assert r["pairs"] == []
    assert "no episode number" in r["unmatched"][0]["why"]


def test_separate_uses_filename_language_words():
    r = assign_separate([
        _f("One Piece - 313 [Japanese].mkv"),
        _f("One Piece - 313 [English].mkv"),
    ])
    assert len(r["pairs"]) == 1
    assert r["pairs"][0]["jpn"]["path"].endswith("[Japanese].mkv")


# --------------------------------------------------------- multitrack -------

def _mt(name, streams):
    return {"path": f"/input/sources/{name}", "name": name, "error": None,
            "audio": streams}


def test_multitrack_identifies_both_tracks():
    r = assign_multitrack([_mt("src_00.mkv", [
        {"index": 1, "language": "jpn", "title": "Japanese"},
        {"index": 2, "language": "eng", "title": "English Dub"},
    ])])
    assert len(r["pairs"]) == 1
    p = r["pairs"][0]
    assert p["jpn"]["stream"] == 1 and p["dub"]["stream"] == 2
    assert p["jpn"]["path"] == p["dub"]["path"]


def test_multitrack_uses_titles_when_untagged():
    r = assign_multitrack([_mt("Episode 313.mkv", [
        {"index": 1, "language": None, "title": "Japanese 2.0"},
        {"index": 2, "language": None, "title": "English Dub 5.1"},
    ])])
    assert len(r["pairs"]) == 1


def test_multitrack_declines_when_a_track_is_unidentifiable():
    r = assign_multitrack([_mt("Episode 313.mkv", [
        {"index": 1, "language": None, "title": None},
        {"index": 2, "language": None, "title": None},
    ])])
    assert r["pairs"] == []
    assert "could not identify" in r["unmatched"][0]["why"]


def test_multitrack_rejects_single_track_files():
    r = assign_multitrack([_mt("Episode 313.mkv", [
        {"index": 1, "language": "jpn", "title": None},
    ])])
    assert r["pairs"] == []
    assert "at least two" in r["unmatched"][0]["why"]
