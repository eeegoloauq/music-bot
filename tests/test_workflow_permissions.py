"""Exercise real filesystem policy with unprivileged writers in scratch trees."""

import errno
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest


@pytest.fixture
def sandbox():
    # Root only prepares fixtures and selects identities. All workflow code
    # executes after dropping privileges, including supplementary group setup.
    foreign = os.geteuid() == 0
    writer = 22001 if foreign else os.geteuid()
    group = 22000 if foreign else os.getegid()
    with tempfile.TemporaryDirectory(prefix="musicbot-workflow-permissions-") as name:
        root = Path(name)
        for path in (root, root / "music", root / "staging"):
            path.mkdir(exist_ok=True)
            if foreign:
                os.chown(path, 22002, group)
            path.chmod(0o2775)

        def run(script, *, observer=False):
            env = os.environ.copy()
            env["CONFIG_FILE"] = "/dev/null"
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            identity = {}
            if foreign:
                uid = 22003 if observer else writer
                identity = dict(user=uid, group=uid, extra_groups=[group])
            result = subprocess.run(
                [sys.executable, "-c", "import os; os.umask(0o002)\n" + script, str(root)],
                env=env, capture_output=True, text=True, timeout=30, **identity,
            )
            assert result.returncode == 0, result.stdout + result.stderr

        yield SimpleNamespace(root=root, run=run, foreign=foreign, writer=writer, group=group)


