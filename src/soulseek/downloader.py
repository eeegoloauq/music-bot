"""Download orchestrator: takes Deezer-resolved metadata + uses Soulseek as the audio source.

Flow per album:
  1. caller fetches album metadata via ``metadata.fetch_album`` and passes it in
  2. scan local library for already-downloaded tracks
  3. fetch cover art from Deezer CDN (no auth)
  4. fetch lyrics from lrclib (parallel)
  5. try album-folder match on slskd; if no luck, per-track matching
  6. enqueue chosen files to slskd, wait for completion
  7. move from slskd's download dir into the library album dir (atomic via .importing)
  8. write Deezer-source-of-truth tags (force-overwrite mode)
"""

import asyncio
import contextlib
import logging
import os
import shutil
import time
from typing import Awaitable, Callable

import aiohttp

from metadata.client import _get_session
from metadata import fetch_album, fetch_lyrics, enrich_genres
from library.files import (
    _find_existing_track, _sanitize, _track_prefix, _ensure_album_dir,
    _cover_url,
)
from library.tagger import (
    _patch_missing_tags, _write_tags, _write_m4a_tags, _write_mp3_tags,
)

from soulseek import client as slskd
from soulseek.client import SearchResult
from soulseek.matcher import find_album, find_track, find_tracks_concurrent
from soulseek.scorer import ScoredTrack, ScoredFolder

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, int, int, str], Awaitable[None]] | None

# Where slskd dumps completed downloads (mounted into both slskd and music-bot
# containers). Override via env if your compose deviates.
SLSKD_DOWNLOAD_DIR = os.environ.get("SLSKD_DOWNLOAD_DIR", "/music/.slskd-downloads")

# Per-track download timeout (seconds). Soulseek peers can be slow; give them a chance.
DOWNLOAD_TIMEOUT_SECS = int(os.environ.get("SLSKD_DOWNLOAD_TIMEOUT", "900"))

# How long after enqueue before we give up if a peer never starts the transfer.
ENQUEUE_GRACE_SECS = 60


# --- helpers ---------------------------------------------------------------

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
                os.chmod(cover_path, 0o666)
                return cover_data
    except Exception:
        logger.warning("Failed to download cover art")
    return None


def _move_into_library(src: str, dest: str) -> None:
    """Atomic-ish move from slskd download dir to library album dir.
    Writes to ``dest + .importing`` first so Navidrome's scanner doesn't pick
    up a partially-written file.
    """
    tmp = dest + ".importing"
    try:
        if os.path.dirname(src) != os.path.dirname(dest):
            shutil.copy2(src, tmp)
        else:
            shutil.move(src, tmp)
        os.replace(tmp, dest)
        try:
            os.chmod(dest, 0o666)
        except OSError:
            pass
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dest):
            try:
                os.remove(src)
            except OSError:
                pass
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _format_label_from_result(r: SearchResult) -> str:
    ext = r.extension.upper()
    bd = r.bit_depth
    sr = r.sample_rate
    parts = [ext]
    if bd:
        parts.append(f"{bd}-bit")
    if sr:
        parts.append(f"{sr // 1000}kHz")
    return " ".join(parts) if parts else ext


async def _await_one_file(
    username: str,
    remote_filename: str,
    timeout_secs: int = DOWNLOAD_TIMEOUT_SECS,
    on_progress=None,
) -> str:
    """Wait for slskd to finish a single file. Returns the final state string.

    ``on_progress`` (optional) is invoked as ``await cb(pct, speed_bps, eta_sec)``
    whenever slskd reports a percent-complete change.
    """
    async def _wrap(_fname: str, pct: float, _state: str,
                    speed_bps: int, bytes_done: int, size: int):
        if on_progress is None:
            return
        eta_sec = 0
        if speed_bps and size and bytes_done < size:
            eta_sec = max(0, int((size - bytes_done) / max(speed_bps, 1)))
        with contextlib.suppress(Exception):
            await on_progress(pct, speed_bps, eta_sec)

    states = await slskd.wait_for_files(
        username=username,
        target_filenames=[remote_filename],
        timeout_secs=timeout_secs,
        progress_cb=_wrap,
    )
    return states.get(remote_filename, "Unknown")


def _write_tags_force(filepath: str, track: dict, album: dict,
                      cover_data: bytes | None, lyrics: dict | None,
                      ext: str):
    """Force-overwrite tags from Deezer metadata, replacing whatever the
    Soulseek peer baked in (their tags are unreliable / inconsistent).
    """
    if ext == "m4a":
        _write_m4a_tags(filepath, track, album, None, cover_data, lyrics, force=True)
    elif ext == "mp3":
        _write_mp3_tags(filepath, track, album, None, cover_data, lyrics, force=True)
    else:
        _write_tags(filepath, track, album, None, cover_data, lyrics, force=True)


