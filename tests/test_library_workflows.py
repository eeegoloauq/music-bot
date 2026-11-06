"""Offline integration checks for local imports and selected library actions."""

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest
import config
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC, USLT
from mutagen.mp4 import MP4, MP4Cover
from telegram import Update, CallbackQuery, User
from telegram.error import NetworkError

import bot
import providers
from flows import uploads as upload_flow
from flows import common, handlers, retag, state
import inline
import retagger
import upload_choices
import upload_import
import uploads
from library.importing import write_import_tags
from library import importing


def make_audio(path):
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=mono", "-t", "0.1", str(path)], check=True)


async def test_original_import_preserves_bytes_sidecars_and_existing_edition(tmp_path, monkeypatch):
    src = tmp_path / "song.flac"
    make_audio(src)
    audio = FLAC(src)
    audio["artist"] = audio["albumartist"] = "Artist"
    audio["album"] = "Album"
    audio["lyrics"] = "[00:01] Original lyrics"
    picture = Picture()
    picture.mime, picture.type, picture.data = "image/jpeg", 3, b"original artwork"
    audio.add_picture(picture)
    audio.save()
    original = src.read_bytes()
    archive = tmp_path / "Album.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Album/Disc 1/01 Song.flac", original)
        zf.writestr("Album/booklet.pdf", b"booklet")
        zf.writestr("Album/cover.jpg", b"cover")
        zf.writestr("Album/playlist.m3u", b"Disc 1/01 Song.flac")
    report = uploads._process_entry(str(archive), str(tmp_path))
    existing = tmp_path / "music" / "Artist" / "Album"
    existing.mkdir(parents=True)
    (existing / "01 Song.flac").write_bytes(b"existing edition")
    lookup = AsyncMock(side_effect=AssertionError("original imports must not resolve metadata"))
    monkeypatch.setattr(upload_import.metadata, "fetch_album", lookup)
    album, result = await upload_import.import_original(report, str(tmp_path / "music"))
    target = Path(result["album_dir"])
    assert album == {"artist": "Artist", "title": "Album"}
    assert target.name == "Album (2)"
    assert (target / "Disc 1" / "01 Song.flac").read_bytes() == original
    assert (target / "booklet.pdf").read_bytes() == b"booklet"
    assert (target / "playlist.m3u").read_bytes() == b"Disc 1/01 Song.flac"
    assert (existing / "01 Song.flac").read_bytes() == b"existing edition"
    assert not os.path.exists(report.staging_dir)
    lookup.assert_not_awaited()


@pytest.mark.parametrize("ext", ["flac", "mp3", "m4a"])
def test_release_tagging_keeps_embedded_artwork_and_lyrics(tmp_path, ext):
    path = tmp_path / f"track.{ext}"
    make_audio(path)
    if ext == "flac":
        tags = FLAC(path)
        tags["lyrics"] = "Original lyrics"
        picture = Picture()
        picture.mime, picture.type, picture.data = "image/jpeg", 3, b"original"
        tags.add_picture(picture)
    elif ext == "mp3":
        tags = ID3(path)
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=b"original"))
        tags.add(USLT(encoding=3, lang="eng", desc="", text="Original lyrics"))
    else:
        tags = MP4(path)
        tags["covr"] = [MP4Cover(b"original")]
        tags["©lyr"] = ["Original lyrics"]
    tags.save()
    track = {"title": "New title", "artist": "Artist", "trackNumber": 1}
    album = {"title": "Album", "artist": "Artist", "id": "42", "numberOfTracks": 1}
    write_import_tags(str(path), track, album, b"replacement", {"plainLyrics": "New lyrics"}, ext)
    if ext == "flac":
        tagged = FLAC(path)
        assert tagged["title"] == ["New title"]
        assert tagged["lyrics"] == ["Original lyrics"]
        assert [p.data for p in tagged.pictures] == [b"original"]
    elif ext == "mp3":
        tagged = ID3(path)
        assert tagged["TIT2"].text == ["New title"]
        assert [p.data for p in tagged.getall("APIC")] == [b"original"]
        assert [p.text for p in tagged.getall("USLT")] == ["Original lyrics"]
    else:
        tagged = MP4(path)
        assert tagged["©nam"] == ["New title"]
        assert tagged["©lyr"] == ["Original lyrics"]
        assert list(tagged["covr"]) == [b"original"]


