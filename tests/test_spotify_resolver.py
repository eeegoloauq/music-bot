import json
from unittest.mock import AsyncMock

import pytest

from metadata import deezer, resolver


def embed_page(entity: dict) -> str:
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return ('<html><script id="__NEXT_DATA__" type="application/json">'
            f"{json.dumps(data)}</script></html>")


# Trimmed from the real embed pages (September 2026): tracks carry an
# ``artists`` list, albums only a ``subtitle``.
TRACK = {"type": "track", "name": "Never Gonna Give You Up",
         "title": "Never Gonna Give You Up", "artists": [{"name": "Rick Astley"}]}
ALBUM = {"type": "album", "name": "The Dark Side of the Moon",
         "title": "The Dark Side of the Moon", "subtitle": "Pink Floyd"}
# What open.spotify.com/track/... now sends a browser user agent: no og: tags.
APP_SHELL = "<html><head><title>Spotify</title></head><body><div id=root></div></body></html>"


class FakeSession:
    def __init__(self, status: int, html: str):
        self.status, self.html, self.urls = status, html, []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def text(self):
        return self.html


@pytest.fixture
def session(monkeypatch):
    def install(status=200, html=""):
        s = FakeSession(status, html)
        monkeypatch.setattr(resolver, "_get_session", AsyncMock(return_value=s))
        return s
    return install


async def test_track_resolved_from_embed_page(monkeypatch, session):
    s = session(html=embed_page(TRACK))
    find = AsyncMock(return_value="14408104")
    monkeypatch.setattr(deezer, "find_track_id", find)
    got = await resolver._resolve_spotify("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=x")
    assert got == ("track", "14408104")
    assert s.urls == ["https://open.spotify.com/embed/track/4uLU6hMCjMI75M1A2tKUQC"]
    find.assert_awaited_once_with("Rick Astley", "Never Gonna Give You Up")


async def test_album_artist_comes_from_subtitle(monkeypatch, session):
    session(html=embed_page(ALBUM))
    find = AsyncMock(return_value="12114240")
    monkeypatch.setattr(deezer, "find_album_id", find)
    got = await resolver._resolve_spotify("https://open.spotify.com/intl-de/album/4LH4d3cOWNNsVw41Gqt2kv")
    assert got == ("album", "12114240")
    find.assert_awaited_once_with("Pink Floyd", "The Dark Side of the Moon")


@pytest.mark.parametrize("status,html", [
    (200, APP_SHELL),
    (200, embed_page({"type": "track", "name": "No Artist"})),
    (200, '<script id="__NEXT_DATA__" type="application/json">{not json</script>'),
    (404, embed_page(TRACK)),
])
async def test_unusable_page_is_a_miss_not_a_crash(monkeypatch, session, status, html):
    session(status=status, html=html)
    find = AsyncMock()
    monkeypatch.setattr(deezer, "find_track_id", find)
    assert await resolver._resolve_spotify("https://open.spotify.com/track/abc") is None
    find.assert_not_awaited()
