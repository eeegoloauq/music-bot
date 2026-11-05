"""Import helpers shared by local uploads and optional download providers."""

import asyncio
import contextlib
import errno
import logging
import os
import shutil

import aiohttp

from metadata.client import _get_session
from library.files import _cover_url
from library.tagger import _write_tags, _write_m4a_tags, _write_mp3_tags

logger = logging.getLogger(__name__)


async def run_to_completion(coro):
    """Await ``coro`` in its own task, so a ``Task.cancel()`` aimed at the
    caller lets it finish before the CancelledError continues.

    For steps that must not be left half-done when the user taps ✖ Cancel
    (docs/cancel.md): an enqueue whose HTTP request is already on the wire —
    abandoning the await would leave slskd downloading a file nobody will
    collect — or a move + tag write running in a worker thread. The thread
    would run to the end anyway; waiting for it keeps the caller's
    bookkeeping (saved counts, the cleanup that follows) in step with disk.
    """
    fut = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await asyncio.shield(fut)
        raise


async def _download_cover(cover_uuid: str, album_dir: str) -> bytes | None:
    if not cover_uuid:
        return None
    cover_path = os.path.join(album_dir, "cover.jpg")
    if os.path.exists(cover_path):
        with open(cover_path, "rb") as f:
            return f.read()
    cover_url = _cover_url(cover_uuid)
    session = await _get_session()
    try:
        async with session.get(cover_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                cover_data = await resp.read()
                with open(cover_path, "wb") as f:
                    f.write(cover_data)
                return cover_data
    except Exception:
        logger.warning("Failed to download cover art")
    return None


def _relocate(src: str, dst: str) -> None:
    """Move ``src`` to ``dst``, renaming when they share a filesystem.

    A rename is a metadata op, not a multi-MB byte copy — slskd's staging dir
    and the library both live under the /media/music mount, so that is the
    normal path. Across devices (the upload intake under /data, say) the copy
    is written through ``open`` rather than ``shutil.copy2``: copy2 would carry
    over the mode of whoever produced the source — a Samba client's 0644 — and
    the track would land in the shared library without group write. Creating
    the file ourselves gives it this process's umask instead.
    """
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst)
    os.unlink(src)


def _move_into_library(src: str, dest: str) -> None:
    """Move a staged audio file into the library album
    dir. Lands on ``dest + .importing`` first so Navidrome's scanner never sees
    a partial file, then renames into place.
    """
    tmp = dest + ".importing"
    try:
        _relocate(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _write_tags_force(filepath: str, track: dict, album: dict,
                      cover_data: bytes | None, lyrics: dict | None,
                      ext: str):
    """Write canonical metadata when importing an audio file.
    """
    if ext == "m4a":
        _write_m4a_tags(filepath, track, album, None, cover_data, lyrics, force=True)
    elif ext == "mp3":
        _write_mp3_tags(filepath, track, album, None, cover_data, lyrics, force=True)
    else:
        _write_tags(filepath, track, album, None, cover_data, lyrics, force=True)


def _result_dict(album_dir, downloaded, skipped, failed, total, fmt, with_lyrics,
                 elapsed_secs: float = 0.0,
                 source_counts: dict[str, int] | None = None) -> dict:
    return {
        "album_dir": album_dir,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "total": total,
        "format": fmt,
        "with_lyrics": with_lyrics,
        "elapsed_secs": elapsed_secs,
        "source_counts": source_counts or {},
    }