@pytest.mark.parametrize("source_mode", [0o644, 0o600, 0o755])
def test_original_upload_uses_library_policy_and_preserves_bytes(sandbox, source_mode):
    source = sandbox.root / "staging" / "song.flac"
    # This path bypasses tag parsing by design; every byte must survive.
    original = b"audio\x00tags\xffembedded artwork\x00lyrics" * 100
    source.write_bytes(original)
    if source_mode == 0o600 and sandbox.foreign:
        os.chown(source, sandbox.writer, sandbox.group)
    source.chmod(source_mode)
    (source.parent / "cover.jpg").write_bytes(b"supplied cover")
    sandbox.run(r'''
import asyncio
import sys
from pathlib import Path
import upload_import
from uploads import IntakeReport
root = Path(sys.argv[1])
album, result = asyncio.run(upload_import.import_original(
    IntakeReport(name="Album", staging_dir=str(root / "staging")), str(root / "music")))
assert result["downloaded"] == 1
''')
    target = sandbox.root / "music" / "Unknown Artist" / "Album" / "song.flac"
    assert target.read_bytes() == original
    for path in (target, target.parent / "cover.jpg"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o664
        assert path.stat().st_uid == sandbox.writer
        assert path.stat().st_gid == sandbox.group
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o2775
    assert not list(sandbox.root.rglob("*.tmp"))
    sandbox.run(r'''
import sys
from pathlib import Path
root = Path(sys.argv[1])
for path in (root / "music").rglob("*"):
    if path.is_file():
        with path.open("r+b") as stream:
            data = stream.read()
            stream.seek(0)
            stream.write(data)
''', observer=True)


def test_matched_upload_and_supplied_sidecars_get_shared_modes(sandbox):
    source = sandbox.root / "staging" / "track.wav"
    source.write_bytes(b"supplied audio")
    source.chmod(0o644)
    (source.parent / "notes.txt").write_bytes(b"supplied notes")
    sandbox.run(r'''
import sys
from pathlib import Path
from upload_import import _file_track, _preserve_extras
root = Path(sys.argv[1])
progress = {}
_preserve_extras(str(root / "staging"), str(root / "music"))
_file_track(str(root / "staging" / "track.wav"), str(root / "music" / "track.wav"),
            {}, {}, None, None, "wav", progress)
assert progress == {"saved": 1}
''')
    for filename in ("track.wav", "notes.txt"):
        path = sandbox.root / "music" / filename
        assert stat.S_IMODE(path.stat().st_mode) == 0o664
        assert path.stat().st_gid == sandbox.group
    assert not source.exists()


def test_new_and_replacement_artwork_are_shared_and_keep_directory_group(sandbox):
    cover = sandbox.root / "music" / "cover.jpg"
    sandbox.run(r'''
import sys
from pathlib import Path
from maintenance import FileUpdate, _write_atomic
root = Path(sys.argv[1])
cover = root / "music" / "cover.jpg"
_write_atomic(FileUpdate(str(cover), None, {"cover_file": b"new art"}), str(cover.parent))
''')
    assert cover.read_bytes() == b"new art"
    assert stat.S_IMODE(cover.stat().st_mode) == 0o664
    # Give the existing artwork to a different group writer for replacement.
    if sandbox.foreign:
        os.chown(cover, 22002, sandbox.group)
    inode = cover.stat().st_ino
    sandbox.run(r'''
import sys
from pathlib import Path
from maintenance import FileUpdate, _write_atomic, fingerprint
cover = Path(sys.argv[1]) / "music" / "cover.jpg"
_write_atomic(FileUpdate(str(cover), fingerprint(str(cover)), {"cover_file": b"replacement"}),
              str(cover.parent))
''')
    assert cover.read_bytes() == b"replacement"
    assert cover.stat().st_ino != inode
    assert cover.stat().st_uid == sandbox.writer
    assert cover.stat().st_gid == sandbox.group
    assert stat.S_IMODE(cover.stat().st_mode) == 0o664
    sandbox.run(r'''
import sys
from pathlib import Path
cover = Path(sys.argv[1]) / "music" / "cover.jpg"
with cover.open("r+b") as stream:
    assert stream.read() == b"replacement"
    stream.seek(0)
    stream.write(b"replacement")
''', observer=True)
    assert not list(cover.parent.glob("*.tmp"))


@pytest.mark.parametrize("workflow", ["maintenance", "matched_import"])
def test_tags_on_foreign_owned_audio_use_library_policy(sandbox, workflow):
    if not sandbox.foreign:
        pytest.skip("requires root only to prepare a foreign-owned fixture")
    if not shutil.which("ffmpeg"):
        pytest.skip("requires ffmpeg")
    path = sandbox.root / ("music" if workflow == "maintenance" else "staging") / "song.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=mono", "-t", "0.1", str(path)], check=True)
    from mutagen.flac import FLAC
    audio = FLAC(path)
    audio["title"] = "Before"
    audio.save()
    os.chown(path, 22002, sandbox.group)
    path.chmod(0o664 if workflow == "maintenance" else 0o644)
    inode = path.stat().st_ino
    if workflow == "maintenance":
        sandbox.run(r'''
import sys
from pathlib import Path
from maintenance import FileUpdate, _write_atomic, fingerprint
from mutagen.flac import FLAC
path = Path(sys.argv[1]) / "music" / "song.flac"
assert path.stat().st_uid != os.geteuid()
_write_atomic(FileUpdate(str(path), fingerprint(str(path)), {"title": "After"}), str(path.parent))
assert FLAC(path)["title"] == ["After"]
''')
    else:
        sandbox.run(r'''
import sys
from pathlib import Path
from upload_import import _file_track
from mutagen.flac import FLAC
root = Path(sys.argv[1])
source, path = root / "staging" / "song.flac", root / "music" / "song.flac"
assert source.stat().st_uid != os.geteuid()
track = {"title": "After", "artist": "Artist", "trackNumber": 1}
album = {"title": "Album", "artist": "Artist", "id": "42", "numberOfTracks": 1}
_file_track(str(source), str(path), track, album, None, None, "flac", {})
assert FLAC(path)["title"] == ["After"]
assert not source.exists()
''')
        path = sandbox.root / "music" / "song.flac"
    assert path.stat().st_uid == sandbox.writer
    assert path.stat().st_ino != inode
    assert path.stat().st_gid == sandbox.group
    assert stat.S_IMODE(path.stat().st_mode) == 0o664
    sandbox.run(r'''
import sys
from pathlib import Path
from mutagen.flac import FLAC
path = Path(sys.argv[1]) / "music" / "song.flac"
audio = FLAC(path)
audio["genre"] = "Other writer"
audio.save()
''', observer=True)
    assert FLAC(path)["genre"] == ["Other writer"]