# --- download a single chosen result --------------------------------------

async def _download_chosen(
    chosen: SearchResult,
    track: dict,
    album: dict,
    album_dir: str,
    cover_data: bytes | None,
    lyrics_task: asyncio.Task | None,
    on_progress=None,
) -> tuple[str, int, str]:
    """Enqueue a single file, wait, move to library, tag.

    ``on_progress`` (optional) is called as ``await cb(pct, speed_bps, eta_sec)``
    while the transfer progresses. Used by the album-level progress bar.

    Returns ``(filepath, bytes_transferred, fmt_label)``.
    Raises RuntimeError on transfer or move failure.
    """
    ok = await slskd.enqueue(chosen.username, [chosen])
    if not ok:
        raise RuntimeError(f"slskd refused enqueue from {chosen.username}")

    state = await _await_one_file(
        chosen.username, chosen.filename, on_progress=on_progress,
    )
    if "succeeded" not in state.lower():
        # Best-effort cancel so a stalled transfer doesn't keep the queue slot.
        with contextlib.suppress(Exception):
            await slskd.cancel_download(chosen.username, chosen.filename)
        raise RuntimeError(f"transfer ended in state '{state}'")

    src_path = slskd.find_local_file(SLSKD_DOWNLOAD_DIR, chosen.username, chosen.filename)
    if not src_path or not os.path.isfile(src_path):
        raise RuntimeError(f"could not locate downloaded file for {chosen.basename}")

    disc = track.get("discNumber", 1)
    num = track.get("trackNumber", 0)
    track_title = _sanitize(track["title"])
    prefix = _track_prefix(disc, num, album.get("numberOfVolumes", 1))
    ext = chosen.extension if chosen.extension in ("flac", "m4a", "mp3") else "flac"
    dest_path = os.path.join(album_dir, f"{prefix} {track_title}.{ext}")

    _move_into_library(src_path, dest_path)
    size = os.path.getsize(dest_path)

    lyrics = None
    if lyrics_task is not None:
        try:
            lyrics = await lyrics_task
        except Exception:
            lyrics = None

    _write_tags_force(dest_path, track, album, cover_data, lyrics, ext)
    fmt = _format_label_from_result(chosen)
    return dest_path, size, fmt


async def _try_candidates(
    candidates: list[SearchResult],
    track: dict,
    album: dict,
    album_dir: str,
    cover_data: bytes | None,
    lyrics_task: asyncio.Task | None,
    on_progress=None,
    max_attempts: int = 3,
) -> tuple[str, int, str, SearchResult] | None:
    """Try peers in order; return on first success. Skips candidates that
    fail with a transfer error (timeout, peer offline, etc) — not
    code-level errors, which propagate.
    """
    seen: set[tuple[str, str]] = set()
    last_err: str | None = None
    attempts = 0
    for cand in candidates:
        if cand is None:
            continue
        key = (cand.username, cand.filename)
        if key in seen:
            continue
        seen.add(key)
        if attempts >= max_attempts:
            break
        attempts += 1
        try:
            filepath, size, fmt = await _download_chosen(
                cand, track, album, album_dir, cover_data, lyrics_task,
                on_progress=on_progress,
            )
            return filepath, size, fmt, cand
        except RuntimeError as e:
            last_err = str(e)
            logger.warning("candidate %s failed for %s: %s",
                           cand.username, track.get("title"), e)
            continue
    if last_err:
        raise RuntimeError(last_err)
    return None


# --- public API ------------------------------------------------------------

async def find_lossy_candidates(track: dict, album_ctx: dict) -> list[SearchResult]:
    """Return mp3/m4a peer candidates for a track. Used by the mp3-fallback
    prompt to preview availability before asking the user to accept.
    """
    auto, alternatives = await find_track(
        track, album_artist=album_ctx.get("artist"), accept_lossy=True,
    )
    out: list[SearchResult] = []
    if auto:
        out.append(auto.result)
    out.extend(a.result for a in alternatives)
    return out


