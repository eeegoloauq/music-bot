"""Watched-folder intake for locally supplied albums.

The owner drops a ``.zip`` (or a plain folder of tracks, or a loose audio
file) into ``UPLOAD_DIR``; the poll loop notices it, stages the audio under
``UPLOAD_DIR/.extracted/<uuid>/`` and reports what it found. Phase 1 stops
there — no tagging, no ``/music`` writes yet (see docs/local-upload-plan.md).

Anything that can't be staged (bad zip, size caps, no audio inside) is moved
to ``UPLOAD_DIR/.rejected/`` so the watcher never chews the same entry twice.
A plain poll loop is used instead of a filesystem-watch dependency: an entry
counts as "done copying" once its size is stable across two ticks (uploads
via the web endpoint will sidestep this with rename-on-complete).
"""

import asyncio
import logging
import os
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field

from config import MAX_FILE_BYTES, UPLOAD_DIR, UPLOAD_MAX_TOTAL_BYTES

logger = logging.getLogger(__name__)

AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
ART_EXTS = {".jpg", ".jpeg", ".png"}

_EXTRACTED = ".extracted"
_REJECTED = ".rejected"

POLL_SECS = 5.0
_COPY_CHUNK = 1 << 20


@dataclass
class IntakeReport:
    """What one dropped zip/folder turned into."""
    name: str                                # original entry name in UPLOAD_DIR
    staging_dir: str | None = None           # set on success
    audio: list[str] = field(default_factory=list)    # staged, relative paths
    art: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "member (reason)"
    error: str | None = None                 # set → entry was rejected


def _classify(name: str) -> str:
    """'audio' | 'art' | 'skip' for a member/file name."""
    base = os.path.basename(name)
    if base.startswith("."):  # dotfiles, AppleDouble ._track.flac droppings
        return "skip"
    ext = os.path.splitext(base)[1].lower()
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in ART_EXTS:
        return "art"
    return "skip"


def _unpack_zip(zip_path: str, staging_dir: str) -> IntakeReport:
    """Extract audio + art members of ``zip_path`` into ``staging_dir``.

    Cap violations and traversal attempts refuse the whole upload (the
    half-written staging dir is removed by the caller via ``report.error``).
    Sizes are enforced on actual streamed bytes, not the zip header — a
    lying header can't smuggle a bomb through.
    """
    report = IntakeReport(name=os.path.basename(zip_path))
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            kind = _classify(info.filename)
            if kind == "skip":
                report.skipped.append(info.filename)
                continue
            dest = os.path.realpath(os.path.join(staging_dir, info.filename))
            if not dest.startswith(os.path.realpath(staging_dir) + os.sep):
                report.error = f"unsafe path in zip: {info.filename}"
                return report
            limit = min(MAX_FILE_BYTES or UPLOAD_MAX_TOTAL_BYTES,
                        UPLOAD_MAX_TOTAL_BYTES - total)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                written = 0
                while chunk := src.read(_COPY_CHUNK):
                    written += len(chunk)
                    if written > limit:
                        report.error = f"size cap exceeded at {info.filename}"
                        return report
                    out.write(chunk)
            total += written
            (report.audio if kind == "audio" else report.art).append(info.filename)
    return report


def _inventory_tree(src_dir: str, report: IntakeReport) -> bool:
    """Fill ``report`` from an already-on-disk tree; False on cap violation."""
    total = 0
    for root, _dirs, files in os.walk(src_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), src_dir)
            kind = _classify(f)
            if kind == "skip":
                report.skipped.append(rel)
                continue
            size = os.path.getsize(os.path.join(root, f))
            total += size
            if (MAX_FILE_BYTES and size > MAX_FILE_BYTES) or total > UPLOAD_MAX_TOTAL_BYTES:
                report.error = f"size cap exceeded at {rel}"
                return False
            (report.audio if kind == "audio" else report.art).append(rel)
    return True


