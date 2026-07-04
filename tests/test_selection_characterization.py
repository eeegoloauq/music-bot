"""Characterization tests for the source-selection system.

These pin *current* behavior — including the failure modes documented in
docs/source-selection.md (marked ``F1``..``F5`` below) — so the redesign can
proceed in small commits with regressions visible immediately. An assertion
here flips only in the commit that deliberately fixes the corresponding
failure mode.
"""

import pytest

from soulseek import client as slskd_client
from soulseek import matcher, scorer
from soulseek.client import PeerFolder
from soulseek.downloader import _modal_quality, _pick_quality_locked
from soulseek.matcher import _is_acceptable_lossy
from soulseek.scorer import (
    _duration_score,
    _match_folder_to_tracks,
    _reliability_score,
    score_folder_results,
    score_track_results,
)

from conftest import install_fake_client, make_result


@pytest.fixture(autouse=True)
def default_quality_cap(monkeypatch):
    """Pin the cap to the documented defaults regardless of local env."""
    monkeypatch.setattr(scorer, "MAX_BIT_DEPTH", 24)
    monkeypatch.setattr(scorer, "MAX_SAMPLE_RATE_HZ", 96000)


# --- scoring math -----------------------------------------------------------

def test_duration_score_ladder():
    assert _duration_score(0) == 40.0
    assert _duration_score(5) == 30.0
    assert _duration_score(6) == 22.0
    assert _duration_score(10) == 10.0
    assert _duration_score(30) == 0.0
    assert _duration_score(31) is None  # past hard cutoff → excluded


def test_reliability_score_blend():
    assert _reliability_score(True, 10_000_000, 0) == 25.0
    assert _reliability_score(False, 0, 0) == 7.0
    assert _reliability_score(False, 0, 10) == -2.0
    assert _reliability_score(False, 0, 40) == -6.0  # penalty is capped
    assert _reliability_score(True, 50_000_000, 0) == 25.0  # speed bonus is capped


def _score_one(filename, **kw):
    results = [make_result("peer", filename, upload_speed=10_000_000, **kw)]
    return score_track_results(results, track_artist="Artist",
                               track_title="Song", track_duration=200)


def test_perfect_candidate_scores_90():
    scored = _score_one("Music\\Artist - Album\\01 - Song.flac")
    assert scored[0].score == 90.0  # 40 duration + 10 quality + 25 reliability + 15 name


def test_version_penalties():
    live = _score_one("Music\\Artist - Album\\01 - Song (Live).flac")
    assert live[0].score == 27.0  # ×0.30
    remaster = _score_one("Music\\Artist - Album\\01 - Song (Remastered).flac")
    assert remaster[0].score == 67.5  # ×0.75


def test_artist_gate_drops_unrelated_paths():
    results = [make_result("peer", "Sounds\\Birds Vol 2\\07 - Barnacle.flac")]
    assert score_track_results(results, track_artist="Radiohead",
                               track_title="Barnacle", track_duration=200) == []
    # ...but artists with no significant word (all <3 chars) bypass the gate
    assert score_track_results(results, track_artist="21",
                               track_title="Barnacle", track_duration=200) != []


def test_quality_cap_excludes_hires():
    over = _score_one("Music\\Artist - Album\\01 - Song.flac",
                      bit_depth=24, sample_rate=192_000)
    assert over == []
    within = _score_one("Music\\Artist - Album\\01 - Song.flac",
                        bit_depth=24, sample_rate=96_000)
    assert within[0].score == 90.0


def test_f1_basename_dedupe_hides_other_peers_copies():
    """F1: dedupe keeps only the best-scored copy of a basename — the
    same-file-other-peer entries that transfer retries need are dropped."""
    best = make_result("fastpeer", "Music\\Artist - Album\\01 - Song.flac",
                       upload_speed=10_000_000)
    spare = make_result("slowpeer", "Stash\\Artist - Album\\01 - Song.flac",
                        has_free_slot=False, upload_speed=0)
    scored = score_track_results([best, spare], track_artist="Artist",
                                 track_title="Song", track_duration=200)
    assert len(scored) == 1
    assert scored[0].result.username == "fastpeer"


