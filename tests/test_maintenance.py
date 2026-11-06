"""Selected writes preserve audio and unrelated tags across all supported formats."""

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, TALB, TCON, TIT2, TPE1, TRCK, TSRC, TXXX, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
import pytest

import bot
import maintenance
from maintenance import fetch_artwork
import retagger
from library.tagger import inspect_selected_tags

OLD_ART = b"\xff\xd8\xffold cover"
NEW_ART = b"\xff\xd8\xffnew cover"
OLD_LRC = "[00:01.00] Original lyrics"
NEW_LRC = "[00:02.00] New lyrics"


@pytest.fixture(params=["flac", "mp3", "m4a"])
def release(request, tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("requires ffmpeg")
    folder = tmp_path / "Artist" / "Album"
    folder.mkdir(parents=True)
    path = folder / f"01 Song.{request.param}"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "0.1", str(path)], check=True)
    (folder / "booklet.txt").write_text("Keep this sidecar")
    if request.param == "flac":
        audio = FLAC(path)
        audio.update(title="Old Song", artist="Artist", album="Album", tracknumber="1",
                     isrc="TEST001", comment="https://www.deezer.com/album/1", genre="Personal",
                     composer="Composer", custom="Keep me", lyrics="Original plain", syncedlyrics=OLD_LRC)
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, "image/jpeg", OLD_ART
        audio.add_picture(picture)
    elif request.param == "m4a":
        audio = MP4(path)
        audio["©nam"], audio["©ART"], audio["©alb"] = ["Old Song"], ["Artist"], ["Album"]
        audio["trkn"] = [(1, 1)]
        audio["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(b"TEST001")]
        audio["©cmt"], audio["©gen"], audio["©wrt"] = ["https://www.deezer.com/album/1"], ["Personal"], ["Composer"]
        audio["----:com.apple.iTunes:CUSTOM"] = [MP4FreeForm(b"Keep me")]
        audio["©lyr"] = ["Original plain"]
        audio["----:com.apple.iTunes:SYNCEDLYRICS"] = [MP4FreeForm(OLD_LRC.encode())]
        audio["covr"] = [MP4Cover(OLD_ART)]
    else:
        audio = MP3(path)
        audio.tags.add(TIT2(encoding=3, text="Old Song"))
        audio.tags.add(TPE1(encoding=3, text="Artist"))
        audio.tags.add(TALB(encoding=3, text="Album"))
        audio.tags.add(TRCK(encoding=3, text="1"))
        audio.tags.add(TSRC(encoding=3, text="TEST001"))
        audio.tags.add(COMM(encoding=3, lang="eng", desc="", text="https://www.deezer.com/album/1"))
        audio.tags.add(TCON(encoding=3, text=["Personal"]))
        audio.tags.add(TXXX(encoding=3, desc="CUSTOM", text="Keep me"))
        audio.tags.add(USLT(encoding=3, lang="eng", desc="", text="Original plain"))
        audio.tags.add(TXXX(encoding=3, desc="SYNCEDLYRICS", text=OLD_LRC))
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=OLD_ART))
    audio.save()
    os.chmod(path, 0o664)
    track = {"id": "2", "title": "New Song", "artist": "Artist", "isrc": "TEST001",
             "trackNumber": 1, "duration": 1}
    album = {"id": "1", "title": "Album", "artist": "Artist", "genres": ["Rock"], "tracks": [track]}
    fetch = AsyncMock(side_effect=lambda *_: deepcopy(album))
    monkeypatch.setattr(retagger.metadata, "fetch_album", fetch)
    enrich = AsyncMock()
    monkeypatch.setattr(retagger.metadata, "enrich_genres", enrich)
    lyrics = AsyncMock(return_value={"syncedLyrics": NEW_LRC, "plainLyrics": "New plain"})
    monkeypatch.setattr(maintenance.metadata, "fetch_lyrics", lyrics)
    cover = AsyncMock(return_value=NEW_ART)
    monkeypatch.setattr(maintenance, "fetch_artwork", cover)
    return SimpleNamespace(path=path, folder=folder, root=tmp_path, fetch=fetch,
                           lyrics=lyrics, cover=cover, enrich=enrich)