@pytest.mark.parametrize("policy", ["read_only", "foreign_read_only", "hard_link", "acl"])
def test_maintenance_refuses_policies_atomic_replacement_cannot_honor(sandbox, policy):
    path = sandbox.root / "music" / "cover.jpg"
    path.write_bytes(b"original")
    if sandbox.foreign:
        os.chown(path, sandbox.writer if policy != "foreign_read_only" else 22002, sandbox.group)
    elif policy == "foreign_read_only":
        pytest.skip("requires a foreign owner")
    path.chmod(0o644 if policy == "foreign_read_only" else 0o444 if policy == "read_only" else 0o664)
    if policy == "hard_link":
        os.link(path, path.with_name("linked.jpg"))
    elif policy == "acl":
        # Linux access ACL with a named reader; requires no external ACL tool.
        acl = struct.pack("<I", 2) + b"".join(
            struct.pack("<HHI", tag, perm, uid) for tag, perm, uid in
            [(1, 6, 0xffffffff), (2, 4, 22004), (4, 6, 0xffffffff),
             (16, 6, 0xffffffff), (32, 4, 0xffffffff)])
        try:
            os.setxattr(path, "system.posix_acl_access", acl)
        except OSError as exc:
            if exc.errno == errno.ENOTSUP:
                pytest.skip("filesystem has no POSIX ACL support")
            raise
    before = path.stat()
    sandbox.run(r'''
import sys
from pathlib import Path
from maintenance import FileUpdate, _write_atomic, fingerprint
path = Path(sys.argv[1]) / "music" / "cover.jpg"
try:
    _write_atomic(FileUpdate(str(path), fingerprint(str(path)), {"cover_file": b"replacement"}),
                  str(path.parent))
except (PermissionError, ValueError):
    pass
else:
    raise AssertionError("policy was bypassed through directory write permission")
''')
    assert path.read_bytes() == b"original"
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_uid == before.st_uid
    assert path.stat().st_mode == before.st_mode
    assert not list(path.parent.glob("*.tmp"))


def test_failures_collisions_and_cancellation_leave_complete_files(sandbox):
    sandbox.run(r'''
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
from pathlib import Path
import maintenance
from library import writing
from library.importing import run_to_completion
from upload_import import _file_original
root = Path(sys.argv[1])
folder = root / "music"
source = root / "staging" / "track.flac"
source.write_bytes(b"original upload")
dest = folder / "track.flac"

# Preparation failure leaves the source and any original destination intact.
real_copy = writing.shutil.copyfileobj
def fail(src, dst):
    dst.write(b"partial")
    raise OSError("simulated full disk")
writing.shutil.copyfileobj = fail
try:
    _file_original(str(source), str(dest), {"saved": 0}, True)
except OSError:
    pass
else:
    raise AssertionError("failed copy accepted")
assert source.read_bytes() == b"original upload" and not dest.exists()
writing.shutil.copyfileobj = real_copy

# Exclusive publication, including a destination appearing during preparation.
def race(src, dst):
    real_copy(src, dst)
    dest.write_bytes(b"other writer")
writing.shutil.copyfileobj = race
try:
    _file_original(str(source), str(dest), {"saved": 0}, True)
except FileExistsError:
    pass
else:
    raise AssertionError("existing library file overwritten")
assert dest.read_bytes() == b"other writer"
assert source.read_bytes() == b"original upload"
writing.shutil.copyfileobj = real_copy

# Maintenance tag preparation failure retains the original inode and content.
update = maintenance.FileUpdate(str(dest), maintenance.fingerprint(str(dest)), {"title": "new"})
before = dest.stat().st_ino
def fail_tags(path, values):
    Path(path).write_bytes(b"partial tag write")
    raise OSError("simulated tag failure")
maintenance.write_selected_tags = fail_tags
try:
    maintenance._write_atomic(update, str(folder))
except OSError:
    pass
else:
    raise AssertionError("failed tag write accepted")
assert dest.read_bytes() == b"other writer" and dest.stat().st_ino == before

# Final rename failure also leaves the original intact and removes the temp.
real_replace = maintenance.os.replace
def fail_replace(*args):
    raise OSError("simulated rename failure")
maintenance.os.replace = fail_replace
try:
    maintenance._write_atomic(maintenance.FileUpdate(str(dest), maintenance.fingerprint(str(dest)),
                              {"cover_file": b"new art"}), str(folder))
except OSError:
    pass
else:
    raise AssertionError("failed rename accepted")
maintenance.os.replace = real_replace
assert dest.read_bytes() == b"other writer" and dest.stat().st_ino == before

# Cancellation waits for the worker, preserving completion and cleanup semantics.
started, finish = threading.Event(), threading.Event()
def slow_tags(path, values):
    started.set()
    assert finish.wait(5)
    Path(path).write_bytes(b"complete update")
maintenance.write_selected_tags = slow_tags
async def cancel_during_write():
    with ThreadPoolExecutor() as waiter:
        task = asyncio.create_task(run_to_completion(asyncio.to_thread(maintenance._write_atomic,
                                    update, str(folder))))
        assert await asyncio.get_running_loop().run_in_executor(waiter, started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancellation was swallowed")
asyncio.run(cancel_during_write())
assert dest.read_bytes() == b"complete update"
assert not list(folder.glob("*.tmp"))
''')