def test_recover_pending_import_and_reject_symlinks(tmp_path):
    folder = tmp_path / "Album"
    folder.mkdir()
    (folder / "song.flac").write_bytes(b"audio")
    report = uploads._process_entry(str(folder), str(tmp_path))
    recovered = uploads.staged_reports(str(tmp_path))
    assert [(r.name, r.audio) for r in recovered] == [("Album", ["song.flac"])]
    (Path(report.staging_dir) / "escape.flac").symlink_to(tmp_path / "outside")
    assert uploads.staged_reports(str(tmp_path)) == []


async def test_audio_search_uploads_only_after_selection_and_checks_owner(monkeypatch):
    inline._audio_choices.clear()
    monkeypatch.setattr(inline.navidrome, "search", AsyncMock(return_value={"song": [
        {"id": "song", "title": "Title", "artist": "Artist", "album": "Album"}]}))
    upload = AsyncMock()
    shares = AsyncMock()
    monkeypatch.setattr(inline, "_inline_now_playing", upload)
    monkeypatch.setattr(inline, "_get_share_url", shares)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                             inline_query=SimpleNamespace(answer=AsyncMock()))
    await inline._inline_audio_search(update, "Artist")
    upload.assert_not_awaited()
    shares.assert_not_awaited()
    article = update.inline_query.answer.call_args.args[0][0]
    token = article.reply_markup.inline_keyboard[0][0].switch_inline_query_current_chat.split()[1]
    update.effective_user.id = 2
    await inline._inline_selected_audio(update, None, token)
    upload.assert_not_awaited()
    update.effective_user.id = 1
    await inline._inline_selected_audio(update, None, token)
    assert upload.call_args.args[2][0]["songId"] == "song"


async def test_selected_retag_plans_only_one_album_and_keeps_folders(tmp_path, monkeypatch):
    selected = tmp_path / "Artist" / "Album"
    sibling = tmp_path / "Artist" / "Other"
    selected.mkdir(parents=True)
    sibling.mkdir()
    plan = retagger.AlbumPlan(str(selected), "Artist", "Album",
        changes=["artist folder: Artist → ARTIST", "title: changed"],
        canonical_artist_dir="ARTIST")
    planner = AsyncMock(return_value=plan)
    monkeypatch.setattr(retagger, "plan_album", planner)
    plans, summary = await retagger.run_dry_run(str(tmp_path), albums=[(str(selected), "Artist", "Album")])
    planner.assert_awaited_once_with(str(selected), "Artist", "Album")
    assert plans == [plan] and summary.total == 1
    assert plan.canonical_artist_dir is None
    assert plan.changes == ["title: changed"]


@pytest.mark.parametrize("action", ["confirm", "stop"])
async def test_preview_cannot_be_confirmed_or_discarded_by_another_user(monkeypatch, action):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=2),
                             effective_chat=SimpleNamespace(id=1))
    session = {"owner": 1, "chat": 1}
    monkeypatch.setattr(state, "_retag_session", session)
    await retag._retag_command(update, [action])
    assert state._retag_session is session
    assert "another user" in message.reply_text.call_args.args[0]


async def test_changed_album_cannot_be_applied(tmp_path, monkeypatch):
    track = tmp_path / "song.flac"
    track.write_bytes(b"old")
    plan = retagger.AlbumPlan(str(tmp_path), "A", "B")
    snapshot = retag._retag_snapshot([plan])
    track.write_bytes(b"new version")
    monkeypatch.setattr(state, "_retag_session", {"plans": [plan], "snapshot": snapshot,
        "expire_at": time.monotonic() + 60, "owner": 1, "chat": 1})
    monkeypatch.setattr(state, "_retag_in_progress", False)
    apply = AsyncMock()
    monkeypatch.setattr(retag, "_do_retag_apply", apply)
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=1),
                             effective_chat=SimpleNamespace(id=1))
    await retag._retag_command(update, ["confirm"])
    apply.assert_not_awaited()
    assert state._retag_session is None