def audio_digest(path):
    return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path),
                                    "-map", "0:a:0", "-f", "md5", "-"])


async def preview(release, fields, **kwargs):
    return await maintenance.preview([(str(release.folder), "Artist", "Album")],
                                      maintenance.Options(frozenset(fields), **kwargs))


def remove_media(release, *, keep_plain=False):
    path = release.path
    if path.suffix == ".flac":
        audio = FLAC(path)
        audio.clear_pictures()
        audio.pop("syncedlyrics", None)
        if not keep_plain:
            audio.pop("lyrics", None)
    elif path.suffix == ".m4a":
        audio = MP4(path)
        audio.pop("covr", None)
        audio.pop("----:com.apple.iTunes:SYNCEDLYRICS", None)
        if not keep_plain:
            audio.pop("©lyr", None)
    else:
        audio = MP3(path)
        audio.tags.delall("APIC")
        audio.tags.delall("TXXX:SYNCEDLYRICS")
        if not keep_plain:
            audio.tags.delall("USLT")
    audio.save()


async def test_selected_title_only_preserves_audio_and_other_fields(release):
    before = release.path.read_bytes()
    digest = audio_digest(release.path)
    old = inspect_selected_tags(str(release.path))
    unmanaged = {key: deepcopy(value) for key, value in MutagenFile(release.path).tags.items()
                 if key not in {"title", "©nam", "TIT2"}}
    plans, summary = await preview(release, {"titles"})
    assert release.path.read_bytes() == before
    assert summary.will_change == 1
    assert plans[0].updates[0].values == {"title": "New Song"}
    release.cover.assert_not_awaited()
    release.lyrics.assert_not_awaited()
    release.enrich.assert_not_awaited()
    # Confirmation must use the fetched preview values even if services go away.
    release.fetch.side_effect = AssertionError("apply must not fetch metadata")
    stats = await retagger.run_apply(plans, apply_fn=maintenance.apply_plan)
    assert stats["files_written"] == 1
    old["title"] = "New Song"
    assert inspect_selected_tags(str(release.path)) == old
    assert {key: value for key, value in MutagenFile(release.path).tags.items()
            if key not in {"title", "©nam", "TIT2"}} == unmanaged
    assert audio_digest(release.path) == digest
    assert os.stat(release.path).st_mode & 0o777 == 0o664
    assert (release.folder / "booklet.txt").read_text() == "Keep this sidecar"
    assert not list(release.folder.glob(".maintenance-*"))


async def test_fill_mode_preserves_existing_media_and_adds_genres(release):
    plans, _ = await preview(release, {"genres", "lyrics", "artwork"})
    assert plans[0].updates[0].values == {"genres": ["Personal", "Rock"]}
    await maintenance.apply_plan(plans[0])
    tags = inspect_selected_tags(str(release.path))
    assert tags["title"] == "Old Song"
    assert tags["artwork"] == [OLD_ART]
    assert tags["synced"] == OLD_LRC
    assert tags["genres"] == ["Personal", "Rock"]
    release.cover.assert_not_awaited()
    release.lyrics.assert_not_awaited()


async def test_missing_synced_lyrics_preserve_supplied_plain_text(release):
    remove_media(release, keep_plain=True)
    plans, _ = await preview(release, {"lyrics", "artwork"})
    before = release.path.read_bytes()
    assert plans[0].updates
    assert release.path.read_bytes() == before
    await maintenance.apply_plan(plans[0])
    tags = inspect_selected_tags(str(release.path))
    assert tags["synced"] == NEW_LRC
    assert tags["plain"] == "Original plain"
    assert tags["artwork"] == [NEW_ART]
    assert tags["title"] == "Old Song"