async def download_single_track(
    track: dict,
    album_ctx: dict,
    dest_dir: str,
    accept_lossy: bool = False,
    precomputed_candidates: list[SearchResult] | None = None,
) -> tuple[str, bool, str]:
    """Download one track. Returns ``(filepath, was_downloaded, format_label)``.
    If the file already exists in the library, patch missing tags and return
    ``was_downloaded=False``.

    ``accept_lossy=True`` tells the matcher to look for mp3≥256kbps + m4a
    instead of FLAC. ``precomputed_candidates`` skips the slskd search step
    when the caller already ran ``find_lossy_candidates`` (mp3-fallback path).
    """
    artist = _sanitize(album_ctx.get("artist", track["artist"]))
    album_title = _sanitize(album_ctx.get("title", "Singles"))
    album_dir = _ensure_album_dir(dest_dir, artist, album_title)

    existing = await asyncio.to_thread(_find_existing_track, album_dir, track)
    if existing:
        added = await _patch_missing_tags(existing, track, album_ctx)
        if added:
            logger.info("Track patched: %s — %s (%s)",
                        track["artist"], track["title"], ", ".join(added))
        else:
            logger.info("Track already exists: %s — %s",
                        track["artist"], track["title"])
        return existing, False, ""

    cover_data = await _download_cover(album_ctx.get("cover_uuid", ""), album_dir)
    lyrics_task = asyncio.create_task(fetch_lyrics(
        track["title"], track["artist"], album_ctx.get("title", ""),
        track.get("duration", 0),
    ))

    if precomputed_candidates is not None:
        candidates = list(precomputed_candidates)
    else:
        auto, alternatives = await find_track(
            track, album_artist=album_ctx.get("artist"), accept_lossy=accept_lossy,
        )
        candidates = []
        if auto:
            candidates.append(auto.result)
        candidates.extend(a.result for a in alternatives)

    if not candidates:
        lyrics_task.cancel()
        if accept_lossy:
            raise RuntimeError(
                f"No mp3/m4a fallback either for {track['artist']} — {track['title']}"
            )
        raise RuntimeError(
            f"No FLAC found on Soulseek for {track['artist']} — {track['title']}"
        )

    t0 = time.monotonic()
    res = await _try_candidates(
        candidates, track, album_ctx, album_dir, cover_data, lyrics_task,
    )
    if not res:
        raise RuntimeError("All candidate peers failed.")
    filepath, size, fmt, chosen = res
    elapsed = time.monotonic() - t0
    speed = (size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    logger.info(
        "Track: %s — %s | %.1fMB in %.1fs (%.1f MB/s) [%s] from %s",
        track["artist"], track["title"], size / (1024 * 1024),
        elapsed, speed, fmt, chosen.username,
    )
    return filepath, True, fmt


async def download_album(
    album_id: str,
    dest_dir: str,
    progress: ProgressCallback = None,
    album: dict | None = None,
) -> dict:
    """Download an album. Returns
    ``{album_dir, downloaded, skipped, failed, total, format, with_lyrics}``.
    """
    if album is None:
        album = await fetch_album(album_id)
        await enrich_genres(album)

    artist = _sanitize(album["artist"])
    title = _sanitize(album["title"])
    album_dir = _ensure_album_dir(dest_dir, artist, title)

    cover_data = await _download_cover(album.get("cover_uuid", ""), album_dir)

    total = len(album["tracks"])
    album_t0 = time.monotonic()
    album_bytes = 0
    downloaded = 0
    skipped = 0
    failed: list[tuple[str, str]] = []
    with_lyrics = 0
    format_label = ""
    logger.info("Album: %s — %s (%d tracks)", album["artist"], album["title"], total)

    existing_map: dict[str, str] = {}
    to_download: list[tuple[int, dict]] = []
    for i, track in enumerate(album["tracks"], 1):
        existing = await asyncio.to_thread(_find_existing_track, album_dir, track)
        if existing:
            existing_map[track["id"]] = existing
        else:
            to_download.append((i, track))

    if existing_map:
        patch_tracks = [(i, t) for i, t in enumerate(album["tracks"], 1)
                        if t["id"] in existing_map]
        results = await asyncio.gather(*[
            _patch_missing_tags(existing_map[t["id"]], t, album)
            for _, t in patch_tracks
        ], return_exceptions=True)
        for (i, t), added in zip(patch_tracks, results):
            if isinstance(added, BaseException):
                logger.warning("  [%d/%d] %s — patch failed: %s",
                               i, total, t["title"], added)
            elif added:
                logger.info("  [%d/%d] %s — patched: %s",
                            i, total, t["title"], ", ".join(added))
            else:
                logger.info("  [%d/%d] %s — already exists, skipping",
                            i, total, t["title"])
        skipped = len(existing_map)

    if not to_download:
        return _result_dict(album_dir, downloaded, skipped, failed, total,
                            format_label, with_lyrics)

    # Folder-match wins outright when one peer has every track of the album:
    # single queue slot, no cross-version mixes, fewer flaky connections.
    chosen_per_track: dict[str, SearchResult] = {}
    track_alts: dict[str, list[SearchResult]] = {}
    folder_match, _ = await find_album(album)
    if folder_match and folder_match.missing_count == 0:
        track_ids_needed = {t["id"] for _, t in to_download}
        for tr, sr in zip(album["tracks"], folder_match.matched_files):
            if tr["id"] in track_ids_needed and sr is not None:
                chosen_per_track[tr["id"]] = sr
        logger.info("Album-folder match: peer=%s score=%.1f covers %d/%d tracks",
                    folder_match.folder.username, folder_match.score,
                    len(chosen_per_track), len(track_ids_needed))

    # Per-track search for whatever the folder match didn't cover. Alts go
    # into the retry pool used when the primary peer stalls.
    needs_per_track = [(i, t) for (i, t) in to_download
                       if t["id"] not in chosen_per_track]
    if needs_per_track:
        per_track_results = await find_tracks_concurrent(
            [t for _, t in needs_per_track],
            album_artist=album["artist"],
        )
        for (i, t), (auto, alts) in zip(needs_per_track, per_track_results):
            chosen = auto.result if auto else (alts[0].result if alts else None)
            if chosen is not None:
                chosen_per_track[t["id"]] = chosen
            track_alts[t["id"]] = [
                a.result for a in alts if (auto is None or a is not auto)
            ]

    # Prefetch lyrics in parallel; the per-track download awaits its own task.
    lyrics_tasks: dict[str, asyncio.Task] = {
        t["id"]: asyncio.create_task(fetch_lyrics(
            t["title"], t["artist"], album["title"], t.get("duration", 0),
        ))
        for _, t in to_download
    }

    # Download tracks sequentially — slskd handles its own per-peer queue.
    download_total = len(to_download)
    for download_idx, (i, track) in enumerate(to_download, 1):
        primary = chosen_per_track.get(track["id"])
        candidates: list[SearchResult] = []
        if primary is not None:
            candidates.append(primary)
        candidates.extend(track_alts.get(track["id"], []))

        # Forward per-chunk transfer info to the album-level callback so the
        # bot can render speed/ETA in the status message.
        async def _on_track_progress(pct: float, speed_bps: int, eta_sec: int,
                                      _i=i, _title=track["title"]):
            if progress:
                with contextlib.suppress(Exception):
                    await progress(
                        _i, total, download_idx, download_total, _title,
                        {"pct": pct, "speed_bps": speed_bps, "eta_sec": eta_sec},
                    )

        # Initial heartbeat — slskd may stall a few seconds before the first
        # transfer-progress event fires, so the user sees movement immediately.
        if progress:
            with contextlib.suppress(Exception):
                await progress(i, total, download_idx, download_total, track["title"], None)

        if not candidates:
            failed.append((track["title"], "no Soulseek match"))
            lyrics_tasks[track["id"]].cancel()
            continue

        try:
            t0 = time.monotonic()
            res = await _try_candidates(
                candidates, track, album, album_dir, cover_data,
                lyrics_tasks.get(track["id"]),
                on_progress=_on_track_progress,
            )
            if res is None:
                raise RuntimeError("no peer returned a usable file")
            filepath, size, fmt, chosen = res
            elapsed = time.monotonic() - t0
            mb = size / (1024 * 1024)
            speed = mb / elapsed if elapsed > 0 else 0
            if not format_label:
                format_label = fmt
            logger.info(
                "  [%d/%d] %s — %.1fMB in %.1fs (%.1f MB/s) [%s] from %s",
                i, total, track["title"], mb, elapsed, speed, fmt, chosen.username,
            )
            album_bytes += size
            downloaded += 1
            try:
                if lyrics_tasks[track["id"]].done():
                    if lyrics_tasks[track["id"]].result():
                        with_lyrics += 1
            except Exception:
                pass
        except Exception as e:
            logger.warning("  [%d/%d] track %s — failed: %s",
                           i, total, track["title"], e)
            failed.append((track["title"], str(e)))

    elapsed = time.monotonic() - album_t0
    mb = album_bytes / (1024 * 1024)
    avg_speed = mb / elapsed if elapsed > 0 else 0
    logger.info(
        "Album done: %s — %s | %.0fMB, %d downloaded, %d skipped, %d failed in %.0fs (%.1f MB/s)",
        album["artist"], album["title"], mb, downloaded, skipped, len(failed),
        elapsed, avg_speed,
    )

    return _result_dict(album_dir, downloaded, skipped, failed, total,
                        format_label, with_lyrics)


def _result_dict(album_dir, downloaded, skipped, failed, total, fmt, with_lyrics) -> dict:
    return {
        "album_dir": album_dir,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "total": total,
        "format": fmt,
        "with_lyrics": with_lyrics,
    }
