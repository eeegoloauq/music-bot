"""Matching behavior in ``soulseek.matcher``: preseed pool reuse, the album
query ladder's early exit, and graceful degradation when searching breaks."""

import pytest

from soulseek import client as sc
from soulseek import matcher
from soulseek.client import SearchThrottledError

from conftest import install_fake_client, make_result

TRACK = {"artist": "Artist", "title": "Song", "duration": 200}


def forbid_search(monkeypatch):
    async def no_search(*args, **kwargs):
        raise AssertionError("search() must not be called")

    monkeypatch.setattr(sc, "search", no_search)


async def test_preseed_auto_pick_makes_zero_searches(monkeypatch):
    forbid_search(monkeypatch)
    preseed = [make_result("gooduser", "Music\\Artist - Album\\01 - Song.flac")]

    auto, alts = await matcher.find_track(TRACK, album_artist="Artist",
                                          preseed=preseed)

    assert auto is not None
    assert auto.result.username == "gooduser"


async def test_find_album_stops_ladder_on_complete_folder(monkeypatch):
    # The album title carries a combining mark ("й"), so fold fallbacks
    # exist in the ladder — a complete match on the primary must skip them.
    resp = [{
        "username": "fullpeer",
        "hasFreeUploadSlot": True,
        "uploadSpeed": 2_000_000,
        "queueLength": 0,
        "files": [
            {"filename": "M\\Артист - Мой Альбом\\01 - Первый.flac",
             "size": 30_000_000, "length": 200, "bitDepth": 16, "sampleRate": 44100},
            {"filename": "M\\Артист - Мой Альбом\\02 - Второй.flac",
             "size": 30_000_000, "length": 210, "bitDepth": 16, "sampleRate": 44100},
        ],
    }]
    calls = install_fake_client(monkeypatch, lambda i: resp)
    album = {
        "artist": "Артист", "title": "Мой Альбом",
        "tracks": [{"title": "Первый", "duration": 200},
                   {"title": "Второй", "duration": 210}],
    }

    best, alts, pool = await matcher.find_album(album)

    assert best is not None and best.missing_count == 0
    assert calls["n"] == 1  # only the primary query ran
    assert len(pool) == 2  # every surfaced file lands in the reuse pool


async def test_find_track_surfaces_throttling_not_no_match(monkeypatch):
    async def throttled(*args, **kwargs):
        raise SearchThrottledError("slskd kept rate-limiting searches")

    monkeypatch.setattr(sc, "search", throttled)

    # Empty pool: the caller must see the real reason, not "no match".
    with pytest.raises(SearchThrottledError):
        await matcher.find_track(TRACK)

    # With a preseed pool it degrades to pool-only matching instead of raising.
    preseed = [make_result("gooduser", "Music\\Artist - X\\01 - Song (live).flac")]
    await matcher.find_track(TRACK, preseed=preseed)


async def test_pool_only_mode_never_searches(monkeypatch):
    forbid_search(monkeypatch)
    # A queued no-slot peer holding the right file: identity-confident on
    # the match axis, so pool-only matching returns it without any search.
    r = make_result("slowpeer", "Music\\Artist - Album\\01 - Song.flac",
                    has_free_slot=False, upload_speed=0, queue_length=10)

    auto, alts = await matcher.find_track(TRACK, preseed=[r], allow_search=False)
    assert auto is not None or len(alts) > 0

    empty_auto, empty_alts = await matcher.find_track(TRACK, preseed=[],
                                                      allow_search=False)
    assert empty_auto is None and empty_alts == []
