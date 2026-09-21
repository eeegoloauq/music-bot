import asyncio
from types import SimpleNamespace

import pytest

import bot
import retagger


class RecordingMessage:
    def __init__(self):
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **_kwargs):
        self.replies.append(text)
        return self

    async def edit_text(self, text, **_kwargs):
        self.edits.append(text)
        return self


@pytest.fixture(autouse=True)
def reset_retag_state(monkeypatch):
    monkeypatch.setattr(bot, "_retag_session", None)
    monkeypatch.setattr(bot, "_retag_invalidated_reason", None)
    monkeypatch.setattr(bot, "_retag_in_progress", False)


async def test_successful_track_download_invalidates_retag_session(
        monkeypatch, tmp_path):
    bot._retag_session = {"plans": [], "expire_at": float("inf")}
    track = {"artist": "Artist", "title": "Track"}
    album = {"artist": "Artist", "title": "Album", "tracks": [track]}
    target = tmp_path / "Artist" / "Album" / "01 - Track.flac"

    async def fake_fetch(_track_id):
        return track, album

    async def no_op(*_args, **_kwargs):
        return None

    async def fake_download(*_args, **_kwargs):
        return str(target), True, "FLAC"

    async def fake_share(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "_download_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(bot.metadata, "fetch_single_track", fake_fetch)
    monkeypatch.setattr(bot.metadata, "enrich_genres", no_op)
    monkeypatch.setattr(bot.soulseek, "download_single_track", fake_download)
    monkeypatch.setattr(bot, "_trigger_scan", no_op)
    monkeypatch.setattr(bot, "_try_share_album", fake_share)
    monkeypatch.setattr(bot, "_send_result", no_op)

    await bot._do_download_track(SimpleNamespace(chat_id=1), RecordingMessage(), "42")

    assert bot._retag_session is None
    assert bot._retag_invalidated_reason == "track downloaded"


async def test_delete_invalidates_retag_session(monkeypatch, tmp_path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    bot._retag_session = {"plans": [], "expire_at": float("inf")}

    async def fake_scan(*_args, **_kwargs):
        return "scan scheduled"

    async def inline_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(bot, "MUSIC_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "_trigger_scan", fake_scan)
    monkeypatch.setattr(bot.asyncio, "to_thread", inline_to_thread)
    message = RecordingMessage()

    await bot._handle_delete(SimpleNamespace(message=message), "Artist/Album")

    assert not album.exists()
    assert bot._retag_session is None
    assert bot._retag_invalidated_reason == "album deleted"


async def test_confirm_reports_invalidation_reason(monkeypatch):
    bot._retag_invalidated_reason = "upload imported"
    monkeypatch.setattr(bot, "ALLOWED_USERS", {1})
    message = RecordingMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
    )

    await bot.cmd_retag(update, SimpleNamespace(args=["confirm"]))

    assert message.replies == [
        "Library changed since the scan (upload imported) — run /retag again."
    ]


async def test_apply_skips_plan_when_folder_disappeared(tmp_path):
    missing = tmp_path / "Artist" / "Gone"
    plan = retagger.AlbumPlan(
        folder=str(missing),
        artist_dir="Artist",
        album_dir="Gone",
        files=[str(missing / "01.flac"), str(missing / "02.flac")],
        changes=["tags"],
    )

    stats = await retagger.run_apply([plan])

    assert stats == {
        "albums_planned": 1,
        "files_written": 0,
        "files_skipped": 2,
        "failed": [],
    }
    assert plan.files_skipped == 2