async def test_manual_selection_skips_automatic_identification(tmp_path, monkeypatch):
    folder = tmp_path / "Album"
    folder.mkdir()
    (folder / "track.flac").write_bytes(b"audio")
    report = uploads._process_entry(str(folder), str(tmp_path))
    key = os.path.basename(report.staging_dir)
    start = AsyncMock()
    monkeypatch.setattr(upload_choices, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(upload_choices, "ALLOWED_USERS", [1])
    choices = upload_choices.UploadChoices(start, lambda *_: "io")
    tasks = []
    context = SimpleNamespace(args=[key, "42"], bot=None,
        application=SimpleNamespace(create_task=lambda coro, **_: tasks.append(asyncio.create_task(coro))))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1), message=SimpleNamespace(reply_text=AsyncMock()))
    await choices.manual(update, context)
    assert key in choices.busy
    await asyncio.gather(*tasks)
    assert start.call_args.kwargs == {"mode": "match", "album_id": "42"}
    assert key not in choices.busy


async def test_original_import_resumes_in_same_album_with_sidecars(tmp_path, monkeypatch):
    folder = tmp_path / "Album"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"first")
    (folder / "02.flac").write_bytes(b"second")
    (folder / "cover.jpg").write_bytes(b"art")
    report = uploads._process_entry(str(folder), str(tmp_path))
    real_file = upload_import._file_original
    progress = {}

    def fail_second(src, dest, counts, audio):
        if counts["saved"] == 1 and audio:
            raise OSError("disk unavailable")
        real_file(src, dest, counts, audio)

    monkeypatch.setattr(upload_import, "_file_original", fail_second)
    music = tmp_path / "music"
    with pytest.raises(OSError):
        await upload_import.import_original(report, str(music), progress)
    assert progress["saved"] == 1
    assert (Path(report.staging_dir) / "cover.jpg").exists()
    monkeypatch.setattr(upload_import, "_file_original", real_file)
    recovered = uploads.staged_reports(str(tmp_path))[0]
    _, result = await upload_import.import_original(recovered, str(music))
    target = Path(result["album_dir"])
    assert target.name == "Album"
    assert (target / "01.flac").read_bytes() == b"first"
    assert (target / "02.flac").read_bytes() == b"second"
    assert (target / "cover.jpg").read_bytes() == b"art"
    assert not (Path(report.staging_dir + ".original.json")).exists()


def test_failed_final_move_restores_staged_file(tmp_path, monkeypatch):
    src, dest = tmp_path / "source.flac", tmp_path / "library.flac"
    src.write_bytes(b"supplied audio")
    monkeypatch.setattr(importing.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("failed")))
    with pytest.raises(OSError):
        importing._move_into_library(str(src), str(dest))
    assert src.read_bytes() == b"supplied audio"
    assert not dest.exists()
    assert not (tmp_path / "library.flac.importing").exists()


async def test_pending_uploads_reappear_without_starting_import(tmp_path, monkeypatch):
    folder = tmp_path / "Album"
    folder.mkdir()
    (folder / "track.flac").write_bytes(b"original audio")
    report = uploads._process_entry(str(folder), str(tmp_path))
    monkeypatch.setattr(upload_choices, "UPLOAD_DIR", str(tmp_path))
    start = AsyncMock()
    io = SimpleNamespace(reply_text=AsyncMock())
    # A fresh instance simulates losing all in-memory state on restart.
    choices = upload_choices.UploadChoices(start, lambda *_: io)
    assert await choices.restore(io)
    buttons = io.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert buttons[0][0].callback_data.endswith(os.path.basename(report.staging_dir))
    assert (Path(report.staging_dir) / "track.flac").read_bytes() == b"original audio"
    start.assert_not_awaited()


async def test_recovery_continues_if_one_upload_prompt_fails(tmp_path, monkeypatch):
    for name in ("First", "Second"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "track.flac").write_bytes(b"audio")
        uploads._process_entry(str(folder), str(tmp_path))
    monkeypatch.setattr(upload_choices, "UPLOAD_DIR", str(tmp_path))
    choices = upload_choices.UploadChoices(AsyncMock(), None)
    io = SimpleNamespace(reply_text=AsyncMock(side_effect=[NetworkError("offline"), None]))
    assert await choices.restore(io)
    assert io.reply_text.await_count == 2
    assert len(uploads.staged_reports(str(tmp_path))) == 2