def test_creation_obeys_umask_and_exclusive_random_names(sandbox):
    sandbox.run(r'''
# A deliberately stricter process policy must not be overridden with chmod.
os.umask(0o027)
import sys
from pathlib import Path
from library import writing
from maintenance import FileUpdate, _write_atomic, fingerprint
folder = Path(sys.argv[1]) / "music"
victim = folder / "victim"
victim.write_bytes(b"untouched")
collision = folder / ".musicbot-collision.tmp"
collision.symlink_to(victim)
names = iter(["collision", "exclusive"])
real_token = writing.secrets.token_hex
writing.secrets.token_hex = lambda _: next(names)
with writing.new_library_file(str(folder / "cover.jpg")) as stream:
    stream.write(b"art")
writing.secrets.token_hex = real_token
cover = folder / "cover.jpg"
assert cover.stat().st_mode & 0o777 == 0o640
_write_atomic(FileUpdate(str(cover), fingerprint(str(cover)), {"cover_file": b"updated"}), str(folder))
assert cover.stat().st_mode & 0o777 == 0o640
assert collision.is_symlink() and victim.read_bytes() == b"untouched"
collision.unlink()
assert not list(folder.glob("*.tmp"))
''')


def test_creation_inherits_directory_acl_and_maintenance_refuses_to_discard_it(sandbox):
    folder = sandbox.root / "music"
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, perm, uid) for tag, perm, uid in
        [(1, 7, 0xffffffff), (2, 4, 22004), (4, 7, 0xffffffff),
         (16, 7, 0xffffffff), (32, 5, 0xffffffff)])
    try:
        os.setxattr(folder, "system.posix_acl_default", acl)
    except OSError as exc:
        if exc.errno == errno.ENOTSUP:
            pytest.skip("filesystem has no POSIX ACL support")
        raise
    sandbox.run(r'''
import sys
from pathlib import Path
from library.writing import new_library_file
from maintenance import FileUpdate, _write_atomic, fingerprint
folder = Path(sys.argv[1]) / "music"
cover = folder / "cover.jpg"
with new_library_file(str(cover)) as stream:
    stream.write(b"art")
assert os.getxattr(cover, "system.posix_acl_access")
assert cover.stat().st_mode & 0o777 == 0o664
try:
    _write_atomic(FileUpdate(str(cover), fingerprint(str(cover)), {"cover_file": b"updated"}), str(folder))
except ValueError as exc:
    assert "ACL" in str(exc)
else:
    raise AssertionError("file access ACL discarded")
assert cover.read_bytes() == b"art"
assert not list(folder.glob("*.tmp"))
''')
