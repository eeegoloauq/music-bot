import asyncio
from types import SimpleNamespace

import pytest
import config

import metadata
import providers
from flows import common, downloads, handlers, library_actions, state
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
    monkeypatch.setattr(state, "_retag_session", None)
    monkeypatch.setattr(state, "_retag_invalidated_reason", None)
    monkeypatch.setattr(state, "_retag_in_progress", False)


async def test_successful_track_download_invalidates_retag_session(
        monkeypatch, tmp_path):
    state._retag_session = {"plans": [], "expire_at": float("inf")}
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

    monkeypatch.setattr(state, "_download_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(metadata, "fetch_single_track", fake_fetch)
    monkeypatch.setattr(metadata, "enrich_genres", no_op)
    monkeypatch.setattr(providers, "download_single_track", fake_download)
    monkeypatch.setattr(library_actions, "_trigger_scan", no_op)
    monkeypatch.setattr(library_actions, "_try_share_album", fake_share)
    monkeypatch.setattr(common, "_send_result", no_op)

    await downloads._do_download_track(SimpleNamespace(chat_id=1), RecordingMessage(), "42")

    assert state._retag_session is None
    assert state._retag_invalidated_reason == "track downloaded"


async def test_delete_invalidates_retag_session(monkeypatch, tmp_path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    state._retag_session = {"plans": [], "expire_at": float("inf")}

    async def fake_scan(*_args, **_kwargs):
        return "scan scheduled"

    async def inline_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(config, "MUSIC_DIR", str(tmp_path))
    monkeypatch.setattr(library_actions, "_trigger_scan", fake_scan)
    monkeypatch.setattr(common.asyncio, "to_thread", inline_to_thread)

    await library_actions._delete_album(str(album))

    assert not album.exists()
    assert state._retag_session is None
    assert state._retag_invalidated_reason == "album deleted"


async def test_confirm_reports_invalidation_reason(monkeypatch):
    state._retag_invalidated_reason = "upload imported"
    monkeypatch.setattr(config, "ALLOWED_USERS", {1})
    message = RecordingMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
        effective_message=message,
        effective_chat=SimpleNamespace(id=1),
    )

    await handlers.cmd_retag(update, SimpleNamespace(args=["confirm"]))

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