async def test_replacement_requires_explicit_options(release):
    plans, _ = await preview(release, {"lyrics", "artwork"}, replace_lyrics=True, replace_artwork=True)
    assert any("replace existing" in change for change in plans[0].changes)
    await maintenance.apply_plan(plans[0])
    tags = inspect_selected_tags(str(release.path))
    assert tags["synced"] == NEW_LRC
    assert tags["plain"] == "New plain"
    assert tags["artwork"] == [NEW_ART]
    assert tags["title"] == "Old Song"
    assert tags["genres"] == ["Personal"]


async def test_unavailable_sources_leave_media_unchanged_and_allow_title_updates(release):
    release.lyrics.side_effect = TimeoutError()
    release.cover.return_value = None
    plans, _ = await preview(release, {"titles", "lyrics", "artwork"},
                             replace_lyrics=True, replace_artwork=True)
    assert plans[0].updates[0].values == {"title": "New Song"}
    assert len(plans[0].notes) == 2
    await maintenance.apply_plan(plans[0])
    tags = inspect_selected_tags(str(release.path))
    assert tags["synced"] == OLD_LRC
    assert tags["artwork"] == [OLD_ART]


async def test_stale_preview_cannot_overwrite_changed_file(release):
    plans, _ = await preview(release, {"titles"})
    with release.path.open("ab") as stream:
        stream.write(b"external change")
    changed = release.path.read_bytes()
    stats = await retagger.run_apply(plans, apply_fn=maintenance.apply_plan)
    assert stats["files_written"] == 0
    assert stats["failed"]
    assert release.path.read_bytes() == changed


async def test_failed_write_preserves_original_and_removes_temporary_file(release, monkeypatch):
    plans, _ = await preview(release, {"titles"})
    before = release.path.read_bytes()
    def fail(path, values):
        Path(path).write_bytes(b"interrupted tag write")
        raise OSError("disk write failed")
    monkeypatch.setattr(maintenance, "write_selected_tags", fail)
    stats = await retagger.run_apply(plans, apply_fn=maintenance.apply_plan)
    assert stats["files_written"] == 0
    assert "disk write failed" in stats["failed"][0][1]
    assert release.path.read_bytes() == before
    assert not list(release.folder.glob(".maintenance-*"))


async def test_cover_sidecar_is_replaced_only_in_explicit_replacement_mode(release):
    cover = release.folder / "cover.jpg"
    cover.write_bytes(OLD_ART)
    before = cover.read_bytes()
    plans, _ = await preview(release, {"artwork"})
    assert not plans[0].updates
    assert cover.read_bytes() == before
    release.cover.assert_not_awaited()
    plans, _ = await preview(release, {"artwork"}, replace_artwork=True)
    assert cover.read_bytes() == before
    await maintenance.apply_plan(plans[0])
    assert cover.read_bytes() == NEW_ART
    assert inspect_selected_tags(str(release.path))["artwork"] == [NEW_ART]


async def test_plain_only_source_does_not_replace_existing_lyrics(release):
    remove_media(release, keep_plain=True)
    release.lyrics.return_value = {"plainLyrics": "Different plain lyrics"}
    before = release.path.read_bytes()
    plans, summary = await preview(release, {"lyrics"}, replace_lyrics=True)
    assert summary.will_change == 0
    assert "synced lyrics unavailable" in plans[0].notes[0]
    assert release.path.read_bytes() == before


async def test_lastfm_only_genres_still_work_without_matching_track(release, monkeypatch):
    base = retagger.AlbumPlan(folder=str(release.folder), artist_dir="Artist", album_dir="Album",
                              files=[str(release.path)], lastfm_only_genres=["Jazz"])
    monkeypatch.setattr(retagger, "plan_album", AsyncMock(return_value=base))
    plans, _ = await preview(release, {"titles", "genres", "lyrics", "artwork"})
    assert plans[0].updates[0].values == {"genres": ["Personal", "Jazz"]}
    await maintenance.apply_plan(plans[0])
    tags = inspect_selected_tags(str(release.path))
    assert tags["title"] == "Old Song"
    assert tags["genres"] == ["Personal", "Jazz"]
    release.lyrics.assert_not_awaited()
    release.cover.assert_not_awaited()


