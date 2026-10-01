"""Inline ``del``: single tracks listed next to albums, one delete flow that
asks before it deletes."""

import os
from types import SimpleNamespace

import pytest

import bot
import inline


class RecordingMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **_kwargs):
        self.replies.append((text, reply_markup))
        return self


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.toasts = []
        self.text = None
        self.markup_removed = False

    async def answer(self, text=None, **_kwargs):
        self.toasts.append(text)

    async def edit_message_text(self, text, **_kwargs):
        self.text = text

    async def edit_message_reply_markup(self, reply_markup=None, **_kwargs):
        self.markup_removed = reply_markup is None


@pytest.fixture
def library(monkeypatch, tmp_path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    for name in ("01 Song One.flac", "02 - Other Song.mp3", "1-03 Disc Song.flac", "cover.jpg"):
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
    monkeypatch.setattr(bot, "ALLOWED_USERS", {1})
    monkeypatch.setattr(bot, "_pending_deletes", {})
    monkeypatch.setattr(bot, "_trigger_scan", fake_scan)
    monkeypatch.setattr(bot.asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(bot, "_retag_session", {"plans": [], "expire_at": float("inf")})
    monkeypatch.setattr(bot, "_retag_invalidated_reason", None)
    return tmp_path


async def ask(rel_path):
    """Send ``delete:<rel_path>``; return (reply text, {action: callback data})."""
    message = RecordingMessage()
    await bot._handle_delete(SimpleNamespace(message=message), rel_path)
    [(text, markup)] = message.replies
    buttons = {}
    if markup is not None:
        for button in markup.inline_keyboard[0]:
            buttons[button.callback_data.split(":")[1]] = button.callback_data
    return text, buttons


async def tap(data):
    query = FakeQuery(data)
    await bot._handle_delete_callback(SimpleNamespace(callback_query=query), None)
    return query


def test_tracks_match_by_title_or_artist_and_title(library):
    by_title = inline._search_local_tracks("song one")
    assert [t["title"] for t in by_title] == ["Song One"]
    by_artist = inline._search_local_tracks("artist other")
    assert [t["title"] for t in by_artist] == ["Other Song"]  # "02 - " stripped
    by_disc = inline._search_local_tracks("artist disc song")
    assert [t["title"] for t in by_disc] == ["Disc Song"]  # "1-03 " stripped
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
    assert set(messages[1:]) == {
        f"delete:{os.path.join(album, name)}"
        for name in ("01 Song One.flac", "02 - Other Song.mp3", "1-03 Disc Song.flac")}


async def test_delete_asks_first_and_deletes_on_confirm(library):
    track = library / "Artist" / "Album" / "01 Song One.flac"
    text, buttons = await ask("Artist/Album/01 Song One.flac")
    assert text == "Delete Artist — Album — Song One?"
    assert track.exists()

    query = await tap(buttons["yes"])
    assert not track.exists()
    assert (library / "Artist" / "Album" / "02 - Other Song.mp3").exists()
    assert query.text.startswith("Deleted: Artist — Album — Song One")
    assert bot._retag_session is None and bot._retag_invalidated_reason == "track deleted"


async def test_album_prompt_counts_tracks(library):
    text, _ = await ask("Artist/Album")
    assert text == "Delete Artist — Album (3 tracks)?"


async def test_cancel_keeps_the_files(library):
    _, buttons = await ask("Artist/Album")
    query = await tap(buttons["no"])
    assert query.text == "Cancelled."
    assert (library / "Artist" / "Album" / "01 Song One.flac").exists()
    assert bot._retag_session is not None


async def test_second_tap_does_nothing(library):
    _, buttons = await ask("Artist/Album/01 Song One.flac")
    await tap(buttons["yes"])
    (library / "Artist" / "Album" / "01 Song One.flac").write_bytes(b"x")  # re-downloaded
    query = await tap(buttons["yes"])
    assert (library / "Artist" / "Album" / "01 Song One.flac").exists()
    assert query.toasts == ["Prompt expired."] and query.markup_removed
    assert query.text is None


async def test_confirm_checks_the_path_again(library):
    _, buttons = await ask("Artist/Album/01 Song One.flac")
    (library / "Artist" / "Album" / "01 Song One.flac").unlink()
    query = await tap(buttons["yes"])
    assert query.text == "Not found: Artist/Album/01 Song One.flac"


async def test_other_users_cannot_confirm(library):
    _, buttons = await ask("Artist/Album")
    query = FakeQuery(buttons["yes"])
    query.from_user = SimpleNamespace(id=2)
    await bot._handle_delete_callback(SimpleNamespace(callback_query=query), None)
    assert (library / "Artist" / "Album").exists()
    assert query.toasts == ["Not allowed."]


async def test_last_track_takes_album_and_empty_artist_folder(library):
    await bot._delete_track(str(library / "Solo" / "Single" / "01 Lonely.flac"))
    assert not (library / "Solo").exists()  # cover-only album and empty artist both gone
    assert library.exists()


async def test_audio_in_a_nested_folder_keeps_the_album(library):
    disc = library / "Solo" / "Single" / "CD2"
    disc.mkdir()
    (disc / "01 Bonus.flac").write_bytes(b"x")
    await bot._delete_track(str(library / "Solo" / "Single" / "01 Lonely.flac"))
    assert (disc / "01 Bonus.flac").exists()


async def test_unknown_files_keep_the_album(library):
    (library / "Solo" / "Single" / "02 Lonely (demo).wav").write_bytes(b"x")
    await bot._delete_track(str(library / "Solo" / "Single" / "01 Lonely.flac"))
    assert (library / "Solo" / "Single" / "02 Lonely (demo).wav").exists()


async def test_track_prompt_never_deletes_a_folder(library):
    track = library / "Artist" / "Album" / "01 Song One.flac"
    _, buttons = await ask("Artist/Album/01 Song One.flac")
    track.unlink()
    track.mkdir()
    (track / "01 Kept.flac").write_bytes(b"x")
    query = await tap(buttons["yes"])
    assert (track / "01 Kept.flac").exists()
    assert query.text.startswith("Changed since the question")


async def test_linked_album_folder_is_left_alone(library):
    (library / "Solo" / "Linked").symlink_to(library / "Solo" / "Single")
    message = await bot._delete_track(str(library / "Solo" / "Linked" / "01 Lonely.flac"))
    assert message.startswith("Deleted: Solo — Linked — Lonely")
    assert (library / "Solo" / "Linked").is_symlink()
    assert (library / "Solo" / "Single" / "cover.jpg").exists()


async def test_track_directly_under_artist_never_climbs_to_the_root(library):
    loose = library / "Loose"
    loose.mkdir()
    (loose / "Only.flac").write_bytes(b"x")
    await bot._delete_track(str(loose / "Only.flac"))
    assert not (loose / "Only.flac").exists()
    assert loose.exists() and library.exists()


async def test_non_audio_file_is_refused(library):
    text, buttons = await ask("Artist/Album/cover.jpg")
    assert text == "Not found: Artist/Album/cover.jpg" and not buttons


async def test_track_path_outside_the_library_is_rejected(library):
    outside = library.parent / "outside.flac"
    outside.write_bytes(b"x")
    text, buttons = await ask("../outside.flac")
    assert text == "Invalid path." and not buttons
    assert outside.exists()