def test_f3_missing_duration_caps_below_auto():
    """F3: a peer that omits length can never reach TRACK_AUTO_THRESHOLD —
    12 neutral duration + 10 + 25 + 15 = 62 < 70, however exact the name."""
    scored = _score_one("Music\\Artist - Album\\01 - Song.flac", length=None)
    assert scored[0].score == 62.0
    assert scored[0].score < matcher.TRACK_AUTO_THRESHOLD


# --- folder matching --------------------------------------------------------

def _folder(username, files, **peer_kw):
    return PeerFolder(username=username, directory=files[0].directory,
                      files=files, **peer_kw)


ALBUM_KW = dict(track_durations=[200, 210], track_titles=["One", "Two"],
                album_artist="Artist", album_title="Album")


def test_complete_folder_beats_partial_from_better_peer():
    partial_good_peer = _folder("fastpeer", [
        make_result("fastpeer", "M\\Artist - Album\\01 - One.flac",
                    length=200, upload_speed=10_000_000),
    ], has_free_slot=True, upload_speed=10_000_000)
    complete_bad_peer = _folder("slowpeer", [
        make_result("slowpeer", "S\\Artist - Album\\01 - One.flac",
                    length=200, has_free_slot=False, upload_speed=0),
        make_result("slowpeer", "S\\Artist - Album\\02 - Two.flac",
                    length=210, has_free_slot=False, upload_speed=0),
    ], has_free_slot=False, upload_speed=0)

    scored = score_folder_results([partial_good_peer, complete_bad_peer], **ALBUM_KW)

    assert [f.folder.username for f in scored] == ["slowpeer", "fastpeer"]
    assert scored[0].score == 67.0  # 50 coverage + 10 quality + 7 reliability
    assert scored[1].score == 3.0   # missing penalty floors a 50%-coverage folder


def test_folder_with_any_overcap_file_is_dropped():
    folder = _folder("peer", [
        make_result("peer", "M\\Artist - Album\\01 - One.flac", length=200),
        make_result("peer", "M\\Artist - Album\\02 - Two.flac", length=210,
                    bit_depth=24, sample_rate=192_000),
    ], has_free_slot=True)
    assert score_folder_results([folder], **ALBUM_KW) == []


def test_matched_files_stay_parallel_to_track_list():
    folder = _folder("peer", [
        make_result("peer", "M\\A\\01.flac", length=200),
        make_result("peer", "M\\A\\03.flac", length=220),
    ])
    matched, missing = _match_folder_to_tracks(
        folder, [200, 210, 220], ["One", "Two", "Three"])
    assert missing == 1
    assert matched[0] is not None and matched[1] is None and matched[2] is not None


def test_f4_closer_duration_beats_exact_title():
    """F4: assignment is duration-first; title overlap only breaks exact
    ties — so two similar-length tracks swap files."""
    folder = _folder("peer", [
        make_result("peer", "M\\A\\01 - Intro.flac", length=102),
        make_result("peer", "M\\A\\02 - Song.flac", length=100),
    ])
    matched, missing = _match_folder_to_tracks(folder, [100, 102], ["Intro", "Song"])
    assert missing == 0
    assert matched[0].basename == "02 - Song.flac"   # "Intro" got the Song file
    assert matched[1].basename == "01 - Intro.flac"  # and vice versa


def test_title_breaks_exact_duration_ties():
    folder = _folder("peer", [
        make_result("peer", "M\\A\\01 - One.flac", length=205),
        make_result("peer", "M\\A\\02 - Two.flac", length=205),
    ])
    matched, _ = _match_folder_to_tracks(folder, [205], ["Two"])
    assert matched[0].basename == "02 - Two.flac"


# --- query building ---------------------------------------------------------

def test_clean_query():
    assert matcher._clean_query("Album (Deluxe Version)") == "Album"
    assert matcher._clean_query("AC/DC") == "AC DC"
    assert matcher._clean_query("A$AP Rocky") == "A$AP Rocky"  # $ is load-bearing


def test_album_query_ladder_for_diacritics():
    primary, fallbacks = matcher._build_album_queries("Stromae", "Racine carrée")
    assert primary == "Stromae Racine carrée"
    assert fallbacks == ["Racine carrée", "Stromae Racine carree", "Racine carree"]


