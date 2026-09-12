from unittest.mock import AsyncMock

import pytest

from metadata import api, deezer


def track(number):
    return {"id": number, "title": f"Track {number}"}


@pytest.mark.parametrize("count,page_size", [(1, 100), (43, 25), (143, 100)])
async def test_full_album_reaches_adapter(monkeypatch, count, page_size):
    tracks = [track(i) for i in range(1, count + 1)]

    async def get(path, params=None):
        if path == "/album/42":
            return {"id": 42, "nb_tracks": count,
                    "tracks": {"data": tracks[:25]}}
        assert path == "/album/42/tracks"
        start = params["index"]
        end = start + page_size
        return {"data": tracks[start:end], "total": count,
                "next": "https://api.deezer.com/album/42/tracks" if end < count else None}

    monkeypatch.setattr(deezer, "_get", get)
    enrich = AsyncMock(side_effect=lambda tid: {"id": tid})
    monkeypatch.setattr(deezer, "get_track", enrich)
    album = await api.fetch_album("42")
    assert album["numberOfTracks"] == count
    assert [t["id"] for t in album["tracks"]] == [str(t["id"]) for t in tracks]
    assert enrich.await_count == count


@pytest.mark.parametrize("pages,message", [
    ([{"data": [track(1)]}], "Incomplete"),
    ([{"data": [track(1), track(2), track(3)]}], "Inconsistent"),
    ([{"data": [], "next": "next"}], "Inconsistent"),
    ([{"data": [track(1)], "next": "next"}, {"data": [track(1)]}], "Duplicate"),
])
async def test_invalid_lists_fail_before_enrichment(monkeypatch, pages, message):
    get = AsyncMock(side_effect=[{"nb_tracks": 2}, *pages])
    enrich = AsyncMock()
    monkeypatch.setattr(deezer, "_get", get)
    monkeypatch.setattr(deezer, "get_track", enrich)
    with pytest.raises(deezer.DeezerError, match=message):
        await api.fetch_album("42")
    enrich.assert_not_awaited()


async def test_page_failure_is_not_a_partial_album(monkeypatch):
    get = AsyncMock(side_effect=[
        {"nb_tracks": 2},
        {"data": [track(1)], "next": "next"},
        deezer.DeezerError("HTTP 503"),
    ])
    monkeypatch.setattr(deezer, "_get", get)
    with pytest.raises(deezer.DeezerError, match="HTTP 503"):
        await api.fetch_album("42")


async def test_cover_does_not_request_tracks(monkeypatch):
    get = AsyncMock(return_value={"cover_xl": "https://example.com/cover.jpg"})
    monkeypatch.setattr(deezer, "_get", get)
    assert await api.fetch_cover_url("42") == "https://example.com/cover.jpg"
    get.assert_awaited_once_with("/album/42")