@pytest.mark.parametrize("user,chat,allowed", [(1, 1, True), (2, 2, False), (1, -10, False)])
async def test_pending_button_is_private_to_upload_owner(monkeypatch, user, chat, allowed):
    monkeypatch.setattr(upload_choices, "ALLOWED_USERS", [1, 2])
    io = SimpleNamespace(reply_text=AsyncMock())
    choices = upload_choices.UploadChoices(AsyncMock(), lambda *_: io)
    restore = AsyncMock(return_value=False)
    monkeypatch.setattr(choices, "restore", restore)
    query = SimpleNamespace(answer=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user),
                             effective_chat=SimpleNamespace(id=chat), callback_query=query)
    await choices.list_pending(update, SimpleNamespace(bot=None))
    assert restore.await_count == int(allowed)
    assert query.answer.await_count == int(allowed)
    assert io.reply_text.await_count == int(allowed)


@pytest.mark.parametrize("data,callback", [
    ("import:help", "_handle_import_help"),
    ("import:original:012345abcdef", "upload"),
    ("uploads:pending", "pending"),
])
def test_import_buttons_reach_their_own_handlers(monkeypatch, data, callback):
    monkeypatch.setattr(config, "TG_TOKEN", "123:fake-token")
    app = bot._build_app()
    update = Update(1, callback_query=CallbackQuery(
        id="query", from_user=User(1, "Owner", False), chat_instance="chat", data=data))
    handler = next(h for h in app.handlers[0] if h.check_update(update))
    expected = {"upload": upload_flow._upload_choices.callback, "pending": upload_flow._upload_choices.list_pending,
                "_handle_import_help": handlers._handle_import_help}[callback]
    assert handler.callback == expected


def test_owner_start_menu_has_pending_upload_button(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USERS", [1])
    owner = SimpleNamespace(effective_chat=SimpleNamespace(id=1))
    other = SimpleNamespace(effective_chat=SimpleNamespace(id=2))
    assert any(b.callback_data == "uploads:pending"
               for row in handlers._chat_shortcuts(owner).inline_keyboard for b in row)
    assert not any(b.callback_data == "uploads:pending"
                   for row in handlers._chat_shortcuts(other).inline_keyboard for b in row)


async def test_repeated_startup_keeps_pending_uploads_quiet_and_announces_new_ones(monkeypatch):
    scheduled = []
    pending = uploads.IntakeReport(name="Waiting", staging_dir="/staging/abcdef123456", audio=["old.mp3"])
    fresh = uploads.IntakeReport(name="New", staging_dir="/staging/123456abcdef", audio=["new.mp3"])
    staged = lambda *_: [pending]
    monkeypatch.setattr(uploads, "staged_reports", staged)
    restore = AsyncMock(wraps=upload_flow._upload_choices.restore)
    offer = AsyncMock()
    monkeypatch.setattr(upload_flow._upload_choices, "restore", restore)
    monkeypatch.setattr(upload_flow._upload_choices, "offer", offer)

    async def watch(callback):
        # Already staged entries stay untouched; only new intake is announced.
        assert uploads.staged_reports() == [pending]
        await callback(fresh)

    monkeypatch.setattr(config, "ALLOWED_USERS", [1])
    monkeypatch.setattr(config, "UPLOAD_HTTP_PORT", 0)
    monkeypatch.setattr(common, "_spawn_background", scheduled.append)
    monkeypatch.setattr(bot.uploads, "watch_loop", watch)
    monkeypatch.setattr(providers, "sharing_enabled", lambda: False)
    monkeypatch.setattr(providers, "check", AsyncMock(
        return_value=SimpleNamespace(state="disabled", ready=False)))
    for _ in range(2):
        await bot._post_init(SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock())))
        await asyncio.gather(*scheduled)
        scheduled.clear()
    restore.assert_not_awaited()
    assert offer.await_count == 2
    assert all(call.args[1] is fresh for call in offer.await_args_list)
    assert uploads.staged_reports() == [pending]
