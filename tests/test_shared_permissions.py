"""Shared-library writes must work without root and without any chmod.

The bot, slskd and Samba all write into the same tree. Group write has to come
from the process umask at creation time: a non-root process cannot chmod a file
another writer owns, so a chmod after the fact would only paper over a mode that
was already wrong.
"""

import asyncio
import errno
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import journal
from library.files import _ensure_album_dir
from soulseek import downloader
from soulseek.downloader import _move_into_library


@pytest.fixture
def shared_umask():
    """The umask main() sets for the whole process."""
    previous = os.umask(0o002)
    yield
    os.umask(previous)


def test_created_paths_are_group_writable(tmp_path, shared_umask):
    album = Path(_ensure_album_dir(str(tmp_path), "Artist", "Album"))
    assert stat.S_IMODE(album.stat().st_mode) == 0o775
    assert stat.S_IMODE(album.parent.stat().st_mode) == 0o775

    source = tmp_path / "download.mp3"
    with open(source, "wb") as handle:
        handle.write(b"audio")
    assert stat.S_IMODE(source.stat().st_mode) == 0o664

    target = album / "track.mp3"
    _move_into_library(str(source), str(target))
    assert not source.exists()
    assert target.read_bytes() == b"audio"
    # A move keeps the mode the writer created the file with, which is why the
    # download side needs the same umask (SLSKD_UMASK=0002 in compose).
    assert stat.S_IMODE(target.stat().st_mode) == 0o664


def test_shared_writes_never_chmod(tmp_path, shared_umask, monkeypatch):
    """A chmod here would fail on paths another writer owns — there must be none.

    Covers every place a chmod used to live: the album directory, the imported
    track, the cover art and the crash ledger.
    """

    def forbidden_chmod(*args, **kwargs):
        raise AssertionError("a shared-tree write must not chmod anything")

    monkeypatch.setattr(os, "chmod", forbidden_chmod)

    album = Path(_ensure_album_dir(str(tmp_path), "Artist", "Album"))
    source = tmp_path / "download.mp3"
    with open(source, "wb") as handle:
        handle.write(b"audio")
    _move_into_library(str(source), str(album / "track.mp3"))

    class _Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def read(self):
            return b"cover"

    class _Session:
        def get(self, *args, **kwargs):
            return _Response()

    async def _session():
        return _Session()

    monkeypatch.setattr(downloader, "_get_session", _session)
    assert asyncio.run(downloader._download_cover("https://example.invalid/c", str(album))) == b"cover"
    assert stat.S_IMODE((album / "cover.jpg").stat().st_mode) == 0o664

    monkeypatch.setattr(journal, "JOURNAL_PATH", str(tmp_path / "state" / "pending.json"))
    journal._write([])
    journal._write([])
    assert stat.S_IMODE(Path(journal.JOURNAL_PATH).stat().st_mode) == 0o664


def test_cross_device_move_does_not_inherit_dropper_mode(tmp_path, shared_umask, monkeypatch):
    """A file dropped over Samba arrives 0644; copying it must not carry that
    into the shared library, or no other writer can retag the track."""
    album = Path(_ensure_album_dir(str(tmp_path), "Artist", "Album"))
    source = tmp_path / "dropped.mp3"
    source.write_bytes(b"audio")
    os.chmod(source, 0o644)

    real_rename = os.rename

    def rename_across_devices(src, dst, *args, **kwargs):
        if str(src) == str(source):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename_across_devices)
    target = album / "track.mp3"
    _move_into_library(str(source), str(target))

    assert not source.exists()
    assert target.read_bytes() == b"audio"
    assert stat.S_IMODE(target.stat().st_mode) == 0o664


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to create foreign-owned fixtures and drop UID")
def test_nonroot_import_tag_cover_and_state_on_shared_mounts():
    from mutagen.id3 import ID3, TIT2

    # Use /tmp directly: pytest's root-owned private parent is not traversable
    # by the child after dropping privileges. All changes stay in this fixture.
    with tempfile.TemporaryDirectory(prefix="music-bot-permissions-") as folder:
        root = Path(folder)
        album = root / "music" / "Artist" / "Album"
        staging = root / "staging"
        data = root / "data"
        album.mkdir(parents=True)
        staging.mkdir()
        data.mkdir()
        # The library belongs to another owner, exactly like the real mount:
        # only the shared group makes it writable.
        for directory in (root, root / "music", album.parent, album, staging):
            os.chown(directory, 0, 1000)
            directory.chmod(0o775)
        os.chown(data, 1000, 1000)
        source = staging / "track.mp3"
        tags = ID3()
        tags.add(TIT2(encoding=3, text="Before"))
        tags.save(source)
        os.chown(source, 0, 1000)
        source.chmod(0o664)
        script = r"""
import asyncio
import os
import stat
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
os.umask(0o002)  # what main() does
from library.files import _ensure_album_dir
from soulseek import downloader
from mutagen.id3 import ID3, TIT2
import journal

root = Path(sys.argv[1])
assert os.geteuid() == 1000
album = Path(_ensure_album_dir(str(root / "music"), "Artist", "Album"))
assert album.stat().st_uid == 0
assert stat.S_IMODE(album.stat().st_mode) == 0o775
track = album / "track.mp3"
downloader._move_into_library(str(root / "staging" / "track.mp3"), str(track))
assert track.stat().st_uid == 0  # same-filesystem rename retains foreign owner
assert stat.S_IMODE(track.stat().st_mode) == 0o664
tags = ID3(track)
tags.add(TIT2(encoding=3, text="After"))
tags.save(track)
assert ID3(track)["TIT2"].text == ["After"]

# A brand-new artist directory is group-writable on creation, no chmod.
fresh = Path(_ensure_album_dir(str(root / "music"), "Newcomer", "Debut"))
assert stat.S_IMODE(fresh.stat().st_mode) == 0o775
assert stat.S_IMODE(fresh.parent.stat().st_mode) == 0o775
assert fresh.stat().st_uid == 1000

class Response:
    status = 200
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def read(self): return b"cover"
class Session:
    def get(self, *args, **kwargs): return Response()
async def session(): return Session()
downloader._get_session = session
assert asyncio.run(downloader._download_cover("https://example.invalid/cover", str(album))) == b"cover"
assert stat.S_IMODE((album / "cover.jpg").stat().st_mode) == 0o664
uploads = root / "data" / "uploads"
uploads.mkdir()
incoming = uploads / "incoming"
incoming.write_bytes(b"upload")
incoming.rename(uploads / "ready")
(uploads / "ready").unlink()
journal.JOURNAL_PATH = str(root / "data" / "pending-downloads.json")
journal._write([])
journal._write([])  # atomic replacement of existing state must also work
assert Path(journal.JOURNAL_PATH).read_text().strip() == "[]"
assert stat.S_IMODE(Path(journal.JOURNAL_PATH).stat().st_mode) == 0o664
"""
        env = os.environ.copy()
        env["CONFIG_FILE"] = "/dev/null"
        result = subprocess.run(
            [sys.executable, "-c", script, folder,
             str(Path(__file__).resolve().parents[1] / "src")],
            user=1000, group=1000, extra_groups=[], env=env,
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
