"""YouTube (oEmbed) and Apple song links, the paths that replaced Odesli."""

from unittest.mock import AsyncMock

import pytest

from metadata import deezer, resolver


class FakeSession:
    def __init__(self, data: dict, status: int = 200):
        self.data, self.status, self.calls = data, status, []

    def get(self, url, params=None, **_kwargs):
        self.calls.append((url, params))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def json(self, content_type=None):
        return self.data


@pytest.fixture
def session(monkeypatch):
    def install(data, status=200):
        s = FakeSession(data, status)
        monkeypatch.setattr(resolver, "_get_session", AsyncMock(return_value=s))
        return s
    return install


@pytest.fixture
def find_track(monkeypatch):
    find = AsyncMock(return_value="14408104")
    monkeypatch.setattr(deezer, "find_track_id", find)
    return find


@pytest.mark.parametrize("url", [
    "https://music.youtube.com/watch?v=lYBUbBu4W08&si=abc",
    "https://www.youtube.com/watch?list=RD&v=lYBUbBu4W08",
    "https://youtu.be/lYBUbBu4W08?t=10",
])
async def test_topic_channel_track(session, find_track, url):
    s = session({"title": "Never Gonna Give You Up", "author_name": "Rick Astley - Topic"})
    assert await resolver.resolve_link(url) == ("track", "14408104")
    assert s.calls == [("https://www.youtube.com/oembed",
                        {"format": "json", "url": "https://www.youtube.com/watch?v=lYBUbBu4W08"})]
    find_track.assert_awaited_once_with("Rick Astley", "Never Gonna Give You Up")


async def test_music_video_title_is_split_and_cleaned(session, find_track):
    session({"title": "Nirvana - Smells Like Teen Spirit (Official Music Video)",
             "author_name": "NirvanaVEVO"})
    assert await resolver.resolve_link("https://www.youtube.com/watch?v=hTWKbfoikeg")
    find_track.assert_awaited_once_with("Nirvana", "Smells Like Teen Spirit")


async def test_topic_title_with_dash_is_not_split(session, find_track):
    session({"title": "Song - 2011 Mix", "author_name": "Band - Topic"})
    await resolver.resolve_link("https://music.youtube.com/watch?v=lYBUbBu4W08")
    find_track.assert_awaited_once_with("Band", "Song - 2011 Mix")


async def test_plain_video_uses_channel_as_artist(session, find_track):
    session({"title": "Blue Valentines [HD]", "author_name": "TomWaitsVEVO"})
    await resolver.resolve_link("https://www.youtube.com/watch?v=lYBUbBu4W08")
    find_track.assert_awaited_once_with("Tom Waits", "Blue Valentines")


async def test_youtube_http_error_is_a_miss(session, find_track):
    session({}, status=404)
    assert await resolver.resolve_link("https://youtu.be/lYBUbBu4W08") is None
    find_track.assert_not_awaited()


@pytest.mark.parametrize("url", [
    "https://music.apple.com/us/song/never-gonna-give-you-up/1559523359",
    "https://www.shazam.com/song/1559523359",
])
async def test_apple_song_link(session, find_track, url):
    s = session({"results": [{"wrapperType": "track", "artistName": "Rick Astley",
                              "trackName": "Never Gonna Give You Up"}]})
    assert await resolver.resolve_link(url) == ("track", "14408104")
    assert s.calls[0][1] == {"id": "1559523359", "entity": "song"}


@pytest.mark.parametrize("url", [
    "https://soundcloud.com/rick-astley-official/never-gonna-give-you-up-4",
    "https://music.youtube.com/playlist?list=OLAK5uy_nMr9h2VlS",
])
async def test_other_links_resolve_to_none_without_network(session, url):
    s = session({})
    assert await resolver.resolve_link(url) is None
    assert s.calls == []
