"""Provider opt-in, readiness and library-first behavior without live services."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

import bot
import journal
import providers


@pytest.mark.parametrize("selection,host,state", [
    ("none", "http://unused", "disabled"),
    ("soulseek", "", "misconfigured"),
])
async def test_missing_provider_never_contacts_a_daemon(monkeypatch, selection, host, state):
    monkeypatch.setattr(providers, "DOWNLOAD_PROVIDER", selection)
    monkeypatch.setattr(providers, "SLSKD_HOST", host)
    def forbidden(*args, **kwargs):
        raise AssertionError("no provider request should be made")
    monkeypatch.setattr(providers.aiohttp, "ClientSession", forbidden)
    assert (await providers.check()).state == state


async def test_missing_optional_package_has_setup_status(monkeypatch):
    monkeypatch.setattr(providers, "DOWNLOAD_PROVIDER", "soulseek")
    monkeypatch.setattr(providers, "SLSKD_HOST", "http://provider.invalid")
    monkeypatch.setattr(providers.importlib.util, "find_spec", lambda _: None)
    assert (await providers.check()).state == "misconfigured"


@pytest.mark.parametrize("payload,error,expected", [
    ({"isLoggedIn": True}, None, "ready"),
    ({"isLoggedIn": False}, None, "unavailable"),
    ({}, None, "unavailable"),
    ([], None, "unavailable"),
    (None, aiohttp.ClientConnectionError(), "unavailable"),
    (None, TimeoutError(), "unavailable"),
    (None, ValueError(), "unavailable"),
    (None, aiohttp.ClientResponseError(None, (), status=401), "unavailable"),
    (None, aiohttp.ClientResponseError(None, (), status=403), "unavailable"),
    (None, aiohttp.ClientResponseError(None, (), status=503), "unavailable"),
])
async def test_health_checks_login_authentication_and_connectivity(
        monkeypatch, payload, error, expected):
    monkeypatch.setattr(providers, "DOWNLOAD_PROVIDER", "soulseek")
    monkeypatch.setattr(providers, "SLSKD_HOST", "http://provider.invalid")
    monkeypatch.setattr(providers, "SLSKD_API_KEY", "test-key")
    monkeypatch.setattr(providers.importlib.util, "find_spec", lambda _: object())
    class Response:
        async def __aenter__(self):
            if error and not isinstance(error, aiohttp.ClientResponseError):
                raise error
            return self
        async def __aexit__(self, *args):
            pass
        def raise_for_status(self):
            if isinstance(error, aiohttp.ClientResponseError):
                raise error
        async def json(self):
            return payload
    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is True
            assert kwargs["timeout"].total == 5
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get(self, url, headers):
            assert url == "http://provider.invalid/api/v0/server"
            assert headers == {"X-API-Key": "test-key"}
            return Response()
    monkeypatch.setattr(providers.aiohttp, "ClientSession", Session)
    assert (await providers.check()).state == expected


@pytest.fixture
def release(monkeypatch, tmp_path):
    track = {"id": "2", "artist": "Artist", "title": "Song", "trackNumber": 1}
    album = {"id": "1", "artist": "Artist", "title": "Album", "tracks": [track]}
    monkeypatch.setattr(bot.metadata, "fetch_album", AsyncMock(return_value=album))
    monkeypatch.setattr(bot.metadata, "fetch_single_track", AsyncMock(return_value=(track, album)))
    monkeypatch.setattr(bot, "MUSIC_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "_locate_existing_album", lambda *_, **kwargs: str(tmp_path))
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(tmp_path / "pending.json"))
    io = SimpleNamespace(reply_text=AsyncMock())
    return io, tmp_path


@pytest.mark.parametrize("kind,id", [("album", "1"), ("track", "2")])
async def test_existing_release_does_not_need_provider_or_create_a_share(monkeypatch, release, kind, id):
    io, path = release
    (path / "01 Song.flac").write_bytes(b"existing audio")
    check = AsyncMock(side_effect=AssertionError("provider must not be checked"))
    share = AsyncMock(side_effect=AssertionError("lookup must not publish a share"))
    monkeypatch.setattr(providers, "check", check)
    monkeypatch.setattr(bot, "_try_share_album", share)
    assert await bot._prepare_download(io, kind, id) is False
    assert "Already in library" in io.reply_text.call_args.args[0]
    check.assert_not_called()
    share.assert_not_called()
    assert journal.load() == []


@pytest.mark.parametrize("kind,id", [("album", "1"), ("track", "2")])
@pytest.mark.parametrize("state", ["disabled", "misconfigured", "unavailable"])
async def test_missing_provider_does_not_queue_or_move_force_backup(monkeypatch, release, kind, id, state):
    io, path = release
    original = path / "01 Song.flac"
    original.write_bytes(b"existing audio")
    monkeypatch.setattr(providers, "check", AsyncMock(
        return_value=providers.ProviderStatus(state, "Provider needs attention")))
    assert await bot._prepare_download(io, kind, id, force=True) is False
    assert original.read_bytes() == b"existing audio"
    assert journal.load() == []
    assert not (path / ".redownload-backup").exists()
    text = io.reply_text.call_args.args[0]
    assert "Artist" in text and "1/1" in text and "send the link again" in text
    actions = io.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0]
    assert actions[0].callback_data == "import:help"
    assert actions[1].url.endswith("docs/providers/soulseek.md")


@pytest.mark.parametrize("kind,id", [("album", "1"), ("track", "2")])
async def test_ready_provider_can_accept_missing_music(monkeypatch, release, kind, id):
    io, _ = release
    monkeypatch.setattr(providers, "check", AsyncMock(
        return_value=providers.ProviderStatus("ready", "Ready")))
    assert await bot._prepare_download(io, kind, id) is True
    io.reply_text.assert_not_called()


@pytest.mark.parametrize("kind,id", [("album", "1"), ("track", "2")])
async def test_download_entrypoint_rejects_work_before_spawning(monkeypatch, release, kind, id):
    io, _ = release
    monkeypatch.setattr(providers, "check", AsyncMock(
        return_value=providers.ProviderStatus("disabled", "No download provider is configured.")))
    monkeypatch.setattr(bot.ChatIO, "from_update", lambda _: io)
    monkeypatch.setattr(bot, "_spawn_run", lambda *args: pytest.fail("must not queue a download"))
    handler = bot._download_album if kind == "album" else bot._download_track
    await handler(object(), id)
    assert "No download provider" in io.reply_text.call_args.args[0]
    assert journal.load() == []


@pytest.mark.parametrize("state", ["disabled", "unavailable", "misconfigured"])
async def test_unavailable_provider_preserves_resume_journal(monkeypatch, tmp_path, state):
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(tmp_path / "pending.json"))
    journal.add(journal.PendingDownload(kind="album", id="1", chat_id=42,
                                       resume_attempts=journal.MAX_RESUME_ATTEMPTS))
    before = Path(journal.JOURNAL_PATH).read_bytes()
    monkeypatch.setattr(providers, "check", AsyncMock(
        return_value=providers.ProviderStatus(state, "Not ready")))
    app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await bot._resume_pending(app)
    assert Path(journal.JOURNAL_PATH).read_bytes() == before
    app.bot.send_message.assert_not_called()


async def test_downloads_do_not_implicitly_enable_sharing(monkeypatch):
    monkeypatch.setattr(providers, "DOWNLOAD_PROVIDER", "soulseek")
    monkeypatch.setattr(providers, "SLSKD_HOST", "http://provider.invalid")
    monkeypatch.setattr(providers, "SOULSEEK_SHARE_SCAN_ENABLED", False)
    monkeypatch.setattr(providers, "_backend", lambda: pytest.fail("sharing must stay disabled"))
    assert await providers.rescan_shares() is False
    assert providers.schedule_rescan_shares() == 0


def test_library_lookup_does_not_rename_folders(monkeypatch, tmp_path):
    from library import files
    artist = tmp_path / "ARTIST"
    album = artist / "Original folder"
    album.mkdir(parents=True)
    (album / "01 Song.flac").write_bytes(b"existing audio")
    monkeypatch.setattr(files, "_folder_signals", lambda _: ("1", "Album"))
    result = files._locate_existing_album(
        str(tmp_path), {"id": "1", "artist": "Artist", "title": "Album"},
        canonicalize=False)
    assert result == str(album)
    assert artist.is_dir()
    assert not (tmp_path / "Artist").exists()


def test_base_startup_and_shutdown_without_soulseek_or_requests(tmp_path):
    """A new interpreter proves imports do not rely on provider modules loaded by conftest."""
    config = tmp_path / "empty.env"
    config.write_text("")
    script = r'''
import asyncio
import importlib.abc
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0, sys.argv[1])
class BlockProvider(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split(".")[0] in {"soulseek", "slskd_api", "requests"}:
            raise AssertionError(f"optional provider imported: {fullname}")
sys.meta_path.insert(0, BlockProvider())
import bot
import upload_import
bot.ALLOWED_USERS = [42]
bot.UPLOAD_HTTP_PORT = 0
started = []
def spawn(coro):
    started.append(coro.cr_code.co_name)
    coro.close()
bot._spawn_background = spawn
app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()))
async def run():
    await bot._post_init(app)
    await bot._shutdown(app)
asyncio.run(run())
assert started == ["watch_loop"], started
commands = app.bot.set_my_commands.call_args.args[0]
assert "provider" in [c.command for c in commands]
assert "sharescan" not in [c.command for c in commands]
'''
    env = {**os.environ, "DOWNLOAD_PROVIDER": "none", "CONFIG_FILE": str(config)}
    result = subprocess.run([sys.executable, "-c", script,
                             str(Path(__file__).resolve().parents[1] / "src")],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
