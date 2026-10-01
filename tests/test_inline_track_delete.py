"""Inline ``del``: single tracks listed next to albums, one delete flow."""

import os
from types import SimpleNamespace

import pytest

import bot
import inline


class RecordingMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **_kwargs):
        self.replies.append(text)
        return self


@pytest.fixture
def library(monkeypatch, tmp_path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    for name in ("01 Song One.flac", "02 - Other Song.mp3", "cover.jpg"):
        (album / name).write_bytes(b"x")
    single = tmp_path / "Solo" / "Single"
    single.mkdir(parents=True)
    (single / "01 Lonely.flac").write_bytes(b"x")
    (single / "cover.jpg").write_bytes(b"x")

    async def fake_scan(*_args, **_kwargs):
        return "scan scheduled"

    async def inline_to_thread(func, *args):
        return func(*args)

    for mod in (bot, inline):
        monkeypatch.setattr(mod, "MUSIC_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "_trigger_scan", fake_scan)
    monkeypatch.setattr(bot.asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(bot, "_retag_session", {"plans": [], "expire_at": float("inf")})
    monkeypatch.setattr(bot, "_retag_invalidated_reason", None)
    return tmp_path


def test_tracks_match_by_title_or_artist_and_title(library):
    by_title = inline._search_local_tracks("song one")
    assert [t["title"] for t in by_title] == ["Song One"]
    by_artist = inline._search_local_tracks("artist other")
    assert [t["title"] for t in by_artist] == ["Other Song"]  # "02 - " stripped
    assert inline._search_local_tracks("cover") == []  # only audio files
    assert len(inline._search_local_tracks("o", limit=2)) == 2


async def test_inline_delete_lists_albums_then_tracks(monkeypatch, library):
    async def no_covers(_pairs):
        return {}

    monkeypatch.setattr(inline, "_fetch_album_covers", no_covers)
    answered = []

    async def answer(results, **_kwargs):
        answered.extend(results)

    update = SimpleNamespace(inline_query=SimpleNamespace(answer=answer))
    await inline._inline_delete(update, "artist")
    messages = [r.input_message_content.message_text for r in answered]
    album = os.path.join("Artist", "Album")
    assert messages[0] == f"delete:{album}"
    assert set(messages[1:]) == {f"delete:{os.path.join(album, '01 Song One.flac')}",
                                 f"delete:{os.path.join(album, '02 - Other Song.mp3')}"}


async def test_deleting_a_track_keeps_the_rest_of_the_album(library):
    message = RecordingMessage()
    await bot._handle_delete(SimpleNamespace(message=message), "Artist/Album/01 Song One.flac")
    album = library / "Artist" / "Album"
    assert not (album / "01 Song One.flac").exists()
    assert (album / "02 - Other Song.mp3").exists() and (album / "cover.jpg").exists()
    assert message.replies[0].startswith("Deleted: Artist — Song One")
    assert bot._retag_session is None and bot._retag_invalidated_reason == "track deleted"


async def test_last_track_takes_album_and_empty_artist_folder(library):
    message = RecordingMessage()
    await bot._handle_delete(SimpleNamespace(message=message), "Solo/Single/01 Lonely.flac")
    assert not (library / "Solo").exists()  # cover-only album and empty artist both gone
    assert library.exists()


async def test_track_directly_under_artist_never_climbs_to_the_root(library):
    loose = library / "Loose"
    loose.mkdir()
    (loose / "Only.flac").write_bytes(b"x")
    await bot._handle_delete(SimpleNamespace(message=RecordingMessage()), "Loose/Only.flac")
    assert not (loose / "Only.flac").exists()
    assert loose.exists() and library.exists()


async def test_non_audio_file_is_not_deleted(library):
    message = RecordingMessage()
    await bot._handle_delete(SimpleNamespace(message=message), "Artist/Album/cover.jpg")
    assert (library / "Artist" / "Album" / "cover.jpg").exists()
    assert message.replies == ["Not found: Artist/Album/cover.jpg"]


async def test_track_path_outside_the_library_is_rejected(library):
    outside = library.parent / "outside.flac"
    outside.write_bytes(b"x")
    message = RecordingMessage()
    await bot._handle_delete(SimpleNamespace(message=message), "../outside.flac")
    assert outside.exists()
    assert message.replies == ["Invalid path."]