def test_junk_title_falls_back_to_artist_only():
    primary, fallbacks = matcher._build_album_queries("Artist", "●●● EP ●●●")
    assert fallbacks[-1] == "Artist"


def test_token_rescue():
    assert matcher._token_rescue("●E.leCtroN...mUS1c●") == "leCtroN mUS1c"


# --- lossy fallback gate ----------------------------------------------------

def test_f5_mp3_floor_rejects_kbps_scale_bitrates():
    """F5: slskd reports bitRate in kbps (320 for CBR mp3), but the floor is
    256_000 — every real-world mp3 fails it, so the fallback is m4a-only."""
    mp3_320 = make_result("peer", "M\\A\\01 - Song.mp3", bit_rate=320)
    assert not _is_acceptable_lossy(mp3_320)
    mp3_unreported = make_result("peer", "M\\A\\01 - Song.mp3", bit_rate=None)
    assert not _is_acceptable_lossy(mp3_unreported)
    # what the code currently expects the field to look like:
    assert _is_acceptable_lossy(make_result("peer", "M\\A\\01.mp3", bit_rate=320_000))
    # m4a has no floor (might be ALAC); flac is never "acceptable lossy"
    assert _is_acceptable_lossy(make_result("peer", "M\\A\\01.m4a"))
    assert not _is_acceptable_lossy(make_result("peer", "M\\A\\01.flac"))


# --- downloader quality lock ------------------------------------------------

def test_modal_quality_and_lock():
    cd = [make_result("p", f"M\\A\\{i}.flac") for i in range(2)]
    hires = make_result("p", "M\\A\\3.flac", bit_depth=24, sample_rate=96_000)
    assert _modal_quality(cd + [hires]) == (16, 44100)
    unreported = make_result("p", "M\\A\\1.flac", bit_depth=None, sample_rate=None)
    assert _modal_quality([unreported]) == (None, None)

    assert _pick_quality_locked([hires, cd[0]], (16, 44100)) is cd[0]
    assert _pick_quality_locked([hires], (16, 44100)) is hires  # fallback: first pick
    assert _pick_quality_locked([hires, cd[0]], None) is hires


# --- pool assembly ----------------------------------------------------------

def test_group_by_folder():
    a1 = make_result("peer", "M\\Album\\01.flac")
    a2 = make_result("peer", "M\\Album\\02.flac")
    b1 = make_result("peer", "M\\Other\\01.flac")
    folders = slskd_client.group_by_folder([a1, a2, b1])
    assert len(folders) == 2
    by_dir = {f.directory: f for f in folders}
    assert len(by_dir["M\\Album"].files) == 2


async def test_f2_fallback_query_files_for_known_folder_are_dropped(monkeypatch):
    """F2: find_album keeps the *first* sighting of a folder wholesale — a
    fallback query that surfaces more files from the same folder is ignored,
    so a complete folder scores as partial and the pool stays thin."""
    def peer_response(files):
        return [{
            "username": "onepeer", "hasFreeUploadSlot": True,
            "uploadSpeed": 2_000_000, "queueLength": 0,
            "files": [{"filename": f"M\\Artist - Album\\{name}",
                       "size": 30_000_000, "length": length,
                       "bitDepth": 16, "sampleRate": 44100}
                      for name, length in files],
        }]

    # Primary query matches one basename; the title-only fallback matches the
    # whole folder (Soulseek matches terms against full paths).
    responses = {
        0: peer_response([("01 - One.flac", 200)]),
        1: peer_response([("01 - One.flac", 200), ("02 - Two.flac", 210)]),
    }
    calls = install_fake_client(monkeypatch, lambda i: responses[i])
    album = {"artist": "Artist", "title": "Album",
             "tracks": [{"title": "One", "duration": 200},
                        {"title": "Two", "duration": 210}]}

    best, alts, pool = await matcher.find_album(album)

    assert calls["n"] == 2  # incomplete result did escalate the ladder
    assert best.missing_count == 1  # fallback's fuller file-list was discarded
    assert len(pool) == 1