async def test_external_edit_during_write_is_not_overwritten(release, monkeypatch):
    plans, _ = await preview(release, {"titles"})
    writer = maintenance.write_selected_tags
    def race(path, values):
        writer(path, values)
        with release.path.open("ab") as stream:
            stream.write(b"external edit during apply")
    monkeypatch.setattr(maintenance, "write_selected_tags", race)
    stats = await retagger.run_apply(plans, apply_fn=maintenance.apply_plan)
    assert stats["files_written"] == 0
    assert stats["failed"]
    assert release.path.read_bytes().endswith(b"external edit during apply")
    assert inspect_selected_tags(str(release.path))["title"] == "Old Song"
    assert not list(release.folder.glob(".maintenance-*"))


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_USERS", [1, 2])
    monkeypatch.setattr(bot, "_retag_session", None)
    monkeypatch.setattr(bot, "_retag_invalidated_reason", None)
    monkeypatch.setattr(bot, "_retag_in_progress", False)
    monkeypatch.setattr(bot, "_download_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(bot, "_trigger_scan", AsyncMock(return_value="Scan requested"))
    message = SimpleNamespace(message_id=1, chat_id=1, reply_text=AsyncMock(), edit_text=AsyncMock())
    message.reply_text.return_value = message
    query = SimpleNamespace(answer=AsyncMock(), message=message, edit_message_reply_markup=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=1),
                             effective_message=message, message=message, callback_query=query)
    context = SimpleNamespace(user_data={}, args=[])
    return update, context


async def test_picker_requires_explicit_replacement_and_keeps_scope(chat, monkeypatch, tmp_path):
    update, context = chat
    await bot.cmd_maintain(update, context)
    selection = context.user_data["maintenance_selection"]
    preview_call = AsyncMock()
    monkeypatch.setattr(bot, "_retag_command", preview_call)
    update.callback_query.data = f"maintain:preview:{selection['token']}"
    await bot._handle_maintenance_callback(update, context)
    preview_call.assert_not_awaited()
    for _ in range(2):
        update.callback_query.data = f"maintain:lyrics:{selection['token']}"
        await bot._handle_maintenance_callback(update, context)
    update.callback_query.data = f"maintain:preview:{selection['token']}"
    await bot._handle_maintenance_callback(update, context)
    options = preview_call.call_args.kwargs["maintenance_options"]
    assert options.fields == {"lyrics"}
    assert options.replace_lyrics is True
    assert options.replace_artwork is False
    assert preview_call.call_args.kwargs["folder"] is None


@pytest.mark.parametrize("invalid", ["user", "chat", "expired", "changed"])
async def test_confirmation_rejects_wrong_owner_expired_or_changed_previews(release, chat, monkeypatch, invalid):
    update, _context = chat
    monkeypatch.setattr(bot, "MUSIC_DIR", str(release.root))
    before = release.path.read_bytes()
    await bot._retag_command(update, [], folder=str(release.folder),
                             maintenance_options=maintenance.Options(frozenset({"titles"})))
    assert bot._retag_session is not None
    if invalid == "user":
        update.effective_user.id = 2
    elif invalid == "chat":
        update.effective_chat.id = 2
    elif invalid == "expired":
        bot._retag_session["expire_at"] = 0
    else:
        with release.path.open("ab") as stream:
            stream.write(b"external change")
        before = release.path.read_bytes()
    await bot._retag_command(update, ["confirm"])
    assert release.path.read_bytes() == before
    bot._trigger_scan.assert_not_awaited()


async def test_album_preview_and_confirmation_use_shared_session_and_scan(release, chat, monkeypatch):
    update, _context = chat
    monkeypatch.setattr(bot, "MUSIC_DIR", str(release.root))
    before = release.path.read_bytes()
    await bot._retag_command(update, [], folder=str(release.folder),
                             maintenance_options=maintenance.Options(frozenset({"titles"})))
    assert release.path.read_bytes() == before
    assert "Selected field preview" in update.message.edit_text.call_args.args[0]
    assert bot._retag_session["owner"] == 1
    await bot._retag_command(update, ["confirm"])
    assert bot._retag_session is None
    assert inspect_selected_tags(str(release.path))["title"] == "New Song"
    bot._trigger_scan.assert_awaited_once()


