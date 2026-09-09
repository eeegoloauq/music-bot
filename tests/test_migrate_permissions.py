"""The migration script runs as root, so its refusals matter more than its work.

It only ever touches this stack's own three directories. The library is
reported on and left alone: a recursive chgrp as root over a path read from a
config file has no safe failure mode, so the commands for it are printed for a
person to read and run. Those printed commands are executed here, because a
test that only greps the text proves nothing about what they do.
"""

import grp
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate-permissions.sh"

needs_root = pytest.mark.skipif(
    os.geteuid() != 0, reason="changing ownership requires root"
)


@pytest.fixture
def shared_gid():
    """Any real group that is not root, standing in for the library group."""
    for entry in grp.getgrall():
        if entry.gr_gid not in (0, os.getgid()) and 100 <= entry.gr_gid < 65000:
            return entry.gr_gid
    pytest.skip("no suitable group on this host")


def make_install(tmp_path, library_gid, library_value=None):
    """An install as it looks after years of the bot running as root."""
    root = tmp_path / "stack"
    real = root / "store" / "music"
    (real / "Artist" / "Album").mkdir(parents=True)
    (real / ".slskd-downloads" / ".incomplete").mkdir(parents=True)
    (real / "lost+found").mkdir()
    (root / "bot-data" / "uploads").mkdir(parents=True)
    (root / "slskd-config").mkdir()
    track = real / "Artist" / "Album" / "old.flac"
    track.touch()

    if os.geteuid() == 0:
        for path in [real, *real.rglob("*"), root / "bot-data",
                     root / "bot-data" / "uploads", root / "slskd-config"]:
            os.chown(path, 0, 0)
        os.chown(real, 0, library_gid)
    for path in real.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    (real / "lost+found").chmod(0o700)

    (root / "compose.yaml").write_text("services: {}\n")
    (root / ".env").write_text(f"MUSIC_LIBRARY_DIR={library_value or real}\n")
    return root, real, track


def run(cwd, *args):
    return subprocess.run([str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True)


# /bin, /lib and /sbin resolve into /usr on a merged-usr system, and /tmp is
# /var/tmp on some. A refusal that only knew one spelling would wave the other
# one through and then recurse into it as root.
@pytest.mark.parametrize(
    "bogus",
    ["/", "/media", "/home", "/var", "/etc", "/bin", "/lib", "/sbin", "/tmp",
     "/usr/bin", "/etc/music", "/srv", "/var/lib", "/var/log", "/root/music", "/media/../usr/bin"],
)
def test_refuses_a_library_path_that_is_a_typo(tmp_path, bogus):
    root, _, _ = make_install(tmp_path, os.getgid(), library_value=bogus)
    result = run(root, "--yes")
    assert result.returncode != 0
    assert "looks wrong" in result.stderr or "too close to the root" in result.stderr


def test_refuses_a_missing_library(tmp_path):
    root, _, _ = make_install(tmp_path, os.getgid(), library_value="/no/such/library")
    result = run(root, "--yes")
    assert result.returncode != 0
    assert "not a directory" in result.stderr


def test_refuses_a_non_numeric_id(tmp_path):
    root, real, _ = make_install(tmp_path, os.getgid())
    (root / ".env").write_text(f"MUSIC_LIBRARY_DIR={real}\nMUSICBOT_GID=audio\n")
    result = run(root, "--yes")
    assert result.returncode != 0
    assert "numeric" in result.stderr


@needs_root
def test_refuses_when_the_library_group_is_root(tmp_path):
    root, _, _ = make_install(tmp_path, 0)
    result = run(root, "--yes")
    assert result.returncode != 0
    assert "root" in result.stderr


@needs_root
@pytest.mark.parametrize("target", ["bot-data", "slskd-config", "staging"])
def test_refuses_a_symlinked_target(tmp_path, shared_gid, target):
    root, real, _ = make_install(tmp_path, shared_gid)
    if target == "staging":
        path = real / ".slskd-downloads"
    else:
        path = root / target
    elsewhere = tmp_path / f"elsewhere-{target}"
    path.rename(elsewhere)
    path.symlink_to(elsewhere)

    result = run(root, "--yes")
    assert result.returncode != 0
    assert "symlink" in result.stderr


@needs_root
def test_resolves_a_symlinked_library(tmp_path, shared_gid):
    root, real, _ = make_install(tmp_path, shared_gid)
    link = root / "link"
    link.symlink_to(real)
    (root / ".env").write_text(f"MUSIC_LIBRARY_DIR={link}\n")

    assert run(root, "--yes").returncode == 0
    staging = real / ".slskd-downloads"
    assert (staging.stat().st_uid, staging.stat().st_gid) == (1000, shared_gid)


@needs_root
def test_quoted_value_in_env(tmp_path, shared_gid):
    root, real, _ = make_install(tmp_path, shared_gid)
    (root / ".env").write_text(f'MUSIC_LIBRARY_DIR="{real}"\n')
    assert run(root, "--yes").returncode == 0
    assert (real / ".slskd-downloads").stat().st_uid == 1000


@needs_root
def test_hands_over_its_own_directories_only(tmp_path, shared_gid):
    root, real, track = make_install(tmp_path, shared_gid)
    before = (track.stat().st_uid, track.stat().st_gid, track.stat().st_mode)

    assert run(root, "--yes").returncode == 0

    for owned in (root / "bot-data", root / "bot-data" / "uploads",
                  root / "slskd-config", real / ".slskd-downloads",
                  real / ".slskd-downloads" / ".incomplete"):
        assert (owned.stat().st_uid, owned.stat().st_gid) == (1000, shared_gid), owned

    assert (track.stat().st_uid, track.stat().st_gid, track.stat().st_mode) == before, \
        "the library belongs to the operator, not to this script"
    fsck = real / "lost+found"
    assert (fsck.stat().st_uid, fsck.stat().st_gid) == (0, 0)
    assert fsck.stat().st_mode & 0o777 == 0o700

    assert f"MUSICBOT_GID={shared_gid}" in (root / ".env").read_text()
    assert run(root, "--yes").returncode == 0, "running it twice must be safe"


@needs_root
def test_the_printed_library_commands_do_what_they_claim(tmp_path, shared_gid):
    root, real, track = make_install(tmp_path, shared_gid)
    outsider = tmp_path / "outsider.flac"
    outsider.touch()
    os.chown(outsider, 0, 0)
    (real / "Artist" / "escape.flac").symlink_to(outsider)

    result = run(root, "--yes")
    assert result.returncode == 0
    commands = re.findall(r"^\s+sudo (find .+)$", result.stdout, re.MULTILINE)
    assert len(commands) == 3, result.stdout
    for command in commands:
        assert subprocess.run(["bash", "-c", command]).returncode == 0

    assert track.stat().st_gid == shared_gid
    assert track.stat().st_uid == 0, "owners are kept"
    assert track.stat().st_mode & 0o020, "group write is what lets the bot retag it"
    album = real / "Artist" / "Album"
    assert album.stat().st_mode & 0o2000, "setgid keeps new files in the group"
    assert album.stat().st_mode & 0o070 == 0o070

    fsck = real / "lost+found"
    assert (fsck.stat().st_gid, fsck.stat().st_mode & 0o777) == (0, 0o700), \
        "lost+found must be pruned, not merely filtered by name"
    assert (outsider.stat().st_uid, outsider.stat().st_gid) == (0, 0), \
        "a symlink inside the library must not drag its target into the group"

    # Nothing left to report once the commands have run.
    again = run(root, "--yes")
    assert "Nothing left to do" in again.stdout
