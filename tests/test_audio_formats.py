"""Every intake format remains visible to library search and deletion."""

import wave

import pytest
import config

from flows import common, library_actions as actions, state
import inline
import retagger
import uploads
from library.files import AUDIO_EXTS, TAGGED_AUDIO_EXTS, _find_existing_track


@pytest.mark.parametrize("extension", sorted(AUDIO_EXTS))
def test_intake_formats_visible_in_search_counts_and_delete(monkeypatch, tmp_path, extension):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / f"01 Song{extension.upper()}"
    track.write_bytes(b"audio")
    (album / "cover.jpg").write_bytes(b"cover")
    for module in (config, inline):
        monkeypatch.setattr(module, "MUSIC_DIR", str(tmp_path))

    assert uploads._classify(track.name) == "audio"
    assert inline._search_local_albums("artist")[0]["tracks"] == 1
    assert [hit["path"] for hit in inline._search_local_tracks("song")] == [str(track)]
    assert actions._count_audio(str(album)) == 1
    assert actions._collect_stats()["tracks"] == 1
    assert actions._check_delete_path(str(track.relative_to(tmp_path))) == (str(track), "")


async def test_wav_delete_preserves_the_other_track(monkeypatch, tmp_path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    wav = album / "01 Song.wav"
    wav.write_bytes(b"audio")
    other = album / "02 Other.flac"
    other.write_bytes(b"audio")
    monkeypatch.setattr(config, "MUSIC_DIR", str(tmp_path))

    async def scan(**_kwargs):
        return "scan scheduled"

    async def inline_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(common.asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(actions, "_trigger_scan", scan)
    monkeypatch.setattr(state, "_retag_session", None)
    monkeypatch.setattr(state, "_retag_invalidated_reason", None)
    result = await actions._delete_track(str(wav))

    assert result.startswith("Deleted: Artist — Album — Song")
    assert not wav.exists()
    assert other.exists()


def test_retag_only_lists_supported_tag_formats(tmp_path):
    for extension in AUDIO_EXTS:
        (tmp_path / f"01 Song{extension}").write_bytes(b"audio")

    assert {path.rsplit(".", 1)[-1] for path in retagger._list_audio_files(str(tmp_path))} == {
        extension[1:] for extension in TAGGED_AUDIO_EXTS
    }


def test_canonical_wav_filename_prevents_a_duplicate(tmp_path):
    path = tmp_path / "01 Song.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(b"\0\0" * 441)

    assert _find_existing_track(str(tmp_path), {"title": "Song", "trackNumber": 1}) == str(path)


async def test_album_browser_lists_wav_and_nested_audio(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from flows import catalog

    album = tmp_path / "Artist" / "Album"
    (album / "Disc 1").mkdir(parents=True)
    (album / "01 Song.wav").write_bytes(b"audio")
    (album / "Disc 1" / "02 Other.opus").write_bytes(b"audio")
    (album / "cover.jpg").write_bytes(b"art")
    monkeypatch.setattr(config, "MUSIC_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ALLOWED_USERS", [1])
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(data="album:open:chosen", answer=AsyncMock(), message=message)
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1),
                             effective_chat=SimpleNamespace(id=1))
    context = SimpleNamespace(user_data={"library_albums": {"chosen": (str(album), 1)}})

    await catalog._handle_album_callback(update, context)

    text = message.reply_text.call_args.args[0]
    assert "2 tracks" in text
    assert "01 Song.wav" in text
    assert "Disc 1/02 Other.opus" in text
    assert "cover.jpg" not in text