async def test_library_picker_previews_all_albums_and_discard_writes_nothing(release, chat, monkeypatch):
    update, context = chat
    other = release.folder.parent / "Other album"
    other.mkdir()
    second = other / release.path.name
    shutil.copyfile(release.path, second)
    before = release.path.read_bytes()
    monkeypatch.setattr(bot, "MUSIC_DIR", str(release.root))
    await bot.cmd_maintain(update, context)
    token = context.user_data["maintenance_selection"]["token"]
    for action in ("titles", "preview"):
        update.callback_query.data = f"maintain:{action}:{token}"
        await bot._handle_maintenance_callback(update, context)
    assert len(bot._retag_session["plans"]) == 2
    assert "2 albums" in update.message.edit_text.call_args.args[0]
    await bot._retag_command(update, ["stop"])
    assert bot._retag_session is None
    assert release.path.read_bytes() == second.read_bytes() == before
    bot._trigger_scan.assert_not_awaited()


async def test_confirm_rechecks_snapshot_after_waiting_for_library_lock(release, chat, monkeypatch):
    update, _context = chat
    monkeypatch.setattr(bot, "MUSIC_DIR", str(release.root))
    await bot._retag_command(update, [], folder=str(release.folder),
                             maintenance_options=maintenance.Options(frozenset({"titles"})))
    plans = bot._retag_session["plans"]
    async with bot._download_semaphore:
        task = asyncio.create_task(bot._do_retag_apply(update, plans))
        await asyncio.sleep(0)
        assert bot._retag_in_progress
        with release.path.open("ab") as stream:
            stream.write(b"external edit while queued")
    await task
    assert release.path.read_bytes().endswith(b"external edit while queued")
    assert inspect_selected_tags(str(release.path))["title"] == "Old Song"
    assert bot._retag_session is None
    bot._trigger_scan.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["user", "chat", "token", "expired"])
async def test_picker_rejects_other_users_chats_and_stale_buttons(chat, monkeypatch, invalid):
    update, context = chat
    await bot.cmd_maintain(update, context)
    selection = context.user_data["maintenance_selection"]
    token = selection["token"]
    if invalid == "user":
        update.effective_user.id = 2
    elif invalid == "chat":
        update.effective_chat.id = 2
    elif invalid == "token":
        token = "0" * 12
    else:
        selection["expires"] = 0
    update.callback_query.data = f"maintain:titles:{token}"
    await bot._handle_maintenance_callback(update, context)
    assert not selection["fields"]
    update.callback_query.edit_message_reply_markup.assert_not_awaited()


def test_preview_stays_within_telegram_limit_with_escaped_names():
    plan = maintenance.Plan(folder="/unused", artist_dir="&" * 200, album_dir="<" * 200,
                            changes=["&" * 500] * 50, notes=["<" * 500] * 50)
    summary = retagger.RetagSummary(total=1)
    text = bot._format_maintenance_preview([plan], summary, maintenance.Options(frozenset({"titles"})))
    assert len(text) < 4096
    assert "+" in text and "more changes" in text


@pytest.mark.parametrize("payload,expected", [
    (NEW_ART, NEW_ART),
    (b"not a JPEG", None),
    (b"\xff\xd8\xff" + b"x" * (8 * 1024**2), None),
])
async def test_artwork_download_validates_type_and_bounds_size(monkeypatch, payload, expected):
    class Response:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def raise_for_status(self):
            pass
        @property
        def content(self):
            return self
        async def iter_chunked(self, size):
            for offset in range(0, len(payload), size):
                yield payload[offset:offset + size]
    monkeypatch.setattr(maintenance, "_get_session", AsyncMock(
        return_value=SimpleNamespace(get=lambda *args, **kwargs: Response())))
    assert await fetch_artwork({"cover_uuid": "https://example.com/1000x1000.jpg"}) == expected