def _process_entry(path: str, upload_dir: str) -> IntakeReport:
    """Stage one ready entry (zip / folder / loose audio file) from the
    watched dir. On success the source is consumed (zip deleted, folder
    moved); on failure it's parked in ``.rejected/``. Never raises."""
    name = os.path.basename(path)
    staging = os.path.join(upload_dir, _EXTRACTED, uuid.uuid4().hex[:12])

    try:
        if os.path.isdir(path):
            report = IntakeReport(name=name)
            if _inventory_tree(path, report) and report.audio:
                os.makedirs(os.path.dirname(staging), exist_ok=True)
                shutil.move(path, staging)
                report.staging_dir = staging
            elif not report.error:
                report.error = "no audio files inside"
        elif zipfile.is_zipfile(path):
            os.makedirs(staging, exist_ok=True)
            report = _unpack_zip(path, staging)
            if not report.error and not report.audio:
                report.error = "no audio files inside"
            if report.error:
                shutil.rmtree(staging, ignore_errors=True)
            else:
                report.staging_dir = staging
                os.remove(path)
        elif _classify(name) == "audio":
            report = IntakeReport(name=name, audio=[name])
            if MAX_FILE_BYTES and os.path.getsize(path) > MAX_FILE_BYTES:
                report.error = "size cap exceeded"
                report.audio = []
            else:
                os.makedirs(staging, exist_ok=True)
                shutil.move(path, os.path.join(staging, name))
                report.staging_dir = staging
        else:
            report = IntakeReport(name=name, error="not a zip, folder, or audio file")
    except Exception as e:  # bad zip, fs error — reject, don't kill the loop
        logger.exception("Upload intake failed for %s", name)
        report = IntakeReport(name=name, error=str(e))

    if report.error and os.path.exists(path):
        try:
            rejected = os.path.join(upload_dir, _REJECTED)
            os.makedirs(rejected, exist_ok=True)
            dest = os.path.join(rejected, name)
            if os.path.exists(dest):  # same name re-dropped — keep both
                dest = os.path.join(rejected, f"{uuid.uuid4().hex[:6]}-{name}")
            shutil.move(path, dest)
        except OSError:
            logger.exception("Could not park rejected upload %s", name)
    return report


def _entry_size(path: str) -> int:
    if not os.path.isdir(path):
        return os.path.getsize(path)
    return sum(
        os.path.getsize(os.path.join(root, f))
        for root, _dirs, files in os.walk(path)
        for f in files
    )


def _scan_stable(upload_dir: str, prev: dict[str, int]) -> tuple[list[str], dict[str, int]]:
    """One poll tick: return (entries whose size matched the previous tick,
    sizes-by-name for the next tick). First sighting is never ready — that's
    the "stable across two ticks" copy-finished heuristic."""
    ready: list[str] = []
    sizes: dict[str, int] = {}
    for name in os.listdir(upload_dir):
        if name.startswith("."):
            continue
        path = os.path.join(upload_dir, name)
        try:
            sizes[name] = _entry_size(path)
        except OSError:
            continue  # vanished / still appearing — next tick
        if prev.get(name) == sizes[name]:
            ready.append(path)
    return ready, sizes


def format_rejection(r: IntakeReport) -> str:
    return (f"❌ Upload rejected: {r.name}\n"
            f"Reason: {r.error}\nMoved to {_REJECTED}/ — fix and re-drop.")


async def watch_loop(handle) -> None:
    """Poll ``UPLOAD_DIR`` forever; ``handle`` is an async
    ``(IntakeReport) -> None`` that owns everything after staging —
    identify/tag/file (upload_import) and telling the owner."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    logger.info("Watching %s for local uploads", UPLOAD_DIR)
    prev: dict[str, int] = {}
    stuck: set[str] = set()  # processed but couldn't be consumed/parked — don't re-chew
    while True:
        try:
            ready, prev = await asyncio.to_thread(_scan_stable, UPLOAD_DIR, prev)
            for path in ready:
                if os.path.basename(path) in stuck:
                    continue
                report = await asyncio.to_thread(_process_entry, path, UPLOAD_DIR)
                prev.pop(os.path.basename(path), None)
                if os.path.exists(path):
                    stuck.add(os.path.basename(path))
                logger.info("Upload intake %s: %s", report.name,
                            report.error or f"{len(report.audio)} audio staged")
                await handle(report)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Upload watch tick failed")
        await asyncio.sleep(POLL_SECS)
